import numpy as np
import tqdm
import random
import wandb
import PIL
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

import itertools
from diffusers import DiffusionPipeline
from peft import LoraConfig
from diffusers.loaders import StableDiffusionLoraLoaderMixin
from peft.utils import get_peft_model_state_dict, set_peft_model_state_dict
from diffusers.utils import (
    check_min_version,
    convert_state_dict_to_diffusers,
    convert_unet_state_dict_to_peft,
    is_wandb_available,
)
import safetensors

from src.models.adapter import *
from src.common.schedulers import CosineAnnealingWarmUpRestartsPTI
from src.common.train_utils import *

class DoraPerformTuningTrainer():
    def __init__(self,
                 cfg,
                 device,
                 train_loader,
                 logger,
                 sd_model,
                 placeholder_ids,
                 ):
        super().__init__()
        
        self.device = device
        self.cfg = cfg
        self.placeholder_ids = placeholder_ids
        
        self.vae = sd_model.vae.to(self.device)
        self.unet = sd_model.unet.to(self.device)
        self.noise_scheduler = sd_model.scheduler

        self.train_loader = train_loader
        
        
        self.tokenizer1 = sd_model.tokenizer
        self.tokenizer2 = sd_model.tokenizer_2
        self.text_encoder1 = sd_model.text_encoder.to(self.device)
        self.text_encoder2 = sd_model.text_encoder_2.to(self.device)
        
        self.logger = logger
        
        self.inject_lora(cfg.lora)
        self.optimizer = self._build_optimizer(cfg.optimizer_type,cfg.optimizer)
        cfg.scheduler.T_0 = len(train_loader)
        self.scheduler = self._build_scheduler(self.optimizer,cfg.scheduler)

        self.dtype = torch.float16
        if cfg.dtype == 'fp32':
            self.dtype = torch.float32
        
        self.epoch = 0
        self.step = 0
        
    def _build_optimizer(self, optimizer_type, optimizer_cfg):
        if optimizer_type == 'adamw':
            return optim.AdamW(self.params_to_optimize, **optimizer_cfg)
        elif optimizer_type == 'adam':
            return optim.Adam(self.params_to_optimize, **optimizer_cfg)
        elif optimizer_type == 'sgd':
            return optim.SGD(self.params_to_optimize, **optimizer_cfg)
        else:
            raise NotImplementedError
        
    def _build_scheduler(self, optimizer, scheduler_cfg):
        return CosineAnnealingWarmUpRestartsPTI(optimizer=optimizer, **scheduler_cfg)
    
    def inject_lora(self,lora_cfg):
        lora_unet_target_modules={"CrossAttention", "Attention", "GEGLU"}
        lora_clip_target_modules={"CLIPAttention"}
        
        #lora_unet_target_modules = ( lora_unet_target_modules | UNET_EXTENDED_TARGET_REPLACE )
        
        unet_lora_config = LoraConfig(
            use_dora=True,
            r=lora_cfg.lora_rank,
            lora_alpha=lora_cfg.lora_rank,
            init_lora_weights='gaussian',
            target_modules=["to_k", "to_q", "to_v", "to_out.0"]
        )
        #self.unet.requires_grad_(False)
        
        self.unet.add_adapter(unet_lora_config)
        unet_lora_params = list(filter(lambda p: p.requires_grad, self.unet.parameters()))
        params_to_optimize = [
        {"params": unet_lora_params, "lr": lora_cfg.unet_lr},
        ]
        
        self.text_encoder1.requires_grad_(False)
        self.text_encoder2.requires_grad_(False)
        
        text_encoder1_lora_params, _ = inject_trainable_lora(
            self.text_encoder1,
            target_replace_module=lora_clip_target_modules,
            r=lora_cfg.lora_rank
        )
        
        params_to_optimize += [
            {
                "params": itertools.chain(*text_encoder1_lora_params),
                "lr": lora_cfg.text_encoder_lr,
            }
        ]
        
        text_encoder2_lora_params, _ = inject_trainable_lora(
            self.text_encoder2,
            target_replace_module=lora_clip_target_modules,
            r=lora_cfg.lora_rank
        )

        params_to_optimize += [
            {
                "params": itertools.chain(*text_encoder2_lora_params),
                "lr": lora_cfg.text_encoder_lr,
            }
        ]
        
        self.params_to_optimize = params_to_optimize
        
    def get_sigmas(self,timesteps, n_dim=4, dtype=torch.float32):
        sigmas = self.noise_scheduler.sigmas.to(device=self.device, dtype=dtype)
        schedule_timesteps = self.noise_scheduler.timesteps.to(self.device)
        timesteps = timesteps.to(self.device)

        step_indices = [(schedule_timesteps == t).nonzero().item() for t in timesteps]

        sigma = sigmas[step_indices].flatten()
        while len(sigma.shape) < n_dim:
            sigma = sigma.unsqueeze(-1)
        return sigma
        
    def train(self):
        cfg = self.cfg
        num_epochs = cfg.pt_epochs
        loss_type = cfg.loss
        self.logger.log_to_wandb(self.step)
        
        for epoch in range(num_epochs):
            self.unet.train()
            self.text_encoder1.train()
            self.text_encoder2.train()
            print(f'Epoch: {epoch}')
            for step, batch in enumerate(tqdm.tqdm(self.train_loader)):
                
                train_logs = {}
                
                img = batch['pixel_values'].to(self.device,dtype=self.dtype)
                
                # convert images to latent space
                
                latents = self.vae.encode(img).latent_dist.sample().detach()

                latents = latents * self.vae.config.scaling_factor
                # smaple noise that we'll add to the latents
                noise = torch.randn_like(latents)
                batch_size = latents.shape[0]
                # Sample a random timestep for each image
                indices = torch.randint(0, self.noise_scheduler.config.num_train_timesteps, (batch_size,))
                timesteps = self.noise_scheduler.timesteps[indices].to(device=self.device)

                noisy_latents = self.noise_scheduler.add_noise(latents,noise,timesteps)
                
                sigmas = self.get_sigmas(timesteps, len(noisy_latents.shape), noisy_latents.dtype)
                
                inp_noisy_latents = noisy_latents / ((sigmas**2 + 1) ** 0.5)
                
                # text embedding
                encoder_hidden_states_1 = (
                    self.text_encoder1(batch['input_ids_1'].to(self.device), output_hidden_states=True).hidden_states[-2].to(dtype=self.dtype)
                )

                encoder_output_2 = self.text_encoder2(
                batch["input_ids_2"].reshape(batch["input_ids_1"].shape[0], -1).to(self.device), output_hidden_states=True
                )
                encoder_hidden_states_2 = encoder_output_2.hidden_states[-2].to(dtype=self.dtype)

                original_size = [
                    (batch["original_size"][0][i].item(), batch["original_size"][1][i].item())
                    for i in range(batch_size)
                ]

                crop_top_left = [
                    (batch["crop_top_left"][0][i].item(), batch["crop_top_left"][1][i].item())
                    for i in range(batch_size)
                ]
                target_size = (cfg.resolution, cfg.resolution)
                add_time_ids = torch.cat(
                    [
                        torch.tensor(original_size[i] + crop_top_left[i] + target_size)
                        for i in range(batch_size)
                    ]
                ).to(self.device, dtype=self.dtype)
                added_cond_kwargs = {"text_embeds": encoder_output_2[0], "time_ids": add_time_ids}
                encoder_hidden_states = torch.cat([encoder_hidden_states_1, encoder_hidden_states_2], dim=-1)
                
                # Predict the noise residual

                model_pred = self.unet(
                    inp_noisy_latents, timesteps, encoder_hidden_states, added_cond_kwargs=added_cond_kwargs
                ).sample

                
                # Get the target for loss depending on the prediction type
                if self.noise_scheduler.config.prediction_type == "epsilon":
                    model_pred = model_pred * (-sigmas) + noisy_latents
                    target = latents 
                    #target = noise
                elif self.noise_scheduler.config.prediction_type == "v_prediction":
                    model_pred = model_pred * (-sigmas / (sigmas**2 + 1) ** 0.5) + (
                                noisy_latents / (sigmas**2 + 1)
                            )
                    target = latents
                    #target = self.noise_scheduler.get_velocity(latents, noise, timesteps)
                else:
                    raise ValueError(f"Unknown prediction type {self.noise_scheduler.config.prediction_type}")

                loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")
                train_logs['pt/loss'] = loss.item()
                scheduler_lr = self.scheduler.get_lr()
                train_logs['pt/lr_unet'] = scheduler_lr[0]
                train_logs['pt/lr_text_encoder1'] = scheduler_lr[1]
                train_logs['pt/lr_text_encoder2'] = scheduler_lr[2]
                self.logger.update_log(**train_logs)
                
                if self.step % cfg.log_every == 0:
                    self.logger.log_to_wandb(self.step)
                
                loss.backward()
                self.optimizer.step()
                self.scheduler.step()
                self.optimizer.zero_grad()
                
                self.step += 1
            self.epoch += 1
        save_path = str(cfg.save_path)
        save_path = f'/content/drive/MyDrive/KoreaUniv/MLV_Lab/Summer_Project/model_dumps/model/{save_path}/'

        placeholder_tokens = self.tokenizer1.convert_ids_to_tokens(self.placeholder_ids)
        ti_path = save_path + 'TI.pt'
        learned_embeds_dict = {}
        for tok, tok_id in zip(placeholder_tokens, self.placeholder_ids):
            learned_embeds = self.text_encoder1.get_input_embeddings().weight[tok_id]
            print(
                f"Current Learned Embeddings for {tok}:, id {tok_id} ",
                learned_embeds[:4],
            )
            learned_embeds_dict[tok] = learned_embeds.detach().cpu()

        torch.save(learned_embeds_dict, ti_path)
        print("Ti saved to ", ti_path)
        
        unet_lora_layers_to_save = convert_state_dict_to_diffusers(get_peft_model_state_dict(self.unet))
        StableDiffusionLoraLoaderMixin.save_lora_weights(
                save_path,
                unet_lora_layers=unet_lora_layers_to_save
            )
        '''
        save_all(self.unet,self.text_encoder1,self.text_encoder2,save_path=save_path,placeholder_token_ids=self.placeholder_ids,placeholder_tokens=placeholder_tokens,
                 target_replace_module_text={"CLIPAttention"},target_replace_module_unet={"CrossAttention", "Attention", "GEGLU"})
                 
        '''
        # after training, make PipeLine
        pipeline = DiffusionPipeline.from_pretrained(
                "stabilityai/stable-diffusion-xl-base-1.0",
                text_encoder = self.text_encoder1,
                text_encoder_2 = self.text_encoder2,
                vae = self.vae,
                unet = self.unet,
                tokenizer = self.tokenizer1,
                tokenizer_2 = self.tokenizer2,
                torch_dtype = torch.float32,
                use_safetensors=True
                ).to(self.device)
        
        return pipeline