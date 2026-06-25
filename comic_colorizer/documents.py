from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
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


@dataclass
class SourceDocument:
    """One output document matching one document from the user's input."""

    container: str
    relative_path: Path
    kind: str
    pages: list[Path] = field(default_factory=list)


@dataclass
class CollectionManifest:
    documents: list[SourceDocument] = field(default_factory=list)
    containers: dict[str, str] = field(default_factory=dict)


def natural_key(path: Path | str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(path))]


def safe_component(value: str, fallback: str = "未命名", max_length: int = 180) -> str:
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", value).strip(" ._")
    value = re.sub(r"\s+", " ", value)
    return value[:max_length] or fallback


def source_label(source: Path) -> str:
    # Uploads are stored as <timestamp>_<index>__<original name>.
    name = re.sub(r"^\d+_\d+__", "", source.name)
    return safe_component(Path(name).stem, "漫画")


def original_upload_name(source: Path) -> str:
    return re.sub(r"^\d+_\d+__", "", source.name)


def result_name(page: Path, index: int, suffix: str = "colored", extension: str = ".jpg") -> str:
    parents = [safe_component(part, max_length=52) for part in page.parts[-3:-1]]
    context = "_".join(parents[-2:])
    stem = safe_component(page.stem, f"page_{index:05d}", max_length=72)
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
    document: SourceDocument | None = None,
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
            if document is not None:
                document.pages.append(target)
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


def _render_archived_pdf(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination_dir: Path,
    pages: list[Path],
    label: str,
    on_progress: ProgressCallback | None,
    progress_state: dict[str, int],
    document: SourceDocument,
) -> int:
    # PyMuPDF can memory-map a filename, but opening bytes forces the complete
    # embedded PDF into RAM. Large comic collections contain hundreds of PDFs,
    # so stream each member to a short-lived file beside the job instead.
    temp_dir = destination_dir.parents[1] / "_extract-temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".pdf",
            prefix="embedded_",
            dir=temp_dir,
            delete=False,
        ) as temporary:
            temp_path = Path(temporary.name)
            with archive.open(info) as stream:
                shutil.copyfileobj(stream, temporary, length=1024 * 1024)
        return _render_pdf(
            temp_path,
            destination_dir,
            pages,
            label,
            on_progress,
            progress_state,
            document,
        )
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        try:
            temp_dir.rmdir()
        except OSError:
            pass


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
    manifest: CollectionManifest,
    container: str,
    output_path: list[str],
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
                    document = SourceDocument(
                        container=container,
                        relative_path=Path(*_merge_parts(output_path, parts)),
                        kind="pdf",
                    )
                    manifest.documents.append(document)
                    _render_archived_pdf(
                        archive,
                        info,
                        pdf_dir,
                        pages,
                        display,
                        on_progress,
                        progress_state,
                        document,
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
                                manifest,
                                container,
                                _merge_parts(output_path, parts[:-1], [nested_label]),
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
) -> tuple[list[Path], str, CollectionManifest]:
    page_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    source_kind = "images"
    manifest = CollectionManifest()
    progress_state = {"done": 0, "total": len(paths)}

    for source_number, source in enumerate(paths, 1):
        suffix = source.suffix.lower()
        label = source_label(source)
        source_folder = f"{source_number:03d}_{label}"
        original_name = original_upload_name(source)
        if on_progress:
            on_progress(
                progress_state["done"],
                max(progress_state["total"], 1),
                f"正在读取 {label}（文件 {source_number}/{len(paths)}）",
            )
        if suffix in IMAGE_EXTS:
            destination = page_dir / source_folder / f"{label}.png"
            _save_image(source, destination, pages)
            manifest.documents.append(
                SourceDocument(
                    container=source_folder,
                    relative_path=Path(original_name),
                    kind="image",
                    pages=[pages[-1]],
                )
            )
            manifest.containers[source_folder] = original_name
            progress_state["done"] += 1
        elif suffix == ".pdf":
            source_kind = "pdf"
            progress_state["done"] += 1
            document = SourceDocument(
                container=source_folder,
                relative_path=Path(original_name),
                kind="pdf",
            )
            manifest.documents.append(document)
            manifest.containers[source_folder] = original_name
            _render_pdf(
                source,
                page_dir / source_folder,
                pages,
                label,
                on_progress,
                progress_state,
                document,
            )
        elif suffix in ARCHIVE_EXTS:
            source_kind = "cbz"
            manifest.containers[source_folder] = original_name
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
                    manifest,
                    source_folder,
                    [],
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
    return pages, source_kind, manifest


