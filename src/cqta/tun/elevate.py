"""Cross-platform privilege elevation, used only to start the TUN helper."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys


class ElevationError(Exception):
    pass


def is_elevated() -> bool:
    if sys.platform == "win32":
        import ctypes

        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def helper_command(args: list[str]) -> list[str]:
    """Command line that re-runs this program in helper mode."""
    if getattr(sys, "frozen", False):
        exe = os.environ.get("APPIMAGE") or sys.executable  # root cannot see the user's FUSE mount
        return [exe, "--tun-helper", *args]
    return [sys.executable, "-m", "cqta", "--tun-helper", *args]


def launch_elevated(cmd: list[str]) -> subprocess.Popen | None:
    """Start `cmd` with administrator rights. Returns a Popen when we can watch the process (else None)."""
    if is_elevated():
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "darwin":
        return _macos(cmd)
    if sys.platform == "win32":
        return _windows(cmd)
    return _linux(cmd)


def _macos(cmd: list[str]) -> None:
    shell = " ".join(shlex.quote(c) for c in cmd) + " >/dev/null 2>&1 &"
    escaped = shell.replace("\\", "\\\\").replace('"', '\\"')
    prompt = "CQta needs administrator rights to create a network interface."
    script = f'do shell script "{escaped}" with prompt "{prompt}" with administrator privileges'
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        msg = r.stderr.strip()
        raise ElevationError("Administrator access was denied" if "-128" in msg else msg or "osascript failed")
    return None


def _windows(cmd: list[str]) -> None:
    import ctypes

    params = subprocess.list2cmdline(cmd[1:])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", cmd[0], params, None, 0)
    if rc <= 32:
        raise ElevationError("Administrator access was denied" if rc == 5 else f"ShellExecute failed ({rc})")
    return None


def _linux(cmd: list[str]) -> subprocess.Popen:
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        raise ElevationError("pkexec (polkit) is not installed; run CQta as root to use a system network interface")
    return subprocess.Popen([pkexec, *cmd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
