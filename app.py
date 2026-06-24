from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from comic_colorizer.colorizer import ColorSettings
from comic_colorizer.cobra_engine import CobraColorizer
from comic_colorizer.jobs import JobManager
from comic_colorizer.paths import MODELS, OUTPUT, ROOT, WORK, ensure_dirs, portable_env, resource_path

ensure_dirs()
portable_env()

app = Flask(
    __name__,
    template_folder=str(resource_path("templates")),
    static_folder=str(resource_path("static")),
)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
manager = JobManager()


def wants_json() -> bool:
    return request.path.startswith("/api/")


@app.errorhandler(404)
def not_found(_error):
    if wants_json():
        return jsonify({"error": "任务不存在或程序刚刚重启，请重新提交一次。"}), 404
    return render_template(
        "index.html",
        lan_url=f"http://{local_ip()}:17860",
        style2paints_ready=(MODELS / "style2paints" / "READY").exists(),
        cobra_ready=CobraColorizer.available(),
    ), 404


@app.errorhandler(500)
def server_error(error):
    if wants_json():
        return jsonify({"error": f"后端处理异常：{error}"}), 500
    return jsonify({"error": str(error)}), 500


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


@app.get("/")
def index():
    return render_template(
        "index.html",
        lan_url=f"http://{local_ip()}:17860",
        style2paints_ready=(MODELS / "style2paints" / "READY").exists(),
        cobra_ready=CobraColorizer.available(),
    )


@app.get("/api/engines")
def engine_status():
    return jsonify({
        "style2paints": (MODELS / "style2paints" / "READY").exists(),
        "cobra": CobraColorizer.available(),
        "onnx": resource_path("assets/models/eccv16-colorizer.onnx").exists(),
    })


@app.post("/api/jobs")
def create_job():
    files = request.files.getlist("files")
    if not files or not any(item.filename for item in files):
        return jsonify({"error": "请选择图片、PDF、CBZ 或 ZIP"}), 400
    upload_dir = WORK / "incoming"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    stamp = str(time.time_ns())
    for index, item in enumerate(files):
        if not item.filename:
            continue
        name = secure_filename(item.filename) or f"upload_{index}"
        path = upload_dir / f"{stamp}_{index}_{name}"
        item.save(path)
        saved.append(path)

    references: list[Path] = []
    for index, reference_file in enumerate(request.files.getlist("references")):
        if not reference_file.filename:
            continue
        ref_name = secure_filename(reference_file.filename) or f"reference_{index}.png"
        reference = upload_dir / f"{stamp}_ref_{index}_{ref_name}"
        reference_file.save(reference)
        references.append(reference)

    first_name = Path(files[0].filename or "comic").stem
    settings = ColorSettings(
        engine=request.form.get("engine", "cobra"),
        saturation=float(request.form.get("saturation", 1.05)),
        strength=float(request.form.get("strength", 0.95)),
        line_protection=float(request.form.get("line_protection", 0.82)),
        reference_strength=float(request.form.get("reference_strength", 0.90)),
        s2p_stage=request.form.get("s2p_stage", "careful"),
        s2p_finish=request.form.get("s2p_finish", "blended_smoothed"),
        s2p_save_layers=request.form.get("s2p_save_layers", "on") == "on",
        s2p_hint_points=request.form.get("s2p_hint_points", "[]"),
        cobra_style=request.form.get("cobra_style", "line_shadow"),
        cobra_steps=int(request.form.get("cobra_steps", 10)),
        cobra_top_k=int(request.form.get("cobra_top_k", 4)),
        cobra_seed=int(request.form.get("cobra_seed", 1)),
        cobra_preserve_lines=float(request.form.get("cobra_preserve_lines", 0.88)),
        cobra_color_strength=float(request.form.get("cobra_color_strength", 0.96)),
        lineart_enhance=request.form.get("lineart_enhance", "") == "on",
        lineart_backend="safe",
        lineart_strength=float(request.form.get("lineart_strength", 0.65)),
        lineart_detail=float(request.form.get("lineart_detail", 0.60)),
        lineart_weight=float(request.form.get("lineart_weight", 0.55)),
        lineart_prompt=request.form.get("lineart_prompt", ColorSettings.lineart_prompt),
        lineart_negative=request.form.get("lineart_negative", ColorSettings.lineart_negative),
    )
    job = manager.create(saved, references, request.form.get("title") or first_name, settings)
    return jsonify({"job_id": job.id})


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在或程序刚刚重启，请重新提交一次。"}), 404
    return jsonify({
        "id": job.id,
        "title": job.title,
        "status": job.status,
        "progress": job.progress,
        "total": job.total,
        "overall_progress": job.overall_progress,
        "message": job.message,
        "stages": list(job.stages.values()),
        "logs": job.logs,
        "previews": job.previews,
        "downloads": {kind: f"/download/{job.id}/{name}" for kind, name in job.downloads.items()},
        "error": job.error,
    })


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在或程序刚刚重启，请重新提交一次。"}), 404
    manager.cancel(job_id)
    return jsonify({"ok": True})


@app.get("/preview/<job_id>/<name>")
def preview(job_id: str, name: str):
    return send_from_directory(WORK / job_id / "colored", name)


@app.get("/lineart-preview/<job_id>/<name>")
def lineart_preview(job_id: str, name: str):
    return send_from_directory(WORK / job_id / "enhanced-lineart", name)


@app.get("/download/<job_id>/<name>")
def download(job_id: str, name: str):
    return send_from_directory(OUTPUT / job_id, name, as_attachment=True)


@app.get("/manifest.webmanifest")
def manifest():
    return app.send_static_file("manifest.webmanifest")


def main() -> None:
    log = open(ROOT / "InkBloom.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log
    port = int(os.getenv("INKBLOOM_PORT", "17860"))
    url = f"http://127.0.0.1:{port}"
    print(f"Starting InkBloom at {url}")
    threading.Timer(1.1, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        (ROOT / "InkBloom-startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
