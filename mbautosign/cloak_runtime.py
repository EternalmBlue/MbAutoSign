from __future__ import annotations

import hashlib
import http.client
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from filelock import FileLock

from .runlog import RunLog
from .settings import ROOT, Settings


CACHE = ROOT / ".cloak-bin"
RELEASES = (("official", "https://cloakbrowser.dev"),
            ("github", "https://github.com/CloakHQ/CloakBrowser/releases/download"))
VERSION_DIR = re.compile(r"chromium-([0-9]+(?:\.[0-9]+){3,4})(?:-pro)?\Z")


class CloakError(RuntimeError):
    pass


@dataclass(frozen=True)
class Asset:
    version: str
    filename: str
    size: int
    sha256: str


# Official release assets; the pinned digest is checked before opening any archive.
ASSETS = {
    ("Windows", "x64"): Asset("146.0.7680.177.5", "cloakbrowser-windows-x64.zip", 562004238,
                              "b213795cb32c3169f766c74ce1d0275fc89d3df256de39c04da7fb4c23b7fdbe"),
    ("Linux", "x64"): Asset("146.0.7680.177.5", "cloakbrowser-linux-x64.tar.gz", 216890134,
                            "4a12bcde95fa1bb1beef2b41ab5e5c27c36be78e3be3d0dac8c64d705216670e"),
    ("Linux", "arm64"): Asset("146.0.7680.177.3", "cloakbrowser-linux-arm64.tar.gz", 208053005,
                              "8b71ce53b4fd131327331a31fba3835d71882d19bfaabde78dd0f5390bd16f45"),
}


def platform_key() -> tuple[str, str]:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return platform.system(), arch


def binary_in(folder: Path, system: str) -> Path | None:
    variants = ("chrome.exe", "chrome-win64/chrome.exe", "chrome-win/chrome.exe") if system == "Windows" else (
        "chrome", "chrome-linux64/chrome", "chrome-linux/chrome",
    )
    try:
        root = folder.resolve()
    except (OSError, RuntimeError):
        return None
    for variant in variants:
        try:
            path = (root / variant).resolve()
            if path.is_relative_to(root) and path.is_file() and os.access(path, os.X_OK):
                return path
        except (OSError, RuntimeError):
            continue
    return None


def configured_binary(path: Path, system: str) -> Path | None:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    # A generic Chrome/Chromium executable must not be used as a fallback.
    for version in (resolved.parent, resolved.parent.parent):
        in_cache = version.parent.name == ".cloakbrowser" or version.parent.parent.name == ".cloak-bin"
        if VERSION_DIR.fullmatch(version.name) and in_cache:
            if binary_in(version, system) == resolved:
                return resolved
    return None


def installed_binary(cache: Path, home_cache: Path, system: str) -> tuple[Path, str] | None:
    for parent, source in ((cache, "project_cache"), (home_cache, "local_install")):
        if not parent.is_dir():
            continue
        candidates = []
        for folder in parent.iterdir():
            version = VERSION_DIR.fullmatch(folder.name)
            if version and not folder.is_symlink() and (binary := binary_in(folder, system)):
                candidates.append((tuple(map(int, version[1].split("."))), binary))
        if candidates:
            return max(candidates)[1], source
    return None


def download_from(url: str, asset: Asset, path: Path, log: RunLog, deadline: float) -> None:
    request = Request(url, headers={"User-Agent": "MbAutoSign-CloakBrowser"})
    received = 0
    digest = hashlib.sha256()
    progress = time.monotonic()
    with urlopen(request, timeout=30) as response, path.open("xb") as output:
        if not response.geturl().startswith("https://"):
            raise CloakError("Download left HTTPS")
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > asset.size:
                raise CloakError("Archive exceeds the pinned asset size")
            digest.update(chunk)
            output.write(chunk)
            now = time.monotonic()
            if now > deadline:
                raise TimeoutError("Cloak download timed out")
            if now - progress >= 20:
                log.event("cloak_download_progress", bytes_received=received, bytes_total=asset.size)
                progress = now
    if received != asset.size or digest.hexdigest() != asset.sha256:
        raise CloakError("Cloak release size or SHA-256 verification failed")
    log.event("cloak_download_verified", bytes_received=received)


