#!/usr/bin/env python3
"""
app.py - Flask API wrapping pull_site.py's download+cleanup pipeline.

Endpoints:
    POST /api/pull-site             {"url": "...", "project_name": "..."} -> starts a background job
    GET  /api/status/<job_id>       -> job status + report once finished
    GET  /api/github/repos          -> lists repos on GITHUB_ORG (org), or on the account owning
                                        GITHUB_TOKEN if GITHUB_ORG is unset
    POST /api/push-to-github        {"project_name", "repo", "default_branch", "url"} -> starts a background push job
    GET  /api/push-status/<job_id>  -> push job status
    GET  /preview/<project>/        -> serves the cleaned mirror for manual browsing
    GET  /healthz                    -> health check

Note: jobs are kept in an in-memory dict, so this only works correctly behind
a single process/worker (no horizontal scaling, no persistence across restarts).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_from_directory

import pull_site

app = Flask(__name__)

SITES_DIR = os.environ.get("SITES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites"))
os.makedirs(SITES_DIR, exist_ok=True)

# How long a finished job (and its mirrored site on disk) is kept around if the
# client never sends an explicit /api/cleanup (tab crash, force-quit, etc.).
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "1800"))
# Backstop for directories with no matching job at all (e.g. a job thread that
# hung/crashed before ever reaching "done"/"failed", so it never got a
# finished_at for JOB_TTL_SECONDS to act on). Anything this old and untracked
# gets deleted regardless of job state.
ORPHAN_MAX_AGE_SECONDS = int(os.environ.get("ORPHAN_MAX_AGE_SECONDS", str(2 * 60 * 60)))
CLEANUP_INTERVAL_SECONDS = 60

PROJECT_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')
REPO_FULL_NAME_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$')

GITHUB_API = "https://api.github.com"
GIT_AUTHOR_NAME = "Pull Site Bot"
GIT_AUTHOR_EMAIL = "bot@pullsite.app"

jobs = {}
jobs_lock = threading.Lock()

push_jobs = {}
push_jobs_lock = threading.Lock()


def _job_project_name(job):
    report = job.get("report") or {}
    return report.get("project_name") or job.get("project_name")


def _delete_project_dir(project_name):
    if not project_name or not PROJECT_NAME_RE.match(project_name):
        return
    project_dir = os.path.join(SITES_DIR, project_name)
    shutil.rmtree(project_dir, ignore_errors=True)


def _sweep_orphaned_dirs():
    """Deletes any directory under SITES_DIR that isn't tied to a job currently
    tracked in memory and hasn't been touched in a long time. This is the
    backstop for jobs whose thread hung or crashed before ever reaching
    "done"/"failed" — those never get a finished_at, so the job-based TTL
    cleanup above has nothing to act on and would otherwise leak forever."""
    try:
        entries = os.listdir(SITES_DIR)
    except OSError:
        return

    with jobs_lock:
        active_names = {_job_project_name(job) for job in jobs.values()}

    now = time.time()
    for name in entries:
        if name in active_names or not PROJECT_NAME_RE.match(name):
            continue
        full = os.path.join(SITES_DIR, name)
        if not os.path.isdir(full):
            continue
        try:
            age = now - os.path.getmtime(full)
        except OSError:
            continue
        if age > ORPHAN_MAX_AGE_SECONDS:
            shutil.rmtree(full, ignore_errors=True)


def _cleanup_expired_jobs():
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()

        to_delete = []
        with jobs_lock:
            for job_id, job in list(jobs.items()):
                finished_at = job.get("finished_at")
                if finished_at and now - finished_at > JOB_TTL_SECONDS:
                    to_delete.append((job_id, _job_project_name(job)))
            for job_id, _ in to_delete:
                del jobs[job_id]
        for _, project_name in to_delete:
            _delete_project_dir(project_name)

        with push_jobs_lock:
            expired_push_ids = [
                job_id for job_id, job in push_jobs.items()
                if job.get("finished_at") and now - job["finished_at"] > JOB_TTL_SECONDS
            ]
            for job_id in expired_push_ids:
                del push_jobs[job_id]

        _sweep_orphaned_dirs()


def _run_job(job_id, url, project_name):
    project_name = project_name or pull_site.slugify(urlsplit(url).netloc)
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["project_name"] = project_name

    def log(stage):
        with jobs_lock:
            jobs[job_id]["stage"] = stage

    try:
        report = pull_site.run_pipeline(url, project_name, SITES_DIR, log=log)
        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["report"] = report
            jobs[job_id]["finished_at"] = time.time()
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["finished_at"] = time.time()


def _github_token():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not configured on the server")
    return token


def _github_api_get(path, token):
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pull-site-tool",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API error ({e.code}): {body[:200]}")


def list_github_repos(token):
    org = os.environ.get("GITHUB_ORG")
    path_base = f"/orgs/{org}/repos" if org else "/user/repos?affiliation=owner"
    sep = "&" if "?" in path_base else "?"

    repos = []
    page = 1
    while page <= 5:
        data = _github_api_get(f"{path_base}{sep}per_page=100&page={page}&sort=updated", token)
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    return [
        {
            "full_name": r["full_name"],
            "private": r["private"],
            "default_branch": r["default_branch"],
            "updated_at": r["updated_at"],
            "html_url": r["html_url"],
        }
        for r in repos
    ]


def push_project_to_github(project_dir, repo_full_name, default_branch, source_url, token, log=lambda msg: None):
    mirror_root = pull_site.discover_mirror_root(project_dir)

    def run(cmd):
        result = subprocess.run(cmd, cwd=mirror_root, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.replace(token, "***").strip()
            raise RuntimeError(f"`{' '.join(cmd[:2])}` failed: {stderr}")
        return result

    log("Preparing git repository...")
    if not os.path.isdir(os.path.join(mirror_root, ".git")):
        run(["git", "init", "-q"])
    run(["git", "checkout", "-B", default_branch])
    run(["git", "add", "-A"])
    run([
        "git", "-c", f"user.name={GIT_AUTHOR_NAME}", "-c", f"user.email={GIT_AUTHOR_EMAIL}",
        "commit", "-m", f"Pull & clean: {source_url}", "--allow-empty",
    ])

    log(f"Pushing to {repo_full_name}...")
    remote_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    run(["git", "push", "--force", remote_url, f"HEAD:{default_branch}"])

    return f"https://github.com/{repo_full_name}"


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
        jobs[job_id] = {
            "status": "pending",
            "stage": "queued",
            "url": url,
            "project_name": project_name,
            "created_at": time.time(),
        }

    thread = threading.Thread(target=_run_job, args=(job_id, url, project_name), daemon=True)
    thread.start()

    return jsonify(job_id=job_id, status="started"), 202


@app.post("/api/cleanup")
def cleanup_job():
    """Deletes a job's mirrored site from disk and its in-memory record.
    Called via navigator.sendBeacon when the user leaves the page; also runs
    on a timed sweep (_cleanup_expired_jobs) as a fallback for clients that
    never get the chance to send this (crash, force-quit, lost connection)."""
    data = request.get_json(silent=True)
    if data is None:
        try:
            data = json.loads(request.get_data(as_text=True) or "{}")
        except ValueError:
            data = {}

    job_id = data.get("job_id")
    if not job_id:
        return jsonify(error="job_id is required"), 400

    with jobs_lock:
        job = jobs.pop(job_id, None)
    if job:
        _delete_project_dir(_job_project_name(job))

    return jsonify(status="ok")


@app.get("/api/status/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="job not found"), 404
    return jsonify(job)


@app.get("/api/github/repos")
def github_repos():
    try:
        token = _github_token()
        repos = list_github_repos(token)
    except RuntimeError as e:
        return jsonify(error=str(e)), 502
    return jsonify(repos=repos)


def _run_push_job(job_id, project_name, repo_full_name, default_branch, source_url):
    with push_jobs_lock:
        push_jobs[job_id]["status"] = "running"

    def log(stage):
        with push_jobs_lock:
            push_jobs[job_id]["stage"] = stage

    try:
        token = _github_token()
        project_dir = os.path.join(SITES_DIR, project_name)
        repo_url = push_project_to_github(project_dir, repo_full_name, default_branch, source_url, token, log=log)
        with push_jobs_lock:
            push_jobs[job_id]["status"] = "done"
            push_jobs[job_id]["repo_url"] = repo_url
            push_jobs[job_id]["finished_at"] = time.time()
    except Exception as e:
        with push_jobs_lock:
            push_jobs[job_id]["status"] = "failed"
            push_jobs[job_id]["error"] = str(e)
            push_jobs[job_id]["finished_at"] = time.time()


@app.post("/api/push-to-github")
def push_to_github_endpoint():
    data = request.get_json(silent=True) or {}
    project_name = data.get("project_name")
    repo_full_name = data.get("repo")
    default_branch = data.get("default_branch") or "main"
    source_url = data.get("url") or ""

    if not project_name or not PROJECT_NAME_RE.match(project_name):
        return jsonify(error="a valid project_name is required"), 400
    if not repo_full_name or not REPO_FULL_NAME_RE.match(repo_full_name):
        return jsonify(error="a valid repo ('owner/name') is required"), 400

    project_dir = os.path.join(SITES_DIR, project_name)
    if not os.path.isdir(project_dir):
        return jsonify(error=f"project '{project_name}' not found"), 404

    job_id = uuid.uuid4().hex
    with push_jobs_lock:
        push_jobs[job_id] = {
            "status": "pending",
            "stage": "queued",
            "project_name": project_name,
            "repo": repo_full_name,
        }

    thread = threading.Thread(
        target=_run_push_job,
        args=(job_id, project_name, repo_full_name, default_branch, source_url),
        daemon=True,
    )
    thread.start()

    return jsonify(job_id=job_id, status="started"), 202


@app.get("/api/push-status/<job_id>")
def push_status(job_id):
    with push_jobs_lock:
        job = push_jobs.get(job_id)
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


threading.Thread(target=_cleanup_expired_jobs, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
