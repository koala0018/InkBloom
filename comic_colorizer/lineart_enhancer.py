from __future__ import annotations

from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from .colorizer import ColorSettings
from .documents import result_name


ProgressCallback = Callable[[str, int, int, str], None]


def _read_gray(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        rgb = np.array(image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def _reference_profile(references: list[Path]) -> tuple[float, float]:
    if not references:
        return 0.50, 0.50
    densities: list[float] = []
    contrasts: list[float] = []
    for path in references[:6]:
        try:
            gray = _read_gray(path)
        except Exception:
            continue
        small = cv2.resize(gray, (512, 512), interpolation=cv2.INTER_AREA)
        edges = cv2.Canny(small, 80, 170)
        densities.append(float(np.mean(edges > 0)))
        contrasts.append(float(np.std(small) / 80.0))
    if not densities:
        return 0.50, 0.50
    density = float(np.clip(np.mean(densities) * 8.0, 0.25, 0.90))
    contrast = float(np.clip(np.mean(contrasts), 0.25, 0.95))
    return density, contrast


def enhance_lineart_page(
    source: Path,
    destination: Path,
    references: list[Path],
    settings: ColorSettings,
) -> None:
    """Visible, structure-preserving manga clean-up (not generative redraw)."""
    gray = _read_gray(source)
    ref_density, ref_contrast = _reference_profile(references)

    strength = float(np.clip(settings.lineart_strength, 0.0, 1.0))
    detail = float(np.clip(settings.lineart_detail, 0.0, 1.0))
    weight = float(np.clip(settings.lineart_weight, 0.0, 1.0))

    denoised = cv2.fastNlMeansDenoising(gray, None, h=3 + int(8 * (1.0 - detail)), templateWindowSize=7, searchWindowSize=21)
    clahe = cv2.createCLAHE(clipLimit=1.4 + ref_contrast * 1.4 + strength, tileGridSize=(8, 8))
    contrast = clahe.apply(denoised)

    blur = cv2.GaussianBlur(contrast, (0, 0), 0.9 + (1.0 - detail) * 0.7)
    sharp = cv2.addWeighted(contrast, 1.25 + strength * 0.55, blur, -(0.25 + strength * 0.55), 0)

    block = 31 + 2 * int(round((1.0 - detail) * 7))
    c_value = 5 + int(round((1.0 - ref_density) * 5 - weight * 3))
    binary = cv2.adaptiveThreshold(
        sharp,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        max(15, block | 1),
        c_value,
    )

    edges = cv2.Canny(sharp, 45 + int((1.0 - detail) * 35), 135 + int((1.0 - detail) * 55))
    edge_layer = 255 - cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1 if weight > 0.45 else 0)
    mixed = cv2.min(binary, edge_layer)

    if weight > 0.58:
        mixed = 255 - cv2.dilate(255 - mixed, np.ones((2, 2), np.uint8), iterations=1)
    elif weight < 0.35:
        mixed = cv2.morphologyEx(mixed, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)

    if strength > 0.45:
        mixed = cv2.medianBlur(mixed, 3)

    # Keep deliberate screentones and gray fills instead of converting the
    # whole page into harsh binary ink. The enhanced lines remain visibly
    # stronger while the source drawing still controls all geometry.
    base_blur = cv2.GaussianBlur(gray, (0, 0), 0.7 + (1.0 - detail) * 0.5)
    base = cv2.addWeighted(
        gray,
        1.08 + strength * 0.18,
        base_blur,
        -(0.08 + strength * 0.18),
        0,
    ).astype(np.float32)
    line_mask = (255.0 - mixed.astype(np.float32)) / 255.0
    line_mask = cv2.GaussianBlur(line_mask, (3, 3), 0.45)
    darken = (8.0 + strength * 20.0) * line_mask
    mixed = np.clip(base - darken, 0, 255).astype(np.uint8)

    # Only restore exterior paper white; enclosed white clothes, walls and
    # speech balloons keep their cleaned edges.
    bright = (gray > 248).astype(np.uint8)
    _count, labels = cv2.connectedComponents(bright, connectivity=8)
    border_labels = np.unique(
        np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1]))
    )
    exterior = np.isin(labels, border_labels[border_labels != 0])
    mixed[exterior] = 255

    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(mixed).convert("RGB").save(destination)


def enhance_pages(
    pages: list[Path],
    output_dir: Path,
    references: list[Path],
    settings: ColorSettings,
    callback: ProgressCallback | None = None,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    for index, page in enumerate(pages, 1):
        if callback:
            callback("redraw", index - 1, len(pages), f"第 {index} 页：正在增强线稿")
        target = output_dir / result_name(page, index, "lineart", ".png")
        enhance_lineart_page(page, target, references, settings)
        results.append(target)
        if callback:
            callback("redraw", index, len(pages), f"第 {index} 页线稿增强完成")
    return results
