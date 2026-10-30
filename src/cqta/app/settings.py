"""Persistent settings (JSON in the per-user config directory)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from ..core.protocol import DEFAULT_CALLSIGN
from ..transports.afsk import AFSKConfig
from ..transports.kiss import KissConfig
from ..transports.mesh import MeshConfig
from ..transports.network import NetworkConfig


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "cqta"


@dataclass
class Settings:
    callsign: str = DEFAULT_CALLSIGN
    monitor: bool = False
    notify: dict[str, bool] = field(default_factory=dict)
    sound: bool = True
    beacon_enabled: bool = True
    beacon_interval: int = 120
    tftp_server: bool = False
    download_dir: str = ""
    file_target: str = ""  # last recipient typed in the file-send box
    transport: str = "meshtastic"  # meshtastic | afsk | kiss | network
    mesh: MeshConfig = field(default_factory=MeshConfig)
    afsk: AFSKConfig = field(default_factory=AFSKConfig)
    kiss: KissConfig = field(default_factory=KissConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    system_interface: bool = False  # create an OS network interface (needs administrator rights)

    @staticmethod
    def path() -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or cls.path()
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return cls()
        return _from_dict(cls, data) if isinstance(data, dict) else cls()

    def save(self, path: Path | None = None) -> None:
        path = path or self.path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
            tmp.replace(path)
        except OSError:
            pass


def _from_dict(cls: type, data: dict[str, Any]):
    """Tolerant loader: unknown keys and wrongly typed values fall back to defaults."""
    obj = cls()
    for f in fields(cls):
        if f.name not in data:
            continue
        current = getattr(obj, f.name)
        value = data[f.name]
        if is_dataclass(current):
            if isinstance(value, dict):
                setattr(obj, f.name, _from_dict(type(current), value))
        elif isinstance(value, type(current)) and not (isinstance(current, int) and isinstance(value, bool)
                                                       and not isinstance(current, bool)):
            setattr(obj, f.name, value)
        elif isinstance(current, float) and isinstance(value, int) and not isinstance(value, bool):
            setattr(obj, f.name, float(value))
    return obj
