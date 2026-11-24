import os
import sys
import webbrowser
import locale

from patcher.constants import (
    COLOR_CYAN,
    COLOR_GREEN,
    COLOR_YELLOW,
    COLOR_RED,
    COLOR_BOLD,
    COLOR_DIM,
    COLOR_WHITE,
    COLOR_UNDERLINE,
    DOWNLOAD_URL,
    GITHUB_URL,
    VERSION,
)
from patcher.utils.console import (
    color,
    link,
    clear_screen,
    print_banner,
    print_menu_section,
    print_menu_row,
    print_menu_divider,
    print_menu_footer,
    info,
    hint,
    ok,
    warn,
    err,
    cancel,
)

from patcher.utils.file import file_size, format_bytes
from patcher.utils.update import check_for_updates, open_releases_page, print_update_status_notice

from patcher.ide.discovery import (
    find_install_root,
    find_main_js,
    get_ag_version,
    resolve_target_path,
)
from patcher.ide.patcher import is_already_patched, do_patch, do_restore
from patcher.agy.discovery import find_agy_binary, resolve_agy_path
from patcher.agy.patcher import is_already_patched as is_agy_patched, do_patch_agy, do_restore_agy

from patcher.manager.discovery import find_manager_binary, resolve_manager_path, get_antigravity_version
from patcher.manager.patcher import is_already_patched as is_mgr_patched, do_patch_manager, do_restore_manager

from patcher.vscode.discovery import (
    find_extension_js,
    resolve_extension_path,
    find_gemini_antigravity_binary,
    describe_gemini_binary_path,
)
from patcher.vscode.patcher import (
    is_already_patched as is_vscode_patched,
    do_patch_vscode,
    do_restore_vscode,
)
from patcher.utils.launcher import launch_application, sync_pinned_shortcuts

try:
    from patcher.app_updater import (
        get_installed_app_version,
        get_cached_update_info,
        check_and_install_component_update,
        start_background_update_checks,
        invalidate_app_update_cache,
    )
    _HAVE_APP_UPDATER = True
except ImportError:
    _HAVE_APP_UPDATER = False
    def get_installed_app_version(*_a, **_kw): return ""
    def get_cached_update_info(*_a, **_kw): return None
    def check_and_install_component_update(_t, p="", **_kw): return False, p, {}
    def start_background_update_checks(*_a, **_kw): return None
    def invalidate_app_update_cache(*_a, **_kw): return None

# --- New modules: updater lock, TLS fix, diagnostics ---
_UPDATER_LOCK_ERROR = None
try:
    from patcher.updater_lock import (
        do_toggle_updates, do_lock_updates, do_unlock_updates,
        is_update_locked, is_ide_locked, is_manager_locked, is_cli_locked, is_vscode_locked,
        toggle_ide_updates, toggle_manager_updates, toggle_cli_updates, toggle_vscode_updates,
        print_lock_status
    )
    _HAVE_UPDATER_LOCK = True
except ImportError as _e:
    _HAVE_UPDATER_LOCK = False
    _UPDATER_LOCK_ERROR = str(_e)
    def is_update_locked(*_a, **_kw): return False
    def is_ide_locked(*_a, **_kw): return False
    def is_manager_locked(*_a, **_kw): return False
    def is_cli_locked(*_a, **_kw): return False
    def is_vscode_locked(*_a, **_kw): return False

try:
    from patcher.tls_fix import do_tls_fix_inject, is_tls_fix_applied
    _HAVE_TLS_FIX = True
except ImportError:
    _HAVE_TLS_FIX = False

try:
    from patcher.diagnostics import (
        do_diagnostics,
        ensure_target_and_env_healthy,
        copy_sanitized_bug_report_to_clipboard,
    )
    _HAVE_DIAGNOSTICS = True
except ImportError:
    _HAVE_DIAGNOSTICS = False
    def ensure_target_and_env_healthy(*_a, **_kw):
        return {"ok": True, "errors": []}
    def copy_sanitized_bug_report_to_clipboard(*_a, **_kw):
        return False

try:
    from patcher.data_reset import do_data_reset
    _HAVE_DATA_RESET = True
except ImportError:
    _HAVE_DATA_RESET = False

_last_offer_cancelled = False


def _has_win_console_stdin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
    except Exception:
        return False


def pause(prompt="  Press Enter (or any key) to return to menu..."):
    if prompt:
        print(color(prompt, COLOR_DIM), end="", flush=True)
    if _has_win_console_stdin():
        try:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
            # Любая нажатая клавиша (Enter, 0, Пробел, Esc и т.д.) мгновенно возвращает в меню
            msvcrt.getch()
            while msvcrt.kbhit():
                msvcrt.getch()
            print()
            return
        except Exception:
            pass
    try:
        sys.stdin.readline()
    except Exception:
        try:
            input()
        except Exception:
            pass
    print()



def print_launch_examples():
    script_name = os.path.basename(sys.argv[0]) or "main.py"
    cmd = script_name if getattr(sys, "frozen", False) else f"python {script_name}"
    windows_example = f'{cmd} "C:\\Path\\To\\Antigravity IDE"'
    macos_example = f'{cmd} "/Applications/Antigravity IDE.app"'
    linux_example = f'{cmd} "/usr/share/antigravity-ide"'

    hint("Usage examples with custom path:")
    print(f"      Windows: {color(windows_example, COLOR_YELLOW)}")
    print(f"      macOS:   {color(macos_example, COLOR_YELLOW)}")
    print(f"      Linux:   {color(linux_example, COLOR_YELLOW)}")


def print_path_examples():
    windows_path = r"C:\Users\Name\AppData\Local\Programs\Antigravity IDE"
    macos_path = "/Applications/Antigravity IDE.app"
    linux_path = "/usr/share/antigravity-ide"

    hint("Path examples:")
    print(f"      Windows: {color(windows_path, COLOR_YELLOW)}")
    print(f"      macOS:   {color(macos_path, COLOR_YELLOW)}")
    print(f"      Linux:   {color(linux_path, COLOR_YELLOW)}")


def _read_console_line(prompt):
    print(prompt, end="", flush=True)

    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is None:
        return sys.stdin.readline().rstrip("\r\n")

    raw = stdin_buffer.readline()
    if not raw:
        return ""

    encodings = [
        sys.stdin.encoding,
        locale.getpreferredencoding(False),
        "utf-8",
        "cp1251",
        "latin-1",
    ]
    for encoding in [e for e in encodings if e]:
        try:
            return raw.decode(encoding).rstrip("\r\n")
        except UnicodeDecodeError:
            pass

    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


def prompt_yn(question):
    question = question.rstrip()
    prompt = f"  [?] {question} ({color('y', COLOR_GREEN)}/{color('n', COLOR_RED)}): "
    return _read_console_line(prompt).strip().lower()


def confirmed(question):
    """Возвращает True, если пользователь ответил 'y'."""
    return prompt_yn(question) in ("y", "yes", "\u0434", "\u0434\u0430")


