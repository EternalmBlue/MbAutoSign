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


LOG_PATH = LOG_DIR / "checkins.log"
DEBUG_LOG_PATH = LOG_DIR / "checkins.jsonl"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 5

RESULT_MESSAGES = {
    "signed": "签到成功，已在 MineBBS 首页确认。",
    "already_signed": "今天已签到，无需重复提交。",
    "scheduled_day_expired": "已跨日，跳过上一日的签到任务。",
    "attempt_already_reserved": "本轮未确认签到。为避免重复提交，请到 MineBBS 核对今天的签到状态。",
    "submission_outcome_unknown": "本轮无法确认签到结果，请到 MineBBS 核对。",
    "confirmation_missing": "论坛尚未确认签到结果，请到 MineBBS 核对。",
    "unexpected_post_response": "签到提交后的响应异常，请到 MineBBS 核对结果。",
    "cloak_unavailable": "签到未完成：无法准备 Cloak Browser，请检查网络或开启调试日志。",
    "browser_launch_timeout": "签到未完成：Cloak Browser 启动超时。",
    "playwright_driver_failed": "签到未完成：无法启动浏览器驱动，请开启调试日志。",
    "profile_in_use": "签到未完成：浏览器配置正被占用，请关闭占用它的浏览器窗口。",
    "homepage_failed": "签到未完成：MineBBS 首页暂不可用。",
    "homepage_unreachable": "签到未完成：无法连接 MineBBS，请检查网络。",
    "operation_timed_out": "签到未完成：页面或浏览器操作超时。",
    "security_check": "签到未完成：论坛的安全检查尚未结束。",
    "verification_required": "签到未完成：论坛需要人工验证，请打开网页处理。",
    "credentials_missing": "签到未完成：尚未登录，请在 config.toml 的 [login] 中填写账号和密码。",
    "login_rejected": "签到未完成：论坛拒绝了登录，请在网页检查账号和密码。",
    "login_not_confirmed": "签到未完成：论坛未确认登录，请检查账户状态。",
    "login_outcome_unknown": "签到未完成：登录结果不明，请在 MineBBS 核对账户状态。",
    "login_ui_not_ready": "签到未完成：论坛的登录界面未能及时加载。",
    "login_failed": "签到未完成：登录过程出错，请在网页检查账户状态。",
    "session_not_confirmed": "签到未完成：无法确认当前账户的登录状态。",
    "signin_form_missing": "签到未完成：没有找到签到入口，请检查 MineBBS 首页。",
    "missing_fields": "签到未完成：论坛的签到表单不完整。",
}

USER_MESSAGES = {
    "run_started": "开始检查签到。",
    "configuration_file_missing": "找不到 config.toml，请在程序目录创建并填写后重试。",
    "command_failed": "程序运行出错，请开启调试日志查看详情。",
    "run_cancelled": "签到任务已取消。",
    "schedule_stopped": "定时签到已停止。",
    "schedule_disabled": "定时签到已关闭。",
    "scheduled_job_disabled": "定时签到已关闭，本次任务已跳过。",
    "scheduled_day_expired": RESULT_MESSAGES["scheduled_day_expired"],
    "schedule_already_running": "当前目录已有定时签到进程在运行。",
    "schedule_start_failed": "无法启动定时签到，请开启调试日志查看详情。",
    "schedule_iteration_failed": "定时任务出错，程序仍在运行，请开启调试日志查看详情。",
    "schedule_retry_exhausted": "已用完本轮的重试次数，今天尚未确认签到。",
    "schedule_retry_skipped": "之前的提交结果未确认，已取消重试。请到 MineBBS 核对。",
    "schedule_completion_write_failed": "签到结果已确认，但无法保存完成记录，请检查日志目录的写入权限。",
    "worker_timeout": "签到任务超过总时限，已停止。请到 MineBBS 核对今天是否签到。",
    "worker_cancelled": "正在运行的签到任务已取消。",
    "cloak_download_started": "本机没有可用的 Cloak Browser，正在下载安装。",
    "security_check_wait_started": "MineBBS 正在进行安全检查，等待页面返回。",
}


def user_message(event: str, fields: dict) -> str | None:
    if event == "run_finished":
        result = fields.get("result")
        message = RESULT_MESSAGES.get(result)
        if message is None:
            if isinstance(result, str) and "login" in result:
                return "签到未完成：无法识别论坛登录页面，请在网页检查。"
            return "签到未完成：无法确认页面状态，请到 MineBBS 检查或开启调试日志。"
        if result == "homepage_failed":
            status = fields.get("login_home_status") or fields.get("home_status")
            if type(status) is int:
                message = f"签到未完成：MineBBS 首页暂不可用（HTTP {status}）。"
        return message
    if event == "schedule_started":
        return f"定时签到已启动：每天 {fields['local_time']}（{fields['timezone']}）。"
    if event == "schedule_day_already_completed":
        return f"{fields['day']} 的签到已确认，跳过定时任务。"
    if event == "schedule_retry_pending":
        return f"将在 {fields['delay_seconds']} 秒后重试（第 {fields['attempt']} 次尝试）。"
    if event == "configuration_invalid":
        return f"配置无效：{fields['setting_error']}"
    if event == "arguments_not_supported":
        return fields["action"]
    if event == "login_phase" and fields.get("phase") == "open_login_overlay":
        return "尚未登录，正在尝试登录 MineBBS。"
    if event == "worker_finished" and fields.get("exit_code") not in {0, 1, 2, 75, 130}:
        return f"签到进程异常退出（退出码 {fields['exit_code']}），请开启调试日志。"
    return USER_MESSAGES.get(event)


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
    def __init__(self, mode: str, tz=timezone.utc, *, run_id: str | None = None,
                 path: Path | None = None, debug: bool = False):
        self.mode = mode
        self.tz = tz
        self.run_id = run_id or uuid4().hex
        self.debug = debug
        self._path = path
        self.started = monotonic()
        self.sink_failed = False

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else DEBUG_LOG_PATH if self.debug else LOG_PATH

    def event(self, event: str, *, level: str = "info", **fields: object) -> None:
        now = datetime.now(self.tz)
        if self.debug:
            row = {"time": now.isoformat(timespec="milliseconds"), "run": self.mode,
                   "run_id": self.run_id, "pid": os.getpid(), "event": event, "level": level,
                   "elapsed_ms": int((monotonic() - self.started) * 1000), **fields}
            line = json.dumps(row, ensure_ascii=False)
        else:
            message = user_message(event, fields)
            if message is None:
                return
            line = f"{now.isoformat(sep=' ', timespec='seconds')} {message}"
        try:
            append_log(self.path, line)
        except (OSError, Timeout) as exc:
            if not self.sink_failed:
                warning = (json.dumps({**row, "event": "log_write_failed", "level": "error",
                                       "error_type": type(exc).__name__}) if self.debug
                           else "无法写入日志文件，请检查 logs 目录的写入权限。")
                emit_console(warning, stderr=True)
            self.sink_failed = True
        else:
            self.sink_failed = False
        emit_console(line)

    def exception(self, event: str, exc: Exception, **fields: object) -> None:
        diagnostics = error_fields(exc) if self.debug else {}
        self.event(event, level="error", **diagnostics, **fields)