def download(asset: Asset, path: Path, log: RunLog) -> None:
    deadline = time.monotonic() + 540
    for index, (source, base) in enumerate(RELEASES):
        log.event("cloak_download_source", source=source)
        try:
            download_from(f"{base}/chromium-v{asset.version}/{asset.filename}", asset, path, log, deadline)
            return
        except (URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            log.exception("cloak_download_source_failed", exc, source=source)
            if index == len(RELEASES) - 1 or time.monotonic() >= deadline:
                raise
            path.unlink(missing_ok=True)


def extract(archive: Path, destination: Path, *, is_zip: bool) -> None:
    def target(name: str) -> Path:
        parts = name.rstrip("/").split("/")
        if any(part in {"", ".."} or "\\" in part or ":" in part for part in parts):
            raise CloakError("Unsafe archive member")
        path = (destination / Path(*parts)).resolve()
        if not path.is_relative_to(destination):
            raise CloakError("Archive member escapes installation directory")
        return path

    if is_zip:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                path = target(member.filename)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise CloakError("Unexpected symlink in Cloak ZIP archive")
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as source, path.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
    else:
        with tarfile.open(archive, "r|gz") as bundle:
            for member in bundle:
                path = target(member.name)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(member) as source, path.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    path.chmod(0o644 | (member.mode & 0o111))
                else:
                    raise CloakError("Unexpected non-file member in Cloak TAR archive")


def resolve_cloak(config: Settings, log: RunLog, *, cache: Path = CACHE, home_cache: Path | None = None) -> Path:
    system, arch = platform_key()
    asset = ASSETS.get((system, arch))
    if asset is None:
        raise CloakError("Unsupported platform for Cloak Browser")
    if cache.is_symlink():
        raise CloakError("The Cloak project cache must be a real directory")
    cache = cache / f"{system.lower()}-{arch}"
    if cache.is_symlink():
        raise CloakError("The Cloak platform cache must be a real directory")
    if config.executable is not None:
        if binary := configured_binary(config.executable, system):
            log.event("cloak_ready", source="configured", platform=system, arch=arch)
            return binary
        log.event("cloak_configured_path_rejected", level="warning", platform=system, arch=arch)
    home_cache = Path.home() / ".cloakbrowser" if home_cache is None else home_cache
    if found := installed_binary(cache, home_cache, system):
        binary, source = found
        log.event("cloak_ready", source=source, platform=system, arch=arch)
        return binary

    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    with FileLock(cache / ".install.lock", timeout=60):
        if found := installed_binary(cache, home_cache, system):
            binary, source = found
            log.event("cloak_ready", source=source, platform=system, arch=arch)
            return binary
        destination = cache / f"chromium-{asset.version}"
        # Only clean temporary directories left in this cache by an interrupted installer.
        for stale in cache.glob(".download-*"):
            if not stale.is_symlink() and stale.is_dir() and stale.resolve().parent == cache:
                shutil.rmtree(stale)
        log.event("cloak_download_started", platform=system, arch=arch, version=asset.version, bytes_total=asset.size)
        with tempfile.TemporaryDirectory(dir=cache, prefix=".download-") as temporary:
            staging = Path(temporary)
            archive = staging / asset.filename
            unpacked = staging / "unpacked"
            unpacked.mkdir()
            download(asset, archive, log)
            log.event("cloak_extract_started")
            extract(archive, unpacked, is_zip=asset.filename.endswith(".zip"))
            candidates = [unpacked, *(path for path in unpacked.iterdir() if path.is_dir())]
            package = next((path for path in candidates if binary_in(path, system)), None)
            if package is None:
                raise CloakError("The verified Cloak package has no expected executable")
            previous = None
            if destination.exists() or destination.is_symlink():
                previous = cache / f".invalid-{asset.version}-{uuid4().hex}"
                destination.rename(previous)
                log.event("cloak_invalid_cache_quarantined", level="warning", version=asset.version)
            try:
                package.rename(destination)
            except OSError:
                if previous is not None:
                    previous.rename(destination)
                raise
        binary = binary_in(destination, system)
        if binary is None:
            raise CloakError("The Cloak executable is unavailable after installation")
        log.event("cloak_ready", source="downloaded", platform=system, arch=arch, version=asset.version)
        return binary
