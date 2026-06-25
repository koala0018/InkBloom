from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

import fitz
from PIL import Image

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
ARCHIVE_EXTS = {".zip", ".cbz"}
EXPORT_VOLUME_PAGES = 250
MAX_ARCHIVE_DEPTH = 4
MAX_ARCHIVE_MEMBERS = 20_000

ProgressCallback = Callable[[int, int, str], None]


def natural_key(path: Path | str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(path))]


def safe_component(value: str, fallback: str = "未命名") -> str:
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", value).strip(" ._")
    value = re.sub(r"\s+", " ", value)
    return value[:80] or fallback


def source_label(source: Path) -> str:
    # Uploads are stored as <timestamp>_<index>__<original name>.
    name = re.sub(r"^\d+_\d+__", "", source.name)
    return safe_component(Path(name).stem, "漫画")


def result_name(page: Path, index: int, suffix: str = "colored", extension: str = ".jpg") -> str:
    parents = [safe_component(part) for part in page.parts[-3:-1]]
    context = "_".join(parents[-2:])
    stem = safe_component(page.stem, f"page_{index:05d}")
    return f"{index:05d}_{context}_{stem}_{suffix}{extension}"


def _archive_parts(filename: str) -> list[str]:
    path = PurePosixPath(filename.replace("\\", "/"))
    return [
        safe_component(part)
        for part in path.parts
        if part not in {"", ".", ".."} and part != "__MACOSX"
    ]


def _merge_parts(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for part in (item for group in groups for item in group):
        if not merged or merged[-1].casefold() != part.casefold():
            merged.append(part)
    return merged


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为页面生成唯一文件名：{path.name}")


def _save_image(source, destination: Path, pages: list[Path]) -> bool:
    with Image.open(source) as image:
        image.seek(0)
        image.load()
        if image.width < 64 or image.height < 64:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = _unique_target(destination.with_suffix(".png"))
        image.convert("RGB").save(destination)
        pages.append(destination)
        return True


def _render_pdf(
    source,
    destination_dir: Path,
    pages: list[Path],
    label: str,
    on_progress: ProgressCallback | None,
    progress_state: dict[str, int],
) -> int:
    doc = fitz.open(stream=source, filetype="pdf") if isinstance(source, bytes) else fitz.open(source)
    count = 0
    try:
        total_pages = len(doc)
        progress_state["total"] += total_pages
        destination_dir.mkdir(parents=True, exist_ok=True)
        for page_number, page in enumerate(doc, 1):
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            target = _unique_target(destination_dir / f"page_{page_number:05d}.png")
            pix.save(target)
            pages.append(target)
            count += 1
            progress_state["done"] += 1
            if on_progress and (page_number == 1 or page_number == total_pages or page_number % 5 == 0):
                on_progress(
                    progress_state["done"],
                    max(progress_state["total"], progress_state["done"]),
                    f"正在拆分 {label}：第 {page_number}/{total_pages} 页",
                )
    finally:
        doc.close()
    return count


def _collect_archive(
    archive: zipfile.ZipFile,
    page_dir: Path,
    pages: list[Path],
    seen: set[tuple[int, int]],
    stats: dict[str, int],
    on_warning,
    on_progress: ProgressCallback | None,
    progress_state: dict[str, int],
    archive_path: list[str],
    depth: int = 0,
) -> None:
    if depth > MAX_ARCHIVE_DEPTH:
        stats["too_deep"] += 1
        return

    infos = sorted(
        (
            info
            for info in archive.infolist()
            if not info.is_dir()
            and "__MACOSX" not in _archive_parts(info.filename)
            and not PurePosixPath(info.filename).name.startswith(".")
        ),
        key=lambda info: natural_key(info.filename),
    )
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"压缩包文件数超过安全上限 {MAX_ARCHIVE_MEMBERS}，请拆成多个压缩包后上传")

    stats["members"] += len(infos)
    progress_state["total"] += len(infos)
    for member_number, info in enumerate(infos, 1):
        parts = _archive_parts(info.filename)
        if not parts:
            progress_state["done"] += 1
            continue
        suffix = Path(parts[-1]).suffix.lower()
        full_parts = _merge_parts(archive_path, parts)
        display = " / ".join(full_parts[-3:])
        try:
            if suffix not in IMAGE_EXTS | ARCHIVE_EXTS | {".pdf"}:
                stats["unsupported"] += 1
            else:
                signature = (info.CRC, info.file_size)
                if suffix in IMAGE_EXTS and signature in seen:
                    stats["duplicates"] += 1
                elif suffix in IMAGE_EXTS:
                    seen.add(signature)
                    destination = page_dir.joinpath(*full_parts)
                    with archive.open(info) as stream:
                        if not _save_image(stream, destination, pages):
                            stats["too_small"] += 1
                elif suffix == ".pdf":
                    pdf_dir = page_dir.joinpath(
                        *_merge_parts(archive_path, parts[:-1], [safe_component(Path(parts[-1]).stem)])
                    )
                    with archive.open(info) as stream:
                        _render_pdf(
                            stream.read(),
                            pdf_dir,
                            pages,
                            display,
                            on_progress,
                            progress_state,
                        )
                else:
                    nested_label = safe_component(Path(parts[-1]).stem, f"嵌套压缩包_{depth + 1}")
                    with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as nested_file:
                        with archive.open(info) as stream:
                            shutil.copyfileobj(stream, nested_file, length=1024 * 1024)
                        nested_file.seek(0)
                        with zipfile.ZipFile(nested_file) as nested:
                            _collect_archive(
                                nested,
                                page_dir,
                                pages,
                                seen,
                                stats,
                                on_warning,
                                on_progress,
                                progress_state,
                                _merge_parts(archive_path, parts[:-1], [nested_label]),
                                depth + 1,
                            )
        except Exception as exc:
            stats["damaged"] += 1
            if on_warning:
                on_warning(f"已跳过无法读取的文件：{display}（{exc}）")
        finally:
            progress_state["done"] += 1
            if on_progress:
                on_progress(
                    progress_state["done"],
                    max(progress_state["total"], progress_state["done"]),
                    f"正在展开压缩包：{display}（{member_number}/{len(infos)}）",
                )