def _colored_filename(name: str) -> str:
    path = Path(name)
    return f"已上色_{path.stem}{path.suffix.lower()}"


def document_output_path(
    root: Path,
    document: SourceDocument,
    manifest: CollectionManifest,
) -> Path:
    container_name = manifest.containers[document.container]
    if Path(container_name).suffix.lower() in ARCHIVE_EXTS:
        archive_folder = safe_component(Path(container_name).stem, "压缩包")
        return (
            root
            / archive_folder
            / document.relative_path.parent
            / _colored_filename(document.relative_path.name)
        )
    return root / _colored_filename(document.relative_path.name)


def _write_pdf(images: list[Path], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".partial.pdf")
    temporary.unlink(missing_ok=True)
    pdf = fitz.open()
    try:
        for image_path in images:
            with Image.open(image_path) as image:
                width, height = image.size
            page = pdf.new_page(width=width, height=height)
            page.insert_image(page.rect, filename=str(image_path))
        pdf.save(temporary, deflate=True, garbage=3)
    finally:
        pdf.close()
    temporary.replace(destination)


def export_completed_documents(
    page_results: dict[Path, Path],
    manifest: CollectionManifest,
    roots: list[Path],
    on_export=None,
) -> list[Path]:
    """Incrementally export every source document whose pages are complete."""
    generated: list[Path] = []
    normalized = {page.resolve(): result for page, result in page_results.items()}
    for document in manifest.documents:
        if not document.pages or not all(page.resolve() in normalized for page in document.pages):
            continue
        images = [normalized[page.resolve()] for page in document.pages]
        created = False
        for root in roots:
            destination = document_output_path(root, document, manifest)
            if destination.exists():
                generated.append(destination)
                continue
            if document.kind == "pdf":
                _write_pdf(images, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(images[0], destination.with_suffix(images[0].suffix))
            generated.append(destination)
            created = True
        if created and on_export:
            on_export(f"已按原文件生成：{_colored_filename(document.relative_path.name)}")
    return generated


def finalize_document_exports(
    page_results: dict[Path, Path],
    manifest: CollectionManifest,
    work_output_dir: Path,
    output_dir: Path,
    title: str,
) -> dict[str, str]:
    export_completed_documents(page_results, manifest, [work_output_dir, output_dir])
    safe_title = safe_component(title, "漫画")
    generated = [
        document_output_path(output_dir, document, manifest)
        for document in manifest.documents
        if document_output_path(output_dir, document, manifest).exists()
    ]
    downloads: dict[str, str] = {}
    if len(generated) == 1 and generated[0].suffix.lower() == ".pdf":
        downloads["pdf"] = str(generated[0].relative_to(output_dir)).replace("\\", "/")

    for container, original_name in manifest.containers.items():
        if Path(original_name).suffix.lower() not in ARCHIVE_EXTS:
            continue
        archive_folder = safe_component(Path(original_name).stem, "压缩包")
        members = [path for path in generated if archive_folder in path.relative_to(output_dir).parts]
        if not members:
            continue
        bundle = output_dir / _colored_filename(original_name)
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in members:
                archive.write(path, path.relative_to(output_dir / archive_folder))
        downloads["bundle"] = bundle.name

    if len(generated) > 1 and "bundle" not in downloads:
        bundle = output_dir / f"{safe_title}_已上色文件合集.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in generated:
                archive.write(path, path.relative_to(output_dir))
        downloads["bundle"] = bundle.name
    return downloads
