# Copyright (c) 2022-2024, InterDigital Communications, Inc
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted (subject to the limitations in the disclaimer
# below) provided that the following conditions are met:

# * Redistributions of source code must retain the above copyright notice,
#   this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# * Neither the name of InterDigital Communications, Inc nor the names of its
#   contributors may be used to endorse or promote products derived from this
#   software without specific prior written permission.

# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY
# THIS LICENSE. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT
# NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import configparser
import errno
import json
import logging
import math
import os
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import torch
import torch.nn as nn
from torch import Tensor

from compressai_vision.codecs.utils import FpnUtils
from compressai_vision.model_wrappers import BaseWrapper
from compressai_vision.registry import register_codec
from compressai_vision.utils import time_measure
from compressai_vision.utils.dataio import PixelFormat, readwriteYUV
from compressai_vision.utils.external_exec import run_cmdline, run_cmdlines_parallel

from .encdec_utils import *
from .encdec_utils.png_yuv import PngFilesToYuvFileConverter, YuvFileToPngFilesConverter
from .utils import (
    MIN_MAX_DATASET,
    compute_frame_resolution,
    min_max_inv_normalization,
    min_max_normalization,
    tensor_to_tiled,
    tiled_to_tensor,
)


def get_filesize(filepath: Union[Path, str]) -> int:
    """
    Get the size of a file in bytes.
    Args:
        filepath (Union[Path, str]): The path to the file. Can be a string or a Path object.
    Returns:
        int: The size of the file in bytes.
    """
    return Path(filepath).stat().st_size


# TODO (fracape) belongs to somewhere else?
def load_bitstream(path):
    """
    Load a bitstream and return it as a "bytes" object.
    Args:
        path (str): path to the file containing the bitstream.
    Returns:
        bytes: The loaded bitstream.
    """
    with open(path, "rb") as fd:
        buf = BytesIO(fd.read())

    return buf.getvalue()


@register_codec("vtm")
class VTM(nn.Module):
    """Encoder/Decoder class for VVC - VTM reference software"""

    def __init__(
        self,
        vision_model: BaseWrapper,
        dataset: Dict,
        **kwargs,
    ):
        super().__init__()

        self.enc_cfgs = kwargs["encoder_config"]
        codec_paths = kwargs["codec_paths"]

        self.encoder_path = Path(codec_paths["encoder_exe"])
        self.decoder_path = Path(codec_paths["decoder_exe"])
        self.cfg_file = Path(codec_paths["cfg_file"])
        self.parcat_path = Path(codec_paths["parcat_exe"])  # optional

        self.parallel_encoding = self.enc_cfgs["parallel_encoding"]  # parallel option
        self.hash_check = self.enc_cfgs["hash_check"]  # md5 hash check
        self.stash_outputs = self.enc_cfgs["stash_outputs"]

        check_list_of_paths = [self.encoder_path, self.decoder_path, self.cfg_file]
        if self.parallel_encoding:  # miminum
            check_list_of_paths.append(self.parcat_path)

        for file_path in check_list_of_paths:
            if not file_path.is_file():
                raise ValueError(
                    f"Could not find path {file_path}. Consider specifying "
                    "++codec.codec_paths._root='/local/path/vtm-12.0'."
                )

        self.qp = self.enc_cfgs["qp"]
        self.eval_encode = kwargs["eval_encode"]

        self.dump = kwargs["dump"]
        self.fpn_sizes_json_dump = self.dump["fpn_sizes_json_dump"]
        self.vision_model = vision_model

        self.datacatalog = dataset.datacatalog
        self.dataset_name = dataset.config["dataset_name"]

        if self.datacatalog in MIN_MAX_DATASET:
            self.min_max_dataset = MIN_MAX_DATASET[self.datacatalog]
        elif self.dataset_name in MIN_MAX_DATASET:
            self.min_max_dataset = MIN_MAX_DATASET[self.dataset_name]
        else:
            raise ValueError("dataset not recognized for normalization")

        self.yuvio = readwriteYUV(device="cpu", format=PixelFormat.YUV400_10le)

        self.intra_period = self.enc_cfgs["intra_period"]
        self.frame_rate = 1
        if not self.datacatalog == "MPEGOIV6":
            config = configparser.ConfigParser()
            config.read(f"{dataset['config']['root']}/{dataset['config']['seqinfo']}")
            self.frame_rate = config["Sequence"]["frameRate"]

        self.logger = logging.getLogger(self.__class__.__name__)
        self.verbosity = kwargs["verbosity"]
        self.ffmpeg_loglevel = "error"
        logging_level = logging.WARN
        if self.verbosity == 1:
            logging_level = logging.INFO
        if self.verbosity >= 2:
            logging_level = logging.DEBUG
            self.ffmpeg_loglevel = "debug"

        self.logger.setLevel(logging_level)

        self.convert_input_to_yuv = PngFilesToYuvFileConverter(
            chroma_format=self.enc_cfgs["chroma_format"],
            input_bitdepth=self.enc_cfgs["input_bitdepth"],
            frame_rate=self.frame_rate,
            ffmpeg_loglevel=self.ffmpeg_loglevel,
            logger=self.logger,
        )

        self.convert_yuv_to_pngs = YuvFileToPngFilesConverter(
            datacatalog=self.datacatalog,
            logger=self.logger,
        )

        self.fpn_utils = FpnUtils()

        self.reset()

    # can be added to base class (if inherited) | Should we inherit from the base codec?
    @property
    def qp_value(self):
        return self.qp

    # can be added to base class (if inherited) | Should we inherit from the base codec?
    @property
    def eval_encode_type(self):
        return self.eval_encode

    def __del__(self):
        self.close_bitstream_file()

    def reset(self):
        self._header_writer = HeaderWriter()
        self._header_reader = HeaderReader()
        self._frame_info_buffer = []
        self._temp_io_buffer = BytesIO()
        self._bitstream_fd = None

    def open_bitstream_file(self, path, mode="rb"):
        self._bitstream_fd = open(path, mode)
        return self._bitstream_fd

    def close_bitstream_file(self):
        if self._bitstream_fd is not None:
            self._bitstream_fd.flush()
            self._bitstream_fd.close()
            self._bitstream_fd = None

    def get_encode_cmd(
        self,
        inp_yuv_path: Path,
        qp: int,
        bitstream_path: Path,
        width: int,
        height: int,
        nb_frames: int = 1,
        parallel_encoding: bool = False,
        hash_check: int = 0,
        chroma_format: str = "400",
        input_bitdepth: int = 10,
        output_bitdepth: int = 0,
    ) -> List[Any]:
        """
        Generates the command to encode a video file using VTM software.
        Args:
            inp_yuv_path (Path): The path to the input YUV file.
            qp (int): The quantization parameter.
            bitstream_path (Path): The path to the output bitstream file.
            width (int): The width of the video.
            height (int): The height of the video.
            nb_frames (int, optional): The number of frames in the video. Defaults to 1.
            parallel_encoding (bool, optional): Whether to perform parallel encoding. Defaults to False.
            hash_check (int, optional): The hash check value. Defaults to 0.
            chroma_format (str, optional): The chroma format of the video. Defaults to "400".
            input_bitdepth (int, optional): The bit depth of the input video. Defaults to 10.
            output_bitdepth (int, optional): The bit depth of the output video. Defaults to 0.
        Returns:
            List[Any]: the command line as a list.
        """
        level = 5.1 if nb_frames > 1 else 6.2  # according to MPEG's anchor
        if output_bitdepth == 0:
            output_bitdepth = input_bitdepth

        decodingRefreshType = 1 if self.intra_period >= 1 else 0
        base_cmd = [
            self.encoder_path,
            "-i",
            inp_yuv_path,
            "-c",
            self.cfg_file,
            "-q",
            qp,
            "-o",
            "/dev/null",
            "-wdt",
            width,
            "-hgt",
            height,
            "-fr",
            self.frame_rate,
            "-ts",  # temporal subsampling to prevent default period of 8 in all intra
            "1",
            "-v",
            "6",
            f"--Level={level}",
            f"--IntraPeriod={self.intra_period}",
            f"--InputChromaFormat={chroma_format}",
            f"--InputBitDepth={input_bitdepth}",
            f"--InternalBitDepth={output_bitdepth}",
            "--ConformanceWindowMode=1",  # needed?
            "-dph",  # md5 has,
            hash_check,
            f"--DecodingRefreshType={decodingRefreshType}",
        ]

        if parallel_encoding is False or nb_frames <= self.intra_period + 1:
            # No need for parallel encoding.
            cmd = [
                *base_cmd,
                f"--BitstreamFile={bitstream_path}",
                f"--FramesToBeEncoded={nb_frames}",
            ]
            cmd = [str(x) for x in cmd]
            self.logger.debug(cmd)
            cmds = [cmd]
        else:
            cmds = self._parallel_encode_cmd(base_cmd, bitstream_path, nb_frames)

        return cmds

    def _parallel_encode_cmd(
        self, base_cmd: List, bitstream_path: Path, nb_frames: int
    ):
        num_workers = round((nb_frames / self.intra_period) + 0.5)

        frame_offsets, frame_counts = _distribute_parallel_work(
            nb_frames, num_workers, self.intra_period
        )

        bitstream_path = Path(bitstream_path)

        cmds = []

        assert num_workers < 10**3  # Due to the string formatting below.

        for worker_idx, (frameSkip, framesToBeEncoded) in enumerate(
            zip(frame_offsets, frame_counts)
        ):
            worker_bitstream_path = (
                f"{bitstream_path.parent}/"
                f"{bitstream_path.stem}-part-{worker_idx:03d}{bitstream_path.suffix}"
            )

            cmd = [
                *base_cmd,
                f"--BitstreamFile={worker_bitstream_path}",
                f"--FrameSkip={frameSkip}",
                f"--FramesToBeEncoded={framesToBeEncoded}",
            ]

            cmd = [str(x) for x in cmd]
            self.logger.debug(cmd)
            cmds.append(cmd)

        return cmds

    def get_parcat_cmd(
        self,
        bitstream_path: Path,
    ) -> Tuple[List[Any], List[Path]]:
        """
        Returns a list of commands and bitstream lists needed to concatenate bitstream files.
        Args:
            bitstream_path (Path): The path to the bitstream file.
        Returns:
            Tuple[List[Any], List[Path]]: the command to concatenate the bitstream files in the folder.
        """
        bp = Path(bitstream_path)
        bitstream_lists = sorted(bp.parent.glob(f"{bp.stem}-part-*{bp.suffix}"))
        cmd = [self.parcat_path, *bitstream_lists, bitstream_path]
        cmd = [str(x) for x in cmd]
        self.logger.debug(cmd)
        return cmd, bitstream_lists

    def get_decode_cmd(
        self, yuv_dec_path: Path, bitstream_path: Path, output_bitdepth: int = 10
    ) -> List[Any]:
        """
        Get command line for decoding a video bitstream with an external VTM decoder.
        Args:
            yuv_dec_path (Path): The path to the output YUV file.
            bitstream_path (Path): The path to the video bitstream file.
            output_bitdepth (int, optional): The bitdepth of the output YUV file. Defaults to 10.
        Returns:
            List[Any]: command line arguments for decoding the video bitstream.
        """
        cmd = [
            f"{self.decoder_path}",
            "-b",
            f"{bitstream_path}",
            "-o",
            f"{yuv_dec_path}",
            "-d",
            f"{output_bitdepth}",
        ]
        self.logger.debug(cmd)
        return cmd

    def encode(
        self,
        x: Dict,
        codec_output_dir,
        bitstream_name,
        file_prefix: str = "",
        remote_inference=False,
    ) -> Dict:
        """
        Encodes the input data.
        Args:
            x (Dict): The input data to be encoded.
            codec_output_dir (str): The directory where the output bitstream will be saved.
            bitstream_name (str): The name of the output bitstream.
            file_prefix (str, optional): The prefix to be added to the output file name. Defaults to "".
            remote_inference (bool, optional): Indicates if the encoding is done remotely. Defaults to False.
        Returns:
            dict: A dictionary containing the bytes per frame and the path to the output bitstream.
        """

        self.reset()
        input_bitdepth = self.enc_cfgs["input_bitdepth"]
        output_bitdepth = self.enc_cfgs["output_bitdepth"]

        if file_prefix == "":
            file_prefix = f"{codec_output_dir}/{bitstream_name}"
        else:
            file_prefix = f"{codec_output_dir}/{bitstream_name}-{file_prefix}"

        print(f"\n-- encoding {file_prefix}", file=sys.stdout)

        # Conversion: reshape data to yuv domain (e.g. 420 or 400)
        if remote_inference:
            start = time.time()
            (yuv_in_path, nb_frames, frame_width, frame_height, file_prefix) = (
                self.convert_input_to_yuv(input=x, file_prefix=file_prefix)
            )
            conversion_time = time.time() - start
            self.logger.debug(f"conversion time:{conversion_time}")
        else:
            start = time.time()
            frames = self.fpn_utils.reshape_feature_pyramid_to_frame(
                x["data"], packing_all_in_one=True
            )

            # Generate json files with fpn sizes for the decoder
            # manually activate the following and run in encode_only mode
            if self.fpn_sizes_json_dump:
                self.dump_fpn_sizes_json(file_prefix, bitstream_name, codec_output_dir)

            # normalization wrt to the bitdepth of the input to VTM
            minv, maxv = self.min_max_dataset
            frames, mid_level = min_max_normalization(
                frames, minv, maxv, bitdepth=input_bitdepth
            )

            num_frames, *_ = frames.shape

            # Same minv, maxv for all frames.
            for _ in range(num_frames):
                frame_info = {
                    "minv": minv,
                    "maxv": maxv,
                }
                self._frame_info_buffer.append(frame_info)

            conversion_time = time.time() - start
            self.logger.debug(f"conversion time:{conversion_time}")

            nb_frames, frame_height, frame_width = frames.size()
            input_bitdepth = self.enc_cfgs["input_bitdepth"]
            chroma_format = self.enc_cfgs["chroma_format"]
            file_prefix = f"{file_prefix}_{frame_width}x{frame_height}_{self.frame_rate }fps_{input_bitdepth}bit_p{chroma_format}"
            yuv_in_path = f"{file_prefix}_input.yuv"

            self.yuvio.setWriter(
                write_path=yuv_in_path,
                frmWidth=frame_width,
                frmHeight=frame_height,
            )

            for frame in frames:
                self.yuvio.write_one_frame(frame, mid_level=mid_level)

        bitstream_path = Path(f"{file_prefix}.bin")
        logpath = Path(f"{file_prefix}_enc.log")
        cmds = self.get_encode_cmd(
            yuv_in_path,
            width=frame_width,
            height=frame_height,
            qp=self.qp,
            bitstream_path=bitstream_path,
            nb_frames=nb_frames,
            chroma_format=self.enc_cfgs["chroma_format"],
            input_bitdepth=self.enc_cfgs["input_bitdepth"],
            output_bitdepth=self.enc_cfgs["output_bitdepth"],
            parallel_encoding=self.parallel_encoding,
            hash_check=self.hash_check,
        )

        start = time.time()
        if len(cmds) > 1:  # post parallel encoding
            run_cmdlines_parallel(cmds, logpath=logpath)
        else:
            run_cmdline(cmds[0], logpath=logpath)
        enc_time = time.time() - start
        self.logger.debug(f"enc_time:{enc_time}")

        if len(cmds) > 1:  # post parallel encoding
            cmd, list_of_bitstreams = self.get_parcat_cmd(bitstream_path)
            run_cmdline(cmd)

            if self.stash_outputs:
                for partial in list_of_bitstreams:
                    Path(partial).unlink()

        assert Path(
            bitstream_path
        ).is_file(), f"bitstream {bitstream_path} was not created"

        if not remote_inference:
            inner_codec_bitstream = load_bitstream(bitstream_path)

            sequence_info = {
                "bitdepth": output_bitdepth,
                "frame_size": (frame_height, frame_width),
                "num_frames": nb_frames,
            }

            assert sequence_info["num_frames"] == len(self._frame_info_buffer)

            # Bistream header to make bitstream self-decodable
            fd = self._temp_io_buffer
            self._header_writer.write_sequence_info(fd, sequence_info)
            for frame_info in self._frame_info_buffer:
                self._header_writer.write_frame_info(fd, frame_info)

            pre_info_bitstream = self.get_io_buffer_contents()
            bitstream = pre_info_bitstream + inner_codec_bitstream

            with open(bitstream_path, "wb") as fw:
                fw.write(bitstream)

        if not self.dump["dump_yuv_input"]:
            Path(yuv_in_path).unlink()

        # to be compatible with the pipelines
        # per frame bits can be collected by parsing enc log to be more accurate
        avg_bytes_per_frame = get_filesize(bitstream_path) / nb_frames
        all_bytes_per_frame = [avg_bytes_per_frame] * nb_frames

        output = {
            "bytes": all_bytes_per_frame,
            "bitstream": str(bitstream_path),
        }
        enc_times = {
            "video": enc_time,
            "conversion": conversion_time,
        }

        mac_calculations = None  # no NN-related complexity calculation with std codecs

        return output, enc_times, mac_calculations

    def decode(
        self,
        bitstream_path: Path = None,
        codec_output_dir: str = "",
        file_prefix: str = "",
        org_img_size: Dict = None,
        remote_inference=False,
    ) -> Dict:
        """
        Decodes the bitstream and returns the output features .

        Args:
            bitstream_path (Path): The path to the bitstream file.
            codec_output_dir (str): The directory to store codec output.
            file_prefix (str): The prefix for the output files.
            org_img_size (Dict): The original image size.
            remote_inference (bool): Specifies if the remote inference pipeline is used.

        Returns:
            Dict: The dictionary of output features.
        """
        self.reset()

        bitstream_path = Path(bitstream_path)
        assert bitstream_path.is_file()

        output_file_prefix = bitstream_path.stem

        dec_path = codec_output_dir / "dec"
        dec_path.mkdir(parents=True, exist_ok=True)
        logpath = Path(f"{dec_path}/{output_file_prefix}_dec.log")
        yuv_dec_path = Path(f"{dec_path}/{output_file_prefix}_dec.yuv")

        print(f"\n-- decoding ${output_file_prefix}", file=sys.stdout)

        if remote_inference:  # remote inference pipeline
            bitdepth = get_raw_video_file_info(output_file_prefix.split("qp")[-1])[
                "bitdepth"
            ]

            cmd = self.get_decode_cmd(
                bitstream_path=bitstream_path,
                yuv_dec_path=yuv_dec_path,
                output_bitdepth=bitdepth,
            )
            self.logger.debug(cmd)

            start = time_measure()
            run_cmdline(cmd, logpath=logpath)
            dec_time = time_measure() - start
            self.logger.debug(f"dec_time:{dec_time}")

            self.convert_yuv_to_pngs(
                output_file_prefix, dec_path, yuv_dec_path, org_img_size
            )

            # output the list of file paths for each frame
            rec_frames = []
            if file_prefix == "":  # Video pipeline
                for file_path in sorted(dec_path.glob("*.png")):
                    rec_frames.append(str(file_path))
                # expecting the length of rec_frames are greather than 1
            else:  # Image pipeline
                for file_path in sorted(Path(dec_path).glob(f"*{file_prefix}*.png")):
                    rec_frames.append(str(file_path))

                assert (
                    file_prefix in rec_frames[0]
                ), f"Can't find a correct filename with {file_prefix} in {dec_path}"
                assert (
                    len(rec_frames) == 1
                ), f"Number of retrieved file must be 1, but got {len(rec_frames)}"

            conversion_time = 0
            output = {"file_names": rec_frames}

        else:  # split inference pipeline
            del org_img_size  # not needed in this pipeline
            bitstream_path_tmp = f"{codec_output_dir}/{output_file_prefix}_tmp.bin"

            bitstream_fd = self.open_bitstream_file(bitstream_path, "rb")

            # read header bitstream header
            sequence_info = self._header_reader.read_sequence_info(bitstream_fd)
            frame_infos = [
                self._header_reader.read_frame_info(bitstream_fd)
                for _ in range(sequence_info["num_frames"])
            ]
            bitdepth = sequence_info["bitdepth"]
            frame_height, frame_width = sequence_info["frame_size"]

            # we need this to read the std codec part of the bitstream
            with open(bitstream_path_tmp, "wb") as fw:
                fw.write(bitstream_fd.read())
                fw.flush()

            cmd = self.get_decode_cmd(
                bitstream_path=bitstream_path_tmp,
                yuv_dec_path=yuv_dec_path,
                output_bitdepth=bitdepth,
            )
            self.logger.debug(cmd)

            start = time_measure()
            run_cmdline(cmd, logpath=logpath)
            dec_time = time_measure() - start
            self.logger.debug(f"dec_time:{dec_time}")

            self.yuvio.setReader(
                read_path=str(yuv_dec_path),
                frmWidth=frame_width,
                frmHeight=frame_height,
            )

            # TODO (fracape) expects raw yuv400 coded on 8 or 16 bit
            factor = int((bitdepth + 7) / 8)
            nb_frames = get_filesize(yuv_dec_path) // (
                frame_width * frame_height * factor
            )

            rec_frames = []
            for i in range(nb_frames):
                rec_yuv = self.yuvio.read_one_frame(i)
                rec_frames.append(rec_yuv)

            rec_frames = torch.stack(rec_frames)

            start = time_measure()
            minv, maxv = self.min_max_dataset
            tol = dict(rel_tol=1e-4, abs_tol=1e-4)
            assert all(
                math.isclose(frame_info["minv"], minv, **tol)
                and math.isclose(frame_info["maxv"], maxv, **tol)
                for frame_info in frame_infos
            )
            rec_frames = min_max_inv_normalization(rec_frames, minv, maxv, bitdepth=10)

            # (fracape) should feature sizes be part of bitstream?
            thisdir = Path(__file__).parent
            if self.datacatalog == "MPEGOIV6":
                fpn_sizes = thisdir.joinpath(
                    f"../../data/mpeg-fcm/{self.datacatalog}/fpn-sizes/{self.dataset_name}/{file_prefix}.json"
                )
            else:
                fpn_sizes = thisdir.joinpath(
                    f"../../data/mpeg-fcm/{self.datacatalog}/fpn-sizes/{self.dataset_name}.json"
                )
            with fpn_sizes.open("r") as f:
                try:
                    json_dict = json.load(f)
                except json.decoder.JSONDecodeError as err:
                    print(f'Error reading file "{fpn_sizes}"')
                    raise err

            features = self.fpn_utils.reshape_frame_to_feature_pyramid(
                rec_frames,
                json_dict["fpn"],
                json_dict["subframe_heights"],
                packing_all_in_one=True,
            )

            conversion_time = time_measure() - start
            self.logger.debug(f"conversion_time:{conversion_time}")

            if not self.dump["dump_yuv_packing_dec"]:
                yuv_dec_path.unlink()
            if self.stash_outputs:
                Path(bitstream_path_tmp).unlink()

            output = {"data": features}

        dec_times = {
            "video": dec_time,
            "conversion": conversion_time,
        }

        mac_calculations = None  # no NN-related complexity calculation with std codecs

        return output, dec_times, mac_calculations

    def get_io_buffer_contents(self):
        return self._temp_io_buffer.getvalue()


