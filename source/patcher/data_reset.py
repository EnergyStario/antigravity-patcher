"""
patcher/data_reset.py -- Safe targeted cache & registry cleanup (fix HTTP 429 / display / launch glitches).

Never deletes User/settings.json (preserves user settings, update.mode: none, and releaseBaseUrl)
and never deletes chat history (User/globalStorage, User/workspaceStorage).

Supported: Windows / macOS / Linux.
"""
from __future__ import annotations
import os
import sys
import shutil
import datetime
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from patcher.utils.console import (
    ok, warn, err, info, hint, step, cancel, color,
    print_menu_section, print_menu_row, print_menu_footer,
    COLOR_CYAN, COLOR_RED, COLOR_YELLOW, COLOR_BOLD, COLOR_DIM, COLOR_WHITE,
)

SAFE_CACHE_DIRS = (
    "CachedData",
    "Code Cache",
    "GPUCache",
    "Cache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
)

_ANTIGRAVITY_EXE_NAMES = {
    "antigravity ide.exe",
    "antigravity.exe",
    "language_server.exe",
    "agy.exe",
    "inno_updater.exe",
}

_BROKEN_COMPAT_FLAGS = {
    "640X480",
    "256COLOR",
    "HIGHDPIAWARE",
    "DPIUNAWARE",
    "GDIDPISCALING",
}

_COMPAT_LAYERS_SUBKEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
_IFEO_SUBKEYS = (
    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows NT\CurrentVersion\Image File Execution Options",
)


def _get_data_dirs() -> List[Tuple[str, Path]]:
    """Return [(display_name, path)] for all found Antigravity data folders."""
    pairs: List[Tuple[str, Path]] = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            pairs = [
                ("Antigravity IDE", Path(appdata) / "Antigravity IDE"),
                ("Antigravity 2.0", Path(appdata) / "Antigravity"),
            ]
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        pairs = [
            ("Antigravity IDE", base / "Antigravity IDE"),
            ("Antigravity 2.0", base / "Antigravity"),
        ]
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        pairs = [
            ("Antigravity IDE", cfg / "Antigravity IDE"),
            ("Antigravity 2.0", cfg / "Antigravity"),
        ]
    return [(n, p) for n, p in pairs if p.is_dir()]


def _backup_dest(name: str) -> Path:
    if os.name == "nt":
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    else:
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            desktop = Path.home()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return desktop / f"{name.replace(chr(32), chr(45))}-backup-{ts}"


_PROC_MAP = {
    "Antigravity IDE": ["Antigravity IDE.exe", "antigravity-ide"],
    "Antigravity 2.0": ["Antigravity.exe", "antigravity", "language_server.exe", "language_server"],
}

_PROC_NAMES = [
    "Antigravity IDE.exe", "antigravity-ide",
    "Antigravity.exe",     "antigravity",
    "language_server.exe", "language_server",
]


def _is_ide_running(target_name: Optional[str] = None) -> Optional[str]:
    procs = _PROC_MAP.get(target_name or "", _PROC_NAMES)
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="ignore",
                timeout=5, creationflags=0x08000000,
            ).stdout
            running_images = set()
            for row in out.splitlines():
                row = row.strip()
                if not row:
                    continue
                img = row.split(",", 1)[0].strip().strip('"').lower()
                if img:
                    running_images.add(img)
            for name in procs:
                if name.lower() in running_images:
                    return name
        else:
            for name in procs:
                if subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=3).returncode == 0:
                    return name
    except Exception:
        pass
    return None


def _kill_ide(proc_name: str) -> bool:
    try:
        if os.name == "nt":
            res = subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                capture_output=True, text=True, errors="ignore",
                timeout=5, creationflags=0x08000000,
            )
            return res.returncode == 0
        else:
            res = subprocess.run(["pkill", "-f", proc_name], capture_output=True, timeout=5)
            return res.returncode == 0
    except Exception:
        return False


_CHAT_DIRS = ["User/globalStorage", "User/workspaceStorage"]


