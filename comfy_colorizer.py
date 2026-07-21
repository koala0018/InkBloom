from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable



def safe_component(value: str, fallback: str = "page", max_length: int = 100) -> str:
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", value).strip(" ._")
    value = re.sub(r"\s+", " ", value)
    return value[:max_length] or fallback


DEFAULT_WORKFLOWS = [
    Path(os.environ["INKBLOOM_COMFY_WORKFLOW"]) if os.environ.get("INKBLOOM_COMFY_WORKFLOW") else None,
    Path(r"E:\ComfyUI_windows_portable\ComfyUI\user_5060ti\default\workflows\Qwen_图片编辑_最小直连_无加密_16GB.json"),
    Path(r"E:\ComfyUI_windows_portable\ComfyUI\user\default\workflows\Qwen_图片编辑_最小直连_无加密_16GB.json"),
]


def _json(url: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _multipart(url: str, fields: dict[str, str], file_field: str, file_path: Path) -> dict:
    boundary = f"----InkBloom{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks += [f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), str(value).encode("utf-8"), b"\r\n"]
    chunks += [f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode(), b"Content-Type: image/png\r\n\r\n", file_path.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode()]
    request = urllib.request.Request(url, data=b"".join(chunks), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def workflow_path() -> Path:
    for candidate in DEFAULT_WORKFLOWS:
        if candidate is not None and candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到 Qwen_图片编辑_最小直连_无加密_16GB.json 工作流")


def _node(nodes: dict, ident: int, class_type: str, inputs: dict) -> dict:
    nodes[str(ident)] = {"class_type": class_type, "inputs": inputs}


def build_prompt(image_name: str, positive: str, negative: str, width: int | None, height: int | None) -> dict:
    """Build API-format prompt matching the supplied GUI workflow's stable node ids."""
    raw = json.loads(workflow_path().read_text(encoding="utf-8"))
    by_id = {str(node["id"]): node for node in raw.get("nodes", [])}
    ckpt = str(by_id.get("153", {}).get("widgets_values", ["Qwen-Rapid-AIO-NSFW-v19.safetensors"])[0])
    default_width = int(by_id.get("154", {}).get("widgets_values", [768])[0])
    default_height = int(by_id.get("155", {}).get("widgets_values", [1024])[0])
    sampler_values = by_id.get("130", {}).get("widgets_values", [random.randrange(1 << 60), "randomize", 4, 1, "sa_solver", "beta", 1])
    seed = int(sampler_values[0]) if isinstance(sampler_values[0], (int, float)) else random.randrange(1 << 60)
    steps = int(sampler_values[2]) if len(sampler_values) > 2 else 4
    cfg = float(sampler_values[3]) if len(sampler_values) > 3 else 1.0
    sampler = str(sampler_values[4]) if len(sampler_values) > 4 else "sa_solver"
    scheduler = str(sampler_values[5]) if len(sampler_values) > 5 else "beta"
    denoise = float(sampler_values[6]) if len(sampler_values) > 6 else 1.0
    w = int(width or default_width)
    h = int(height or default_height)
    nodes: dict[str, dict] = {}
    _node(nodes, 153, "CheckpointLoaderSimple", {"ckpt_name": ckpt})
    _node(nodes, 147, "LoadImage", {"image": image_name})
    _node(nodes, 128, "FL_QwenImageEditStrength", {"clip": ["153", 1], "vae": ["153", 2], "image1": ["147", 0], "prompt": negative or "", "interpolation_method": "weighted_sum", "image1_strength": 1.0, "image2_strength": 0.0, "image3_strength": 0.0})
    _node(nodes, 129, "FL_QwenImageEditStrength", {"clip": ["153", 1], "vae": ["153", 2], "image1": ["147", 0], "prompt": positive, "interpolation_method": "weighted_sum", "image1_strength": 1.0, "image2_strength": 0.0, "image3_strength": 0.0})
    _node(nodes, 137, "easy int", {"value": 1})
    _node(nodes, 154, "easy int", {"value": w})
    _node(nodes, 155, "easy int", {"value": h})
    _node(nodes, 139, "EmptyLatentImage", {"width": ["154", 0], "height": ["155", 0], "batch_size": ["137", 0]})
    _node(nodes, 130, "KSampler", {"model": ["153", 0], "positive": ["129", 0], "negative": ["128", 0], "latent_image": ["139", 0], "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler, "scheduler": scheduler, "denoise": denoise})
    # Do not inject easy cleanGpuUsed here. It clears ComfyUI's model cache
    # after every page, forcing Qwen to be loaded again for the next page.
    _node(nodes, 132, "VAEDecode", {"samples": ["130", 0], "vae": ["153", 2]})
    _node(nodes, 121, "SaveImage", {"images": ["132", 0], "filename_prefix": "InkBloom_Comfy"})
    return nodes


class ComfyService:
    def __init__(self, name: str, base_url: str):
        self.name = name
        self.base_url = base_url.rstrip("/")

    def health(self) -> dict:
        info = _json(f"{self.base_url}/system_stats", timeout=5)
        devices = info.get("devices") or []
        return {"name": self.name, "url": self.base_url, "online": True, "device": devices[0].get("name", "") if devices else ""}

    def run(self, image: Path, positive: str, negative: str, width: int | None, height: int | None, output: Path, progress: Callable[[str], None] | None = None) -> None:
        upload = _multipart(f"{self.base_url}/upload/image", {"type": "input", "overwrite": "true"}, "image", image)
        image_name = upload.get("name") or image.name
        prompt_id = _json(f"{self.base_url}/prompt", {"prompt": build_prompt(image_name, positive, negative, width, height), "client_id": f"inkbloom-{uuid.uuid4().hex}"}).get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"{self.name} 未返回 prompt_id")
        if progress:
            progress(f"{self.name} 已提交")
        deadline = time.time() + 1800
        while time.time() < deadline:
            history = _json(f"{self.base_url}/history/{urllib.parse.quote(str(prompt_id))}", timeout=30)
            item = history.get(str(prompt_id))
            if item and item.get("status", {}).get("completed"):
                images = []
                for group in (item.get("outputs") or {}).values():
                    images.extend(group.get("images") or [])
                if not images:
                    raise RuntimeError(f"{self.name} 完成但没有输出图片")
                image_info = images[-1]
                query = urllib.parse.urlencode({"filename": image_info["filename"], "subfolder": image_info.get("subfolder", ""), "type": image_info.get("type", "output")})
                with urllib.request.urlopen(f"{self.base_url}/view?{query}", timeout=120) as response:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(response.read())
                return
            if item and item.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"{self.name} 工作流执行失败: {item.get('status')}")
            time.sleep(1.0)
        raise TimeoutError(f"{self.name} 等待 ComfyUI 输出超时")


def services() -> list[ComfyService]:
    return [ComfyService("RTX 2080 Ti", os.environ.get("INKBLOOM_COMFY_2080_URL", "http://127.0.0.1:8188")), ComfyService("RTX 5060 Ti", os.environ.get("INKBLOOM_COMFY_5060_URL", "http://127.0.0.1:8189"))]


def _output_relative_path(index: int, page: Path, page_root: Path | None) -> Path:
    """Keep upload/document/chapter folders visible in the colored output."""
    if page_root is not None:
        try:
            relative = page.relative_to(page_root)
        except ValueError:
            relative = Path(page.name)
    else:
        relative = Path(page.name)
    parent = Path(*[p for p in relative.parent.parts if p not in {"", "."}])
    stem = safe_component(relative.stem, f"page_{index + 1:05d}", max_length=100)
    return parent / f"{index + 1:05d}_{stem}_colored.png"


def run_parallel(pages: list[Path], out_dir: Path, positive: str, negative: str, width: int | None, height: int | None, on_page: Callable[[int, int, str], None] | None = None, page_root: Path | None = None) -> None:
    workers = services()
    online: list[ComfyService] = []
    for service in workers:
        try:
            service.health()
            online.append(service)
        except Exception:
            pass
    if len(online) < 2:
        raise RuntimeError("需要两个 ComfyUI 服务同时在线（2080 Ti:8188、5060 Ti:8189），当前在线服务不足两个")
    out_dir.mkdir(parents=True, exist_ok=True)
    pending_dir = out_dir.parent / ".comfy-pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    def one(index: int, page: Path, service: ComfyService) -> tuple[int, str]:
        target = pending_dir / _output_relative_path(index, page, page_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        service.run(page, positive, negative, width, height, target)
        return index, service.name

    ready: dict[int, tuple[str, Path]] = {}
    next_assign = 0
    next_publish = 0
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="comfy") as pool:
        running: dict[object, tuple[int, ComfyService]] = {}
        for service in online:
            if next_assign >= len(pages):
                break
            future = pool.submit(one, next_assign, pages[next_assign], service)
            running[future] = (next_assign, service)
            next_assign += 1
        while running:
            finished, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
            for future in finished:
                index, service = running.pop(future)
                completed_index, service_name = future.result()
                ready[completed_index] = (service_name, pending_dir / _output_relative_path(completed_index, pages[completed_index], page_root))
                # Reuse the service that just became free; never queue a
                # second job behind it. This keeps both GPUs busy dynamically.
                if next_assign < len(pages):
                    next_future = pool.submit(one, next_assign, pages[next_assign], service)
                    running[next_future] = (next_assign, service)
                    next_assign += 1
                while next_publish in ready:
                    ready_service, pending_file = ready.pop(next_publish)
                    pending_file.replace(out_dir / pending_file.name)
                    next_publish += 1
                    if on_page:
                        on_page(next_publish, len(pages), f"第 {next_publish} 页完成（{ready_service}）")
    try:
        pending_dir.rmdir()
    except OSError:
        pass