@register_codec("hm")
class HM(VTM):
    """Encoder / Decoder class for HEVC - HM reference software"""

    def __init__(
        self,
        vision_model: BaseWrapper,
        dataset: Dict,
        **kwargs,
    ):
        super().__init__(vision_model, dataset, **kwargs)

    def get_encode_cmd(
        self,
        inp_yuv_path: Path,
        qp: int,
        bitstream_path: Path,
        width: int,
        height: int,
        nb_frames: int = 1,
        parallel_encoding: bool = False,
        hash_check: int = 0,
        chroma_format: str = "400",
        input_bitdepth: int = 10,
        output_bitdepth: int = 0,
    ) -> List[Any]:
        """
        Generates the command to encode a video using the specified parameters.
        Args:
            inp_yuv_path (Path): The path to the input YUV file.
            qp (int): The quantization parameter.
            bitstream_path (Path): The path to the output bitstream file.
            width (int): The width of the video.
            height (int): The height of the video.
            nb_frames (int, optional): The number of frames in the video. Defaults to 1.
            parallel_encoding (bool, optional): Whether to enable parallel encoding. Defaults to False.
            hash_check (int, optional): The hash check value. Defaults to 0.
            chroma_format (str, optional): The chroma format of the video. Defaults to "400".
            input_bitdepth (int, optional): The bitdepth of the input video. Defaults to 10.
            output_bitdepth (int, optional): The bitdepth of the output video. Defaults to 0.
        Returns:
            List[Any]: commands line to encode the video.
        """
        level = 5.1 if nb_frames > 1 else 6.2  # according to MPEG's anchor
        if output_bitdepth == 0:
            output_bitdepth = input_bitdepth

        decodingRefreshType = 1 if self.intra_period >= 1 else 0
        base_cmd = [
            self.encoder_path,
            "-i",
            inp_yuv_path,
            "-c",
            self.cfg_file,
            "-q",
            qp,
            "-o",
            "/dev/null",
            "-wdt",
            width,
            "-hgt",
            height,
            "-fr",
            self.frame_rate,
            "-ts",  # temporal subsampling to prevent default period of 8 in all intra
            "1",
            f"--Level={level}",
            f"--IntraPeriod={self.intra_period}",
            f"--InputChromaFormat={chroma_format}",
            f"--InputBitDepth={input_bitdepth}",
            f"--InternalBitDepth={output_bitdepth}",
            "--ConformanceWindowMode=1",  # needed?
            f"--DecodingRefreshType={decodingRefreshType}",
        ]

        if parallel_encoding is False or nb_frames <= self.intra_period + 1:
            # No need for parallel encoding.
            base_cmd.append(f"--BitstreamFile={bitstream_path}")
            base_cmd.append(f"--FramesToBeEncoded={nb_frames}")
            cmd = list(map(str, base_cmd))
            self.logger.debug(cmd)
            cmds = [cmd]
        else:
            cmds = self._parallel_encode_cmd(base_cmd, bitstream_path, nb_frames)

        return cmds