def _restore_chats(backup: Path, fresh: Path) -> bool:
    """Copy only chat folders from backup. Never copies settings.json."""
    success = True
    for rel in _CHAT_DIRS:
        src = backup / rel
        dst = fresh / rel
        if not src.is_dir():
            step(f"  {rel}", None, "not in backup - skipping")
            continue
        try:
            if dst.exists():
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst)
            n = sum(1 for f in dst.rglob("*") if f.is_file())
            step(f"  {rel}", True, f"restored ({n} files)")
        except Exception as ex:
            err(f"  Could not restore {rel}: {ex}")
            success = False
    if success:
        try:
            from patcher.conversations.rebuilder import rebuild_conversations_index
            step("  Index synchronization", True, "rebuilding conversation index & trajectory summaries...")
            rebuild_conversations_index(auto_workspaces=True)
            step("  Index status", True, "conversations chronologically indexed and attached")
        except Exception:
            pass
    return success


def _dir_size_str(path: Path) -> str:
    try:
        total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return f"{total // (1024*1024)} MB" if total >= 1024*1024 else f"{total // 1024} KB"
    except Exception:
        return "unknown size"


def _choose_app(available: List[Tuple[str, Path]],
                preferred_name: Optional[str]) -> Tuple[Optional[str], Optional[Path]]:
    if len(available) == 1:
        return available[0]
    if preferred_name:
        for n, p in available:
            if preferred_name.lower() in n.lower():
                return n, p
    print(color("  Select application to reset:", COLOR_WHITE, COLOR_BOLD))
    for i, (n, p) in enumerate(available, 1):
        print_menu_row(str(i), n, _dir_size_str(p), COLOR_CYAN)
    print_menu_row("0", "Cancel", "", COLOR_DIM)
    print_menu_footer()
    print()
    sel = input(color("  Select > ", COLOR_CYAN, COLOR_BOLD)).strip().lower()
    if sel in ("0", "", "b", "q", "quit", "exit", "back"):
        return None, None
    try:
        idx = int(sel) - 1
        if 0 <= idx < len(available):
            return available[idx]
    except ValueError:
        pass
    return None, None


def _is_antigravity_compat_entry(value_name: str) -> bool:
    raw = str(value_name or "").strip()
    if not raw:
        return False
    base = os.path.basename(raw.replace("\\", "/")).lower()
    if base in _ANTIGRAVITY_EXE_NAMES:
        return True
    low = raw.lower()
    return "antigravity" in low and low.endswith(".exe")


def _delete_reg_tree(winreg_mod, hkey, subkey_path: str, access_flags: int) -> None:
    """Recursively delete a Windows registry subkey."""
    with winreg_mod.OpenKey(hkey, subkey_path, 0, access_flags | winreg_mod.KEY_READ | winreg_mod.KEY_SET_VALUE) as sk:
        sub_count = winreg_mod.QueryInfoKey(sk)[0]
        children = [winreg_mod.EnumKey(sk, i) for i in range(sub_count)]
    for child in children:
        _delete_reg_tree(winreg_mod, hkey, f"{subkey_path}\\{child}", access_flags)
    winreg_mod.DeleteKeyEx(hkey, subkey_path, access_flags, 0) if hasattr(winreg_mod, "DeleteKeyEx") else winreg_mod.DeleteKey(hkey, subkey_path)


