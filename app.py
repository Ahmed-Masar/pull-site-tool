#!/usr/bin/env python3
"""
app.py - Flask API wrapping pull_site.py's download+cleanup pipeline.

Endpoints:
    POST /api/pull-site        {"url": "...", "project_name": "..."} -> starts a background job
    GET  /api/status/<job_id>  -> job status + report once finished
    GET  /preview/<project>/   -> serves the cleaned mirror for manual browsing
    GET  /healthz               -> health check

Note: jobs are kept in an in-memory dict, so this only works correctly behind
a single process/worker (no horizontal scaling, no persistence across restarts).
"""
import os
import re
import threading
import uuid

from flask import Flask, abort, jsonify, request, send_from_directory

import pull_site

app = Flask(__name__)

SITES_DIR = os.environ.get("SITES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites"))
os.makedirs(SITES_DIR, exist_ok=True)

PROJECT_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')

jobs = {}
jobs_lock = threading.Lock()


def _run_job(job_id, url, project_name):
    with jobs_lock:
        jobs[job_id]["status"] = "running"

    def log(stage):
        with jobs_lock:
            jobs[job_id]["stage"] = stage

    try:
        report = pull_site.run_pipeline(url, project_name, SITES_DIR, log=log)
        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["report"] = report
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.post("/api/pull-site")
def pull_site_endpoint():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    project_name = data.get("project_name")

    if not url:
        return jsonify(error="url is required"), 400
    if project_name and not PROJECT_NAME_RE.match(project_name):
        return jsonify(error="project_name may only contain letters, numbers, '-' and '_'"), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"status": "pending", "stage": "queued", "url": url, "project_name": project_name}

    thread = threading.Thread(target=_run_job, args=(job_id, url, project_name), daemon=True)
    thread.start()

    return jsonify(job_id=job_id, status="started"), 202


@app.get("/api/status/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="job not found"), 404
    return jsonify(job)


@app.get("/preview/<project>/")
@app.get("/preview/<project>")
def preview_root(project):
    return preview_file(project, "index.html")


@app.get("/preview/<project>/<path:filepath>")
def preview_file(project, filepath):
    if not PROJECT_NAME_RE.match(project):
        abort(404)
    project_dir = os.path.join(SITES_DIR, project)
    if not os.path.isdir(project_dir):
        abort(404)
    mirror_root = pull_site.discover_mirror_root(project_dir)
    return send_from_directory(mirror_root, filepath)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
