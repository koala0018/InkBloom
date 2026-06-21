from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading

import cv2
import numpy as np
from PIL import Image

from .paths import resource_path


@dataclass
class ColorSettings:
    engine: str = "ai"
    saturation: float = 0.82
    strength: float = 0.88
    line_protection: float = 0.92


class ReferencePaletteColorizer:
    """Fast, deterministic CPU reference colorizer with edge-aware chroma propagation."""

    def __init__(self, reference: Path | None, settings: ColorSettings):
        self.settings = settings
        self.palette = self._extract_palette(reference) if reference else self._default_palette()

    @staticmethod
    def _default_palette() -> np.ndarray:
        return np.array([
            [245, 203, 181], [198, 116, 93], [87, 62, 72], [84, 122, 153],
            [112, 151, 116], [224, 181, 91], [151, 109, 151], [55, 62, 75],
        ], dtype=np.uint8)

    def _extract_palette(self, reference: Path) -> np.ndarray:
        image = cv2.imread(str(reference), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法读取参考图")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        scale = min(1.0, 640 / max(image.shape[:2]))
        if scale < 1:
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
        mask = (hsv[..., 1] > 32) & (hsv[..., 2] > 35) & (hsv[..., 2] < 250)
        pixels = image[mask]
        if len(pixels) < 64:
            pixels = image.reshape(-1, 3)
        if len(pixels) > 25000:
            rng = np.random.default_rng(2026)
            pixels = pixels[rng.choice(len(pixels), 25000, replace=False)]
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.5)
        _, labels, centers = cv2.kmeans(
            pixels.astype(np.float32), 10, None, criteria, 5, cv2.KMEANS_PP_CENTERS
        )
        counts = np.bincount(labels.ravel(), minlength=len(centers))
        centers = centers[np.argsort(counts)[::-1]]
        return np.clip(centers, 0, 255).astype(np.uint8)

    def colorize(self, source: Path, destination: Path) -> None:
        gray_rgb = np.asarray(Image.open(source).convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(gray_rgb, cv2.COLOR_RGB2GRAY)
        height, width = gray.shape

        palette_lab = cv2.cvtColor(self.palette[None, :, :], cv2.COLOR_RGB2LAB)[0].astype(np.float32)
        palette_lab = palette_lab[np.argsort(palette_lab[:, 0])]
        smooth = cv2.bilateralFilter(gray, 9, 28, 28)

        # Tone bands plus low-frequency spatial variation keep adjacent regions distinct.
        tone = smooth.astype(np.float32) / 255.0
        yy, xx = np.mgrid[0:height, 0:width]
        spatial = (np.sin(xx / 91.0) + np.cos(yy / 113.0)) * 0.08
        index = np.clip(((tone + spatial) * len(palette_lab)).astype(np.int32), 0, len(palette_lab) - 1)
        chroma = palette_lab[index, 1:3]
        chroma = chroma.astype(np.float32)
        chroma = np.stack(
            [cv2.bilateralFilter(chroma[..., channel], 11, 35, 35) for channel in range(2)],
            axis=-1,
        )

        lab = np.empty((height, width, 3), dtype=np.float32)
        lab[..., 0] = gray
        neutral = 128.0
        lab[..., 1:] = neutral + (chroma - neutral) * self.settings.saturation
        rgb = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)

        # Ink and screentones remain crisp by restoring original luminance and dark lines.
        line_mask = np.clip((92.0 - gray.astype(np.float32)) / 92.0, 0, 1)[..., None]
        original = gray_rgb.astype(np.float32)
        mixed = rgb.astype(np.float32) * self.settings.strength + original * (1 - self.settings.strength)
        mixed = mixed * (1 - line_mask * self.settings.line_protection) + original * line_mask * self.settings.line_protection
        Image.fromarray(np.clip(mixed, 0, 255).astype(np.uint8)).save(destination, quality=92, subsampling=0)


_session = None
_session_lock = threading.Lock()


def _ai_session():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort

            model_path = resource_path("assets/models/eccv16-colorizer.onnx")
            if not model_path.exists():
                raise FileNotFoundError("AI 模型缺失，请使用完整便携版或下载模型资产")
            options = ort.SessionOptions()
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 4) - 1))
            _session = ort.InferenceSession(
                str(model_path), options, providers=["CPUExecutionProvider"]
            )
    return _session


class NeuralReferenceColorizer:
    """ECCV16 neural colorizer with reference-image chroma alignment."""

    def __init__(self, reference: Path | None, settings: ColorSettings):
        self.settings = settings
        self.reference_stats = self._reference_stats(reference) if reference else None
        self.session = _ai_session()

    @staticmethod
    def _reference_stats(reference: Path) -> tuple[np.ndarray, np.ndarray]:
        rgb = np.asarray(Image.open(reference).convert("RGB"), dtype=np.float32) / 255.0
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
        chroma = lab[..., 1:]
        mask = np.linalg.norm(chroma, axis=2) > 7
        values = chroma[mask] if mask.sum() > 64 else chroma.reshape(-1, 2)
        return values.mean(axis=0), np.maximum(values.std(axis=0), 4.0)

    def colorize(self, source: Path, destination: Path) -> None:
        rgb8 = np.asarray(Image.open(source).convert("RGB"), dtype=np.uint8)
        rgb = rgb8.astype(np.float32) / 255.0
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
        l_full = lab[..., 0]
        l_small = cv2.resize(l_full, (256, 256), interpolation=cv2.INTER_AREA)
        tensor = l_small[None, None].astype(np.float32)
        output = self.session.run(["ab_channels"], {"l_channel": tensor})[0][0]
        ab = output.transpose(1, 2, 0)
        ab = cv2.resize(
            ab, (rgb8.shape[1], rgb8.shape[0]), interpolation=cv2.INTER_CUBIC
        )

        content_mask = (l_full > 8) & (l_full < 96)
        values = ab[content_mask] if content_mask.sum() > 64 else ab.reshape(-1, 2)
        pred_mean = values.mean(axis=0)
        pred_std = np.maximum(values.std(axis=0), 2.0)
        if self.reference_stats is not None:
            ref_mean, ref_std = self.reference_stats
            scale = np.clip(ref_std / pred_std, 0.65, 1.75)
            aligned = (ab - pred_mean) * scale + pred_mean
            aligned += (ref_mean - pred_mean) * 0.42
            ab = ab * 0.32 + aligned * 0.68
        ab *= self.settings.saturation
        # Keep paper and speech balloons neutral while retaining light skin tones.
        paper_chroma = np.clip((100.0 - l_full) / 5.0, 0.0, 1.0)[..., None]
        ab *= paper_chroma

        result_lab = np.dstack((l_full, np.clip(ab, -110, 110))).astype(np.float32)
        colored = cv2.cvtColor(result_lab, cv2.COLOR_LAB2RGB)
        colored = np.clip(colored * 255.0, 0, 255)
        original = rgb8.astype(np.float32)
        line_mask = np.clip((35.0 - l_full) / 35.0, 0, 1)[..., None]
        mixed = colored * self.settings.strength + original * (1 - self.settings.strength)
        mixed = mixed * (1 - line_mask * self.settings.line_protection) + original * line_mask * self.settings.line_protection
        Image.fromarray(np.clip(mixed, 0, 255).astype(np.uint8)).save(
            destination, quality=92, subsampling=0
        )


def make_colorizer(reference: Path | None, settings: ColorSettings):
    if settings.engine == "fast":
        return ReferencePaletteColorizer(reference, settings)
    return NeuralReferenceColorizer(reference, settings)
