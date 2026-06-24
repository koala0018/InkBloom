from __future__ import annotations

import re
import zipfile
from pathlib import Path

import fitz
from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
EXPORT_VOLUME_PAGES = 250


def natural_key(path: Path | str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(path))]


def collect_inputs(
    paths: list[Path],
    page_dir: Path,
    on_warning=None,
) -> tuple[list[Path], str]:
    page_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    source_kind = "images"

    for source in paths:
        suffix = source.suffix.lower()
        if suffix in IMAGE_EXTS:
            target = page_dir / f"page_{len(pages):05d}.png"
            with Image.open(source) as image:
                image.convert("RGB").save(target)
            pages.append(target)
        elif suffix == ".pdf":
            source_kind = "pdf"
            doc = fitz.open(source)
            try:
                for page in doc:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    target = page_dir / f"page_{len(pages):05d}.png"
                    pix.save(target)
                    pages.append(target)
            finally:
                doc.close()
        elif suffix in {".zip", ".cbz"}:
            source_kind = "cbz"
            with zipfile.ZipFile(source) as archive:
                infos = sorted(
                    (
                        info
                        for info in archive.infolist()
                        if not info.is_dir()
                        and Path(info.filename).suffix.lower() in IMAGE_EXTS
                        and "__MACOSX" not in Path(info.filename).parts
                        and not Path(info.filename).name.startswith(".")
                    ),
                    key=lambda info: natural_key(info.filename),
                )
                seen: set[tuple[int, int]] = set()
                skipped = 0
                for info in infos:
                    signature = (info.CRC, info.file_size)
                    if signature in seen:
                        skipped += 1
                        continue
                    seen.add(signature)
                    try:
                        with archive.open(info) as stream, Image.open(stream) as image:
                            image.load()
                            if image.width < 64 or image.height < 64:
                                skipped += 1
                                continue
                            target = page_dir / f"page_{len(pages):05d}.png"
                            image.convert("RGB").save(target)
                            pages.append(target)
                    except Exception as exc:
                        skipped += 1
                        if on_warning:
                            on_warning(f"已跳过无法读取的图片：{info.filename}（{exc}）")
                if skipped and on_warning:
                    on_warning(f"压缩包预检完成：跳过 {skipped} 个重复、过小或损坏图片")
        else:
            raise ValueError(f"不支持的文件类型：{source.name}")

    if not pages:
        raise ValueError("没有找到可处理的漫画页面")
    return pages, source_kind


def export_results(images: list[Path], output_dir: Path, title: str, source_kind: str) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", title).strip("_") or "comic"
    volumes = [
        images[start : start + EXPORT_VOLUME_PAGES]
        for start in range(0, len(images), EXPORT_VOLUME_PAGES)
    ]
    generated: list[Path] = []

    for volume_index, volume in enumerate(volumes, 1):
        suffix = "" if len(volumes) == 1 else f"_第{volume_index:02d}卷"
        base = (volume_index - 1) * EXPORT_VOLUME_PAGES

        cbz_path = output_dir / f"{safe_title}_彩色版{suffix}.cbz"
        with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, image in enumerate(volume, base + 1):
                archive.write(image, f"{index:05d}.jpg")
        generated.append(cbz_path)

        pdf_path = output_dir / f"{safe_title}_彩色版{suffix}.pdf"
        pdf = fitz.open()
        try:
            for image_path in volume:
                with Image.open(image_path) as image:
                    width, height = image.size
                page = pdf.new_page(width=width, height=height)
                page.insert_image(page.rect, filename=str(image_path))
            pdf.save(pdf_path, deflate=True, garbage=3)
        finally:
            pdf.close()
        generated.append(pdf_path)

    if len(volumes) == 1:
        return {"cbz": generated[0].name, "pdf": generated[1].name}

    bundle = output_dir / f"{safe_title}_彩色版_分卷合集.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as archive:
        for path in generated:
            archive.write(path, path.name)
    return {"bundle": bundle.name}
