# CQta - IP-over-radio tunneling with QSO features

CQta is a **cross-platform** (Windows, Linux, macOS) tunneling app that routes
IPv4 over a [Meshtastic](https://meshtastic.org) radio connected via BLE, AFSK
audio modem or an existing KISS TNC.

It creates a network interface with a real IP address, through which a real IP
subnet is routed. This is obtained by using
[pytap3](https://github.com/sntfrc/pytap3) package.

Some basic IPv4 filtering blocks IGMP, SSDP/mDNS and other OS chatter.

It also contains an implementation of a basic, human-readable UDP protocol
called CQta, designed for this app and whose canonical [protocol
specification](PROTOCOL.md) is also maintained here. It is fun for testing that
the link works, having QSOs over the link without additional software and small
TFTP file transfers. It interoperates with the mobile CQta apps. Of course, the
real usefulness is in using your existing IP applications over the tunnel. But
be aware that TCP is a very chatty protocol and, at its default settings, not
really efficient for radio; UDP stuff will work better.

Four ways to reach the radio network:

| Transport | Details |
|-----------|---------|
| **Meshtastic** | USB serial, Bluetooth LE or TCP |
| **AFSK sound modem** | 1200 baud Bell-202 style AFSK over a sound card |
| **KISS TCP TNC** | Raw IPv4 in KISS frames over an already running TCP KISS TNC such as Dire Wolf |
| **Network interface** | Plain UDP over an already configured OS interface. Useful if the underlying interface is already radio, like with HSMM links or similar devices |

## Privileges

CQta runs as a regular user. Chat, ping and file transfer build and parse IP
packets inside the app and need no special rights. Only if you tick *System
network interface* is a small helper process started with elevated rights to
create the TUN/TAP device through [pytap3](https://github.com/sntfrc/pytap3);
the GUI itself never runs elevated.

## Run the GUI application

```
pip install cqta
cqta               # or: python -m cqta
```

## Starting the tunnel interface from command line (no GUI)

`cqta --tunnel` runs headless and keeps an OS tunnel up until Ctrl+C / SIGTERM,
then tears it down. It **never asks for administrator rights**: start it as root
(`sudo cqta --tunnel`) or from an elevated terminal on Windows, otherwise it
says so and exits with an error.

It is useful for headless systems and for when you want to bring up the tunnel
automatically via systemd and similar.

Use `cqta --help` to see the full syntax; here are some examples.

```
sudo cqta --tunnel                       # the connection used last in the GUI
sudo cqta --tunnel --meshtastic-serial /dev/ttyUSB0
sudo cqta --tunnel --meshtastic-ble    # value left out: strongest Bluetooth node
sudo cqta --tunnel 44.130.1.5/16 --kiss 127.0.0.1:8001/0
sudo cqta --tunnel 10.115.4.7/16 --afsk --input-device "USB Audio Device" --output-device "USB Audio Device" --beacon-interval 120 --callsign MYCALL
cqta --list serial|ble|audio|interfaces  # discover device names
cqta --dump-config                       # print the command line equivalent to the GUI's current connection
cqta --help                              # every option, with ranges and defaults
```

An useful workflow would be to use the GUI to configure your transport, check
that it works, then use `--dump-config` to see the command line you have to use
to run exactly like that in an automated script.

The beacon is useful even if you don't use the GUI, because it will send out an
UDP packet containing your cleartext callsign every interval, thus satisfying
ham radio laws periodic identification requirements.

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

Produces `dist/cqta-<version>-<platform>-<architecture>` as a single-file `.exe`
(Windows), `.AppImage` (Linux, needs `appimagetool` and docker) or a signed
`.app` in a `.dmg` (macOS).

- **Linux:** the PyInstaller step runs in a `manylinux_2_28` docker container,
  so the AppImage needs glibc 2.28 or newer (e.g. Debian 10, Ubuntu 18.10, RHEL
  8). `CQTA_NO_CONTAINER=1` builds against the host instead. PySide6 only
  publishes x86_64 wheels for this baseline.

- **macOS:** the build makes its own venv from python.org's Python 3.13
  (`CQTA_MACOS_PYTHON` overrides the path) with only wheels built for macOS 12
  or older, so the app runs on macOS 12+; the build fails if any bundled binary
  needs something newer. The app is signed with the first of:
  `CQTA_CODESIGN_IDENTITY`, a Developer ID Application certificate, another
  valid certificate (Apple Development / personal team), a self-signed
  certificate, or an ad-hoc signature. With a Developer ID Application
  certificate the app and dmg are also notarized and stapled so Gatekeeper
  accepts them. Credentials come from `APPLE_ID` + `APPLE_TEAM_ID` +
  `APPLE_APP_PASSWORD`, or from a `notarytool` keychain profile (`xcrun
  notarytool store-credentials app-notary`; `CQTA_NOTARY_PROFILE` selects
  another name). With no Developer ID certificate or no credentials the build
  still succeeds, unnotarized.

- **Windows:** the executable runs on Windows 10 or better, and already bundles
  all required DLLs.