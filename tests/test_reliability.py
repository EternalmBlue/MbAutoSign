from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from datetime import datetime, time
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import psutil
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout

from mbautosign import cli as signin
from mbautosign import cloak_runtime as cloak
from mbautosign import runlog, scheduler
from mbautosign.settings import ROOT, Settings, load_settings


def config(root: Path, **overrides) -> Settings:
    return Settings(**({"profile": root / "profile", "headless": True, "timeout_ms": 2000,
                       "enabled": True, "scheduled_time": time(9), "timezone": ZoneInfo("Asia/Shanghai")}
                      | overrides))


def tar_payload(name: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        member = tarfile.TarInfo(name)
        contents = b"offline fixture"
        member.mode = 0o755
        member.size = len(contents)
        tar.addfile(member, io.BytesIO(contents))
    return output.getvalue()


class DownloadResponse(io.BytesIO):
    def geturl(self):
        return "https://example.invalid/fixture"


def inline_transfer(target, args, _deadline):
    try:
        target(*args)
    except SystemExit as exc:
        return exc.code
    return 0


def stalled_then_healthy_source(source, url, asset, path, log, deadline):
    if source == "official":
        path.write_bytes(b"unfinished transfer")
        path.with_suffix(".pid").write_text(str(os.getpid()), encoding="ascii")
        sleep(60)
    with patch.object(cloak, "urlopen", return_value=DownloadResponse(b"verified fixture")):
        cloak.transfer_worker(source, url, asset, path, log, deadline)


class ReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = Mock(spec=runlog.RunLog)

    def test_entrypoint_uses_config_only_and_never_creates_missing_config(self):
        cfg = config(self.root)
        with (patch.object(sys, "argv", ["signin.py"]),
              patch.dict(os.environ, {scheduler.RUN_ID_ENV: "", scheduler.RUN_DAY_ENV: ""}),
              patch.object(signin, "RunLog", return_value=self.log),
              patch.object(signin, "run_once", return_value=0) as single,
              patch.object(signin, "watch", return_value=0) as auto,
              patch.object(signin, "load_settings",
                           side_effect=[FileNotFoundError, config(self.root, enabled=False), cfg])):
            self.assertEqual(signin.main(), 2)
            self.assertEqual(self.log.event.call_args.args[0], "configuration_file_missing")
            self.assertIn("config.toml", self.log.event.call_args.kwargs["action"])
            single.assert_not_called()
            auto.assert_not_called()
            self.assertEqual(signin.main(), 0)
            single.assert_called_once()
            auto.assert_not_called()
            self.assertEqual(signin.main(), 0)
            auto.assert_called_once_with(cfg)

    def test_old_executable_is_ignored_and_old_schedule_key_is_compatible(self):
        path = self.root / "config.toml"
        example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        path.write_text(example.replace("enable = false", "enabled = true").replace(
            "[browser]", '[browser]\nexecutable = "/untrusted/chrome"'), encoding="utf-8")
        cfg = load_settings(path)
        self.assertTrue(cfg.enabled)
        self.assertFalse(hasattr(cfg, "executable"))

    def test_scheduler_moves_to_next_day_after_exception_and_does_not_retry(self):
        cfg = config(self.root)
        clock = iter(datetime(2026, 1, day, hour, tzinfo=cfg.timezone) for day, hour in
                     [(1, 8), (1, 9), (1, 10), (2, 9)])
        stopping = Event()
        attempted = []

        def worker(_cfg, _log, stop, day):
            attempted.append(day)
            if len(attempted) == 1:
                raise RuntimeError("fixture worker failure")
            stop.set()

        with patch.object(scheduler, "POLL_SECONDS", 0):
            scheduler.daily_loop(cfg, self.log, stopping, now=lambda: next(clock), worker=worker,
                                 claims=self.root / "days")
        self.assertEqual(attempted, ["2026-01-01", "2026-01-02"])
        self.assertFalse(scheduler.claim_day(self.root / "days", "2026-01-01"))
        self.assertEqual(self.log.exception.call_args.args[0], "schedule_iteration_failed")

    def test_timed_out_process_is_reaped_and_a_later_worker_runs(self):
        cfg = config(self.root, job_timeout_seconds=2)
        marker = self.root / "child.pid"
        launch = ("import subprocess,sys,time; from pathlib import Path; "
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                  "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
        scheduler.run_worker(cfg, self.log, Event(), "2026-01-01", command=[sys.executable, "-c", launch, str(marker)])
        try:
            descendant = psutil.Process(int(marker.read_text()))
        except psutil.NoSuchProcess:
            pass
        else:
            try:
                try:
                    self.assertEqual(descendant.status(), psutil.STATUS_ZOMBIE)
                except psutil.NoSuchProcess:
                    pass
            finally:
                try:
                    descendant.kill()
                except psutil.NoSuchProcess:
                    pass
        names = [call.args[0] for call in self.log.event.call_args_list]
        self.assertIn("worker_timeout", names)
        self.assertIn("worker_cleanup", names)
        self.assertFalse(self.log.event.call_args.kwargs["cleanup_failed"])
        scheduler.run_worker(cfg, self.log, Event(), "2026-01-02", command=[sys.executable, "-c", "pass"])
        self.assertEqual(self.log.event.call_args.kwargs["exit_code"], 0)

    def test_scheduler_retries_only_explicit_transient_failures_within_limit(self):
        cfg = config(self.root, retry_count=2, retry_delay_seconds=15)
        stop = Event()
        attempts = []
        instants = iter([(1, 0), (1, 14), (1, 15), (1, 30), (1, 45), (2, 0)])
        ticks = 0

        def now():
            nonlocal ticks
            instant = next(instants, None)
            if instant is None:
                stop.set()
                return datetime(2026, 1, 2, 9, tzinfo=cfg.timezone)
            day, ticks = instant
            return datetime(2026, 1, day, 9, 0, ticks, tzinfo=cfg.timezone)

        def worker(_config, _log, stopping, day):
            attempts.append((day, ticks))
            if day == "2026-01-02":
                stopping.set()
                return 0
            return scheduler.RETRY_EXIT

        with patch.object(scheduler, "POLL_SECONDS", 0):
            scheduler.daily_loop(cfg, self.log, stop, now=now, elapsed=lambda: ticks,
                                 worker=worker, claims=self.root / "schedule")
        self.assertEqual(attempts, [("2026-01-01", 0), ("2026-01-01", 15),
                                    ("2026-01-01", 30), ("2026-01-02", 0)])
        self.assertIn("schedule_retry_exhausted", [call.args[0] for call in self.log.event.call_args_list])

    def test_scheduler_drops_pending_retry_when_day_changes_or_submission_is_reserved(self):
        cfg = config(self.root, retry_delay_seconds=15)
        for reserved in (True, False):
            with self.subTest(reserved=reserved):
                log = Mock(spec=runlog.RunLog)
                stop = Event()
                attempts = []
                logs = self.root / str(reserved)
                times = [(1, 9, 0), (1, 9, 40), (2, 9, 0)] if reserved else [(1, 23, 0), (2, 9, 0)]
                clock = iter(datetime(2026, 1, day, hour, 0, sec, tzinfo=cfg.timezone) for day, hour, sec in times)
                elapsed = iter((0, 20))

                def now():
                    current = next(clock, None)
                    if current is not None:
                        return current
                    stop.set()
                    return datetime(2026, 1, 2, 9, tzinfo=cfg.timezone)

                def worker(_cfg, _log, stopping, day):
                    attempts.append(day)
                    if day == "2026-01-02":
                        stopping.set()
                        return 0
                    if reserved:
                        scheduler.claim_day(logs / "attempts", day)
                    return scheduler.RETRY_EXIT

                with patch.object(scheduler, "POLL_SECONDS", 0):
                    scheduler.daily_loop(cfg, log, stop, now=now, elapsed=lambda: next(elapsed),
                                         worker=worker, claims=logs / "schedule")
                self.assertEqual(attempts, ["2026-01-01", "2026-01-02"])
                event = "schedule_retry_skipped" if reserved else "schedule_retry_expired"
                self.assertIn(event, [call.args[0] for call in log.event.call_args_list])

    def test_truncated_download_uses_verified_fallback_and_then_reuses_cache(self):
        payload = tar_payload("chrome")
        asset = cloak.Asset("146.0.7680.177.5", "fixture.tar.gz", len(payload), hashlib.sha256(payload).hexdigest())
        cache = self.root / ".cloak-bin"
        incomplete = cache / "linux-x64" / f"chromium-{asset.version}"
        incomplete.mkdir(parents=True)
        (incomplete / "previous-file").touch()
        other_browser = self.root / "local-install" / "chromium-999.0.0.0" / "chrome"
        other_browser.parent.mkdir(parents=True)
        other_browser.write_bytes(b"not cloak")
        other_browser.chmod(0o755)

        with (patch.object(cloak, "platform_key", return_value=("Linux", "x64")),
              patch.dict(cloak.ASSETS, {("Linux", "x64"): asset}),
              patch.object(cloak, "bounded_transfer", side_effect=inline_transfer),
              patch.object(cloak, "urlopen", side_effect=[DownloadResponse(payload[:-1]),
                                                        DownloadResponse(payload)]) as fetch):
            first = cloak.resolve_cloak(self.log, cache=cache, home_cache=other_browser.parent.parent)
            second = cloak.resolve_cloak(self.log, cache=cache, home_cache=other_browser.parent.parent)
            self.assertEqual(first, second)
            self.assertEqual(first, (incomplete / "chrome").resolve())
            self.assertEqual(fetch.call_count, 2)
            failure = next(call.kwargs for call in self.log.event.call_args_list
                           if call.args[0] == "cloak_download_source_failed")
            self.assertEqual((failure["source"], failure["reason"]), ("official", "incomplete_download"))
            self.assertTrue(list(incomplete.parent.glob(".invalid-*/previous-file")))

    def test_tampered_download_never_installs(self):
        payload = tar_payload("chrome")
        asset = cloak.Asset("146.0.7680.177.5", "fixture.tar.gz", len(payload), "0" * 64)
        cache = self.root / ".cloak-bin"
        with (patch.object(cloak, "platform_key", return_value=("Linux", "x64")),
              patch.dict(cloak.ASSETS, {("Linux", "x64"): asset}),
              patch.object(cloak, "bounded_transfer", side_effect=inline_transfer),
              patch.object(cloak, "urlopen", side_effect=lambda *_a, **_kw: DownloadResponse(payload)) as fetch):
            with self.assertRaisesRegex(cloak.CloakError, "were rejected"):
                cloak.resolve_cloak(self.log, cache=cache, home_cache=self.root / "missing")
            self.assertEqual(fetch.call_count, 2)
            slot = cache / "linux-x64"
            self.assertEqual(list(slot.glob("chromium-*")), [])
            self.assertEqual(list(slot.glob(".download-*")), [])

    def test_offline_sources_log_both_failures_and_never_launch_browser(self):
        cache = self.root / ".cloak-bin"
        cfg = config(self.root)
        with (patch.object(cloak, "platform_key", return_value=("Linux", "x64")),
              patch.object(cloak, "bounded_transfer", side_effect=inline_transfer),
              patch.object(cloak, "urlopen", side_effect=[URLError(socket.gaierror("private DNS detail")),
                  HTTPError("https://example.invalid/?secret", 503, "unavailable", {}, None)]),
              patch.object(signin, "resolve_cloak", side_effect=lambda log: cloak.resolve_cloak(
                  log, cache=cache, home_cache=self.root / "missing")),
              patch.object(signin, "sync_playwright") as driver):
            code = signin.run_once(cfg, log=self.log, scheduled_day="2026-01-01")
            self.assertEqual(code, scheduler.RETRY_EXIT)
            driver.assert_not_called()
        failures = [call.kwargs for call in self.log.event.call_args_list
                    if call.args[0] == "cloak_download_source_failed"]
        self.assertEqual([(row["source"], row["reason"], row["http_status"]) for row in failures],
                         [("official", "dns", None), ("github", "http_status", 503)])
        self.assertEqual(self.log.event.call_args.kwargs["result"], "cloak_unavailable")
        self.assertNotIn("secret", str(self.log.mock_calls))
        self.assertEqual(list((cache / "linux-x64").glob(".download-*")), [])
        self.assertEqual(list((cache / "linux-x64").glob("chromium-*")), [])

    def test_stalled_transfer_is_killed_before_healthy_fallback_writes(self):
        archive = self.root / "fixture.tar"
        payload = b"verified fixture"
        asset = cloak.Asset("146.0.7680.177.5", archive.name, len(payload), hashlib.sha256(payload).hexdigest())
        log = runlog.RunLog("test", path=self.root / "events.jsonl")
        with (patch.object(cloak, "DOWNLOAD_TIMEOUT_SECONDS", 5),
              patch.object(cloak, "transfer_worker", stalled_then_healthy_source),
              patch.object(runlog, "emit_console")):
            started = monotonic()
            cloak.download(asset, archive, log)
        self.assertLess(monotonic() - started, 7)
        self.assertEqual(archive.read_bytes(), payload)
        pid = int(archive.with_suffix(".pid").read_text(encoding="ascii"))
        self.assertFalse(psutil.pid_exists(pid))
        rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        self.assertIn(("official", "timeout"),
                      [(row.get("source"), row.get("reason")) for row in rows])

    def test_archive_cannot_write_outside_staging(self):
        archive = self.root / "archive.tar.gz"
        archive.write_bytes(tar_payload("../out"))
        staging = self.root / "staging"
        staging.mkdir()
        with self.assertRaises(cloak.CloakError):
            cloak.extract(archive, staging, is_zip=False)
        self.assertFalse((self.root / "out").exists())

    def test_windows_package_layout_is_supported(self):
        archive = self.root / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("chrome-win64/chrome.exe", "fixture")
            bundle.writestr("chrome-win64/locales/en-US.pak", "fixture resource")
        intermediate = self.root / "intermediate"
        intermediate.mkdir()
        staging = intermediate / ".." / "staging"
        staging.mkdir()
        cloak.extract(archive, staging, is_zip=True)
        exe = staging / "chrome-win64" / "chrome.exe"
        exe.chmod(0o755)
        self.assertEqual(cloak.binary_in(staging, "Windows"), exe.resolve())

    def test_log_rotates_and_records_safe_location_instead_of_exception_text(self):
        log_path = self.root / "events.jsonl"
        log = runlog.RunLog("test", path=log_path)
        with patch.object(runlog, "LOG_MAX_BYTES", 500), patch.object(runlog, "emit_console"):
            log.event("first", fixture_padding="x" * 380)
            try:
                raise ValueError("fixture secret must not be logged")
            except ValueError as exc:
                log.exception("second", exc)
        old = json.loads(log_path.with_name("events.jsonl.1").read_text(encoding="utf-8"))
        current = log_path.read_text(encoding="utf-8")
        self.assertEqual(old["event"], "first")
        self.assertEqual(json.loads(current)["error_type"], "ValueError")
        self.assertNotIn("fixture secret", current)

    def test_expected_redirect_verifies_once_and_cross_site_redirect_is_not_followed(self):
        for status, location, outcome in [(303, "/", "signed"), (307, "https://example.invalid/", "unexpected_post_response")]:
            with self.subTest(status=status):
                cfg = config(self.root)
                flow = signin.CheckFlow(self.log)
                page, cdp = Mock(), Mock()
                handlers = {}
                cdp.on.side_effect = lambda name, handler: handlers.update({name: handler})

                def fetch(*_args):
                    flow.posts += 1
                    sent = handlers["Network.requestWillBeSent"]
                    sent({"requestId": "fixture", "request": {"method": "POST", "url": signin.CLOCK}})
                    sent({"requestId": "fixture", "request": {"method": "GET", "url": signin.HOME},
                          "redirectResponse": {"url": signin.CLOCK, "status": status, "headers": {"location": location}}})
                    return 0

                page.locator.return_value.evaluate.side_effect = fetch
                with (patch.object(signin, "claim_day", return_value=True),
                      patch.object(signin, "inspect_home", return_value={"home_status": 200, "result": "already_signed"}) as verify):
                    result = signin.submit_checkin(page, cdp, cfg, flow, {"session_state": "signed_in"})
                self.assertEqual(result["result"], outcome)
                self.assertEqual(result["post_count"], 1)
                self.assertEqual(verify.call_count, int(status == 303))
                self.assertFalse(flow.armed)

    def test_checkin_waits_for_security_page_then_stops_when_already_signed(self):
        cfg = config(self.root, challenge_wait_seconds=10)
        context = Mock(pages=[Mock()])
        blocked = {"home_status": 468, "session_state": "unknown", "result": "security_check", "post_count": 0}
        recovered = {"home_status": 200, "session_state": "signed_in", "result": "already_signed", "post_count": 0}
        with (patch.object(signin, "browser_session", return_value=nullcontext(context)),
              patch.object(signin, "inspect_home", side_effect=[blocked.copy(), recovered.copy()]),
              patch.object(signin, "restore_login", return_value={"login_result": "already_logged_in"}) as login):
            self.assertEqual(signin.run_once(cfg, log=self.log), 0)
            login.assert_called_once()
            self.assertEqual(login.call_args.kwargs["challenge_wait_ms"], 10000)
            self.assertEqual(self.log.event.call_args.kwargs["post_count"], 0)

    def test_home_outages_have_safe_diagnostics_without_login_or_submission(self):
        cfg = config(self.root)
        cases = [
            (503, None, "homepage_failed", None, True),
            (429, None, "homepage_failed", None, True),
            (None, BrowserError("net::ERR_NAME_NOT_RESOLVED at https://example.invalid/?secret"),
             "homepage_unreachable", "dns", True),
            (None, BrowserError("net::ERR_INTERNET_DISCONNECTED"), "homepage_unreachable", "offline", True),
            (None, BrowserError("net::ERR_CONNECTION_REFUSED"), "homepage_unreachable", "connection", True),
            (None, BrowserError("net::ERR_TIMED_OUT"), "operation_timed_out", "timeout", True),
            (None, BrowserTimeout("navigation timeout"), "operation_timed_out", "timeout", True),
            (None, BrowserError("net::ERR_CERT_AUTHORITY_INVALID"), "homepage_unreachable", "tls", False),
        ]
        for status, error, outcome, network, retry in cases:
            with self.subTest(status=status, network=network):
                log = runlog.RunLog("test", path=self.root / "events.jsonl")
                page = Mock()
                page.goto.return_value.status = status
                page.goto.side_effect = error
                with (patch.object(signin, "browser_session", return_value=nullcontext(Mock(pages=[page]))),
                      patch.object(signin, "restore_login") as login, patch.object(signin, "submit_checkin") as post,
                      patch.object(runlog, "emit_console")):
                    code = signin.run_once(cfg, log=log, scheduled_day="2026-01-01")
                    self.assertEqual(code, scheduler.RETRY_EXIT if retry else 1)
                    login.assert_not_called()
                    post.assert_not_called()
                    page.evaluate.assert_not_called()
                recorded = log.path.read_text(encoding="utf-8")
                row = json.loads(recorded.splitlines()[-1])
                self.assertEqual(row["result"], outcome)
                self.assertEqual(row.get("home_status"), status)
                self.assertEqual(row.get("network_error"), network)
                self.assertEqual(row["retryable"], retry)
                self.assertEqual(row["post_count"], 0)
                self.assertNotIn("secret", recorded)

    def test_login_home_disconnect_can_retry_but_verification_after_login_post_cannot(self):
        cfg = config(self.root)
        page = Mock()
        page.goto.return_value.status = 468
        context = Mock(pages=[page])
        context.new_page.return_value.goto.side_effect = BrowserError("net::ERR_CONNECTION_RESET")
        with patch.object(signin, "browser_session", return_value=nullcontext(context)):
            code = signin.run_once(cfg, log=self.log, scheduled_day="2026-01-01")
        self.assertEqual(code, scheduler.RETRY_EXIT)
        self.assertEqual(self.log.event.call_args.kwargs["network_error"], "connection")
        context.new_page.return_value.close.assert_called_once()

        page.goto.side_effect = [Mock(status=200), BrowserError("net::ERR_CONNECTION_RESET")]
        with (patch.object(signin, "browser_session", return_value=nullcontext(context)),
              patch.object(signin, "session_state", return_value="signed_out"),
              patch.object(signin, "page_state", return_value="ready"),
              patch.object(signin, "restore_login", return_value={"login_result": "submitted", "login_post_count": 1})):
            cfg = config(self.root, username="fixture-user", password="fixture-password")
            code = signin.run_once(cfg, log=self.log, scheduled_day="2026-01-01")
        self.assertEqual(code, 1)
        self.assertFalse(self.log.event.call_args.kwargs["retryable"])

    def test_expired_scheduled_date_does_not_reserve_or_submit_next_day(self):
        flow = signin.CheckFlow(self.log, scheduled_day="1900-01-01")
        page, cdp = Mock(), Mock()
        with patch.object(signin, "claim_day") as reserve:
            result = signin.submit_checkin(page, cdp, config(self.root), flow, {"session_state": "signed_in"})
        self.assertEqual(result["result"], "scheduled_day_expired")
        reserve.assert_not_called()
        page.locator.assert_not_called()

    def test_failed_post_keeps_reservation_disarms_and_never_retries(self):
        cfg = config(self.root)
        flow = signin.CheckFlow(self.log)
        page, cdp = Mock(), Mock()
        page.locator.return_value.evaluate.side_effect = TimeoutError
        with patch.object(signin, "claim_day", side_effect=(True, False)):
            first = signin.submit_checkin(page, cdp, cfg, flow, {"session_state": "signed_in"})
            next_run = signin.submit_checkin(page, cdp, cfg, signin.CheckFlow(self.log), {"session_state": "signed_in"})
        self.assertEqual(first["result"], "submission_outcome_unknown")
        self.assertEqual(next_run["result"], "attempt_already_reserved")
        first["retryable"] = True  # The reservation is the independent, final retry guard.
        flow.scheduled_day = "2026-01-01"
        self.assertEqual(signin.emit_result(flow, first), 1)
        self.assertFalse(self.log.event.call_args.kwargs["retryable"])
        page.locator.return_value.evaluate.assert_called_once()
        self.assertFalse(flow.armed)


if __name__ == "__main__":
    unittest.main()
