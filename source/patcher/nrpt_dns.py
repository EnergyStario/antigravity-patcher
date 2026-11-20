"""Single-source loader for canonical extensions/core/nrpt_dns.py."""

from __future__ import annotations

import ipaddress  # noqa: F401 - ensure PyInstaller bundles ipaddress for frozen builds
import socket     # noqa: F401 - ensure PyInstaller bundles socket for frozen builds
import sys
from pathlib import Path

if sys.platform == "win32":
    import winreg  # noqa: F401 - ensure PyInstaller freezes winreg for bundled core/nrpt_dns.py


def _resolve_core_nrpt() -> Path | None:
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "extensions" / "core" / "nrpt_dns.py")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "extensions" / "core" / "nrpt_dns.py")
    candidates.append(Path.cwd() / "extensions" / "core" / "nrpt_dns.py")
    for p in candidates:
        if p.is_file():
            return p
    return None


_core_path = _resolve_core_nrpt()
if _core_path is None:
    raise FileNotFoundError("Canonical extensions/core/nrpt_dns.py not found")

_code = compile(_core_path.read_text(encoding="utf-8"), str(_core_path), "exec")
exec(_code, globals())
