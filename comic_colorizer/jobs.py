from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .colorizer import ColorSettings, make_colorizer
from .documents import collect_inputs, export_results
from .paths import OUTPUT, WORK


@dataclass
class Job:
    id: str
    title: str
    status: str = "queued"
    progress: int = 0
    total: int = 0
    message: str = "等待处理"
    previews: list[str] = field(default_factory=list)
    downloads: dict[str, str] = field(default_factory=dict)
    error: str | None = None


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def create(self, uploads: list[Path], reference: Path | None, title: str, settings: ColorSettings) -> Job:
        job_id = uuid.uuid4().hex[:10]
        job = Job(id=job_id, title=title)
        self.jobs[job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job, uploads, reference, settings), daemon=True
        )
        thread.start()
        return job

    def _run(self, job: Job, uploads: list[Path], reference: Path | None, settings: ColorSettings):
        job_dir = WORK / job.id
        page_dir = job_dir / "pages"
        colored_dir = job_dir / "colored"
        colored_dir.mkdir(parents=True, exist_ok=True)
        try:
            job.status = "extracting"
            job.message = "正在展开漫画文档"
            pages, source_kind = collect_inputs(uploads, page_dir)
            job.total = len(pages)
            engine = make_colorizer(reference, settings)
            results: list[Path] = []
            job.status = "colorizing"
            for index, page in enumerate(pages, 1):
                job.message = f"正在上色 {index}/{len(pages)}"
                target = colored_dir / f"colored_{index:05d}.jpg"
                engine.colorize(page, target)
                results.append(target)
                job.previews.append(f"/preview/{job.id}/{target.name}")
                job.progress = index
            job.status = "exporting"
            job.message = "正在生成 PDF 和 CBZ"
            out_dir = OUTPUT / job.id
            job.downloads = export_results(results, out_dir, job.title, source_kind)
            job.status = "done"
            job.message = "完成"
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.message = "处理失败"
        finally:
            for upload in uploads:
                upload.unlink(missing_ok=True)
            if reference:
                reference.unlink(missing_ok=True)

    def clean_old(self) -> None:
        for path in WORK.iterdir() if WORK.exists() else []:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
