from __future__ import annotations

import json
import gc
import os
from pathlib import Path
import sys
import traceback
import zlib


def emit(event: str, **payload) -> None:
    print(
        "INKBLOOM_JSON:"
        + json.dumps({"event": event, **payload}, ensure_ascii=False),
        flush=True,
    )


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    repo = Path(config["repo"]).resolve()
    os.chdir(repo)
    # Cobra ships a customized Diffusers fork. Use the copy beside the
    # portable worker directly so moving the InkBloom folder never leaves an
    # editable-install path pointing at the old machine/location.
    sys.path.insert(0, str(repo / "diffusers" / "src"))
    sys.path.insert(0, str(repo))
    import app as cobra

    emit("ready", message="Cobra 模型加载完成")
    references = [Path(path).resolve() for path in config["references"]]
    files = [type("ReferenceFile", (), {"name": str(path)})() for path in references]
    # Cobra expands Top-K across four query regions. On a 16 GB GPU, K=12
    # means 48 reference tensors and causes WDDM to spill CUDA allocations
    # into shared system memory. Keep a conservative automatic ceiling.
    total_vram_gb = cobra.torch.cuda.get_device_properties(0).total_memory / 1024**3
    safe_top_k = 4 if total_vram_gb < 20 else 8 if total_vram_gb < 32 else 20
    top_k = min(int(config["top_k"]), safe_top_k, max(1, len(references) * 5))
    emit(
        "memory",
        message=(
            f"显存安全模式：{total_vram_gb:.1f}GB，实际 Top-K={top_k}；"
            + ("连续页面使用章节固定种子" if config.get("consistency", True) else "逐页使用不同种子")
        ),
    )
    pages = [Path(path).resolve() for path in config["pages"]]
    output_dir = Path(config["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    for index, page in enumerate(pages, 1):
        if config.get("consistency", True):
            # Keep one deterministic visual identity per source/chapter.
            # Incrementing the seed on every page was a major cause of sudden
            # palette and rendering-style changes between adjacent pages.
            chapter_key = str(page.parent).encode("utf-8", errors="replace")
            page_seed = int(config["seed"]) + zlib.crc32(chapter_key) % 100_000
        else:
            page_seed = int(config["seed"]) + index - 1
        emit(
            "page_start",
            current=index,
            total=len(pages),
            message=f"第 {index} 页：检索多张样例并生成颜色",
        )
        last_error = None
        for attempt in range(2):
            try:
                source = cobra.Image.open(page).convert("RGB")
                extracted, _preview, hint_mask, origin, extracted_original, resolution = (
                    cobra.extract_sketch_line_image(source, config["style"])
                )
                gallery = cobra.colorize_image(
                    extracted,
                    files,
                    resolution,
                    page_seed,
                    int(config["steps"]),
                    top_k,
                    hint_mask,
                    extracted,
                    origin,
                    extracted_original,
                )
                gallery[0].save(output_dir / f"cobra_{index:05d}.png")
                emit(
                    "page_done",
                    current=index,
                    total=len(pages),
                    message=f"第 {index} 页 Cobra 上色完成",
                )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                cobra.torch.cuda.empty_cache()
                gc.collect()
                if attempt == 0:
                    emit(
                        "page_retry",
                        current=index,
                        total=len(pages),
                        message=f"第 {index} 页处理异常，正在自动重试：{exc}",
                    )
        if last_error is not None:
            emit(
                "page_failed",
                current=index,
                total=len(pages),
                message=f"第 {index} 页重试失败，已保留原页占位：{last_error}",
            )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("error", message=f"{exc}\n{traceback.format_exc()}")
        raise
