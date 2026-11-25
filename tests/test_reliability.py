from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from datetime import datetime, time
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import psutil

from mbautosign import cli as signin
from mbautosign import cloak_runtime as cloak
from mbautosign import runlog, scheduler
from mbautosign.settings import Settings


def config(root: Path, **overrides) -> Settings:
    return Settings(**({"profile": root / "profile", "executable": None, "headless": True, "timeout_ms": 2000,
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


class ReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = Mock(spec=runlog.RunLog)

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

    def test_missing_cloak_uses_verified_project_package_and_then_reuses_it(self):
        payload = tar_payload("chrome")
        asset = cloak.Asset("146.0.7680.177.5", "fixture.tar.gz", len(payload), hashlib.sha256(payload).hexdigest())
        cache = self.root / ".cloak-bin"
        incomplete = cache / "linux-x64" / f"chromium-{asset.version}"
        incomplete.mkdir(parents=True)
        (incomplete / "previous-file").touch()
        other_browser = self.root / "Chrome" / "chrome"
        other_browser.parent.mkdir()
        other_browser.write_bytes(b"not cloak")

        with (patch.object(cloak, "platform_key", return_value=("Linux", "x64")),
              patch.dict(cloak.ASSETS, {("Linux", "x64"): asset}),
              patch.object(cloak, "urlopen", return_value=DownloadResponse(payload)) as fetch):
            cfg = config(self.root, executable=other_browser)
            first = cloak.resolve_cloak(cfg, self.log, cache=cache, home_cache=self.root / "missing")
            second = cloak.resolve_cloak(cfg, self.log, cache=cache, home_cache=self.root / "missing")
            self.assertEqual(first, second)
            self.assertTrue(first.is_relative_to(cache / "linux-x64"))
            self.assertEqual(fetch.call_count, 1)
            self.assertIn("cloak_configured_path_rejected", [call.args[0] for call in self.log.event.call_args_list])
            self.assertTrue(list(incomplete.parent.glob(".invalid-*/previous-file")))

    def test_tampered_download_never_installs(self):
        payload = tar_payload("chrome")
        asset = cloak.Asset("146.0.7680.177.5", "fixture.tar.gz", len(payload), "0" * 64)
        cache = self.root / ".cloak-bin"
        with (patch.object(cloak, "platform_key", return_value=("Linux", "x64")),
              patch.dict(cloak.ASSETS, {("Linux", "x64"): asset}),
              patch.object(cloak, "urlopen", return_value=DownloadResponse(payload))):
            with self.assertRaises(cloak.CloakError):
                cloak.resolve_cloak(config(self.root), self.log, cache=cache, home_cache=self.root / "missing")
            slot = cache / "linux-x64"
            self.assertEqual(list(slot.glob("chromium-*")), [])
            self.assertEqual(list(slot.glob(".download-*")), [])

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
        staging = self.root / "staging"
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

    def test_read_only_keeps_security_page_inert_and_login_mode_can_wait(self):
        cfg = config(self.root, challenge_wait_seconds=10)
        context = Mock(pages=[Mock()])
        blocked = {"home_status": 468, "session_state": "unknown", "result": "security_check", "post_count": 0}
        recovered = {"home_status": 200, "session_state": "signed_in", "result": "already_signed", "post_count": 0}
        with (patch.object(signin, "browser_session", return_value=nullcontext(context)),
              patch.object(signin, "inspect_home", side_effect=[blocked.copy(), blocked.copy(), recovered.copy()]),
              patch.object(signin, "restore_login", return_value={"login_result": "already_logged_in"}) as login):
            self.assertEqual(signin.run_once(cfg, submit=False, log=self.log), 1)
            login.assert_not_called()
            self.assertEqual(signin.run_once(cfg, submit=False, login_only=True, log=self.log), 0)
            self.assertEqual(login.call_args.kwargs["challenge_wait_ms"], 10000)
            self.assertEqual(self.log.event.call_args.kwargs["post_count"], 0)

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
        page.locator.return_value.evaluate.assert_called_once()
        self.assertFalse(flow.armed)


if __name__ == "__main__":
    unittest.main()
