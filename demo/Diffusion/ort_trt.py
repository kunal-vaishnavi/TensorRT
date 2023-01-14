# Note: The <name>_ort_trt.onnx files were created as follows.
# 1) Copy the non-optimized model (i.e. <name>.onnx, not <name>.opt.onnx) and rename copy as <name>_ort_trt.onnx
# 2) Run ORT's symbolic shape inference tool on the <name>_ort_trt.onnx model
#    python3 -m onnxruntime.transformers.symbolic_shape_infer --input <name>_ort_trt.onnx --output <name>_ort_trt.onnx --auto_merge

import argparse
import numpy as np
import os
import onnx
import onnxruntime as ort
import torch
import time

from tqdm import tqdm
from onnxruntime.transformers.benchmark_helper import measure_memory
from transformers import CLIPTokenizer
from utilities import LMSDiscreteScheduler, DPMScheduler, save_image

def get_args():
    parser = argparse.ArgumentParser()
    # User settings
    parser.add_argument('--prompt', default='a beautiful photograph of Mt. Fuji during cherry blossom', type=str, help='Text prompt(s) to guide image generation')
    parser.add_argument('--batch-size', default=1, type=int)
    parser.add_argument('--height', default=512, type=int)
    parser.add_argument('--width', default=512, type=int)
    parser.add_argument('--num-warmup-runs', default=5, type=int)
    parser.add_argument('--denoising-steps', default=50, type=int, help='Number of inference steps')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--onnx-dir', default='./onnx', type=str, help='Output directory for ONNX export')

    # Pipeline configuration
    parser.add_argument('--device', default='cuda', type=str)
    parser.add_argument('--tokenizer', default=CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14"))
    parser.add_argument('--scheduler', default='lmsd', choices=['dpm', 'lmsd'])
    parser.add_argument('--io-binding', action='store_true')
    parser.add_argument('--denoising-prec', default='fp16', choices=['fp16', 'fp32'], help='Denoiser model precision')

    parser.add_argument('--num_images', default=1, type=int, help='Number of images per prompt')
    parser.add_argument('--negative-prompt', default=[''], help="The negative prompt(s) to guide the image generation.")
    parser.add_argument('--guidance-scale', default=7.5, type=float)
    parser.add_argument('--output-dir', default='./output', help='Output directory for logs and image artifacts')

    args = parser.parse_args()
    
    # Set prompt sizes
    #args.prompt = [args.prompt for _ in range(args.batch_size)]
    #args.negative_prompt = args.negative_prompt * args.batch_size

    # Set scheduler
    sched_opts = {'num_train_timesteps': 1000, 'beta_start': 0.00085, 'beta_end': 0.012}
    if args.scheduler == "lmsd":
        setattr(args, 'scheduler', LMSDiscreteScheduler(device=args.device, **sched_opts))
    else:
        setattr(args, 'scheduler', DPMScheduler(device=args.device, **sched_opts))
    args.scheduler.set_timesteps(args.denoising_steps)
    args.scheduler.configure()
    return args

def to_np(tensor, new_dtype):
    if torch.is_tensor(tensor):
        tensor = tensor.detach().cpu().numpy()
    assert isinstance(tensor, np.ndarray)
    return tensor.astype(new_dtype) if tensor.dtype != new_dtype else tensor

def to_pt(tensor, new_dtype):
    if isinstance(tensor, np.ndarray):
        tensor = torch.from_numpy(tensor)
    assert torch.is_tensor(tensor)
    return tensor.type(new_dtype) if tensor.dtype != new_dtype else tensor

def run_ort_trt(model_path, input_args, output_args=None, use_io_binding=False):
    """
    Run the stable diffusion pipeline with ORT-TRT.

    Args:
        model_path (str):
            Path to the ONNX model.
        input_args (dict):
            The input arguments needed to run the InferenceSession. This can be in two formats:

            With IO Binding:
                {
                    'name': <input name>,
                    'device_type': 'cuda',
                    'device_id': 0,
                    'element_type': <input element type>,
                    'shape': <input shape>,
                    'buffer_ptr': <pointer to allocated input>
                }

            Without IO Binding:
                {
                    '<model-input-1-name>': <model-input-1-data>,
                    '<model-input-2-name>': <model-input-2-data>,
                    ...
                }
        output_args (dict, optional):
            The output arguments needed for IO Binding. The format is:

            {
                'name': <output name>,
                'device_type': 'cuda',
                'device_id': 0,
                'element_type': <output element type>,
                'shape': <output shape>,
                'buffer_ptr': <pointer to allocated output>
            }
        use_io_binding (bool):
            Whether to use IO Binding or not
            
            For details on IO Binding, visit https://onnxruntime.ai/docs/api/python/api_summary.html#data-on-device
    """
    sess = ort.InferenceSession(model_path, providers=['TensorrtExecutionProvider', 'CUDAExecutionProvider'])
    #print(ort.get_all_providers())
    #print(sess.get_providers())
    if use_io_binding:
        io_binding = sess.io_binding()
        io_binding.bind_input(**input_args)
        io_binding.bind_output(**output_args)
        outputs = sess.run_with_iobinding(io_binding)
        #return io_binding.copy_outputs_to_cpu()
    else:
        outputs = sess.run(None, input_args)
    return outputs

# Modified from demo-diffusion.py
def run_pipeline(args):
    latent_height, latent_width = args.height // 8, args.width // 8
    generator = torch.Generator(device=args.device).manual_seed(args.seed)

    with torch.inference_mode(), torch.autocast(args.device):
        # latents need to be generated on the target device
        unet_channels = 4
        latents_shape = (args.batch_size * args.num_images, unet_channels, latent_height, latent_width)
        latents_dtype = torch.float32
        latents = torch.randn(latents_shape, device=args.device, dtype=latents_dtype, generator=generator)

        # Scale the initial noise by the standard deviation required by the scheduler
        latents = latents * args.scheduler.init_noise_sigma

        torch.cuda.synchronize()
        start_time = time.time()

        # Tokenizer input
        torch.cuda.synchronize()
        clip_start_time = time.time()

        text_input_ids = args.tokenizer(
            args.prompt,
            padding="max_length",
            max_length=args.tokenizer.model_max_length,
            return_tensors="np",
        ).input_ids.astype(np.int32)

        # CLIP text encoder with text embeddings
        text_input_args = {"input_ids": text_input_ids}
        text_embeddings = args.clip_sess.run(None, text_input_args)[0]
        text_embeddings = to_pt(text_embeddings, torch.float32)

        # Duplicate text embeddings for each generation per prompt
        bs_embed, seq_len, _ = text_embeddings.shape
        text_embeddings = text_embeddings.repeat(1, args.num_images, 1)
        text_embeddings = text_embeddings.view(bs_embed * args.num_images, seq_len, -1)

        max_length = text_input_ids.shape[-1]
        uncond_input_ids = args.tokenizer(
            args.negative_prompt,
            padding="max_length",
            max_length=max_length,
            truncation=True,
            return_tensors="np",
        ).input_ids.astype(np.int32)

        # CLIP text encoder with uncond embeddings
        uncond_input_args = {"input_ids": uncond_input_ids}
        uncond_embeddings = args.clip_sess.run(None, uncond_input_args)[0]
        uncond_embeddings = to_pt(uncond_embeddings, torch.float32)

        # Duplicate unconditional embeddings for each generation per prompt
        seq_len = uncond_embeddings.shape[1]
        uncond_embeddings = uncond_embeddings.repeat(1, args.num_images, 1)
        uncond_embeddings = uncond_embeddings.view(args.batch_size * args.num_images, seq_len, -1)

        # Concatenate the unconditional and text embeddings into a single batch to avoid doing two forward passes for classifier free guidance
        text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        if args.denoising_prec == 'fp16':
            text_embeddings = text_embeddings.to(dtype=torch.float16)

        torch.cuda.synchronize()
        clip_end_time = time.time()

        torch.cuda.synchronize()
        unet_start_time = time.time()
        for step_index, timestep in enumerate(tqdm(args.scheduler.timesteps)):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2)
            # LMSDiscreteScheduler.scale_model_input()
            latent_model_input = args.scheduler.scale_model_input(latent_model_input, step_index)

            # predict the noise residual
            dtype = np.float16 if args.denoising_prec == 'fp16' else np.float32
            if timestep.dtype != torch.float32:
                timestep_float = timestep.float()
            else:
                timestep_float = timestep

            # UNet with sample, timestep, and encoder hidden states
            unet_args = {
                "sample": to_np(latent_model_input, np.float32),
                "timestep": np.array([to_np(timestep_float, np.float32)], dtype=np.float32),
                "encoder_hidden_states": to_np(text_embeddings, dtype)
            }
            noise_pred = args.unet_sess.run(None, unet_args)[0]
            noise_pred = to_pt(noise_pred, torch.float16).to(args.device)

            # Perform guidance
            noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
            noise_pred = noise_pred_uncond + args.guidance_scale * (noise_pred_text - noise_pred_uncond)

            latents = args.scheduler.step(noise_pred, latents, step_index, timestep)

        latents = 1. / 0.18215 * latents
        torch.cuda.synchronize()
        unet_end_time = time.time()

        # VAE with latents
        torch.cuda.synchronize()
        vae_start_time = time.time()

        vae_input_args = {"latent": to_np(latents, np.float32)}
        images = args.vae_sess.run(None, vae_input_args)[0]
        images = to_pt(images, torch.float16)

        torch.cuda.synchronize()
        vae_end_time = time.time()

        torch.cuda.synchronize()
        end_time = time.time()
        if not args.warmup:
            print('|------------|--------------|')
            print('| {:^10} | {:^12} |'.format('Module', 'Latency'))
            print('|------------|--------------|')
            print('| {:^10} | {:>9.2f} ms |'.format('CLIP', (clip_end_time - clip_start_time)*1000))
            print('| {:^10} | {:>9.2f} ms |'.format('UNet x '+str(args.denoising_steps), (unet_end_time - unet_start_time)*1000))
            print('| {:^10} | {:>9.2f} ms |'.format('VAE', (vae_end_time - vae_start_time)*1000))
            print('|------------|--------------|')
            print('| {:^10} | {:>10.2f} s |'.format('Pipeline', (end_time - start_time)))
            print('|------------|--------------|')

            # Save image
            image_name_prefix = 'sd-' + args.denoising_prec + ''.join(set(['-'+args.prompt[i].replace(' ','_')[:10] for i in range(args.batch_size)]))+'-'
            save_image(images, args.output_dir, image_name_prefix)

