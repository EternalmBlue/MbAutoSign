from __future__ import annotations

import argparse
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin

from filelock import FileLock, Timeout as LockTimeout
from playwright.sync_api import CDPSession, Error as BrowserError, Page, Route, TimeoutError as BrowserTimeout, sync_playwright

from .cloak_runtime import CloakError, resolve_cloak
from .minebbs_login import HOME, restore_login, session_state
from .runlog import RunLog
from .scheduler import RUN_ID_ENV, claim_day, watch
from .settings import LOG_DIR, ConfigError, Settings, load_settings


CLOCK = urljoin(HOME, "/credits/clock")
FORM = ".mjc-signin-status form.mjc-signin-form"
OK = {"ready", "signed", "already_signed", "attempt_already_reserved", "logged_in", "already_logged_in"}


@dataclass
class CheckFlow:
    log: RunLog
    phase: str = "start"
    armed: bool = False
    reserved: bool = False
    posts: int = 0

    def stage(self, phase: str, **fields: object) -> None:
        self.phase = phase
        self.log.event("phase_started", phase=phase, **fields)

    def route(self, route: Route) -> None:
        request = route.request
        if request.method == "GET" and request.url == HOME and request.is_navigation_request():
            route.continue_()
        elif (self.armed and self.posts == 0 and request.resource_type == "fetch"
              and request.method == "POST" and request.url == CLOCK):
            self.posts += 1
            self.log.event("checkin_request_allowed", post_count=self.posts)
            route.continue_()
        else:
            route.abort()


def page_state(page: Page) -> str:
    return page.evaluate("""({home, clock, selector}) => {
        if (location.href !== home) return 'unexpected_page';
        if (document.querySelector('#slg-box')) return 'security_check';
        const boxes = document.querySelectorAll('.mjc-signin-status');
        if (boxes.length > 1) return 'ambiguous_signin_form';
        const status = boxes[0]?.textContent ?? '';
        const forms = document.querySelectorAll(selector);
        if (!forms.length) return status.includes('今日签到已完成') ? 'already_signed' : 'signin_form_missing';
        if (forms.length !== 1 || status.includes('今日签到已完成')) return 'ambiguous_signin_form';
        const form = forms[0];
        if (form.action !== clock || form.method !== 'post' ||
            form.enctype !== 'application/x-www-form-urlencoded' || !status.includes('今日尚未签到')) {
            return 'unexpected_form';
        }
        const fields = [...form.elements].filter(x => x.type === 'hidden' && x.name && !x.matches(':disabled'));
        const tokens = fields.filter(x => x.name === '_xfToken');
        const currencies = fields.filter(x => x.name === 'currency_ids[]');
        return tokens.length === 1 && tokens[0].value && currencies.length === 2 && currencies.every(x => x.value)
            ? 'ready' : 'missing_fields';
    }""", {"home": HOME, "clock": CLOCK, "selector": FORM})


def inspect_home(page: Page, flow: CheckFlow, phase: str = "check_home") -> dict:
    flow.stage(phase)
    response = page.goto(HOME, wait_until="domcontentloaded")
    status = response.status if response else None
    session = "unknown"
    if status == 468:
        state = "security_check"
    else:
        state = page_state(page)
        if status != 200 and state not in {"security_check", "unexpected_page"}:
            state = "homepage_failed"
        elif status == 200 and state not in {"security_check", "unexpected_page"}:
            session = session_state(page)
            if session == "signed_out":
                state = "login_required"
    details = {"home_status": status, "session_state": session, "post_count": flow.posts, "result": state}
    flow.log.event("homepage_checked", phase=phase, **details)
    return details


