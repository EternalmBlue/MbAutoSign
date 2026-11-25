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
import tarfile
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


def make_linux_installer(package: Path, output: Path, scratch: Path) -> None:
    archive = scratch / "payload.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=6) as stream:
        for entry in sorted(package.iterdir()):
            stream.add(entry, arcname=entry.name)
    header = """#!/usr/bin/env bash
set -euo pipefail
if (( $# > 1 )); then
    printf 'Usage: %s [new-install-directory]\n' "$0" >&2
    exit 2
fi
destination=${1:-./MbAutoSign}
parent=$(dirname -- "$destination")
if [[ -e "$destination" || -L "$destination" ]]; then
    printf 'Install directory already exists: %s\n' "$destination" >&2
    exit 2
fi
staging=$(mktemp -d -- "$parent/.mbautosign-install-XXXXXXXX")
trap 'rm -rf -- "$staging"' EXIT
tail -n +PAYLOAD_LINE -- "$0" | tar -xz -C "$staging"
mv -nT -- "$staging" "$destination"
if [[ -d "$staging" ]]; then
    printf 'Install directory already exists: %s\n' "$destination" >&2
    exit 2
fi
printf 'Installed in %s. Create config.toml from config.example.toml, then run run-linux.sh.\n' "$destination"
exit 0
"""
    header = header.replace("PAYLOAD_LINE", str(header.count("\n") + 1))
    with output.open("wb") as target, archive.open("rb") as payload:
        target.write(header.encode("ascii"))
        shutil.copyfileobj(payload, target)
    output.chmod(0o755)


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
    rows = run(2)
    assert rows[-1]["event"] == "configuration_file_missing" and "config.toml" in rows[-1]["action"]
    assert not config.exists()
    shutil.copyfile(ROOT / "config.example.toml", config)  # Test fixture only; the application never creates config.toml.
    try:
        rows = run(1)
        failures = [row["source"] for row in rows if row["event"] == "cloak_download_source_failed"]
        assert failures == ["official", "github"], rows
        assert rows[-1]["result"] == "cloak_unavailable" and rows[-1]["post_count"] == 0
        assert not list((root / ".cloak-bin").glob("*/.download-*"))
        text = config.read_text(encoding="utf-8").replace("enable = false", "enable = true")
        config.write_text(text.replace('time = "09:00"', 'time = "00:00"'), encoding="utf-8")
        verify_scheduler()
    finally:
        config.unlink()


def build(output: Path) -> None:
    system = platform.system()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if (system, arch) not in {("Windows", "x64"), ("Linux", "x64"), ("Linux", "arm64")}:
        raise RuntimeError(f"Unsupported build target: {system}/{arch}")
    windows = system == "Windows"
    name = "MbAutoSign-windows-x64" if windows else "MbAutoSign"
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mbautosign-release-") as temp:
        work = Path(temp)
        target = work / "dist"
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx",
            "--onefile" if windows else "--onedir", "--name", name,
            "--collect-all", "playwright", "--collect-data", "tzdata",
            "--distpath", str(target), "--workpath", str(work / "build"), "--specpath", str(work),
            str(ROOT / "signin.py"),
        ], cwd=ROOT, check=True)
        if windows:
            exe = target / f"{name}.exe"
            launcher = target / "run-windows.cmd"
            shutil.copyfile(PACKAGING / launcher.name, launcher)
            smoke([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(launcher)], target, work)
            shutil.copyfile(exe, output / exe.name)
            shutil.copyfile(launcher, output / launcher.name)
            shutil.copyfile(ROOT / "config.example.toml", output / "config.example.toml")
            write_notices(output / "THIRD-PARTY-NOTICES-windows-x64.txt")
        else:
            package = target / name
            launcher = package / "run-linux.sh"
            shutil.copyfile(PACKAGING / launcher.name, launcher)
            launcher.chmod(0o755)
            shutil.copyfile(ROOT / "config.example.toml", package / "config.example.toml")
            write_notices(package / "THIRD-PARTY-NOTICES.txt")
            installer = work / f"MbAutoSign-linux-{arch}.sh"
            make_linux_installer(package, installer, work)
            installed = work / "installed with spaces"
            subprocess.run(["bash", str(installer), str(installed)], cwd=work, check=True, timeout=90)
            smoke([str(installed / launcher.name)], installed, work)
            shutil.copy2(installer, output / installer.name)
            shutil.copy2(launcher, output / launcher.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build and smoke-test this platform's release assets.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    build(parser.parse_args().output.resolve())