@register_codec("jm")
class JM(VTM):
    """Encoder / Decoder class for AVC - JM reference software"""

    def __init__(
        self,
        vision_model: BaseWrapper,
        dataset: Dict,
        **kwargs,
    ):
        super().__init__(vision_model, dataset, **kwargs)

        self.default_cfg_file = Path(kwargs["codec_paths"]["default_cfg_file"])

    def get_encode_cmd(
        self,
        inp_yuv_path: Path,
        qp: int,
        bitstream_path: Path,
        width: int,
        height: int,
        nb_frames: int = 1,
        parallel_encoding: bool = False,
        hash_check: int = 0,
        chroma_format: str = "400",
        input_bitdepth: int = 10,
        output_bitdepth: int = 0,
    ) -> List[Any]:
        """
        Generates the command to encode a video using the specified parameters.
        Args:
            inp_yuv_path (Path): The path to the input YUV file.
            qp (int): The quantization parameter.
            bitstream_path (Path): The path to the output bitstream file.
            width (int): The width of the video.
            height (int): The height of the video.
            nb_frames (int, optional): The number of frames in the video. Defaults to 1.
            parallel_encoding (bool, optional): Whether to enable parallel encoding. Defaults to False.
            hash_check (int, optional): The hash check value. Defaults to 0.
            chroma_format (str, optional): The chroma format of the video. Defaults to "400".
            input_bitdepth (int, optional): The bitdepth of the input video. Defaults to 10.
            output_bitdepth (int, optional): The bitdepth of the output video. Defaults to 0.
        Returns:
            List[Any]: commands line to encode the video.
        """

        assert parallel_encoding == False, "JM does not support parallel coding"
        level = 62  # enable large frames
        if output_bitdepth == 0:
            output_bitdepth = input_bitdepth

        # decodingRefreshType = 1 if self.intra_period >= 1 else 0
        cmd = [
            self.encoder_path,
            "-d",
            self.default_cfg_file,
            "-f",
            self.cfg_file,
            "-p",
            f"InputFile={inp_yuv_path}",
            "-p",
            f"QPISlice={qp}",
            "-p",
            f"QPPSlice={qp}",
            "-p",
            f"QPBSlice={qp}",
            "-p",
            f"SourceWidth={width}",
            "-p",
            f"OutputWidth={width}",
            "-p",
            f"SourceHeight={height}",
            "-p",
            f"OutputHeight={height}",
            "-p",
            f"FrameRate={self.frame_rate}",
            "-p",
            f"IntraPeriod={self.intra_period}",
            "-p",
            "YUVFormat=0",
            "-p",
            f"SourceBitDepthLuma={output_bitdepth}",
            "-p",
            "ChromaWeightSupport=0",
            "-p",
            f"OutputFile={bitstream_path}",
            "-p",
            f"FramesToBeEncoded={nb_frames}",
            "-p",
            f"LevelIDC={level}",
        ]
        cmd = list(map(str, cmd))
        self.logger.debug(cmd)
        return [cmd]

    def get_decode_cmd(
        self, yuv_dec_path: Path, bitstream_path: Path, output_bitdepth=None
    ) -> List[Any]:
        del output_bitdepth
        cmd = [
            f"{self.decoder_path}",
            "-p",
            f"InputFile={bitstream_path}",
            "-p",
            f"OutputFile={yuv_dec_path}",
            "-p",
            "WriteUV=0",
        ]

        self.logger.debug(cmd)
        return cmd


