from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from .colorizer import ColorSettings
from .documents import result_name
from .paths import MODELS, ROOT


ProgressCallback = Callable[[str, int, int, str], None]


class CobraColorizer:
    """Run the official Cobra reference colorizer in an isolated Python process."""

    def __init__(self, references: list[Path], settings: ColorSettings):
        if not references:
            raise ValueError("Cobra 模式必须上传至少一张彩色样例图")
        self.references = [Path(path).resolve() for path in references]
        self.settings = settings
        self.root = MODELS / "cobra"
        self.repo = self.root / "Cobra"
        self.python = self.root / ".venv" / "Scripts" / "python.exe"
        self.worker = self.root / "inkbloom_worker.py"
        self.log_path = ROOT / "InkBloom-Cobra.log"
        self.reference_palettes = [self._palette_stats(path) for path in self.references]
        palette = np.vstack([mean for mean, _std in self.reference_palettes])
        self.reference_mean = np.median(palette, axis=0)
        self.reference_std = np.maximum(
            np.median(np.vstack([std for _mean, std in self.reference_palettes]), axis=0),
            8.0,
        )
        self.previous_group: Path | None = None
        self.previous_mean: np.ndarray | None = None
        self.previous_std: np.ndarray | None = None
        self.reference_plan: list[list[int]] = []
        self.group_reference: dict[Path, int] = {}

    @classmethod
    def available(cls) -> bool:
        root = MODELS / "cobra"
        return all(
            path.exists()
            for path in (
                root / "Cobra" / "app.py",
                root / ".venv" / "Scripts" / "python.exe",
                root / "inkbloom_worker.py",
                root / "READY",
            )
        )

    @staticmethod
    def _match_feature(path: Path) -> np.ndarray:
        gray = np.asarray(Image.open(path).convert("L").resize((128, 128)), dtype=np.uint8)
        histogram = cv2.calcHist([gray], [0], None, [24], [0, 256]).ravel()
        histogram /= max(float(histogram.sum()), 1.0)
        edges = cv2.Canny(gray, 50, 150).astype(np.float32) / 255.0
        thumbnail = cv2.resize(gray, (24, 24), interpolation=cv2.INTER_AREA).astype(np.float32)
        thumbnail = (thumbnail - thumbnail.mean()) / max(float(thumbnail.std()), 1.0)
        return np.r_[histogram * 2.0, edges.mean(), thumbnail.ravel() * 0.035]

    @staticmethod
    def _palette_stats(path: Path) -> tuple[np.ndarray, np.ndarray]:
        rgb = np.asarray(
            Image.open(path).convert("RGB").resize((256, 256)),
            dtype=np.float32,
        ) / 255.0
        lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
        chroma = lab[..., 1:].reshape(-1, 2)
        colorful = chroma[np.linalg.norm(chroma, axis=1) > 3.0]
        if not len(colorful):
            colorful = np.zeros((1, 2), np.float32)
        return np.median(colorful, axis=0), np.maximum(np.std(colorful, axis=0), 8.0)

    def prepare_reference_plan(self, pages, ask, reference_paths) -> None:
        if len(reference_paths) <= 1:
            self.reference_plan = [[0] for _ in pages]
            self.group_reference = {page.parent: 0 for page in pages}
            return
        reference_features = [self._match_feature(path) for path in reference_paths]
        previous_choice: int | None = None
        self.reference_plan = []
        self.group_reference = {}
        page_index = 0
        while page_index < len(pages):
            group = pages[page_index].parent
            group_end = page_index + 1
            while group_end < len(pages) and pages[group_end].parent == group:
                group_end += 1
            group_pages = group_end - page_index
            page_feature = self._match_feature(pages[page_index])
            distances = np.array(
                [np.mean((page_feature - feature) ** 2) for feature in reference_features]
            )
            ranked = np.argsort(distances)
            best = int(ranked[0])
            if self.settings.cobra_reference_confirmation:
                result = ask(
                    page_index,
                    [int(item) for item in ranked[:4]],
                    previous_choice,
                    group.name,
                    group_pages,
                )
                if result.get("new_reference"):
                    new_path = Path(result["new_reference"]).resolve()
                    self.references.append(new_path)
                    reference_paths.append(new_path)
                    reference_features.append(self._match_feature(new_path))
                    self.reference_palettes.append(self._palette_stats(new_path))
                    choice = len(reference_features) - 1
                else:
                    choice = int(result.get("reference", best))
            else:
                choice = best
            secondary = next((int(item) for item in ranked if int(item) != choice), choice)
            plan = [choice, choice, secondary]
            self.reference_plan.extend([plan] * group_pages)
            self.group_reference[group] = choice
            previous_choice = choice
            page_index = group_end

    def _finish_image(self, source: Path, generated: Path, destination: Path, settings: ColorSettings) -> None:
        original = np.asarray(Image.open(source).convert("RGB"), dtype=np.uint8)
        result = np.asarray(
            Image.open(generated).convert("RGB").resize(
                (original.shape[1], original.shape[0]), Image.Resampling.LANCZOS
            ),
            dtype=np.float32,
        )

        # Keep more of Cobra's lighting and chroma so skin, clothes, and
        # backgrounds remain close to the references. A soft chroma limiter
        # avoids returning to the over-heavy, dirty look of older engines.
        original_f = original.astype(np.float32) / 255.0
        result_f = result / 255.0
        original_lab = cv2.cvtColor(original_f, cv2.COLOR_RGB2LAB)
        result_lab = cv2.cvtColor(result_f, cv2.COLOR_RGB2LAB)
        color_strength = float(np.clip(settings.cobra_color_strength, 0.0, 1.0))
        result_lab[..., 0] = original_lab[..., 0] * 0.62 + result_lab[..., 0] * 0.38
        chroma = result_lab[..., 1:] * color_strength
        guide = cv2.cvtColor(original, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        filtered = np.stack(
            [
                cv2.ximgproc.guidedFilter(
                    guide=guide,
                    src=chroma[..., channel].astype(np.float32),
                    radius=7,
                    eps=0.004,
                )
                for channel in range(2)
            ],
            axis=-1,
        )
        # Remove small chroma speckles in screentones while keeping character
        # boundaries and deliberate local colors.
        chroma = chroma * 0.28 + filtered * 0.72
        if settings.cobra_consistency:
            # Cobra normally chooses reference patches independently for each
            # page. Anchor every page to the uploaded sample palette and use
            # the preceding page only as a weak continuity cue inside the same
            # source/chapter folder.
            weight = float(np.clip(settings.cobra_consistency_strength, 0.0, 1.0))
            valid = np.linalg.norm(chroma, axis=2) > 3.0
            if np.any(valid):
                page_values = chroma[valid]
                page_mean = np.median(page_values, axis=0)
                page_std = np.maximum(np.std(page_values, axis=0), 8.0)
                group = source.parent
                selected = self.group_reference.get(source.parent)
                if selected is not None and selected < len(self.reference_palettes):
                    continuity_mean, continuity_std = self.reference_palettes[selected]
                else:
                    continuity_mean, continuity_std = self.reference_mean, self.reference_std
                if self.previous_group == group and self.previous_mean is not None:
                    continuity_mean = continuity_mean * 0.78 + self.previous_mean * 0.22
                    continuity_std = continuity_std * 0.82 + self.previous_std * 0.18
                normalized = (chroma - page_mean) / page_std
                anchored = normalized * continuity_std + continuity_mean
                chroma = chroma * (1.0 - weight * 0.42) + anchored * (weight * 0.42)
                final_values = chroma[valid]
                self.previous_group = group
                self.previous_mean = np.median(final_values, axis=0)
                self.previous_std = np.maximum(np.std(final_values, axis=0), 8.0)
        chroma_norm = np.linalg.norm(chroma, axis=2, keepdims=True)
        soft_limit = 62.0
        chroma *= np.tanh(chroma_norm / soft_limit) * soft_limit / np.maximum(chroma_norm, 1e-5)
        result_lab[..., 1:] = chroma
        clean = np.clip(cv2.cvtColor(result_lab, cv2.COLOR_LAB2RGB), 0.0, 1.0)

        gray = cv2.cvtColor(original, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
        ink = np.clip((0.30 - gray) / 0.25, 0.0, 1.0)[..., None]
        preserve = float(np.clip(settings.cobra_preserve_lines, 0.0, 1.0))
        mixed = clean * (1.0 - ink * preserve) + original_f * (ink * preserve)
        # Preserve only bright paper connected to the outer page boundary.
        # The previous global white mask also erased white walls, sky, floors,
        # and other enclosed environments that Cobra had correctly colored.
        bright = (gray > 0.965).astype(np.uint8)
        count, labels = cv2.connectedComponents(bright, connectivity=8)
        border_labels = np.unique(
            np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1]))
        )
        exterior = np.isin(labels, border_labels[border_labels != 0]).astype(np.float32)
        feather = max(3, int(round(max(original.shape[:2]) / 900)) | 1)
        exterior = cv2.GaussianBlur(exterior, (feather, feather), 0)[..., None]
        mixed = mixed * (1.0 - exterior * 0.98) + original_f * (exterior * 0.98)
        Image.fromarray(np.uint8(np.clip(mixed * 255.0, 0, 255))).save(
            destination, quality=96, subsampling=0
        )

    def colorize_batch(
        self,
        pages: list[Path],
        output_dir: Path,
        callback: ProgressCallback | None = None,
    ) -> list[Path]:
        if not self.available():
            raise RuntimeError("Cobra 尚未安装完成，请运行 install-cobra.ps1")

        output_dir.mkdir(parents=True, exist_ok=True)
        raw_dir = output_dir.parent / "cobra-raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "repo": str(self.repo),
            "pages": [str(path.resolve()) for path in pages],
            "references": [str(path) for path in self.references],
            "reference_plan": self.reference_plan,
            "output_dir": str(raw_dir.resolve()),
            "style": "line + shadow" if self.settings.cobra_style == "line_shadow" else "line",
            "steps": max(4, min(30, int(self.settings.cobra_steps))),
            "top_k": max(1, min(20, int(self.settings.cobra_top_k))),
            "seed": int(self.settings.cobra_seed),
            "consistency": bool(self.settings.cobra_consistency),
        }
        config_path = output_dir.parent / "cobra-job.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        env = os.environ.copy()
        env["HF_HOME"] = str(self.root / "huggingface")
        env["HUGGINGFACE_HUB_CACHE"] = str(self.root / "huggingface")
        env["TORCH_HOME"] = str(self.root / "torch")
        env["INKBLOOM_COBRA_CACHE"] = str(self.root / "huggingface")
        env["INKBLOOM_COBRA_OFFLINE"] = "1"
        env["HF_HUB_OFFLINE"] = "1"
        env["PYTHONUTF8"] = "1"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        command = [str(self.python), "-u", str(self.worker), str(config_path)]
        if callback:
            callback("engine", 0, 1, "正在加载 Cobra 模型（首次启动需要更久）")

        log = self.log_path.open("a", encoding="utf-8", buffering=1)
        process = subprocess.Popen(
            command,
            cwd=self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        results: list[Path] = []
        completed: set[int] = set()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                line = line.strip()
                if not line.startswith("INKBLOOM_JSON:"):
                    continue
                event = json.loads(line.removeprefix("INKBLOOM_JSON:"))
                kind = event.get("event")
                current = int(event.get("current", 0))
                total = int(event.get("total", max(1, len(pages))))
                message = str(event.get("message", "Cobra 正在处理"))
                if callback:
                    if kind == "ready":
                        callback("engine", 1, 1, message)
                        callback("reference", 1, 1, f"已载入 {len(self.references)} 张样例图")
                        callback("lineart", 1, 1, "将保留原始线稿与网点结构")
                    elif kind == "memory":
                        callback("reference", 0, 1, message)
                    elif kind == "page_start":
                        callback("stage1", current - 1, total, message)
                    elif kind == "page_done":
                        raw = raw_dir / f"cobra_{current:05d}.png"
                        if not raw.exists():
                            raise RuntimeError(f"Cobra 缺少第 {current} 页输出")
                        destination = output_dir / result_name(pages[current - 1], current)
                        self._finish_image(pages[current - 1], raw, destination, self.settings)
                        raw.unlink(missing_ok=True)
                        results.append(destination)
                        completed.add(current)
                        callback("stage1", current, total, message)
                        callback("stage2", current, total, f"第 {current} 页色彩清理完成")
                        callback("layers", current, total, f"第 {current} 页已保存，可立即查看高清预览")
                    elif kind == "page_retry":
                        callback("stage1", current - 1, total, message)
                    elif kind == "page_failed":
                        destination = output_dir / result_name(pages[current - 1], current)
                        Image.open(pages[current - 1]).convert("RGB").save(
                            destination, quality=96, subsampling=0
                        )
                        results.append(destination)
                        completed.add(current)
                        callback("stage1", current, total, message)
                        callback("layers", current, total, f"第 {current} 页使用原页占位，可继续完成整本任务")
                    elif kind == "error":
                        raise RuntimeError(message)
        except Exception:
            if process.poll() is None:
                process.terminate()
            raise
        finally:
            if process.stdout:
                process.stdout.close()
            log.close()

        code = process.wait()
        if code != 0:
            raise RuntimeError(f"Cobra 推理进程异常退出（代码 {code}），请查看 {self.log_path}")

        for index, page in enumerate(pages, 1):
            if index in completed:
                continue
            raw = raw_dir / f"cobra_{index:05d}.png"
            if not raw.exists():
                raise RuntimeError(f"Cobra 缺少第 {index} 页输出")
            destination = output_dir / result_name(page, index)
            self._finish_image(page, raw, destination, self.settings)
            raw.unlink(missing_ok=True)
            results.append(destination)
            if callback:
                callback("layers", index, len(pages), f"第 {index} 页已保存高清结果")
        return sorted(results)
