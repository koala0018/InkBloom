from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from .documents import safe_component


@dataclass(frozen=True)
class AutoReference:
    page: Path
    reference: Path
    reference_index: int
    score: float
    quality: float


def page_color_metrics(path: Path) -> dict[str, float]:
    """Measure whether a page is useful as a color reference, not just colorful."""
    with Image.open(path) as image:
        rgb_u8 = np.asarray(image.convert("RGB").resize((320, 320)), dtype=np.uint8)
    rgb = rgb_u8.astype(np.float32) / 255.0
    gray = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2GRAY)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    chroma = np.linalg.norm(lab[..., 1:].reshape(-1, 2), axis=1)
    colorful_pixels = chroma[chroma > 7.0]
    colorful_ratio = float(len(colorful_pixels)) / max(float(chroma.size), 1.0)
    median_chroma = float(np.median(colorful_pixels)) if len(colorful_pixels) else 0.0
    strong_ratio = float(np.mean(chroma > 16.0))
    channel_spread = np.max(rgb_u8, axis=2) - np.min(rgb_u8, axis=2)
    saturated = (channel_spread > 12).astype(np.uint8)
    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        saturated,
        connectivity=8,
    )
    component_areas = sorted(
        (int(stats[index, cv2.CC_STAT_AREA]) for index in range(1, component_count)),
        reverse=True,
    )
    area = float(gray.size)
    largest_color_component = component_areas[0] / area if component_areas else 0.0
    top5_color_components = sum(component_areas[:5]) / area if component_areas else 0.0
    white_ratio = float(np.mean(gray > 242))
    near_white_ratio = float(np.mean(gray > 225))
    dark_ratio = float(np.mean(gray < 80))
    edge_ratio = float(np.mean(cv2.Canny(gray, 60, 160) > 0))
    score = (
        colorful_ratio * 0.65
        + min(median_chroma / 42.0, 1.0) * 0.25
        + strong_ratio * 0.10
    )
    # High-quality references are covers, color illustrations, or full-color
    # story pages. They have coherent color regions. Index/contents/copyright
    # pages tend to be mostly white with a few small colored logos or labels.
    layout_penalty = 0.0
    if near_white_ratio > 0.62 and top5_color_components < 0.20:
        layout_penalty += 0.35
    if white_ratio > 0.78:
        layout_penalty += 0.25
    if top5_color_components < 0.10:
        layout_penalty += 0.20
    if edge_ratio > 0.32 and largest_color_component < 0.18:
        layout_penalty += 0.12
    quality = max(0.0, score - layout_penalty)
    if largest_color_component > 0.28 or top5_color_components > 0.42:
        quality += 0.08
    return {
        "score": float(score),
        "quality": float(quality),
        "colorful_ratio": colorful_ratio,
        "strong_ratio": strong_ratio,
        "median_chroma": median_chroma,
        "white_ratio": white_ratio,
        "near_white_ratio": near_white_ratio,
        "dark_ratio": dark_ratio,
        "edge_ratio": edge_ratio,
        "largest_color_component": largest_color_component,
        "top5_color_components": top5_color_components,
    }


def color_page_score(path: Path) -> float:
    """Return a conservative colorfulness score for an extracted comic page."""
    return page_color_metrics(path)["score"]


def is_color_reference_page(path: Path) -> bool:
    """Avoid treating black/white screentone pages as color references."""
    metrics = page_color_metrics(path)
    if metrics["score"] < 0.16 or metrics["quality"] < 0.18:
        return False
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB").resize((192, 192)), dtype=np.float32)
    channel_spread = np.max(rgb, axis=2) - np.min(rgb, axis=2)
    return float(np.mean(channel_spread > 10.0)) > 0.10


def collect_auto_references(
    pages: list[Path],
    reference_dir: Path,
    first_reference_index: int,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[Path], dict[Path, list[AutoReference]]]:
    """Find official color pages and copy them into the reference directory.

    Pages are grouped by their extraction folder. A PDF containing several
    chapters is handled by detecting later color pages and using each as the
    first-priority reference for the following segment.
    """
    auto_dir = reference_dir / "auto-color-pages"
    discovered: list[Path] = []
    by_group: dict[Path, list[AutoReference]] = {}
    index = first_reference_index
    page_index = 0
    scanned = 0
    while page_index < len(pages):
        group = pages[page_index].parent
        group_end = page_index + 1
        while group_end < len(pages) and pages[group_end].parent == group:
            group_end += 1
        group_pages = pages[page_index:group_end]
        candidates: list[tuple[int, Path, float]] = []
        for local_index, page in enumerate(group_pages):
            metrics = page_color_metrics(page)
            score = metrics["score"]
            scanned += 1
            if on_progress and (scanned == 1 or scanned == len(pages) or scanned % 10 == 0):
                on_progress(
                    scanned,
                    len(pages),
                    f"正在识别本话彩页参考：{scanned}/{len(pages)}",
                )
            if score >= 0.16 and metrics["quality"] >= 0.18 and is_color_reference_page(page):
                candidates.append((local_index, page, score, metrics["quality"]))
        if candidates:
            group_refs: list[AutoReference] = []
            for local_index, page, score, quality in candidates:
                auto_dir.mkdir(parents=True, exist_ok=True)
                target = auto_dir / (
                    f"{index + 1:03d}_auto_{safe_component(group.name, 'chapter', 60)}_"
                    f"{local_index + 1:03d}{page.suffix.lower()}"
                )
                shutil.copy2(page, target)
                discovered.append(target)
                group_refs.append(
                    AutoReference(
                        page=page.resolve(),
                        reference=target.resolve(),
                        reference_index=index,
                        score=score,
                        quality=quality,
                    )
                )
                index += 1
            by_group[group] = group_refs
        page_index = group_end
    return discovered, by_group
