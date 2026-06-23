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
    engine: str = "style2paints"
    saturation: float = 1.05
    strength: float = 0.95
    line_protection: float = 0.82
    reference_strength: float = 0.90
    s2p_stage: str = "careful"
    s2p_finish: str = "blended_smoothed"
    s2p_save_layers: bool = True
    s2p_hint_points: str = "[]"
    lineart_enhance: bool = False
    lineart_strength: float = 0.65
    lineart_detail: float = 0.60
    lineart_weight: float = 0.55


def _rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)


class RegionReferenceMatcher:
    """Transfer colors from multiple references using page and region similarity."""

    FEATURE_WEIGHTS = np.array([3.2, 1.6, 2.0, 1.15, 1.15, 0.25], np.float32)

    def __init__(self, references: list[Path]):
        if not references:
            raise ValueError("至少需要一张彩色样例图")
        self.references = [self._describe(_rgb(path), include_color=True) for path in references]

    @staticmethod
    def _resize(rgb: np.ndarray, max_side: int = 720) -> np.ndarray:
        scale = min(1.0, max_side / max(rgb.shape[:2]))
        if scale == 1.0:
            return rgb
        return cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    @staticmethod
    def _superpixels(rgb: np.ndarray) -> tuple[np.ndarray, int]:
        region = max(10, int(max(rgb.shape[:2]) / 48))
        slic = cv2.ximgproc.createSuperpixelSLIC(
            rgb, algorithm=cv2.ximgproc.SLICO, region_size=region, ruler=9.0
        )
        slic.iterate(10)
        slic.enforceLabelConnectivity(min_element_size=18)
        return slic.getLabels(), slic.getNumberOfSuperpixels()

    @classmethod
    def _describe(cls, rgb: np.ndarray, include_color: bool) -> dict[str, np.ndarray]:
        small = cls._resize(rgb)
        lab = cv2.cvtColor(small.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 45, 135).astype(np.float32) / 255.0
        labels, count = cls._superpixels(small)
        flat = labels.ravel()
        counts = np.maximum(np.bincount(flat, minlength=count).astype(np.float32), 1)

        def mean(values: np.ndarray) -> np.ndarray:
            return np.bincount(flat, weights=values.ravel(), minlength=count) / counts

        l_value = lab[..., 0]
        l_mean = mean(l_value)
        l_std = np.sqrt(np.maximum(mean(l_value * l_value) - l_mean * l_mean, 0))
        yy, xx = np.mgrid[0 : small.shape[0], 0 : small.shape[1]]
        features = np.column_stack(
            (
                l_mean / 100.0,
                l_std / 35.0,
                mean(edges),
                mean(xx.astype(np.float32)) / max(1, small.shape[1] - 1),
                mean(yy.astype(np.float32)) / max(1, small.shape[0] - 1),
                np.log1p(counts) / 9.0,
            )
        ).astype(np.float32)

        hist = cv2.calcHist([gray], [0], None, [16], [0, 256]).ravel().astype(np.float32)
        hist /= max(hist.sum(), 1)
        page = np.r_[hist, edges.mean(), gray.mean() / 255.0].astype(np.float32)
        thumb = cv2.resize(gray, (192, 192), interpolation=cv2.INTER_AREA).astype(np.float32)
        thumb = (thumb - thumb.mean()) / max(thumb.std(), 1.0)
        result = {
            "labels": labels,
            "features": features,
            "page": page,
            "thumb": thumb,
            "rgb": small,
        }
        if include_color:
            result["colors"] = np.column_stack((mean(lab[..., 1]), mean(lab[..., 2]))).astype(np.float32)
        return result

    def transfer(self, target_rgb: np.ndarray) -> np.ndarray:
        target = self._describe(target_rgb, include_color=False)
        structure_scores = np.array(
            [float(np.mean(reference["thumb"] * target["thumb"])) for reference in self.references]
        )
        distances = np.array(
            [np.mean((reference["page"] - target["page"]) ** 2) for reference in self.references]
        )
        selected = np.argsort(distances)[: min(4, len(self.references))]
        ref_features = np.concatenate([self.references[index]["features"] for index in selected])
        ref_colors = np.concatenate([self.references[index]["colors"] for index in selected])
        # For different-layout pages, neutral paper regions are poor donors for clothes/skin.
        colorful = np.linalg.norm(ref_colors, axis=1) > 4.0
        if np.count_nonzero(colorful) >= 32:
            ref_features = ref_features[colorful]
            ref_colors = ref_colors[colorful]

        weighted_ref = ref_features * self.FEATURE_WEIGHTS
        weighted_target = target["features"] * self.FEATURE_WEIGHTS
        segment_colors = np.empty((len(weighted_target), 2), dtype=np.float32)
        for start in range(0, len(weighted_target), 192):
            block = weighted_target[start : start + 192]
            squared = np.sum((block[:, None, :] - weighted_ref[None, :, :]) ** 2, axis=2)
            k = min(5, squared.shape[1])
            nearest = np.argpartition(squared, k - 1, axis=1)[:, :k]
            near_dist = np.take_along_axis(squared, nearest, axis=1)
            weights = 1.0 / (near_dist + 0.018)
            colors = ref_colors[nearest]
            segment_colors[start : start + len(block)] = np.sum(
                colors * weights[..., None], axis=1
            ) / np.sum(weights, axis=1, keepdims=True)

        low_ab = segment_colors[target["labels"]]
        mapped = cv2.resize(
            low_ab,
            (target_rgb.shape[1], target_rgb.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )
        best_structure = int(np.argmax(structure_scores))
        best_reference = self.references[best_structure]
        same_aspect = abs(
            target_rgb.shape[1] / target_rgb.shape[0]
            - best_reference["rgb"].shape[1] / best_reference["rgb"].shape[0]
        ) < 0.035
        if same_aspect and structure_scores[best_structure] > 0.52:
            reference_lab = cv2.cvtColor(
                best_reference["rgb"].astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB
            )
            direct = cv2.resize(
                reference_lab[..., 1:],
                (target_rgb.shape[1], target_rgb.shape[0]),
                interpolation=cv2.INTER_CUBIC,
            )
            direct_weight = np.clip((structure_scores[best_structure] - 0.52) / 0.28, 0.55, 1.0)
            mapped = mapped * (1.0 - direct_weight) + direct * direct_weight

        guide = cv2.cvtColor(target_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        mapped = np.stack(
            [
                cv2.ximgproc.guidedFilter(
                    guide=guide,
                    src=mapped[..., channel].astype(np.float32),
                    radius=13,
                    eps=0.0025,
                )
                for channel in range(2)
            ],
            axis=-1,
        )
        return mapped

    def best_reference_index(self, target_rgb: np.ndarray) -> int:
        target = self._describe(target_rgb, include_color=False)
        structure = np.array(
            [float(np.mean(reference["thumb"] * target["thumb"])) for reference in self.references]
        )
        page_distance = np.array(
            [np.mean((reference["page"] - target["page"]) ** 2) for reference in self.references]
        )
        page_score = 1.0 - page_distance / max(float(page_distance.max()), 1e-6)
        return int(np.argmax(structure * 0.72 + page_score * 0.28))


_session = None
_session_lock = threading.Lock()


def _ai_session():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort

            model_path = resource_path("assets/models/eccv16-colorizer.onnx")
            if not model_path.exists():
                raise FileNotFoundError("AI 模型缺失，请使用完整便携版")
            options = ort.SessionOptions()
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 4) - 1))
            _session = ort.InferenceSession(
                str(model_path), options, providers=["CPUExecutionProvider"]
            )
    return _session