@register_codec("vvenc")
class VVENC(VTM):
    """Encoder / Decoder class for VVC - vvenc/vvdec  software"""

    def __init__(
        self,
        vision_model: BaseWrapper,
        dataset_name: "str" = "",
        **kwargs,
    ):
        super().__init__(vision_model, dataset_name, **kwargs)

    def get_encode_cmd(
        self,
        inp_yuv_path: Path,
        qp: int,
        bitstream_path: Path,
        width: int,
        height: int,
        nb_frames: int = 1,
    ) -> List[Any]:
        """
        Generate a command to encode a YUV video file using VVENCs.
        Args:
            inp_yuv_path (Path): The path to the input YUV video file.
            qp (int): The quantization parameter for the encoding process.
            bitstream_path (Path): The path to save the encoded bitstream.
            width (int): The width of the video frame.
            height (int): The height of the video frame.
            nb_frames (int, optional): The number of frames to encode (default is 1).
        Returns:
            List[Any]: A list of strings representing the encoding command.
        """
        cmd = [
            self.encoder_path,
            "-i",
            inp_yuv_path,
            "-q",
            qp,
            "--output",
            bitstream_path,
            "--size",
            f"{width}x{height}",
            "--framerate",
            self.frame_rate,
            "--frames",
            nb_frames,
            "--format",
            "yuv420_10",
            "--preset",
            "fast",
        ]
        return list(map(str, cmd))


