from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    profile: Path
    executable: Path | None
    headless: bool
    timeout_ms: int
    enabled: bool
    scheduled_time: time
    timezone: ZoneInfo
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    challenge_wait_seconds: int = 10
    job_timeout_seconds: int = 600


def load_settings(path: Path = ROOT / "config.toml") -> Settings:
    try:
        with path.open("rb") as source:
            config = tomllib.load(source)
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise ConfigError("config.toml 格式无效，请检查 TOML 语法和 UTF-8 编码。") from exc

    def table(name: str, *, optional: bool = False) -> dict:
        value = config.get(name, {} if optional else None)
        if not isinstance(value, dict):
            raise ConfigError(f"配置需要 [{name}] 段。")
        return value

    schedule, browser, login = table("schedule"), table("browser"), table("login", optional=True)
    keys = {id(schedule): "schedule", id(browser): "browser", id(login): "login"}

    def get(section: dict, name: str, kind: type, default: object = None):
        value = section.get(name, default)
        if type(value) is not kind:
            raise ConfigError(f"{keys[id(section)]}.{name} 必须为 {kind.__name__}。")
        return value

    def bounded(section: dict, name: str, default: int, low: int, high: int) -> int:
        value = get(section, name, int, default)
        if not low <= value <= high:
            raise ConfigError(f"{keys[id(section)]}.{name} 必须在 {low} 到 {high} 之间。")
        return value

    launch_at = get(schedule, "time", str)
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", launch_at):
        raise ConfigError("schedule.time 必须使用 HH:MM 格式。")
    try:
        timezone = ZoneInfo(get(schedule, "timezone", str))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("schedule.timezone 必须是有效的 IANA 时区，例如 Asia/Shanghai。") from exc

    profile_name = get(browser, "profile", str).strip()
    if not profile_name or "\x00" in profile_name:
        raise ConfigError("browser.profile 必须指向专用浏览器目录。")
    profile = (path.parent / Path(profile_name).expanduser()).resolve()
    if profile in {profile.parent, path.parent.resolve(), Path.home().resolve()}:
        raise ConfigError("browser.profile 不能是根目录、项目目录或用户主目录。")
    executable = get(browser, "executable", str, "").strip()
    if "\x00" in executable:
        raise ConfigError("browser.executable 路径无效。")
    return Settings(
        profile=profile,
        executable=(path.parent / Path(executable).expanduser()) if executable else None,
        headless=get(browser, "headless", bool),
        timeout_ms=bounded(browser, "timeout_ms", 20000, 100, 180000),
        enabled=get(schedule, "enabled", bool),
        scheduled_time=time.fromisoformat(launch_at),
        timezone=timezone,
        username=get(login, "username", str, "").strip(),
        password=get(login, "password", str, ""),
        challenge_wait_seconds=bounded(browser, "challenge_wait_seconds", 10, 0, 120),
        job_timeout_seconds=bounded(schedule, "job_timeout_seconds", 600, 1, 7200),
    )
