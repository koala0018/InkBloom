from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from comfy_colorizer import run_parallel
from comic_colorizer.documents import collect_inputs, natural_key, safe_component


@dataclass
class Job:
    id: str
    title: str
    root: Path
    positive: str
    negative: str
    width: int | None
    height: int | None
    status: str = "queued"
    progress: int = 0
    total: int = 0
    message: str = "等待"
    error: str | None = None
    logs: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] {message}")
        self.logs = self.logs[-200:]


class JobManager:
    def __init__(self, work: Path):
        self.work = work
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def create(self, uploads: list[Path], title: str, positive: str, negative: str, width: int | None, height: int | None) -> Job:
        job_id = uuid.uuid4().hex[:10]
        root = self.work / f"{safe_component(title, '漫画')}_{job_id}"
        root.mkdir(parents=True, exist_ok=True)
        job = Job(job_id, title, root, positive, negative, width, height)
        job.log("任务已创建，等待拆页")
        self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job, uploads), daemon=True).start()
        return job

    def _run(self, job: Job, uploads: list[Path]) -> None:
        try:
            page_dir = job.root / "pages"
            colored = job.root / "colored"
            job.status, job.message = "extract", "正在拆分输入文件"
            job.log("开始读取上传文件并拆分页面")
            pages, _kind, _manifest = collect_inputs(uploads, page_dir, on_progress=lambda done, total, msg: self._update(job, done, total, msg))
            pages = sorted(pages, key=natural_key)
            if not pages:
                raise ValueError("输入中没有找到可处理的图片页面")
            job.total = len(pages)
            job.status, job.progress, job.message = "comfy", 0, "等待两个 ComfyUI 服务"
            job.log(f"拆页完成，共 {job.total} 页；等待 8188/8189")
            run_parallel(pages, colored, job.positive, job.negative, job.width, job.height, lambda done, total, msg: self._update(job, done, total, msg), page_root=page_dir)
            job.status, job.progress, job.message = "done", job.total, f"完成：{job.total} 页"
            job.log(f"任务完成，共生成 {job.total} 页")
        except Exception as exc:
            job.status, job.error, job.message = "error", str(exc), str(exc)
            job.log(f"错误：{exc}")

    @staticmethod
    def _update(job: Job, done: int, total: int, message: str) -> None:
        job.progress, job.total, job.message = done, total, message
        if done == 0 or done == total or done % 5 == 0 or job.status == "extract":
            job.log(message)

    def status_json(self, job: Job) -> dict:
        percent = round(job.progress * 100 / job.total, 1) if job.total else 0
        return {"id": job.id, "title": job.title, "status": job.status, "progress": job.progress, "total": job.total, "percent": percent, "message": job.message, "error": job.error, "logs": job.logs[-80:], "work_dir": str(job.root), "colored_dir": str(job.root / 'colored')}
