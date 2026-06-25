from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import cv2
import numpy as np
from PIL import Image

from .colorizer import ColorSettings, RegionReferenceMatcher, _rgb
from .documents import result_name
from .paths import MODELS, ROOT


ProgressCallback = Callable[[str, int, int, str], None]


class Style2PaintsColorizer:
    """Adapter for the official local Style2Paints V4.5 HTTP service."""
    S2P_UPLOAD_LIMIT = 650_000

    OUTPUTS = {
        ("careless", "flat"): "flat_careless",
        ("careless", "blended_flat"): "blended_flat_careless",
        ("careless", "smoothed"): "smoothed_careless",
        ("careless", "blended_smoothed"): "blended_smoothed_careless",
        ("careful", "flat"): "flat_careful",
        ("careful", "blended_flat"): "blended_flat_careful",
        ("careful", "smoothed"): "smoothed_careful",
        ("careful", "blended_smoothed"): "blended_smoothed_careful",
    }

    def __init__(self, references: list[Path], settings: ColorSettings):
        if not references:
            raise ValueError("Style2Paints 模式必须上传至少一张彩色样例图")
        self.references = [Path(path).resolve() for path in references]
        self.settings = settings
        self.matcher = RegionReferenceMatcher(self.references)
        self.model_root = MODELS / "style2paints"
        self.base_url = os.getenv("INKBLOOM_STYLE2PAINTS_URL", "http://127.0.0.1:8233").rstrip("/")
        self.process: subprocess.Popen | None = None
        self.log_path = ROOT / "InkBloom-Style2Paints.log"
        if settings.s2p_stage not in {"careless", "careful"}:
            raise ValueError("未知的 Style2Paints 阶段")
        if settings.s2p_finish not in {"flat", "blended_flat", "smoothed", "blended_smoothed"}:
            raise ValueError("未知的 Style2Paints 输出类型")
        try:
            points = json.loads(settings.s2p_hint_points or "[]")
            if not isinstance(points, list) or len(points) > 500:
                raise ValueError
            self.hint_points = points
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("颜色提示点格式无效") from exc

    @staticmethod
    def _resize_for_style2paints(image: Image.Image, max_side: int) -> Image.Image:
        image = image.convert("RGB")
        width, height = image.size
        longest = max(width, height)
        if longest <= max_side:
            return image
        scale = max_side / longest
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        return image.resize(size, Image.Resampling.LANCZOS)

    @classmethod
    def _data_url(cls, path: Path, *, kind: str = "reference") -> tuple[str, tuple[int, int], int]:
        original = Image.open(path).convert("RGB")
        if kind == "sketch":
            attempts = [
                ("PNG", "image/png", 1536, {}),
                ("PNG", "image/png", 1280, {}),
                ("PNG", "image/png", 1024, {}),
                ("PNG", "image/png", 896, {}),
                ("JPEG", "image/jpeg", 1024, {"quality": 92, "subsampling": 0}),
                ("JPEG", "image/jpeg", 896, {"quality": 90, "subsampling": 0}),
                ("JPEG", "image/jpeg", 768, {"quality": 88, "subsampling": 0}),
            ]
        else:
            attempts = [
                ("JPEG", "image/jpeg", 1024, {"quality": 92, "subsampling": 0}),
                ("JPEG", "image/jpeg", 768, {"quality": 90, "subsampling": 0}),
                ("JPEG", "image/jpeg", 640, {"quality": 88, "subsampling": 0}),
            ]

        best: tuple[str, tuple[int, int], int] | None = None
        for fmt, mime, max_side, options in attempts:
            image = cls._resize_for_style2paints(original, max_side)
            buffer = io.BytesIO()
            save_options = {"optimize": True} if fmt == "PNG" else {}
            save_options.update(options)
            image.save(buffer, format=fmt, **save_options)
            raw = buffer.getvalue()
            data_url = f"data:{mime};base64," + base64.urlsafe_b64encode(raw).decode("ascii")
            best = (data_url, image.size, len(raw))
            if len(data_url) <= cls.S2P_UPLOAD_LIMIT:
                return best
        assert best is not None
        return best

    def _request(self, path: str, form: dict[str, str] | None = None, timeout: int = 900) -> bytes:
        data = urlencode(form).encode("utf-8") if form is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            raise RuntimeError(f"Style2Paints 服务返回 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"无法连接 Style2Paints 本地服务：{self.base_url}") from exc

    def _service_ready(self) -> bool:
        parsed = urlparse(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except Exception:
            return False

    def _find_launcher(self) -> Path | None:
        # The official V4.5 archive contains two different Windows entry points:
        #
        # - style2paints45beta1214B/style2paints.exe
        #   A small GPU-selection/client shell. It shows an old GPU list and can
        #   fail on modern cards (for example RTX 50-series) before the local
        #   HTTP service is ready.
        #
        # - style2paints45beta1214B/assets/Style2PaintsV45.exe
        #   The actual Python/TensorFlow backend that serves the documented
        #   V4.5 HTTP API on http://127.0.0.1:8233.
        #
        # InkBloom talks to the HTTP API directly, so prefer the backend and
        # keep the GUI shell only as a last-ditch fallback.
        preferred = [
            self.model_root / "Style2PaintsV45.exe",
            self.model_root / "style2paints45beta1214B" / "assets" / "Style2PaintsV45.exe",
            self.model_root / "style2paints45beta1214B" / "Style2PaintsV45.exe",
            self.model_root / "style2paints45beta1214B" / "launch_win.exe",
            self.model_root / "style2paints45beta1214B" / "assets" / "dotnet" / "Style2Paints.exe",
        ]
        for path in preferred:
            if path.exists():
                return path
        candidates = []
        if self.model_root.exists():
            for path in self.model_root.rglob("*.exe"):
                name = path.name.lower()
                if any(token in name for token in ("style2paint", "launch", "server")):
                    candidates.append(path)

        def rank(path: Path) -> tuple[int, int, str]:
            lowered = str(path).lower()
            name = path.name.lower()
            if name == "style2paintsv45.exe":
                return (0, len(path.parts), lowered)
            if "assets" in path.parts and name.startswith("style2paints"):
                return (1, len(path.parts), lowered)
            if name in {"launch_win.exe", "server.exe"}:
                return (2, len(path.parts), lowered)
            if name == "style2paints.exe" and "style2paints45beta1214b" in lowered:
                return (9, len(path.parts), lowered)
            return (5, len(path.parts), lowered)

        return sorted(candidates, key=rank)[0] if candidates else None

    def _ensure_service(self, callback: ProgressCallback | None) -> None:
        if self._service_ready():
            if callback:
                callback("engine", 1, 1, "已连接 Style2Paints V4.5 本地服务")
            return
        if callback:
            callback("engine", 0, 1, "正在检查 Style2Paints 官方程序")
        launcher = self._find_launcher()
        if launcher is None:
            raise RuntimeError(
                "未找到 Style2Paints V4.5 官方程序。请运行 install-style2paints.ps1，"
                "或把官方 style2paints45beta1214B 解压到 models/style2paints。"
            )
        if callback:
            try:
                label = str(launcher.relative_to(self.model_root))
            except ValueError:
                label = launcher.name
            callback("engine", 0, 1, f"正在启动 Style2Paints 后端：{label}")
        log = self.log_path.open("a", encoding="utf-8", buffering=1)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            [str(launcher)],
            cwd=launcher.parent,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                syslog = launcher.parent / "syslog.txt"
                extra = f"；官方日志：{syslog}" if syslog.exists() else ""
                raise RuntimeError(f"Style2Paints 启动失败，请查看 {self.log_path}{extra}")
            if self._service_ready():
                if callback:
                    callback("engine", 1, 1, "Style2Paints 模型加载完成")
                return
            time.sleep(1)
        syslog = launcher.parent / "syslog.txt"
        extra = f"；官方日志：{syslog}" if syslog.exists() else ""
        raise RuntimeError(f"Style2Paints 模型加载超时，请查看 {self.log_path}{extra}")

    def _upload_sketch(self, page: Path, callback: ProgressCallback | None = None) -> str:
        sketch, size, byte_count = self._data_url(page, kind="sketch")
        if callback:
            callback("lineart", 0, 1, f"已压缩线稿给 Style2Paints：{size[0]}×{size[1]}，{byte_count // 1024} KB")
        response = self._request("/upload_sketch", {"sketch": sketch}, timeout=900)
        text = response.decode("utf-8", errors="replace").strip()
        room = text.split("_", 1)[0]
        if not room or "<" in room:
            raise RuntimeError(f"Style2Paints 无法创建画布：{text[:120]}")
        return room

    def _render(self, room: str, reference: Path) -> str:
        face, _face_size, _face_bytes = self._data_url(reference, kind="reference")
        form = {
            "room": room,
            "points": json.dumps(self.hint_points, ensure_ascii=False, separators=(",", ":")),
            "face": face,
            "faceID": str(65535 - 233),
            # Kept for compatibility with the V4 client and V4.5 binary wrapper.
            "need_render": "0",
            "skipper": "null",
            "inv4": "1",
            "r": "-1",
            "g": "-1",
            "b": "-1",
            "h": "0.5",
            "d": "0",
        }
        response = self._request("/request_result", form, timeout=900)
        text = response.decode("utf-8", errors="replace").strip()
        parts = text.rsplit("_", 1)
        if len(parts) != 2 or not parts[1]:
            raise RuntimeError(f"Style2Paints 返回了无效结果：{text[:120]}")
        return parts[1]

    def _download_image(self, room: str, step: str, variant: str) -> Image.Image:
        raw = self._request(f"/rooms/{room}/{step}.{variant}.png", timeout=120)
        import io

        try:
            return Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as exc:
            raise RuntimeError(f"Style2Paints 缺少输出层：{variant}") from exc

    def colorize_batch(
        self,
        pages: list[Path],
        output_dir: Path,
        callback: ProgressCallback | None = None,
    ) -> list[Path]:
        self._ensure_service(callback)
        output_dir.mkdir(parents=True, exist_ok=True)
        layer_root = output_dir.parent / "style2paints-layers"
        if self.settings.s2p_save_layers:
            layer_root.mkdir(parents=True, exist_ok=True)
        results: list[Path] = []
        selected_variant = self.OUTPUTS[(self.settings.s2p_stage, self.settings.s2p_finish)]
        all_variants = list(self.OUTPUTS.values())

        for index, page in enumerate(pages, 1):
            reference_index = self.matcher.best_reference_index(_rgb(page))
            reference = self.references[reference_index]
            if callback:
                callback("reference", index, len(pages), f"第 {index} 页已匹配样例 {reference_index + 1}")
                callback("lineart", index - 1, len(pages), f"第 {index} 页：线稿清理与分区")
            room = self._upload_sketch(page, callback)
            if callback:
                callback("lineart", index, len(pages), f"第 {index} 页线稿预处理完成")
                callback("stage1", index - 1, len(pages), f"第 {index} 页：Stage I 固有色生成")
            step = self._render(room, reference)
            if callback:
                callback("stage1", index, len(pages), f"第 {index} 页 Stage I 完成")
                callback("stage2", index, len(pages), f"第 {index} 页 Stage II 精细渲染完成")

            variants = all_variants if self.settings.s2p_save_layers else [selected_variant]
            downloaded: dict[str, Image.Image] = {}
            for variant in variants:
                downloaded[variant] = self._download_image(room, step, variant)
            chosen = downloaded[selected_variant]
            original = Image.open(page).convert("RGB")
            if chosen.size != original.size:
                chosen = chosen.resize(original.size, Image.Resampling.LANCZOS)
            destination = output_dir / result_name(page, index)
            chosen.save(destination, quality=96, subsampling=0)
            results.append(destination)

            if self.settings.s2p_save_layers:
                page_layers = layer_root / f"page_{index:05d}"
                page_layers.mkdir(parents=True, exist_ok=True)
                for variant, image in downloaded.items():
                    image.save(page_layers / f"{variant}.png")
            if callback:
                callback("layers", index, len(pages), f"第 {index} 页完成，已保存 {len(variants)} 个输出层")
        return results
