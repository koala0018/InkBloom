from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from comic_colorizer.colorizer import ColorSettings
from comic_colorizer.jobs import JobManager
from comic_colorizer.paths import OUTPUT, ROOT, WORK, ensure_dirs, portable_env, resource_path

ensure_dirs()
portable_env()

app = Flask(
    __name__,
    template_folder=str(resource_path("templates")),
    static_folder=str(resource_path("static")),
)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
manager = JobManager()


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


@app.get("/")
def index():
    return render_template("index.html", lan_url=f"http://{local_ip()}:17860")


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
        engine=request.form.get("engine", "manganinja"),
        saturation=float(request.form.get("saturation", 1.05)),
        strength=float(request.form.get("strength", 0.95)),
        line_protection=float(request.form.get("line_protection", 0.82)),
        reference_strength=float(request.form.get("reference_strength", 0.90)),
    )
    job = manager.create(saved, references, request.form.get("title") or first_name, settings)
    return jsonify({"job_id": job.id})


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify({
        "id": job.id,
        "title": job.title,
        "status": job.status,
        "progress": job.progress,
        "total": job.total,
        "message": job.message,
        "previews": job.previews,
        "downloads": {kind: f"/download/{job.id}/{name}" for kind, name in job.downloads.items()},
        "error": job.error,
    })


@app.get("/preview/<job_id>/<name>")
def preview(job_id: str, name: str):
    return send_from_directory(WORK / job_id / "colored", name)


@app.get("/download/<job_id>/<name>")
def download(job_id: str, name: str):
    return send_from_directory(OUTPUT / job_id, name, as_attachment=True)


@app.get("/manifest.webmanifest")
def manifest():
    return app.send_static_file("manifest.webmanifest")


def main() -> None:
    # Windowed PyInstaller builds have no stdout/stderr; Werkzeug expects writable streams.
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
