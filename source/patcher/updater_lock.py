"""
patcher/updater_lock.py -- Управление блокировкой авто-обновлений для ВСЕХ компонентов:
  1. Antigravity IDE (inno_updater.exe + update.mode: none + папки кэша обновлений)
  2. Antigravity 2.0 (resources/app-update.yml + папка antigravity-updater)
  3. VS Code Extension (settings.json -> extensions.autoUpdate: false)
"""
from __future__ import annotations
import os
import json
import subprocess
from pathlib import Path
from typing import Optional, List

from patcher.utils.console import ok, warn, err, info, hint, step, color, COLOR_GREEN, COLOR_RED, COLOR_YELLOW, COLOR_BOLD

UPDATER_EXE = "inno_updater.exe"
UPDATER_BAK = "inno_updater.exe.bak"
APP_UPDATE_YML = "app-update.yml"
APP_UPDATE_BAK = "app-update.yml.bak"

LOCK_STATE = Path(os.path.expanduser("~")) / ".gemini" / "ag_patcher_updlock.txt"

_IDE_LOCK_DIRS = [
    "{temp}\\antigravity-stable-user-x64",
    "{localappdata}\\Temp\\antigravity-stable-user-x64",
    "{localappdata}\\Temp\\antigravity-ide-updater",
    "{temp}\\Antigravity IDE Crashes",
    "{temp}\\antigravity-ide-updater",
]

_MANAGER_LOCK_DIRS = [
    "{localappdata}\\antigravity-updater",
    "{temp}\\antigravity-updater",
    "{localappdata}\\Temp\\antigravity-updater",
]

_CLI_LOCK_DIRS = [
    "{userprofile}\\.gemini\\bin\\.new",
]

_WIN_LOCK_DIRS = _IDE_LOCK_DIRS + _MANAGER_LOCK_DIRS + _CLI_LOCK_DIRS


def _env() -> dict:
    return {
        "localappdata": os.environ.get("LOCALAPPDATA", ""),
        "temp": os.environ.get("TEMP", ""),
        "appdata": os.environ.get("APPDATA", ""),
        "userprofile": os.environ.get("USERPROFILE", str(Path.home())),
    }


def _resolve_lock_dirs(templates: List[str]) -> List[Path]:
    e = _env()
    seen: set[str] = set()
    out: List[Path] = []
    for t in templates:
        if "{temp}" in t and not e["temp"]:
            continue
        if "{localappdata}" in t and not e["localappdata"]:
            continue
        if "{userprofile}" in t and not e["userprofile"]:
            continue
        raw = Path(t.format(**e))
        try:
            resolved = raw.resolve()
        except OSError:
            resolved = Path(os.path.abspath(str(raw)))
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return out


def _ide_lock_dirs() -> List[Path]:
    return _resolve_lock_dirs(_IDE_LOCK_DIRS)


def _manager_lock_dirs() -> List[Path]:
    return _resolve_lock_dirs(_MANAGER_LOCK_DIRS)


def _cli_lock_dirs() -> List[Path]:
    return _resolve_lock_dirs(_CLI_LOCK_DIRS)


def _lock_dirs() -> List[Path]:
    return _resolve_lock_dirs(_WIN_LOCK_DIRS)


def _kill_updater_processes() -> None:
    if os.name != "nt":
        return
    for proc in ["AntigravitySetup*.exe", "inno_updater.exe"]:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", proc],
                capture_output=True, text=True, errors="ignore",
                timeout=5, creationflags=0x08000000,
            )
        except Exception:
            pass


def _cleanup_ide_staging(ide_root: Optional[str] = None) -> None:
    import shutil
    cands = []
    if ide_root:
        cands.append(Path(ide_root) / "_")
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.append(Path(local) / "Programs" / "Antigravity IDE" / "_")
    for c in cands:
        if c.is_dir():
            try:
                shutil.rmtree(c, ignore_errors=True)
            except Exception:
                pass


