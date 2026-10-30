"""Build a distributable: python packaging/build.py

Outputs into dist/:
  Windows  cqta-<version>-windows-<arch>.exe      single-file executable
  Linux    cqta-<version>-linux-<arch>.AppImage   needs appimagetool on PATH (or APPIMAGETOOL=...)
  macOS    cqta-<version>-macos-<arch>.dmg        signed .app (CQTA_CODESIGN_IDENTITY, ad-hoc if unset)

macOS optional: CQTA_NOTARY_PROFILE=<notarytool keychain profile> to notarize and staple.
Requires: pip install -e .[build]
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "src" / "cqta" / "cqta.png"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from cqta import __version__  # noqa: E402


def arch() -> str:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64" if sys.platform == "darwin" else "aarch64"
    return m


def run(cmd: list, **kw) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def make_icons() -> Path:
    """Render the app icon with Qt into packaging/icons/."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    app = QGuiApplication.instance() or QGuiApplication([])
    source = QImage(str(ICON))

    def make_pixmap(size: int):
        return source.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    out = BUILD / "icons"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    make_pixmap(512).save(str(out / "cqta.png"))
    if sys.platform == "win32":
        make_pixmap(256).save(str(out / "cqta.ico"))
    if sys.platform == "darwin":
        iconset = out / "cqta.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            make_pixmap(base).save(str(iconset / f"icon_{base}x{base}.png"))
            make_pixmap(base * 2).save(str(iconset / f"icon_{base}x{base}@2x.png"))
        run(["iconutil", "-c", "icns", iconset, "-o", out / "cqta.icns"])
    del app
    return out


def pyinstaller(name: str, extra: list) -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", name,
         "--distpath", DIST / "pyi", "--workpath", BUILD / "pyi", "--specpath", BUILD,
         "--add-data", f"{ICON}{os.pathsep}cqta", "--add-data", f"{ICON.with_name('cqta-tray.png')}{os.pathsep}cqta", "--collect-all", "pytap3", "--collect-all", "sounddevice", "--collect-submodules", "bleak",
         "--exclude-module", "tkinter", "--exclude-module", "PySide6.QtWebEngineCore",
         "--paths", ROOT / "src", *extra, HERE / "launcher.py"])


def build_windows(icons: Path) -> Path:
    name = f"cqta-{__version__}-windows-{arch()}"
    pyinstaller(name, ["--onefile", "--windowed", "--icon", icons / "cqta.ico"])
    out = DIST / f"{name}.exe"
    shutil.move(DIST / "pyi" / f"{name}.exe", out)
    return out


def build_linux(icons: Path) -> Path:
    name = f"cqta-{__version__}-linux-{arch()}"
    pyinstaller("cqta", ["--onedir", "--windowed"])
    appdir = BUILD / "AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    (appdir / "usr" / "bin").mkdir(parents=True)
    shutil.copytree(DIST / "pyi" / "cqta", appdir / "usr" / "bin" / "cqta")
    shutil.copy(icons / "cqta.png", appdir / "cqta.png")
    (appdir / "cqta.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=CQta\nComment=IP chat and file transfer over amateur radio\n"
        "Exec=cqta\nIcon=cqta\nCategories=HamRadio;Network;\nTerminal=false\n")
    apprun = appdir / "AppRun"
    apprun.write_text('#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
                      'exec "$HERE/usr/bin/cqta/cqta" "$@"\n')
    apprun.chmod(0o755)
    tool = os.environ.get("APPIMAGETOOL") or shutil.which("appimagetool")
    if not tool:
        sys.exit("appimagetool not found; install it or set APPIMAGETOOL")
    out = DIST / f"{name}.AppImage"
    env = dict(os.environ, ARCH=arch())
    run([tool, "--appimage-extract-and-run", appdir, out], env=env)
    return out


def build_macos(icons: Path) -> Path:
    name = f"cqta-{__version__}-macos-{arch()}"
    entitlements = HERE / "entitlements.plist"
    pyinstaller("CQta", [
        *[a for m in ("objc", "CoreBluetooth", "libdispatch", "Foundation", "CoreFoundation", "UserNotifications")
          for a in ("--collect-all", m)],
        "--windowed", "--onedir", "--icon", icons / "cqta.icns", "--osx-bundle-identifier", "org.cqta.app",
    ])
    app = DIST / "pyi" / "CQta.app"
    plist = app / "Contents" / "Info.plist"
    import plistlib

    info = plistlib.loads(plist.read_bytes())
    info.update({
        "CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
        "NSMicrophoneUsageDescription": "CQta receives AFSK packet audio from your radio's audio interface.",
        "NSBluetoothAlwaysUsageDescription": "CQta connects to Meshtastic nodes over Bluetooth LE.",
        "NSHighResolutionCapable": True,
    })
    plist.write_bytes(plistlib.dumps(info))
    identity = os.environ.get("CQTA_CODESIGN_IDENTITY", "-")
    sign = ["codesign", "--force", "--deep", "--timestamp" if identity != "-" else "--timestamp=none",
            "--options", "runtime", "--entitlements", entitlements, "--sign", identity]
    run([*sign, app])
    run(["codesign", "--verify", "--deep", "--strict", app])
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / "CQta.app", symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    out = DIST / f"{name}.dmg"
    out.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", f"CQta {__version__}", "-srcfolder", stage, "-ov", "-format", "UDZO", out])
    if identity != "-":
        run(["codesign", "--force", "--sign", identity, out])
    profile = os.environ.get("CQTA_NOTARY_PROFILE")
    if profile and identity != "-":
        run(["xcrun", "notarytool", "submit", out, "--keychain-profile", profile, "--wait"])
        run(["xcrun", "stapler", "staple", out])
    return out


def main() -> None:
    DIST.mkdir(exist_ok=True)
    icons = make_icons()
    builder = {"win32": build_windows, "linux": build_linux, "darwin": build_macos}.get(sys.platform)
    if builder is None:
        sys.exit(f"Unsupported platform {sys.platform}")
    out = builder(icons)
    print(f"\nBuilt {out}")


if __name__ == "__main__":
    main()