def main():
    args = get_args()
    print(args)
    os.environ['ORT_TENSORRT_FP16_ENABLE'] = '1'
    #one_gb = 1073741824
    #os.environ['ORT_TENSORRT_MAX_WORKSPACE_SIZE'] = str(int(one_gb * 5))

    # Load models and convert to FP16 with first inference passes to reduce latency
    batch_size = 1
    print("Loading CLIP model.")
    clip_sess = ort.InferenceSession('./onnx/clip_ort_trt.onnx', providers=['TensorrtExecutionProvider', 'CUDAExecutionProvider'])
    clip_sess.run(None, {'input_ids': np.zeros((batch_size, 77), dtype=np.int32)})
    setattr(args, 'clip_sess', clip_sess)

    print("Loading UNet model.")
    unet_sess = ort.InferenceSession('./onnx/unet_fp16_ort_trt.onnx', providers=['TensorrtExecutionProvider', 'CUDAExecutionProvider'])
    unet_args = {
        'sample': np.zeros((2*batch_size, 4, args.height // 8, args.width // 8), dtype=np.float32),
        'timestep': np.zeros((1,), dtype=np.float32),
        'encoder_hidden_states': np.zeros((2*batch_size, 77, 768), dtype=np.float16 if args.denoising_prec == 'fp16' else np.float32),
    }
    unet_sess.run(None, unet_args)
    setattr(args, 'unet_sess', unet_sess)

    print("Loading VAE model.")
    vae_sess = ort.InferenceSession('./onnx/vae_ort_trt.onnx', providers=['TensorrtExecutionProvider', 'CUDAExecutionProvider'])
    vae_sess.run(None, {'latent': np.zeros((batch_size, 4, args.height // 8, args.width // 8), dtype=np.float32)})
    setattr(args, 'vae_sess', vae_sess)

    # Warm up pipeline
    setattr(args, 'warmup', True)
    print("Warming up pipeline.")
    for _ in tqdm(range(args.num_warmup_runs)):
        run_pipeline(args)
    setattr(args, 'warmup', False)

    # Measure each batch size
    init_prompt = args.prompt
    init_negative_prompt = args.negative_prompt
    for bs in [1, 2, 4, 8, 16]:
        args.batch_size = bs
        print(f"\nBatch size = {bs}\n")

        args.prompt = [init_prompt for _ in range(args.batch_size)]
        args.negative_prompt = init_negative_prompt * args.batch_size

        # Measure latency
        print("Measuring latency.")
        run_pipeline(args)

        # Measure memory usage
        print("Measuring memory usage. Ignore any latency metrics or any images saved.")
        measure_memory(is_gpu=(args.device == 'cuda'), func=lambda: run_pipeline(args))
        print("Measured memory usage.")

if __name__ == '__main__':
    main()