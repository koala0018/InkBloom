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


def color_page_score(path: Path) -> float:
    """Return a conservative colorfulness score for an extracted comic page."""
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB").resize((320, 320)), dtype=np.float32) / 255.0
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    chroma = np.linalg.norm(lab[..., 1:].reshape(-1, 2), axis=1)
    colorful_pixels = chroma[chroma > 7.0]
    colorful_ratio = float(len(colorful_pixels)) / max(float(chroma.size), 1.0)
    if len(colorful_pixels) == 0:
        return 0.0
    median_chroma = float(np.median(colorful_pixels))
    strong_ratio = float(np.mean(chroma > 16.0))
    return colorful_ratio * 0.65 + min(median_chroma / 42.0, 1.0) * 0.25 + strong_ratio * 0.10


def is_color_reference_page(path: Path) -> bool:
    """Avoid treating black/white screentone pages as color references."""
    score = color_page_score(path)
    if score < 0.16:
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
            score = color_page_score(page)
            scanned += 1
            if on_progress and (scanned == 1 or scanned == len(pages) or scanned % 10 == 0):
                on_progress(
                    scanned,
                    len(pages),
                    f"正在识别本话彩页参考：{scanned}/{len(pages)}",
                )
            if score >= 0.16 and is_color_reference_page(page):
                candidates.append((local_index, page, score))
        if candidates:
            group_refs: list[AutoReference] = []
            for local_index, page, score in candidates:
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
                    )
                )
                index += 1
            by_group[group] = group_refs
        page_index = group_end
    return discovered, by_group
