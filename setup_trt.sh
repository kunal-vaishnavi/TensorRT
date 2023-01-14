#!/bin/bash

#################################
# Build TensorRT Plugins library
#################################

export TRT_OSSPATH=/workspace

cd $TRT_OSSPATH
mkdir -p build && cd build
cmake .. -DTRT_OUT_DIR=$PWD/out
cd plugin
make -j$(nproc)

export PLUGIN_LIBS="$TRT_OSSPATH/build/out/libnvinfer_plugin.so"

############################
# Install required packages
############################

cd $TRT_OSSPATH/demo/Diffusion
pip install --upgrade pip
pip3 install -r requirements.txt
pip install py3nvml

pip uninstall -y onnxruntime
pip install onnxruntime-gpu

# Create output directories
mkdir -p onnx engine output

############################
# Export Hugging Face token
############################
export HF_TOKEN=$1

