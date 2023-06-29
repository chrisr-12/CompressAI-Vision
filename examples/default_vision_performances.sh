ENTRY_CMD='compressai-fcvcm/compressai_vision/run/eval_encdec.py'
MPEG_OIV6_SRC='/pa/home/hyomin.choi/Projects/compressai-fcvcm/compressai-fcvcm/vcm_testdata/mpeg-oiv6'
SFU_HW_SRC='/pa/home/hyomin.choi/Projects/compressai-fcvcm/compressai-fcvcm/vcm_testdata/SFU_HW_Obj'

cd ../../

# MPEGOIV6 - Detection with Faster RCNN
python ${ENTRY_CMD} --config-name=eval_example.yaml ++vision_model.arch=faster_rcnn_X_101_32x8d_FPN_3x ++dataset.type=Detectron2Dataset ++dataset.datacatalog=MPEGOIV6 ++dataset.config.root=${MPEG_OIV6_SRC} ++dataset.config.annotation_file=mpeg-oiv6-detection-coco.json ++dataset.config.dataset_name=mpeg-oiv6-detection ++evaluator.type=OIC-EVAL

# MPEGOIV6 - Segmentation with Mask RCNN
python ${ENTRY_CMD} --config-name=eval_example.yaml ++vision_model.arch=mask_rcnn_X_101_32x8d_FPN_3x ++dataset.type=Detectron2Dataset ++dataset.datacatalog=MPEGOIV6 ++dataset.config.root=${MPEG_OIV6_SRC} ++dataset.config.annotation_file=mpeg-oiv6-segmentation-coco.json ++dataset.config.dataset_name=mpeg-oiv6-segmentation ++evaluator.type=OIC-EVAL

# SFU - Segmentation with Faster RCNN
for SEQ in \
            'Traffic_2560x1600_30_val' \
            'Kimono_1920x1080_24_val' \
            'ParkScene_1920x1080_24_val' \
            'Cactus_1920x1080_50_val' \
            'BasketballDrive_1920x1080_50_val' \
            'BQTerrace_1920x1080_60_val' \
            'BasketballDrill_832x480_50_val' \
            'BQMall_832x480_60_val' \
            'PartyScene_832x480_50_val' \
            'RaceHorses_832x480_30_val' \
            'BasketballPass_416x240_50_val' \
            'BQSquare_416x240_60_val' \
            'BlowingBubbles_416x240_50_val' \
            'RaceHorses_416x240_30_val'
do
    python ${ENTRY_CMD} --config-name=eval_example.yaml ++vision_model.arch=faster_rcnn_X_101_32x8d_FPN_3x ++dataset.type=Detectron2Dataset ++dataset.datacatalog=SFUHW ++dataset.config.root=${SFU_HW_SRC}/${SEQ} ++dataset.config.annotation_file=${SEQ}.json ++dataset.config.dataset_name=sfu-hw-${SEQ} ++evaluator.type=COCO-EVAL
done
