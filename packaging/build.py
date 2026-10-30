"""Build a distributable: python packaging/build.py

Outputs into dist/:
  Windows  cqta-<version>-windows-<arch>.exe      single-file executable
  Linux    cqta-<version>-linux-<arch>.AppImage   needs appimagetool on PATH (or APPIMAGETOOL=...) and docker
  macOS    cqta-<version>-macos-<arch>.dmg        signed .app

Linux: the PyInstaller step runs in a manylinux_2_28 container (glibc 2.28, so it runs on older distributions);
  CQTA_NO_CONTAINER=1 builds against the host's glibc instead.
macOS: runs in a venv made from python.org's Python 3.13 (CQTA_MACOS_PYTHON overrides it) using only wheels
  built for macOS 12 or older, and fails if any bundled binary needs something newer.
  Signing picks the first available of: CQTA_CODESIGN_IDENTITY, a Developer ID Application certificate, any other
  valid certificate (Apple Development / personal team), a self-signed certificate, then ad-hoc.
  With a Developer ID certificate the app and dmg are notarized and stapled so Gatekeeper accepts them, using
  APPLE_ID + APPLE_TEAM_ID + APPLE_APP_PASSWORD, or the notarytool keychain profile CQTA_NOTARY_PROFILE (default
  "app-notary"). Without either, the build continues unnotarized.
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

from cqta import APP_ID, __version__  # noqa: E402


MACOS_MIN = (12, 0)  # newest PySide6 wheels that still run on this release are 6.9
MANYLINUX_IMAGE = "quay.io/pypa/manylinux_2_28_{arch}"
DEFAULT_NOTARY_PROFILE = "app-notary"
MACOS_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"


def arch() -> str:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "arm64" if sys.platform == "darwin" else "aarch64"
    return m


def requirements() -> list[str]:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    return [*project["dependencies"], *project["optional-dependencies"]["build"]]


def macos_wheel_platforms() -> list[str]:
    machine = platform.machine()
    tags = []
    for archname in (machine, "universal2"):
        tags += [f"macosx_{MACOS_MIN[0]}_0_{archname}", f"macosx_11_0_{archname}"]
        tags += [f"macosx_10_{n}_{archname}" for n in range(16, 5, -1)]
    return tags


def reexec_in_macos_venv() -> None:
    """Switches to a venv of python.org's Python holding only wheels that run on the oldest supported macOS."""
    if sys.platform != "darwin" or os.environ.get("CQTA_BUILD_VENV"):
        return
    python = os.environ.get("CQTA_MACOS_PYTHON", MACOS_PYTHON)
    if not Path(python).exists():
        python = sys.executable
        print(f"note: {MACOS_PYTHON} not found, using {python}")
    venv = BUILD / "venv-macos"
    wheels = BUILD / "wheelhouse"
    shutil.rmtree(wheels, ignore_errors=True)
    wheels.mkdir(parents=True, exist_ok=True)
    reqs = BUILD / "requirements.txt"
    # pytap3 is pure Python but only published as an sdist, which a platform-restricted download cannot fetch
    binary = [r for r in requirements() if not r.lower().startswith("pytap3")]
    reqs.write_text("\n".join(binary) + "\n")
    if not (venv / "bin" / "python").exists():
        run([python, "-m", "venv", venv])
    # Restricting the platform tags makes pip pick wheels built for old macOS releases instead of the host's
    run([python, "-m", "pip", "download", "--only-binary=:all:", "--python-version", "3.13", "-r", reqs, "-d", wheels,
         *[a for t in macos_wheel_platforms() for a in ("--platform", t)]])
    run([venv / "bin" / "python", "-m", "pip", "install", "--no-index", "--find-links", wheels,
         "--upgrade", "-r", reqs])
    run([venv / "bin" / "python", "-m", "pip", "install", "--no-deps", "pytap3>=3.1.2"])
    os.execve(venv / "bin" / "python", [str(venv / "bin" / "python"), *sys.argv],
              dict(os.environ, CQTA_BUILD_VENV="1"))


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def check_macos_floor(app: Path) -> None:
    """Fails if a bundled Mach-O binary needs a newer macOS than MACOS_MIN."""
    import re

    too_new = []
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        out = subprocess.run(["vtool", "-show-build", str(path)], capture_output=True, text=True).stdout
        for minos in re.findall(r"minos (\d+(?:\.\d+)*)|version (\d+(?:\.\d+)*)\n\s+sdk", out):
            value = version_tuple(next(v for v in minos if v))
            if value > MACOS_MIN:
                too_new.append((value, path.relative_to(app)))
    if too_new:
        for value, path in sorted(too_new)[-15:]:
            print(f"  requires macOS {'.'.join(map(str, value))}: {path}")
        sys.exit(f"{len(too_new)} binaries need macOS newer than {'.'.join(map(str, MACOS_MIN))}")
    print(f"All bundled binaries run on macOS {'.'.join(map(str, MACOS_MIN))} or later")


def notary_credentials() -> list[str] | None:
    """notarytool credential arguments, or None when notarization cannot be done."""
    env = os.environ
    if env.get("APPLE_ID") and env.get("APPLE_TEAM_ID") and env.get("APPLE_APP_PASSWORD"):
        return ["--apple-id", env["APPLE_ID"], "--team-id", env["APPLE_TEAM_ID"], "--password", env["APPLE_APP_PASSWORD"]]
    profile = env.get("CQTA_NOTARY_PROFILE", DEFAULT_NOTARY_PROFILE)
    probe = subprocess.run(["xcrun", "notarytool", "history", "--keychain-profile", profile],
                           capture_output=True, text=True)
    return ["--keychain-profile", profile] if probe.returncode == 0 else None