def collect_inputs(
    paths: list[Path],
    page_dir: Path,
    on_warning=None,
    on_progress: ProgressCallback | None = None,
) -> tuple[list[Path], str]:
    page_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    source_kind = "images"
    progress_state = {"done": 0, "total": len(paths)}

    for source_number, source in enumerate(paths, 1):
        suffix = source.suffix.lower()
        label = source_label(source)
        source_folder = f"{source_number:03d}_{label}"
        if on_progress:
            on_progress(
                progress_state["done"],
                max(progress_state["total"], 1),
                f"正在读取 {label}（文件 {source_number}/{len(paths)}）",
            )
        if suffix in IMAGE_EXTS:
            destination = page_dir / source_folder / f"{label}.png"
            _save_image(source, destination, pages)
            progress_state["done"] += 1
        elif suffix == ".pdf":
            source_kind = "pdf"
            progress_state["done"] += 1
            _render_pdf(source, page_dir / source_folder, pages, label, on_progress, progress_state)
        elif suffix in ARCHIVE_EXTS:
            source_kind = "cbz"
            stats = {
                "members": 0,
                "duplicates": 0,
                "too_small": 0,
                "damaged": 0,
                "unsupported": 0,
                "too_deep": 0,
            }
            seen: set[tuple[int, int]] = set()
            progress_state["done"] += 1
            before = len(pages)
            with zipfile.ZipFile(source) as archive:
                _collect_archive(
                    archive,
                    page_dir,
                    pages,
                    seen,
                    stats,
                    on_warning,
                    on_progress,
                    progress_state,
                    [source_folder],
                )
            if on_warning:
                on_warning(
                    f"压缩包预检：{label} 扫描 {stats['members']} 个文件，"
                    f"提取 {len(pages) - before} 页；重复 {stats['duplicates']}、"
                    f"过小 {stats['too_small']}、损坏 {stats['damaged']}、"
                    f"不支持 {stats['unsupported']}"
                )
        else:
            raise ValueError(f"不支持的文件类型：{source.name}")

    if not pages:
        raise ValueError(
            "没有找到可处理的漫画页面。请确认压缩包内包含图片、PDF、CBZ 或 ZIP；"
            "RAR/CBR/7Z 请先解压或转换为 ZIP。"
        )
    if on_progress:
        on_progress(1, 1, f"文档拆页完成，共 {len(pages)} 页，已按来源和章节归档")
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
