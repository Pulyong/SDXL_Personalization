import argparse
import os
from src.models.adapter.lora_utils import patch_pipe
from src.models import build_stable_diffusion
from diffusers import DiffusionPipeline
import hydra
import omegaconf
from hydra import compose, initialize
import torch

def main(args):
    initialize(version_base='1.3', config_path='./configs')
    cfg = compose(config_name='PivotalTuningInversion')
    def eval_resolver(s: str):
        return eval(s)
    omegaconf.OmegaConf.register_new_resolver("eval", eval_resolver)
    exp_name = str(cfg.exp_name)
    sd_model = build_stable_diffusion(cfg.model)
    device = "cuda:0"
    img_path = '/content/drive/MyDrive/KoreaUniv/MLV_Lab/Summer_Project/model_dumps/vis/'
    os.makedirs(img_path+cfg.exp_name,exist_ok=True)
    
    
    vae = sd_model[0].to(device)
    unet = sd_model[1].to(device)
    noise_scheduler = sd_model[2]

    if len(sd_model[3]) == 2:
        tokenizer1 = sd_model[3][0]
        tokenizer2 = sd_model[3][1]
        text_encoder1 = sd_model[4][0].to(device)
        text_encoder2 = sd_model[4][1].to(device)
    else:
        tokenizer1 = sd_model[3]
        text_encoder1 = sd_model[4].to(device)

    pipeline = DiffusionPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            text_encoder=text_encoder1,
            text_encoder_2=text_encoder2,
            vae=vae,
            unet=unet,
            tokenizer=tokenizer1,
            tokenizer_2=tokenizer2,
            torch_dtype=torch.float32,
            use_safetensors=True
            ).to(device)

    patch_pipe(
        pipeline,
        args.model_path
    )

    prompt = args.prompt or "<s1> <s1_1> A profile sitting next to a tree with a Christmas spirit."
    prompt2 = prompt
    prompt = str(prompt)
    prompt = prompt.replace('dog', '<s1> <s1_1>')
    images = []#

    for i in range(10):
        image = pipeline(
            prompt=prompt,
            num_inference_steps=40,
            denoising_end=0.8,
            output_type="latent", #
        ).images
        images.append(image)#
    #
    pipeline.to('cpu')
    torch.cuda.empty_cache()

    refiner = DiffusionPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-refiner-1.0",
        text_encoder_2=pipeline.text_encoder_2,
        vae=pipeline.vae,
        use_safetensors=True,
    ).to(device)
    #
    img_path = '/content/drive/MyDrive/KoreaUniv/MLV_Lab/Summer_Project/model_dumps/vis'
    os.makedirs(img_path, exist_ok=True)
    for i in range(10):
        image = refiner(
            prompt=prompt2,
            num_inference_steps=40,
            denoising_start=0.8,
            image=images[i],
        ).images[0]
        image.save(f'{img_path}/{exp_name}/exp_test_{i}.png','png')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test PTI with different model paths")
    parser.add_argument('--prompt', type=str, required=True, help='Prompt to generate images')
    parser.add_argument('--model_path', type=str, required=True, help='Path to the model to be patched')
    args = parser.parse_args()
    main(args)
