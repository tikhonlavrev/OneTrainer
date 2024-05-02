from typing import Iterable

import torch
from torch.nn import Parameter

from accelerate import Accelerator

from modules.model.StableDiffusionModel import StableDiffusionModel
from modules.modelSetup.BaseStableDiffusionSetup import BaseStableDiffusionSetup
from modules.util import create
from modules.util.TrainProgress import TrainProgress
from modules.util.config.TrainConfig import TrainConfig


class StableDiffusionFineTuneVaeSetup(
    BaseStableDiffusionSetup,
):
    def __init__(
            self,
            train_device: torch.device,
            temp_device: torch.device,
            debug_mode: bool,
    ):
        super(StableDiffusionFineTuneVaeSetup, self).__init__(
            train_device=train_device,
            temp_device=temp_device,
            debug_mode=debug_mode,
        )
        
        # Initialize the accelerator
        self.accelerator = Accelerator()
        
        # Define self.train_device as self.accelerator.device
        self.train_device = self.accelerator.device
        
        # Debug Print
        print(f"The StableDiffusionFineTuneVAE is using: {self.accelerator.state.device} with {self.accelerator.num_processes} processes")

    def create_parameters(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
    ) -> Iterable[Parameter]:
        return model.vae.decoder.parameters()

    def create_parameters_for_optimizer(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
    ) -> Iterable[Parameter] | list[dict]:
        return [
            self.create_param_groups(
                config,
                model.vae.decoder.parameters(),
                config.learning_rate,
            )
        ]

    def setup_model(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
    ):
        model.text_encoder.requires_grad_(False)
        model.vae.requires_grad_(False)
        model.vae.decoder.requires_grad_(True)
        model.unet.requires_grad_(False)

        model.optimizer = create.create_optimizer(
            self.create_parameters_for_optimizer(model, config), model.optimizer_state_dict, config
        )
        model.optimizer_state_dict = None

        model.ema = create.create_ema(
            self.create_parameters(model, config), model.ema_state_dict, config
        )
        model.ema_state_dict = None

        self._setup_optimizations(model, config)

    def setup_train_device(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
    ):
        accelerator = Accelerator()  # Initialize the accelerator

        model.text_encoder = accelerator.prepare(model.text_encoder)
        model.vae = accelerator.prepare(model.vae)
        model.unet = accelerator.prepare(model.unet)
        if model.depth_estimator is not None:
            model.depth_estimator = accelerator.prepare(model.depth_estimator)

        model.text_encoder.eval()
        model.vae.train()
        model.unet.eval()

    def predict(
            self,
            model: StableDiffusionModel,
            batch: dict,
            config: TrainConfig,
            train_progress: TrainProgress,
            *,
            deterministic: bool = False,
    ) -> dict:
        latent_image = batch['latent_image']
        image = batch['image']

        predicted_image = model.vae.decode(latent_image, return_dict=True).sample

        model_output_data = {
            'loss_type': 'target',
            'predicted': predicted_image,
            'target': image,
        }

        if config.debug_mode:
            with torch.no_grad():
                # image
                self._save_image(image, config.debug_dir + "/training_batches", "1-image", train_progress.global_step)

                # predicted image
                predicted_image_clamped = predicted_image.clamp(-1, 1)
                self._save_image(
                    predicted_image_clamped, config.debug_dir + "/training_batches", "2-predicted_image",
                    train_progress.global_step
                )

        model_output_data['prediction_type'] = model.noise_scheduler.config.prediction_type
        return model_output_data

    def after_optimizer_step(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
            train_progress: TrainProgress
    ):
        pass

    def report_learning_rates(
            self,
            model,
            config,
            scheduler,
            tensorboard
    ):
        lr = scheduler.get_last_lr()[0]
        lr = config.optimizer.optimizer.maybe_adjust_lrs([lr], model.optimizer)[0]
        tensorboard.add_scalar("lr/vae", lr, model.train_progress.global_step)