def notarize(path: Path, credentials: list[str]) -> None:
    """Submits a .app (zipped) or .dmg to Apple and waits; fails the build if it is rejected."""
    submit = path
    if path.suffix == ".app":
        submit = BUILD / "notarize.zip"
        submit.unlink(missing_ok=True)
        run(["ditto", "-c", "-k", "--keepParent", path, submit])
    run(["xcrun", "notarytool", "submit", submit, *credentials, "--wait"])
    run(["xcrun", "stapler", "staple", path])


def signing_identity() -> tuple[str, bool]:
    """Returns (identity, is_developer_id): the best certificate available on this Mac, or "-" for ad-hoc."""
    import re

    forced = os.environ.get("CQTA_CODESIGN_IDENTITY")
    if forced:
        return forced, "Developer ID Application" in forced
    out = subprocess.run(["security", "find-identity", "-p", "codesigning"], capture_output=True, text=True).stdout
    found = []  # (hash, name, valid)
    valid_section = False
    for line in out.splitlines():
        if "Valid identities only" in line:
            valid_section = True
            continue
        m = re.match(r'\s*\d+\) ([0-9A-F]{40}) "(.*)"', line)
        if m:
            found.append((m.group(1), m.group(2), valid_section))
    valid = [(h, n) for h, n, v in found if v]
    for prefix in ("Developer ID Application", "Apple Development", "Mac Developer"):
        for h, n in valid:
            if n.startswith(prefix):
                return h, prefix == "Developer ID Application"
    if valid:
        return valid[0][0], False
    if found:  # e.g. a self-signed certificate that is not trusted
        return found[0][0], False
    return "-", False


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


def linux_container_build() -> None:
    """Runs the PyInstaller step inside manylinux_2_28 so the bundled libraries only need glibc 2.28."""
    image = MANYLINUX_IMAGE.format(arch="aarch64" if arch() in ("arm64", "aarch64") else arch())
    (BUILD / "requirements.txt").write_text("\n".join(requirements()) + "\n")
    script = r"""
set -e
for p in portaudio mesa-libEGL mesa-libGL libxkbcommon libxkbcommon-x11 fontconfig freetype dbus-libs libX11 \
         libxcb xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil xcb-util-cursor; do
  dnf install -y "$p" >/dev/null 2>&1 || echo "note: $p not installed"
done
# manylinux's own Pythons are static builds, which PyInstaller cannot bundle; uv's managed Python has libpython
/opt/python/cp313-cp313/bin/pip install uv
/opt/python/cp313-cp313/bin/uv venv --python 3.13 --managed-python /tmp/venv
/opt/python/cp313-cp313/bin/uv pip install --python /tmp/venv/bin/python -r /src/build/requirements.txt
CQTA_IN_CONTAINER=1 /tmp/venv/bin/python /src/packaging/build.py
chown -R "$HOST_UID:$HOST_GID" /src/build /src/dist
"""
    target = "linux/arm64" if image.endswith("aarch64") else "linux/amd64"
    run(["docker", "run", "--rm", "--platform", target, "-v", f"{ROOT}:/src", "-w", "/src", "-e", f"HOST_UID={os.getuid()}",
         "-e", f"HOST_GID={os.getgid()}", image, "bash", "-c", script])


def build_linux(icons: Path) -> Path:
    name = f"cqta-{__version__}-linux-{arch()}"
    if os.environ.get("CQTA_NO_CONTAINER"):
        pyinstaller("cqta", ["--onedir", "--windowed"])
    else:
        shutil.rmtree(DIST / "pyi", ignore_errors=True)
        linux_container_build()
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
        "--windowed", "--onedir", "--icon", icons / "cqta.icns", "--osx-bundle-identifier", APP_ID,
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
        "LSMinimumSystemVersion": ".".join(map(str, MACOS_MIN)),
    })
    plist.write_bytes(plistlib.dumps(info))
    check_macos_floor(app)
    identity, developer_id = signing_identity()
    print(f"Signing with {identity if identity == '-' else identity + (' (Developer ID)' if developer_id else '')}")
    sign = ["codesign", "--force", "--deep", "--timestamp" if developer_id else "--timestamp=none",
            "--options", "runtime", "--entitlements", entitlements, "--sign", identity]
    run([*sign, app])
    run(["codesign", "--verify", "--deep", "--strict", app])
    credentials = notary_credentials() if developer_id else None
    if credentials:
        notarize(app, credentials)
    else:
        why = "no Developer ID Application certificate" if not developer_id else \
            f"no notarytool credentials (store a keychain profile named {DEFAULT_NOTARY_PROFILE} or set CQTA_NOTARY_PROFILE)"
        print(f"warning: not notarizing ({why}); Gatekeeper will block the app on other Macs")
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / "CQta.app", symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    out = DIST / f"{name}.dmg"
    out.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", f"CQta {__version__}", "-srcfolder", stage, "-ov", "-format", "UDZO", out])
    if identity != "-":
        run(["codesign", "--force", "--timestamp" if developer_id else "--timestamp=none", "--sign", identity, out])
    if credentials:
        notarize(out, credentials)
        run(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "-v", out])
    return out


def main() -> None:
    DIST.mkdir(exist_ok=True)
    if os.environ.get("CQTA_IN_CONTAINER"):
        pyinstaller("cqta", ["--onedir", "--windowed"])
        return
    reexec_in_macos_venv()
    icons = make_icons()
    builder = {"win32": build_windows, "linux": build_linux, "darwin": build_macos}.get(sys.platform)
    if builder is None:
        sys.exit(f"Unsupported platform {sys.platform}")
    out = builder(icons)
    print(f"\nBuilt {out}")


if __name__ == "__main__":
    main()
