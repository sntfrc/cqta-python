# CQta for desktop

Cross-platform (Windows, macOS, Linux) Python port of the CQta iOS app: amateur-radio style chat (QSOs, CQ calls,
broadcast), ping and TFTP file transfer over an IP-over-radio link. See the iOS repository's `PROTOCOL.md` for the
wire protocol.

Three ways to reach the radio network:

| Transport | Details |
|-----------|---------|
| **Meshtastic** | USB serial, Bluetooth LE or TCP. The Meshtastic protocol (framing, handshake, private port 33) is implemented in this package; the `meshtastic` Python package is not used. |
| **AFSK sound modem** | 1200 baud Bell-202 style AFSK over a sound card. Input and output audio devices are chosen explicitly. |
| **KISS TCP TNC** | Raw IPv4 in KISS frames over a TCP connection (for example Direwolf). |
| **Network interface** | Plain UDP over an already configured OS interface (Ethernet, Wi-Fi, VPN): chat goes to the interface's broadcast address on UDP 37373, file transfers use UDP 69. No elevated privileges or tunnel; ping is unavailable. |

## Privileges

CQta runs as a regular user. Chat, ping and file transfer build and parse IP packets inside the app and need no
special rights. Only if you tick *System network interface* is a small helper process started with elevated rights
(UAC / `osascript` admin prompt / `pkexec`) to create the TUN/TAP device through
[pytap3](https://github.com/sntfrc/pytap3); the GUI itself never runs elevated.

## Run

```
pip install -e .
cqta            # or: python -m cqta
```

Layout: broadcast panel on top; *Last heard* list and one tab per QSO below; files in a separate window
(Tools menu). Settings are stored in `%APPDATA%\cqta`, `~/Library/Application Support/cqta` or `~/.config/cqta`.
Received files go to `~/Documents/CQta` by default.

## Tests

```
pip install -e .[dev]
pytest
```

## Packaged builds

```
pip install -e .[build]
python packaging/build.py
```

Produces `dist/cqta-<version>-<platform>-<architecture>` as a single-file `.exe` (Windows), `.AppImage` (Linux,
needs `appimagetool` and docker) or a signed `.app` in a `.dmg` (macOS). The GitHub workflow builds all platforms.

- **macOS:** the build makes its own venv from python.org's Python 3.13 (`CQTA_MACOS_PYTHON` overrides the path) with
  only wheels built for macOS 12 or older, so the app runs on macOS 12+; the build fails if any bundled binary needs
  something newer. The app is signed with the first of: `CQTA_CODESIGN_IDENTITY`, a Developer ID Application
  certificate, another valid certificate (Apple Development / personal team), a self-signed certificate, or an
  ad-hoc signature. `CQTA_NOTARY_PROFILE` notarizes and staples (Developer ID only).
- **Linux:** the PyInstaller step runs in a `manylinux_2_28` docker container, so the AppImage needs glibc 2.28 or
  newer (e.g. Debian 10, Ubuntu 18.10, RHEL 8). `CQTA_NO_CONTAINER=1` builds against the host instead. PySide6 only
  publishes x86_64 wheels for this baseline.
