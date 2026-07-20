from __future__ import annotations

import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from comfy_colorizer import services, workflow_path
from comfy_jobs import JobManager
from comic_colorizer.documents import IMAGE_EXTS, ARCHIVE_EXTS

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
WORK = ROOT / "work"
UPLOADS = WORK / "incoming"
WORK.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
manager = JobManager(WORK)


def save_upload(item, index: int) -> Path:
    original = Path((item.filename or f"upload_{index}.png").replace("\\", "/"))
    safe = "_".join(part for part in original.parts if part not in {".", ".."})
    target = UPLOADS / f"{time.time_ns()}_{index}__{safe or 'upload.png'}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        item.save(target)
        return target
    except PermissionError:
        fallback = Path(tempfile.gettempdir()) / f"InkBloom_{uuid.uuid4().hex}{original.suffix.lower()}"
        item.save(fallback)
        return fallback


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/services")
def service_status():
    result = []
    for service in services():
        try:
            result.append(service.health())
        except Exception as exc:
            result.append({"name": service.name, "url": service.base_url, "online": False, "error": str(exc)})
    return jsonify({"workflow": str(workflow_path()), "services": result})


@app.post("/api/jobs")
def create_job():
    items = [item for item in request.files.getlist("files") if item.filename]
    if not items:
        return jsonify({"error": "请上传图片、文件夹中的图片、PDF 或 ZIP/CBZ 压缩包"}), 400
    uploads = [save_upload(item, index) for index, item in enumerate(items)]
    positive = request.form.get("positive", "给漫画进行上色，颜色不要太淡")
    negative = request.form.get("negative", "低饱和，灰度，褪色，未上色，模糊，重影，文字变形")
    def number(name: str):
        value = request.form.get(name, "").strip()
        return int(value) if value else None
    title = request.form.get("title") or Path(items[0].filename).stem
    job = manager.create(uploads, title, positive, negative, number("width"), number("height"))
    return jsonify({"job_id": job.id})


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(manager.status_json(job))


@app.get("/api/jobs/<job_id>/files/<path:filename>")
def job_file(job_id: str, filename: str):
    job = manager.jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    target = (job.root / filename).resolve()
    try:
        target.relative_to(job.root.resolve())
    except ValueError:
        return jsonify({"error": "非法路径"}), 400
    if not target.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(target)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=17860, threaded=True)
