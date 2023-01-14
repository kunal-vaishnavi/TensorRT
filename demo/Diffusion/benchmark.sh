#!/bin/bash

PROMPT="a beautiful photograph of Mt. Fuji during cherry blossom"
HEIGHT=512
WIDTH=512
SEED=0
PRECISION="fp16"
BATCH_SIZE=$1

echo python3 demo-diffusion.py --prompt "${PROMPT}" --batch_size $BATCH_SIZE --height $HEIGHT --width $WIDTH --precision $PRECISION --seed $SEED
LD_PRELOAD=${PLUGIN_LIBS} python3 demo-diffusion.py "${PROMPT}" \
        --repeat-prompt $BATCH_SIZE \
	--height $HEIGHT \
	--width $WIDTH \
	--denoising-prec $PRECISION \
	--seed $SEED \
	--hf-token=$HF_TOKEN \
	-v
