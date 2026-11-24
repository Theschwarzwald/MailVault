"""Background jobs (exports, deep scans) with progress and cancel."""
from __future__ import annotations

import os
import secrets
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

JOB_DIR = os.path.join(os.environ.get("MAILVAULT_UPLOADS") or os.path.join(tempfile.gettempdir(), "mailvault"), "jobs")
os.makedirs(JOB_DIR, exist_ok=True)
POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("MAILVAULT_JOBS", "2")))
JOBS: dict[str, "Job"] = {}
KEEP_SECONDS = 6 * 3600


class Job:
    def __init__(self, user: str, title: str, total: int):
        self.id = secrets.token_hex(8)
        self.user, self.title, self.total = user, title, max(total, 1)
        self.done = 0
        self.state = "queued"         # queued | running | done | error | cancelled
        self.error = ""
        self.message = ""
        self.result = ""
        self.result_name = ""
        self.cancelled = False
        self.created = time.time()

    def progress(self, done: int, total: int | None = None):
        self.done = done
        if total:
            self.total = max(total, 1)

    def cancel(self):
        self.cancelled = True
        if self.state == "queued":
            self.state = "cancelled"

    def temp_path(self, suffix: str) -> str:
        return os.path.join(JOB_DIR, self.id + suffix)

    def write(self, name: str, data: bytes):
        path = self.temp_path(os.path.splitext(name)[1])
        with open(path, "wb") as fh:
            fh.write(data)
        self.finish_file(path, name)

    def finish_file(self, path: str, name: str):
        self.result, self.result_name = path, name

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "state": self.state, "done": self.done, "total": self.total,
                "percent": int(100 * self.done / self.total) if self.total else 0, "error": self.error,
                "message": self.message, "file": self.result_name, "has_file": bool(self.result)}


def _cleanup():
    now = time.time()
    for jid, j in list(JOBS.items()):
        if now - j.created > KEEP_SECONDS and j.state not in ("running", "queued"):
            if j.result and os.path.exists(j.result):
                try:
                    os.remove(j.result)
                except OSError:
                    pass
            JOBS.pop(jid, None)


def start_job(user: str, title: str, total: int, work) -> Job:
    _cleanup()
    job = Job(user, title, total)
    JOBS[job.id] = job

    def run():
        if job.cancelled:
            job.state = "cancelled"
            return
        job.state = "running"
        try:
            work(job)
            job.state = "cancelled" if job.cancelled else "done"
            job.done = job.total
        except Exception as e:  # noqa: BLE001
            from ..core.export import Cancelled
            if isinstance(e, Cancelled) or job.cancelled:
                job.state = "cancelled"
            else:
                job.state, job.error = "error", f"{type(e).__name__}: {e}"
    POOL.submit(run)
    return job


__all__ = ["Job", "JOBS", "start_job", "threading"]
