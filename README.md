# CQta for desktop

Cross-platform (Windows, macOS, Linux) CQta desktop app and reference implementation: amateur-radio style chat (QSOs,
CQ calls, broadcast), ping and TFTP file transfer over an IP-over-radio link. The canonical [protocol specification](PROTOCOL.md)
is maintained here.

Three ways to reach the radio network:

| Transport | Details |
|-----------|---------|
| **Meshtastic** | USB serial, Bluetooth LE or TCP. The Meshtastic protocol (framing, handshake, private port 33) is implemented in this package; the `meshtastic` Python package is not used. |
| **AFSK sound modem** | 1200 baud Bell-202 style AFSK over a sound card. Input and output audio devices are chosen explicitly. |
| **KISS TCP TNC** | Raw IPv4 in KISS frames over a TCP connection (for example Direwolf). |
| **Network interface** | Plain UDP over an already configured OS interface (Ethernet, Wi-Fi, VPN): chat goes to the interface's broadcast address on UDP 37373, file transfer is not offered (the OS handles TFTP). No elevated privileges or tunnel; ping is unavailable. |

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

## Command line (no GUI)

`cqta --tunnel` runs headless and keeps an OS tunnel up until Ctrl+C / SIGTERM, then tears it down. It **never asks for
administrator rights**: start it as root (`sudo cqta --tunnel`) or from an elevated terminal on Windows, otherwise it
says so and exits with an error.

```
sudo cqta --tunnel                       # the connection used last in the GUI
sudo cqta --tunnel --meshtastic-serial /dev/ttyUSB0
sudo cqta --tunnel --meshtastic-ble    # value left out: strongest Bluetooth node
sudo cqta --tunnel 44.130.1.5/16 --kiss 127.0.0.1:8001/0
sudo cqta --tunnel 10.115.4.7/16 --afsk --input-device "USB Audio CODEC" --output-device "USB Audio CODEC" --beacon-interval 120 --callsign MYCALL
cqta --list serial|ble|audio|interfaces  # discover device names
cqta --dump-config                       # print the command line equivalent to the GUI's current connection
cqta --help                              # every option, with ranges and defaults
```

The connection type is inferred from the options (`--meshtastic-*`, `--kiss`, or `--afsk` / the audio options; `--afsk` alone means AFSK with all defaults). The value of `--meshtastic-serial/-ble/-tcp` and `--kiss HOST:PORT/KISS-PORT` is optional: serial picks the last `COM*` (Windows) or the first `/dev/ttyACM*`, `/dev/ttyUSB*`, `/dev/tty.usb*`; ble scans and takes the strongest node; tcp uses `127.0.0.1:4403`; kiss uses `127.0.0.1:8001/0` (each part can be changed alone, e.g. `--kiss :9000`). Options apply to this session only, are not saved, and anything not given takes its default (never the
GUI's saved value). A bare `cqta --tunnel` is the only form that uses the saved settings, unchanged. `--callsign` and `--beacon-interval` must be given together (and only then); the station's IPv4 address is the argument of `--tunnel` (required for
`kiss` and `afsk`; with `meshtastic` it comes from the node). No beacons are sent unless `--beacon-interval` is given.
The Network interface mode has no tunnel and is not available here. On Windows the windowed
`cqta.exe` prints to the console it was started from.


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
  ad-hoc signature. With a Developer ID Application certificate the app and dmg are also notarized and stapled so
  Gatekeeper accepts them. Credentials come from `APPLE_ID` + `APPLE_TEAM_ID` + `APPLE_APP_PASSWORD`, or from a
  `notarytool` keychain profile (`xcrun notarytool store-credentials app-notary`; `CQTA_NOTARY_PROFILE` selects
  another name). With no Developer ID certificate or no credentials the build still succeeds, unnotarized.
- **Linux:** the PyInstaller step runs in a `manylinux_2_28` docker container, so the AppImage needs glibc 2.28 or
  newer (e.g. Debian 10, Ubuntu 18.10, RHEL 8). `CQTA_NO_CONTAINER=1` builds against the host instead. PySide6 only
  publishes x86_64 wheels for this baseline.
