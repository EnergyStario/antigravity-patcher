"""
patcher/auto_repatch.py -- Automatic re-patch & relaunch after Antigravity updates.

Detects unpatched Antigravity IDE, Antigravity 2.0 (binary + app.asar + Desktop UI),
Antigravity CLI (agy), VS Code extension, and Antigravity Assistant extension,
closes running instances cleanly, patches everything non-interactively, and
relaunches apps that were open. On Windows, also manages a lightweight
Scheduled Task watcher (`AntigravityPatcherAutoWatch`) so updates are caught
automatically in the background without user intervention.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from patcher.constants import VERSION
from patcher.utils.console import err, hint, info, ok, warn

TASK_NAME = "AntigravityPatcherAutoWatch"
CREATE_NO_WINDOW = 0x08000000


def _home_dir() -> Path:
    return Path.home()


def _tools_dir() -> Path:
    env_dir = os.environ.get("AG_TOOLS_DIR", "").strip()
    return Path(env_dir) if env_dir else (_home_dir() / ".antigravity_tools")


def _config_path() -> Path:
    return _tools_dir() / "auto_repatch.json"


def _log_path() -> Path:
    return _tools_dir() / "auto_repatch.log"


def _auto_update_stamp_path() -> Path:
    return _tools_dir() / "auto_update_stamp.json"


def _should_check_app_updates(min_interval_sec: float = 14400.0) -> bool:
    """Rate-limit background official feed update checks (default 4h)."""
    p = _auto_update_stamp_path()
    if not p.is_file():
        return True
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        last_ts = float(data.get("last_check_ts", 0.0) or 0.0)
        return (time.time() - last_ts) >= max(0.0, float(min_interval_sec))
    except Exception:
        return True


def _mark_app_updates_checked() -> None:
    try:
        p = _auto_update_stamp_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_check_ts": time.time()}), encoding="utf-8")
    except Exception:
        pass


def _watcher_script_path() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            return Path(local) / "AntigravityPatcher" / "auto_watch.ps1"
    return _tools_dir() / "auto_watch.ps1"


def _append_log(message: str) -> None:
    try:
        lp = _log_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines: list[str] = []
        if lp.is_file():
            lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append(f"[{ts}] {message}")
        lp.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_auto_repatch_config() -> dict[str, Any]:
    cfg_p = _config_path()
    if cfg_p.is_file():
        try:
            data = json.loads(cfg_p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"enabled": False, "user_disabled": False, "runner": []}


def save_auto_repatch_config(cfg: dict[str, Any]) -> None:
    cfg_p = _config_path()
    cfg_p.parent.mkdir(parents=True, exist_ok=True)
    cfg_p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _install_durable_frozen_runner() -> str | None:
    """When running as a frozen Windows exe, install/refresh a stable background copy in %LOCALAPPDATA%\\AntigravityPatcher."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    try:
        src = Path(sys.executable).resolve()
        if not src.is_file() or src.stat().st_size < 1024 * 1024:
            return None
        local = os.environ.get("LOCALAPPDATA", "")
        if not local:
            return None
        dst_dir = Path(local) / "AntigravityPatcher"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "Antigravity_Patcher_Windows_x64.exe"
        if src == dst.resolve() if dst.exists() else False:
            return str(dst)
        import shutil
        src_st = src.stat()
        needs_copy = (not dst.is_file()) or (dst.stat().st_size != src_st.st_size) or (int(dst.stat().st_mtime) < int(src_st.st_mtime))
        if needs_copy:
            shutil.copy2(src, dst)
        return str(dst) if dst.is_file() else None
    except Exception:
        return None


def resolve_runner_command() -> list[str]:
    """Return command vector to run patcher CLI with flags (frozen exe or python + main.py)."""
    if getattr(sys, "frozen", False):
        durable = _install_durable_frozen_runner()
        return [durable or sys.executable]
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    if not main_py.is_file():
        main_py = Path.cwd() / "source" / "main.py"
    return [sys.executable, str(main_py)]


def _is_ide_ls_patched(ide_ls_path: str) -> bool:
    if not ide_ls_path or not os.path.isfile(ide_ls_path):
        return True
    try:
        from patcher.manager.patcher import MANAGER_GATE, get_executable_ranges

        ranges = get_executable_ranges(ide_ls_path)
        with open(ide_ls_path, "rb") as f:
            bdata = f.read()
        try:
            kind, _off, _g = MANAGER_GATE.resolve(bdata, ranges)
            if kind == "unpatched":
                return False
        except LookupError:
            pass
        if b"ineligible" in bdata:
            return False
        return True
    except Exception:
        return True


def _is_extension_deployed(home: Path | None = None) -> bool:
    h = home or _home_dir()
    tools = h / ".antigravity_tools"
    target_ver_dir = f"energystar.ag-toolkit-{VERSION}"
    ide_ext_ok = (h / ".antigravity-ide" / "extensions" / target_ver_dir / "extension.js").is_file()
    swarm_ok = (tools / "swarm_engine" / "swarm" / "ipc_server.py").is_file()
    skill_ok = (h / ".gemini" / "antigravity" / "skills" / "ag-swarm" / "SKILL.md").is_file()
    return ide_ext_ok and swarm_ok and skill_ok


def _is_desktop_ui_files_deployed(home: Path | None = None) -> bool:
    h = home or _home_dir()
    tools = h / ".antigravity_tools"
    required = (
        tools / "ag_desktop_ui.js",
        tools / "quota_engine.js",
        tools / "account_switcher.py",
        tools / "switch_runner.py",
        tools / "nrpt_dns.py",
        tools / "swarm_engine" / "swarm" / "ipc_server.py",
        h / ".gemini" / "antigravity" / "skills" / "ag-swarm" / "SKILL.md",
    )
    return all(p.is_file() for p in required)


def _status_cache_file() -> Path:
    return _tools_dir() / "patch_status_cache.json"


