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
from patcher.utils.i18n import tr, toggle_lang, get_lang, set_lang, SUPPORTED_LANGS
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


def pause(prompt=None):
    if prompt is None:
        prompt = tr(
            "  Press Enter (or any key) to return to menu...",
            "  Нажмите Enter (или любую клавишу) для возврата в меню...",
        )
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

    hint(tr("Usage examples with custom path:", "Примеры запуска с указанием пути:"))
    print(f"      Windows: {color(windows_example, COLOR_YELLOW)}")
    print(f"      macOS:   {color(macos_example, COLOR_YELLOW)}")
    print(f"      Linux:   {color(linux_example, COLOR_YELLOW)}")


def print_path_examples():
    windows_path = r"C:\Users\Name\AppData\Local\Programs\Antigravity IDE"
    macos_path = "/Applications/Antigravity IDE.app"
    linux_path = "/usr/share/antigravity-ide"

    hint(tr("Path examples:", "Примеры пути:"))
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


_BACK_CHOICES = {"0", "", "b", "back", "q", "quit", "exit", "\x1b", "назад"}


def _read_menu_input(prompt=None, default_on_eof="0"):
    if prompt is None:
        prompt = tr("\n  Select option > ", "\n  Выберите пункт > ")
    try:
        raw = input(color(prompt, COLOR_CYAN, COLOR_BOLD))
    except (EOFError, KeyboardInterrupt):
        return default_on_eof
    cleaned = str(raw).lstrip("\ufeff").strip().lower()
    if cleaned.startswith("\x1b"):
        cleaned = cleaned.lstrip("\x1b") or "0"
    return cleaned


def _read_path_input(prompt):
    try:
        raw = input(color(prompt, COLOR_CYAN, COLOR_BOLD)).strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    if not raw or raw.startswith("\x1b"):
        return ""
    return raw