def offer_download_and_block(software_name="Antigravity", target_type=None):
    err(f"{software_name} path is not set or software is not installed.")
    err("Patching attempt blocked.")
    hint(f"Download URL: {color(DOWNLOAD_URL, COLOR_CYAN)}")
    print()
    print_menu_row("1", "Open download page in browser", DOWNLOAD_URL, COLOR_GREEN)
    print_menu_row("2", f"Specify path to installed {software_name}", "manual path selection", COLOR_CYAN)
    print_menu_row("0", "Return to main menu", "", COLOR_RED)
    print()

    action = input(color("  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip().lower()
    if action in ("0", "", "b", "back", "q", "exit", "quit"):
        global _last_offer_cancelled
        _last_offer_cancelled = True
        cancel("Cancelled.")
        return None
    elif action == "1":
        webbrowser.open(DOWNLOAD_URL)
        ok(f"Opening: {color(DOWNLOAD_URL, COLOR_CYAN)}")
        return None
    elif action == "2":
        print()
        hint(f"Enter the path to {software_name} (or 0 / Enter to cancel).")
        print_path_examples()
        raw = input(color(f"\n  {software_name} Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
        if not raw or raw.lower() in ("0", "q", "exit", "back"):
            cancel("Cancelled.")
            return None
        if raw:
            if target_type == "ide" or software_name == "Antigravity IDE":
                new_path = resolve_target_path(raw)
                if new_path and os.path.isdir(new_path):
                    new_path = find_main_js(new_path)
                if new_path and os.path.isfile(new_path) and new_path.endswith("main.js"):
                    ok(f"{software_name} path updated!")
                    return new_path
                else:
                    err(f"Could not resolve a valid {software_name} target (main.js not found).")
            elif target_type == "manager" or software_name == "Antigravity 2.0":
                new_path = resolve_manager_path(raw)
                if new_path and os.path.isfile(new_path):
                    ok(f"{software_name} path updated!")
                    return new_path
                else:
                    err(f"Could not resolve a valid {software_name} target (language_server not found).")
            elif target_type == "agy" or software_name == "Antigravity CLI":
                new_path = resolve_agy_path(raw)
                if new_path and os.path.isfile(new_path):
                    ok(f"{software_name} path updated!")
                    return new_path
                else:
                    err(f"Could not resolve a valid {software_name} target.")
            else:
                p = resolve_target_path(raw) or resolve_manager_path(raw) or resolve_agy_path(raw)
                if p and os.path.exists(p):
                    ok("Path updated!")
                    return p
                else:
                    err("Invalid path provided.")
        return None
    else:
        cancel("Cancelled.")
        return None


def _kv(label, value_text, value_color):
    """Отформатированная пара ключ/значение с выравниванием меток."""
    print(f"      {label:<9}{color(value_text, value_color)}")


def print_target_info(main_js_path, manager_path="", agy_path="", vscode_path="", vscode_agy_path="", show_search_line=False):
    if show_search_line:
        info("Searching for installations...")

    # 1. Antigravity IDE Info
    print_menu_section("ANTIGRAVITY IDE")
    _kv("Target:", main_js_path if main_js_path else "Not found", COLOR_CYAN)
    if main_js_path and os.path.exists(main_js_path):
        try:
            with open(main_js_path, "r", encoding="utf-8") as f:
                content = f.read()
            _kv("Status:", "found", COLOR_GREEN)
            patched = is_already_patched(content)
            _kv("Patch:", "already patched" if patched else "not patched",
                COLOR_YELLOW if patched else COLOR_GREEN)
        except Exception:
            _kv("Status:", "unreadable", COLOR_RED)
            _kv("Patch:", "unreadable", COLOR_RED)

        ver_str, _ = get_ag_version(main_js_path)
        _kv("Version:", ver_str if ver_str else "not detected",
            COLOR_GREEN if ver_str else COLOR_YELLOW)

        size = file_size(main_js_path)
        _kv("Size:", format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv("Status:", "not found", COLOR_RED)

    print()

    # 2. Antigravity 2.0 Info
    print_menu_section("ANTIGRAVITY 2.0")
    _kv("Target:", manager_path if manager_path else "Not found", COLOR_CYAN)
    if manager_path and os.path.isfile(manager_path):
        _kv("Status:", "found", COLOR_GREEN)
        patched = is_mgr_patched(manager_path)
        _kv("Patch:", "already patched" if patched else "not patched",
            COLOR_YELLOW if patched else COLOR_GREEN)
        
        ver_str = get_antigravity_version(manager_path)
        _kv("Version:", ver_str if ver_str else "not detected",
            COLOR_GREEN if ver_str else COLOR_YELLOW)

        size = file_size(manager_path)
        _kv("Size:", format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv("Status:", "not found", COLOR_YELLOW)

    print()

    # 3. Antigravity CLI Info
    print_menu_section("ANTIGRAVITY CLI")
    _kv("Target:", agy_path if agy_path else "Not found", COLOR_CYAN)
    if agy_path and os.path.isfile(agy_path):
        _kv("Status:", "found", COLOR_GREEN)
        patched = is_agy_patched(agy_path)
        _kv("Patch:", "already patched" if patched else "not patched",
            COLOR_YELLOW if patched else COLOR_GREEN)
        size = file_size(agy_path)
        _kv("Size:", format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv("Status:", "not found", COLOR_YELLOW)

    print()

    # 4. Antigravity VS Code Extension Info (extension.js + бинарь agy)
    print_menu_section("ANTIGRAVITY VS CODE EXTENSION")
    _kv("Target:", vscode_path if vscode_path else "Not found", COLOR_CYAN)
    if vscode_path and os.path.isfile(vscode_path):
        _kv("Status:", "found", COLOR_GREEN)
        try:
            with open(vscode_path, "r", encoding="utf-8") as f:
                content = f.read()
            patched = is_vscode_patched(content)
            _kv("Patch:", "already patched" if patched else "not patched",
                COLOR_YELLOW if patched else COLOR_GREEN)
        except Exception:
            _kv("Patch:", "unreadable", COLOR_RED)
        size = file_size(vscode_path)
        _kv("Size:", format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv("Status:", "not found", COLOR_YELLOW)

    print()

    _kv("AGY Bin:", vscode_agy_path if vscode_agy_path else "Not found", COLOR_CYAN)
    if vscode_agy_path and os.path.isfile(vscode_agy_path):
        _kv("Status:", "found", COLOR_GREEN)
        patched = is_agy_patched(vscode_agy_path)
        _kv("Patch:", "already patched" if patched else "not patched",
            COLOR_YELLOW if patched else COLOR_GREEN)
        size = file_size(vscode_agy_path)
        _kv("Size:", format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv("Status:", "not found", COLOR_YELLOW)


def show_about():
    clear_screen()
    print_banner()
    print_menu_section("ABOUT ANTIGRAVITY PATCHER")
    print()
    print(f"  {color('Antigravity Patcher by energystar', COLOR_BOLD, COLOR_CYAN)} v{VERSION}")
    print("  Open-source diagnostics, network routing, and extension toolkit for Antigravity:")
    print("  Antigravity IDE, Antigravity 2.0 (language_server), and Antigravity CLI (agy).")
    print("  Provides selective DNS routing, quota visibility, and runtime diagnostics without VPN.")
    print()
    hint("Features:")
    print(f"   • {color('Antigravity IDE config', COLOR_GREEN)} — main.js diagnostics, dialog & TLS support")
    print(f"   • {color('Antigravity 2.0 support', COLOR_GREEN)} — language_server & Desktop UI integration")
    print(f"   • {color('Antigravity CLI support', COLOR_GREEN)} — agy CLI environment compatibility")
    print(f"   • {color('Auto-launch Apps', COLOR_GREEN)}     — automatically opens configured application")
    print(f"   • {color('Auto-update Lock', COLOR_GREEN)}    — granular lock for IDE, 2.0 & VS Code")
    print(f"   • {color('Backup & Restore', COLOR_GREEN)}     — safe, fully reversible modifications")
    print()
    hint("Author & Community:")
    tg_main = link("https://t.me/energystar6", "@energystar6", COLOR_CYAN, COLOR_UNDERLINE)
    gh_link = link("https://github.com/EnergyStario/antigravity-patcher", "github.com/EnergyStario/antigravity-patcher", COLOR_CYAN, COLOR_UNDERLINE)

    print(f"   • Author:   {color('energystar', COLOR_YELLOW)}")
    print(f"   • Telegram: {tg_main}")
    print(f"   • GitHub:   {gh_link}")
    print()
    hint("License:")
    print("   • GPL-3.0 License")
    print()
    print_menu_row("0", "Back to main menu", "return to patcher", COLOR_RED)



_TARGET_STATUS_CACHE = {}
_DISK_CACHE_LOADED = False
_MENU_STATUS_CACHE = {
    "update_locked": None,
    "nrpt_status": None,
    "hosts_pinned": None,
}


def _status_cache_file():
    from pathlib import Path
    return Path.home() / ".antigravity_tools" / "patch_status_cache.json"


def _load_disk_status_cache() -> None:
    global _DISK_CACHE_LOADED
    if _DISK_CACHE_LOADED:
        return
    _DISK_CACHE_LOADED = True
    try:
        p = _status_cache_file()
        if not p.is_file():
            return
        import json
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for path_str, rec in raw.items():
                if isinstance(rec, dict) and "mtime_ns" in rec and "size" in rec and "patched" in rec:
                    _TARGET_STATUS_CACHE[(path_str, int(rec["mtime_ns"]), int(rec["size"]))] = bool(rec["patched"])
    except Exception:
        pass


def _save_disk_status_cache() -> None:
    try:
        p = _status_cache_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        out = {}
        for k, patched in _TARGET_STATUS_CACHE.items():
            if isinstance(k, tuple) and len(k) == 3:
                path_str, mtime_ns, size = k
                out[str(path_str)] = {"mtime_ns": int(mtime_ns), "size": int(size), "patched": bool(patched)}
        import json
        p.write_text(json.dumps(out), encoding="utf-8")
    except Exception:
        pass


def invalidate_menu_status_cache(key=None):
    """Invalidate cached menu status checks (all keys or a specific key)."""
    if key is None:
        for k in _MENU_STATUS_CACHE:
            _MENU_STATUS_CACHE[k] = None
    elif key in _MENU_STATUS_CACHE:
        _MENU_STATUS_CACHE[key] = None


def _get_cached_update_locked() -> bool:
    val = _MENU_STATUS_CACHE.get("update_locked")
    if val is None:
        val = bool(is_update_locked())
        _MENU_STATUS_CACHE["update_locked"] = val
    return val


def _get_cached_nrpt_status() -> dict:
    val = _MENU_STATUS_CACHE.get("nrpt_status")
    if val is None:
        from patcher.nrpt_dns import get_nrpt_status, is_nrpt_supported
        val = get_nrpt_status() if is_nrpt_supported() else {}
        _MENU_STATUS_CACHE["nrpt_status"] = val
    return val


def _get_cached_hosts_pinned() -> tuple:
    val = _MENU_STATUS_CACHE.get("hosts_pinned")
    if val is None:
        from patcher.hosts_pin import is_hosts_pinned
        val = is_hosts_pinned()
        _MENU_STATUS_CACHE["hosts_pinned"] = val
    return val


def _run_env_heal(target: str = "all", quiet: bool = False, main_js_path: str = "", manager_path: str = "", agy_path: str = "") -> None:
    try:
        ensure_target_and_env_healthy(
            target=target,
            quiet=quiet,
            main_js_path=main_js_path,
            manager_path=manager_path,
            agy_path=agy_path,
        )
    except Exception as exc:
        import traceback
        err(f"Auto-heal error ({target}): {exc}")
        copy_sanitized_bug_report_to_clipboard(
            traceback.format_exc(),
            context_info=f"cli auto-heal target={target}",
        )
    finally:
        invalidate_menu_status_cache()


def _resolve_ide_root_for_lock(main_js_path: str) -> str:
    if not main_js_path:
        return ""
    from pathlib import Path

    p = Path(main_js_path)
    for _ in range(5):
        if (p / "Antigravity IDE.exe").is_file():
            return str(p)
        p = p.parent
    return ""


def _target_ver_label(target_key: str, path: str, unlocked: bool) -> tuple[str, str]:
    """Return (ver_tag_for_status_bar, hint_suffix_for_menu_row)."""
    if not path or not os.path.isfile(path) or not _HAVE_APP_UPDATER:
        return "", ""
    ver = get_installed_app_version(target_key, path)
    ver_tag = f" v{ver}" if ver else ""
    upd_tag = ""
    hint_extra = f"v{ver}" if ver else ""
    cached = get_cached_update_info(target_key)
    if cached and cached.get("update_available") and cached.get("latest_version"):
        new_v = cached["latest_version"]
        upd_tag = " " + color(f"(-> v{new_v}!)", COLOR_CYAN)
        hint_extra = f"v{ver} -> v{new_v} available" if ver else f"v{new_v} available"
    elif unlocked and ver:
        hint_extra = f"v{ver}, auto-upd ON"
    elif ver:
        hint_extra = f"v{ver}, lock ON"
    return ver_tag + upd_tag, hint_extra


def _is_target_unlocked(target_key: str, main_js_path: str = "") -> bool:
    if not _HAVE_UPDATER_LOCK:
        return True
    if target_key == "ide":
        ide_root = _resolve_ide_root_for_lock(main_js_path)
        return not is_ide_locked(ide_root or None)
    if target_key == "manager":
        return not is_manager_locked()
    if target_key == "cli":
        return not is_cli_locked()
    return False


def _maybe_upgrade_unlocked_target(target_key: str, current_path: str, main_js_path: str = "") -> tuple[bool, str]:
    """Check official feed and silently install newer release (even if Google's native updater was disabled/locked)."""
    if not _HAVE_APP_UPDATER or not current_path or not os.path.isfile(current_path):
        return False, current_path
    try:
        updated, new_path, _ = check_and_install_component_update(
            target_key,
            current_path,
            is_locked_fn=None,
            quiet=False,
            auto_install=True,
        )
        if updated:
            invalidate_menu_status_cache()
            _TARGET_STATUS_CACHE.clear()
            _save_disk_status_cache()
        return bool(updated), (new_path or current_path)
    except Exception:
        return False, current_path


def _apply_patch_deploy_and_launch_for_target(
    target_key: str,
    target_path: str,
    launch_after: bool = True,
) -> bool:
    """Re-patch, deploy extension/UI, heal environment, and launch a newly updated target."""
    if not target_path or not os.path.isfile(target_path):
        return False
    if target_key == "ide":
        from patcher.ide.patcher import ensure_agy_backend_patched
        from patcher.extension_installer import deploy_assistant_extension

        do_patch(target_path, show_search_line=False)
        deploy_assistant_extension(export_vsix=False)
        _run_env_heal(target="ide", quiet=False, main_js_path=target_path)
        if launch_after:
            launch_application("ide", target_path)
            ensure_agy_backend_patched(wait_seconds=3.0)
        return True
    if target_key == "manager":
        from patcher.extension_installer import deploy_antigravity_2_ui

        do_patch_manager(target_path)
        if is_mgr_patched(target_path):
            deploy_antigravity_2_ui()
            _run_env_heal(target="manager", quiet=False, manager_path=target_path)
            if launch_after:
                launch_application("manager", target_path)
            return True
        return False
    if target_key == "cli":
        do_patch_agy(target_path)
        _run_env_heal(target="cli", quiet=False, agy_path=target_path)
        return True
    return False


def _cached_target_badge(label, path, checker_fn, is_text=False, target_key="", unlocked=False):
    if not path or not os.path.isfile(path):
        return f"{label}: " + color("—", COLOR_DIM)
    _load_disk_status_cache()
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        if key in _TARGET_STATUS_CACHE:
            patched = _TARGET_STATUS_CACHE[key]
        else:
            if is_text:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    patched = bool(checker_fn(f.read()))
            else:
                patched = bool(checker_fn(path))
            _TARGET_STATUS_CACHE[key] = patched
            _save_disk_status_cache()
        ver_suffix, _ = _target_ver_label(target_key, path, unlocked) if target_key else ("", "")
        state_str = color("OK", COLOR_GREEN) if patched else color("NEED PATCH", COLOR_YELLOW)
        return f"{label}{ver_suffix}: {state_str}"
    except Exception:
        return f"{label}: " + color("?", COLOR_RED)


def redraw_main_screen(main_js_path, manager_path="", agy_path="", vscode_path="", vscode_agy_path="", show_search_line=False):
    clear_screen()
    print_banner()
    print_update_status_notice()
    _load_disk_status_cache()
    ide_root = _resolve_ide_root_for_lock(main_js_path)
    ide_unlocked = bool(_HAVE_UPDATER_LOCK and not is_ide_locked(ide_root or None))
    mgr_unlocked = bool(_HAVE_UPDATER_LOCK and not is_manager_locked())
    cli_unlocked = bool(_HAVE_UPDATER_LOCK and not is_cli_locked())

    badge_specs = [
        ("IDE", main_js_path, is_already_patched, True, "ide", ide_unlocked),
        ("2.0", manager_path, is_mgr_patched, False, "manager", mgr_unlocked),
        ("CLI", agy_path, is_agy_patched, False, "cli", cli_unlocked),
    ]
    if vscode_path and os.path.isfile(vscode_path):
        badge_specs.append(("VSC", vscode_path, is_vscode_patched, True, "", False))

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(badge_specs)) as pool:
        parts = list(pool.map(lambda s: _cached_target_badge(*s), badge_specs))

    if not ide_unlocked and not mgr_unlocked:
        lock_tag = color("LOCKED", COLOR_GREEN)
    elif ide_unlocked and mgr_unlocked:
        lock_tag = color("OPEN", COLOR_YELLOW)
    else:
        open_list = []
        if ide_unlocked:
            open_list.append("IDE")
        if mgr_unlocked:
            open_list.append("2.0")
        lock_tag = color(f"OPEN ({'/'.join(open_list)})", COLOR_YELLOW)
    print(f"  Status:  {'  |  '.join(parts)}  |  Updates: {lock_tag}")
    print()


def set_vscode_path(new_path):
    """Колбэк для do_patch_vscode_flow — обновляет локальную переменную vscode_path
    в кадре run_cli (через sys._getframe)."""
    frame = sys._getframe(2)
    if frame.f_code.co_name == "run_cli":
        frame.f_locals["vscode_path"] = new_path


def set_vscode_agy_path(new_path):
    """Колбэк для do_patch_vscode_flow — обновляет локальную переменную vscode_agy_path
    в кадре run_cli (через sys._getframe)."""
    frame = sys._getframe(2)
    if frame.f_code.co_name == "run_cli":
        frame.f_locals["vscode_agy_path"] = new_path


def do_patch_vscode_flow(vscode_path, vscode_agy_path, set_js_cb=None, set_agy_cb=None, silent_if_missing=False):
    """Патчит расширение google.google-antigravity (extension.js) и бинарь
    ~/.gemini/bin/antigravity (agy) через agy-патчер.

    Патч блокируется, пока не найдены ОБА файла: extension.js и бинарь agy."""
    from patcher.agy.patcher import do_patch_agy

    # --- 0. Предварительный поиск: оба файла должны существовать ---
    if not vscode_path or not os.path.isfile(vscode_path):
        vscode_path = find_extension_js()
        if vscode_path and set_js_cb:
            set_js_cb(vscode_path)

    if not vscode_agy_path or not os.path.isfile(vscode_agy_path):
        vscode_agy_path = find_gemini_antigravity_binary()
        if vscode_agy_path and set_agy_cb:
            set_agy_cb(vscode_agy_path)

    missing = []
    if not vscode_path or not os.path.isfile(vscode_path):
        missing.append("extension.js (VS Code extension 'google.google-antigravity')")
    if not vscode_agy_path or not os.path.isfile(vscode_agy_path):
        missing.append(f"agy binary ({describe_gemini_binary_path()})")

    if missing:
        if silent_if_missing:
            hint("Antigravity VS Code extension not installed — skipping")
            return False
        err("Antigravity VS Code Patch blocked — required files not found:")
        for m in missing:
            err(f"  - {m}")
        hint("Install the 'Google Antigravity' extension in VS Code first")
        hint("(Extensions view -> search 'Antigravity' -> Install),")
        hint("run it once — it downloads the binary to ~/.gemini/bin automatically.")
        return False

    # --- 1. Патчим extension.js ---
    do_patch_vscode(vscode_path)

    # --- 2. Патчим бинарь ~/.gemini/bin/antigravity (agy-патчем) ---
    print()
    info(f"Found downloaded binary: {color(vscode_agy_path, COLOR_CYAN)}")
    do_patch_agy(vscode_agy_path)
    return True


def run_cli():
    invalidate_menu_status_cache()
    main_js_path = ""
    manager_path = ""
    agy_path = ""
    vscode_path = ""
    vscode_agy_path = ""
    searched = False

    # 1. Проверяем аргументы командной строки
    if len(sys.argv) > 1:
        if "--auto-patch" in sys.argv[1:]:
            try:
                _run_env_heal(target="all", quiet=True)
                from patcher.auto_repatch import run_auto_repatch
                run_auto_repatch(restart_running=True)
            except Exception as exc:
                import traceback
                err(f"Auto-patch failed: {exc}")
                copy_sanitized_bug_report_to_clipboard(
                    traceback.format_exc(),
                    context_info="cli --auto-patch",
                )
            return
        args = [a for a in sys.argv[1:] if a not in ("--rollback", "-r")]
        if args:
            arg = " ".join(args)
            # Пытаемся определить тип цели
            ide_path = resolve_target_path(arg)
            if ide_path and os.path.isdir(ide_path):
                ide_path = find_main_js(ide_path)
            
            if ide_path and os.path.isfile(ide_path) and ide_path.endswith("main.js"):
                main_js_path = ide_path
            else:
                # Пробуем как Antigravity 2.0 (Manager / language_server)
                mgr_path = resolve_manager_path(arg)
                if mgr_path and os.path.isfile(mgr_path):
                    manager_path = mgr_path
                else:
                    # Пробуем как CLI (agy)
                    agy_p = resolve_agy_path(arg)
                    if agy_p and os.path.isfile(agy_p):
                        agy_path = agy_p
                    else:
                        err(f"Provided path does not exist or invalid: {arg}")

    # 2. Проверяем текущую директорию (для Antigravity IDE)
    if not main_js_path and not manager_path and not agy_path:
        local = os.path.join(os.getcwd(), "main.js")
        if os.path.exists(local):
            main_js_path = local
            info("Found main.js in current directory")

    # 3. Авто-поиск в системе (быстрый, без блокирующих задержек)
    if not main_js_path and not manager_path and not agy_path:
        ide_root = find_install_root()
        if ide_root:
            main_js_path = find_main_js(ide_root)

        manager_path = find_manager_binary()
        agy_path = find_agy_binary()

    # VS Code extension и его бинарь agy ищем всегда
    if not vscode_path:
        vscode_path = find_extension_js()
    if not vscode_agy_path:
        vscode_agy_path = find_gemini_antigravity_binary()

    # Автоматическая неблокирующая проверка и автозагрузка обновлений патчера в фоновом daemon-потоке
    try:
        import threading
        from patcher.utils.update import auto_update_on_startup

        def _bg_update_check():
            try:
                auto_update_on_startup()
            except Exception:
                pass

        threading.Thread(
            target=_bg_update_check,
            name="ag-cli-auto-update",
            daemon=True,
        ).start()
    except Exception:
        pass

    try:
        import threading
        from patcher.auto_repatch import sync_auto_repatch_state
        _sync_fn = sync_auto_repatch_state
        threading.Thread(
            target=lambda: _sync_fn(main_js_path, manager_path, agy_path),
            name="ag-watcher-sync",
            daemon=True,
        ).start()
    except Exception:
        pass

    if _HAVE_APP_UPDATER:
        try:
            start_background_update_checks(
                main_js_path=main_js_path,
                manager_path=manager_path,
                agy_path=agy_path,
                unlocked_targets={
                    "ide": _is_target_unlocked("ide", main_js_path),
                    "manager": _is_target_unlocked("manager", main_js_path),
                    "cli": _is_target_unlocked("cli", main_js_path),
                },
            )
        except Exception:
            pass

    # Full Autopilot on startup: if any installed target needs patching or environment healing, fix silently now
    if any([main_js_path, manager_path, agy_path, vscode_path, vscode_agy_path]) and os.environ.get("AG_DISABLE_STARTUP_AUTOPILOT") != "1":
        try:
            from patcher.auto_repatch import inspect_targets, run_auto_repatch
            _st = inspect_targets(
                main_js_path=main_js_path,
                manager_path=manager_path,
                agy_path=agy_path,
                vscode_path=vscode_path,
                vscode_agy_path=vscode_agy_path,
            )
            if _st.get("needs_any"):
                info("Автопилот: обнаружены неподготовленные компоненты — применяю исправления автоматически...")
                run_auto_repatch(
                    restart_running=True,
                    main_js_path=main_js_path,
                    manager_path=manager_path,
                    agy_path=agy_path,
                    vscode_path=vscode_path,
                    vscode_agy_path=vscode_agy_path,
                )
                invalidate_menu_status_cache()
                _TARGET_STATUS_CACHE.clear()
            else:
                # Even when binaries are already patched, verify & heal proxy / NRPT DNS / TLS / locks
                _hl = ensure_target_and_env_healthy(
                    target="all",
                    quiet=True,
                    main_js_path=main_js_path,
                    manager_path=manager_path,
                    agy_path=agy_path,
                ) or {}
                if _hl.get("proxy_healed") or _hl.get("tls_healed") or _hl.get("nrpt_auto_enabled"):
                    ok("Автопилот: сетевое окружение (прокси / TLS / NRPT DNS) автоматически восстановлено.")
                    invalidate_menu_status_cache()
                    _TARGET_STATUS_CACHE.clear()
        except Exception:
            pass

    redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path=vscode_path,
                       vscode_agy_path=vscode_agy_path, show_search_line=False)

    while True:
        _, _ide_hint_ver = _target_ver_label("ide", main_js_path, _is_target_unlocked("ide", main_js_path))
        _, _mgr_hint_ver = _target_ver_label("manager", manager_path, _is_target_unlocked("manager", main_js_path))
        _, _cli_hint_ver = _target_ver_label("cli", agy_path, _is_target_unlocked("cli", main_js_path))
        print_menu_section("PATCH")
        print_menu_row("1", "Apply all patches", "check updates, apply all patches & relaunch", COLOR_GREEN)
        print_menu_row("2", "Antigravity IDE patch", f"configure IDE runtime ({_ide_hint_ver})" if _ide_hint_ver else "configure IDE runtime", COLOR_GREEN)
        print_menu_row("3", "Antigravity 2.0 patch", f"patch language_server & UI ({_mgr_hint_ver})" if _mgr_hint_ver else "patch language_server binary & Desktop UI", COLOR_GREEN)
        print_menu_row("4", "Antigravity CLI (agy) patch", f"configure agy CLI ({_cli_hint_ver})" if _cli_hint_ver else "configure agy CLI", COLOR_GREEN)
        print_menu_row("5", "Antigravity VS Code Patch", "patch google.google-antigravity extension.js + agy binary", COLOR_GREEN)

        print_menu_section("RESTORE")
        print_menu_row("6", "Restore all from backup", "restore all available backups", COLOR_YELLOW)
        print_menu_row("7", "Antigravity 2.0", "from backup", COLOR_YELLOW)
        print_menu_row("8", "Antigravity CLI", "from backup", COLOR_YELLOW)
        print_menu_row("9", "Antigravity VS Code extension", "from backup", COLOR_YELLOW)

        print_menu_section("EXTRAS")
        cached_locked = _get_cached_update_locked()
        lock_label = "Unlock all auto-updates" if cached_locked else "Lock all auto-updates"
        lock_hint  = "unblock IDE, 2.0 & VS Code updaters" if cached_locked else "block IDE, 2.0 & VS Code updaters"
        print_menu_row("10", lock_label, lock_hint, COLOR_YELLOW)
        print_menu_row("11", "TLS certificate fix", "fix expired localhost TLS cert", COLOR_YELLOW)
        print_menu_row("12", "Diagnostics / Auto-fix", "analyze logs & auto-fix errors", COLOR_YELLOW)
        print_menu_row("13", "Reset data / Fix 429", "reset IDE data folder, preserve chats", COLOR_YELLOW)

        # 14. Selective DNS routing indicator (cached in memory between menu redraws)
        from patcher.nrpt_dns import is_nrpt_supported
        nrpt = _get_cached_nrpt_status()
        hosts_act, hosts_ip = _get_cached_hosts_pinned()
        if hosts_act and nrpt.get("active"):
            dns_status = "ACTIVE (NRPT + Hosts)"
            dns_color = COLOR_GREEN
        elif hosts_act:
            dns_status = f"ACTIVE (Hosts: {hosts_ip})"
            dns_color = COLOR_GREEN
        elif nrpt.get("active"):
            dns_status = f"ACTIVE ({nrpt.get('server', 'Unblock DNS')})"
            dns_color = COLOR_GREEN
        elif not is_nrpt_supported():
            dns_status = "UNSUPPORTED"
            dns_color = COLOR_DIM
        else:
            dns_status = "DISABLED"
            dns_color = COLOR_YELLOW
        print_menu_row("14", f"Selective DNS & Routing [{color(dns_status, dns_color)}]", "route 400 endpoints without VPN (NRPT/Hosts)", COLOR_YELLOW)

        print_menu_section("TOOLS")
        print_menu_row("15", "Open GitHub repository", "source & updates", COLOR_CYAN)
        print_menu_row("16", "Select custom path", "override auto-detected target", COLOR_CYAN)
        print_menu_row("17", "About program", "information & author links", COLOR_CYAN)
        print_menu_row("18", "Install Antigravity Assistant", "deploy extension & export VSIX", COLOR_CYAN)
        print_menu_row("19", "Check for cloud updates", "auto-download & apply latest release", COLOR_CYAN)
        print_menu_row("20", "Rebuild & repair chat history", "restore missing/ordered conversations", COLOR_CYAN)

        print()
        print_menu_row("0", "Exit", "quit the patcher", COLOR_RED)
        print_menu_footer("Tip: patches are reversible — use RESTORE any time.")

        choice = input(color("\n  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip().lower()
        print()

        if choice in ("0", "q", "quit", "exit"):
            return

        # Пустой ввод или команда "back" в главном меню — не выходим, просто перерисовываем меню
        if choice in ("", "back", "b"):
            redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                               vscode_agy_path, show_search_line=False)
            continue

        valid_choices = {str(i) for i in range(1, 21)}
        if choice not in valid_choices:
            err("Invalid choice")
            print()
            pause()
            redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                               vscode_agy_path, show_search_line=False)
            continue

        handled = True
        clear_screen()
        print_banner()

        if choice == "1":
            handled = True
            try:
                info("Applying all patches in sequence...")
                print()

                from patcher.utils.launcher import detect_running_antigravity_apps
                was_running_1 = detect_running_antigravity_apps()

                # Авто-поиск путей перед применением всех патчей
                if not main_js_path or not os.path.isfile(main_js_path):
                    _r = find_install_root()
                    if _r:
                        main_js_path = find_main_js(_r) or ""

                if not manager_path or not os.path.isfile(manager_path):
                    manager_path = find_manager_binary() or ""

                if not agy_path or not os.path.isfile(agy_path):
                    agy_path = find_agy_binary() or ""

                if not vscode_path or not os.path.isfile(vscode_path):
                    vscode_path = find_extension_js() or ""

                if not vscode_agy_path or not os.path.isfile(vscode_agy_path):
                    vscode_agy_path = find_gemini_antigravity_binary() or ""

                # 1/5 - IDE patch
                has_other_installed = bool(
                    (manager_path and os.path.isfile(manager_path)) or
                    (agy_path and os.path.isfile(agy_path)) or
                    (vscode_path and os.path.isfile(vscode_path))
                )
                if not main_js_path or not os.path.isfile(main_js_path):
                    if not has_other_installed:
                        new_p = offer_download_and_block("Antigravity IDE", target_type="ide")
                        if new_p:
                            main_js_path = new_p

                if main_js_path and os.path.isfile(main_js_path):
                    print_menu_section("1 / 5 - Antigravity IDE patch")
                    _, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                    do_patch(main_js_path)
                else:
                    warn("IDE (main.js) not found - skipping (use TOOLS -> 16 to set path)")

                # 2/5 - Manager patch
                if manager_path and os.path.isfile(manager_path):
                    print_menu_section("2 / 5 - Antigravity 2.0 patch")
                    _, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                    do_patch_manager(manager_path)
                else:
                    warn("Antigravity 2.0 (language_server) not found - skipping")

                # 3/5 - CLI patch
                if not agy_path or not os.path.isfile(agy_path):
                    agy_path = find_agy_binary() or ""
                if agy_path and os.path.isfile(agy_path):
                    print_menu_section("3 / 5 - Antigravity CLI (agy) patch")
                    _, agy_path = _maybe_upgrade_unlocked_target("cli", agy_path, main_js_path)
                    do_patch_agy(agy_path)
                else:
                    warn("Antigravity CLI (agy) not found - skipping")

                # 4/5 - VS Code - same flow as choice "5"
                print_menu_section("4 / 5 - Antigravity VS Code patch")
                do_patch_vscode_flow(vscode_path, vscode_agy_path,
                                     set_js_cb=None, set_agy_cb=None,
                                     silent_if_missing=True)
                print()

                # 5/5 - Antigravity Assistant Extension
                print_menu_section("5 / 5 - Antigravity Assistant Extension")
                from patcher.extension_installer import deploy_assistant_extension
                deploy_assistant_extension(export_vsix=False)
                print()
                ok("All available patches and extension applied.")

                _run_env_heal(
                    target="all",
                    quiet=False,
                    main_js_path=main_js_path,
                    manager_path=manager_path,
                    agy_path=agy_path,
                )

                from patcher.ide.patcher import ensure_agy_backend_patched
                launched_any = False
                if main_js_path and os.path.isfile(main_js_path):
                    if launch_application("ide", main_js_path):
                        launched_any = True
                        ensure_agy_backend_patched(wait_seconds=3.0)
                if manager_path and os.path.isfile(manager_path) and (was_running_1.get("manager") or not launched_any):
                    if launch_application("manager", manager_path):
                        launched_any = True
                if not launched_any:
                    hint("You can now start Antigravity normally.")
            except Exception as exc:
                import traceback
                err(f"Unexpected error while applying all patches: {exc}")
                copy_sanitized_bug_report_to_clipboard(
                    traceback.format_exc(),
                    context_info="choice=1 Apply all patches",
                )
        elif choice == "2":
            try:
                from patcher.ide.patcher import ensure_agy_backend_patched
                if main_js_path and os.path.isfile(main_js_path):
                    _, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                    do_patch(main_js_path, show_search_line=False)
                    from patcher.extension_installer import deploy_assistant_extension
                    deploy_assistant_extension(export_vsix=False)
                    _run_env_heal(target="ide", quiet=False, main_js_path=main_js_path)
                    launch_application("ide", main_js_path)
                    ensure_agy_backend_patched(wait_seconds=3.0)
                else:
                    new_p = offer_download_and_block("Antigravity IDE", target_type="ide")
                    if new_p:
                        main_js_path = new_p
                        _, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                        do_patch(main_js_path, show_search_line=False)
                        from patcher.extension_installer import deploy_assistant_extension
                        deploy_assistant_extension(export_vsix=False)
                        _run_env_heal(target="ide", quiet=False, main_js_path=main_js_path)
                        launch_application("ide", main_js_path)
                        ensure_agy_backend_patched(wait_seconds=3.0)
            except Exception as exc:
                import traceback
                err(f"Unexpected error patching Antigravity IDE: {exc}")
                copy_sanitized_bug_report_to_clipboard(
                    traceback.format_exc(),
                    context_info="choice=2 Antigravity IDE patch",
                )
        elif choice == "3":
            try:
                if manager_path and os.path.isfile(manager_path):
                    _, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                    do_patch_manager(manager_path)
                    if is_mgr_patched(manager_path):
                        from patcher.extension_installer import deploy_antigravity_2_ui
                        deploy_antigravity_2_ui()
                        _run_env_heal(target="manager", quiet=False, manager_path=manager_path)
                        launch_application("manager", manager_path)
                else:
                    new_p = offer_download_and_block("Antigravity 2.0", target_type="manager")
                    if new_p:
                        manager_path = new_p
                        _, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                        do_patch_manager(manager_path)
                        if is_mgr_patched(manager_path):
                            from patcher.extension_installer import deploy_antigravity_2_ui
                            deploy_antigravity_2_ui()
                            _run_env_heal(target="manager", quiet=False, manager_path=manager_path)
                            launch_application("manager", manager_path)
            except Exception as exc:
                import traceback
                err(f"Unexpected error patching Antigravity 2.0: {exc}")
                copy_sanitized_bug_report_to_clipboard(
                    traceback.format_exc(),
                    context_info="choice=3 Antigravity 2.0 patch",
                )
        elif choice == "4":
            try:
                if agy_path and os.path.isfile(agy_path):
                    _, agy_path = _maybe_upgrade_unlocked_target("cli", agy_path, main_js_path)
                    do_patch_agy(agy_path)
                    _run_env_heal(target="cli", quiet=False, agy_path=agy_path)
                else:
                    new_p = offer_download_and_block("Antigravity CLI", target_type="agy")
                    if new_p:
                        agy_path = new_p
                        _, agy_path = _maybe_upgrade_unlocked_target("cli", agy_path, main_js_path)
                        do_patch_agy(agy_path)
                        _run_env_heal(target="cli", quiet=False, agy_path=agy_path)
            except Exception as exc:
                import traceback
                err(f"Unexpected error patching Antigravity CLI: {exc}")
                copy_sanitized_bug_report_to_clipboard(
                    traceback.format_exc(),
                    context_info="choice=4 Antigravity CLI patch",
                )
        elif choice == "5":
            try:
                if do_patch_vscode_flow(vscode_path, vscode_agy_path,
                                     set_js_cb=set_vscode_path, set_agy_cb=set_vscode_agy_path):
                    from patcher.extension_installer import deploy_assistant_extension
                    deploy_assistant_extension(export_vsix=False)
                    _run_env_heal(target="vscode", quiet=False)
                    launch_application("vscode")
            except Exception as exc:
                import traceback
                err(f"Unexpected error patching VS Code extension: {exc}")
                copy_sanitized_bug_report_to_clipboard(
                    traceback.format_exc(),
                    context_info="choice=5 Antigravity VS Code patch",
                )
        elif choice == "6":
            handled = True
            print_menu_section("RESTORE ALL TARGETS FROM BACKUP")
            print()
            hint("This will restore ALL applications from their original backups:")
            hint("  • Antigravity IDE (main.js)")
            hint("  • Antigravity 2.0 (language_server)")
            hint("  • Antigravity CLI (agy)")
            hint("  • Antigravity VS Code extension (extension.js + agy)")
            print()

            # Авто-поиск путей перед восстановлением
            if not main_js_path or not os.path.isfile(main_js_path):
                _r = find_install_root()
                if _r:
                    main_js_path = find_main_js(_r) or ""

            if not manager_path or not os.path.isfile(manager_path):
                manager_path = find_manager_binary() or ""

            if not vscode_path or not os.path.isfile(vscode_path):
                vscode_path = find_extension_js() or ""

            if not agy_path or not os.path.isfile(agy_path):
                agy_path = find_gemini_antigravity_binary() or ""

            if not vscode_agy_path or not os.path.isfile(vscode_agy_path):
                vscode_agy_path = find_gemini_antigravity_binary() or ""

            info("Restoring all targets in sequence...")
            print()

            # 1/4 - IDE restore
            print_menu_section("1 / 4 - Antigravity IDE restore")
            if main_js_path and os.path.isfile(main_js_path):
                do_restore(main_js_path, show_search_line=False, force=True)
            else:
                warn("Antigravity IDE (main.js) not found - skipping (use TOOLS -> 16 to set path)")

            # 2/4 - Manager restore
            print_menu_section("2 / 4 - Antigravity 2.0 restore")
            if manager_path and os.path.isfile(manager_path):
                do_restore_manager(manager_path, force=True)
            else:
                warn("Antigravity 2.0 (language_server) not found - skipping")

            # 3/4 - CLI restore
            print_menu_section("3 / 4 - Antigravity CLI restore")
            cli_target = agy_path or vscode_agy_path
            if cli_target and os.path.isfile(cli_target):
                do_restore_agy(cli_target, force=True)
            else:
                warn("Antigravity CLI (agy) not found - skipping")

            # 4/4 - VS Code restore
            print_menu_section("4 / 4 - Antigravity VS Code restore")
            if vscode_path and os.path.isfile(vscode_path):
                do_restore_vscode(vscode_path, force=True)
            else:
                warn("Antigravity VS Code extension not found - skipping")
            print()
            ok("All available targets restored from backup.")
        elif choice == "7":
            if manager_path:
                do_restore_manager(manager_path, force=True)
            else:
                err("Antigravity 2.0 (language_server) path is not set. Please select custom path (Option 16) first.")
        elif choice == "8":
            if agy_path:
                do_restore_agy(agy_path, force=True)
            else:
                err("Antigravity CLI path is not set. Please select custom path (Option 16) first.")
        elif choice == "9":
            if vscode_path:
                do_restore_vscode(vscode_path, force=True)
            else:
                err("Antigravity VS Code extension path is not set. Please select custom path (Option 16) first.")
        elif choice == "10":
            if _HAVE_UPDATER_LOCK:
                import pathlib as _pathlib
                _ide_root_dir = ""
                if main_js_path:
                    _p = _pathlib.Path(main_js_path)
                    for _ in range(5):
                        if (_p / "Antigravity IDE.exe").is_file():
                            _ide_root_dir = str(_p)
                            break
                        _p = _p.parent

                while True:
                    clear_screen()
                    print_banner()
                    print_menu_section("AUTO-UPDATE MANAGEMENT")
                    print()
                    ide_is_locked = is_ide_locked(_ide_root_dir or None)
                    mgr_is_locked = is_manager_locked()
                    vsc_is_locked = is_vscode_locked()
                    all_is_locked = is_update_locked()

                    ide_st = color("[LOCKED]", COLOR_RED) if ide_is_locked else color("[ENABLED]", COLOR_GREEN)
                    mgr_st = color("[LOCKED]", COLOR_RED) if mgr_is_locked else color("[ENABLED]", COLOR_GREEN)
                    vsc_st = color("[LOCKED]", COLOR_RED) if vsc_is_locked else color("[ENABLED]", COLOR_GREEN)

                    ide_ver_txt, ide_upd_hint = _target_ver_label("ide", main_js_path, not ide_is_locked)
                    mgr_ver_txt, mgr_upd_hint = _target_ver_label("manager", manager_path, not mgr_is_locked)

                    print("  Current Status:")
                    print(f"    • Antigravity IDE{ide_ver_txt or ''}:   {ide_st}")
                    print(f"    • Antigravity 2.0{mgr_ver_txt or ''}:   {mgr_st}")
                    print(f"    • Antigravity VS Code:        {vsc_st}")
                    print()

                    all_action = "Unlock all auto-updates" if all_is_locked else "Lock all auto-updates"
                    all_hint = "unblock & check updates now (IDE, 2.0 & VS Code)" if all_is_locked else "block IDE, 2.0 & VS Code"
                    print_menu_row("1", all_action, all_hint, COLOR_YELLOW)
                    print_menu_row("2", "Antigravity IDE updates", f"toggle lock ({ide_upd_hint or ide_st})", COLOR_GREEN)
                    print_menu_row("3", "Antigravity 2.0 updates", f"toggle lock ({mgr_upd_hint or mgr_st})", COLOR_GREEN)
                    print_menu_row("4", "Antigravity VS Code updates", f"toggle lock (currently {vsc_st})", COLOR_GREEN)
                    print_menu_row("5", "Check & install available updates now", "download, close app, update, re-patch & relaunch", COLOR_CYAN)
                    print()
                    print_menu_row("0", "Back to main menu", "return to patcher menu", COLOR_RED)
                    print_menu_footer("Unlocking or option [5] checks, installs, re-patches & relaunches automatically.")

                    sub_choice = input(color("\n  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip().lower()
                    if sub_choice in ("0", "", "q", "exit", "back"):
                        handled = False
                        break
                    elif sub_choice == "5":
                        print()
                        if _HAVE_APP_UPDATER:
                            invalidate_app_update_cache()
                        did_any_upd = False
                        if main_js_path and os.path.isfile(main_js_path):
                            did_i, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                            if did_i:
                                did_any_upd = True
                                _apply_patch_deploy_and_launch_for_target("ide", main_js_path, launch_after=True)
                        if manager_path and os.path.isfile(manager_path):
                            did_m, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                            if did_m:
                                did_any_upd = True
                                _apply_patch_deploy_and_launch_for_target("manager", manager_path, launch_after=True)
                        if agy_path and os.path.isfile(agy_path):
                            did_c, agy_path = _maybe_upgrade_unlocked_target("cli", agy_path, main_js_path)
                            if did_c:
                                did_any_upd = True
                                _apply_patch_deploy_and_launch_for_target("cli", agy_path, launch_after=False)
                        if not did_any_upd:
                            info("No newer IDE / 2.0 / CLI updates found right now.")
                        pause()
                    elif sub_choice == "1":
                        print()
                        was_ide_locked = ide_is_locked
                        was_mgr_locked = mgr_is_locked
                        do_toggle_updates(_ide_root_dir or None)
                        invalidate_menu_status_cache("update_locked")
                        now_ide_unlocked = not is_ide_locked(_ide_root_dir or None)
                        now_mgr_unlocked = not is_manager_locked()
                        did_any_upd = False
                        if was_ide_locked and now_ide_unlocked and main_js_path and os.path.isfile(main_js_path):
                            did_i, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                            if did_i:
                                did_any_upd = True
                                _apply_patch_deploy_and_launch_for_target("ide", main_js_path, launch_after=True)
                        if was_mgr_locked and now_mgr_unlocked and manager_path and os.path.isfile(manager_path):
                            did_m, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                            if did_m:
                                did_any_upd = True
                                _apply_patch_deploy_and_launch_for_target("manager", manager_path, launch_after=True)
                        if did_any_upd:
                            pause()
                        else:
                            import time
                            time.sleep(0.9)
                    elif sub_choice == "2":
                        print()
                        was_ide_locked = ide_is_locked
                        toggle_ide_updates(_ide_root_dir or None)
                        invalidate_menu_status_cache("update_locked")
                        now_ide_unlocked = not is_ide_locked(_ide_root_dir or None)
                        did_i = False
                        if was_ide_locked and now_ide_unlocked and main_js_path and os.path.isfile(main_js_path):
                            did_i, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                            if did_i:
                                _apply_patch_deploy_and_launch_for_target("ide", main_js_path, launch_after=True)
                        if did_i:
                            pause()
                        else:
                            import time
                            time.sleep(0.9)
                    elif sub_choice == "3":
                        print()
                        was_mgr_locked = mgr_is_locked
                        toggle_manager_updates()
                        invalidate_menu_status_cache("update_locked")
                        now_mgr_unlocked = not is_manager_locked()
                        did_m = False
                        if was_mgr_locked and now_mgr_unlocked and manager_path and os.path.isfile(manager_path):
                            did_m, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                            if did_m:
                                _apply_patch_deploy_and_launch_for_target("manager", manager_path, launch_after=True)
                        if did_m:
                            pause()
                        else:
                            import time
                            time.sleep(0.9)
                    elif sub_choice == "4":
                        print()
                        toggle_vscode_updates()
                        invalidate_menu_status_cache("update_locked")
                        import time
                        time.sleep(0.9)
                    else:
                        print()
                        err("Invalid choice")
                        print()
                        pause()
                try:
                    from patcher.auto_repatch import sync_auto_repatch_state
                    sync_auto_repatch_state(main_js_path, manager_path, agy_path)
                except Exception:
                    pass
            else:
                err(f"updater_lock module not available: {_UPDATER_LOCK_ERROR or 'module failed to load'}")
        elif choice == "11":
            if _HAVE_TLS_FIX:
                if main_js_path and os.path.isfile(main_js_path):
                    do_tls_fix_inject(main_js_path)
                else:
                    err("Antigravity IDE (main.js) not found. Set path first (Option 16).")
            else:
                err("tls_fix module not available.")
        elif choice == "12":
            if _HAVE_DIAGNOSTICS:
                print_target_info(main_js_path, manager_path, agy_path, vscode_path=vscode_path,
                                  vscode_agy_path=vscode_agy_path, show_search_line=False)
                print()
                do_diagnostics(main_js_path, manager_path, agy_path,
                               vscode_path=vscode_path, vscode_agy_path=vscode_agy_path)
                invalidate_menu_status_cache()
            else:
                err("diagnostics module not available.")
        elif choice == "13":
            if _HAVE_DATA_RESET:
                if do_data_reset() is False:
                    handled = False
                else:
                    invalidate_menu_status_cache("update_locked")
            else:
                err("data_reset module not available.")
        elif choice == "14":
            from patcher.nrpt_dns import (
                enable_nrpt_rule, disable_nrpt_rule,
                get_dns_config, PUBLIC_UNBLOCK_SERVERS,
                get_preferred_dns_ip
            )
            from patcher.hosts_pin import (
                enable_hosts_pinning, disable_hosts_pinning
            )
            print_menu_section("SELECTIVE DNS & ROUTING (NO VPN REQUIRED)")
            nrpt = _get_cached_nrpt_status()
            cfg = get_dns_config()
            is_active = bool(nrpt.get("active"))
            current_server = nrpt.get("server", "") or cfg.get("server_ip", "")
            status_str = f"ACTIVE ({current_server})" if is_active else "DISABLED"

            hosts_active, hosts_ip = _get_cached_hosts_pinned()
            hosts_status_str = f"ACTIVE ({hosts_ip})" if hosts_active else "DISABLED"

            print()
            info(f"NRPT DNS Status:     {color(status_str, COLOR_GREEN if is_active else COLOR_YELLOW)}")
            info(f"Hosts-Pinning (VPN): {color(hosts_status_str, COLOR_GREEN if hosts_active else COLOR_YELLOW)}")
            print()
            hint("Routes 'User location is not supported' endpoints for cloudcode-pa & daily:")
            hint("  • [1] NRPT DNS: routes Google Cloud queries via selective Windows policy table (no VPN).")
            hint("  • [4] Direct Hosts-Pinning: pins verified SNI proxy (100% works WITH and WITHOUT VPN).")
            print()
            if is_active:
                print_menu_row("1", "Disable NRPT DNS Routing (Restore standard)", f"currently active via {current_server} — click to turn off", COLOR_RED)
                print_menu_row("2", "Connect Private Ultra DNS / Change Server", "switch or update private DNS server", COLOR_CYAN)
            else:
                target_ip = get_preferred_dns_ip(cfg)
                mode_label = "Private DNS" if cfg.get("mode") == "private" else "Geohide Public DNS"
                print_menu_row("1", f"Enable NRPT DNS Routing ({mode_label})", f"use resolver ({target_ip})", COLOR_GREEN)
                print_menu_row("2", "Connect Private Ultra DNS / Premium Server", "enter your private DNS IP or server", COLOR_CYAN)

            if hosts_active:
                print_menu_row("4", "Disable Direct Hosts-Pinning", f"remove proxy mapping ({hosts_ip}) from hosts", COLOR_RED)
            else:
                print_menu_row("4", "Enable Direct Hosts-Pinning (VPN-Proof)", "pin verified SNI proxy to etc/hosts (recommended with VPN)", COLOR_GREEN)

            print_menu_row("0", "Back to main menu", "", COLOR_YELLOW)
            print()
            try:
                sub = input(color("  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip().lower()
            except EOFError:
                sub = "0"
            if sub in ("0", "", "b", "back", "q", "quit", "exit"):
                handled = False
            elif sub == "3" or (sub == "1" and is_active):
                ok_flag, msg = disable_nrpt_rule()
                invalidate_menu_status_cache("nrpt_status")
                if ok_flag:
                    ok("NRPT DNS routing disabled. Standard DNS restored.")
                else:
                    err(msg)
                handled = True
            elif sub == "1":
                target_ip = get_preferred_dns_ip(cfg)
                target_mode = cfg.get("mode", "public")
                target_name = cfg.get("server_name") or ("Private DNS" if target_mode == "private" else PUBLIC_UNBLOCK_SERVERS[0]["name"])
                ok_flag, msg = enable_nrpt_rule(
                    target_ip,
                    mode=target_mode,
                    server_name=target_name,
                )
                invalidate_menu_status_cache("nrpt_status")
                if ok_flag:
                    ok(msg)
                else:
                    err(msg)
                handled = True
            elif sub == "2":
                print()
                hint("Enter your dedicated DNS IP or server hostname (or 0 / Enter to cancel):")
                try:
                    custom_ip = input(color("  Private DNS IP / Host > ", COLOR_CYAN, COLOR_BOLD)).strip()
                except EOFError:
                    custom_ip = ""
                if not custom_ip or custom_ip.lower() in ("0", "b", "back", "q", "quit", "exit"):
                    cancel("Cancelled.")
                    handled = False
                else:
                    ok_flag, msg = enable_nrpt_rule(custom_ip, mode="private")
                    invalidate_menu_status_cache("nrpt_status")
                    if ok_flag:
                        ok(f"Premium Private DNS activated: {msg}")
                    else:
                        err(msg)
                    handled = True
            elif sub in ("4", "h") and hosts_active:
                ok_flag, msg = disable_hosts_pinning()
                invalidate_menu_status_cache("hosts_pinned")
                if ok_flag:
                    ok(msg)
                else:
                    err(msg)
                handled = True
            elif sub in ("4", "h"):
                ok_flag, msg = enable_hosts_pinning()
                invalidate_menu_status_cache("hosts_pinned")
                if ok_flag:
                    ok(msg)
                else:
                    err(msg)
                handled = True
            else:
                err("Invalid choice")
                handled = True
        elif choice == "15":
            webbrowser.open(GITHUB_URL)
            ok(f"Opening GitHub repository in browser: {color(GITHUB_URL, COLOR_CYAN)}")
        elif choice == "16":
            while True:
                redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                                   vscode_agy_path, show_search_line=False)
                print_menu_section("SELECT CUSTOM PATH")
                print_menu_row("1", "Antigravity IDE path", "folder or main.js", COLOR_GREEN)
                print_menu_row("2", "Antigravity 2.0 path", "folder or language_server binary", COLOR_GREEN)
                print_menu_row("3", "Antigravity CLI path", "agy.exe or folder", COLOR_GREEN)
                print_menu_row("4", "VS Code extension path (JS)", "extension.js or extensions folder", COLOR_GREEN)
                print_menu_row("5", "VS Code binary path (agy)", "agy/antigravity binary in ~/.gemini/bin", COLOR_GREEN)
                print()
                print_menu_row("0", "Back", "return to main menu", COLOR_RED)
                print_menu_footer("Leaves auto-detection results intact for other targets.")

                try:
                    sub_choice = input(color("\n  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip().lower()
                except EOFError:
                    sub_choice = "0"
                if sub_choice in ("0", "", "b", "back", "q", "quit", "exit"):
                    handled = False
                    break

                if sub_choice not in ("1", "2", "3", "4", "5"):
                    err("Invalid choice")
                    print()
                    pause()
                    continue

                if sub_choice == "1":
                     print()
                     hint("Enter the path to Antigravity IDE folder or main.js file (or 0 / Enter to cancel).")
                     print_path_examples()
                     try:
                         raw = input(color("\n  IDE Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     except EOFError:
                         raw = ""
                     if not raw or raw.lower() in ("0", "b", "back", "q", "quit", "exit"):
                         cancel("Cancelled.")
                         continue
                     new_path = resolve_target_path(raw)
                     if new_path and os.path.exists(new_path):
                         if os.path.isdir(new_path):
                             new_path = find_main_js(new_path)
                         if new_path and os.path.isfile(new_path):
                             main_js_path = new_path
                             ok("Antigravity IDE path updated!")
                         else:
                             err("Could not resolve a valid Antigravity IDE target (main.js not found).")
                     else:
                         err("Path does not exist.")
                     print()
                     pause()
                elif sub_choice == "2":
                     print()
                     hint("Enter the path to Antigravity 2.0 app folder or language_server binary (or 0 / Enter to cancel).")
                     print_path_examples()
                     try:
                         raw = input(color("\n  Antigravity 2.0 Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     except EOFError:
                         raw = ""
                     if not raw or raw.lower() in ("0", "b", "back", "q", "quit", "exit"):
                         cancel("Cancelled.")
                         continue
                     new_path = resolve_manager_path(raw)
                     if new_path and os.path.isfile(new_path):
                         manager_path = new_path
                         ok("Antigravity 2.0 path updated!")
                     else:
                         err("Could not resolve a valid Antigravity 2.0 target (language_server not found).")
                     print()
                     pause()
                elif sub_choice == "3":
                     print()
                     hint("Enter the path to the agy binary (agy.exe) or its folder (or 0 / Enter to cancel).")
                     print_path_examples()
                     try:
                         raw = input(color("\n  AGY Path > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     except EOFError:
                         raw = ""
                     if not raw or raw.lower() in ("0", "b", "back", "q", "quit", "exit"):
                         cancel("Cancelled.")
                         continue
                     new_path = resolve_agy_path(raw)
                     if new_path and os.path.isfile(new_path):
                         agy_path = new_path
                         ok("Antigravity CLI path updated!")
                     else:
                         err("Could not resolve a valid Antigravity CLI target.")
                     print()
                     pause()
                elif sub_choice == "4":
                     print()
                     hint("Enter the path to extension.js, the google.google-antigravity-*")
                     hint("extension folder, or the .vscode/extensions root folder (or 0 / Enter to cancel).")
                     try:
                         raw = input(color("\n  VS Code Extension Path (JS) > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     except EOFError:
                         raw = ""
                     if not raw or raw.lower() in ("0", "b", "back", "q", "quit", "exit"):
                         cancel("Cancelled.")
                         continue
                     new_path = resolve_extension_path(raw)
                     if new_path and os.path.isfile(new_path):
                         vscode_path = new_path
                         ok("Antigravity VS Code extension path updated!")
                     else:
                         err("Could not resolve a valid VS Code extension target (extension.js not found).")
                     print()
                     pause()
                elif sub_choice == "5":
                     print()
                     hint(f"Enter the path to the agy/antigravity binary downloaded")
                     hint(f"by the extension ({describe_gemini_binary_path()}) or any agy file (or 0 / Enter to cancel).")
                     try:
                         raw = input(color("\n  VS Code Binary Path (agy) > ", COLOR_CYAN, COLOR_BOLD)).strip()
                     except EOFError:
                         raw = ""
                     if not raw or raw.lower() in ("0", "b", "back", "q", "quit", "exit"):
                         cancel("Cancelled.")
                         continue
                     new_path = resolve_agy_path(raw)
                     if new_path and os.path.isfile(new_path):
                         vscode_agy_path = new_path
                         ok("VS Code agy binary path updated!")
                     else:
                         err("Could not resolve a valid VS Code agy binary target.")
                     print()
                     pause()
        elif choice == "17":
            show_about()
            print()
            pause("  Press Enter, 0 or any key to return to menu...")
            handled = False
        elif choice == "18":
            print_menu_section("INSTALL ANTIGRAVITY ASSISTANT EXTENSION")
            info("Deploying Antigravity Assistant extension (Account Switcher, Quotas, Ping)...")
            from patcher.extension_installer import deploy_assistant_extension
            res = deploy_assistant_extension(export_vsix=True)
            if res:
                ok("Antigravity Assistant successfully installed to Antigravity IDE and VS Code!")
                hint("Status Bar item $(shield) will appear on next editor launch.")
            else:
                err("Failed to deploy extension.")
            handled = True
        elif choice == "19":
            print_menu_section("CHECK FOR CLOUD UPDATES")
            from patcher.utils.update import perform_cloud_update
            perform_cloud_update(interactive=False)
            handled = True
        elif choice == "20":
            from patcher.conversations.rebuilder import run_rebuild_wizard
            run_rebuild_wizard()
            handled = True

        print()

        if handled:
            pause()
        redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                           vscode_agy_path, show_search_line=False)