def clean_compat_and_ifeo_registry() -> Dict[str, List[str]]:
    """
    Remove broken Windows compatibility flags (640X480, 256COLOR, HIGHDPIAWARE, etc.)
    from HKCU\\...\\AppCompatFlags\\Layers and stale Image File Execution Options
    entries for Antigravity executables.
    """
    removed_compat: List[str] = []
    removed_ifeo: List[str] = []
    if sys.platform != "win32":
        return {"compat_removed": removed_compat, "ifeo_removed": removed_ifeo}

    try:
        import winreg
    except ImportError:
        return {"compat_removed": removed_compat, "ifeo_removed": removed_ifeo}

    # 1. HKCU (and best-effort HKLM) AppCompatFlags\Layers
    for root_hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                root_hive,
                _COMPAT_LAYERS_SUBKEY,
                0,
                winreg.KEY_READ | winreg.KEY_SET_VALUE,
            ) as k:
                val_count = winreg.QueryInfoKey(k)[1]
                entries: List[Tuple[str, object, int]] = []
                for idx in range(val_count):
                    try:
                        vname, vdata, vtype = winreg.EnumValue(k, idx)
                        entries.append((vname, vdata, vtype))
                    except OSError:
                        continue
                for vname, vdata, vtype in entries:
                    if not _is_antigravity_compat_entry(vname):
                        continue
                    tokens = str(vdata or "").split()
                    if not any(tok.upper() in _BROKEN_COMPAT_FLAGS for tok in tokens):
                        continue
                    remaining = [tok for tok in tokens if tok.upper() not in _BROKEN_COMPAT_FLAGS]
                    try:
                        if not remaining or remaining == ["~"]:
                            winreg.DeleteValue(k, vname)
                        else:
                            winreg.SetValueEx(k, vname, 0, vtype, " ".join(remaining))
                        removed_compat.append(vname)
                    except OSError:
                        pass
        except OSError:
            continue

    # 2. Stale Image File Execution Options (IFEO) entries for Antigravity executables
    ifeo_exes = ("Antigravity IDE.exe", "Antigravity.exe", "language_server.exe", "agy.exe")
    wow_flag = getattr(winreg, "KEY_WOW64_64KEY", 0)
    for ifeo_root in _IFEO_SUBKEYS:
        for exe in ifeo_exes:
            full_subkey = f"{ifeo_root}\\{exe}"
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    full_subkey,
                    0,
                    winreg.KEY_READ | winreg.KEY_SET_VALUE | wow_flag,
                ) as sub:
                    num_sub, num_vals, _ = winreg.QueryInfoKey(sub)
                    has_debugger = False
                    for idx in range(num_vals):
                        try:
                            vn, _, _ = winreg.EnumValue(sub, idx)
                            if str(vn).lower() == "debugger":
                                has_debugger = True
                                break
                        except OSError:
                            break
                    is_stale = has_debugger or num_vals > 0 or num_sub > 0
                if not is_stale:
                    continue
                try:
                    _delete_reg_tree(winreg, winreg.HKEY_LOCAL_MACHINE, full_subkey, wow_flag)
                    removed_ifeo.append(full_subkey)
                except OSError:
                    if has_debugger:
                        try:
                            with winreg.OpenKey(
                                winreg.HKEY_LOCAL_MACHINE,
                                full_subkey,
                                0,
                                winreg.KEY_SET_VALUE | wow_flag,
                            ) as sub_w:
                                winreg.DeleteValue(sub_w, "Debugger")
                                removed_ifeo.append(f"{full_subkey}\\Debugger")
                        except OSError:
                            pass
            except OSError:
                continue

    return {"compat_removed": removed_compat, "ifeo_removed": removed_ifeo}


def clean_target_caches(target_path: Path) -> List[str]:
    """
    Safely delete disposable Chromium/Electron cache directories (CachedData, Code Cache, GPUCache, etc.)
    inside target_path without ever touching User/settings.json, update locks, or chat storage.
    """
    cleaned: List[str] = []
    if not target_path.is_dir():
        return cleaned

    for cache_name in SAFE_CACHE_DIRS:
        cache_dir = target_path / cache_name
        if cache_dir.is_dir():
            try:
                shutil.rmtree(cache_dir, ignore_errors=False)
                cleaned.append(cache_name)
            except Exception:
                shutil.rmtree(cache_dir, ignore_errors=True)
                if not cache_dir.exists():
                    cleaned.append(cache_name)
    return cleaned


def _resolve_reset_targets(
    available: List[Tuple[str, Path]],
    app_name: Optional[str],
    auto_confirm: bool,
) -> List[Tuple[str, Path]]:
    """Resolve one or more reset targets without prompting when auto_confirm is True."""
    if app_name:
        low = app_name.strip().lower()
        if low in ("all", "*"):
            return list(available)
        matched = [(n, p) for n, p in available if low in n.lower()]
        if matched:
            return matched if auto_confirm else [matched[0]]
    if auto_confirm:
        return list(available)
    chosen_name, chosen_path = _choose_app(available, app_name)
    if chosen_name is None or chosen_path is None:
        return []
    return [(chosen_name, chosen_path)]