def submit_checkin(page: Page, cdp: CDPSession, config: Settings, flow: CheckFlow, result: dict) -> dict:
    if result["session_state"] != "signed_in":
        return {**result, "result": "session_not_confirmed"}
    flow.stage("reserve_checkin")
    day = datetime.now(config.timezone).date().isoformat()
    if not claim_day(LOG_DIR / "attempts", day):
        return {**result, "result": "attempt_already_reserved"}
    flow.reserved = True
    flow.stage("submit_checkin", day=day)
    post_id = None
    post = None

    def request_sent(event: dict) -> None:
        nonlocal post_id, post
        request, redirect = event["request"], event.get("redirectResponse")
        if post_id is None and request["method"] == "POST" and request["url"] == CLOCK:
            post_id = event["requestId"]
        if post_id == event["requestId"] and redirect and redirect["url"] == CLOCK:
            post = redirect

    def response_received(event: dict) -> None:
        nonlocal post
        if post_id == event["requestId"] and event["response"]["url"] == CLOCK:
            post = event["response"]

    # Chromium exposes a manual fetch redirect here even when JS only sees an opaque response.
    cdp.on("Network.requestWillBeSent", request_sent)
    cdp.on("Network.responseReceived", response_received)
    try:
        flow.armed = True
        fetch_status = page.locator(FORM).evaluate("""async (form, {clock, timeout}) => {
            if (form.action !== clock || form.method !== 'post') throw new Error('The form changed');
            const data = new URLSearchParams();
            const inputs = [...form.elements].filter(x => x.type === 'hidden' && !x.matches(':disabled'));
            const tokens = inputs.filter(x => x.name === '_xfToken');
            const currencies = inputs.filter(x => x.name === 'currency_ids[]');
            if (tokens.length !== 1 || !tokens[0].value || currencies.length !== 2 || !currencies.every(x => x.value)) {
                throw new Error('The fields changed');
            }
            for (const input of [...tokens, ...currencies]) data.append(input.name, input.value);
            const response = await fetch(clock, {
                method: 'POST', credentials: 'same-origin', redirect: 'manual', body: data,
                signal: AbortSignal.timeout(timeout),
            });
            return response.status;
        }""", {"clock": CLOCK, "timeout": config.timeout_ms})
        flow.armed = False
        deadline = time.monotonic() + min(2, config.timeout_ms / 1000)
        while post is None and time.monotonic() < deadline:
            page.wait_for_timeout(25)
    except Exception as exc:
        flow.log.exception("checkin_request_failed", exc, post_count=flow.posts, reserved=True)
        return {**result, "post_count": flow.posts, "result": "submission_outcome_unknown"}
    finally:
        flow.armed = False
        cdp.remove_listener("Network.requestWillBeSent", request_sent)
        cdp.remove_listener("Network.responseReceived", response_received)

    result = {**result, "post_count": flow.posts, "fetch_status": fetch_status}
    if post is None:
        return {**result, "result": "submission_outcome_unknown"}
    location = next((value for key, value in post["headers"].items() if key.lower() == "location"), None)
    try:
        home_redirect = isinstance(location, str) and urljoin(CLOCK, location) == HOME
    except ValueError:
        home_redirect = False
    result.update(post_status=post["status"], redirect_to_home=home_redirect)
    flow.log.event("checkin_response", post_count=flow.posts, post_status=post["status"], redirect_to_home=home_redirect)
    if post["status"] != 303 or not home_redirect:
        return {**result, "result": "unexpected_post_response"}

    try:
        verified = inspect_home(page, flow, "verify_checkin")
    except Exception as exc:
        flow.log.exception("checkin_verification_failed", exc, post_count=flow.posts)
        return {**result, "result": "confirmation_missing"}
    return {**result, "verify_status": verified["home_status"],
            "result": "signed" if verified["result"] == "already_signed" else "confirmation_missing"}


@contextmanager
def browser_session(config: Settings, flow: CheckFlow):
    flow.stage("resolve_cloak")
    executable = resolve_cloak(config, flow.log)
    flow.stage("lock_profile")
    initialized = (config.profile / "Local State").is_file()
    config.profile.mkdir(parents=True, exist_ok=True)
    with FileLock(config.profile / ".mbautosign.lock", timeout=0), managed_playwright(flow) as playwright:
        flow.stage("launch_browser", headless=config.headless, profile_initialized=initialized)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=config.profile, executable_path=executable, headless=config.headless,
            service_workers="block", timeout=config.timeout_ms,
        )
        flow.log.event("browser_opened", headless=config.headless)
        try:
            yield context
        finally:
            try:
                context.close()
                flow.log.event("browser_closed")
            except Exception as exc:
                flow.log.exception("browser_cleanup_failed", exc)


@contextmanager
def managed_playwright(flow: CheckFlow):
    flow.stage("start_playwright")
    with sync_playwright() as playwright:
        yield playwright


def failure(flow: CheckFlow, exc: Exception) -> dict:
    if flow.reserved:
        outcome = "submission_outcome_unknown"
    elif flow.phase == "lock_profile" and isinstance(exc, LockTimeout):
        outcome = "profile_in_use"
    elif flow.phase == "launch_browser" and isinstance(exc, BrowserError) and any(
        text in str(exc).lower() for text in ("processsingleton", "profile in use", "user data directory is already in use")
    ):
        outcome = "profile_in_use"
    elif flow.phase == "resolve_cloak":
        outcome = "cloak_unavailable"
    elif flow.phase == "start_playwright":
        outcome = "playwright_driver_failed"
    elif isinstance(exc, (BrowserTimeout, TimeoutError, LockTimeout)):
        outcome = "operation_timed_out"
    else:
        outcome = "run_failed"
    details = {"phase": flow.phase, "result": outcome, "post_count": flow.posts, "attempt_reserved": flow.reserved}
    if isinstance(exc, CloakError):
        details["cloak_error"] = str(exc)  # All CloakError messages are fixed local diagnostics.
    if flow.phase == "launch_browser" and isinstance(exc, BrowserError):
        if library := re.search(r"shared libraries: (lib[A-Za-z0-9_.+-]{1,80})", str(exc)):
            details["missing_library"] = library[1]
    flow.log.exception("phase_failed", exc, **details)
    return details


