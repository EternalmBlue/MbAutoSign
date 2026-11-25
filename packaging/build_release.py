from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import sysconfig
import tomllib
from tempfile import TemporaryDirectory
from time import monotonic, sleep

import psutil


ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def write_notices(path: Path) -> None:
    blocks = ["MbAutoSign distribution: third-party notices\n",
              "Cloak Browser is not included. The application obtains it directly from CloakHQ.\n"]
    python_license = next((item for item in (Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE")
        if item.is_file()), None)
    if python_license is None:
        raise RuntimeError("Cannot find the Python runtime license")
    blocks.append("=== Python ===\n" + python_license.read_text(encoding="utf-8"))
    for package in ("filelock", "greenlet", "playwright", "psutil", "pyee", "PyInstaller", "typing-extensions", "tzdata"):
        try:
            dist = metadata.distribution(package)
        except metadata.PackageNotFoundError:
            continue
        blocks.append(f"=== {dist.metadata['Name']} {dist.version} ===")
        for entry in dist.files or ():
            name = entry.name.lower()
            if name.startswith(("license", "licence", "notice", "copying")):
                file = dist.locate_file(entry)
                if file.is_file():
                    blocks.append(f"--- {entry} ---\n" + file.read_text(encoding="utf-8", errors="replace"))
    path.write_text("\n\n".join(blocks), encoding="utf-8")


def smoke(command: list[str], root: Path, scratch: Path) -> None:
    home = scratch / "empty-home"
    home.mkdir()
    proxy = "http://127.0.0.1:1"
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch),
           "HTTPS_PROXY": proxy, "https_proxy": proxy, "HTTP_PROXY": proxy, "http_proxy": proxy,
           "NO_PROXY": "", "no_proxy": "", "MBAUTOSIGN_JOB_ID": "", "MBAUTOSIGN_JOB_DAY": ""}
    log = root / "logs" / "checkins.jsonl"

    def run(expected: int) -> list[dict]:
        result = subprocess.run(command, cwd=scratch, env=env, timeout=90, capture_output=True)
        if result.returncode != expected or not log.is_file():
            raise RuntimeError(f"Smoke test expected exit {expected}, got {result.returncode}: "
                               f"{result.stdout!r}\n{result.stderr!r}")
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        log.unlink()
        return rows

    def verify_scheduler() -> None:
        with (scratch / "scheduler-output.txt").open("wb") as console:
            worker = subprocess.Popen(command, cwd=scratch, env=env, stdout=console, stderr=console)
            try:
                deadline = monotonic() + 90
                while monotonic() < deadline and worker.poll() is None:
                    if log.is_file():
                        lines = log.read_text(encoding="utf-8").splitlines()
                        rows = [json.loads(line) for line in lines if line.endswith("}")]
                        finished = next((row for row in rows if row["event"] == "worker_finished"), None)
                        if finished:
                            assert finished["exit_code"] == 75, rows
                            assert any(row["event"] == "run_finished" and row["run_id"] == finished["job_id"]
                                       and row["result"] == "cloak_unavailable" for row in rows)
                            return
                    sleep(0.1)
                raise RuntimeError("Frozen scheduler did not launch and finish its worker")
            finally:
                if worker.poll() is None and os.name != "nt":
                    worker.send_signal(signal.SIGTERM)
                    try:
                        worker.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        pass
                if worker.poll() is None:
                    try:
                        descendants = psutil.Process(worker.pid).children(recursive=True)
                    except psutil.NoSuchProcess:
                        descendants = []
                    for process in reversed(descendants):
                        try:
                            process.kill()
                        except psutil.NoSuchProcess:
                            pass
                    worker.kill()
                    worker.wait(timeout=8)
                    psutil.wait_procs(descendants, timeout=5)

    config = root / "config.toml"
    assert list(root.glob("*.toml")) == [config], "The archive must include only config.toml"
    original = config.read_bytes()
    text = original.decode("utf-8")
    defaults = tomllib.loads(text)
    if defaults.get("login") != {"username": "", "password": ""}:
        raise RuntimeError("The release config must contain blank login credentials")
    schedule = defaults["schedule"]
    assert schedule["enable"] is True and schedule["time"] == "00:05" and schedule["timezone"] == "Asia/Shanghai"
    try:
        config.unlink()
        rows = run(2)
        assert rows[-1]["event"] == "configuration_file_missing" and "config.toml" in rows[-1]["action"]
        assert not config.exists()

        config.write_text(text.replace("enable = true", "enable = false", 1), encoding="utf-8", newline="")
        rows = run(1)
        failures = [row["source"] for row in rows if row["event"] == "cloak_download_source_failed"]
        assert failures == ["official", "github"], rows
        assert rows[-1]["result"] == "cloak_unavailable" and rows[-1]["post_count"] == 0
        assert not list((root / ".cloak-bin").glob("*/.download-*"))
        config.write_text(text.replace('time = "00:05"', 'time = "00:00"', 1), encoding="utf-8", newline="")
        verify_scheduler()
    finally:
        config.write_bytes(original)


def build(output: Path) -> None:
    system = platform.system()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if (system, arch) not in {("Windows", "x64"), ("Linux", "x64"), ("Linux", "arm64")}:
        raise RuntimeError(f"Unsupported build target: {system}/{arch}")
    windows = system == "Windows"
    name = "MbAutoSign"
    distribution = f"{name}-{system.lower()}-{arch}"
    executable = f"{name}.exe" if windows else name
    launcher = "run-windows.ps1" if windows else "run-linux.sh"
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mbautosign-release-") as temp:
        work = Path(temp)
        target = work / "dist"
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx",
            "--onefile", "--name", name,
            "--collect-all", "playwright", "--collect-data", "tzdata",
            "--distpath", str(target), "--workpath", str(work / "build"), "--specpath", str(work),
            str(ROOT / "signin.py"),
        ], cwd=ROOT, check=True)
        package = work / distribution
        package.mkdir()
        shutil.move(target / executable, package / executable)
        shutil.copyfile(PACKAGING / launcher, package / launcher)
        if not windows:
            (package / launcher).chmod(0o755)
        shutil.copyfile(PACKAGING / "config.toml", package / "config.toml")
        write_notices(package / "THIRD-PARTY-NOTICES.txt")

        archive = Path(shutil.make_archive(str(package), "zip" if windows else "gztar",
                                          root_dir=work, base_dir=distribution))
        extracted = work / "extracted with spaces"
        shutil.unpack_archive(archive, extracted)
        installed = extracted / distribution
        command = [str(installed / launcher)]
        if windows:
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
                       "-File", *command]
        smoke(command, installed, work)
        shutil.copy2(archive, output / archive.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build and smoke-test this platform's release assets.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    build(parser.parse_args().output.resolve())
