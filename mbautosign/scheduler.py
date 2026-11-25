from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

import psutil
from filelock import FileLock, Timeout

from .runlog import RunLog
from .settings import LOG_DIR, ROOT, Settings


RUN_ID_ENV = "MBAUTOSIGN_JOB_ID"
RUN_DAY_ENV = "MBAUTOSIGN_JOB_DAY"
# The child opts in only on a known transient failure before any POST could have occurred.
RETRY_EXIT = 75
POLL_SECONDS = 15


def claim_day(directory: Path, day: str) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        # The marker itself is sufficient; never remove it after an ambiguous failure.
        with (directory / f"{day}.lock").open("xb"):
            pass
    except FileExistsError:
        return False
    return True


def completion_file(directory: Path, day: str) -> Path:
    # Older .lock files marked dispatch, not confirmed completion.
    return directory / f"{day}.done"


def mark_completed(directory: Path, day: str) -> None:
    marker = completion_file(directory, day)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("xb"):
            pass
    except FileExistsError:
        if not marker.is_file():
            raise


def stop_worker(worker: subprocess.Popen, log: RunLog, job_id: str) -> None:
    descendants = []
    cleanup_failed = False
    try:
        # Chromium/Playwright may start a new process group, so also track descendants.
        descendants = psutil.Process(worker.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        pass
    except psutil.Error as exc:
        cleanup_failed = True
        log.exception("worker_tree_inspection_failed", exc, job_id=job_id)

    if os.name != "nt":
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            cleanup_failed = True
            log.exception("worker_group_stop_failed", exc, job_id=job_id)
    try:
        worker.kill()
    except ProcessLookupError:
        pass
    except OSError as exc:
        cleanup_failed = True
        log.exception("worker_stop_failed", exc, job_id=job_id)

    for child in reversed(descendants):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            cleanup_failed = True
            log.exception("worker_descendant_stop_failed", exc, job_id=job_id)
    try:
        worker.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        cleanup_failed = True
        log.exception("worker_reap_failed", exc, job_id=job_id)
    try:
        _, remaining = psutil.wait_procs(descendants, timeout=1)
        active = sum(child.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD} for child in remaining if child.is_running())
    except psutil.NoSuchProcess:
        active = 0
    except psutil.Error as exc:
        active = 0
        cleanup_failed = True
        log.exception("worker_descendant_verify_failed", exc, job_id=job_id)
    cleanup_failed |= active > 0
    log.event("worker_cleanup", level="warning" if cleanup_failed else "info", job_id=job_id,
              descendants=len(descendants), still_active=active, cleanup_failed=cleanup_failed)


def run_worker(config: Settings, log: RunLog, stopping: Event, day: str, *,
               command: list[str] | None = None) -> int | None:
    job_id = uuid4().hex
    env = {**os.environ, RUN_ID_ENV: job_id, RUN_DAY_ENV: day, "PYTHONUNBUFFERED": "1"}
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    if command is None:
        command = [sys.executable] if getattr(sys, "frozen", False) else [
            sys.executable, "-X", "utf8", str(ROOT / "signin.py"),
        ]
    log.event("worker_starting", job_id=job_id, day=day, timeout_seconds=config.job_timeout_seconds)
    started = monotonic()
    worker = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, env=env, **flags)
    try:
        deadline = started + config.job_timeout_seconds
        while True:
            if (code := worker.poll()) is not None:
                log.event("worker_finished", level="info" if code == 0 else "warning", job_id=job_id, day=day,
                          exit_code=code, retryable=code == RETRY_EXIT,
                          job_elapsed_ms=int((monotonic() - started) * 1000))
                return code
            remaining = deadline - monotonic()
            if remaining <= 0 or stopping.is_set():
                log.event("worker_cancelled" if stopping.is_set() else "worker_timeout", level="warning",
                          job_id=job_id, day=day, job_elapsed_ms=int((monotonic() - started) * 1000))
                return
            stopping.wait(min(remaining, 0.25))
    finally:
        if worker.poll() is None:
            stop_worker(worker, log, job_id)


def daily_loop(config: Settings, log: RunLog, stopping: Event, *, now=None, worker=None,
               elapsed=monotonic, completions: Path = LOG_DIR / "schedule") -> None:
    now = now or (lambda: datetime.now(config.timezone))
    worker = worker or run_worker
    last_day = None
    pending: tuple[str, float, int] | None = None
    while not stopping.is_set():
        try:
            current = now()
            day = current.date().isoformat()
            attempt = None
            if pending is not None:
                retry_day, retry_at, next_attempt = pending
                if retry_day != day:
                    log.event("schedule_retry_expired", day=retry_day, attempt=next_attempt)
                    pending = None
                elif elapsed() >= retry_at:
                    pending = None
                    if completion_file(completions, day).is_file():
                        log.event("schedule_day_already_completed", day=day)
                    elif (completions.with_name("attempts") / f"{day}.lock").exists():
                        log.event("schedule_retry_skipped", day=day, reason="checkin_already_reserved")
                    else:
                        attempt = next_attempt
            if day != last_day and current.time() >= config.scheduled_time:
                last_day = day
                if completion_file(completions, day).is_file():
                    log.event("schedule_day_already_completed", day=day)
                else:
                    log.event("schedule_due", day=day)
                    attempt = 1
            if attempt is not None and not stopping.is_set():
                if attempt > 1:
                    log.event("schedule_retry_started", day=day, attempt=attempt)
                code = worker(config, log, stopping, day)
                if code == RETRY_EXIT and not stopping.is_set():
                    if attempt <= config.retry_count:
                        pending = (day, elapsed() + config.retry_delay_seconds, attempt + 1)
                        log.event("schedule_retry_pending", level="warning", day=day, attempt=attempt + 1,
                                  delay_seconds=config.retry_delay_seconds)
                    else:
                        log.event("schedule_retry_exhausted", level="warning", day=day, attempts=attempt)
        except Exception as exc:
            log.exception("schedule_iteration_failed", exc)
        stopping.wait(POLL_SECONDS)


def watch(config: Settings) -> int:
    log = RunLog("watch", config.timezone, debug=config.debug)
    if not config.enabled:
        log.event("schedule_disabled")
        return 0
    stopping = Event()
    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {sig: signal.signal(sig, lambda *_: stopping.set()) for sig in signals}
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with FileLock(LOG_DIR / "watch.lock", timeout=0):
            log.event("schedule_started", local_time=config.scheduled_time.strftime("%H:%M"),
                      timezone=config.timezone.key, timeout_seconds=config.job_timeout_seconds,
                      retry_count=config.retry_count, retry_delay_seconds=config.retry_delay_seconds)
            try:
                daily_loop(config, log, stopping)
            finally:
                log.event("schedule_stopped")
    except Timeout:
        log.event("schedule_already_running", level="warning")
        return 1
    except Exception as exc:
        log.exception("schedule_start_failed", exc)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