def emit_result(flow: CheckFlow, result: dict) -> int:
    ok = result["result"] in OK
    flow.log.event("run_finished", level="info" if ok else "warning", **result)
    return 0 if ok else 1


def run_once(config: Settings, submit: bool, login_only: bool = False, *, log: RunLog | None = None) -> int:
    flow = CheckFlow(log or RunLog("login-only" if login_only else "automatic" if submit else "read-only", config.timezone))
    flow.log.event("run_started", submit=submit, login_only=login_only)
    result = None
    login_details = {}
    try:
        with browser_session(config, flow) as context:
            flow.stage("configure_lightweight_page")
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(config.timeout_ms)
            page.set_default_navigation_timeout(config.timeout_ms)
            page.route("**/*", flow.route)
            cdp = context.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.send("Network.setBypassServiceWorker", {"bypass": True})
            cdp.send("Emulation.setScriptExecutionDisabled", {"value": True})
            home = inspect_home(page, flow)
            security_wait_requested = home["result"] == "security_check" and config.challenge_wait_seconds > 0
            if (submit or login_only) and (home["result"] == "login_required" or security_wait_requested):
                flow.stage("restore_login", reason=home["result"])
                if home["result"] == "login_required" and not (config.username and config.password):
                    login_details = {"login_result": "credentials_missing", "login_post_count": 0}
                else:
                    login_details = restore_login(context, config.username, config.password, config.timeout_ms,
                                                  challenge_wait_ms=config.challenge_wait_seconds * 1000, log=flow.log)
                flow.log.event("login_completed", **login_details)
                if login_details["login_result"] in {"submitted", "already_logged_in"}:
                    home = inspect_home(page, flow, "verify_login")
                    if home["session_state"] == "signed_in":
                        login_details["login_result"] = "logged_in"
                        if login_only:
                            home["result"] = "logged_in"
                    else:
                        status = "verification_required" if home["result"] == "security_check" else "login_not_confirmed"
                        login_details["login_result"] = home["result"] = status
                else:
                    home["result"] = login_details["login_result"]
            elif login_only and home["session_state"] == "signed_in":
                home["result"] = "already_logged_in"
            result = submit_checkin(page, cdp, config, flow, home) if submit and home["result"] == "ready" else home
    except Exception as exc:
        if result is None:
            result = failure(flow, exc)
        else:
            flow.log.exception("browser_shutdown_failed", exc)
    return emit_result(flow, {**login_details, **result})


def main() -> int:
    parser = argparse.ArgumentParser(description="MineBBS 签到，默认只检查。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只读取首页，不提交（默认）。")
    mode.add_argument("--login", action="store_true", help="检查并在需要时重新登录，不提交签到。")
    mode.add_argument("--submit", action="store_true", help="签到一次；已签到时跳过。")
    mode.add_argument("--watch", action="store_true", help="按 config.toml 每天运行；需要进程保持运行。")
    options = parser.parse_args()

    mode_name = ("watch" if options.watch else "login-only" if options.login else
                 "automatic" if options.submit else "read-only")
    run_id = os.environ.get(RUN_ID_ENV, "")
    log = RunLog(mode_name, run_id=run_id if options.submit and re.fullmatch(r"[0-9a-f]{32}", run_id) else None)
    try:
        config = load_settings()
        log.tz = config.timezone
        if options.watch:
            return watch(config)
        if options.submit and re.fullmatch(r"[0-9a-f]{32}", run_id) and not config.enabled:
            log.event("scheduled_job_disabled", result="skipped")
            return 0
        return run_once(config, options.submit, options.login, log=log)
    except KeyboardInterrupt:
        log.event("run_cancelled")
        return 130
    except ConfigError as exc:
        log.exception("configuration_invalid", exc, setting_error=str(exc))
        return 2
    except FileNotFoundError as exc:
        log.exception("configuration_file_missing", exc)
        return 2
    except Exception as exc:
        log.exception("command_failed", exc)
        return 1