def _reset_single_target(
    target_name: str,
    target_path: Path,
    auto_confirm: bool,
) -> Optional[bool]:
    """Terminate running target if needed and wipe disposable caches safely."""
    print()
    info(f"Target : {color(str(target_path), COLOR_CYAN)}")
    info(f"Size   : {color(_dir_size_str(target_path), COLOR_WHITE)}")
    print()

    running = _is_ide_running(target_name)
    if running:
        warn(f"Process '{running}' is currently running!")
        if auto_confirm:
            info(f"Auto-closing '{running}' before safe cache cleanup...")
            if _kill_ide(running):
                ok(f"Process '{running}' terminated.")
                import time
                time.sleep(1.5)
            else:
                warn(f"Could not terminate '{running}' cleanly; cleaning unlocked caches.")
        else:
            warn(f"You should close {target_name} before cleaning caches.")
            print()
            choice = input(color(f"  Kill '{running}' now? [y/N] > ", COLOR_YELLOW)).strip().lower()
            if choice == "y":
                if _kill_ide(running):
                    ok(f"Process '{running}' terminated.")
                    import time
                    time.sleep(2)
                else:
                    err("Could not kill the process. Please close it manually.")
                    return None
            else:
                cancel(f"Please close {target_name} and try again.")
                return False

    if not auto_confirm:
        print()
        info("This will clean ONLY temporary caches (CachedData, Code Cache, GPUCache)")
        info("and broken AppCompatFlags/IFEO registry entries. settings.json & chats stay intact.")
        print()
        confirm = input(color(f"  Confirm safe cleanup for '{target_name}'? [y/N] > ", COLOR_YELLOW, COLOR_BOLD)).strip().lower()
        if confirm != "y":
            cancel("Cancelled -- no changes made.")
            return False

    print()
    cleaned_caches = clean_target_caches(target_path)
    if cleaned_caches:
        step(f"Cleaned caches ({target_name})", True, ", ".join(cleaned_caches))
    else:
        step(f"Caches ({target_name})", None, "already clean")

    settings_file = target_path / "User" / "settings.json"
    if settings_file.is_file():
        step("User/settings.json", True, "preserved (update lock & settings intact)")
    return True


def do_data_reset(
    app_name: Optional[str] = None,
    auto_confirm: bool = False,
) -> Optional[bool]:
    """
    Safe targeted cache & registry cleanup (fix GPU/render/cache glitches & 429).
    Preserves User/settings.json (update.mode: none), accounts, and all chat history.
    app_name: "Antigravity IDE" | "Antigravity 2.0" | "all" | None.
    auto_confirm: when True, runs zero-prompt autopilot cleanup across selected/all targets.
    Returns True on completed cleanup, False when cancelled by user, None on error.
    """
    print_menu_section("SAFE CACHE & REGISTRY CLEANUP  --  PRESERVE SETTINGS")
    print()
    info("Performs safe targeted cache cleanup (CachedData, Code Cache, GPUCache)")
    info("and cleans broken Windows compatibility/IFEO registry flags.")
    info("User/settings.json, update lock, accounts and chats are 100% PRESERVED.")
    print()

    available = _get_data_dirs()
    if not available:
        err("No Antigravity IDE / Antigravity 2.0 data folders found.")
        if os.name == "nt":
            hint(r"  Expected: %APPDATA%\Antigravity IDE  or  %APPDATA%\Antigravity")
        return None

    targets = _resolve_reset_targets(available, app_name, auto_confirm=auto_confirm)
    if not targets:
        cancel("Cancelled.")
        return False

    did_any = False
    for target_name, target_path in targets:
        res = _reset_single_target(target_name, target_path, auto_confirm=auto_confirm)
        if res is False:
            return False
        if res is None and not auto_confirm:
            return None
        if res is True:
            did_any = True

    reg_res = clean_compat_and_ifeo_registry()
    if reg_res["compat_removed"]:
        step("Cleaned AppCompatFlags", True, f"{len(reg_res['compat_removed'])} entry(s)")
    if reg_res["ifeo_removed"]:
        step("Cleaned stale IFEO", True, f"{len(reg_res['ifeo_removed'])} entry(s)")

    print()
    if did_any:
        ok("Safe cache & registry cleanup complete.")
    hint("Tip: for HTTP 429 quota limits, switch accounts in the Antigravity status bar / Assistant.")
    return True if did_any else None