class HeaderWriter:
    def __init__(self):
        pass

    def write_sequence_info(self, fd, sequence_info):
        expected_keys = [
            "bitdepth",
            "frame_size",
            "num_frames",
        ]
        assert set(sequence_info.keys()) == set(expected_keys), sequence_info.keys()

        return sum(
            [
                write_uchars(fd, (sequence_info["bitdepth"],)),
                write_uints(fd, sequence_info["frame_size"]),
                write_uints(fd, (sequence_info["num_frames"],)),
            ]
        )

    def write_frame_info(self, fd, frame_info):
        expected_keys = [
            "minv",
            "maxv",
        ]
        assert set(frame_info.keys()) == set(expected_keys)

        return sum(
            [
                write_float32(fd, (frame_info["minv"],)),
                write_float32(fd, (frame_info["maxv"],)),
            ]
        )


class HeaderReader:
    def __init__(self):
        self._sequence_info = None
        self._num_frames_read = 0

    def read_sequence_info(self, fd):
        [bitdepth] = read_uchars(fd, 1)
        frame_size = read_uints(fd, 2)
        [num_frames] = read_uints(fd, 1)

        sequence_info = {
            "bitdepth": bitdepth,
            "frame_size": frame_size,
            "num_frames": num_frames,
        }

        self._sequence_info = sequence_info

        return sequence_info

    def read_frame_info(self, fd):
        frame_id = self._num_frames_read
        [minv] = read_float32(fd, 1)
        [maxv] = read_float32(fd, 1)

        frame_info = {
            "frame_id": frame_id,
            "minv": minv,
            "maxv": maxv,
        }

        self._num_frames_read += 1

        return frame_info


def _distribute_parallel_work(num_frames: int, num_workers: int, intra_period: int):
    """Distributes frame encoding work.

    worker[i] is to be assigned frames in the interval
    [offsets[i], offsets[i] + counts[i]).
    """
    offsets = []
    counts = []

    offset = 0
    num_remaining = num_frames

    # WARN: Current implementation assumes one worker per intra period.
    # assert num_workers == num_frames // intra_period

    for _ in range(num_workers):
        assert num_remaining > 0

        # NOTE: The first and last frames must both be intra-frames, hence the +1.
        count = min(intra_period + 1, num_remaining)

        offsets.append(offset)
        counts.append(count)

        offset += intra_period
        num_remaining -= intra_period

    assert offsets[-1] + counts[-1] == num_frames

    return offsets, counts