def _cleanup_updater_dir_contents(path: Path) -> None:
    """Remove pre-downloaded updater payloads (installer.exe, pending/, tmp) before locking ACL."""
    import shutil
    if not path.is_dir():
        return
    try:
        # Ensure writable ACLs first if previously denied so cleanup can succeed
        _icacls(path, "reset")
    except Exception:
        pass
    try:
        for child in path.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def _update_agy_version_sidecar(lock: bool) -> None:
    """Keep ~/.gemini/bin/agy.version.json intact (size/mtime/ino) while locking/unlocking releaseBaseUrl."""
    sidecar = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".gemini" / "bin" / "agy.version.json"
    if not sidecar.is_file():
        return
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data["releaseBaseUrl"] = (
            "http://127.0.0.1:9"
            if lock
            else "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
        )
        sidecar.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _icacls(path: Path, action: str) -> bool:
    try:
        if action == "deny":
            if path.exists() and path.is_file():
                # Regular file (e.g. .gemini/bin/.new): deny write/delete without dir-inheritance flags
                cmd = ["icacls", str(path), "/deny", "*S-1-1-0:(W,D,WA,WEA)"]
            else:
                path.mkdir(parents=True, exist_ok=True)
                # Use universal SID *S-1-1-0 (Everyone/World) across all Windows languages
                cmd = ["icacls", str(path), "/deny", "*S-1-1-0:(OI)(CI)(W,D,AD,DC,WD)", "/T"]
        else:
            if not path.exists():
                return True
            cmd = ["icacls", str(path), "/reset"] if path.is_file() else ["icacls", str(path), "/reset", "/T"]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return True
    except Exception as ex:
        warn(f"icacls {action} failed for {path}: {ex}")
        return False


def _find_ide_updater(ide_root: Optional[str] = None) -> Optional[Path]:
    cands = []
    if ide_root:
        cands.append(Path(ide_root) / UPDATER_EXE)
        cands.append(Path(ide_root) / "tools" / UPDATER_EXE)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.append(Path(local) / "Programs" / "Antigravity IDE" / UPDATER_EXE)
        cands.append(Path(local) / "Programs" / "Antigravity IDE" / "tools" / UPDATER_EXE)
    for c in cands:
        if c.is_file():
            return c
        bak = c.parent / UPDATER_BAK
        if bak.is_file():
            return bak
    return None


def _find_manager_update_yml() -> Optional[Path]:
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        yml = Path(local) / "Programs" / "antigravity" / "resources" / APP_UPDATE_YML
        if yml.is_file():
            return yml
        bak = yml.parent / APP_UPDATE_BAK
        if bak.is_file():
            return bak
    return None


