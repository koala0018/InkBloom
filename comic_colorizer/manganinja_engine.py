from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from .colorizer import ColorSettings, RegionReferenceMatcher, _rgb
from .paths import MODELS, ROOT


class MangaNinjaColorizer:
    """GPU reference-following colorizer backed by official MangaNinja."""

    def __init__(self, references: list[Path], settings: ColorSettings):
        if not references:
            raise ValueError("人物精细模式必须上传至少一张彩色人物样例")
        self.references = references
        self.settings = settings
        self.matcher = RegionReferenceMatcher(references)
        self.model_root = MODELS / "manganinja"
        self.python = self.model_root / "runtime" / "Scripts" / "python.exe"
        self.source = self.model_root / "source"
        self.checkpoints = self.model_root / "checkpoints"
        if not (self.model_root / "READY").exists() or not self.python.exists():
            raise RuntimeError(
                f"MangaNinja 尚未安装。请运行：powershell -ExecutionPolicy Bypass -File \"{ROOT / 'install-manganinja.ps1'}\""
            )

    def colorize_batch(
        self,
        pages: list[Path],
        output_dir: Path,
        callback: Callable[[int, int], None] | None = None,
    ) -> list[Path]:
        staging = output_dir / "manganinja-input"
        raw_output = output_dir / "manganinja-output"
        staging.mkdir(parents=True, exist_ok=True)
        raw_output.mkdir(parents=True, exist_ok=True)
        paired_references: list[Path] = []

        for index, page in enumerate(pages, 1):
            target_rgb = _rgb(page)
            reference_index = self.matcher.best_reference_index(target_rgb)
            reference_copy = staging / f"page_{index:05d}_reference.png"
            Image.open(self.references[reference_index]).convert("RGB").save(reference_copy)
            paired_references.append(reference_copy)

        command = [
            str(self.python),
            str(self.source / "infer.py"),
            "--output_dir", str(raw_output),
            "--denoise_steps", "24",
            "--seed", "2026",
            "--pretrained_model_name_or_path", str(self.checkpoints / "StableDiffusion"),
            "--image_encoder_path", str(self.checkpoints / "models" / "clip-vit-large-patch14"),
            "--controlnet_model_name_or_path", str(self.checkpoints / "models" / "control_v11p_sd15_lineart"),
            "--annotator_ckpts_path", str(self.checkpoints / "models" / "Annotators"),
            "--manga_reference_unet_path", str(self.checkpoints / "MangaNinjia" / "reference_unet.pth"),
            "--manga_main_model_path", str(self.checkpoints / "MangaNinjia" / "denoising_unet.pth"),
            "--manga_controlnet_model_path", str(self.checkpoints / "MangaNinjia" / "controlnet.pth"),
            "--point_net_path", str(self.checkpoints / "MangaNinjia" / "point_net.pth"),
            "--input_reference_paths",
            *[str(path) for path in paired_references],
            "--input_lineart_paths",
            *[str(path) for path in pages],
            "--guidance_scale_ref", "0.0001",
        ]
        environment = os.environ.copy()
        environment["HF_HOME"] = str(self.model_root / "huggingface")
        environment["TORCH_HOME"] = str(self.model_root / "torch")
        environment["XFORMERS_FORCE_DISABLE_TRITON"] = "1"
        log_path = ROOT / "InkBloom-MangaNinja.log"
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.run(
                command,
                cwd=self.source,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=max(600, 180 * len(pages)),
            )
        if process.returncode != 0:
            raise RuntimeError(f"MangaNinja 推理失败，请查看 {log_path}")

        results: list[Path] = []
        for index, (page, reference) in enumerate(zip(pages, paired_references), 1):
            raw = raw_output / f"{reference.stem}_colorized.png"
            if not raw.exists():
                raise RuntimeError(f"MangaNinja 没有生成第 {index} 页")
            original = _rgb(page)
            generated = _rgb(raw)
            generated = cv2.resize(
                generated, (original.shape[1], original.shape[0]), interpolation=cv2.INTER_CUBIC
            )
            original_lab = cv2.cvtColor(original.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
            generated_lab = cv2.cvtColor(generated.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
            generated_lab[..., 0] = original_lab[..., 0]
            restored = np.clip(cv2.cvtColor(generated_lab, cv2.COLOR_LAB2RGB) * 255.0, 0, 255)
            dark_ink = np.clip((18.0 - original_lab[..., 0]) / 16.0, 0, 1)[..., None]
            restored = restored * (1 - dark_ink) + original.astype(np.float32) * dark_ink
            destination = output_dir / f"colored_{index:05d}.jpg"
            Image.fromarray(restored.astype(np.uint8)).save(destination, quality=95, subsampling=0)
            results.append(destination)
            if callback:
                callback(index, len(pages))
        return results
