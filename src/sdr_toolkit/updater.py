"""Updates from the project's GitHub releases.

Each release carries the wheel and sdr-toolkit.json ({"version", "wheel",
"sha256"}). The wheel must come from github.com, match the checksum, and is
installed with pip into the venv scripts/install.sh made. Running from a source
checkout, the app only reports that an update exists.

On Windows, the release also carries the installer and sdr-toolkit-windows.json
({"version", "installer", "sha256"}): the installed app downloads the installer,
checks it the same way, runs it silently and exits; the installer starts the new
version when it's done (as SDR Tools Relay updates itself).
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from sdr_toolkit import REPO, __version__, system

LOG = logging.getLogger(__name__)

API = f"https://api.github.com/repos/{REPO}/releases/latest"
INFO_ASSET = "sdr-toolkit-windows.json" if system.WINDOWS else "sdr-toolkit.json"
CHECK_EVERY = 12 * 3600
USER_AGENT = f"sdr-toolkit/{__version__}"


@dataclass
class Update:
    version: str
    wheel_url: str          # the wheel's API address (see _download)
    wheel_name: str
    sha256: str
    wheel_link: str = ""    # its plain download link, the fallback


def parse_version(version: str) -> tuple[int, ...]:
    parts = []
    for part in version.lstrip("v").split("."):
        digits = "".join(c for c in part if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _get(url: str, accept: str | None = None, timeout=20) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _trusted(url: str) -> bool:
    return url.startswith((f"https://api.github.com/repos/{REPO}/releases/assets/",
                           f"https://github.com/{REPO}/releases/download/"))


def _download(api_url: str, link: str) -> bytes:
    """A release file: through the API first, then the plain link.

    GitHub's plain release links time out for anonymous users at busy times (an incident
    on 2026-11-09 had every one of them failing for minutes) while the API still served the
    same files.
    """
    errors = []
    for url, accept in ((api_url, "application/octet-stream"), (link, None)):
        if not url or not _trusted(url):
            continue
        try:
            return _get(url, accept, timeout=60)
        except OSError as e:
            errors.append(f"{url}: {e}")
            LOG.info("download failed, %s", errors[-1])
    raise OSError("; ".join(errors) or "no trusted download address")


def check() -> Update | None:
    """The latest release if it's newer than this version, else None."""
    release = json.loads(_get(API, "application/vnd.github+json"))
    assets = {a["name"]: (a["url"], a["browser_download_url"]) for a in release.get("assets", [])}
    if INFO_ASSET not in assets:
        return None
    info = json.loads(_download(*assets[INFO_ASSET]))
    if parse_version(info["version"]) <= parse_version(__version__):
        return None
    name = info["installer" if system.WINDOWS else "wheel"]
    if name not in assets:
        return None
    api_url, link = assets[name]
    if not (_trusted(api_url) or _trusted(link)):
        return None
    return Update(info["version"], api_url, name, info["sha256"].lower(), link)


def _state_dir() -> Path:
    return system.state_dir()


def due() -> bool:
    try:
        last = json.loads((_state_dir() / "update.json").read_text())["checked_at"]
    except (OSError, ValueError, KeyError):
        last = 0
    return time.time() - last > CHECK_EVERY


def mark_checked() -> None:
    (_state_dir() / "update.json").write_text(json.dumps({"checked_at": time.time()}))


def installed_in_venv() -> bool:
    """True when running from the venv scripts/install.sh made."""
    return (Path(sys.prefix) / "sdr-toolkit-venv").exists()


def can_install() -> bool:
    """This copy can update itself: the Windows installer's, or install.sh's venv."""
    return system.FROZEN if system.WINDOWS else installed_in_venv()


def install(update: Update) -> None:
    """Download, verify and pip-install the update. Raises on any failure.

    New outside parts a version needs are offered by the window after the restart.
    """
    if not can_install():
        raise RuntimeError(
            f"This copy runs from source. Update it with git, or get "
            f"{update.version} from https://github.com/{REPO}/releases"
        )
    folder = _state_dir() / "updates"
    folder.mkdir(exist_ok=True)
    path = folder / update.wheel_name
    data = _download(update.wheel_url, update.wheel_link)
    if hashlib.sha256(data).hexdigest() != update.sha256:
        raise RuntimeError("The download didn't match its checksum; nothing was installed.")
    path.write_bytes(data)
    if system.WINDOWS:
        # The installer closes this app, replaces it and starts the new version (see restart()).
        subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], close_fds=True)
        LOG.info("started the installer for %s", update.version)
        return
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", str(path)],
        capture_output=True,
        text=True,
    )
    path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"pip couldn't install the update:\n{result.stderr[-2000:]}")
    LOG.info("installed update %s", update.version)


def restart() -> None:
    if system.WINDOWS:
        os._exit(0)             # the installer is running: get out of its way; it starts the new version
    os.execv(sys.executable, [sys.executable, "-m", "sdr_toolkit"])
