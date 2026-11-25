from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from filelock import FileLock, Timeout

from .settings import LOG_DIR


LOG_PATH = LOG_DIR / "checkins.jsonl"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 5


def error_fields(exc: Exception) -> dict:
    # Exception messages, source text and local variables can contain secrets.
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        frames.append(f"{Path(code.co_filename).name}:{trace.tb_lineno}:{code.co_name}")
        trace = trace.tb_next
    result = {"error_type": type(exc).__name__, "frames": frames[-8:]}
    for key in ("errno", "winerror", "code"):
        value = getattr(exc, key, None)
        if type(value) is int:
            result[key] = value
    return result


def append_log(path: Path, line: str) -> None:
    data = (line + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path.with_name(path.name + ".lock"), timeout=1):
        if path.exists() and path.stat().st_size + len(data) > LOG_MAX_BYTES:
            for slot in range(LOG_BACKUPS, 0, -1):
                previous = path if slot == 1 else path.with_name(f"{path.name}.{slot - 1}")
                if previous.exists():
                    previous.replace(path.with_name(f"{path.name}.{slot}"))
        with path.open("ab") as stream:
            stream.write(data)


def emit_console(line: str, *, stderr: bool = False) -> None:
    try:
        print(line, file=sys.stderr if stderr else sys.stdout, flush=True)
    except (OSError, UnicodeError, ValueError):
        # Closing a pipe or losing the console should not stop the service.
        pass


class RunLog:
    def __init__(self, mode: str, tz=timezone.utc, *, run_id: str | None = None, path: Path = LOG_PATH):
        self.mode = mode
        self.tz = tz
        self.run_id = run_id or uuid4().hex
        self.path = path
        self.started = monotonic()
        self.sink_failed = False

    def event(self, event: str, *, level: str = "info", **fields: object) -> None:
        row = {"time": datetime.now(self.tz).isoformat(timespec="milliseconds"), "run": self.mode,
               "run_id": self.run_id, "pid": os.getpid(), "event": event, "level": level,
               "elapsed_ms": int((monotonic() - self.started) * 1000), **fields}
        line = json.dumps(row, ensure_ascii=False)
        try:
            append_log(self.path, line)
        except (OSError, Timeout) as exc:
            if not self.sink_failed:
                emit_console(json.dumps({**row, "event": "log_write_failed", "level": "error",
                                         "error_type": type(exc).__name__}), stderr=True)
            self.sink_failed = True
        else:
            self.sink_failed = False
        emit_console(line)

    def exception(self, event: str, exc: Exception, **fields: object) -> None:
        self.event(event, level="error", **error_fields(exc), **fields)
