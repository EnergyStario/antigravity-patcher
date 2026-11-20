"""Single-source loader for canonical extensions/core/hosts_pin.py."""

from __future__ import annotations
import sys
from pathlib import Path


def _resolve_core_hosts_pin() -> Path | None:
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "extensions" / "core" / "hosts_pin.py")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "extensions" / "core" / "hosts_pin.py")
    candidates.append(Path.cwd() / "extensions" / "core" / "hosts_pin.py")
    for p in candidates:
        if p.is_file():
            return p
    return None


_core_path = _resolve_core_hosts_pin()
if _core_path is None:
    raise FileNotFoundError("Canonical extensions/core/hosts_pin.py not found")

_code = compile(_core_path.read_text(encoding="utf-8"), str(_core_path), "exec")
exec(_code, globals())

