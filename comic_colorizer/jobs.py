from __future__ import annotations

import shutil
import threading
import time
import uuid
import zipfile
import csv
from dataclasses import dataclass, field
from pathlib import Path

from .auto_references import collect_auto_references
from .colorizer import ColorSettings, make_colorizer
from .documents import (
    collect_inputs,
    export_completed_documents,
    finalize_document_exports,
    result_name,
    safe_component,
)
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
    source_pages: list[Path] = field(default_factory=list)
    reference_paths: list[Path] = field(default_factory=list)
    pending_decision: dict | None = None
    decision_result: dict | None = None
    decision_condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
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

    def ask_reference(
        self,
        page_index: int,
        candidates: list[int],
        previous: int | None,
        group_label: str,
        group_pages: int,
    ) -> dict:
        options = [
            {
                "id": f"reference:{index}",
                "label": f"样例 {index + 1}",
                "image": f"/reference-preview/{self.id}/{index}",
            }
            for index in candidates
        ]
        if previous is not None:
            options.insert(
                0,
                {
                    "id": f"reference:{previous}",
                    "label": f"沿用上一页（样例 {previous + 1}）",
                    "image": f"/reference-preview/{self.id}/{previous}",
                    "recommended": True,
                },
            )
        with self.decision_condition:
            self.decision_result = None
            self.pending_decision = {
                "kind": "reference",
                "page": page_index + 1,
                "title": f"为“{group_label}”选择主参考图",
                "message": (
                    f"这是该连续色调组的第一张底稿。你的选择将作为后续 {group_pages} 页的主画风，"
                    "请对照人物、服装和场景选择；也可以补充新的参考图。"
                ),
                "source": f"/source-preview/{self.id}/{page_index}",
                "options": options,
            }
            self.status = "waiting_user"
            self.message = f"第 {page_index + 1} 页等待你确认参考图"
            self.log(self.message)
            while self.decision_result is None and not self.cancel_requested:
                self.decision_condition.wait(timeout=1.0)
            self.raise_if_cancelled()
            result = self.decision_result or {}
            self.pending_decision = None
            self.status = "reference"
            self.log(f"第 {page_index + 1} 页已收到参考图选择，继续处理")
            return result

    def resolve_decision(self, result: dict) -> None:
        with self.decision_condition:
            self.decision_result = result
            self.decision_condition.notify_all()


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def create(self, uploads: list[Path], references: list[Path], title: str, settings: ColorSettings) -> Job:
        self.clean_old(keep=20)
        job_id = uuid.uuid4().hex[:10]
        work_name = f"{safe_component(title, '漫画', max_length=80)}_{job_id}"
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
        with job.decision_condition:
            job.decision_condition.notify_all()

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
        document_dir = job_dir / "已上色文档"
        out_dir = OUTPUT / job.work_name
        colored_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        original_references = list(references)
        try:
            reference_dir = job_dir / "references"
            reference_dir.mkdir(parents=True, exist_ok=True)
            local_references: list[Path] = []
            for index, reference in enumerate(references, 1):
                target = reference_dir / (
                    f"{index:03d}_{safe_component(reference.stem, '样例')}{reference.suffix.lower()}"
                )
                shutil.copy2(reference, target)
                local_references.append(target)
            references = local_references
            job.reference_paths = references
            job.update_stage("extract", 0, 1, "正在展开漫画文档")
            pages, source_kind, manifest = collect_inputs(
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
            job.source_pages = pages
            source_pages = list(pages)
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
            auto_reference_map = {}
            if settings.engine == "cobra" and settings.cobra_auto_color_reference:
                job.update_stage("reference", 0, 1, "正在识别本话官方彩色页作为参考图")
                auto_references, auto_reference_map = collect_auto_references(
                    pages,
                    reference_dir,
                    len(references),
                    on_progress=lambda current, total, message: job.update_stage(
                        "reference",
                        current,
                        total,
                        message,
                    ),
                )
                if auto_references:
                    references.extend(auto_references)
                    job.reference_paths = references
                    job.log(
                        f"已自动识别 {len(auto_references)} 张彩色页，将作为所在话/段落的优先参考图"
                    )
                else:
                    job.log("未检测到可靠的本话彩色页，继续使用上传的样例图")
            if settings.lineart_enhance:
                enhanced_dir = job_dir / "enhanced-lineart"
                pages = enhance_pages(pages, enhanced_dir, references, settings, job.update_stage)
                for preview in pages[:6]:
                    job.previews.append(f"/lineart-preview/{job.id}/{preview.name}")
                job.log("已使用增强线稿继续进入上色流程")
            else:
                job.update_stage("redraw", 1, 1, "未启用，保留原始线稿")
            engine = make_colorizer(references, settings)
            if hasattr(engine, "set_auto_references"):
                engine.set_auto_references(auto_reference_map)
            if hasattr(engine, "prepare_reference_plan"):
                engine.prepare_reference_plan(
                    pages,
                    lambda page_index, candidates, previous, group_label, group_pages: job.ask_reference(
                        page_index, candidates, previous, group_label, group_pages
                    ),
                    job.reference_paths,
                )
            job.raise_if_cancelled()
            results: list[Path] = []
            page_results: dict[Path, Path] = {}

            def flush_documents() -> None:
                try:
                    export_completed_documents(
                        page_results,
                        manifest,
                        [document_dir, out_dir],
                        on_export=lambda message: job.log(message, "success"),
                    )
                except Exception as exc:
                    job.log(f"来源文档增量导出失败，将在任务结束时重试：{exc}", "error")

            if hasattr(engine, "colorize_batch"):
                previewed: set[int] = set()

                def on_stage(stage: str, current: int, total: int, message: str) -> None:
                    job.raise_if_cancelled()
                    job.update_stage(stage, current, total, message)
                    if stage == "layers" and current > 0 and current not in previewed:
                        target = colored_dir / result_name(pages[current - 1], current)
                        page_results[source_pages[current - 1]] = target
                        flush_documents()
                        job.previews.append(f"/preview/{job.id}/{target.name}")
                        job.progress = current
                        previewed.add(current)

                results = engine.colorize_batch(pages, colored_dir, on_stage)
                for index, target in enumerate(results):
                    if index < len(source_pages):
                        page_results[source_pages[index]] = target
                flush_documents()
            else:
                job.update_stage("engine", 1, 1, "CPU 兼容引擎已加载")
                for index, page in enumerate(pages, 1):
                    job.raise_if_cancelled()
                    job.update_stage("stage1", index - 1, len(pages), f"正在上色 {index}/{len(pages)}")
                    target = colored_dir / result_name(page, index)
                    engine.colorize(page, target)
                    results.append(target)
                    page_results[source_pages[index - 1]] = target
                    flush_documents()
                    job.previews.append(f"/preview/{job.id}/{target.name}")
                    job.progress = index
                    job.update_stage("stage1", index, len(pages), f"已完成 {index}/{len(pages)}")
                for key in ("reference", "lineart", "stage2", "layers"):
                    job.update_stage(key, 1, 1, "兼容模式不使用此阶段")

            job.update_stage("export", 0, 1, "正在按原始文件名和格式生成成品")
            job.raise_if_cancelled()
            job.downloads = finalize_document_exports(
                page_results,
                manifest,
                document_dir,
                out_dir,
                job.title,
            )
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
            for reference in original_references:
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