def _update_json_settings(path: Path, new_settings: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data.update(new_settings)
        path.write_text(json.dumps(data, indent=4), encoding="utf-8")
        return True
    except Exception as ex:
        warn(f"Could not update {path.name}: {ex}")
        return False


def _save_state(data: dict) -> None:
    any_locked = any(
        bool(data.get(k))
        for k in ("ide_locked", "manager_locked", "cli_locked", "vscode_locked")
    )
    if not any_locked:
        try:
            LOCK_STATE.unlink(missing_ok=True)
        except Exception:
            pass
        return
    LOCK_STATE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_STATE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_state() -> dict:
    if not LOCK_STATE.exists():
        return {}
    try:
        return json.loads(LOCK_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Статусы блокировки отдельных компонентов
# ---------------------------------------------------------------------------

def is_ide_locked(ide_root: Optional[str] = None) -> bool:
    ide_upd = _find_ide_updater(ide_root)
    if ide_upd and ide_upd.name == UPDATER_EXE:
        return False
    appdata = os.environ.get("APPDATA", "")
    settings_locked = False
    if appdata:
        p = Path(appdata) / "Antigravity IDE" / "User" / "settings.json"
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                settings_locked = (
                    d.get("update.mode") == "none"
                    and d.get("antigravity.releaseBaseUrl") == "http://127.0.0.1:9"
                )
            except Exception:
                pass
    if ide_upd and ide_upd.name == UPDATER_BAK:
        return settings_locked or bool(_load_state().get("ide_locked"))
    if settings_locked:
        return True
    st = _load_state()
    return bool(st.get("ide_locked"))


def is_manager_locked() -> bool:
    mgr_yml = _find_manager_update_yml()
    if mgr_yml and mgr_yml.name == APP_UPDATE_YML:
        return False
    if mgr_yml and mgr_yml.name == APP_UPDATE_BAK:
        return True
    st = _load_state()
    return bool(st.get("manager_locked"))


def is_cli_locked() -> bool:
    st = _load_state()
    if "cli_locked" in st:
        return bool(st.get("cli_locked"))
    sidecar = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".gemini" / "bin" / "agy.version.json"
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("releaseBaseUrl") == "http://127.0.0.1:9":
                if not st.get("ide_locked") and not st.get("manager_locked"):
                    return True
        except Exception:
            pass
    return False


def is_vscode_locked() -> bool:
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        p = Path(appdata) / "Code" / "User" / "settings.json"
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if d.get("extensions.autoUpdate") is False:
                    return True
            except Exception:
                pass
    st = _load_state()
    return bool(st.get("vscode_locked"))


def is_update_locked() -> bool:
    """True, если заблокирован хотя бы один из компонентов (IDE, 2.0, CLI или VS Code)."""
    return is_ide_locked() or is_manager_locked() or is_cli_locked() or is_vscode_locked()


def print_lock_status(ide_root: Optional[str] = None) -> None:
    """Отображает текущий статус блокировки обновлений для всех компонентов."""
    ide_is_locked = is_ide_locked(ide_root)
    mgr_is_locked = is_manager_locked()
    cli_is_locked = is_cli_locked()
    vsc_is_locked = is_vscode_locked()

    ide_st = color("[LOCKED]", COLOR_RED) if ide_is_locked else color("[ENABLED]", COLOR_GREEN)
    mgr_st = color("[LOCKED]", COLOR_RED) if mgr_is_locked else color("[ENABLED]", COLOR_GREEN)
    cli_st = color("[LOCKED]", COLOR_RED) if cli_is_locked else color("[ENABLED]", COLOR_GREEN)
    vsc_st = color("[LOCKED]", COLOR_RED) if vsc_is_locked else color("[ENABLED]", COLOR_GREEN)

    print("  Current Status:")
    print(f"    • Antigravity IDE:            {ide_st}")
    print(f"    • Antigravity 2.0:            {mgr_st}")
    print(f"    • Antigravity CLI (agy):      {cli_st}")
    print(f"    • Antigravity VS Code:        {vsc_st}")


# ---------------------------------------------------------------------------
# Управление отдельными компонентами
# ---------------------------------------------------------------------------

def toggle_ide_updates(ide_root: Optional[str] = None) -> bool:
    """Включает/выключает блокировку обновлений только для Antigravity IDE."""
    st = _load_state()
    if is_ide_locked(ide_root):
        info("Unlocking Antigravity IDE auto-updates...")
        ide_upd = _find_ide_updater(ide_root)
        if ide_upd and ide_upd.name == UPDATER_BAK:
            try:
                orig = ide_upd.parent / UPDATER_EXE
                orig.unlink(missing_ok=True)
                os.replace(ide_upd, orig)
                step(f"Restored {UPDATER_BAK} -> {UPDATER_EXE}", True)
            except Exception as ex:
                err(f"Could not restore updater: {ex}")
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            p = Path(appdata) / "Antigravity IDE" / "User" / "settings.json"
            _update_json_settings(p, {
                "update.mode": "default",
                "update.enableWindowsBackgroundUpdates": True,
                "antigravity.releaseBaseUrl": "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app",
            })
            step("IDE settings: update.mode = default", True)
        if not is_cli_locked() and not is_manager_locked():
            _update_agy_version_sidecar(lock=False)
        for d in _ide_lock_dirs():
            _icacls(d, "reset")
        st["ide_locked"] = False
        st.pop("ide_updater_bak", None)
        _save_state(st)
        ok("Antigravity IDE auto-updates UNLOCKED.")
        return False
    else:
        info("Locking Antigravity IDE auto-updates...")
        _kill_updater_processes()
        _cleanup_ide_staging(ide_root)
        ide_upd = _find_ide_updater(ide_root)
        if ide_upd and ide_upd.name == UPDATER_EXE:
            try:
                bp = ide_upd.parent / UPDATER_BAK
                bp.unlink(missing_ok=True)
                os.replace(ide_upd, bp)
                st["ide_updater_bak"] = str(bp)
                step(f"Renamed {UPDATER_EXE} -> {UPDATER_BAK}", True)
            except Exception as ex:
                err(f"Could not rename updater: {ex}")
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            p = Path(appdata) / "Antigravity IDE" / "User" / "settings.json"
            _update_json_settings(p, {
                "update.mode": "none",
                "update.enableWindowsBackgroundUpdates": False,
                "update.showReleaseNotes": False,
                "antigravity.releaseBaseUrl": "http://127.0.0.1:9",
                "extensions.autoUpdate": False,
                "extensions.autoCheckUpdates": False,
                "telemetry.telemetryLevel": "off",
                "telemetry.enableCrashReporter": False,
                "telemetry.enableTelemetry": False,
            })
            step("IDE settings: update.mode = none & releaseBaseUrl locked, telemetry = off", True)
        _update_agy_version_sidecar(lock=True)
        # Purge stale staging installers & lock IDE temp update dirs only
        for d in _ide_lock_dirs():
            _cleanup_updater_dir_contents(d)
            _icacls(d, "deny")
        st["ide_locked"] = True
        _save_state(st)
        ok("Antigravity IDE auto-updates LOCKED.")
        return True


def toggle_manager_updates() -> bool:
    """Включает/выключает блокировку обновлений только для Antigravity 2.0."""
    st = _load_state()
    appdata = os.environ.get("APPDATA", "")
    mgr_settings = Path(appdata) / "antigravity" / "User" / "settings.json" if appdata else None
    if is_manager_locked():
        info("Unlocking Antigravity 2.0 auto-updates...")
        mgr_yml = _find_manager_update_yml()
        if mgr_yml and mgr_yml.name == APP_UPDATE_BAK:
            try:
                orig = mgr_yml.parent / APP_UPDATE_YML
                orig.unlink(missing_ok=True)
                os.replace(mgr_yml, orig)
                step(f"Restored {APP_UPDATE_BAK} -> {APP_UPDATE_YML}", True)
            except Exception as ex:
                err(f"Could not restore {APP_UPDATE_YML}: {ex}")
        if mgr_settings and mgr_settings.is_file():
            _update_json_settings(mgr_settings, {
                "update.mode": "default",
                "antigravity.releaseBaseUrl": "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app",
            })
        for d in _manager_lock_dirs():
            _icacls(d, "reset")
        st["manager_locked"] = False
        st.pop("manager_yml_bak", None)
        _save_state(st)
        ok("Antigravity 2.0 auto-updates UNLOCKED.")
        return False
    else:
        info("Locking Antigravity 2.0 auto-updates...")
        _kill_updater_processes()
        mgr_yml = _find_manager_update_yml()
        if mgr_yml and mgr_yml.name == APP_UPDATE_YML:
            try:
                bp = mgr_yml.parent / APP_UPDATE_BAK
                bp.unlink(missing_ok=True)
                os.replace(mgr_yml, bp)
                st["manager_yml_bak"] = str(bp)
                step(f"Renamed {APP_UPDATE_YML} -> {APP_UPDATE_BAK}", True)
            except Exception as ex:
                err(f"Could not rename {APP_UPDATE_YML}: {ex}")
        if mgr_settings:
            _update_json_settings(mgr_settings, {
                "update.mode": "none",
                "update.enableWindowsBackgroundUpdates": False,
                "antigravity.releaseBaseUrl": "http://127.0.0.1:9",
            })
        _update_agy_version_sidecar(lock=True)
        for d in _manager_lock_dirs():
            _cleanup_updater_dir_contents(d)
            _icacls(d, "deny")
        st["manager_locked"] = True
        _save_state(st)
        ok("Antigravity 2.0 auto-updates LOCKED.")
        return True


def toggle_cli_updates() -> bool:
    """Включает/выключает блокировку обновлений только для Antigravity CLI (agy)."""
    st = _load_state()
    if is_cli_locked():
        info("Unlocking Antigravity CLI (agy) auto-updates...")
        _update_agy_version_sidecar(lock=False)
        for d in _cli_lock_dirs():
            _icacls(d, "reset")
        st["cli_locked"] = False
        _save_state(st)
        ok("Antigravity CLI (agy) auto-updates UNLOCKED.")
        return False
    else:
        info("Locking Antigravity CLI (agy) auto-updates...")
        _kill_updater_processes()
        _update_agy_version_sidecar(lock=True)
        for d in _cli_lock_dirs():
            _cleanup_updater_dir_contents(d)
            _icacls(d, "deny")
        st["cli_locked"] = True
        _save_state(st)
        ok("Antigravity CLI (agy) auto-updates LOCKED.")
        return True


def toggle_vscode_updates() -> bool:
    """Включает/выключает блокировку обновлений для расширения VS Code."""
    st = _load_state()
    appdata = os.environ.get("APPDATA", "")
    p = Path(appdata) / "Code" / "User" / "settings.json" if appdata else None
    if is_vscode_locked():
        info("Unlocking VS Code extension auto-updates...")
        if p and p.is_file():
            _update_json_settings(p, {"extensions.autoUpdate": True, "extensions.autoCheckUpdates": True})
            step("VS Code settings: extensions.autoUpdate = true", True)
        st["vscode_locked"] = False
        _save_state(st)
        ok("VS Code extension auto-updates UNLOCKED.")
        return False
    else:
        info("Locking VS Code extension auto-updates...")
        if p:
            _update_json_settings(p, {"extensions.autoUpdate": False, "extensions.autoCheckUpdates": False})
            step("VS Code settings: extensions.autoUpdate = false", True)
        st["vscode_locked"] = True
        _save_state(st)
        ok("VS Code extension auto-updates LOCKED.")
        return True


# ---------------------------------------------------------------------------
# Массовая блокировка / разблокировка
# ---------------------------------------------------------------------------

def do_lock_updates(ide_root: Optional[str] = None) -> bool:
    """Блокирует авто-обновления для ВСЕХ компонентов: IDE, 2.0, CLI и VS Code."""
    if os.name != "nt":
        warn("Auto-update lock is Windows-only.")
        return False

    info("Locking auto-updates for ALL applications (IDE, 2.0, CLI, VS Code)...")
    print()

    # 1. IDE
    if not is_ide_locked(ide_root):
        toggle_ide_updates(ide_root)
    else:
        step("Antigravity IDE", None, "already locked")

    # 2. Antigravity 2.0
    if not is_manager_locked():
        toggle_manager_updates()
    else:
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            _update_json_settings(
                Path(appdata) / "antigravity" / "User" / "settings.json",
                {
                    "update.mode": "none",
                    "update.enableWindowsBackgroundUpdates": False,
                    "antigravity.releaseBaseUrl": "http://127.0.0.1:9",
                },
            )
        _update_agy_version_sidecar(lock=True)
        for d in _manager_lock_dirs():
            _cleanup_updater_dir_contents(d)
            _icacls(d, "deny")
        step("Antigravity 2.0", None, "already locked (verified sidecar & cache)")

    # 3. Antigravity CLI
    if not is_cli_locked():
        toggle_cli_updates()
    else:
        step("Antigravity CLI (agy)", None, "already locked")

    # 4. VS Code
    if not is_vscode_locked():
        toggle_vscode_updates()
    else:
        step("VS Code Extension", None, "already locked")

    print()
    ok("Auto-update lock ENABLED for ALL applications.")
    return True


def do_unlock_updates(ide_root: Optional[str] = None) -> bool:
    """Снимает блокировку авто-обновлений для ВСЕХ приложений."""
    if os.name != "nt":
        warn("Update lock management is Windows-only.")
        return False

    info("Unlocking auto-updates for ALL applications...")
    print()

    if is_ide_locked(ide_root):
        toggle_ide_updates(ide_root)

    if is_manager_locked():
        toggle_manager_updates()

    if is_cli_locked():
        toggle_cli_updates()

    if is_vscode_locked():
        toggle_vscode_updates()

    # Сбрасываем icacls
    for d in _lock_dirs():
        _icacls(d, "reset")

    try:
        LOCK_STATE.unlink(missing_ok=True)
    except Exception:
        pass

    print()
    ok("Auto-update lock REMOVED for ALL applications.")
    return True


def do_toggle_updates(ide_root: Optional[str] = None) -> bool:
    """Если заблокировано хотя бы что-то — снимает. Иначе блокирует всё."""
    if is_update_locked():
        return do_unlock_updates(ide_root)
    return do_lock_updates(ide_root)
