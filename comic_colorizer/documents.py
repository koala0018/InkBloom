from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import fitz
from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def natural_key(path: Path | str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(path))]


def collect_inputs(paths: list[Path], page_dir: Path) -> tuple[list[Path], str]:
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
            for page in doc:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                target = page_dir / f"page_{len(pages):05d}.png"
                pix.save(target)
                pages.append(target)
            doc.close()
        elif suffix in {".zip", ".cbz"}:
            source_kind = "cbz"
            with zipfile.ZipFile(source) as archive:
                names = sorted(
                    (n for n in archive.namelist() if Path(n).suffix.lower() in IMAGE_EXTS),
                    key=natural_key,
                )
                for name in names:
                    with archive.open(name) as stream, Image.open(io.BytesIO(stream.read())) as image:
                        target = page_dir / f"page_{len(pages):05d}.png"
                        image.convert("RGB").save(target)
                        pages.append(target)
        else:
            raise ValueError(f"不支持的文件类型：{source.name}")
    if not pages:
        raise ValueError("没有找到可处理的漫画页面")
    return pages, source_kind


def export_results(images: list[Path], output_dir: Path, title: str, source_kind: str) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", title).strip("_") or "comic"
    cbz_path = output_dir / f"{safe_title}_彩色版.cbz"
    with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, image in enumerate(images, 1):
            archive.write(image, f"{index:05d}.jpg")

    pdf_path = output_dir / f"{safe_title}_彩色版.pdf"
    pdf = fitz.open()
    for image_path in images:
        with Image.open(image_path) as image:
            width, height = image.size
        page = pdf.new_page(width=width, height=height)
        page.insert_image(page.rect, filename=str(image_path))
    pdf.save(pdf_path, deflate=True)
    pdf.close()
    return {"cbz": cbz_path.name, "pdf": pdf_path.name}