_PATCHER_LOGIC_SIG_CACHE: str | None = None


def _patcher_logic_signature() -> str:
    """Return a fast digest of current patcher checker logic so stale disk caches self-invalidate on patcher upgrades."""
    global _PATCHER_LOGIC_SIG_CACHE
    if _PATCHER_LOGIC_SIG_CACHE is not None:
        return _PATCHER_LOGIC_SIG_CACHE
    import hashlib
    h = hashlib.sha256()
    h.update(str(VERSION).encode("utf-8"))
    for mod_name, fn_name in (
        ("patcher.ide.patcher", "is_already_patched"),
        ("patcher.manager.patcher", "is_already_patched"),
        ("patcher.manager.asar_patcher", "_is_asar_file_patched"),
        ("patcher.manager.asar_patcher", "_patch_language_server_js"),
        ("patcher.agy.patcher", "is_already_patched"),
        ("patcher.vscode.patcher", "is_already_patched"),
        ("patcher.tls_fix", "is_tls_fix_applied"),
    ):
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)
            code = getattr(fn, "__code__", None)
            if code is not None:
                h.update(code.co_code)
                h.update(repr(code.co_consts).encode("utf-8", errors="ignore"))
        except Exception:
            pass
    _PATCHER_LOGIC_SIG_CACHE = h.hexdigest()[:16]
    return _PATCHER_LOGIC_SIG_CACHE


def _load_inspect_cache() -> dict[str, dict[str, Any]]:
    p = _status_cache_file()
    sig = _patcher_logic_signature()
    if not p.is_file():
        return {"__meta__": {"patcher_sig": sig}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"__meta__": {"patcher_sig": sig}}
        meta = raw.get("__meta__")
        if not isinstance(meta, dict) or meta.get("patcher_sig") != sig:
            return {"__meta__": {"patcher_sig": sig}}
        return raw
    except Exception:
        return {"__meta__": {"patcher_sig": sig}}


def _save_inspect_cache(cache: dict[str, dict[str, Any]]) -> None:
    try:
        p = _status_cache_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        cache["__meta__"] = {"patcher_sig": _patcher_logic_signature()}
        p.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def _cached_target_check(
    path: str,
    checker_fn: Callable[[Any], bool],
    cache: dict[str, dict[str, Any]],
    cache_tag: str = "",
    is_text: bool = False,
) -> tuple[bool, bool]:
    """Check patch status with (mtime_ns, size) disk cache; returns (result, cache_dirty)."""
    if not path or not os.path.isfile(path):
        return True, False
    try:
        st = os.stat(path)
        key = f"{cache_tag}::{path}" if cache_tag else path
        rec = cache.get(key)
        if (
            isinstance(rec, dict)
            and int(rec.get("mtime_ns", -1)) == int(st.st_mtime_ns)
            and int(rec.get("size", -1)) == int(st.st_size)
            and "patched" in rec
        ):
            return bool(rec["patched"]), False
        if is_text:
            val = Path(path).read_text(encoding="utf-8", errors="replace")
            res = bool(checker_fn(val))
        else:
            res = bool(checker_fn(path))
        cache[key] = {"mtime_ns": int(st.st_mtime_ns), "size": int(st.st_size), "patched": res}
        return res, True
    except Exception:
        return False, False


def inspect_targets(
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
    vscode_path: str = "",
    vscode_agy_path: str = "",
) -> dict[str, Any]:
    """Inspect all Antigravity targets without mutating disk."""
    from patcher.ide import discovery as ide_disc, patcher as ide_patcher
    from patcher.manager import discovery as mgr_disc, patcher as mgr_patcher
    from patcher.agy import discovery as agy_disc, patcher as agy_patcher
    from patcher.vscode import discovery as vsc_disc, patcher as vsc_patcher
    from patcher.tls_fix import is_tls_fix_applied

    if not main_js_path or not os.path.isfile(main_js_path):
        root = ide_disc.find_install_root()
        main_js_path = ide_disc.find_main_js(root) if root else ""
    if not manager_path or not os.path.isfile(manager_path):
        manager_path = mgr_disc.find_manager_binary() or ""
    if not agy_path or not os.path.isfile(agy_path):
        agy_path = agy_disc.find_agy_binary() or ""
    if not vscode_path or not os.path.isfile(vscode_path):
        vscode_path = vsc_disc.find_extension_js() or ""
    if not vscode_agy_path or not os.path.isfile(vscode_agy_path):
        vscode_agy_path = vsc_disc.find_gemini_antigravity_binary() or ""

    cache = _load_inspect_cache()
    cache_dirty = False

    needs_ide = False
    needs_tls = False
    if main_js_path and os.path.isfile(main_js_path):
        ide_ok, d1 = _cached_target_check(main_js_path, ide_patcher.is_already_patched, cache, is_text=True)
        cache_dirty = cache_dirty or d1
        needs_ide = not ide_ok
        if not needs_ide:
            ide_ls = ide_patcher.find_ide_internal_language_server(main_js_path)
            if ide_ls:
                ls_ok, d2 = _cached_target_check(ide_ls, _is_ide_ls_patched, cache, cache_tag="ide_ls")
                cache_dirty = cache_dirty or d2
                if not ls_ok:
                    needs_ide = True
        tls_ok, d3 = _cached_target_check(main_js_path, is_tls_fix_applied, cache, cache_tag="tls_v2")
        cache_dirty = cache_dirty or d3
        needs_tls = not tls_ok

    needs_mgr = False
    if manager_path and os.path.isfile(manager_path):
        mgr_ok, d4 = _cached_target_check(manager_path, mgr_patcher.is_already_patched, cache, cache_tag="mgr_v2")
        cache_dirty = cache_dirty or d4
        needs_mgr = not mgr_ok
        if not needs_mgr:
            from patcher.manager.discovery import find_asar_relative_to_manager
            from patcher.manager.asar_patcher import _is_asar_file_patched
            asar_p = find_asar_relative_to_manager(manager_path)
            if asar_p and os.path.isfile(asar_p):
                asar_ok, d4b = _cached_target_check(asar_p, _is_asar_file_patched, cache, cache_tag="asar_v2")
                cache_dirty = cache_dirty or d4b
                if not asar_ok:
                    needs_mgr = True

    needs_agy = False
    if agy_path and os.path.isfile(agy_path):
        agy_ok, d5 = _cached_target_check(agy_path, agy_patcher.is_already_patched, cache)
        cache_dirty = cache_dirty or d5
        needs_agy = not agy_ok

    needs_vsc = False
    if vscode_path and os.path.isfile(vscode_path):
        vsc_ok, d6 = _cached_target_check(vscode_path, vsc_patcher.is_already_patched, cache, is_text=True)
        cache_dirty = cache_dirty or d6
        needs_vsc = not vsc_ok
    if vscode_agy_path and os.path.isfile(vscode_agy_path):
        same_as_agy = bool(
            agy_path
            and os.path.isfile(agy_path)
            and os.path.realpath(vscode_agy_path).lower() == os.path.realpath(agy_path).lower()
        )
        if same_as_agy:
            if needs_agy:
                needs_vsc = True
        else:
            v_agy_ok, d7 = _cached_target_check(vscode_agy_path, agy_patcher.is_already_patched, cache)
            cache_dirty = cache_dirty or d7
            if not v_agy_ok:
                needs_vsc = True

    if cache_dirty:
        _save_inspect_cache(cache)

    needs_ext = False
    if (main_js_path and os.path.isfile(main_js_path)) or (vscode_path and os.path.isfile(vscode_path)):
        needs_ext = not _is_extension_deployed()

    needs_ui = False
    if manager_path and os.path.isfile(manager_path):
        needs_ui = not _is_desktop_ui_files_deployed()

    any_needed = any([needs_ide, needs_tls, needs_mgr, needs_agy, needs_vsc, needs_ext, needs_ui])
    return {
        "main_js_path": main_js_path or "",
        "manager_path": manager_path or "",
        "agy_path": agy_path or "",
        "vscode_path": vscode_path or "",
        "vscode_agy_path": vscode_agy_path or "",
        "needs_ide": needs_ide,
        "needs_tls": needs_tls,
        "needs_mgr": needs_mgr,
        "needs_agy": needs_agy,
        "needs_vsc": needs_vsc,
        "needs_ext": needs_ext,
        "needs_ui": needs_ui,
        "needs_any": any_needed,
    }