def prompt_yn(question):
    question = question.rstrip()
    prompt = f"  [?] {question} ({color('y', COLOR_GREEN)}/{color('n', COLOR_RED)}): "
    try:
        return _read_console_line(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "n"


def confirmed(question):
    """Возвращает True, если пользователь ответил 'y'."""
    return prompt_yn(question) in ("y", "yes", "\u0434", "\u0434\u0430")


def offer_download_and_block(software_name="Antigravity", target_type=None):
    err(tr(f"{software_name} path is not set or software is not installed.",
           f"Путь к {software_name} не задан или программа не установлена."))
    err(tr("Patching attempt blocked.", "Попытка пропатчивания остановлена."))
    hint(f"{tr('Download URL', 'Ссылка для скачивания')}: {color(DOWNLOAD_URL, COLOR_CYAN)}")
    print()
    print_menu_row("1", tr("Open download page in browser", "Открыть страницу загрузки"), DOWNLOAD_URL, COLOR_GREEN)
    print_menu_row("2", tr(f"Specify path to {software_name}", f"Указать путь к {software_name}"),
                   tr("manual path selection", "выбор папки или файла вручную"), COLOR_CYAN)
    print_menu_row("0", tr("Back to main menu", "Назад в главное меню"), "Enter / 0 / b", COLOR_RED)
    print()

    action = _read_menu_input(tr("  Select option > ", "  Выберите пункт > "))
    if action in _BACK_CHOICES:
        global _last_offer_cancelled
        _last_offer_cancelled = True
        cancel(tr("Cancelled.", "Отменено."))
        return None
    elif action == "1":
        webbrowser.open(DOWNLOAD_URL)
        ok(f"{tr('Opening', 'Открываю')}: {color(DOWNLOAD_URL, COLOR_CYAN)}")
        return None
    elif action == "2":
        print()
        hint(tr(f"Enter the path to {software_name} (or 0 / Enter / b to cancel).",
                f"Введите путь к {software_name} (или 0 / Enter / b для отмены)."))
        print_path_examples()
        raw = _read_path_input(f"\n  {software_name} {tr('Path', 'Путь')} > ")
        if not raw or raw.lower() in _BACK_CHOICES:
            cancel(tr("Cancelled.", "Отменено."))
            return None
        if raw:
            if target_type == "ide" or software_name == "Antigravity IDE":
                new_path = resolve_target_path(raw)
                if new_path and os.path.isdir(new_path):
                    new_path = find_main_js(new_path)
                if new_path and os.path.isfile(new_path) and new_path.endswith("main.js"):
                    ok(tr(f"{software_name} path updated!", f"Путь к {software_name} обновлён!"))
                    return new_path
                else:
                    err(tr(f"Could not resolve a valid {software_name} target (main.js not found).",
                           f"Не удалось найти корректную цель {software_name} (main.js не найден)."))
            elif target_type == "manager" or software_name == "Antigravity 2.0":
                new_path = resolve_manager_path(raw)
                if new_path and os.path.isfile(new_path):
                    ok(tr(f"{software_name} path updated!", f"Путь к {software_name} обновлён!"))
                    return new_path
                else:
                    err(tr(f"Could not resolve a valid {software_name} target (language_server not found).",
                           f"Не удалось найти корректную цель {software_name} (language_server не найден)."))
            elif target_type == "agy" or software_name == "Antigravity CLI":
                new_path = resolve_agy_path(raw)
                if new_path and os.path.isfile(new_path):
                    ok(tr(f"{software_name} path updated!", f"Путь к {software_name} обновлён!"))
                    return new_path
                else:
                    err(tr(f"Could not resolve a valid {software_name} target.",
                           f"Не удалось найти корректную цель {software_name}."))
            else:
                p = resolve_target_path(raw) or resolve_manager_path(raw) or resolve_agy_path(raw)
                if p and os.path.exists(p):
                    ok(tr("Path updated!", "Путь обновлён!"))
                    return p
                else:
                    err(tr("Invalid path provided.", "Указан некорректный путь."))
        return None
    else:
        cancel(tr("Cancelled.", "Отменено."))
        return None


def _kv(label, value_text, value_color):
    """Отформатированная пара ключ/значение с выравниванием меток."""
    print(f"      {label:<10}{color(value_text, value_color)}")


def print_target_info(main_js_path, manager_path="", agy_path="", vscode_path="", vscode_agy_path="", show_search_line=False):
    if show_search_line:
        info(tr("Searching for installations...", "Поиск установленных компонентов..."))

    _not_found = tr("not found", "не найден")
    _not_found_cap = tr("Not found", "Не найден")
    _found = tr("found", "найден")
    _patched_yes = tr("already patched", "пропатчен")
    _patched_no = tr("not patched", "не пропатчен")
    _unreadable = tr("unreadable", "не читается")
    _not_detected = tr("not detected", "не определена")

    # 1. Antigravity IDE Info
    print_menu_section("ANTIGRAVITY IDE")
    _kv(tr("Target:", "Цель:"), main_js_path if main_js_path else _not_found_cap, COLOR_CYAN)
    if main_js_path and os.path.exists(main_js_path):
        try:
            with open(main_js_path, "r", encoding="utf-8") as f:
                content = f.read()
            _kv(tr("Status:", "Статус:"), _found, COLOR_GREEN)
            patched = is_already_patched(content)
            _kv(tr("Patch:", "Патч:"), _patched_yes if patched else _patched_no,
                COLOR_YELLOW if patched else COLOR_GREEN)
        except Exception:
            _kv(tr("Status:", "Статус:"), _unreadable, COLOR_RED)
            _kv(tr("Patch:", "Патч:"), _unreadable, COLOR_RED)

        ver_str, _ = get_ag_version(main_js_path)
        _kv(tr("Version:", "Версия:"), ver_str if ver_str else _not_detected,
            COLOR_GREEN if ver_str else COLOR_YELLOW)

        size = file_size(main_js_path)
        _kv(tr("Size:", "Размер:"), format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv(tr("Status:", "Статус:"), _not_found, COLOR_RED)

    print()

    # 2. Antigravity 2.0 Info
    print_menu_section("ANTIGRAVITY 2.0")
    _kv(tr("Target:", "Цель:"), manager_path if manager_path else _not_found_cap, COLOR_CYAN)
    if manager_path and os.path.isfile(manager_path):
        _kv(tr("Status:", "Статус:"), _found, COLOR_GREEN)
        patched = is_mgr_patched(manager_path)
        _kv(tr("Patch:", "Патч:"), _patched_yes if patched else _patched_no,
            COLOR_YELLOW if patched else COLOR_GREEN)
        
        ver_str = get_antigravity_version(manager_path)
        _kv(tr("Version:", "Версия:"), ver_str if ver_str else _not_detected,
            COLOR_GREEN if ver_str else COLOR_YELLOW)

        size = file_size(manager_path)
        _kv(tr("Size:", "Размер:"), format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv(tr("Status:", "Статус:"), _not_found, COLOR_YELLOW)

    print()

    # 3. Antigravity CLI Info
    print_menu_section("ANTIGRAVITY CLI")
    _kv(tr("Target:", "Цель:"), agy_path if agy_path else _not_found_cap, COLOR_CYAN)
    if agy_path and os.path.isfile(agy_path):
        _kv(tr("Status:", "Статус:"), _found, COLOR_GREEN)
        patched = is_agy_patched(agy_path)
        _kv(tr("Patch:", "Патч:"), _patched_yes if patched else _patched_no,
            COLOR_YELLOW if patched else COLOR_GREEN)
        size = file_size(agy_path)
        _kv(tr("Size:", "Размер:"), format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv(tr("Status:", "Статус:"), _not_found, COLOR_YELLOW)

    print()

    # 4. Antigravity VS Code Extension Info (extension.js + бинарь agy)
    print_menu_section(tr("ANTIGRAVITY VS CODE EXTENSION", "РАСШИРЕНИЕ ANTIGRAVITY VS CODE"))
    _kv(tr("Target:", "Цель:"), vscode_path if vscode_path else _not_found_cap, COLOR_CYAN)
    if vscode_path and os.path.isfile(vscode_path):
        _kv(tr("Status:", "Статус:"), _found, COLOR_GREEN)
        try:
            with open(vscode_path, "r", encoding="utf-8") as f:
                content = f.read()
            patched = is_vscode_patched(content)
            _kv(tr("Patch:", "Патч:"), _patched_yes if patched else _patched_no,
                COLOR_YELLOW if patched else COLOR_GREEN)
        except Exception:
            _kv(tr("Patch:", "Патч:"), _unreadable, COLOR_RED)
        size = file_size(vscode_path)
        _kv(tr("Size:", "Размер:"), format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv(tr("Status:", "Статус:"), _not_found, COLOR_YELLOW)

    print()

    _kv(tr("AGY Bin:", "Файл AGY:"), vscode_agy_path if vscode_agy_path else _not_found_cap, COLOR_CYAN)
    if vscode_agy_path and os.path.isfile(vscode_agy_path):
        _kv(tr("Status:", "Статус:"), _found, COLOR_GREEN)
        patched = is_agy_patched(vscode_agy_path)
        _kv(tr("Patch:", "Патч:"), _patched_yes if patched else _patched_no,
            COLOR_YELLOW if patched else COLOR_GREEN)
        size = file_size(vscode_agy_path)
        _kv(tr("Size:", "Размер:"), format_bytes(size), COLOR_GREEN if size > 0 else COLOR_YELLOW)
    else:
        _kv(tr("Status:", "Статус:"), _not_found, COLOR_YELLOW)


def show_about():
    clear_screen()
    print_banner()
    print_menu_section(tr("ABOUT ANTIGRAVITY PATCHER", "О ПРОГРАММЕ ANTIGRAVITY PATCHER"))
    print()
    print(f"  {color('Antigravity Patcher by energystar', COLOR_BOLD, COLOR_CYAN)} v{VERSION}")
    print(tr("  Open-source diagnostics, network routing, and extension toolkit for Antigravity:",
             "  Открытый набор инструментов диагностики, маршрутизации и расширений для Antigravity:"))
    print(tr("  Antigravity IDE, Antigravity 2.0 (language_server), and Antigravity CLI (agy).",
             "  Antigravity IDE, Antigravity 2.0 (language_server) и Antigravity CLI (agy)."))
    print(tr("  Provides selective DNS routing, quota visibility, and runtime diagnostics without VPN.",
             "  Выборочный DNS-роутинг, мониторинг квот, ротация аккаунтов 429 и автодиагностика без VPN."))
    print()
    hint(tr("Features:", "Возможности:"))
    print(f"   • {color(tr('Antigravity IDE config', 'Настройка Antigravity IDE'), COLOR_GREEN)} — {tr('main.js diagnostics, dialog & TLS support', 'main.js, диалоги, роутинг и TLS')}")
    print(f"   • {color(tr('Antigravity 2.0 support', 'Поддержка Antigravity 2.0'), COLOR_GREEN)} — {tr('language_server & Desktop UI integration', 'language_server и встроенный Desktop UI')}")
    print(f"   • {color(tr('Antigravity CLI support', 'Поддержка Antigravity CLI'), COLOR_GREEN)} — {tr('agy CLI environment compatibility', 'совместимость среды agy CLI')}")
    print(f"   • {color(tr('Auto-launch Apps', 'Автозапуск приложений'), COLOR_GREEN)}     — {tr('automatically opens configured application', 'запускает готовое приложение после патча')}")
    print(f"   • {color(tr('Auto-update Lock', 'Контроль обновлений'), COLOR_GREEN)}    — {tr('granular lock for IDE, 2.0 & VS Code', 'раздельная блокировка для IDE, 2.0 и VS Code')}")
    print(f"   • {color(tr('Backup & Restore', 'Бэкапы и восстановление'), COLOR_GREEN)}     — {tr('safe, fully reversible modifications', 'безопасный откат всех изменений')}")
    print()
    hint(tr("Author & Community:", "Автор и ссылки:"))
    tg_main = link("https://t.me/energystar6", "@energystar6", COLOR_CYAN, COLOR_UNDERLINE)
    gh_link = link("https://github.com/EnergyStario/antigravity-patcher", "github.com/EnergyStario/antigravity-patcher", COLOR_CYAN, COLOR_UNDERLINE)

    print(f"   • {tr('Author:', 'Автор:')}   {color('energystar', COLOR_YELLOW)}")
    print(f"   • Telegram: {tg_main}")
    print(f"   • GitHub:   {gh_link}")
    print()
    hint(tr("License:", "Лицензия:"))
    print("   • GPL-3.0 License")
    print()
    print_menu_row("0", tr("Back to main menu", "Назад в главное меню"), tr("return to patcher", "Enter / 0 / b"), COLOR_RED)



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
        avail = tr("available", "доступно")
        hint_extra = f"v{ver} -> v{new_v} {avail}" if ver else f"v{new_v} {avail}"
    elif unlocked and ver:
        hint_extra = tr(f"v{ver}, auto-upd ON", f"v{ver}, автообн. ВКЛ")
    elif ver:
        hint_extra = tr(f"v{ver}, lock ON", f"v{ver}, блок ВКЛ")
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
        state_str = color("OK", COLOR_GREEN) if patched else color(tr("NEED PATCH", "НУЖЕН ПАТЧ"), COLOR_YELLOW)
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
        lock_tag = color(tr("LOCKED", "ЗАБЛОК."), COLOR_GREEN)
    elif ide_unlocked and mgr_unlocked:
        lock_tag = color(tr("OPEN", "РАЗРЕШЕНЫ"), COLOR_YELLOW)
    else:
        open_list = []
        if ide_unlocked:
            open_list.append("IDE")
        if mgr_unlocked:
            open_list.append("2.0")
        open_lbl = tr("OPEN", "ОТКР.")
        lock_tag = color(f"{open_lbl} ({'/'.join(open_list)})", COLOR_YELLOW)
    lang_badge = color(get_lang().upper(), COLOR_CYAN, COLOR_BOLD)
    print(f"  {tr('Status:', 'Статус:')}  {'  |  '.join(parts)}  |  {tr('Updates:', 'Обновл.:')} {lock_tag}  |  {tr('Lang:', 'Язык:')} {lang_badge}")
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
        missing.append(tr("extension.js (VS Code extension 'google.google-antigravity')",
                          "extension.js (расширение VS Code 'google.google-antigravity')"))
    if not vscode_agy_path or not os.path.isfile(vscode_agy_path):
        missing.append(tr(f"agy binary ({describe_gemini_binary_path()})",
                          f"бинарник agy ({describe_gemini_binary_path()})"))

    if missing:
        if silent_if_missing:
            hint(tr("Antigravity VS Code extension not installed — skipping",
                    "Расширение Antigravity VS Code не найдено — пропуск"))
            return False
        err(tr("Antigravity VS Code Patch blocked — required files not found:",
               "Патч Antigravity VS Code невозможен — не найдены обязательные файлы:"))
        for m in missing:
            err(f"  - {m}")
        hint(tr("Install the 'Google Antigravity' extension in VS Code first",
                "Сначала установите расширение 'Google Antigravity' в VS Code"))
        hint(tr("(Extensions view -> search 'Antigravity' -> Install),",
                "(Панель Extensions -> поиск 'Antigravity' -> Install),"))
        hint(tr("run it once — it downloads the binary to ~/.gemini/bin automatically.",
                "запустите его один раз — бинарник автоматически скачается в ~/.gemini/bin."))
        return False

    # --- 1. Патчим extension.js ---
    do_patch_vscode(vscode_path)

    # --- 2. Патчим бинарь ~/.gemini/bin/antigravity (agy-патчем) ---
    print()
    info(f"{tr('Found downloaded binary', 'Найден скачанный бинарник')}: {color(vscode_agy_path, COLOR_CYAN)}")
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
        args = []
        skip_next_arg = False
        for a in sys.argv[1:]:
            if skip_next_arg:
                skip_next_arg = False
                if a.strip().lower() in SUPPORTED_LANGS:
                    set_lang(a.strip().lower())
                continue
            if a in ("--rollback", "-r"):
                continue
            if a == "--lang":
                skip_next_arg = True
                continue
            if a.startswith("--lang="):
                cand_lang = a.split("=", 1)[1].strip().lower()
                if cand_lang in SUPPORTED_LANGS:
                    set_lang(cand_lang)
                continue
            args.append(a)
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
                        err(tr(f"Provided path does not exist or invalid: {arg}",
                               f"Указанный путь не существует или недействителен: {arg}"))

    # 2. Проверяем текущую директорию (для Antigravity IDE)
    if not main_js_path and not manager_path and not agy_path:
        local = os.path.join(os.getcwd(), "main.js")
        if os.path.exists(local):
            main_js_path = local
            info(tr("Found main.js in current directory", "Найден main.js в текущей папке"))

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

    def _pick_single_primary_target() -> tuple[str, str]:
        """Choose strictly ONE installed app to auto-open on clean first-run autopilot."""
        has_ide = bool(main_js_path and os.path.isfile(main_js_path))
        has_mgr = bool(manager_path and os.path.isfile(manager_path))
        if has_ide and not has_mgr:
            return "ide", main_js_path
        if has_mgr and not has_ide:
            return "manager", manager_path
        if not has_ide and not has_mgr:
            return "", ""
        # Both installed: prefer whichever had more recent user activity, defaulting to IDE
        try:
            from pathlib import Path
            appdata = os.environ.get("APPDATA", "")
            ide_log_dir = Path(appdata) / "Antigravity IDE" / "logs" if appdata else None
            mgr_log_dir = Path(appdata) / "Antigravity" / "logs" if appdata else None
            ide_mtime = max((p.stat().st_mtime for p in ide_log_dir.iterdir()), default=0.0) if (ide_log_dir and ide_log_dir.is_dir()) else 0.0
            mgr_mtime = max((p.stat().st_mtime for p in mgr_log_dir.iterdir()), default=0.0) if (mgr_log_dir and mgr_log_dir.is_dir()) else 0.0
            if mgr_mtime > ide_mtime and mgr_mtime > 0.0 and ide_mtime == 0.0:
                return "manager", manager_path
            if mgr_mtime > ide_mtime + 60.0:
                return "manager", manager_path
        except Exception:
            pass
        return "ide", main_js_path

    # Full Autopilot on startup: patch missing targets synchronously, and run network/DNS/watcher healing
    # asynchronously in a daemon thread so the CMD menu opens instantaneously without freezing.
    _run_bg_watcher_sync = True
    if any([main_js_path, manager_path, agy_path, vscode_path, vscode_agy_path]) and os.environ.get("AG_DISABLE_STARTUP_AUTOPILOT") != "1":
        try:
            from patcher.auto_repatch import inspect_targets, run_auto_repatch
            from patcher.utils.launcher import detect_running_antigravity_apps
            _st = inspect_targets(
                main_js_path=main_js_path,
                manager_path=manager_path,
                agy_path=agy_path,
                vscode_path=vscode_path,
                vscode_agy_path=vscode_agy_path,
            )
            if _st.get("needs_any"):
                info(tr(
                    "Autopilot: unpatched components detected — applying fixes automatically...",
                    "Автопилот: обнаружены неподготовленные компоненты — применяю исправления автоматически...",
                ))
                _was_open = detect_running_antigravity_apps()
                _rep = run_auto_repatch(
                    restart_running=True,
                    main_js_path=main_js_path,
                    manager_path=manager_path,
                    agy_path=agy_path,
                    vscode_path=vscode_path,
                    vscode_agy_path=vscode_agy_path,
                ) or {}
                _run_env_heal(
                    target="all",
                    quiet=True,
                    main_js_path=main_js_path,
                    manager_path=manager_path,
                    agy_path=agy_path,
                )
                if not _was_open.get("ide") and not _was_open.get("manager") and not _rep.get("restarted"):
                    _prim_key, _prim_path = _pick_single_primary_target()
                    if _prim_key and _prim_path:
                        if launch_application(_prim_key, _prim_path):
                            if _prim_key == "ide":
                                try:
                                    from patcher.ide.patcher import ensure_agy_backend_patched
                                    ensure_agy_backend_patched(wait_seconds=3.0)
                                except Exception:
                                    pass
                invalidate_menu_status_cache()
                _TARGET_STATUS_CACHE.clear()
                _run_bg_watcher_sync = False
            else:
                _run_bg_watcher_sync = False
                import threading

                def _bg_env_heal():
                    try:
                        _hl = ensure_target_and_env_healthy(
                            target="all",
                            quiet=True,
                            main_js_path=main_js_path,
                            manager_path=manager_path,
                            agy_path=agy_path,
                        ) or {}
                        if _hl.get("changed") or _hl.get("proxy_healed") or _hl.get("tls_healed") or _hl.get("nrpt_auto_enabled") or _hl.get("hosts_auto_enabled"):
                            invalidate_menu_status_cache()
                            _TARGET_STATUS_CACHE.clear()
                    except Exception:
                        pass

                threading.Thread(
                    target=_bg_env_heal,
                    name="ag-env-heal",
                    daemon=True,
                ).start()
        except Exception:
            pass

    if _run_bg_watcher_sync:
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

    redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path=vscode_path,
                       vscode_agy_path=vscode_agy_path, show_search_line=False)

    while True:
        _, _ide_hint_ver = _target_ver_label("ide", main_js_path, _is_target_unlocked("ide", main_js_path))
        _, _mgr_hint_ver = _target_ver_label("manager", manager_path, _is_target_unlocked("manager", main_js_path))
        _, _cli_hint_ver = _target_ver_label("cli", agy_path, _is_target_unlocked("cli", main_js_path))
        print_menu_section(tr("PATCH", "ПАТЧИНГ"))
        print_menu_row("1", tr("Apply all patches", "Применить все патчи"),
                       tr("check updates, apply all patches & relaunch", "обновить, пропатчить всё и перезапустить"), COLOR_GREEN)
        print_menu_row("2", tr("Antigravity IDE patch", "Патч Antigravity IDE"),
                       (tr(f"configure IDE runtime ({_ide_hint_ver})", f"настроить IDE ({_ide_hint_ver})")
                        if _ide_hint_ver else tr("configure IDE runtime", "настроить среду IDE")), COLOR_GREEN)
        print_menu_row("3", tr("Antigravity 2.0 patch", "Патч Antigravity 2.0"),
                       (tr(f"patch language_server & UI ({_mgr_hint_ver})", f"language_server и UI ({_mgr_hint_ver})")
                        if _mgr_hint_ver else tr("patch language_server binary & Desktop UI", "language_server и Desktop UI")), COLOR_GREEN)
        print_menu_row("4", tr("Antigravity CLI (agy) patch", "Патч Antigravity CLI (agy)"),
                       (tr(f"configure agy CLI ({_cli_hint_ver})", f"настроить agy CLI ({_cli_hint_ver})")
                        if _cli_hint_ver else tr("configure agy CLI", "настроить agy CLI")), COLOR_GREEN)
        print_menu_row("5", tr("Antigravity VS Code Patch", "Патч Antigravity VS Code"),
                       tr("patch google.google-antigravity extension.js + agy binary", "расширение VS Code + бинарник agy"), COLOR_GREEN)

        print_menu_section(tr("RESTORE", "ВОССТАНОВЛЕНИЕ"))
        print_menu_row("6", tr("Restore all from backup", "Восстановить всё из бэкапа"),
                       tr("restore all available backups", "откат всех доступных резервных копий"), COLOR_YELLOW)
        print_menu_row("7", "Antigravity 2.0", tr("from backup", "из бэкапа"), COLOR_YELLOW)
        print_menu_row("8", "Antigravity CLI", tr("from backup", "из бэкапа"), COLOR_YELLOW)
        print_menu_row("9", tr("Antigravity VS Code extension", "Расширение VS Code"),
                       tr("from backup", "из бэкапа"), COLOR_YELLOW)

        print_menu_section(tr("EXTRAS", "ДОПОЛНИТЕЛЬНО"))
        cached_locked = _get_cached_update_locked()
        lock_label = (
            tr("Unlock all auto-updates", "Разблокировать обновления")
            if cached_locked else
            tr("Lock all auto-updates", "Заблокировать обновления")
        )
        lock_hint = (
            tr("unblock IDE, 2.0 & VS Code updaters", "разрешить обновления IDE, 2.0 и VS Code")
            if cached_locked else
            tr("block IDE, 2.0 & VS Code updaters", "запретить обновления IDE, 2.0 и VS Code")
        )
        print_menu_row("10", lock_label, lock_hint, COLOR_YELLOW)
        print_menu_row("11", tr("TLS certificate fix", "Фикс TLS-сертификата"),
                       tr("fix expired localhost TLS cert", "исправить локальный TLS-сертификат"), COLOR_YELLOW)
        print_menu_row("12", tr("Diagnostics / Auto-fix", "Диагностика / Автолечение"),
                       tr("analyze logs & auto-fix errors", "анализ логов и автоисправление ошибок"), COLOR_YELLOW)
        print_menu_row("13", tr("Reset data / Fix 429", "Очистка кэша / Фикс 429"),
                       tr("reset IDE data folder, preserve chats", "очистить кэш без потери настроек и чатов"), COLOR_YELLOW)

        # 14. Selective DNS routing indicator (cached in memory between menu redraws)
        from patcher.nrpt_dns import is_nrpt_supported
        nrpt = _get_cached_nrpt_status()
        hosts_act, hosts_ip = _get_cached_hosts_pinned()
        if hosts_act and nrpt.get("active"):
            dns_status = tr("ACTIVE (NRPT + Hosts)", "АКТИВНО (NRPT + Hosts)")
            dns_color = COLOR_GREEN
        elif hosts_act:
            dns_status = tr(f"ACTIVE (Hosts: {hosts_ip})", f"АКТИВНО (Hosts: {hosts_ip})")
            dns_color = COLOR_GREEN
        elif nrpt.get("active"):
            srv_lbl = nrpt.get("server", "Unblock DNS")
            dns_status = tr(f"ACTIVE ({srv_lbl})", f"АКТИВНО ({srv_lbl})")
            dns_color = COLOR_GREEN
        elif not is_nrpt_supported():
            dns_status = tr("UNSUPPORTED", "НЕ ПОДДЕРЖ.")
            dns_color = COLOR_DIM
        else:
            dns_status = tr("DISABLED", "ВЫКЛ")
            dns_color = COLOR_YELLOW
        print_menu_row("14", f"{tr('Selective DNS & Routing', 'DNS и роутинг')} [{color(dns_status, dns_color)}]",
                       tr("route 400 endpoints without VPN (NRPT/Hosts)", "доступ без VPN через NRPT / Hosts"), COLOR_YELLOW)

        print_menu_section(tr("TOOLS", "ИНСТРУМЕНТЫ"))
        print_menu_row("15", tr("Open GitHub repository", "Открыть GitHub репозиторий"),
                       tr("source & updates", "исходный код и релизы"), COLOR_CYAN)
        print_menu_row("16", tr("Select custom path", "Указать свой путь"),
                       tr("override auto-detected target", "ручной выбор папки приложения"), COLOR_CYAN)
        print_menu_row("17", tr("About program", "О программе"),
                       tr("information & author links", "информация и ссылки автора"), COLOR_CYAN)
        print_menu_row("18", tr("Install Antigravity Assistant", "Установить Assistant (UI)"),
                       tr("deploy extension & export VSIX", "установка расширения и экспорт VSIX"), COLOR_CYAN)
        print_menu_row("19", tr("Check for cloud updates", "Обновления патчера"),
                       tr("auto-download & apply latest release", "скачать и установить новый релиз"), COLOR_CYAN)
        print_menu_row("20", tr("Rebuild & repair chat history", "Восстановить историю чатов"),
                       tr("restore missing/ordered conversations", "вернуть пропавшие диалоги и порядок"), COLOR_CYAN)
        cur_lang_code = get_lang().upper()
        lang_row_label = tr(
            f"Language: English [{color(cur_lang_code, COLOR_GREEN)}]",
            f"Язык: Русский [{color(cur_lang_code, COLOR_GREEN)}]",
        )
        lang_row_hint = tr("switch to Русский (press L)", "переключить на English (нажмите L)")
        print_menu_row("L", lang_row_label, lang_row_hint, COLOR_CYAN)

        print()
        print_menu_row("0", tr("Exit", "Выход"), tr("quit the patcher (0 / q / Esc)", "закрыть патчер (0 / q / Esc)"), COLOR_RED)
        print_menu_footer(tr(
            "Enter — refresh  |  L — EN/RU  |  0 / q / Esc — exit  |  Submenus: Enter / 0 / b — back.",
            "Enter — обновить  |  L — RU/EN  |  0 / q / Esc — выход  |  Подменю: Enter / 0 / b — назад.",
        ))

        valid_choices = {str(i) for i in range(1, 22)} | {"l", "lang", "язык"}
        choice = None
        while True:
            raw_choice = _read_menu_input(tr("\n  Select option > ", "\n  Выберите пункт > "), default_on_eof="0")
            if raw_choice in ("0", "q", "quit", "exit"):
                return
            if raw_choice in ("", "back", "b", "назад"):
                choice = ""
                break
            if raw_choice in valid_choices:
                choice = raw_choice
                break
            err(tr(
                f"Unknown option '{raw_choice}' — enter 1-20, L to switch language, Enter to refresh, or 0/q to exit.",
                f"Неизвестный пункт '{raw_choice}' — введите 1-20, L для смены языка, Enter для обновления или 0/q для выхода.",
            ))

        print()
        if choice == "":
            redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                               vscode_agy_path, show_search_line=False)
            continue

        if choice in ("21", "l", "lang", "язык"):
            toggle_lang()
            redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                               vscode_agy_path, show_search_line=False)
            continue

        handled = True
        if choice not in ("10", "16", "17"):
            clear_screen()
            print_banner()

        if choice == "1":
            handled = True
            try:
                info(tr("Applying all patches in sequence...", "Последовательное применение всех патчей..."))
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
                    print_menu_section(tr("1 / 5 - Antigravity IDE patch", "1 / 5 - Патч Antigravity IDE"))
                    _, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                    do_patch(main_js_path)
                else:
                    warn(tr("IDE (main.js) not found - skipping (use TOOLS -> 16 to set path)",
                            "IDE (main.js) не найден - пропуск (укажите путь в пункте 16)"))

                # 2/5 - Manager patch
                if manager_path and os.path.isfile(manager_path):
                    print_menu_section(tr("2 / 5 - Antigravity 2.0 patch", "2 / 5 - Патч Antigravity 2.0"))
                    _, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                    do_patch_manager(manager_path)
                else:
                    warn(tr("Antigravity 2.0 (language_server) not found - skipping",
                            "Antigravity 2.0 (language_server) не найден - пропуск"))

                # 3/5 - CLI patch
                if not agy_path or not os.path.isfile(agy_path):
                    agy_path = find_agy_binary() or ""
                if agy_path and os.path.isfile(agy_path):
                    print_menu_section(tr("3 / 5 - Antigravity CLI (agy) patch", "3 / 5 - Патч Antigravity CLI (agy)"))
                    _, agy_path = _maybe_upgrade_unlocked_target("cli", agy_path, main_js_path)
                    do_patch_agy(agy_path)
                else:
                    warn(tr("Antigravity CLI (agy) not found - skipping",
                            "Antigravity CLI (agy) не найден - пропуск"))

                # 4/5 - VS Code - same flow as choice "5"
                print_menu_section(tr("4 / 5 - Antigravity VS Code patch", "4 / 5 - Патч Antigravity VS Code"))
                do_patch_vscode_flow(vscode_path, vscode_agy_path,
                                     set_js_cb=None, set_agy_cb=None,
                                     silent_if_missing=True)
                print()

                # 5/5 - Antigravity Assistant Extension
                print_menu_section(tr("5 / 5 - Antigravity Assistant Extension", "5 / 5 - Расширение Antigravity Assistant"))
                from patcher.extension_installer import deploy_assistant_extension
                deploy_assistant_extension(export_vsix=False)
                print()
                ok(tr("All available patches and extension applied.",
                      "Все доступные патчи и расширение успешно применены."))

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
                    hint(tr("You can now start Antigravity normally.",
                            "Теперь можно запускать Antigravity в обычном режиме."))
            except Exception as exc:
                import traceback
                err(tr(f"Unexpected error while applying all patches: {exc}",
                       f"Ошибка при применении всех патчей: {exc}"))
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
                    else:
                        handled = False
            except Exception as exc:
                import traceback
                err(tr(f"Unexpected error patching Antigravity IDE: {exc}",
                       f"Ошибка при патче Antigravity IDE: {exc}"))
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
                    else:
                        handled = False
            except Exception as exc:
                import traceback
                err(tr(f"Unexpected error patching Antigravity 2.0: {exc}",
                       f"Ошибка при патче Antigravity 2.0: {exc}"))
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
                    else:
                        handled = False
            except Exception as exc:
                import traceback
                err(tr(f"Unexpected error patching Antigravity CLI: {exc}",
                       f"Ошибка при патче Antigravity CLI: {exc}"))
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
                err(tr(f"Unexpected error patching VS Code extension: {exc}",
                       f"Ошибка при патче расширения VS Code: {exc}"))
                copy_sanitized_bug_report_to_clipboard(
                    traceback.format_exc(),
                    context_info="choice=5 Antigravity VS Code patch",
                )
        elif choice == "6":
            handled = True
            print_menu_section(tr("RESTORE ALL TARGETS FROM BACKUP", "ВОССТАНОВЛЕНИЕ ВСЕХ ЦЕЛЕЙ ИЗ БЭКАПА"))
            print()
            hint(tr("This will restore ALL applications from their original backups:",
                    "Будут восстановлены ВСЕ приложения из исходных резервных копий:"))
            hint("  • Antigravity IDE (main.js)")
            hint("  • Antigravity 2.0 (language_server)")
            hint("  • Antigravity CLI (agy)")
            hint(tr("  • Antigravity VS Code extension (extension.js + agy)",
                    "  • Расширение Antigravity VS Code (extension.js + agy)"))
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

            info(tr("Restoring all targets in sequence...", "Последовательное восстановление из бэкапов..."))
            print()

            # 1/4 - IDE restore
            print_menu_section(tr("1 / 4 - Antigravity IDE restore", "1 / 4 - Откат Antigravity IDE"))
            if main_js_path and os.path.isfile(main_js_path):
                do_restore(main_js_path, show_search_line=False, force=True)
            else:
                warn(tr("Antigravity IDE (main.js) not found - skipping (use TOOLS -> 16 to set path)",
                        "Antigravity IDE (main.js) не найден - пропуск (укажите путь в пункте 16)"))

            # 2/4 - Manager restore
            print_menu_section(tr("2 / 4 - Antigravity 2.0 restore", "2 / 4 - Откат Antigravity 2.0"))
            if manager_path and os.path.isfile(manager_path):
                do_restore_manager(manager_path, force=True)
            else:
                warn(tr("Antigravity 2.0 (language_server) not found - skipping",
                        "Antigravity 2.0 (language_server) не найден - пропуск"))

            # 3/4 - CLI restore
            print_menu_section(tr("3 / 4 - Antigravity CLI restore", "3 / 4 - Откат Antigravity CLI"))
            cli_target = agy_path or vscode_agy_path
            if cli_target and os.path.isfile(cli_target):
                do_restore_agy(cli_target, force=True)
            else:
                warn(tr("Antigravity CLI (agy) not found - skipping",
                        "Antigravity CLI (agy) не найден - пропуск"))

            # 4/4 - VS Code restore
            print_menu_section(tr("4 / 4 - Antigravity VS Code restore", "4 / 4 - Откат Antigravity VS Code"))
            if vscode_path and os.path.isfile(vscode_path):
                do_restore_vscode(vscode_path, force=True)
            else:
                warn(tr("Antigravity VS Code extension not found - skipping",
                        "Расширение Antigravity VS Code не найдено - пропуск"))
            print()
            ok(tr("All available targets restored from backup.",
                  "Все доступные цели восстановлены из резервных копий."))
        elif choice == "7":
            if manager_path:
                do_restore_manager(manager_path, force=True)
            else:
                err(tr("Antigravity 2.0 (language_server) path is not set. Please select custom path (Option 16) first.",
                       "Путь к Antigravity 2.0 не задан. Сначала выберите путь (пункт 16)."))
        elif choice == "8":
            if agy_path:
                do_restore_agy(agy_path, force=True)
            else:
                err(tr("Antigravity CLI path is not set. Please select custom path (Option 16) first.",
                       "Путь к Antigravity CLI не задан. Сначала выберите путь (пункт 16)."))
        elif choice == "9":
            if vscode_path:
                do_restore_vscode(vscode_path, force=True)
            else:
                err(tr("Antigravity VS Code extension path is not set. Please select custom path (Option 16) first.",
                       "Путь к расширению VS Code не задан. Сначала выберите путь (пункт 16)."))
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
                    print_menu_section(tr("AUTO-UPDATE MANAGEMENT", "УПРАВЛЕНИЕ АВТООБНОВЛЕНИЯМИ"))
                    print()
                    ide_is_locked = is_ide_locked(_ide_root_dir or None)
                    mgr_is_locked = is_manager_locked()
                    vsc_is_locked = is_vscode_locked()
                    all_is_locked = is_update_locked()

                    _lbl_locked = tr("[LOCKED]", "[ЗАБЛОК.]")
                    _lbl_enabled = tr("[ENABLED]", "[ВКЛЮЧЕНО]")
                    ide_st = color(_lbl_locked, COLOR_RED) if ide_is_locked else color(_lbl_enabled, COLOR_GREEN)
                    mgr_st = color(_lbl_locked, COLOR_RED) if mgr_is_locked else color(_lbl_enabled, COLOR_GREEN)
                    vsc_st = color(_lbl_locked, COLOR_RED) if vsc_is_locked else color(_lbl_enabled, COLOR_GREEN)

                    ide_ver_txt, ide_upd_hint = _target_ver_label("ide", main_js_path, not ide_is_locked)
                    mgr_ver_txt, mgr_upd_hint = _target_ver_label("manager", manager_path, not mgr_is_locked)

                    print(tr("  Current Status:", "  Текущий статус:"))
                    print(f"    • Antigravity IDE{ide_ver_txt or ''}:   {ide_st}")
                    print(f"    • Antigravity 2.0{mgr_ver_txt or ''}:   {mgr_st}")
                    print(f"    • Antigravity VS Code:        {vsc_st}")
                    print()

                    all_action = (
                        tr("Unlock all auto-updates", "Разблокировать все обновления")
                        if all_is_locked else
                        tr("Lock all auto-updates", "Заблокировать все обновления")
                    )
                    all_hint = (
                        tr("unblock & check updates now (IDE, 2.0 & VS Code)", "разрешить и сразу проверить обновления")
                        if all_is_locked else
                        tr("block IDE, 2.0 & VS Code", "запретить обновления IDE, 2.0 и VS Code")
                    )
                    _tgl = tr("toggle lock", "перекл. блок.")
                    print_menu_row("1", all_action, all_hint, COLOR_YELLOW)
                    print_menu_row("2", tr("Antigravity IDE updates", "Обновления Antigravity IDE"),
                                   f"{_tgl} ({ide_upd_hint or ide_st})", COLOR_GREEN)
                    print_menu_row("3", tr("Antigravity 2.0 updates", "Обновления Antigravity 2.0"),
                                   f"{_tgl} ({mgr_upd_hint or mgr_st})", COLOR_GREEN)
                    print_menu_row("4", tr("Antigravity VS Code updates", "Обновления VS Code"),
                                   tr(f"toggle lock (currently {vsc_st})", f"{_tgl} (сейчас {vsc_st})"), COLOR_GREEN)
                    print_menu_row("5", tr("Check & install available updates now", "Проверить и установить обновления сейчас"),
                                   tr("download, close app, update, re-patch & relaunch", "скачать, закрыть, обновить и заново пропатчить"), COLOR_CYAN)
                    print()
                    print_menu_row("0", tr("Back to main menu", "Назад в главное меню"), "Enter / 0 / b", COLOR_RED)
                    print_menu_footer(tr(
                        "0 / Enter / b — Back  |  [5] forces immediate update check & install.",
                        "0 / Enter / b — Назад  |  [5] принудительно проверяет и ставит обновления.",
                    ))

                    sub_choice = None
                    while True:
                        raw_sub = _read_menu_input(tr("\n  Select option > ", "\n  Выберите пункт > "))
                        if raw_sub in _BACK_CHOICES:
                            sub_choice = "0"
                            break
                        if raw_sub in ("1", "2", "3", "4", "5"):
                            sub_choice = raw_sub
                            break
                        err(tr(
                            f"Unknown option '{raw_sub}' — enter 1-5 or press Enter / 0 / b to go back.",
                            f"Неизвестный пункт '{raw_sub}' — введите 1-5 или Enter / 0 / b для возврата.",
                        ))
                    if sub_choice == "0":
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
                            info(tr("No newer IDE / 2.0 / CLI updates found right now.",
                                    "Новых обновлений IDE / 2.0 / CLI сейчас нет."))
                        pause(tr("  Press Enter (or any key) to continue...",
                                 "  Нажмите Enter (или любую клавишу), чтобы продолжить..."))
                    elif sub_choice == "1":
                        print()
                        was_ide_locked = ide_is_locked
                        was_mgr_locked = mgr_is_locked
                        do_toggle_updates(_ide_root_dir or None)
                        invalidate_menu_status_cache("update_locked")
                        now_ide_unlocked = not is_ide_locked(_ide_root_dir or None)
                        now_mgr_unlocked = not is_manager_locked()
                        if was_ide_locked and now_ide_unlocked and main_js_path and os.path.isfile(main_js_path):
                            did_i, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                            if did_i:
                                _apply_patch_deploy_and_launch_for_target("ide", main_js_path, launch_after=True)
                        if was_mgr_locked and now_mgr_unlocked and manager_path and os.path.isfile(manager_path):
                            did_m, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                            if did_m:
                                _apply_patch_deploy_and_launch_for_target("manager", manager_path, launch_after=True)
                        pause(tr("  Press Enter (or any key) to continue...",
                                 "  Нажмите Enter (или любую клавишу), чтобы продолжить..."))
                    elif sub_choice == "2":
                        print()
                        was_ide_locked = ide_is_locked
                        toggle_ide_updates(_ide_root_dir or None)
                        invalidate_menu_status_cache("update_locked")
                        now_ide_unlocked = not is_ide_locked(_ide_root_dir or None)
                        if was_ide_locked and now_ide_unlocked and main_js_path and os.path.isfile(main_js_path):
                            did_i, main_js_path = _maybe_upgrade_unlocked_target("ide", main_js_path, main_js_path)
                            if did_i:
                                _apply_patch_deploy_and_launch_for_target("ide", main_js_path, launch_after=True)
                        pause(tr("  Press Enter (or any key) to continue...",
                                 "  Нажмите Enter (или любую клавишу), чтобы продолжить..."))
                    elif sub_choice == "3":
                        print()
                        was_mgr_locked = mgr_is_locked
                        toggle_manager_updates()
                        invalidate_menu_status_cache("update_locked")
                        now_mgr_unlocked = not is_manager_locked()
                        if was_mgr_locked and now_mgr_unlocked and manager_path and os.path.isfile(manager_path):
                            did_m, manager_path = _maybe_upgrade_unlocked_target("manager", manager_path, main_js_path)
                            if did_m:
                                _apply_patch_deploy_and_launch_for_target("manager", manager_path, launch_after=True)
                        pause(tr("  Press Enter (or any key) to continue...",
                                 "  Нажмите Enter (или любую клавишу), чтобы продолжить..."))
                    elif sub_choice == "4":
                        print()
                        toggle_vscode_updates()
                        invalidate_menu_status_cache("update_locked")
                        pause(tr("  Press Enter (or any key) to continue...",
                                 "  Нажмите Enter (или любую клавишу), чтобы продолжить..."))
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
                    err(tr("Antigravity IDE (main.js) not found. Set path first (Option 16).",
                           "Antigravity IDE (main.js) не найден. Сначала укажите путь (пункт 16)."))
            else:
                err(tr("tls_fix module not available.", "Модуль tls_fix недоступен."))
        elif choice == "12":
            if _HAVE_DIAGNOSTICS:
                print_target_info(main_js_path, manager_path, agy_path, vscode_path=vscode_path,
                                  vscode_agy_path=vscode_agy_path, show_search_line=False)
                print()
                do_diagnostics(main_js_path, manager_path, agy_path,
                               vscode_path=vscode_path, vscode_agy_path=vscode_agy_path)
                invalidate_menu_status_cache()
            else:
                err(tr("diagnostics module not available.", "Модуль diagnostics недоступен."))
        elif choice == "13":
            if _HAVE_DATA_RESET:
                if do_data_reset() is False:
                    handled = False
                else:
                    invalidate_menu_status_cache("update_locked")
            else:
                err(tr("data_reset module not available.", "Модуль data_reset недоступен."))
        elif choice == "14":
            from patcher.nrpt_dns import (
                enable_nrpt_rule, disable_nrpt_rule,
                get_dns_config, PUBLIC_UNBLOCK_SERVERS,
                get_preferred_dns_ip
            )
            from patcher.hosts_pin import (
                enable_hosts_pinning, disable_hosts_pinning
            )
            print_menu_section(tr("SELECTIVE DNS & ROUTING (NO VPN REQUIRED)",
                                  "ВЫБОРОЧНЫЙ DNS И РОУТИНГ (БЕЗ VPN)"))
            nrpt = _get_cached_nrpt_status()
            cfg = get_dns_config()
            is_active = bool(nrpt.get("active"))
            current_server = nrpt.get("server", "") or cfg.get("server_ip", "")
            status_str = (
                tr(f"ACTIVE ({current_server})", f"АКТИВНО ({current_server})")
                if is_active else tr("DISABLED", "ВЫКЛ")
            )

            hosts_active, hosts_ip = _get_cached_hosts_pinned()
            hosts_status_str = (
                tr(f"ACTIVE ({hosts_ip})", f"АКТИВНО ({hosts_ip})")
                if hosts_active else tr("DISABLED", "ВЫКЛ")
            )

            print()
            info(f"{tr('NRPT DNS Status:', 'Статус NRPT DNS:')}     {color(status_str, COLOR_GREEN if is_active else COLOR_YELLOW)}")
            info(f"{tr('Hosts-Pinning (VPN):', 'Hosts-Pinning (VPN):')} {color(hosts_status_str, COLOR_GREEN if hosts_active else COLOR_YELLOW)}")
            print()
            hint(tr("Routes 'User location is not supported' endpoints for cloudcode-pa & daily:",
                    "Маршрутизирует запросы cloudcode-pa и daily при ошибке региона:"))
            hint(tr("  • [1] NRPT DNS: routes Google Cloud queries via selective Windows policy table (no VPN).",
                    "  • [1] NRPT DNS: выборочная маршрутизация Google Cloud в Windows (без VPN)."))
            hint(tr("  • [4] Direct Hosts-Pinning: pins verified SNI proxy (100% works WITH and WITHOUT VPN).",
                    "  • [4] Прямой Hosts-Pinning: закрепляет проверенный SNI-узел (работает и С VPN, и БЕЗ него)."))
            print()
            if is_active:
                print_menu_row("1", tr("Disable NRPT DNS Routing (Restore standard)", "Отключить NRPT DNS (вернуть стандартный)"),
                               tr(f"currently active via {current_server} — click to turn off", f"сейчас активен {current_server}"), COLOR_RED)
                print_menu_row("2", tr("Connect Private Ultra DNS / Change Server", "Подключить приватный DNS / сменить сервер"),
                               tr("switch or update private DNS server", "указать свой приватный DNS IP"), COLOR_CYAN)
            else:
                target_ip = get_preferred_dns_ip(cfg)
                mode_label = tr("Private DNS", "Приватный DNS") if cfg.get("mode") == "private" else tr("Public Unblock DNS", "Публичный DNS")
                print_menu_row("1", tr(f"Enable NRPT DNS Routing ({mode_label})", f"Включить NRPT DNS ({mode_label})"),
                               tr(f"use resolver ({target_ip})", f"сервер ({target_ip})"), COLOR_GREEN)
                print_menu_row("2", tr("Connect Private Ultra DNS / Premium Server", "Подключить приватный DNS / сервер"),
                               tr("enter your private DNS IP or server", "ввести IP/адрес приватного DNS"), COLOR_CYAN)

            if hosts_active:
                print_menu_row("4", tr("Disable Direct Hosts-Pinning", "Отключить прямой Hosts-Pinning"),
                               tr(f"remove proxy mapping ({hosts_ip}) from hosts", f"убрать узел ({hosts_ip}) из hosts"), COLOR_RED)
            else:
                print_menu_row("4", tr("Enable Direct Hosts-Pinning (VPN-Proof)", "Включить Hosts-Pinning (для VPN и без)"),
                               tr("pin verified SNI proxy to etc/hosts (recommended with VPN)", "закрепить проверенный SNI-IP в etc/hosts"), COLOR_GREEN)

            print_menu_row("0", tr("Back to main menu", "Назад в главное меню"), "Enter / 0 / b", COLOR_YELLOW)
            print_menu_footer(tr("0 / Enter / b — Back to main menu.", "0 / Enter / b — Назад в главное меню."))
            print()
            sub = _read_menu_input(tr("  Select option > ", "  Выберите пункт > "))
            if sub in _BACK_CHOICES:
                handled = False
            elif sub == "3" or (sub == "1" and is_active):
                ok_flag, msg = disable_nrpt_rule()
                invalidate_menu_status_cache("nrpt_status")
                if ok_flag:
                    ok(tr("NRPT DNS routing disabled. Standard DNS restored.",
                          "NRPT DNS отключён. Восстановлен стандартный DNS."))
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
                hint(tr("Enter your dedicated DNS IP or server hostname (or 0 / Enter / b to cancel):",
                        "Введите IP или адрес выделенного DNS-сервера (или 0 / Enter / b для отмены):"))
                custom_ip = _read_path_input(tr("  Private DNS IP / Host > ", "  Приватный DNS IP / Хост > "))
                if not custom_ip or custom_ip.lower() in _BACK_CHOICES:
                    cancel(tr("Cancelled.", "Отменено."))
                    handled = False
                else:
                    ok_flag, msg = enable_nrpt_rule(custom_ip, mode="private")
                    invalidate_menu_status_cache("nrpt_status")
                    if ok_flag:
                        ok(tr(f"Private DNS activated: {msg}", f"Приватный DNS активирован: {msg}"))
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
                err(tr("Invalid choice", "Неверный выбор"))
                handled = True
        elif choice == "15":
            webbrowser.open(GITHUB_URL)
            ok(f"{tr('Opening GitHub repository in browser', 'Открываю репозиторий GitHub')}: {color(GITHUB_URL, COLOR_CYAN)}")
        elif choice == "16":
            while True:
                redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                                   vscode_agy_path, show_search_line=False)
                print_menu_section(tr("SELECT CUSTOM PATH", "РУЧНОЙ ВЫБОР ПУТИ"))
                print_menu_row("1", tr("Antigravity IDE path", "Путь к Antigravity IDE"),
                               tr("folder or main.js", "папка или main.js"), COLOR_GREEN)
                print_menu_row("2", tr("Antigravity 2.0 path", "Путь к Antigravity 2.0"),
                               tr("folder or language_server binary", "папка или бинарник language_server"), COLOR_GREEN)
                print_menu_row("3", tr("Antigravity CLI path", "Путь к Antigravity CLI"),
                               tr("agy.exe or folder", "agy.exe или папка"), COLOR_GREEN)
                print_menu_row("4", tr("VS Code extension path (JS)", "Путь к расширению VS Code (JS)"),
                               tr("extension.js or extensions folder", "extension.js или папка extensions"), COLOR_GREEN)
                print_menu_row("5", tr("VS Code binary path (agy)", "Путь к бинарнику VS Code (agy)"),
                               tr("agy/antigravity binary in ~/.gemini/bin", "файл agy/antigravity в ~/.gemini/bin"), COLOR_GREEN)
                print()
                print_menu_row("0", tr("Back to main menu", "Назад в главное меню"), "Enter / 0 / b", COLOR_RED)
                print_menu_footer(tr(
                    "0 / Enter / b — Back  |  Leaves auto-detection results intact for other targets.",
                    "0 / Enter / b — Назад  |  Автоопределённые пути других целей не сбрасываются.",
                ))

                sub_choice = None
                while True:
                    raw_sub = _read_menu_input(tr("\n  Select option > ", "\n  Выберите пункт > "))
                    if raw_sub in _BACK_CHOICES:
                        sub_choice = "0"
                        break
                    if raw_sub in ("1", "2", "3", "4", "5"):
                        sub_choice = raw_sub
                        break
                    err(tr(
                        f"Unknown option '{raw_sub}' — enter 1-5 or press Enter / 0 / b to go back.",
                        f"Неизвестный пункт '{raw_sub}' — введите 1-5 или Enter / 0 / b для возврата.",
                    ))
                if sub_choice == "0":
                    handled = False
                    break

                _cont_prompt = tr("  Press Enter (or any key) to continue...",
                                  "  Нажмите Enter (или любую клавишу), чтобы продолжить...")
                if sub_choice == "1":
                     print()
                     hint(tr("Enter the path to Antigravity IDE folder or main.js file (or 0 / Enter / b to cancel).",
                             "Введите путь к папке Antigravity IDE или к файлу main.js (0 / Enter / b — отмена)."))
                     print_path_examples()
                     raw = _read_path_input(tr("\n  IDE Path > ", "\n  Путь к IDE > "))
                     if not raw or raw.lower() in _BACK_CHOICES:
                         cancel(tr("Cancelled.", "Отменено."))
                         continue
                     new_path = resolve_target_path(raw)
                     if new_path and os.path.exists(new_path):
                         if os.path.isdir(new_path):
                             new_path = find_main_js(new_path)
                         if new_path and os.path.isfile(new_path):
                             main_js_path = new_path
                             ok(tr("Antigravity IDE path updated!", "Путь к Antigravity IDE обновлён!"))
                         else:
                             err(tr("Could not resolve a valid Antigravity IDE target (main.js not found).",
                                    "Не удалось найти корректную цель Antigravity IDE (main.js не найден)."))
                     else:
                         err(tr("Path does not exist.", "Указанный путь не существует."))
                     print()
                     pause(_cont_prompt)
                elif sub_choice == "2":
                     print()
                     hint(tr("Enter the path to Antigravity 2.0 app folder or language_server binary (or 0 / Enter / b to cancel).",
                             "Введите путь к папке Antigravity 2.0 или бинарнику language_server (0 / Enter / b — отмена)."))
                     print_path_examples()
                     raw = _read_path_input(tr("\n  Antigravity 2.0 Path > ", "\n  Путь к Antigravity 2.0 > "))
                     if not raw or raw.lower() in _BACK_CHOICES:
                         cancel(tr("Cancelled.", "Отменено."))
                         continue
                     new_path = resolve_manager_path(raw)
                     if new_path and os.path.isfile(new_path):
                         manager_path = new_path
                         ok(tr("Antigravity 2.0 path updated!", "Путь к Antigravity 2.0 обновлён!"))
                     else:
                         err(tr("Could not resolve a valid Antigravity 2.0 target (language_server not found).",
                                "Не удалось найти корректную цель Antigravity 2.0 (language_server не найден)."))
                     print()
                     pause(_cont_prompt)
                elif sub_choice == "3":
                     print()
                     hint(tr("Enter the path to the agy binary (agy.exe) or its folder (or 0 / Enter / b to cancel).",
                             "Введите путь к бинарнику agy (agy.exe) или его папке (0 / Enter / b — отмена)."))
                     print_path_examples()
                     raw = _read_path_input(tr("\n  AGY Path > ", "\n  Путь к AGY > "))
                     if not raw or raw.lower() in _BACK_CHOICES:
                         cancel(tr("Cancelled.", "Отменено."))
                         continue
                     new_path = resolve_agy_path(raw)
                     if new_path and os.path.isfile(new_path):
                         agy_path = new_path
                         ok(tr("Antigravity CLI path updated!", "Путь к Antigravity CLI обновлён!"))
                     else:
                         err(tr("Could not resolve a valid Antigravity CLI target.",
                                "Не удалось найти корректную цель Antigravity CLI."))
                     print()
                     pause(_cont_prompt)
                elif sub_choice == "4":
                     print()
                     hint(tr("Enter the path to extension.js, the google.google-antigravity-*",
                             "Введите путь к extension.js, папке google.google-antigravity-*"))
                     hint(tr("extension folder, or the .vscode/extensions root folder (or 0 / Enter / b to cancel).",
                             "или корневой папке .vscode/extensions (0 / Enter / b — отмена)."))
                     raw = _read_path_input(tr("\n  VS Code Extension Path (JS) > ", "\n  Путь к расширению VS Code (JS) > "))
                     if not raw or raw.lower() in _BACK_CHOICES:
                         cancel(tr("Cancelled.", "Отменено."))
                         continue
                     new_path = resolve_extension_path(raw)
                     if new_path and os.path.isfile(new_path):
                         vscode_path = new_path
                         ok(tr("Antigravity VS Code extension path updated!", "Путь к расширению VS Code обновлён!"))
                     else:
                         err(tr("Could not resolve a valid VS Code extension target (extension.js not found).",
                                "Не удалось найти расширение VS Code (extension.js не найден)."))
                     print()
                     pause(_cont_prompt)
                elif sub_choice == "5":
                     print()
                     hint(tr("Enter the path to the agy/antigravity binary downloaded",
                             "Введите путь к бинарнику agy/antigravity, скачанному"))
                     hint(tr(f"by the extension ({describe_gemini_binary_path()}) or any agy file (or 0 / Enter / b to cancel).",
                             f"расширением ({describe_gemini_binary_path()}), или к любому файлу agy (0 / Enter / b — отмена)."))
                     raw = _read_path_input(tr("\n  VS Code Binary Path (agy) > ", "\n  Путь к бинарнику VS Code (agy) > "))
                     if not raw or raw.lower() in _BACK_CHOICES:
                         cancel(tr("Cancelled.", "Отменено."))
                         continue
                     new_path = resolve_agy_path(raw)
                     if new_path and os.path.isfile(new_path):
                         vscode_agy_path = new_path
                         ok(tr("VS Code agy binary path updated!", "Путь к бинарнику VS Code agy обновлён!"))
                     else:
                         err(tr("Could not resolve a valid VS Code agy binary target.",
                                "Не удалось найти бинарник VS Code agy."))
                     print()
                     pause(_cont_prompt)
        elif choice == "17":
            show_about()
            print()
            pause(tr("  Press Enter (or any key) to return to menu...",
                     "  Нажмите Enter (или любую клавишу) для возврата в меню..."))
            handled = False
        elif choice == "18":
            print_menu_section(tr("INSTALL ANTIGRAVITY ASSISTANT EXTENSION", "УСТАНОВКА РАСШИРЕНИЯ ANTIGRAVITY ASSISTANT"))
            info(tr("Deploying Antigravity Assistant extension (Account Switcher, Quotas, Ping)...",
                    "Установка расширения Antigravity Assistant (переключение аккаунтов, квоты, пинг)..."))
            from patcher.extension_installer import deploy_assistant_extension
            res = deploy_assistant_extension(export_vsix=True)
            if res:
                ok(tr("Antigravity Assistant successfully installed to Antigravity IDE and VS Code!",
                      "Antigravity Assistant успешно установлен в Antigravity IDE и VS Code!"))
                hint(tr("Status Bar item $(shield) will appear on next editor launch.",
                        "Иконка $(shield) появится в нижней панели после запуска редактора."))
            else:
                err(tr("Failed to deploy extension.", "Не удалось установить расширение."))
            handled = True
        elif choice == "19":
            print_menu_section(tr("CHECK FOR CLOUD UPDATES", "ПРОВЕРКА ОБНОВЛЕНИЙ ПАТЧЕРА"))
            from patcher.utils.update import perform_cloud_update
            perform_cloud_update(interactive=False)
            handled = True
        elif choice == "20":
            from patcher.conversations.rebuilder import run_rebuild_wizard
            if run_rebuild_wizard() is False:
                handled = False
            else:
                handled = True

        print()

        if handled:
            pause()
        redraw_main_screen(main_js_path, manager_path, agy_path, vscode_path,
                           vscode_agy_path, show_search_line=False)










