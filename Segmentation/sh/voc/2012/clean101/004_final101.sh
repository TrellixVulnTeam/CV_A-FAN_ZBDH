GPU=${1}
EXP=FINAL_101_110207
SELAYER=3
SDLAYER=concat
GAMMASD=0.8
AdvWeight=0.3

SELAYER=2
GAMMASE=0.01
MIX=01
python -u main_aug_final.py --year 2012 --crop_val --batch_size 4 \
--model deeplabv3plus_resnet101 \
--pertub_idx_sd ${SDLAYER} \
--pertub_idx_se ${SELAYER} \
--adv_loss_weight_sd ${AdvWeight} \
--gamma_se ${GAMMASE} \
--gamma_sd ${GAMMASD} \
--gpu_id ${GPU} \
--mix_layer ${MIX} \
${EXP}

EXP=FINAL_101_110208
SELAYER=2
GAMMASE=0.03
MIX=01
python -u main_aug_final.py --year 2012 --crop_val --batch_size 4 \
--model deeplabv3plus_resnet101 \
--pertub_idx_sd ${SDLAYER} \
--pertub_idx_se ${SELAYER} \
--adv_loss_weight_sd ${AdvWeight} \
--gamma_se ${GAMMASE} \
--gamma_sd ${GAMMASD} \
--gpu_id ${GPU} \
--mix_layer ${MIX} \
${EXP}