def is_installer_busy() -> bool:
    """Return True if an Antigravity updater/installer appears to be running."""
    if os.name != "nt":
        return False
    try:
        from patcher.utils.admin import _get_running_win_images
        running = _get_running_win_images()
        if not running:
            return False
        for img in running:
            if img == "inno_updater.exe" or img.startswith("antigravitysetup"):
                return True
    except Exception:
        pass
    return False


def wait_for_installer_idle(max_wait_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + max(0.0, max_wait_seconds)
    while time.monotonic() < deadline and is_installer_busy():
        time.sleep(1.5)


def are_updates_allowed() -> bool:
    """Return True if auto-updates are enabled for at least one *installed* Antigravity component."""
    try:
        from patcher.updater_lock import is_ide_locked, is_manager_locked, is_vscode_locked
        from patcher.ide import discovery as ide_disc
        from patcher.manager import discovery as mgr_disc
        from patcher.vscode import discovery as vsc_disc

        has_ide = bool(ide_disc.find_install_root())
        has_mgr = bool(mgr_disc.find_manager_binary())
        has_vsc = bool(vsc_disc.find_extension_js())

        if not (has_ide or has_mgr or has_vsc):
            return not (is_ide_locked() and is_manager_locked() and is_vscode_locked())

        checks: list[bool] = []
        if has_ide:
            checks.append(is_ide_locked())
        if has_mgr:
            checks.append(is_manager_locked())
        if has_vsc:
            checks.append(is_vscode_locked())
        return not all(checks)
    except Exception:
        return True


def _restore_active_session_after_patch() -> list[str]:
    """Preserve active auth & suppress onboarding wipe if an app update reset session state."""
    restored: list[str] = []
    tools = _tools_dir()
    acc_index = tools / "accounts.json"
    if not acc_index.is_file():
        return restored
    try:
        idx = json.loads(acc_index.read_text(encoding="utf-8"))
    except Exception:
        return restored

    sa_id = idx.get("current_standalone_account_id")
    ide_id = idx.get("current_ide_account_id") or idx.get("current_account_id")
    accounts = idx.get("accounts") or []

    def _find_acc(aid: Any) -> dict[str, Any] | None:
        if not aid:
            return None
        for a in accounts:
            if isinstance(a, dict) and a.get("id") == aid:
                return a
        return None

    # 1. Keep Desktop 2.0 onboarding dismissed when an account is active
    appdata = os.environ.get("APPDATA", "")
    sa_acc = _find_acc(sa_id)
    if appdata and sa_acc and sa_acc.get("email"):
        app_storage = Path(appdata) / "antigravity" / "app_storage.json"
        if app_storage.is_file():
            try:
                st = json.loads(app_storage.read_text(encoding="utf-8"))
                if isinstance(st, dict):
                    dirty = False
                    if st.get("ide-install-wizard-shown") != "true":
                        st["ide-install-wizard-shown"] = "true"
                        dirty = True
                    if not st.get("agy.onboarding.lastLoginUsername"):
                        st["agy.onboarding.lastLoginUsername"] = sa_acc["email"]
                        dirty = True
                    if st.get("agy.onboarding.lastLoginIsGcpTos") != "false":
                        st["agy.onboarding.lastLoginIsGcpTos"] = "false"
                        dirty = True
                    if dirty:
                        app_storage.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
                        restored.append("Desktop onboarding state")
            except Exception:
                pass

    # 2. If IDE update cleared antigravityAuthStatus from state.vscdb, restore current IDE account
    ide_acc = _find_acc(ide_id)
    if appdata and ide_acc and ide_id:
        vscdb = Path(appdata) / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb"
        if vscdb.is_file():
            try:
                import sqlite3
                conn = sqlite3.connect(f"file:{vscdb}?mode=ro", uri=True, timeout=2.0)
                try:
                    rows = conn.execute(
                        "SELECT value FROM ItemTable WHERE key IN "
                        "('antigravityAuthStatus', 'antigravityUnifiedStateSync.oauthToken')"
                    ).fetchall()
                finally:
                    conn.close()
                has_auth = any(
                    r and r[0] and str(r[0]).strip() not in ("", "null", "{}")
                    for r in rows
                )
                if not has_auth:
                    sw_path = tools / "account_switcher.py"
                    if sw_path.is_file():
                        import importlib.util
                        spec = importlib.util.spec_from_file_location("ag_acc_sw_restore", str(sw_path))
                        if spec and spec.loader:
                            mod = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(mod)
                            sw_ok = False
                            if hasattr(mod, "switch_to_account"):
                                sw_res = mod.switch_to_account(ide_id, app_type="ide", restart=False)
                                sw_ok = bool(sw_res.get("ok")) if isinstance(sw_res, dict) else bool(sw_res)
                            elif hasattr(mod, "switch_account"):
                                sw_ok = bool(mod.switch_account(ide_id, app_target="ide"))
                            if sw_ok:
                                restored.append("IDE auth session")
            except Exception:
                pass

    return restored


def run_auto_repatch(
    restart_running: bool = True,
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
    vscode_path: str = "",
    vscode_agy_path: str = "",
    force_relaunch: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """
    Check all Antigravity targets; if anything became unpatched (or extension/UI went missing),
    close affected running apps cleanly, apply all needed patches non-interactively,
    re-deploy extension & Desktop UI, and relaunch apps that were open.
    """
    is_auto_patch_invocation = (
        "--auto-patch" in sys.argv[1:]
        or os.environ.get("AG_AUTO_REPATCH_SILENT") == "1"
    )
    try:
        return _run_auto_repatch_impl(
            restart_running=restart_running,
            main_js_path=main_js_path,
            manager_path=manager_path,
            agy_path=agy_path,
            vscode_path=vscode_path,
            vscode_agy_path=vscode_agy_path,
            force_relaunch=force_relaunch,
            heal_env=is_auto_patch_invocation,
        )
    except Exception as exc:
        if is_auto_patch_invocation:
            try:
                import traceback
                from patcher.diagnostics import copy_sanitized_bug_report_to_clipboard
                copy_sanitized_bug_report_to_clipboard(
                    traceback.format_exc(),
                    context_info="run_auto_repatch (--auto-patch)",
                )
            except Exception:
                pass
        _append_log(f"repatch exception: {exc}")
        raise


def _run_auto_repatch_impl(
    restart_running: bool = True,
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
    vscode_path: str = "",
    vscode_agy_path: str = "",
    force_relaunch: dict[str, bool] | None = None,
    heal_env: bool = False,
) -> dict[str, Any]:
    wait_for_installer_idle(max_wait_seconds=30.0)

    if heal_env:
        try:
            from patcher.diagnostics import ensure_target_and_env_healthy
            ensure_target_and_env_healthy(
                target="all",
                quiet=True,
                main_js_path=main_js_path,
                manager_path=manager_path,
                agy_path=agy_path,
            )
        except Exception as exc:
            _append_log(f"ensure_target_and_env_healthy warning: {exc}")

    pre_update_running = {"ide": False, "manager": False}
    if heal_env:
        wait_for_installer_idle(max_wait_seconds=60.0)
        try:
            from patcher.utils.launcher import detect_running_antigravity_apps
            from patcher.app_updater import check_and_install_component_update
            from patcher.updater_lock import is_ide_locked, is_manager_locked

            def _resolve_ide_root_for_lock(mjs: str) -> str | None:
                if not mjs:
                    return None
                p = Path(mjs)
                for _ in range(5):
                    if (p / "Antigravity IDE.exe").is_file():
                        return str(p)
                    p = p.parent
                return None

            if not is_installer_busy() and _should_check_app_updates():
                _mark_app_updates_checked()
                pre_update_running = detect_running_antigravity_apps()
                ide_root_lock = _resolve_ide_root_for_lock(main_js_path)
                if main_js_path and not is_ide_locked(ide_root_lock):
                    did_i, new_i, _ = check_and_install_component_update(
                        "ide",
                        main_js_path,
                        is_locked_fn=lambda: is_ide_locked(ide_root_lock),
                        quiet=True,
                        auto_install=True,
                    )
                    if did_i and new_i:
                        main_js_path = new_i
                if manager_path and not is_manager_locked():
                    did_m, new_m, _ = check_and_install_component_update(
                        "manager",
                        manager_path,
                        is_locked_fn=is_manager_locked,
                        quiet=True,
                        auto_install=True,
                    )
                    if did_m and new_m:
                        manager_path = new_m
        except Exception as exc:
            _append_log(f"auto update check warning: {exc}")

    status = inspect_targets(
        main_js_path=main_js_path,
        manager_path=manager_path,
        agy_path=agy_path,
        vscode_path=vscode_path,
        vscode_agy_path=vscode_agy_path,
    )
    if not status["needs_any"]:
        return {"changed": False, "patched": [], "failed": [], "restarted": [], "status": status}

    from patcher.utils.launcher import detect_running_antigravity_apps, restart_antigravity_apps
    from patcher.utils.admin import terminate_processes, terminate_standalone_processes
    from patcher.ide import patcher as ide_patcher
    from patcher.manager import patcher as mgr_patcher
    from patcher.agy import patcher as agy_patcher
    from patcher.vscode import patcher as vsc_patcher
    from patcher.tls_fix import do_tls_fix_inject, is_tls_fix_applied
    from patcher.extension_installer import deploy_assistant_extension, deploy_antigravity_2_ui

    live_running = detect_running_antigravity_apps()
    was_running = {
        "ide": bool(live_running.get("ide") or pre_update_running.get("ide")),
        "manager": bool(live_running.get("manager") or pre_update_running.get("manager")),
    }

    ide_will_restart = status["needs_ide"] or status["needs_tls"]
    mgr_will_restart = status["needs_mgr"]

    terminated = False
    if restart_running:
        if ide_will_restart and was_running.get("ide"):
            if terminate_processes(["Antigravity IDE", "Antigravity IDE.exe"]):
                terminated = True
        if mgr_will_restart and was_running.get("manager"):
            if terminate_standalone_processes():
                terminated = True
    if terminated:
        time.sleep(1.0)

    patched: list[str] = []
    failed: list[str] = []

    p_ide = status["main_js_path"]
    if p_ide:
        if status["needs_ide"]:
            if ide_patcher.do_patch(p_ide, show_search_line=False):
                patched.append("Antigravity IDE")
            else:
                failed.append("Antigravity IDE")
        if status["needs_tls"] or not is_tls_fix_applied(p_ide):
            if do_tls_fix_inject(p_ide):
                patched.append("TLS/Proxy fix")
            else:
                failed.append("TLS/Proxy fix")

    p_mgr = status["manager_path"]
    if p_mgr and status["needs_mgr"]:
        mgr_patcher.do_patch_manager(p_mgr, kill_on_lock=True)
        if mgr_patcher.is_already_patched(p_mgr):
            patched.append("Antigravity 2.0")
        else:
            failed.append("Antigravity 2.0")

    p_agy = status["agy_path"]
    if p_agy and status["needs_agy"]:
        agy_patcher.do_patch_agy(p_agy)
        if agy_patcher.is_already_patched(p_agy):
            patched.append("Antigravity CLI")
        else:
            failed.append("Antigravity CLI")

    if status["needs_vsc"]:
        p_vsc = status["vscode_path"]
        p_vsc_agy = status["vscode_agy_path"]
        vsc_ok = True
        if p_vsc and os.path.isfile(p_vsc):
            if not vsc_patcher.do_patch_vscode(p_vsc):
                vsc_ok = False
        if p_vsc_agy and os.path.isfile(p_vsc_agy) and not agy_patcher.is_already_patched(p_vsc_agy):
            agy_patcher.do_patch_agy(p_vsc_agy)
            if not agy_patcher.is_already_patched(p_vsc_agy):
                vsc_ok = False
        if vsc_ok:
            patched.append("VS Code extension")
        else:
            failed.append("VS Code extension")

    if status["needs_ext"]:
        if deploy_assistant_extension(export_vsix=False):
            patched.append("Antigravity Assistant Extension")
        else:
            failed.append("Antigravity Assistant Extension")
    elif status["needs_ui"]:
        if deploy_antigravity_2_ui():
            patched.append("Antigravity 2.0 UI")
        else:
            failed.append("Antigravity 2.0 UI")

    if patched:
        try:
            restored_auth = _restore_active_session_after_patch()
            if restored_auth:
                patched.extend(restored_auth)
        except Exception:
            pass

    fr = force_relaunch or {}
    restart_mask = {
        "ide": bool(ide_will_restart and (was_running.get("ide") or fr.get("ide"))),
        "manager": bool(mgr_will_restart and (was_running.get("manager") or fr.get("manager"))),
    }
    restarted: list[str] = []
    if restart_running and any(restart_mask.values()):
        restarted = restart_antigravity_apps(
            restart_mask,
            main_js_path=p_ide,
            manager_path=p_mgr,
            ensure_backend=True,
        )

    _append_log(
        f"repatch patched={patched or 'none'} failed={failed or 'none'} restarted={restarted or 'none'}"
    )
    return {
        "changed": bool(patched or restarted),
        "patched": patched,
        "failed": failed,
        "restarted": restarted,
        "status": status,
    }


def _ps_quote(val: str) -> str:
    return "'" + str(val).replace("'", "''") + "'"


def _build_ps_watcher_script(runner_cmd: list[str]) -> str:
    """Build lightweight PowerShell update + log/network autopilot watcher script for Windows."""
    exe = runner_cmd[0] if runner_cmd else "python"
    base_extra = list(runner_cmd[1:])
    auto_patch_args = base_extra + ["--auto-patch"]
    heal_args = base_extra + ["--autopilot-heal"]
    arg_list_literal = "@(" + ", ".join(_ps_quote('"' + str(a).replace('"', '\\"') + '"') for a in auto_patch_args) + ")"
    heal_list_literal = "@(" + ", ".join(_ps_quote('"' + str(a).replace('"', '\\"') + '"') for a in heal_args) + ")"

    return f"""$ErrorActionPreference = 'SilentlyContinue'
$mutexCreated = $false
$mutex = New-Object System.Threading.Mutex($false, 'Local\\AntigravityPatcher_WatcherLoop', [ref]$mutexCreated)
if (-not $mutexCreated) {{
    exit 0
}}

$RunnerExe = {_ps_quote(exe)}
$RunnerBaseArgs = {arg_list_literal}
$HealBaseArgs = {heal_list_literal}

function Test-CliActive {{
    try {{
        $cliMutex = $null
        if ([System.Threading.Mutex]::TryOpenExisting('Local\\AntigravityPatcher_SingleInstance', [ref]$cliMutex)) {{
            try {{ $cliMutex.Dispose() }} catch {{}}
            return $true
        }}
    }} catch {{}}
    return $false
}}

function Get-WatchedSignature {{
    $items = New-Object System.Collections.Generic.List[string]
    $fixedPaths = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\\Antigravity IDE\\resources\\app\\out\\main.js'),
        (Join-Path $env:LOCALAPPDATA 'Programs\\antigravity\\resources\\bin\\language_server.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\\antigravity\\resources\\app.asar'),
        (Join-Path $env:USERPROFILE '.gemini\\bin\\agy.exe'),
        (Join-Path $env:USERPROFILE '.gemini\\antigravity\\bin\\agy.exe'),
        (Join-Path $env:USERPROFILE '.antigravity_tools\\ag_desktop_ui.js'),
        (Join-Path $env:USERPROFILE '.gemini\\antigravity\\skills\\ag-swarm\\SKILL.md')
    )
    $cfgFile = Join-Path $env:USERPROFILE '.antigravity_tools\\auto_repatch.json'
    if (Test-Path -LiteralPath $cfgFile) {{
        try {{
            $cfg = Get-Content -LiteralPath $cfgFile -Raw | ConvertFrom-Json
            if (-not $cfg.enabled) {{ return 'DISABLED' }}
            if ($cfg.targets) {{
                foreach ($tp in $cfg.targets) {{
                    if ($tp -and ($fixedPaths -notcontains $tp)) {{ $fixedPaths += $tp }}
                }}
            }}
        }} catch {{}}
    }}
    foreach ($p in $fixedPaths) {{
        if ($p -and (Test-Path -LiteralPath $p)) {{
            $fi = Get-Item -LiteralPath $p -ErrorAction SilentlyContinue
            if ($fi) {{
                $items.Add("$p=$($fi.Length):$($fi.LastWriteTimeUtc.Ticks)")
            }}
        }} else {{
            $items.Add("$p=MISSING")
        }}
    }}
    $extRoot = Join-Path $env:USERPROFILE '.antigravity-ide\\extensions'
    $extDirs = @(Get-ChildItem -LiteralPath $extRoot -Filter 'energystar.ag-toolkit-*' -Directory -ErrorAction SilentlyContinue)
    $items.Add("ext_count=$($extDirs.Count)")
    $vscRoot = Join-Path $env:USERPROFILE '.vscode\\extensions'
    $vscDirs = @(Get-ChildItem -LiteralPath $vscRoot -Filter 'google.google-antigravity-*' -Directory -ErrorAction SilentlyContinue)
    $items.Add("vsc_count=$($vscDirs.Count)")
    return ($items -join '|')
}}

function Get-RuntimeHeartbeatSignature {{
    $parts = New-Object System.Collections.Generic.List[string]
    try {{
        $reg = Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' -ErrorAction SilentlyContinue
        $parts.Add("px=$($reg.ProxyEnable):$($reg.ProxyServer)")
    }} catch {{}}
    $logDirs = @(
        (Join-Path $env:APPDATA 'Antigravity\\logs'),
        (Join-Path $env:USERPROFILE '.gemini\\antigravity\\log')
    )
    foreach ($d in $logDirs) {{
        if ($d -and (Test-Path -LiteralPath $d)) {{
            $latest = Get-ChildItem -LiteralPath $d -Filter '*.log' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 2
            foreach ($f in $latest) {{
                $parts.Add("$($f.Name)=$($f.Length):$($f.LastWriteTimeUtc.Ticks)")
            }}
        }}
    }}
    $ideLogs = Join-Path $env:APPDATA 'Antigravity IDE\\logs'
    if (Test-Path -LiteralPath $ideLogs) {{
        $sess = Get-ChildItem -LiteralPath $ideLogs -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        if ($sess) {{
            $eh = Get-ChildItem -LiteralPath $sess.FullName -Recurse -Filter '*.log' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 3
            foreach ($f in $eh) {{
                $parts.Add("$($f.Name)=$($f.Length):$($f.LastWriteTimeUtc.Ticks)")
            }}
        }}
    }}
    return ($parts -join '|')
}}

function Wait-InstallerIdle {{
    for ($i = 0; $i -lt 40; $i++) {{
        $busy = Get-Process -ErrorAction SilentlyContinue | Where-Object {{
            $_.ProcessName -match '^(inno_updater|AntigravitySetup|Update)$'
        }}
        if (-not $busy) {{ break }}
        Start-Sleep -Seconds 3
    }}
}}

function Wait-SignatureSettled {{
    $last = Get-WatchedSignature
    for ($i = 0; $i -lt 5; $i++) {{
        Start-Sleep -Seconds 4
        $cur = Get-WatchedSignature
        if ($cur -eq $last) {{ break }}
        $last = $cur
    }}
}}

function Invoke-Runner([string[]]$cmdArgs) {{
    if (-not (Test-Path -LiteralPath $RunnerExe)) {{ return }}
    if (Test-CliActive) {{ return }}
    $env:AG_AUTO_REPATCH_SILENT = '1'
    try {{
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $RunnerExe
        $psi.Arguments = ($cmdArgs -join ' ')
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        $p = [System.Diagnostics.Process]::Start($psi)
        if ($p) {{ $p.WaitForExit() }}
    }} catch {{
        Start-Process -FilePath $RunnerExe -ArgumentList $cmdArgs -WindowStyle Hidden -Wait | Out-Null
    }}
}}

function Invoke-AutoPatch([bool]$relaunchIde, [bool]$relaunchMgr) {{
    if (-not (Test-Path -LiteralPath $RunnerExe)) {{ return }}
    Wait-InstallerIdle
    Wait-SignatureSettled
    Start-Sleep -Seconds 2
    if (Test-CliActive) {{ return }}
    $args = @()
    $args += $RunnerBaseArgs
    if ($relaunchIde) {{ $args += '"--relaunch-ide"' }}
    if ($relaunchMgr) {{ $args += '"--relaunch-mgr"' }}
    Invoke-Runner $args
}}

function Invoke-AutopilotHeal {{
    if (-not (Test-Path -LiteralPath $RunnerExe)) {{ return }}
    Wait-InstallerIdle
    Invoke-Runner $HealBaseArgs
}}

$sigCacheFile = Join-Path $env:USERPROFILE '.antigravity_tools\\auto_watch.sig'

function Read-SavedSignature {{
    if (Test-Path -LiteralPath $sigCacheFile) {{
        try {{ return (Get-Content -LiteralPath $sigCacheFile -Raw).Trim() }} catch {{}}
    }}
    return ''
}}

function Save-WatchedSignature([string]$val) {{
    try {{
        $dir = Split-Path -Parent $sigCacheFile
        if (-not (Test-Path -LiteralPath $dir)) {{ New-Item -ItemType Directory -Path $dir -Force | Out-Null }}
        [System.IO.File]::WriteAllText($sigCacheFile, $val, [System.Text.Encoding]::UTF8)
    }} catch {{}}
}}

$lastIdeSeen = $null
$lastMgrSeen = $null

function Update-SeenApps {{
    $now = Get-Date
    if ([System.Diagnostics.Process]::GetProcessesByName('Antigravity IDE').Length -gt 0) {{ $script:lastIdeSeen = $now }}
    if ([System.Diagnostics.Process]::GetProcessesByName('Antigravity').Length -gt 0) {{ $script:lastMgrSeen = $now }}
}}

try {{
    Update-SeenApps
    $sig = Get-WatchedSignature
    $savedSig = Read-SavedSignature
    if ($sig -ne 'DISABLED' -and $sig -ne $savedSig -and -not (Test-CliActive)) {{
        Invoke-AutoPatch $false $false
        $sig = Get-WatchedSignature
        Save-WatchedSignature $sig
    }} elseif ($sig -ne 'DISABLED' -and -not $savedSig) {{
        Save-WatchedSignature $sig
    }}

    $rtSig = Get-RuntimeHeartbeatSignature
    $lastHealCheck = Get-Date
    $tick = 0
    if ($sig -ne 'DISABLED' -and -not (Test-CliActive)) {{
        Invoke-AutopilotHeal
    }}

    while ($true) {{
        Start-Sleep -Seconds 20
        $tick++
        Update-SeenApps
        $cur = Get-WatchedSignature
        if ($cur -eq 'DISABLED') {{
            Start-Sleep -Seconds 30
            continue
        }}
        if ($cur -ne $sig) {{
            if (Test-CliActive) {{
                # Defer without overwriting $sig so pending updates still re-patch once Patcher UI exits
                continue
            }}
            $now = Get-Date
            $relaunchIde = ($null -ne $lastIdeSeen) -and ((($now - $lastIdeSeen).TotalSeconds) -le 180)
            $relaunchMgr = ($null -ne $lastMgrSeen) -and ((($now - $lastMgrSeen).TotalSeconds) -le 180)
            Invoke-AutoPatch $relaunchIde $relaunchMgr
            Update-SeenApps
            $sig = Get-WatchedSignature
            Save-WatchedSignature $sig
            $rtSig = Get-RuntimeHeartbeatSignature
            $lastHealCheck = Get-Date
            continue
        }}

        if (($tick % 3) -eq 0 -and -not (Test-CliActive)) {{
            $curRt = Get-RuntimeHeartbeatSignature
            $elapsedSec = ((Get-Date) - $lastHealCheck).TotalSeconds
            if (($curRt -ne $rtSig) -or ($elapsedSec -ge 300)) {{
                Invoke-AutopilotHeal
                $rtSig = Get-RuntimeHeartbeatSignature
                $lastHealCheck = Get-Date
            }}
        }}
    }}
}} finally {{
    try {{ $mutex.ReleaseMutex() }} catch {{}}
    $mutex.Dispose()
}}
"""


def _vbs_launcher_path() -> Path:
    return _tools_dir() / "auto_watch.vbs"


def _sig_cache_path() -> Path:
    return _tools_dir() / "auto_watch.sig"


def _write_hidden_vbs_wrapper(script_path: Path) -> Path:
    """Create a VBScript wrapper so Task Scheduler launches PowerShell with SW_HIDE (0 window flash)."""
    vbs_path = _vbs_launcher_path()
    vbs_path.parent.mkdir(parents=True, exist_ok=True)
    ps_cmd = (
        f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass '
        f'-File ""{str(script_path)}""'
    )
    vbs_text = (
        'Set shell = CreateObject("WScript.Shell")\r\n'
        f'shell.Run "{ps_cmd}", 0, False\r\n'
    )
    vbs_path.write_text(vbs_text, encoding="utf-8")
    return vbs_path


def _register_windows_task(script_path: Path) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Background update watcher task is only supported on Windows."

    vbs_path = _write_hidden_vbs_wrapper(script_path)
    vbs_arg = f'//B //Nologo "{vbs_path}"'
    for run_level in ("Highest", "Limited"):
        ps_cmd = (
            "$ErrorActionPreference = 'Stop'; "
            f"$act = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument {_ps_quote(vbs_arg)}; "
            "$trigLogon = New-ScheduledTaskTrigger -AtLogOn; "
            "$trigRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5); "
            "$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit (New-TimeSpan -Days 0) -MultipleInstances IgnoreNew; "
            f"$prin = New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" -LogonType Interactive -RunLevel {run_level}; "
            f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Action $act -Trigger @($trigLogon, $trigRepeat) -Settings $set -Principal $prin -Force | Out-Null; "
            f"Start-ScheduledTask -TaskName {_ps_quote(TASK_NAME)}"
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True, text=True, errors="ignore", timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0:
                return True, f"Scheduled task '{TASK_NAME}' active ({run_level})"
            last_msg = (res.stderr or res.stdout or "").strip()
        except Exception as ex:
            last_msg = str(ex)
    return False, last_msg or "Failed to register scheduled task."


def _unregister_windows_task() -> tuple[bool, str]:
    if os.name != "nt":
        return True, "Disabled."
    ps_cmd = (
        f"Stop-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue; "
        f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue; "
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -like '*auto_watch.ps1*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, errors="ignore", timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        return True, f"Scheduled task '{TASK_NAME}' removed"
    except Exception as ex:
        return False, str(ex)


def _stop_running_watcher_ps() -> None:
    if os.name != "nt":
        return
    ps_cmd = (
        f"Stop-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} -ErrorAction SilentlyContinue; "
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -like '*auto_watch.ps1*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, errors="ignore", timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def is_windows_task_registered() -> bool:
    if os.name != "nt":
        return False
    try:
        res = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, text=True, errors="ignore", timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_auto_repatch_status() -> dict[str, Any]:
    cfg = load_auto_repatch_config()
    task_ok = is_windows_task_registered() if os.name == "nt" else False
    return {
        "enabled": bool(cfg.get("enabled")),
        "user_disabled": bool(cfg.get("user_disabled")),
        "task_registered": task_ok,
        "runner": cfg.get("runner") or resolve_runner_command(),
        "script_path": str(_watcher_script_path()),
        "log_path": str(_log_path()),
    }


def enable_auto_repatch(
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
    run_initial_sync: bool = True,
    quiet: bool = False,
) -> tuple[bool, str]:
    """Enable automatic update watcher & optionally run catch-up patch immediately."""
    runner = resolve_runner_command()
    targets = [p for p in (main_js_path, manager_path, agy_path) if p]
    if manager_path:
        try:
            from patcher.manager.discovery import find_asar_relative_to_manager
            asar_p = find_asar_relative_to_manager(manager_path)
            if asar_p:
                targets.append(asar_p)
        except Exception:
            pass

    cfg = load_auto_repatch_config()
    cfg["enabled"] = True
    cfg["user_disabled"] = False
    cfg["runner"] = runner
    cfg["targets"] = targets
    cfg["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_auto_repatch_config(cfg)

    if run_initial_sync:
        run_auto_repatch(
            restart_running=True,
            main_js_path=main_js_path,
            manager_path=manager_path,
            agy_path=agy_path,
        )

    if os.name != "nt":
        if not quiet:
            ok("Auto-repatch configuration saved (scheduled background watcher is Windows-only; use '--auto-patch' on cron/launchd).")
        return True, "Saved configuration (use '--auto-patch')."

    _stop_running_watcher_ps()
    script_path = _watcher_script_path()
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_build_ps_watcher_script(runner), encoding="utf-8")

    reg_ok, reg_msg = _register_windows_task(script_path)
    if reg_ok:
        if not quiet:
            ok(f"Auto-patch on updates enabled ({reg_msg}).")
        _append_log(f"watcher enabled ({runner[0]})")
    else:
        if not quiet:
            warn(f"Could not register Windows watcher task: {reg_msg}")
    return reg_ok, reg_msg


def disable_auto_repatch(user_initiated: bool = True) -> tuple[bool, str]:
    cfg = load_auto_repatch_config()
    cfg["enabled"] = False
    cfg["user_disabled"] = bool(user_initiated)
    cfg["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_auto_repatch_config(cfg)

    ok_unreg, msg = _unregister_windows_task()
    for extra in (_watcher_script_path(), _vbs_launcher_path(), _sig_cache_path()):
        if extra.is_file():
            try:
                extra.unlink()
            except OSError:
                pass
    _append_log("watcher disabled by user" if user_initiated else "watcher paused (updates locked)")
    return ok_unreg, msg


def _has_any_patched_target(
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
) -> bool:
    """Return True if at least one Antigravity installation is currently patched."""
    try:
        st = inspect_targets(
            main_js_path=main_js_path,
            manager_path=manager_path,
            agy_path=agy_path,
        )
    except Exception:
        return False
    if st.get("main_js_path") and os.path.isfile(st["main_js_path"]) and not st.get("needs_ide"):
        return True
    if st.get("manager_path") and os.path.isfile(st["manager_path"]) and not st.get("needs_mgr"):
        return True
    if st.get("agy_path") and os.path.isfile(st["agy_path"]) and not st.get("needs_agy"):
        return True
    if st.get("vscode_path") and os.path.isfile(st["vscode_path"]) and not st.get("needs_vsc"):
        return True
    return False


def sync_auto_repatch_state(
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
) -> None:
    """Keep background auto-repatch enabled whenever any target is patched or updates are allowed.

    Acts as a second line of defense even when auto-updates are locked (`are_updates_allowed() == False`).
    """
    if os.name != "nt":
        return
    cfg = load_auto_repatch_config()
    any_patched = _has_any_patched_target(main_js_path, manager_path, agy_path)
    if not any_patched and not are_updates_allowed():
        return

    new_runner = resolve_runner_command()
    old_runner = cfg.get("runner") or []
    old_exe_valid = bool(
        old_runner
        and isinstance(old_runner, list)
        and old_runner[0]
        and str(old_runner[0]).lower().endswith(".exe")
        and os.path.isfile(old_runner[0])
    )
    runner_mismatch = (new_runner != old_runner) and (getattr(sys, "frozen", False) or not old_exe_valid)
    script_path = _watcher_script_path()
    vbs_path = _vbs_launcher_path()
    script_current = False
    if script_path.is_file() and vbs_path.is_file():
        try:
            txt = script_path.read_text(encoding="utf-8", errors="ignore")
            script_current = "Wait-SignatureSettled" in txt and "CreateNoWindow" in txt
            if script_current and "auto_watch.sig" in txt and "Invoke-AutopilotHeal" not in txt:
                script_current = False
        except OSError:
            script_current = False

    if (
        not cfg.get("enabled")
        or runner_mismatch
        or not script_current
        or not is_windows_task_registered()
    ):
        enable_auto_repatch(
            main_js_path=main_js_path,
            manager_path=manager_path,
            agy_path=agy_path,
            run_initial_sync=False,
            quiet=True,
        )


def refresh_auto_repatch_if_enabled(
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
) -> None:
    """Backwards-compatible alias synchronizing the automatic background update watcher."""
    sync_auto_repatch_state(
        main_js_path=main_js_path,
        manager_path=manager_path,
        agy_path=agy_path,
    )