class MultiReferenceColorizer:
    def __init__(self, references: list[Path], settings: ColorSettings):
        self.settings = settings
        self.matcher = RegionReferenceMatcher(references) if references else None
        self.session = _ai_session() if settings.engine == "ai" else None

    def _automatic_ab(self, l_full: np.ndarray) -> np.ndarray:
        l_small = cv2.resize(l_full, (256, 256), interpolation=cv2.INTER_AREA)
        tensor = l_small[None, None].astype(np.float32)
        output = self.session.run(["ab_channels"], {"l_channel": tensor})[0][0]
        return cv2.resize(
            output.transpose(1, 2, 0),
            (l_full.shape[1], l_full.shape[0]),
            interpolation=cv2.INTER_CUBIC,
        )

    @staticmethod
    def _default_ab(l_full: np.ndarray) -> np.ndarray:
        palette = np.array(
            [[18, 17], [32, 8], [8, -24], [-13, -18], [25, -20], [15, 30]],
            dtype=np.float32,
        )
        index = np.clip((l_full / 100 * len(palette)).astype(np.int32), 0, len(palette) - 1)
        return palette[index]

    def colorize(self, source: Path, destination: Path) -> None:
        rgb8 = _rgb(source)
        rgb = rgb8.astype(np.float32) / 255.0
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
        l_full = lab[..., 0]

        if self.session is not None:
            ab = self._automatic_ab(l_full)
        else:
            ab = self._default_ab(l_full)

        if self.matcher is not None:
            reference_ab = self.matcher.transfer(rgb8)
            weight = np.clip(self.settings.reference_strength, 0.0, 1.0)
            ab = ab * (1.0 - weight) + reference_ab * weight

        ab *= self.settings.saturation
        # Only absolute paper-white pixels are neutralized. Light clothes and skin stay colored.
        paper = np.clip((l_full - 99.0) / 0.8, 0.0, 1.0)[..., None]
        ab *= 1.0 - paper * 0.96

        result_lab = np.dstack((l_full, np.clip(ab, -110, 110))).astype(np.float32)
        colored = np.clip(cv2.cvtColor(result_lab, cv2.COLOR_LAB2RGB) * 255.0, 0, 255)
        original = rgb8.astype(np.float32)
        # Protect only genuine dark ink, not gray screentones or clothing fills.
        line_mask = np.clip((20.0 - l_full) / 18.0, 0, 1)[..., None]
        mixed = colored * self.settings.strength + original * (1 - self.settings.strength)
        mixed = mixed * (1 - line_mask * self.settings.line_protection) + original * line_mask * self.settings.line_protection
        Image.fromarray(np.clip(mixed, 0, 255).astype(np.uint8)).save(
            destination, quality=94, subsampling=0
        )


def make_colorizer(references: list[Path], settings: ColorSettings):
    if settings.engine == "style2paints":
        from .style2paints_engine import Style2PaintsColorizer

        return Style2PaintsColorizer(references, settings)
    return MultiReferenceColorizer(references, settings)
