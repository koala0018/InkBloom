from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable


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
    _node(nodes, 131, "easy cleanGpuUsed", {"anything": ["130", 0]})
    _node(nodes, 132, "VAEDecode", {"samples": ["131", 0], "vae": ["153", 2]})
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


def run_parallel(pages: list[Path], out_dir: Path, positive: str, negative: str, width: int | None, height: int | None, on_page: Callable[[int, int, str], None] | None = None) -> None:
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
    def one(index: int, page: Path) -> tuple[int, str]:
        service = online[index % len(online)]
        target = out_dir / f"{index + 1:05d}_{page.stem}_colored.png"
        service.run(page, positive, negative, width, height, target)
        return index, service.name
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="comfy") as pool:
        futures = [pool.submit(one, index, page) for index, page in enumerate(pages)]
        for done, future in enumerate(as_completed(futures), 1):
            index, service_name = future.result()
            if on_page:
                on_page(done, len(pages), f"第 {index + 1} 页完成（{service_name}）")
