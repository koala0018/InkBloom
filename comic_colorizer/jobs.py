from __future__ import annotations

import shutil
import threading
import time
import uuid
import zipfile
import csv
from dataclasses import dataclass, field
from pathlib import Path

from .colorizer import ColorSettings, make_colorizer
from .documents import collect_inputs, export_results, result_name, safe_component
from .lineart_enhancer import enhance_pages
from .paths import OUTPUT, WORK


STAGE_LABELS = {
    "extract": "文档拆页",
    "redraw": "线稿增强 / 重绘",
    "engine": "加载上色引擎",
    "reference": "多样例检索与匹配",
    "lineart": "线稿清理与分区",
    "stage1": "Stage I 固有色",
    "stage2": "Stage II 精细渲染",
    "layers": "输出分层结果",
    "export": "生成 PDF / CBZ",
}


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    title: str
    work_name: str = ""
    status: str = "queued"
    progress: int = 0
    total: int = 0
    overall_progress: int = 0
    message: str = "等待处理"
    previews: list[str] = field(default_factory=list)
    downloads: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    cancel_requested: bool = False
    stages: dict[str, dict] = field(
        default_factory=lambda: {
            key: {"key": key, "label": label, "status": "pending", "progress": 0, "message": "等待"}
            for key, label in STAGE_LABELS.items()
        }
    )
    logs: list[dict] = field(default_factory=list)

    def log(self, message: str, level: str = "info") -> None:
        self.logs.append({"time": time.strftime("%H:%M:%S"), "level": level, "message": message})
        if len(self.logs) > 300:
            del self.logs[:-300]

    def update_stage(self, key: str, current: int, total: int, message: str) -> None:
        self.raise_if_cancelled()
        if key not in self.stages:
            return
        percent = 100 if total <= 0 else max(0, min(100, round(current / total * 100)))
        stage = self.stages[key]
        # Nested archives and embedded PDFs reveal additional work while being
        # scanned. Keep the visible bar monotonic even when the total grows.
        percent = max(stage["progress"], percent)
        stage["progress"] = percent
        stage["message"] = message
        stage["status"] = "done" if percent >= 100 else "running"
        self.status = key
        self.message = message
        self.overall_progress = round(sum(item["progress"] for item in self.stages.values()) / len(self.stages))
        self.log(message)

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise JobCancelled("任务已取消")


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def create(self, uploads: list[Path], references: list[Path], title: str, settings: ColorSettings) -> Job:
        self.clean_old(keep=5)
        job_id = uuid.uuid4().hex[:10]
        work_name = f"{safe_component(title, '漫画')}_{job_id}"
        job = Job(id=job_id, title=title, work_name=work_name)
        job.log("任务已创建，文件仅在本机处理")
        job.log(f"过程文件：{WORK / work_name}；最终成品：{OUTPUT / work_name}")
        self.jobs[job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job, uploads, references, settings), daemon=True
        )
        thread.start()
        return job

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        job.cancel_requested = True
        job.status = "cancelled"
        job.message = "正在取消任务"
        job.log("已请求取消任务；当前页如果正在提交给 Style2Paints，会在本页返回后停止。", "error")

    @staticmethod
    def _pack_layers(layer_dir: Path, out_dir: Path, title: str) -> str | None:
        if not layer_dir.exists():
            return None
        name = f"{title}_Style2Paints分层.zip"
        destination = out_dir / name
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(layer_dir.rglob("*.png")):
                archive.write(path, path.relative_to(layer_dir))
        return name

    @staticmethod
    def _pack_lineart(lineart_dir: Path, out_dir: Path, title: str) -> str | None:
        if not lineart_dir.exists():
            return None
        name = f"{title}_enhanced_lineart.zip"
        destination = out_dir / name
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, path in enumerate(sorted(lineart_dir.glob("*.png")), 1):
                archive.write(path, f"{index:05d}.png")
        return name

    def _run(self, job: Job, uploads: list[Path], references: list[Path], settings: ColorSettings):
        job_dir = WORK / job.work_name
        page_dir = job_dir / "pages"
        colored_dir = job_dir / "colored"
        colored_dir.mkdir(parents=True, exist_ok=True)
        try:
            job.update_stage("extract", 0, 1, "正在展开漫画文档")
            pages, source_kind = collect_inputs(
                uploads,
                page_dir,
                on_warning=lambda message: job.log(
                    message,
                    "info" if message.startswith("压缩包预检：") else "error",
                ),
                on_progress=lambda current, total, message: job.update_stage(
                    "extract", current, total, message
                ),
            )
            job.raise_if_cancelled()
            job.total = len(pages)
            job.update_stage("extract", 1, 1, f"文档拆页完成，共 {job.total} 页")
            index_path = job_dir / "页面索引.csv"
            with index_path.open("w", encoding="utf-8-sig", newline="") as index_file:
                writer = csv.writer(index_file)
                writer.writerow(["全局页码", "来源/章节", "过程页面文件", "预计上色文件"])
                for index, page in enumerate(pages, 1):
                    relative = page.relative_to(page_dir)
                    writer.writerow([
                        index,
                        str(relative.parent),
                        str(relative),
                        result_name(page, index),
                    ])
            job.log(f"页面归类索引已保存：{index_path}")
            if job.total > 250:
                job.log(f"超大任务将按每 250 页自动分卷导出，共约 {(job.total + 249) // 250} 卷")
            if settings.lineart_enhance:
                enhanced_dir = job_dir / "enhanced-lineart"
                pages = enhance_pages(pages, enhanced_dir, references, settings, job.update_stage)
                for preview in pages[:6]:
                    job.previews.append(f"/lineart-preview/{job.id}/{preview.name}")
                job.log("已使用增强线稿继续进入上色流程")
            else:
                job.update_stage("redraw", 1, 1, "未启用，保留原始线稿")
            engine = make_colorizer(references, settings)
            job.raise_if_cancelled()
            results: list[Path] = []

            if hasattr(engine, "colorize_batch"):
                previewed: set[int] = set()

                def on_stage(stage: str, current: int, total: int, message: str) -> None:
                    job.raise_if_cancelled()
                    job.update_stage(stage, current, total, message)
                    if stage == "layers" and current > 0 and current not in previewed:
                        target = colored_dir / result_name(pages[current - 1], current)
                        job.previews.append(f"/preview/{job.id}/{target.name}")
                        job.progress = current
                        previewed.add(current)

                results = engine.colorize_batch(pages, colored_dir, on_stage)
            else:
                job.update_stage("engine", 1, 1, "CPU 兼容引擎已加载")
                for index, page in enumerate(pages, 1):
                    job.raise_if_cancelled()
                    job.update_stage("stage1", index - 1, len(pages), f"正在上色 {index}/{len(pages)}")
                    target = colored_dir / result_name(page, index)
                    engine.colorize(page, target)
                    results.append(target)
                    job.previews.append(f"/preview/{job.id}/{target.name}")
                    job.progress = index
                    job.update_stage("stage1", index, len(pages), f"已完成 {index}/{len(pages)}")
                for key in ("reference", "lineart", "stage2", "layers"):
                    job.update_stage(key, 1, 1, "兼容模式不使用此阶段")

            job.update_stage("export", 0, 1, "正在生成 PDF、CBZ 与分层压缩包")
            job.raise_if_cancelled()
            out_dir = OUTPUT / job.work_name
            job.downloads = export_results(results, out_dir, job.title, source_kind)
            layer_name = self._pack_layers(job_dir / "style2paints-layers", out_dir, job.title)
            if layer_name:
                job.downloads["layers"] = layer_name
            lineart_name = self._pack_lineart(job_dir / "enhanced-lineart", out_dir, job.title)
            if lineart_name:
                job.downloads["lineart"] = lineart_name
            job.update_stage("export", 1, 1, "导出完成")
            job.status = "done"
            job.overall_progress = 100
            job.message = "全部完成"
            job.log("任务完成", "success")
        except JobCancelled as exc:
            job.status = "cancelled"
            job.error = None
            job.message = "任务已取消"
            job.log(str(exc), "error")
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            job.message = "处理失败"
            failed_stage = next(
                (stage for stage in job.stages.values() if stage["status"] == "running"),
                next((stage for stage in job.stages.values() if stage["status"] == "pending"), None),
            )
            if failed_stage:
                failed_stage["status"] = "error"
                failed_stage["message"] = str(exc)
            job.log(str(exc), "error")
        finally:
            for upload in uploads:
                upload.unlink(missing_ok=True)
            for reference in references:
                reference.unlink(missing_ok=True)

    def clean_old(self, keep: int = 5) -> None:
        if not WORK.exists():
            return
        active = {
            job.work_name
            for job in self.jobs.values()
            if job.status not in {"done", "error", "cancelled"}
        }
        candidates = sorted(
            (
                path
                for path in WORK.iterdir()
                if path.is_dir() and path.name != "incoming" and path.name not in active
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates[keep:]:
            shutil.rmtree(path, ignore_errors=True)
