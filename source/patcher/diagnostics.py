r"""
patcher/diagnostics.py
----------------------
Режим диагностики - читает реальные логи Antigravity IDE и Antigravity 2.0,
распознаёт известные ошибки и применяет автофиксы за один проход.

Логи хранятся в:
  Windows: %APPDATA%\Antigravity IDE\logs\<session>\  и  %APPDATA%\Antigravity\logs\
  macOS:   ~/Library/Application Support/Antigravity IDE/logs/<session>/
  Linux:   ~/.config/Antigravity IDE/logs/<session>/

Ключевые файлы:
  language_server.log / ls-main.log - language server (TLS/429/400/403/500/сети)
  auth.log                          - авторизация Google
  cloudcode.log                     - Cloud Code API
  main.log                          - основной процесс IDE / Electron
  exthost.log / 2-Antigravity.log   - логи запуска бинарника расширения
"""

from __future__ import annotations

import os
import sys
import re
import socket
import platform
import subprocess
import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, List, Dict, Tuple, Any

from patcher.utils.console import (
    ok, warn, err, info, hint, step, color, cancel,
    print_menu_section, print_menu_row, print_menu_footer,
    COLOR_CYAN, COLOR_GREEN, COLOR_RED, COLOR_YELLOW, COLOR_BOLD, COLOR_DIM, COLOR_WHITE
)


# ═══════════════════════════════════════════════════════════════════════════════
# БАЗА ИЗВЕСТНЫХ ОШИБОК
# ═══════════════════════════════════════════════════════════════════════════════

class KnownError:
    """Описание известной ошибки с правилами детекции, авто-фикса и кнопки в меню."""
    def __init__(self, key: str, title: str, patterns: List[str],
                 description: str, fix_label: str, fix_fn=None, severity: str = "ERROR",
                 menu_button: str = "", is_already_fixed_fn=None,
                 resolve_menu_button_fn=None):
        self.key        = key
        self.title      = title
        self.patterns   = [re.compile(p, re.IGNORECASE) for p in patterns]
        self.description = description
        self.fix_label  = fix_label
        self.fix_fn     = fix_fn   # callable(ctx) -> bool | None
        self.is_already_fixed_fn = is_already_fixed_fn # callable(ctx) -> bool
        self.severity   = severity # "ERROR" | "WARN" | "INFO"
        self.menu_button = menu_button  # Единственная кнопка в главном меню для ручного фикса
        self.resolve_menu_button_fn = resolve_menu_button_fn  # optional callable(ctx) -> str
        self.hits: List[str] = []       # строки из логов, совпавшие с паттерном
        self.fix_status: str = "Не запускался"

    def resolve_menu_button(self, ctx: Optional[dict] = None) -> str:
        """Возвращает актуальную единственную кнопку меню с учётом состояния системы."""
        if self.resolve_menu_button_fn and ctx is not None:
            try:
                resolved = self.resolve_menu_button_fn(ctx)
                if resolved:
                    self.menu_button = resolved
            except Exception:
                pass
        return self.menu_button

    def matches(self, line: str) -> bool:
        low = line.lower()
        # Исключаем ложные срабатывания, если фраза содержится в теле промпта пользователя
        if any(marker in low for marker in ("received prompt:", "prompt=", '"prompt":', '"messages":', "request body:")):
            return False
        return any(p.search(line) for p in self.patterns)


_PROXYCONNECT_RE = re.compile(
    r"proxyconnect tcp:\s*dial tcp\s+(?:\[([^\]]+)\]|([^:\s]+)):(\d+)",
    re.IGNORECASE,
)


def _check_tcp_reachable(host: str, port: int, timeout: float = 0.8) -> bool:
    """Проверяет, принимает ли TCP-подключения указанный локальный/удалённый прокси."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _parse_proxy_host_port(proxy_spec: str) -> Optional[Tuple[str, int]]:
    """Извлекает (host, port) из строки прокси (URL, host:port или Windows 'http=h:p;https=h:p')."""
    if not proxy_spec:
        return None
    raw = proxy_spec.strip()
    if "=" in raw and "://" not in raw:
        parts = [p.strip() for p in raw.split(";") if "=" in p]
        for pref in ("https=", "http="):
            match = next((p.split("=", 1)[1].strip() for p in parts if p.lower().startswith(pref)), None)
            if match:
                raw = match
                break
        else:
            raw = parts[0].split("=", 1)[1].strip() if parts else raw
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlparse(raw)
        if parsed.hostname and parsed.port:
            return parsed.hostname, int(parsed.port)
    except Exception:
        return None
    return None


def _extract_proxy_from_hits(hits: List[str]) -> Optional[Tuple[str, int]]:
    """Извлекает адрес (host, port) из строк с ошибкой proxyconnect."""
    for hit in hits:
        m = _PROXYCONNECT_RE.search(hit)
        if m:
            host = m.group(1) or m.group(2)
            try:
                return host, int(m.group(3))
            except (TypeError, ValueError):
                continue
    return None


def _read_windows_registry_proxy() -> Tuple[bool, str]:
    """Читает (ProxyEnable, ProxyServer) из HKCU\\...\\Internet Settings."""
    if sys.platform != "win32":
        return False, ""
    try:
        import winreg
        subkey = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as k:
            try:
                enable_val, _ = winreg.QueryValueEx(k, "ProxyEnable")
            except OSError:
                enable_val = 0
            try:
                server_val, _ = winreg.QueryValueEx(k, "ProxyServer")
            except OSError:
                server_val = ""
        return bool(enable_val), str(server_val or "")
    except Exception:
        return False, ""


def _disable_windows_registry_proxy() -> bool:
    """Отключает системный прокси Windows (ProxyEnable = 0) и уведомляет WinINet."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        subkey = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        try:
            import ctypes
            wininet = ctypes.windll.wininet
            wininet.InternetSetOptionW(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
            wininet.InternetSetOptionW(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH
        except Exception:
            pass
        return True
    except Exception:
        return False


def _clear_dead_user_env_proxies() -> List[Tuple[str, str, int]]:
    """Удаляет недоступные переменные прокси из HKCU\\Environment на Windows."""
    if sys.platform != "win32":
        return []
    cleared: List[Tuple[str, str, int]] = []
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as k:
            for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
                try:
                    val, _ = winreg.QueryValueEx(k, var)
                except OSError:
                    continue
                hp = _parse_proxy_host_port(str(val or ""))
                if hp and not _check_tcp_reachable(hp[0], hp[1], timeout=0.6):
                    winreg.DeleteValue(k, var)
                    cleared.append((var, hp[0], hp[1]))
    except Exception:
        pass
    if cleared:
        try:
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, None)
        except Exception:
            pass
    return cleared


def _get_dead_env_proxies() -> List[Tuple[str, str, int]]:
    """Возвращает [(VAR, host, port)] для переменных окружения с недоступным прокси."""
    dead: List[Tuple[str, str, int]] = []
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        hp = _parse_proxy_host_port(val)
        if hp and not _check_tcp_reachable(hp[0], hp[1], timeout=0.6):
            dead.append((var, hp[0], hp[1]))
    return dead


def _make_error_db() -> List[KnownError]:
    """Создаёт базу всех известных ошибок с их авто-фиксами и номерами кнопок в меню."""

    def _get_ide_path(ctx):
        p = ctx.get("main_js_path", "")
        if not p or not os.path.isfile(p):
            from patcher.ide.discovery import find_install_root, find_main_js
            root = find_install_root()
            if root:
                p = find_main_js(root) or ""
        return p if (p and os.path.isfile(p)) else ""

    def _get_manager_path(ctx):
        p = ctx.get("manager_path", "")
        if not p or not os.path.isfile(p):
            from patcher.manager.discovery import find_manager_binary
            p = find_manager_binary() or ""
        return p if (p and os.path.isfile(p)) else ""

    def _get_agy_path(ctx):
        p = ctx.get("agy_path", "")
        if not p or not os.path.isfile(p):
            from patcher.agy.discovery import find_agy_binary
            p = find_agy_binary() or ""
        return p if (p and os.path.isfile(p)) else ""

    def _get_vscode_agy_path(ctx):
        p = ctx.get("vscode_agy_path", "")
        if not p or not os.path.isfile(p):
            from patcher.vscode.discovery import find_gemini_antigravity_binary
            p = find_gemini_antigravity_binary() or ""
        return p if (p and os.path.isfile(p)) else ""

    def _flush_dns_safe():
        try:
            from patcher.nrpt_dns import _flush_dns_cache
            _flush_dns_cache()
        except Exception:
            pass

    def _is_tls_applied(ctx):
        p = _get_ide_path(ctx)
        if p and os.path.isfile(p):
            from patcher.tls_fix import is_tls_fix_applied
            return is_tls_fix_applied(p)
        return False

    def _is_ide_patched(ctx):
        p = _get_ide_path(ctx)
        if p and os.path.isfile(p):
            from patcher.ide.patcher import is_already_patched
            try:
                content = Path(p).read_text(encoding="utf-8", errors="replace")
                return bool(is_already_patched(content))
            except Exception:
                pass
        return False

    def _is_mgr_patched(ctx):
        mgr = _get_manager_path(ctx)
        if mgr and os.path.isfile(mgr):
            from patcher.manager.patcher import is_already_patched as is_mgr_patched
            try:
                return bool(is_mgr_patched(mgr))
            except Exception:
                pass
        return False

    def _is_agy_patched(path):
        if path and os.path.isfile(path):
            from patcher.agy.patcher import is_already_patched as is_cli_patched
            try:
                return bool(is_cli_patched(path))
            except Exception:
                pass
        return False

    def _can_resolve_google_dns(host: str = "daily-cloudcode-pa.googleapis.com") -> bool:
        try:
            return bool(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except Exception:
            return False

    def _should_touch_app(ctx: Optional[dict], app_name: str, err_keys: Tuple[str, ...]) -> bool:
        if not isinstance(ctx, dict):
            return True
        t_app = str(ctx.get("_target_app") or "all").lower()
        if t_app in ("ide", "manager", "cli"):
            return app_name == t_app
        err_apps = ctx.get("_error_apps")
        if not isinstance(err_apps, dict) or not err_apps:
            return True
        matched_tags = set()
        for k in err_keys:
            tags = err_apps.get(k)
            if tags:
                matched_tags.update(tags)
        if not matched_tags or "shared" in matched_tags:
            return True
        return app_name in matched_tags

    def _check_cloudcode_cached(ctx: Optional[dict]) -> dict:
        if isinstance(ctx, dict) and isinstance(ctx.get("_live_conn_cache"), dict):
            return ctx["_live_conn_cache"]
        res = _check_cloudcode_connectivity(timeout=2.5)
        if isinstance(ctx, dict):
            ctx["_live_conn_cache"] = res
        return res

    def _resolve_tls_btn(ctx):
        if not _get_ide_path(ctx):
            return "[16] Select custom path"
        return "[11] TLS certificate fix"

    def _resolve_network_auth_btn(ctx):
        if ctx.get("_network_failed_button"):
            return ctx["_network_failed_button"]
        net_keys = ("auth_failure", "ls_not_initialized", "cache_refresh_fail")
        p = _get_ide_path(ctx) if _should_touch_app(ctx, "ide", net_keys) else None
        mgr = _get_manager_path(ctx) if _should_touch_app(ctx, "manager", net_keys) else None
        if not p and not mgr:
            return "[16] Select custom path"
        if p and not _is_tls_applied(ctx):
            return "[11] TLS certificate fix"
        if mgr and not _is_mgr_patched(ctx):
            return "[3] Antigravity 2.0 patch"
        return "[14] Selective DNS & Routing"

    def _resolve_ls_init_btn(ctx):
        p = _get_ide_path(ctx)
        mgr = _get_manager_path(ctx)
        if p and not _is_tls_applied(ctx):
            return "[11] TLS certificate fix"
        if mgr and not _is_mgr_patched(ctx):
            return "[3] Antigravity 2.0 patch"
        if not p and mgr:
            return "[3] Antigravity 2.0 patch"
        if not p and not mgr:
            return "[16] Select custom path"
        return "[14] Selective DNS & Routing"

    def _resolve_loc400_btn(ctx):
        if ctx.get("_loc400_failed_button"):
            return ctx["_loc400_failed_button"]
        p = _get_ide_path(ctx)
        mgr = _get_manager_path(ctx)
        agy = _get_agy_path(ctx)
        vsc_agy = _get_vscode_agy_path(ctx)
        if not any([p, mgr, agy, vsc_agy]):
            return "[16] Select custom path"
        if p and not _is_ide_patched(ctx):
            return "[2] Antigravity IDE patch"
        if mgr and not _is_mgr_patched(ctx):
            return "[3] Antigravity 2.0 patch"
        if (agy and not _is_agy_patched(agy)) or (vsc_agy and not _is_agy_patched(vsc_agy)):
            return "[4] Antigravity CLI (agy) patch"
        return "[14] Selective DNS & Routing"

    def _is_dead_proxy_fixed(ctx):
        reg_enabled, reg_server = _read_windows_registry_proxy()
        if reg_enabled and reg_server:
            hp = _parse_proxy_host_port(reg_server)
            if hp and not _check_tcp_reachable(hp[0], hp[1], timeout=0.6):
                return False
        if _get_dead_env_proxies():
            return False
        return True

    def _fix_dead_proxy(ctx):
        still_blocked = False
        fixed_any = False

        reg_enabled, reg_server = _read_windows_registry_proxy()
        if reg_enabled and reg_server:
            hp = _parse_proxy_host_port(reg_server)
            if hp and not _check_tcp_reachable(hp[0], hp[1], timeout=0.6):
                if _disable_windows_registry_proxy():
                    ok(f"Отключён нерабочий системный прокси Windows ({hp[0]}:{hp[1]}).")
                    fixed_any = True
                else:
                    err(f"Не удалось отключить нерабочий системный прокси Windows ({hp[0]}:{hp[1]}).")
                    still_blocked = True

        cleared_user_env = _clear_dead_user_env_proxies()
        cleared_vars = {var.upper() for var, _, _ in cleared_user_env}
        for var, h, p in cleared_user_env:
            ok(f"Удалена нерабочая переменная прокси {var}={h}:{p} из профиля пользователя.")
            os.environ.pop(var, None)
            os.environ.pop(var.lower(), None)
            fixed_any = True

        dead_env = _get_dead_env_proxies()
        for var, h, p in dead_env:
            if var.upper() in cleared_vars:
                os.environ.pop(var, None)
                os.environ.pop(var.lower(), None)
                continue
            warn(f"В переменных окружения задан недоступный прокси {var}={h}:{p} — сбрасываем для текущей сессии.")
            os.environ.pop(var, None)
            os.environ.pop(var.lower(), None)
            fixed_any = True

        if not fixed_any and not still_blocked:
            hits_proxy = _extract_proxy_from_hits(ctx.get("_hits_by_key", {}).get("dead_proxy", []))
            if hits_proxy:
                h, p = hits_proxy
                if not _check_tcp_reachable(h, p, timeout=0.6):
                    hint(
                        f"Локальный прокси {h}:{p} не отвечает. Если используете VPN/прокси-клиент, "
                        "перезапустите его либо отключите прокси в настройках системы / IDE."
                    )

        _flush_dns_safe()

        if sys.platform == "win32" and not _can_resolve_google_dns():
            try:
                from patcher.nrpt_dns import enable_nrpt_rule, get_nrpt_status, get_preferred_dns_ip
                if not get_nrpt_status().get("active"):
                    ok_nrpt, msg = enable_nrpt_rule(get_preferred_dns_ip())
                    if ok_nrpt:
                        _flush_dns_safe()
                        ok(f"Windows NRPT DNS активирован ({msg})!")
                    else:
                        warn(f"NRPT DNS: {msg}")
            except Exception:
                pass

        return not still_blocked

    def _is_dns_lookup_fixed(ctx):
        dns_ok = _can_resolve_google_dns()
        if sys.platform == "win32":
            try:
                from patcher.nrpt_dns import get_nrpt_status
                from patcher.hosts_pin import is_hosts_pinned
                nrpt_act = bool(get_nrpt_status().get("active"))
                hosts_act, _ = is_hosts_pinned()
                return (nrpt_act or bool(hosts_act)) and dns_ok
            except Exception:
                return False
        return dns_ok

    def _fix_dns_lookup(ctx):
        heal = _heal_google_connectivity(ctx=ctx, quiet=False, require_bypass=True)
        if not _can_resolve_google_dns():
            warn("Домен daily-cloudcode-pa.googleapis.com всё ещё не резолвится локальным DNS.")
            return False
        if sys.platform == "win32":
            return bool(heal.get("nrpt_active") or heal.get("hosts_active"))
        return True

    def _is_network_and_auth_fixed(ctx):
        net_keys = ("auth_failure", "ls_not_initialized", "cache_refresh_fail")
        p = _get_ide_path(ctx) if _should_touch_app(ctx, "ide", net_keys) else None
        mgr = _get_manager_path(ctx) if _should_touch_app(ctx, "manager", net_keys) else None
        if not p and not mgr:
            return False
        if p and not _is_tls_applied(ctx):
            return False
        if mgr and not _is_mgr_patched(ctx):
            return False
        conn = _check_cloudcode_cached(ctx)
        if not conn.get("reachable"):
            return False
        return True

    def _fix_tls(ctx):
        from patcher.tls_fix import do_tls_fix_inject, is_tls_fix_applied
        p = _get_ide_path(ctx)
        if not p:
            err("Antigravity IDE (main.js) не найден — укажите путь в меню [16].")
            return False
        ok_tls = bool(do_tls_fix_inject(p))
        _flush_dns_safe()
        if ok_tls:
            ctx["_tls_fixed_apps"] = {"ide": True, "manager": False}
        return ok_tls and is_tls_fix_applied(p)

    def _fix_network_and_auth(ctx):
        from patcher.tls_fix import do_tls_fix_inject, is_tls_fix_applied
        ctx.pop("_network_failed_button", None)
        ctx.pop("_live_conn_cache", None)
        net_keys = ("auth_failure", "ls_not_initialized", "cache_refresh_fail")
        p = _get_ide_path(ctx) if _should_touch_app(ctx, "ide", net_keys) else None
        mgr = _get_manager_path(ctx) if _should_touch_app(ctx, "manager", net_keys) else None
        if not p and not mgr:
            ctx["_network_failed_button"] = "[16] Select custom path"
            err("Цели Antigravity (IDE / 2.0) не найдены — укажите путь в меню [16].")
            return False

        success = True

        if p:
            if not is_tls_fix_applied(p):
                if not do_tls_fix_inject(p):
                    success = False
            if not is_tls_fix_applied(p):
                success = False
                ctx.setdefault("_network_failed_button", "[11] TLS certificate fix")

        if mgr:
            if not _is_mgr_patched(ctx):
                from patcher.manager.patcher import do_patch_manager
                do_patch_manager(mgr)
                if not _is_mgr_patched(ctx):
                    success = False
                    ctx.setdefault("_network_failed_button", "[3] Antigravity 2.0 patch")

        heal = _heal_google_connectivity(ctx=ctx, quiet=False)
        if not heal.get("connectivity_ok"):
            success = False
            ctx.setdefault("_network_failed_button", "[14] Selective DNS & Routing")

        return success

    def _fix_ide_patch(ctx):
        from patcher.ide.patcher import do_patch
        p = _get_ide_path(ctx)
        if not p:
            err("Antigravity IDE (main.js) не найден — укажите путь в меню [16].")
            return False
        if not _is_ide_patched(ctx):
            do_patch(p, show_search_line=False)
        return _is_ide_patched(ctx)

    def _fix_reset_data(ctx):
        try:
            from patcher.data_reset import do_data_reset
            has_ide = bool(_get_ide_path(ctx))
            has_mgr = bool(_get_manager_path(ctx))
            if has_ide and not has_mgr:
                preferred = "Antigravity IDE"
            elif has_mgr and not has_ide:
                preferred = "Antigravity 2.0"
            else:
                preferred = "all"
            ok_reset = do_data_reset(preferred, auto_confirm=True) is True
            if ok_reset:
                ctx["_auto_cleaned_apps"] = {
                    "ide": bool(has_ide or preferred == "all"),
                    "manager": bool(has_mgr or preferred == "all"),
                }
            return ok_reset
        except Exception as e:
            err(f"Не удалось выполнить сброс данных: {e}")
            return False

    def _fix_sqlite_repair(ctx):
        try:
            from patcher.conversations.rebuilder import rebuild_conversations_index
            stats = rebuild_conversations_index(auto_workspaces=True)
            return bool(stats and isinstance(stats, dict))
        except Exception as e:
            err(f"Не удалось восстановить базу данных SQLite: {e}")
            return False

    def _extract_refused_sni_ips(hits: list) -> set[str]:
        ips: set[str] = set()
        for h in hits or ():
            for m in re.finditer(r"connect\s+ECONNREFUSED\s+(?!127\.0\.0\.1)(\d{1,3}(?:\.\d{1,3}){3}):443", str(h)):
                ips.add(m.group(1))
        return ips

    def _is_sni_host_refused_fixed(ctx):
        bad_ips = _extract_refused_sni_ips(ctx.get("_hits_by_key", {}).get("sni_host_refused", []))
        if sys.platform == "win32" and bad_ips:
            try:
                from patcher.hosts_pin import is_hosts_pinned
                hosts_act, pinned_ip = is_hosts_pinned()
                if hosts_act and pinned_ip in bad_ips:
                    return False
            except Exception:
                pass
        return bool(_check_cloudcode_cached(ctx).get("reachable"))

    def _fix_sni_host_refused(ctx):
        ctx.pop("_live_conn_cache", None)
        bad_ips = _extract_refused_sni_ips(ctx.get("_hits_by_key", {}).get("sni_host_refused", []))
        if sys.platform == "win32":
            try:
                from patcher.hosts_pin import enable_hosts_pinning, is_hosts_pinned
                hosts_act, pinned_ip = is_hosts_pinned()
                if hosts_act and (not bad_ips or pinned_ip in bad_ips):
                    excl = bad_ips | ({pinned_ip} if pinned_ip and pinned_ip != "active" else set())
                    ok_hp, msg_hp = enable_hosts_pinning(exclude_ips=excl)
                    _flush_dns_safe()
                    if ok_hp:
                        ok(f"Выполнена ротация SNI-маршрута hosts: {msg_hp}")
                    else:
                        warn(f"Не удалось переключить SNI-IP в hosts: {msg_hp}")
            except Exception as ex_sni:
                warn(f"Ошибка ротации SNI-маршрута: {ex_sni}")
        heal = _heal_google_connectivity(ctx=ctx, quiet=False)
        return bool(heal.get("connectivity_ok"))

    def _get_ide_user_data_dir() -> Optional[Path]:
        override = os.environ.get("AG_IDE_USER_DATA_DIR", "").strip()
        if override:
            p = Path(override)
            return p if p.is_dir() else None
        if os.name == "nt":
            appdata = os.environ.get("APPDATA", "")
            cand = Path(appdata) / "Antigravity IDE" if appdata else None
        elif sys.platform == "darwin":
            cand = Path.home() / "Library" / "Application Support" / "Antigravity IDE"
        else:
            cand = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Antigravity IDE"
        return cand if (cand and cand.is_dir()) else None

    def _inspect_ide_session_issues(user_dir: Path) -> dict:
        import json as _json
        import sqlite3 as _sqlite3

        issues: dict[str, Any] = {"vscd_targets": [], "fix_storage": False, "backup_ids": set()}
        storage_file = user_dir / "User" / "globalStorage" / "storage.json"
        if storage_file.is_file():
            try:
                sdata = _json.loads(storage_file.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(sdata, dict):
                    win_state = sdata.get("windowsState") or {}
                    last_win = win_state.get("lastActiveWindow") if isinstance(win_state, dict) else None
                    if isinstance(last_win, dict) and not last_win.get("folder") and not last_win.get("workspace"):
                        ui = last_win.get("uiState") if isinstance(last_win.get("uiState"), dict) else {}
                        bp = str(last_win.get("backupPath") or "")
                        if bp:
                            issues["backup_ids"].add(Path(bp).name)
                        if ui.get("width", 1280) < 700 and ui.get("height", 800) < 450:
                            issues["fix_storage"] = True
            except Exception:
                pass

        ws_root = user_dir / "User" / "workspaceStorage"
        if ws_root.is_dir():
            for ws_dir in ws_root.iterdir():
                if not ws_dir.is_dir():
                    continue
                if (ws_dir / "workspace.json").is_file():
                    continue
                vscdb = ws_dir / "state.vscdb"
                if not vscdb.is_file():
                    continue
                if issues["backup_ids"] and ws_dir.name not in issues["backup_ids"] and not ws_dir.name.isdigit():
                    continue
                try:
                    con = _sqlite3.connect(str(vscdb), timeout=2.0)
                    rows = dict(
                        con.execute(
                            "SELECT key, value FROM ItemTable WHERE key IN "
                            "('memento/workspaceTrust', 'antigravity.agentViewContainerId.state')"
                        ).fetchall()
                    )
                    con.close()
                except Exception:
                    continue
                needs_trust = False
                needs_panel = False
                raw_trust = rows.get("memento/workspaceTrust")
                if raw_trust:
                    try:
                        td = _json.loads(raw_trust)
                        if isinstance(td, dict) and td.get("isEmptyWorkspaceTrusted") is False:
                            needs_trust = True
                    except Exception:
                        pass
                raw_panel = rows.get("antigravity.agentViewContainerId.state")
                if raw_panel:
                    try:
                        pd = _json.loads(raw_panel)
                        if isinstance(pd, dict) and isinstance(pd.get("antigravity.panel"), dict) and pd["antigravity.panel"].get("isHidden") is True:
                            # Only treat hidden panel as broken session when workspace is untrusted OR window is stuck small (<700x450)
                            if needs_trust or issues["fix_storage"]:
                                needs_panel = True
                    except Exception:
                        pass
                if needs_trust or needs_panel:
                    issues["vscd_targets"].append((vscdb, needs_trust, needs_panel))
        return issues

    def _is_ide_session_blocked_fixed(ctx) -> bool:
        user_dir = _get_ide_user_data_dir()
        if not user_dir:
            return True
        issues = _inspect_ide_session_issues(user_dir)
        return not issues["vscd_targets"] and not issues["fix_storage"]

    def _fix_ide_session_blocked(ctx) -> bool:
        import json as _json
        import sqlite3 as _sqlite3

        user_dir = _get_ide_user_data_dir()
        changed = False
        if user_dir:
            issues = _inspect_ide_session_issues(user_dir)
            for vscdb, needs_trust, needs_panel in issues["vscd_targets"]:
                try:
                    con = _sqlite3.connect(str(vscdb), timeout=3.0)
                    if needs_trust:
                        cur_val = con.execute("SELECT value FROM ItemTable WHERE key='memento/workspaceTrust'").fetchone()
                        td = _json.loads(cur_val[0]) if cur_val and cur_val[0] else {}
                        if isinstance(td, dict):
                            td["isEmptyWorkspaceTrusted"] = True
                            con.execute(
                                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                                ("memento/workspaceTrust", _json.dumps(td, separators=(",", ":"))),
                            )
                            changed = True
                    if needs_panel:
                        cur_p = con.execute("SELECT value FROM ItemTable WHERE key='antigravity.agentViewContainerId.state'").fetchone()
                        pd = _json.loads(cur_p[0]) if cur_p and cur_p[0] else {}
                        if isinstance(pd, dict) and isinstance(pd.get("antigravity.panel"), dict):
                            pd["antigravity.panel"]["isHidden"] = False
                            con.execute(
                                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                                ("antigravity.agentViewContainerId.state", _json.dumps(pd, separators=(",", ":"))),
                            )
                            changed = True
                    con.commit()
                    con.close()
                except Exception as ex_db:
                    warn(f"Не удалось обновить workspaceTrust/panel ({vscdb.name}): {ex_db}")

            storage_file = user_dir / "User" / "globalStorage" / "storage.json"
            if issues["fix_storage"] and storage_file.is_file():
                try:
                    sdata = _json.loads(storage_file.read_text(encoding="utf-8", errors="ignore"))
                    if isinstance(sdata, dict):
                        win_state = sdata.get("windowsState")
                        if isinstance(win_state, dict) and isinstance(win_state.get("lastActiveWindow"), dict):
                            lw = win_state["lastActiveWindow"]
                            if not lw.get("folder") and not lw.get("workspace"):
                                ui = lw.get("uiState")
                                if isinstance(ui, dict) and ui.get("width", 1280) < 700 and ui.get("height", 800) < 450:
                                    ui["width"] = 1280
                                    ui["height"] = 800
                                    changed = True
                                if lw.pop("backupPath", None):
                                    changed = True
                        bw = sdata.get("backupWorkspaces")
                        if isinstance(bw, dict) and isinstance(bw.get("emptyWindows"), list) and bw["emptyWindows"]:
                            bw["emptyWindows"] = []
                            changed = True
                        if changed:
                            storage_file.write_text(_json.dumps(sdata, indent=4, ensure_ascii=False), encoding="utf-8")
                except Exception as ex_st:
                    warn(f"Не удалось очистить состояние пустого окна storage.json: {ex_st}")

        try:
            from patcher.extension_installer import deploy_assistant_extension
            if changed:
                deploy_assistant_extension()
        except Exception:
            pass

        if changed:
            ok("Восстановлен доверенный режим пустой сессии Antigravity IDE и панель агента.")
        return bool(changed)

    def _is_user_location_400_fixed(ctx):
        loc_keys = ("user_location_400", "is_google_internal")
        p = _get_ide_path(ctx) if _should_touch_app(ctx, "ide", loc_keys) else None
        mgr = _get_manager_path(ctx) if _should_touch_app(ctx, "manager", loc_keys) else None
        agy = _get_agy_path(ctx) if _should_touch_app(ctx, "cli", loc_keys) else None
        vsc_agy = _get_vscode_agy_path(ctx) if _should_touch_app(ctx, "cli", loc_keys) else None
        has_any = any([p, mgr, agy, vsc_agy])
        if not has_any:
            return False

        if p and not _is_ide_patched(ctx):
            return False
        if mgr and not _is_mgr_patched(ctx):
            return False
        if agy and not _is_agy_patched(agy):
            return False
        if vsc_agy and not _is_agy_patched(vsc_agy):
            return False

        if sys.platform == "win32":
            try:
                from patcher.nrpt_dns import get_nrpt_status
                from patcher.hosts_pin import is_hosts_pinned
                nrpt_st = get_nrpt_status()
                hosts_act, _ = is_hosts_pinned()
                if not (bool(nrpt_st.get("active")) or hosts_act):
                    return False
                conn = _check_cloudcode_cached(ctx)
                return not bool(conn.get("regional_400"))
            except Exception:
                return False
        return True

    def _fix_user_location_400(ctx):
        ctx.pop("_loc400_failed_button", None)
        success = True
        patched_any = 0
        loc_keys = ("user_location_400", "is_google_internal")

        p = _get_ide_path(ctx) if _should_touch_app(ctx, "ide", loc_keys) else None
        if p:
            patched_any += 1
            if not _is_ide_patched(ctx):
                from patcher.ide.patcher import do_patch
                do_patch(p, show_search_line=False)
                if not _is_ide_patched(ctx):
                    success = False
                    ctx.setdefault("_loc400_failed_button", "[2] Antigravity IDE patch")
        elif _should_touch_app(ctx, "ide", loc_keys):
            hint("Antigravity IDE (main.js) не обнаружен — пропускаем шаг IDE.")

        mgr = _get_manager_path(ctx) if _should_touch_app(ctx, "manager", loc_keys) else None
        if mgr:
            patched_any += 1
            if not _is_mgr_patched(ctx):
                from patcher.manager.patcher import do_patch_manager
                do_patch_manager(mgr)
                if not _is_mgr_patched(ctx):
                    success = False
                    ctx.setdefault("_loc400_failed_button", "[3] Antigravity 2.0 patch")

        agy = _get_agy_path(ctx) if _should_touch_app(ctx, "cli", loc_keys) else None
        if agy:
            patched_any += 1
            if not _is_agy_patched(agy):
                from patcher.agy.patcher import do_patch_agy
                do_patch_agy(agy)
                if not _is_agy_patched(agy):
                    success = False
                    ctx.setdefault("_loc400_failed_button", "[4] Antigravity CLI (agy) patch")

        vsc_agy = _get_vscode_agy_path(ctx) if _should_touch_app(ctx, "cli", loc_keys) else None
        if vsc_agy and vsc_agy != agy:
            patched_any += 1
            if not _is_agy_patched(vsc_agy):
                from patcher.agy.patcher import do_patch_agy
                do_patch_agy(vsc_agy)
                if not _is_agy_patched(vsc_agy):
                    success = False
                    ctx.setdefault("_loc400_failed_button", "[4] Antigravity CLI (agy) patch")

        if patched_any == 0:
            err("Цели Antigravity (IDE / 2.0 / CLI) не найдены — укажите путь в меню [16].")
            ctx.setdefault("_loc400_failed_button", "[16] Select custom path")
            success = False

        if sys.platform == "win32":
            heal = _heal_google_connectivity(ctx=ctx, quiet=False, require_bypass=True)
            if not (heal.get("nrpt_active") or heal.get("hosts_active")):
                ctx.setdefault("_loc400_failed_button", "[14] Selective DNS & Routing")
                success = False

        return success

    return [
        KnownError(
            key="tls_expired",
            title="TLS certificate expired (localhost handshake error)",
            patterns=[
                r"TLS handshake error",
                r"remote error: tls:",
                r"certificate has expired",
                r"secure TLS connection was established",
                r"certificate.*expired",
                r"unknown certificate",
                r"bad certificate",
                r"CERT_HAS_EXPIRED",
                r"UNABLE_TO_VERIFY_LEAF_SIGNATURE",
                r"UNABLE_TO_VERIFY_FIRST_CERTIFICATE",
                r"unable to get local issuer",
                r"SELF_SIGNED_CERT",
                r"DEPTH_ZERO_SELF_SIGNED_CERT",
                r"x509:",
            ],
            description=(
                "Истёк или отклонён внутренний TLS-сертификат localhost для связи IDE и языкового сервера. "
                "IDE сразу сбрасывает запросы к агенту."
            ),
            fix_label="Внедрить NODE_TLS_REJECT_UNAUTHORIZED=0 + Proxy fix в main.js и сбросить DNS-кэш",
            fix_fn=_fix_tls,
            is_already_fixed_fn=_is_tls_applied,
            severity="ERROR",
            menu_button="[11] TLS certificate fix",
            resolve_menu_button_fn=_resolve_tls_btn,
        ),
        KnownError(
            key="dead_proxy",
            title="Недоступен локальный / системный прокси (proxyconnect / connection refused)",
            patterns=[
                r"proxyconnect\s+tcp:",
                r"ERR_PROXY_CONNECTION_FAILED",
                r"ECONNREFUSED\s+127\.0\.0\.1:\d+",
                r"dial tcp 127\.0\.0\.1:\d+:.*actively refused",
            ],
            description=(
                "IDE пытается подключиться к Google API через локальный/системный прокси-порт "
                "(обычно оставшийся от выключенного VPN-клиента), но порт активно отклоняет соединение."
            ),
            fix_label="Отключить нерабочий системный прокси Windows и сбросить DNS-кэш",
            fix_fn=_fix_dead_proxy,
            is_already_fixed_fn=_is_dead_proxy_fixed,
            severity="ERROR",
            menu_button="[14] Selective DNS & Routing",
        ),
        KnownError(
            key="dns_lookup_fail",
            title="Ошибка DNS-резолвинга (lookup ...: no such host / ENOTFOUND)",
            patterns=[
                r"dial tcp: lookup .+no such host",
                r"lookup [^:\s]+googleapis\.com[^:]*: no such host",
                r"getaddrinfo\s+(?:ENOTFOUND|EAI_AGAIN)",
                r"ERR_NAME_NOT_RESOLVED",
            ],
            description=(
                "Системный DNS не может разрешить домен Google API (no such host / ENOTFOUND). "
                "Обычно вызвано устаревшим кэшем DNS, сбоем DNS после VPN или блокировкой провайдера."
            ),
            fix_label="Сбросить DNS-кэш Windows и активировать NRPT DNS-маршрут",
            fix_fn=_fix_dns_lookup,
            is_already_fixed_fn=_is_dns_lookup_fixed,
            severity="ERROR",
            menu_button="[14] Selective DNS & Routing",
        ),
        KnownError(
            key="sni_host_refused",
            title="Сброс соединения прибитого SNI-сервера (ECONNREFUSED :443)",
            patterns=[
                r"connect\s+ECONNREFUSED\s+(?!127\.0\.0\.1)(?:\d{1,3}\.){3}\d{1,3}:443",
            ],
            description=(
                "Закреплённый в hosts SNI-прокси отклонил подключение к порту 443. "
                "Требуется автоматическая ротация на другой живой IP-узел из пула."
            ),
            fix_label="Переключить hosts на другой рабочий SNI-узел и сбросить DNS-кэш",
            fix_fn=_fix_sni_host_refused,
            is_already_fixed_fn=_is_sni_host_refused_fixed,
            severity="ERROR",
            menu_button="[14] Selective DNS & Routing",
        ),
        KnownError(
            key="ide_session_blocked",
            title="Блокировка расширений IDE в Restricted Mode / битой пустой сессии",
            patterns=[
                r"command\s+'antigravity\.handleAuthRefresh'\s+not\s+found",
                r"\[createInstance\]\s+\w+\s+depends\s+on\s+UNKNOWN\s+service\s+agentSessions",
            ],
            description=(
                "Antigravity IDE запустилась в пустом недоверенном окне (Restricted Mode) или со сломанным "
                "состоянием редактора: встроенное расширение google.google-antigravity и ag-toolkit не активируются."
            ),
            fix_label="Включить доверие пустой сессии IDE, открыть панель агента и переустановить ag-toolkit",
            fix_fn=_fix_ide_session_blocked,
            is_already_fixed_fn=_is_ide_session_blocked_fixed,
            severity="ERROR",
            menu_button="[18] Install Antigravity Assistant",
        ),
        KnownError(
            key="resource_exhausted_429",
            title="HTTP 429 - RESOURCE_EXHAUSTED (исчерпание квоты / токенов)",
            patterns=[
                r"RESOURCE_EXHAUSTED",
                r"code 429",
                r"HTTP 429",
                r"status 429",
                r"Resource has been exhausted",
                r"check quota",
            ],
            description=(
                "Лимит квоты Google API или зависшие токены в сессии IDE. "
                "Лечится сбросом временных данных IDE с сохранением всех чатов."
            ),
            fix_label="Сброс папки данных IDE (с сохранением истории чатов)",
            fix_fn=_fix_reset_data,
            severity="ERROR",
            menu_button="[13] Reset data / Fix 429",
        ),
        KnownError(
            key="user_location_400",
            title="HTTP 400 / Ineligible - User location not supported (региональная блокировка)",
            patterns=[
                r"User location is not supported",
                r"FAILED_PRECONDITION",
                r"code 400",
                r"HTTP 400",
                r"status 400",
                r"\bineligible\b",
                r"SET_INELIGIBLE",
                r"available in your location",
                r"ineligibleTiers",
                r"loginError.*(?:400|location|inelig|precondition)",
            ],
            description=(
                "Google Cloud API отклоняет запросы из неподдерживаемого региона (HTTP 400). "
                "Требуются патчи приложений (IDE / 2.0 / CLI) и выборочный Windows NRPT DNS для cloudcode-pa."
            ),
            fix_label="Применить патчи (IDE / 2.0 / CLI) + включить Windows NRPT DNS (без VPN)",
            fix_fn=_fix_user_location_400,
            is_already_fixed_fn=_is_user_location_400_fixed,
            severity="ERROR",
            menu_button="[14] Selective DNS & Routing",
            resolve_menu_button_fn=_resolve_loc400_btn,
        ),
        KnownError(
            key="subscription_required_403",
            title="HTTP 403 - SUBSCRIPTION_REQUIRED (нет активной лицензии)",
            patterns=[
                r"SUBSCRIPTION_REQUIRED",
                r"You do not have a valid license",
                r"code 403",
                r"HTTP 403",
                r"status 403",
                r"PERMISSION_DENIED",
            ],
            description=(
                "Google-аккаунт не имеет активной лицензии Antigravity или "
                "зарегистрирован в регионе без доступа к сервису. "
                "Локальным патчем не лечится — требуется смена региона аккаунта Google."
            ),
            fix_label="Требуется аккаунт с лицензией / смена региона Google-аккаунта",
            fix_fn=None,
            severity="ERROR",
            menu_button="[17] About program (проверьте регион Google аккаунта)",
        ),
        KnownError(
            key="internal_error_500",
            title="HTTP 500 - Internal Server Error (ошибка серверов Google)",
            patterns=[
                r"Internal error encountered",
                r"code 500",
                r"HTTP 500",
                r"status 500",
            ],
            description=(
                "Временный сбой на стороне серверов Google. "
                "Локальный патч не требуется. Попробуйте перезапустить IDE или сменить аккаунт."
            ),
            fix_label="Временная ошибка Google (локальный авто-фикс не требуется)",
            fix_fn=None,
            severity="ERROR",
            menu_button="Перезапустите IDE позже (временный сбой серверов Google)",
        ),
        KnownError(
            key="is_google_internal",
            title="Конфигурация региона IDE отсутствует или была сброшена",
            patterns=[
                r"isGoogleInternal",
                r"eligibility check",
                r"Account ineligible",
                r"Eligibility check failed",
            ],
            description="Патч в main.js не обнаружен (возможно, после автоматического обновления IDE).",
            fix_label="Применить конфигурацию к main.js",
            fix_fn=_fix_ide_patch,
            is_already_fixed_fn=_is_ide_patched,
            severity="ERROR",
            menu_button="[2] Antigravity IDE patch",
        ),
        KnownError(
            key="auth_failure",
            title="Сбой авторизации / соединения с Google Auth",
            patterns=[
                r"\[Auth\].+Error",
                r"Auth state.+fail",
                r"Auth state changed to: loginError",
                r"^(?!.*(?:proxyconnect|no such host)).*oauth2.+fail",
                r"Failed to refresh.+userinfo",
                r"Failed to refresh user status",
                r"Error refreshing user status",
            ],
            description=(
                "Ошибка авторизации или сброс соединения с сервисами Google. "
                "Обычно вызвана просроченным сертификатом localhost или блокировкой соединения."
            ),
            fix_label="Проверить TLS-фикс main.js, патч 2.0 и сбросить DNS-кэш",
            fix_fn=_fix_network_and_auth,
            is_already_fixed_fn=_is_network_and_auth_fixed,
            severity="WARN",
            menu_button="[14] Selective DNS & Routing",
            resolve_menu_button_fn=_resolve_network_auth_btn,
        ),
        KnownError(
            key="ls_not_initialized",
            title="Language Server не инициализирован (сбой связи)",
            patterns=[
                r"Language server client has not been initialized",
                r"LS check error",
                r"\[LAUNCH ERROR\].*exited unexpectedly with code [1-9]",
            ],
            description=(
                "Языковой сервер не смог подключиться к клиенту. "
                "Обычно связано с истекшим TLS-сертификатом localhost или патчем 2.0."
            ),
            fix_label="Применить TLS certificate fix для восстановления связи",
            fix_fn=_fix_tls,
            is_already_fixed_fn=_is_tls_applied,
            severity="WARN",
            menu_button="[11] TLS certificate fix",
            resolve_menu_button_fn=_resolve_ls_init_btn,
        ),
        KnownError(
            key="cache_refresh_fail",
            title="Сбой обновления кэша / соединения с Google API (TLS timeout / EOF / ECONNRESET)",
            patterns=[
                r"^(?!.*(?:proxyconnect|no such host)).*(?:Failed to refresh cache|Singleflight refresh failed).+(?:TLS handshake timeout|EOF|ECONNRESET|ETIMEDOUT|wsarecv|dial tcp|context deadline exceeded)",
                r"^(?!.*(?:proxyconnect|no such host)).*(?:googleapis\.com|gstatic\.com|loadCodeAssist|fetchUserInfo|cascadeNuxes|listExperiments).*TLS handshake timeout",
                r"^(?!.*(?:proxyconnect|no such host)).*oauth2.+(?:EOF|ECONNRESET|ETIMEDOUT|TLS handshake timeout)",
                r"^(?!.*proxyconnect).*reason:\s*(?:read ECONNRESET|connect ETIMEDOUT|connect ECONNREFUSED)",
            ],
            description="Соединение с Google Cloud Code / OAuth сбрасывается (TLS handshake timeout / EOF / ECONNRESET).",
            fix_label="Проверить TLS/Proxy fix, очистить прокси и активировать NRPT / Hosts SNI-маршрут",
            fix_fn=_fix_network_and_auth,
            is_already_fixed_fn=_is_network_and_auth_fixed,
            severity="WARN",
            menu_button="[14] Selective DNS & Routing",
            resolve_menu_button_fn=_resolve_network_auth_btn,
        ),
        KnownError(
            key="pattern_not_found",
            title="Целевой файл main.js не распознан",
            patterns=[r"pattern not found", r"isGoogleInternal.*not found"],
            description=(
                "Файл main.js не соответствует Antigravity IDE или указан неверный каталог. "
                "Укажите правильный путь вручную в меню [16]."
            ),
            fix_label="Ручной выбор правильного пути к Antigravity IDE",
            fix_fn=None,
            severity="ERROR",
            menu_button="[16] Select custom path",
        ),
        KnownError(
            key="sqlite_corrupt",
            title="Повреждение базы данных SQLite (state.vscdb / история чатов)",
            patterns=[
                r"SQLITE_CORRUPT",
                r"database disk image is malformed",
                r"file is not a database",
                r"Corrupt sqlite",
                r"Corrupt database",
            ],
            description=(
                "Повреждена база данных сессий или история диалогов VS Code/IDE (state.vscdb). "
                "Чаты могут не отображаться или IDE выдаёт ошибку при сохранении состояния."
            ),
            fix_label="Синхронизировать и восстановить базу диалогов (Rebuild conversation index)",
            fix_fn=_fix_sqlite_repair,
            severity="ERROR",
            menu_button="[20] Rebuild & repair chat history",
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# ЧТЕНИЕ ЛОГОВ
# ═══════════════════════════════════════════════════════════════════════════════

def _get_log_base_dirs() -> List[Path]:
    """Возвращает список папок с логами IDE и CLI для текущей ОС."""
    dirs = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            dirs += [
                Path(appdata) / "Antigravity IDE" / "logs",
                Path(appdata) / "Antigravity" / "logs",
            ]
    elif sys.platform == "darwin":
        h = Path.home()
        dirs += [
            h / "Library" / "Application Support" / "Antigravity IDE" / "logs",
            h / "Library" / "Application Support" / "Antigravity" / "logs",
        ]
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        dirs += [
            cfg / "Antigravity IDE" / "logs",
            cfg / "Antigravity" / "logs",
        ]

    # CLI логи (~/.gemini/antigravity/log и ~/.gemini/antigravity-cli/log)
    home = Path.home()
    cli_dirs = [
        home / ".gemini" / "antigravity" / "log",
        home / ".gemini" / "antigravity",
        home / ".gemini" / "antigravity-cli" / "log",
        home / ".gemini" / "antigravity-cli",
    ]
    for d in cli_dirs:
        if d.is_dir() and d not in dirs:
            dirs.append(d)

    return [d for d in dirs if d.is_dir()]


# Файлы с наибольшей диагностической ценностью (в порядке приоритета)
_PRIORITY_LOGS = [
    "language_server.log",  # Antigravity 2.0 language server
    "ls-main.log",          # language server - главный источник TLS / 429 / 403
    "auth.log",             # авторизация
    "cloudcode.log",        # Cloud Code API
    "main.log",             # основной процесс
    "exthost.log",          # extension host (IDE)
    "sharedprocess.log",
]


def _find_latest_session(base: Path) -> Optional[Path]:
    """Находит папку с последней сессией (самая свежая по имени/дате)."""
    sessions = [d for d in base.iterdir() if d.is_dir()] if base.is_dir() else []
    if not sessions:
        return None
    return max(sessions, key=lambda d: d.stat().st_mtime)


_GLOG_INFO_RE = re.compile(r"^ERROR:\s+logging before google\.Init:\s+I\d{4}\b")
_UPDATER_NOISE_RE = re.compile(r"ENOENT.*app-update\.yml", re.IGNORECASE)
_CRITICAL_SIGNAL_RE = re.compile(
    r"TLS|certificate|handshake|x509|RESOURCE_EXHAUSTED|SUBSCRIPTION_REQUIRED|"
    r"PERMISSION_DENIED|FAILED_PRECONDITION|(?:code|HTTP|status)\s+[45]\d\d|"
    r"isGoogleInternal|eligibility|User location is not supported|"
    r"proxyconnect|no such host|ERR_PROXY_CONNECTION_FAILED|ERR_NAME_NOT_RESOLVED|"
    r"ECONNRESET|ETIMEDOUT|ENOTFOUND|ECONNREFUSED|wsarecv|dial tcp|EOF|loadCodeAssist|"
    r"Singleflight refresh failed|Failed to refresh|Auth.*Error|not been initialized|"
    r"handleAuthRefresh|UNKNOWN service agentSessions|agentSidePanel|"
    r"\bineligible\b|SET_INELIGIBLE|available in your location|ineligibleTiers|"
    r"CERT_HAS_EXPIRED|DEPTH_ZERO_SELF_SIGNED_CERT|\[LAUNCH ERROR\]",
    re.IGNORECASE,
)
_COLLECT_KEYWORD_RE = re.compile(
    r"\[error\]|\[warn\]|\[LAUNCH ERROR\]|:\s+[EFW]\d{4}\s|TLS|certificate|handshake|x509|"
    r"RESOURCE_EXHAUSTED|SUBSCRIPTION_REQUIRED|PERMISSION_DENIED|FAILED_PRECONDITION|"
    r"(?:code|HTTP|status)\s+[45]\d\d|isGoogleInternal|eligibility|"
    r"proxyconnect|no such host|ERR_PROXY_CONNECTION_FAILED|ERR_NAME_NOT_RESOLVED|"
    r"ECONNRESET|ETIMEDOUT|ENOTFOUND|ECONNREFUSED|wsarecv|dial tcp|EOF|loadCodeAssist|"
    r"Singleflight refresh failed|Failed to refresh|Auth.*Error|not been initialized|"
    r"handleAuthRefresh|UNKNOWN service agentSessions|agentSidePanel|"
    r"loginError|\bineligible\b|SET_INELIGIBLE|available in your location|unable to get local issuer",
    re.IGNORECASE,
)


def _tag_app_for_log_path(log_path: Path) -> str:
    """Classify a log directory or file into 'ide', 'manager', 'cli', or 'shared'."""
    low = str(log_path).replace("\\", "/").lower()
    if "/antigravity ide/" in low or low.endswith("/antigravity ide") or "/antigravity-ide/" in low:
        return "ide"
    if "/antigravity-cli/" in low or "/.gemini/" in low:
        return "cli"
    if "/antigravity/" in low or low.endswith("/antigravity"):
        return "manager"
    return "shared"


def _collect_log_lines_tagged(
    max_lines_per_file: int = 2000,
    max_age_days: float = 7.0,
    target_app: str = "all",
) -> List[Tuple[str, str, str, str]]:
    """
    Читает логи IDE / 2.0 / CLI и возвращает список (session_dir, filename, line, app_tag).
    """
    import time as _time
    result: List[Tuple[str, str, str, str]] = []
    keyword_re = _COLLECT_KEYWORD_RE

    limit = max(int(max_lines_per_file or 2000), 200)
    cutoff = _time.time() - (max(float(max_age_days), 0.1) * 86400.0)
    t_norm = (target_app or "all").lower()

    def _is_recent_file(p: Path) -> bool:
        try:
            return p.stat().st_mtime >= cutoff
        except OSError:
            return False

    for base in _get_log_base_dirs():
        app_tag = _tag_app_for_log_path(base)
        if t_norm in ("ide", "manager", "cli") and app_tag not in (t_norm, "shared"):
            continue
        sessions = sorted(
            [d for d in base.iterdir() if d.is_dir() and _is_recent_file(d)],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        ) if base.is_dir() else []
        target_sessions: List[Path] = []
        if base.is_dir() and any(f.is_file() and f.suffix == ".log" and _is_recent_file(f) for f in base.iterdir()):
            target_sessions.append(base)
        for s in sessions[:2]:
            if s not in target_sessions:
                target_sessions.append(s)

        for session in target_sessions:
            log_files_ordered: List[Path] = []
            for name in _PRIORITY_LOGS:
                p = session / name
                if p.is_file() and _is_recent_file(p) and p not in log_files_ordered:
                    log_files_ordered.append(p)

            extra_logs = [
                f for f in session.iterdir()
                if f.is_file() and f.suffix == ".log" and f not in log_files_ordered and _is_recent_file(f)
            ]
            if session == base and extra_logs:
                extra_logs = sorted(extra_logs, key=lambda f: f.stat().st_mtime, reverse=True)[:4]
            else:
                extra_logs.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            log_files_ordered.extend(extra_logs[:5])

            # Вложенные логи extension host и renderer (window1/renderer.log, window1/exthost/exthost.log, output_logging_*/2-Antigravity*.log)
            if session != base:
                for pattern in (
                    "window*/renderer.log",
                    "window*/exthost/exthost.log",
                    "window*/exthost/output_logging_*/*Antigravity*.log",
                ):
                    for f in sorted(session.glob(pattern)):
                        if f.is_file() and _is_recent_file(f) and f not in log_files_ordered:
                            log_files_ordered.append(f)

            for log_file in log_files_ordered:
                try:
                    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    if len(lines) <= limit:
                        candidates = lines
                    else:
                        head = min(500, limit // 3)
                        tail = max(200, limit - head)
                        candidates = lines[:head] + lines[-tail:]

                    for ln in candidates:
                        if '"postmessage-rpc"' in ln or '"type":"postmessage-rpc"' in ln:
                            continue
                        if _UPDATER_NOISE_RE.search(ln):
                            continue
                        if _GLOG_INFO_RE.match(ln) and not _CRITICAL_SIGNAL_RE.search(ln):
                            continue
                        if keyword_re.search(ln):
                            result.append((session.name, log_file.name, ln.strip(), app_tag))
                except Exception:
                    pass

    return result


_LAST_LOG_APP_TAGS: Dict[Tuple[str, str, str], set] = {}


def collect_log_lines(max_lines_per_file: int = 2000, max_age_days: float = 7.0) -> List[Tuple[str, str, str]]:
    """
    Читает логи IDE / 2.0 и возвращает список (session_dir, filename, line).
    Собирает строки с ошибками и ключевыми словами из свежих сессий.
    """
    _LAST_LOG_APP_TAGS.clear()
    tagged = _collect_log_lines_tagged(
        max_lines_per_file=max_lines_per_file,
        max_age_days=max_age_days,
    )
    out: List[Tuple[str, str, str]] = []
    for sess, fname, ln, app_tag in tagged:
        key = (sess, fname, ln)
        _LAST_LOG_APP_TAGS.setdefault(key, set()).add(app_tag)
        out.append(key)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# АНАЛИЗ ЛОГОВ
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_logs(log_lines: List[Tuple[str, str, str]],
                 error_db: List[KnownError]) -> List[KnownError]:
    """
    Сопоставляет строки логов с базой известных ошибок.
    Возвращает список сработавших KnownError (с заполненным .hits).
    """
    for ke in error_db:
        ke.hits = []

    for (session, fname, line) in log_lines:
        for ke in error_db:
            if ke.matches(line):
                short = f"[{fname}] {line[-180:]}"
                if short not in ke.hits:
                    ke.hits.append(short)

    return [ke for ke in error_db if ke.hits]


# ═══════════════════════════════════════════════════════════════════════════════
# ОТОБРАЖЕНИЕ
# ═══════════════════════════════════════════════════════════════════════════════

def _sev_color(sev: str) -> str:
    return {"ERROR": COLOR_RED, "WARN": COLOR_YELLOW, "INFO": COLOR_CYAN}.get(sev, COLOR_WHITE)


def print_found_errors(found: List[KnownError], ctx: Optional[dict] = None):
    """Выводит список найденных ошибок с указанием кнопки в главном меню для ручного фикса."""
    if not found:
        ok("В логах Antigravity IDE известных ошибок не обнаружено. Всё чисто!")
        return

    print_menu_section(f"ОБНАРУЖЕНО ПРОБЛЕМ В ЛОГАХ: {len(found)}")
    print()

    for i, ke in enumerate(found, 1):
        sev_col = _sev_color(ke.severity)
        print(f"  {color(f'[{i}]', sev_col, COLOR_BOLD)} {color(ke.title, COLOR_WHITE, COLOR_BOLD)}")
        print(f"      {color('Описание:', COLOR_DIM)} {ke.description}")

        for hit in ke.hits[:2]:
            display = hit[-160:]
            print(f"      {color('Лог:', COLOR_DIM)} {color(display, COLOR_DIM)}")

        is_fixed = ke.is_already_fixed_fn(ctx) if (ke.is_already_fixed_fn and ctx) else False
        if is_fixed:
            print(f"      {color('Статус в системе:', COLOR_GREEN, COLOR_BOLD)} {color('[✓] Фикс УЖЕ внедрён в систему (файлы / DNS)', COLOR_GREEN, COLOR_BOLD)}")
            print(f"      {color('Примечание:', COLOR_DIM)} Ошибки в логах архивные (были до внедрения фикса).")
            print(f"      {color('Действие:', COLOR_YELLOW, COLOR_BOLD)} Если IDE сейчас запущена — закройте и откройте её заново.")
        elif ke.fix_fn:
            print(f"      {color('Авто-фикс доступен:', COLOR_GREEN)} {ke.fix_label}")
        else:
            print(f"      {color('Авто-фикс недоступен:', COLOR_YELLOW)} {ke.fix_label}")

        btn_text = ke.resolve_menu_button(ctx) if ctx is not None else ke.menu_button
        btn_text = btn_text if btn_text else "Не требуется / Индивидуально"
        print(f"      {color('Кнопка в меню (ручной фикс):', COLOR_YELLOW, COLOR_BOLD)} {color(btn_text, COLOR_CYAN, COLOR_BOLD)}")
        print()


# ═══════════════════════════════════════════════════════════════════════════════
# СОХРАНЕНИЕ ОТЧЁТА
# ═══════════════════════════════════════════════════════════════════════════════

def save_report(found: List[KnownError], log_lines: List[Tuple[str, str, str]]) -> str:
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    env_tools = os.environ.get("AG_TOOLS_DIR", "").strip()
    logs_dir = (Path(env_tools) if env_tools else (Path.home() / ".antigravity_tools")) / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        out = logs_dir / f"diagnostics_{ts}.txt"
    except Exception:
        out = Path.cwd() / f"diagnostics_{ts}.txt"

    lines_out = [
        "==================================================================",
        "=== Antigravity Patcher by energystar - Diagnostics & Auto-fix ===",
        "==================================================================",
        f"Дата проверки: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Платформа:     {platform.system()} {platform.release()} {platform.machine()}",
        f"Python:        {sys.version.split()[0]}",
        "",
        f"=== Обнаружено проблем в логах: {len(found)} ===",
        "",
    ]
    if not found:
        lines_out.append("Известных ошибок не обнаружено. Все компоненты работают штатно.\n")
    else:
        for ke in found:
            lines_out += [
                f"[{ke.severity}] {ke.title}",
                f"  Описание:    {ke.description}",
                f"  Авто-фикс:   {ke.fix_label}",
                f"  Статус:      {ke.fix_status}",
                f"  Кнопка меню: {ke.menu_button or 'Не требуется'}",
                f"  ИНСТРУКЦИЯ ДЛЯ РУЧНОГО ИСПРАВЛЕНИЯ:",
                f"    Если авто-фикс не помог, откройте главное меню программы",
                f"    и нажмите кнопку: {ke.menu_button}",
                "  Строки из логов:",
            ]
            for h in ke.hits[:5]:
                lines_out.append(f"    {h}")
            lines_out.append("")

    lines_out += [
        "==================================================================",
        "=== Последние 200 актуальных строк из логов Antigravity IDE ===",
        "==================================================================",
        "",
    ]
    for (_, fname, ln) in log_lines[-200:]:
        lines_out.append(f"[{fname}] {ln}")

    try:
        out.write_text("\n".join(lines_out), encoding="utf-8")
        return str(out)
    except Exception as e:
        return f"(ошибка сохранения: {e})"


# ═══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА (АВТО-РЕЖИМ)
# ═══════════════════════════════════════════════════════════════════════════════

def _print_system_health(ctx: dict) -> None:
    """Кратко проверяет готовность ключевых защит (патчи IDE/2.0/CLI, TLS, NRPT DNS)."""
    rows: List[Tuple[str, bool, str]] = []
    main_js = ctx.get("main_js_path", "")
    manager_p = ctx.get("manager_path", "")
    agy_p = ctx.get("agy_path", "")

    if main_js and os.path.isfile(main_js):
        try:
            from patcher.ide.patcher import is_already_patched as is_ide_patched
            from patcher.tls_fix import is_tls_fix_applied
            rows.append(("Antigravity IDE patch", bool(is_ide_patched(main_js)), "меню [2]"))
            rows.append(("TLS + Proxy fix (main.js)", bool(is_tls_fix_applied(main_js)), "меню [11]"))
        except Exception:
            pass

    if manager_p and os.path.isfile(manager_p):
        try:
            from patcher.manager.patcher import is_already_patched as is_mgr_patched
            rows.append(("Antigravity 2.0 patch", bool(is_mgr_patched(manager_p)), "меню [3]"))
        except Exception:
            pass

    if agy_p and os.path.isfile(agy_p):
        try:
            from patcher.agy.patcher import is_already_patched as is_agy_patched
            rows.append(("Antigravity CLI (agy) patch", bool(is_agy_patched(agy_p)), "меню [4]"))
        except Exception:
            pass

    if sys.platform == "win32":
        try:
            from patcher.nrpt_dns import get_nrpt_status
            nrpt_st = get_nrpt_status()
            srv = nrpt_st.get("server") or "Unblock DNS"
            rows.append((f"Windows NRPT DNS ({srv})", bool(nrpt_st.get("active")), "меню [14]"))
        except Exception:
            pass
        try:
            from patcher.hosts_pin import is_hosts_pinned
            hosts_act, pinned_ip = is_hosts_pinned()
            rows.append((f"Hosts-Pinning (VPN-Proof: {pinned_ip or 'direct'})", bool(hosts_act), "меню [14]"))
        except Exception:
            pass

    if not rows:
        return
    print()
    print_menu_section("ПРОВЕРКА ЗАЩИТЫ СИСТЕМЫ")
    for label, active, manual_hint in rows:
        if active:
            step(label, True, "активен")
        else:
            step(label, False, f"не применён ({manual_hint})")


def _detect_running_antigravity_apps() -> dict[str, bool]:
    try:
        from patcher.utils.launcher import detect_running_antigravity_apps
        return detect_running_antigravity_apps()
    except Exception:
        return {"ide": False, "manager": False}


def _restart_antigravity_apps(
    was_running: dict[str, bool], main_js_path: str = "", manager_path: str = ""
) -> list[str]:
    try:
        from patcher.utils.launcher import restart_antigravity_apps
        return restart_antigravity_apps(
            was_running, main_js_path=main_js_path, manager_path=manager_path
        )
    except Exception:
        return []


def do_diagnostics(main_js_path: str = "", manager_path: str = "", agy_path: str = "",
                   vscode_path: str = "", vscode_agy_path: str = "",
                   target_app: str = "all"):
    """Запускает полную диагностику с автоматическим применением найденных фиксов (Авто-режим)."""

    t_scope = (target_app or "all").lower()
    info("Чтение логов Antigravity IDE и Antigravity 2.0...")
    print()

    ctx = {
        "main_js_path": main_js_path,
        "manager_path": manager_path,
        "agy_path": agy_path,
        "vscode_path": vscode_path,
        "vscode_agy_path": vscode_agy_path,
    }

    # 1. Собираем логи (с очисткой кеша app-тегов перед вызовом для совместимости с моками)
    _LAST_LOG_APP_TAGS.clear()
    log_lines = collect_log_lines(max_lines_per_file=2000)

    if t_scope in ("ide", "manager", "cli") and _LAST_LOG_APP_TAGS:
        log_lines = [
            item for item in log_lines
            if t_scope in _LAST_LOG_APP_TAGS.get(item, {"shared"})
            or "shared" in _LAST_LOG_APP_TAGS.get(item, {"shared"})
        ]

    if not log_lines:
        log_bases = _get_log_base_dirs()
        if log_bases:
            warn(f"В папке логов нет свежих записей с ошибками: {log_bases[0]}")
            hint("Сначала запустите Antigravity IDE / Antigravity 2.0, чтобы сгенерировались актуальные логи.")
        else:
            warn("Папка логов Antigravity не найдена.")
            hint("Ожидаемый путь: %APPDATA%\\Antigravity IDE\\logs\\  или  %APPDATA%\\Antigravity\\logs\\")
        _print_system_health(ctx)
        return

    info(f"Проанализировано {len(log_lines)} актуальных строк из логов последней сессии.")
    print()

    # 2. Анализируем логи на известные ошибки
    error_db = _make_error_db()
    found = analyze_logs(log_lines, error_db)
    error_apps: Dict[str, set] = {}
    for (sess, fname, ln) in log_lines:
        tags = _LAST_LOG_APP_TAGS.get((sess, fname, ln)) or {"shared"}
        for ke in found:
            if ke.matches(ln):
                error_apps.setdefault(ke.key, set()).update(tags)
    ctx["_target_app"] = t_scope
    ctx["_error_apps"] = error_apps
    ctx["_hits_by_key"] = {ke.key: list(ke.hits) for ke in found}
    for ke in found:
        ke.resolve_menu_button(ctx)

    # 3. Выводим найденные проблемы с номерами кнопок в меню и статусом
    print_found_errors(found, ctx)

    if not found:
        _print_system_health(ctx)
        print()
        report_path = save_report(found, log_lines)
        ok(f"Отчёт сохранён: {color(report_path, COLOR_CYAN)}")
        return

    # 4. Авто-режим: автоматическое применение фиксов
    if sys.platform == "win32" and any(
        ke.key in (
            "tls_expired",
            "dead_proxy",
            "dns_lookup_fail",
            "user_location_400",
            "auth_failure",
            "ls_not_initialized",
            "cache_refresh_fail",
        )
        for ke in found
    ):
        try:
            from patcher.nrpt_dns import _flush_dns_cache
            _flush_dns_cache()
        except Exception:
            pass

    fixable = [ke for ke in found if ke.fix_fn is not None]
    was_running = _detect_running_antigravity_apps()
    needs_runtime_restart = False
    affected_apps = {"ide": False, "manager": False}

    def _mark_affected_for_key(err_key: str) -> None:
        if t_scope == "ide":
            affected_apps["ide"] = True
            return
        if t_scope == "manager":
            affected_apps["manager"] = True
            return
        if t_scope == "cli":
            return
        if err_key == "resource_exhausted_429" and isinstance(ctx.get("_auto_cleaned_apps"), dict):
            affected_apps["ide"] = affected_apps["ide"] or bool(ctx["_auto_cleaned_apps"].get("ide"))
            affected_apps["manager"] = affected_apps["manager"] or bool(ctx["_auto_cleaned_apps"].get("manager"))
            return
        apps = error_apps.get(err_key) or {"shared"}
        if err_key == "tls_expired" and isinstance(ctx.get("_tls_fixed_apps"), dict) and "manager" not in apps:
            affected_apps["ide"] = True
            return
        if "shared" in apps:
            affected_apps["ide"] = True
            affected_apps["manager"] = True
            return
        if "ide" in apps:
            affected_apps["ide"] = True
        if "manager" in apps:
            affected_apps["manager"] = True

    _runtime_keys = {
        "tls_expired",
        "dead_proxy",
        "sni_host_refused",
        "dns_lookup_fail",
        "user_location_400",
        "is_google_internal",
        "auth_failure",
        "ls_not_initialized",
        "cache_refresh_fail",
        "ide_session_blocked",
    }

    if fixable:
        print_menu_section("АВТО-РЕЖИМ: АВТОМАТИЧЕСКОЕ ИСПРАВЛЕНИЕ")
        print()
        info(f"Найдено {len(fixable)} проблем(ы), для которых доступно авто-исправление.")
        print()

        applied_keys = set()
        failed_keys = set()
        for ke in fixable:
            fn_key = getattr(ke.fix_fn, "__name__", ke.key)
            if fn_key in applied_keys:
                ke.fix_status = "УСПЕШНО ПРИМЕНЁН (ранее в рамках единого фикса)"
                if ke.key in _runtime_keys or ke.key == "resource_exhausted_429":
                    _mark_affected_for_key(ke.key)
                hint(f"  • {ke.title}: фикс уже применён на предыдущем шаге")
                continue

            if fn_key in failed_keys:
                ke.resolve_menu_button(ctx)
                ke.fix_status = f"ОШИБКА АВТО-ФИКСА (нажмите в меню: {ke.menu_button})"
                hint(f"  • {ke.title}: авто-фикс не удался на предыдущем шаге ({ke.menu_button})")
                continue

            # Если фикс УЖЕ применён в системе
            if ke.is_already_fixed_fn and ke.is_already_fixed_fn(ctx):
                ke.fix_status = "УЖЕ АКТИВЕН В СИСТЕМЕ (требуется перезапуск IDE)"
                applied_keys.add(fn_key)
                if ke.key in _runtime_keys:
                    needs_runtime_restart = True
                    _mark_affected_for_key(ke.key)
                ok(f"[✓] {color(ke.title, COLOR_WHITE, COLOR_BOLD)}: фикс уже активен в системе.")
                hint(f"    Ошибки в логах относятся к сессии до применения фикса.")
                print()
                continue

            print(f"  [*] Применяем авто-фикс: {color(ke.title, COLOR_WHITE, COLOR_BOLD)}")
            hint(f"      {ke.fix_label}")
            try:
                res = ke.fix_fn(ctx)
                if res is False:
                    failed_keys.add(fn_key)
                    ke.resolve_menu_button(ctx)
                    if ke.key == "resource_exhausted_429":
                        ke.fix_status = "ОТМЕНЕНО ПОЛЬЗОВАТЕЛЕМ (ручной сброс: [13])"
                        warn(f"Сброс данных отменён пользователем. Для ручного сброса выберите: {color(ke.menu_button, COLOR_YELLOW, COLOR_BOLD)}")
                    else:
                        ke.fix_status = f"ОШИБКА АВТО-ФИКСА (нажмите в меню: {ke.menu_button})"
                        err(f"Авто-фикс не сработал. Нажмите в главном меню: {color(ke.menu_button, COLOR_YELLOW, COLOR_BOLD)}")
                else:
                    ke.fix_status = "УСПЕШНО ПРИМЕНЁН АВТОМАТИЧЕСКИ"
                    applied_keys.add(fn_key)
                    needs_runtime_restart = True
                    _mark_affected_for_key(ke.key)
                    ok("Авто-фикс успешно применён!")
            except Exception as ex:
                failed_keys.add(fn_key)
                ke.resolve_menu_button(ctx)
                ke.fix_status = f"ИСКЛЮЧЕНИЕ: {ex} (нажмите в меню: {ke.menu_button})"
                err(f"Ошибка при выполнении авто-фикса: {ex}")
                err(f"Нажмите в главном меню кнопку: {color(ke.menu_button, COLOR_YELLOW, COLOR_BOLD)}")
            print()

        if needs_runtime_restart:
            ide_target = main_js_path
            mgr_target = manager_path
            if not ide_target or not os.path.isfile(ide_target):
                try:
                    from patcher.ide.discovery import find_install_root, find_main_js
                    root = find_install_root()
                    if root:
                        ide_target = find_main_js(root) or ""
                except Exception:
                    pass
            if not mgr_target or not os.path.isfile(mgr_target):
                try:
                    from patcher.manager.discovery import find_manager_binary
                    mgr_target = find_manager_binary() or ""
                except Exception:
                    pass

            restart_mask = {
                "ide": bool(was_running.get("ide") and affected_apps.get("ide")),
                "manager": bool(was_running.get("manager") and affected_apps.get("manager")),
            }
            if any(restart_mask.values()):
                restarted = _restart_antigravity_apps(restart_mask, main_js_path=ide_target, manager_path=mgr_target)
                if restarted:
                    ok(f"Автоматически перезапущено для применения настроек: {', '.join(restarted)}")
                else:
                    hint("Перезапустите Antigravity IDE / 2.0, чтобы изменения вступили в силу.")
            elif any(was_running.values()):
                hint("Исправление применено точечно к проблемному приложению; здоровые приложения остались открытыми.")
            else:
                # If the user launched Auto-fix after an error crashed/closed the app, start it automatically
                def _has_real_bundle(ttype: str, tpath: str) -> bool:
                    if not tpath:
                        return False
                    p = Path(tpath)
                    exe_names = ("Antigravity IDE.exe",) if ttype == "ide" else ("Antigravity.exe",)
                    for _ in range(6):
                        if any((p / en).is_file() for en in exe_names):
                            return True
                        if sys.platform == "darwin" and p.name.endswith(".app") and p.is_dir():
                            return True
                        p = p.parent
                    return False

                launched_idle: list[str] = []
                try:
                    from patcher.utils.launcher import launch_application
                    want_mgr = bool(affected_apps.get("manager")) or (t_scope == "all" and not any(affected_apps.values()))
                    want_ide = bool(affected_apps.get("ide")) or (t_scope == "all" and not any(affected_apps.values()))
                    if want_mgr and mgr_target and _has_real_bundle("manager", mgr_target):
                        if launch_application("manager", mgr_target):
                            launched_idle.append("Antigravity 2.0")
                    if want_ide and ide_target and _has_real_bundle("ide", ide_target):
                        if launch_application("ide", ide_target):
                            launched_idle.append("Antigravity IDE")
                except Exception:
                    launched_idle = []
                if launched_idle:
                    ok(f"Автоматически запущено после исправления: {', '.join(launched_idle)}")
                else:
                    hint("Перезапустите Antigravity IDE / 2.0, чтобы изменения вступили в силу.")
    else:
        for ke in found:
            ke.resolve_menu_button(ctx)
            ke.fix_status = f"НЕТ АВТО-ФИКСА (ручное действие: {ke.menu_button})"

    # 5. Сохраняем подробный отчёт
    report_path = save_report(found, log_lines)
    ok(f"Отчёт сохранён: {color(report_path, COLOR_CYAN)}")
    hint("В отчёте указаны все найденные ошибки, логи и кнопки в меню для ручного исправления.")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# АВТО-ЛЕЧЕНИЕ СРЕДЫ И ОБЕЗЛИЧЕННЫЙ БАГРЕПОРТ В БУФЕР ОБМЕНА
# ═══════════════════════════════════════════════════════════════════════════════

_SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"ya29\.[A-Za-z0-9\-_]{10,}"), "[REDACTED_ACCESS_TOKEN]"),
    (re.compile(r"1//[A-Za-z0-9\-_]{10,}"), "[REDACTED_REFRESH_TOKEN]"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?:ghp_|github_pat_|sk-)[A-Za-z0-9\-_]{12,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/=]{8,}", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(
            r'("(?:access_token|refresh_token|id_token|token|secret|password|api_key|apiKey|authorization|cookie)"\s*:\s*)"[^"]*"',
            re.IGNORECASE,
        ),
        r'\1"[REDACTED]"',
    ),
    (
        re.compile(
            r"((?:access_token|refresh_token|id_token|api_key|token|secret|password)=)([^\s&\"']+)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?i)(C:\\Users\\)([^\\/:*?\"<>|\r\n]+)"), r"\1[USER]"),
    (re.compile(r"(/Users/|/home/)([^/\r\n]+)"), r"\1[USER]"),
]


def _sanitize_report_text(raw: str) -> str:
    """Redact OAuth tokens, API keys, emails, and OS usernames from bug report text."""
    text = str(raw or "")
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _copy_text_to_clipboard(text: str) -> bool:
    """Copy UTF-8 text to the OS clipboard cleanly without showing console windows."""
    if not text:
        return False
    flags = 0x08000000 if sys.platform == "win32" else 0
    if sys.platform == "win32":
        # 1. PowerShell Set-Clipboard preserves UTF-8 Cyrillic cleanly
        try:
            ps_cmd = (
                "[System.Console]::InputEncoding = [System.Text.Encoding]::UTF8; "
                "Set-Clipboard -Value ([System.Console]::In.ReadToEnd())"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=6,
                creationflags=flags,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass
        # 2. Fallback to clip.exe with UTF-16LE BOM
        try:
            res = subprocess.run(
                ["clip"],
                input=b"\xff\xfe" + text.encode("utf-16-le"),
                capture_output=True,
                timeout=5,
                creationflags=flags,
            )
            if res.returncode == 0:
                return True
        except Exception:
            return False
    elif sys.platform == "darwin":
        try:
            res = subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True, timeout=5)
            return res.returncode == 0
        except Exception:
            return False
    else:
        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            try:
                res = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=5)
                if res.returncode == 0:
                    return True
            except Exception:
                continue
    return False


def _collect_compact_env_summary() -> List[str]:
    """Gather sanitized environment/patch status lines without raising."""
    lines: List[str] = []
    try:
        from patcher.constants import VERSION
        lines.append(f"Patcher: v{VERSION}")
    except Exception:
        lines.append("Patcher: unknown")
    lines.append(f"OS: {platform.system()} {platform.release()} {platform.machine()} | Python {sys.version.split()[0]}")
    if sys.platform == "win32":
        try:
            from patcher.nrpt_dns import get_nrpt_status
            nrpt = get_nrpt_status()
            lines.append(f"NRPT: {'ACTIVE (' + str(nrpt.get('server') or '') + ')' if nrpt.get('active') else 'DISABLED'}")
        except Exception:
            pass
        try:
            from patcher.hosts_pin import is_hosts_pinned
            h_act, h_ip = is_hosts_pinned()
            lines.append(f"Hosts-Pinning: {'ACTIVE (' + str(h_ip) + ')' if h_act else 'DISABLED'}")
        except Exception:
            pass
        try:
            from patcher.updater_lock import is_update_locked
            lines.append(f"UpdateLock: {'LOCKED' if is_update_locked() else 'OPEN'}")
        except Exception:
            pass
    return lines


def copy_sanitized_bug_report_to_clipboard(error_text: str, context_info: str = "") -> bool:
    """
    Build a compact sanitized bug report (redacting tokens/emails/secrets),
    copy it to the Windows clipboard, and print a clear Russian notice telling
    the user to paste it (Ctrl+V) to @energystar6 / https://t.me/energystar6.
    """
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = [
        "=== Antigravity Patcher Bug Report (Sanitized) ===",
        f"Time: {ts}",
    ] + _collect_compact_env_summary()

    if context_info:
        header.append(f"Context: {_sanitize_report_text(context_info)[:400]}")

    sanitized_err = _sanitize_report_text(error_text).strip()
    if len(sanitized_err) > 2500:
        sanitized_err = "...(truncated)...\n" + sanitized_err[-2500:]

    report_parts = header + ["", "--- Error / Traceback ---", sanitized_err]
    report = _sanitize_report_text("\n".join(report_parts))

    copied = _copy_text_to_clipboard(report)
    print()
    if copied:
        warn("Обнаружена нестандартная ошибка — готовый обезличенный отчёт УЖЕ скопирован в ваш буфер обмена!")
        hint("Просто нажмите Ctrl+V и отправьте его разработчику в Telegram: @energystar6 (https://t.me/energystar6)")
    else:
        warn("Обнаружена нестандартная ошибка (не удалось скопировать в буфер обмена автоматически).")
        hint("Отправьте текст ошибки ниже разработчику в Telegram: @energystar6 (https://t.me/energystar6)")
        print(report[:1200])
    return copied


def _check_tls_host_reachable(host: str = "www.googleapis.com", timeout: float = 2.5) -> Tuple[bool, str]:
    """Perform a direct TCP + TLS handshake probe against a Google host."""
    import ssl

    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                return True, str(tls.version() or "TLS")
    except Exception as ex:
        return False, str(ex)


def _check_cloudcode_connectivity(timeout: float = 2.5) -> dict:
    """
    Check DNS resolution and HTTPS reachability of Google Cloud Code API.
    Returns dict with keys: reachable, dns_poisoned, regional_400, tls_timeout, network_blocked, detail.
    """
    import urllib.request
    import urllib.error

    host = "daily-cloudcode-pa.googleapis.com"
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = [
            row[4][0]
            for row in (infos or [])
            if isinstance(row, (tuple, list)) and len(row) > 4 and isinstance(row[4], (tuple, list)) and len(row[4]) > 0
        ]
        if not ips or all(str(ip).startswith(("127.", "0.", "::1")) for ip in ips):
            return {
                "reachable": False,
                "dns_poisoned": True,
                "regional_400": False,
                "tls_timeout": False,
                "network_blocked": True,
                "detail": f"DNS returned bogon/empty for {host}: {ips}",
            }
    except OSError as ex:
        return {
            "reachable": False,
            "dns_poisoned": True,
            "regional_400": False,
            "tls_timeout": False,
            "network_blocked": True,
            "detail": f"DNS lookup failed for {host}: {ex}",
        }

    url = f"https://{host}/v1internal:loadCodeAssist"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json", "User-Agent": "AntigravityPatcher"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "reachable": True,
                "dns_poisoned": False,
                "regional_400": False,
                "tls_timeout": False,
                "network_blocked": False,
                "detail": f"HTTP {resp.status}",
            }
    except urllib.error.HTTPError as he:
        body = ""
        try:
            body = he.read(512).decode("utf-8", errors="ignore")
        except Exception:
            pass
        low = body.lower()
        is_geo_400 = he.code == 400 and any(
            k in low for k in ("location", "failed_precondition", "ineligible", "not supported")
        )
        if is_geo_400:
            return {
                "reachable": False,
                "dns_poisoned": False,
                "regional_400": True,
                "tls_timeout": False,
                "network_blocked": False,
                "detail": "HTTP 400 (regional restriction)",
            }
        # 401/403/404 from Google API means endpoint is reachable through the network
        return {
            "reachable": True,
            "dns_poisoned": False,
            "regional_400": False,
            "tls_timeout": False,
            "network_blocked": False,
            "detail": f"HTTP {he.code}",
        }
    except Exception as ex:
        msg_low = str(ex).lower()
        is_tls_to = any(k in msg_low for k in ("timed out", "timeout", "handshake", "ssl", "eof"))
        return {
            "reachable": False,
            "dns_poisoned": False,
            "regional_400": False,
            "tls_timeout": is_tls_to,
            "network_blocked": True,
            "detail": str(ex),
        }


def _heal_google_connectivity(
    ctx: Optional[dict] = None,
    quiet: bool = False,
    require_bypass: bool = False,
) -> dict:
    """
    Unified network/TLS/DNS/Hosts self-healer for Google Cloud Code connectivity.
    1. Clears dead Windows/User/Env proxies.
    2. Flushes DNS cache and probes daily-cloudcode-pa.googleapis.com.
    3. On Windows failure (400 / TLS handshake timeout / DNS fail): tries NRPT DNS
       with live failover across unblock servers, and falls back to VPN-proof
       Hosts-Pinning (direct SNI proxy IPv4, bypassing broken VPN DNS/IPv6).
    """
    if isinstance(ctx, dict):
        ctx.pop("_live_conn_cache", None)

    out: Dict[str, Any] = {
        "proxy_healed": False,
        "nrpt_auto_enabled": False,
        "hosts_auto_enabled": False,
        "nrpt_active": False,
        "hosts_active": False,
        "connectivity_ok": False,
        "changed": False,
        "diagnosis": "",
        "errors": [],
    }

    reg_enabled, reg_server = _read_windows_registry_proxy()
    if reg_enabled and reg_server:
        hp = _parse_proxy_host_port(reg_server)
        if hp and not _check_tcp_reachable(hp[0], hp[1], timeout=0.5):
            if _disable_windows_registry_proxy():
                out["proxy_healed"] = True
                out["changed"] = True
                if not quiet:
                    ok(f"Авто-лечение: отключён мёртвый системный прокси ({hp[0]}:{hp[1]}).")

    cleared_user_env = _clear_dead_user_env_proxies()
    for var, h, p in cleared_user_env:
        os.environ.pop(var, None)
        os.environ.pop(var.lower(), None)
        out["proxy_healed"] = True
        out["changed"] = True
        if not quiet:
            ok(f"Авто-лечение: удалён мёртвый прокси {var}={h}:{p}.")

    for var, h, p in _get_dead_env_proxies():
        os.environ.pop(var, None)
        os.environ.pop(var.lower(), None)
        if not quiet:
            warn(f"Сброшен недоступный прокси окружения {var}={h}:{p}.")

    if (
        os.environ.get("AG_DISABLE_LOCAL_PROXY_PROBE") != "1"
        and ("PYTEST_CURRENT_TEST" not in os.environ or os.environ.get("AG_PROBE_LOCAL_PROXY") == "1")
    ):
        import json as _json
        import time as _time
        import urllib.parse as _urlparse

        _env_dir = os.environ.get("AG_TOOLS_DIR", "").strip()
        _cfg_dir = Path(_env_dir) if _env_dir else (Path.home() / ".antigravity_tools")
        _cfg_file = _cfg_dir / "proxy_config.json"
        _prev_proxy_url = ""
        _prev_proxy_src = ""
        if _cfg_file.is_file():
            try:
                _prev_cfg = _json.loads(_cfg_file.read_text(encoding="utf-8"))
                if isinstance(_prev_cfg, dict):
                    _raw_prev = str(_prev_cfg.get("proxy_url") or _prev_cfg.get("url") or "").strip()
                    if _raw_prev:
                        _prev_proxy_url = _raw_prev if "://" in _raw_prev else f"http://{_raw_prev}"
                    _prev_proxy_src = str(_prev_cfg.get("source") or "").strip()
            except Exception:
                pass

        _prev_alive = False
        if _prev_proxy_url:
            _hp_saved = _parse_proxy_host_port(_prev_proxy_url)
            if _hp_saved:
                _prev_alive = _check_tcp_reachable(_hp_saved[0], _hp_saved[1], timeout=0.18)
            else:
                _prev_alive = True

        if _prev_proxy_url and not _prev_alive and _prev_proxy_src.startswith("Local VPN"):
            try:
                _cfg_file.unlink()
            except Exception:
                pass

        _has_env_proxy = any(
            os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy")
        )
        if not _has_env_proxy:
            if _prev_alive and _prev_proxy_url:
                os.environ["HTTP_PROXY"] = _prev_proxy_url
                os.environ["HTTPS_PROXY"] = _prev_proxy_url
                os.environ["ALL_PROXY"] = _prev_proxy_url
                os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
            else:
                _found_vpn = False
                for _vpn_port in (10808, 10809, 2080, 7890, 2081, 7891, 8889, 12334):
                    if _check_tcp_reachable("127.0.0.1", _vpn_port, timeout=0.12):
                        _vpn_url = f"http://127.0.0.1:{_vpn_port}"
                        os.environ["HTTP_PROXY"] = _vpn_url
                        os.environ["HTTPS_PROXY"] = _vpn_url
                        os.environ["ALL_PROXY"] = _vpn_url
                        os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
                        try:
                            _cfg_dir.mkdir(parents=True, exist_ok=True)
                            _cfg_file.write_text(
                                _json.dumps(
                                    {
                                        "proxy_url": _vpn_url,
                                        "source": f"Local VPN ({_vpn_port})",
                                        "updated_at": int(_time.time()),
                                    },
                                    indent=2,
                                ),
                                encoding="utf-8",
                            )
                        except Exception:
                            pass
                        if _prev_proxy_url != _vpn_url:
                            out["proxy_healed"] = True
                            out["changed"] = True
                        _found_vpn = True
                        break
                if not _found_vpn and _prev_proxy_url and not _prev_alive and _prev_proxy_src.startswith("Local VPN"):
                    out["proxy_healed"] = True
                    out["changed"] = True

    if sys.platform == "win32" and out["proxy_healed"]:
        try:
            from patcher.nrpt_dns import _flush_dns_cache
            _flush_dns_cache()
        except Exception:
            pass

    conn = _check_cloudcode_connectivity(timeout=2.5)
    out["connectivity_ok"] = bool(conn.get("reachable"))

    if sys.platform != "win32":
        if not out["connectivity_ok"]:
            out["diagnosis"] = str(conn.get("detail") or "unreachable")
        return out

    try:
        from patcher.nrpt_dns import (
            enable_nrpt_rule,
            get_nrpt_status,
            get_preferred_dns_ip,
            is_nrpt_supported,
            _flush_dns_cache,
        )
        from patcher.hosts_pin import enable_hosts_pinning, is_hosts_pinned

        nrpt_st = get_nrpt_status() if is_nrpt_supported() else {}
        hosts_act, hosts_ip = is_hosts_pinned()
        out["nrpt_active"] = bool(nrpt_st.get("active"))
        out["hosts_active"] = bool(hosts_act)

        if require_bypass or not out["connectivity_ok"]:
            # Step 1: Ensure NRPT uses a live substituting resolver if not using hosts pinning
            if is_nrpt_supported() and not hosts_act:
                target_ip = get_preferred_dns_ip()
                need_nrpt = (not nrpt_st.get("active")) or (nrpt_st.get("server") in ("", "176.108.113.214"))
                if need_nrpt:
                    ok_nrpt, msg_nrpt = enable_nrpt_rule(target_ip)
                    if ok_nrpt:
                        _flush_dns_cache()
                        out["nrpt_auto_enabled"] = True
                        out["nrpt_active"] = True
                        out["changed"] = True
                        if not quiet:
                            ok(f"Авто-обход 400/DNS: включён Windows NRPT DNS ({msg_nrpt}).")
                        conn = _check_cloudcode_connectivity(timeout=2.5)
                        out["connectivity_ok"] = bool(conn.get("reachable"))
                    else:
                        out["errors"].append(f"nrpt_enable: {msg_nrpt}")
                        if not quiet:
                            warn(f"NRPT DNS недоступен: {msg_nrpt}")

            # Step 2: If direct/NRPT STILL fails (common behind VPN TUN / IPv6 TLS timeout!),
            # OR bypass was explicitly required when NRPT failed or VPN DNS overrode NRPT to standard Google IPs,
            # activate VPN-proof direct Hosts-Pinning.
            nrpt_bypassed_by_vpn = False
            if require_bypass and out["nrpt_active"] and not out["hosts_active"]:
                _google_prefixes = ("172.217.", "142.250.", "108.177.", "74.125.", "216.58.", "64.233.")
                for _dom in ("cloudcode-pa.googleapis.com", "daily-cloudcode-pa.googleapis.com"):
                    try:
                        _infos = socket.getaddrinfo(_dom, 443, type=socket.SOCK_STREAM)
                        _ips = [str(r[4][0]) for r in (_infos or []) if isinstance(r, (tuple, list)) and len(r) > 4]
                        if _ips and all(ip.startswith(_google_prefixes) for ip in _ips):
                            nrpt_bypassed_by_vpn = True
                            break
                    except Exception:
                        pass

            if (
                (not out["connectivity_ok"])
                or (require_bypass and not out["hosts_active"] and (not out["nrpt_active"] or nrpt_bypassed_by_vpn))
            ):
                ok_hp, msg_hp = enable_hosts_pinning()
                if ok_hp:
                    _flush_dns_cache()
                    out["hosts_auto_enabled"] = True
                    out["hosts_active"] = True
                    out["changed"] = True
                    if not quiet:
                        ok(f"Авто-лечение TLS/DNS: активирован Hosts-Pinning ({msg_hp}).")
                    conn = _check_cloudcode_connectivity(timeout=3.0)
                    out["connectivity_ok"] = bool(conn.get("reachable"))
                else:
                    out["errors"].append(f"hosts_pin: {msg_hp}")
                    if not quiet:
                        warn(f"Hosts-Pinning: {msg_hp}")
                        if "Administrator" in str(msg_hp) or "denied" in str(msg_hp).lower():
                            hint("Для правки NRPT / hosts запустите патчер от имени Администратора.")

            if not out["connectivity_ok"]:
                oauth_ok, oauth_detail = _check_tls_host_reachable("www.googleapis.com", timeout=2.5)
                if not oauth_ok:
                    out["diagnosis"] = (
                        "Текущий VPN-туннель или сеть сбрасывает TLS handshake до Google "
                        f"({conn.get('detail')}; oauth={oauth_detail}). "
                        "Переключите сервер VPN или временно отключите VPN."
                    )
                else:
                    out["diagnosis"] = str(conn.get("detail") or "Cloud Code API unreachable")
                if not quiet:
                    warn(f"Проверка соединения с Google API не пройдена: {out['diagnosis']}")
    except Exception as ex:
        out["errors"].append(f"connectivity_heal: {ex}")
        out["diagnosis"] = str(ex)

    if isinstance(ctx, dict):
        ctx["_live_conn_cache"] = {"reachable": out["connectivity_ok"], "regional_400": bool(conn.get("regional_400")), "detail": out.get("diagnosis") or conn.get("detail", "")}
    return out


def _sync_target_update_lock(target: str) -> bool:
    """Ensure update lock is applied for the patched component(s) on Windows."""
    if os.name != "nt":
        return False
    from patcher import updater_lock
    from patcher.ide import discovery as ide_disc
    from patcher.manager import discovery as mgr_disc
    from patcher.agy import discovery as agy_disc
    from patcher.vscode import discovery as vsc_disc

    t = (target or "all").lower()
    if t == "all":
        if ide_disc.find_install_root() and not updater_lock.is_ide_locked():
            updater_lock.toggle_ide_updates()
        if mgr_disc.find_manager_binary() and not updater_lock.is_manager_locked():
            updater_lock.toggle_manager_updates()
        if (agy_disc.find_agy_binary() or vsc_disc.find_gemini_antigravity_binary()) and not updater_lock.is_cli_locked():
            updater_lock.toggle_cli_updates()
        if vsc_disc.find_extension_js() and not updater_lock.is_vscode_locked():
            updater_lock.toggle_vscode_updates()
        return True
    if t == "ide":
        if not updater_lock.is_ide_locked():
            updater_lock.toggle_ide_updates()
        return True
    if t == "manager":
        if not updater_lock.is_manager_locked():
            updater_lock.toggle_manager_updates()
        return True
    if t == "cli":
        if not updater_lock.is_cli_locked():
            updater_lock.toggle_cli_updates()
        return True
    if t == "vscode":
        if not updater_lock.is_vscode_locked():
            updater_lock.toggle_vscode_updates()
        if not updater_lock.is_cli_locked():
            updater_lock.toggle_cli_updates()
        return True
    return False


def ensure_target_and_env_healthy(
    target: str = "all",
    quiet: bool = False,
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
    run_connectivity: bool = True,
) -> dict:
    """
    Автоматический конвейер самолечения при каждом патче и в --auto-patch:
      (a) лечит мёртвый системный/env прокси и чистит битые флаги AppCompatFlags\\Layers + IFEO;
      (b) проверяет доступность Google Cloud Code и при региональном блоке 400 / сбое TLS/DNS
          автоматически включает Windows NRPT DNS и VPN-устойчивый Hosts-Pinning;
      (c) синхронизирует блокировку авто-обновлений и фоновый сторож AutoWatch;
      (d) при любой неисправимой ошибке копирует обезличенный отчёт в буфер обмена.
    """
    result: Dict[str, Any] = {
        "ok": True,
        "target": target,
        "proxy_healed": False,
        "tls_healed": False,
        "registry_cleaned": {"compat_removed": [], "ifeo_removed": []},
        "connectivity_ok": True,
        "nrpt_auto_enabled": False,
        "hosts_auto_enabled": False,
        "diagnosis": "",
        "update_lock_synced": False,
        "autowatch_synced": False,
        "errors": [],
        "bug_report_copied": False,
    }

    try:
        # (a) Registry/IFEO cleanup
        try:
            from patcher.data_reset import clean_compat_and_ifeo_registry
            reg_clean = clean_compat_and_ifeo_registry()
            result["registry_cleaned"] = reg_clean
            if not quiet and (reg_clean.get("compat_removed") or reg_clean.get("ifeo_removed")):
                ok(
                    f"Авто-лечение: очищены битые флаги реестра "
                    f"(AppCompat: {len(reg_clean.get('compat_removed', []))}, "
                    f"IFEO: {len(reg_clean.get('ifeo_removed', []))})."
                )
        except Exception as reg_ex:
            result["errors"].append(f"registry_cleanup: {reg_ex}")

        # (a2) Ensure IDE TLS/proxy fix is intact if main.js exists
        try:
            _mjs = main_js_path
            if not _mjs:
                from patcher.ide import discovery as _ide_disc
                _root = _ide_disc.find_install_root()
                _mjs = _ide_disc.find_main_js(_root) if _root else ""
            if _mjs and Path(_mjs).is_file():
                from patcher.tls_fix import do_tls_fix_inject, is_tls_fix_applied
                if not is_tls_fix_applied(_mjs):
                    import contextlib, io
                    if quiet:
                        with contextlib.redirect_stdout(io.StringIO()):
                            injected = bool(do_tls_fix_inject(_mjs))
                    else:
                        injected = bool(do_tls_fix_inject(_mjs))
                    if injected:
                        result["tls_healed"] = True
                        if not quiet:
                            ok("Авто-лечение: восстановлен TLS + Proxy fix в main.js.")
        except Exception as tls_ex:
            result["errors"].append(f"tls_fix: {tls_ex}")

        # (b) Heal dead proxies & Google Cloud Code TLS/DNS/400 connectivity
        if run_connectivity:
            heal = _heal_google_connectivity(ctx=None, quiet=quiet)
            result["proxy_healed"] = bool(heal.get("proxy_healed"))
            result["connectivity_ok"] = bool(heal.get("connectivity_ok"))
            result["nrpt_auto_enabled"] = bool(heal.get("nrpt_auto_enabled"))
            result["hosts_auto_enabled"] = bool(heal.get("hosts_auto_enabled"))
            result["diagnosis"] = str(heal.get("diagnosis") or "")
            result["errors"].extend(heal.get("errors") or [])

        # (c) Ensure update lock + auto-repatch watcher are synced
        try:
            if quiet:
                import contextlib, io
                with contextlib.redirect_stdout(io.StringIO()):
                    result["update_lock_synced"] = _sync_target_update_lock(target)
            else:
                result["update_lock_synced"] = _sync_target_update_lock(target)
        except Exception as lock_ex:
            result["errors"].append(f"updater_lock: {lock_ex}")

        try:
            from patcher.auto_repatch import sync_auto_repatch_state
            if quiet:
                import contextlib, io
                with contextlib.redirect_stdout(io.StringIO()):
                    sync_auto_repatch_state(
                        main_js_path=main_js_path,
                        manager_path=manager_path,
                        agy_path=agy_path,
                    )
            else:
                sync_auto_repatch_state(
                    main_js_path=main_js_path,
                    manager_path=manager_path,
                    agy_path=agy_path,
                )
            result["autowatch_synced"] = True
        except Exception as watch_ex:
            result["errors"].append(f"auto_repatch: {watch_ex}")

    except Exception as exc:
        import traceback
        result["ok"] = False
        result["errors"].append(str(exc))
        result["bug_report_copied"] = copy_sanitized_bug_report_to_clipboard(
            traceback.format_exc(),
            context_info=f"ensure_target_and_env_healthy(target={target})",
        )
        if not quiet:
            err(f"Критическая ошибка авто-лечения ({target}): {exc}")

    return result


def _autopilot_state_path() -> Path:
    ag_dir = os.environ.get("AG_TOOLS_DIR", "").strip()
    base = Path(ag_dir) if ag_dir else (Path.home() / ".antigravity_tools")
    return base / "autopilot_state.json"


def _auto_heal_config_path() -> Path:
    ag_dir = os.environ.get("AG_TOOLS_DIR", "").strip()
    base = Path(ag_dir) if ag_dir else (Path.home() / ".antigravity_tools")
    return base / "auto_heal_config.json"


def load_auto_heal_config() -> dict:
    """Load auto-heal settings; auto-mode is ON by default out of the box."""
    import json

    defaults = {"enabled": True, "auto_relaunch_broken_app": True}
    p = _auto_heal_config_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(defaults)
                merged.update(data)
                return merged
        except Exception:
            pass
    return defaults


def save_auto_heal_config(cfg: dict) -> dict:
    """Atomically persist auto-heal settings."""
    import json

    cur = load_auto_heal_config()
    if isinstance(cfg, dict):
        cur.update(cfg)
    p = _auto_heal_config_path()
    try:
        from patcher.utils.atomic import write_atomic
        write_atomic(p, json.dumps(cur, indent=2, ensure_ascii=False).encode("utf-8"))
    except Exception:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return cur


def _load_autopilot_state() -> dict:
    import json

    p = _autopilot_state_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"file_cursors": {}, "cooldowns": {}, "last_run_ts": 0, "healed_count": 0}


def _save_autopilot_state(state: dict) -> None:
    import json

    p = _autopilot_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


_LAST_FRESH_DELTA_BY_APP: Dict[str, List[Tuple[str, str, str]]] = {}


def _collect_fresh_log_delta(
    state: dict,
    max_age_minutes: int = 15,
    target_app: str = "all",
) -> List[Tuple[str, str, str]]:
    """
    Read only newly appended lines from recent IDE / 2.0 / CLI logs since the
    last autopilot cursor check (never re-triggering on historical logs).
    """
    import time as _time

    _LAST_FRESH_DELTA_BY_APP.clear()
    now = _time.time()
    cutoff = now - (max(1, int(max_age_minutes)) * 60.0)
    cursors = state.setdefault("file_cursors", {})
    collected: List[Tuple[str, str, str]] = []
    t_scope = (target_app or "all").lower()

    candidate_files: List[Tuple[str, Path, str]] = []
    for base in _get_log_base_dirs():
        app_tag = _tag_app_for_log_path(base)
        if t_scope in ("ide", "manager", "cli") and app_tag not in (t_scope, "shared"):
            continue
        try:
            if base.is_dir():
                for f in base.iterdir():
                    if f.is_file() and f.suffix == ".log" and f.stat().st_mtime >= cutoff:
                        candidate_files.append((base.name, f, app_tag))
                sessions = sorted(
                    [d for d in base.iterdir() if d.is_dir()],
                    key=lambda d: d.stat().st_mtime,
                    reverse=True,
                )[:2]
                for sess in sessions:
                    for name in _PRIORITY_LOGS:
                        p = sess / name
                        if p.is_file() and p.stat().st_mtime >= cutoff:
                            candidate_files.append((sess.name, p, app_tag))
                    for pattern in (
                        "window*/renderer.log",
                        "window*/exthost/exthost.log",
                        "window*/exthost/output_logging_*/*Antigravity*.log",
                    ):
                        for p in sess.glob(pattern):
                            if p.is_file() and p.stat().st_mtime >= cutoff:
                                candidate_files.append((sess.name, p, app_tag))
        except Exception:
            continue

    seen_paths: set[str] = set()
    for sess_name, log_file, app_tag in candidate_files:
        key = str(log_file)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        try:
            st = log_file.stat()
        except OSError:
            continue

        prev = cursors.get(key) if isinstance(cursors.get(key), dict) else None
        if prev and int(prev.get("mtime_ns", -1)) == int(st.st_mtime_ns) and int(prev.get("size", -1)) == int(st.st_size):
            continue

        prev_offset = int(prev.get("offset", 0)) if prev else max(0, int(st.st_size) - 32768)
        if int(st.st_size) < prev_offset:
            prev_offset = max(0, int(st.st_size) - 32768)

        try:
            with open(log_file, "rb") as fh:
                fh.seek(prev_offset)
                chunk = fh.read(131072)
                new_offset = fh.tell()
            cursors[key] = {"mtime_ns": int(st.st_mtime_ns), "size": int(st.st_size), "offset": int(new_offset)}
            text = chunk.decode("utf-8", errors="replace")
            for ln in text.splitlines():
                s = ln.strip()
                if s and '"postmessage-rpc"' not in s and not _UPDATER_NOISE_RE.search(s):
                    entry = (sess_name, log_file.name, s)
                    collected.append(entry)
                    _LAST_FRESH_DELTA_BY_APP.setdefault(app_tag, []).append(entry)
        except Exception:
            continue

    # Prune stale cursors to keep state tiny
    if len(cursors) > 80:
        for stale_key in list(cursors.keys())[: len(cursors) - 40]:
            cursors.pop(stale_key, None)

    return collected


def run_autopilot_guardian(
    quiet: bool = True,
    max_age_minutes: int = 15,
    main_js_path: str = "",
    manager_path: str = "",
    agy_path: str = "",
    target_app: str = "all",
    force: bool = False,
    allow_restart: bool = True,
) -> dict:
    """
    Autonomous self-healing loop entrypoint (used by AutoWatch, Studio, and In-App Mini-Window):
      1. Inspects binary/UI targets and re-patches silently if an update reset anything.
      2. Checks only newly written recent log lines (cursor-based) + live network/TLS health,
         tagged per-app ('ide' vs 'manager' vs 'cli' vs 'shared').
      3. Respects auto_heal_config.json (ON by default out of the box), and restarts ONLY the
         actually broken/modified app — never killing a healthy Antigravity 2.0 session for IDE
         errors (or vice versa).
    """
    import contextlib
    import io
    import time as _time

    now = int(_time.time())
    t_scope = (target_app or "all").lower()
    if t_scope not in ("all", "ide", "manager", "cli"):
        t_scope = "all"

    heal_cfg = load_auto_heal_config()
    auto_heal_enabled = bool(heal_cfg.get("enabled", True))
    state = _load_autopilot_state()
    cooldowns = state.setdefault("cooldowns", {})
    actions: list[str] = []
    errors: list[str] = []
    restart_apps: Dict[str, bool] = {"ide": False, "manager": False}
    diagnosis = ""
    connectivity_ok = True
    pending_issues: Dict[str, List[str]] = {}

    def _cooldown_ready(cat: str, min_interval_sec: int) -> bool:
        if force:
            return True
        last_ts = int(cooldowns.get(cat, 0) or 0)
        return (now - last_ts) >= min_interval_sec

    def _mark_cooldown(cat: str) -> None:
        cooldowns[cat] = now

    def _run_silent(fn, *args, **kwargs):
        if not quiet:
            return fn(*args, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    try:
        from patcher.auto_repatch import inspect_targets, run_auto_repatch, _append_log

        # Collect fresh log lines first (scoped when target_app != 'all')
        _LAST_FRESH_DELTA_BY_APP.clear()
        fresh_lines = (
            _collect_fresh_log_delta(state, max_age_minutes=max_age_minutes, target_app=t_scope)
            if t_scope != "all"
            else _collect_fresh_log_delta(state, max_age_minutes=max_age_minutes)
        )
        found_fresh: list[KnownError] = []
        if fresh_lines:
            found_fresh = analyze_logs(fresh_lines, _make_error_db())
        fresh_keys = {ke.key for ke in found_fresh}

        # Tag errors per app so an IDE failure never restarts Antigravity 2.0 (or vice versa)
        fresh_keys_by_app: Dict[str, set] = {}
        if _LAST_FRESH_DELTA_BY_APP:
            for app_tag, app_lines in _LAST_FRESH_DELTA_BY_APP.items():
                if not app_lines:
                    continue
                app_found = analyze_logs(app_lines, _make_error_db())
                if app_found:
                    fresh_keys_by_app[app_tag] = {ke.key for ke in app_found}
        elif fresh_keys:
            # Fallback when _collect_fresh_log_delta is mocked in tests
            fallback_tag = t_scope if t_scope in ("ide", "manager", "cli") else "shared"
            fresh_keys_by_app[fallback_tag] = set(fresh_keys)

        if not auto_heal_enabled and not force:
            for app_tag, kset in fresh_keys_by_app.items():
                pending_issues[app_tag] = sorted(kset)
            state["pending_issues"] = pending_issues
            state["last_run_ts"] = now
            state["last_status"] = "pending" if pending_issues else str(state.get("last_status") or "healthy")
            _save_autopilot_state(state)
            return {
                "ok": True,
                "status": state["last_status"],
                "auto_heal_enabled": False,
                "connectivity_ok": bool(state.get("connectivity_ok", True)),
                "diagnosis": str(state.get("diagnosis") or ""),
                "actions": [],
                "errors": [],
                "pending_issues": pending_issues,
                "last_run_ts": now,
                "healed_count": int(state.get("healed_count", 0) or 0),
            }

        # 1. Check unpatched / updated binaries or missing UI/extensions (scoped to target_app)
        tgt_st = inspect_targets(
            main_js_path=main_js_path,
            manager_path=manager_path,
            agy_path=agy_path,
        )
        scoped_needs_ide = bool(tgt_st.get("needs_ide") or tgt_st.get("needs_tls")) if t_scope in ("all", "ide") else False
        scoped_needs_mgr = bool(tgt_st.get("needs_mgr")) if t_scope in ("all", "manager") else False
        scoped_needs_other = bool(
            (t_scope in ("all", "cli") and tgt_st.get("needs_agy"))
            or (t_scope == "all" and (tgt_st.get("needs_vsc") or tgt_st.get("needs_ext") or tgt_st.get("needs_ui")))
            or (t_scope == "ide" and tgt_st.get("needs_ext"))
            or (t_scope == "manager" and tgt_st.get("needs_ui"))
        )
        scoped_needs_any = bool(scoped_needs_ide or scoped_needs_mgr or scoped_needs_other) if t_scope != "all" else bool(tgt_st.get("needs_any"))

        if scoped_needs_any and _cooldown_ready(f"repatch:{t_scope}", 120):
            _mark_cooldown(f"repatch:{t_scope}")
            needs_core_restart = bool(scoped_needs_ide or scoped_needs_mgr) and bool(allow_restart)
            repatch_kwargs = {
                "restart_running": needs_core_restart,
                "main_js_path": main_js_path if t_scope in ("all", "ide") else "",
                "manager_path": manager_path if t_scope in ("all", "manager") else "",
                "agy_path": agy_path if t_scope in ("all", "cli") else "",
            }
            rep = _run_silent(run_auto_repatch, **repatch_kwargs) or {}
            if rep.get("patched"):
                actions.append(f"repatch:{','.join(rep['patched'])}")

        # 2. Verify local environment & Google TLS connectivity (throttled when healthy & no fresh errors)
        _net_tls_keys = {"tls_expired", "dead_proxy", "sni_host_refused", "dns_lookup_fail", "auth_failure", "cache_refresh_fail", "ls_not_initialized"}
        _loc_400_keys = {"user_location_400", "is_google_internal"}
        _env_error_keys = _net_tls_keys | _loc_400_keys
        _fatal_restart_keys = {"tls_expired", "dead_proxy", "sni_host_refused", "dns_lookup_fail", "user_location_400", "is_google_internal", "ide_session_blocked"}

        net_ready = bool(fresh_keys & _net_tls_keys) and _cooldown_ready("net_tls", 300)
        loc_ready = bool(fresh_keys & _loc_400_keys) and _cooldown_ready("loc_400", 300)
        need_bypass = bool(fresh_keys & (_loc_400_keys | {"dns_lookup_fail"}))

        def _mark_broken_app_restarts(include_all_on_empty: bool = False) -> None:
            if t_scope == "ide":
                restart_apps["ide"] = True
                return
            if t_scope == "manager":
                restart_apps["manager"] = True
                return
            if t_scope == "cli":
                return
            matched_any = False
            for app_name in ("ide", "manager", "shared"):
                kset = fresh_keys_by_app.get(app_name) or set()
                trigger_set = kset & (_fatal_restart_keys if not include_all_on_empty else _env_error_keys)
                if not trigger_set:
                    trigger_set = kset & _env_error_keys if include_all_on_empty else set()
                if trigger_set:
                    matched_any = True
                    if app_name == "ide":
                        restart_apps["ide"] = True
                    elif app_name == "manager":
                        restart_apps["manager"] = True
                    else:
                        restart_apps["ide"] = True
                        restart_apps["manager"] = True
            if not matched_any and include_all_on_empty:
                # Only restart running apps when no specific app log tag narrowed it down AND caller explicitly had blocking keys
                if fresh_keys & _fatal_restart_keys:
                    restart_apps["ide"] = True
                    restart_apps["manager"] = True

        prev_conn_ok = bool(state.get("connectivity_ok", False))
        should_probe_env = force or (not prev_conn_ok) or bool(fresh_keys & _env_error_keys) or _cooldown_ready("env_health_probe", 180)
        if should_probe_env:
            _mark_cooldown("env_health_probe")
            health_kwargs = {
                "target": t_scope if t_scope in ("ide", "manager", "cli") else "all",
                "quiet": True,
                "main_js_path": main_js_path,
                "manager_path": manager_path,
                "agy_path": agy_path,
            }
            if net_ready or loc_ready:
                health_kwargs["run_connectivity"] = False
            health = _run_silent(ensure_target_and_env_healthy, **health_kwargs) or {}
            connectivity_ok = bool(health.get("connectivity_ok", True))
            diagnosis = str(health.get("diagnosis") or "")
            if health.get("tls_healed"):
                actions.append("tls_healed")
                if t_scope in ("all", "ide"):
                    restart_apps["ide"] = True
            for flag, label in (
                ("proxy_healed", "proxy_healed"),
                ("nrpt_auto_enabled", "nrpt_enabled"),
                ("hosts_auto_enabled", "hosts_pinned"),
            ):
                if health.get(flag):
                    actions.append(label)
                    _mark_broken_app_restarts(include_all_on_empty=force)
        else:
            connectivity_ok = prev_conn_ok
            diagnosis = str(state.get("diagnosis") or "")

        if net_ready or loc_ready or (force and not (net_ready or loc_ready)):
            if net_ready:
                _mark_cooldown("net_tls")
            if loc_ready:
                _mark_cooldown("loc_400")
            if need_bypass:
                heal2 = _run_silent(_heal_google_connectivity, ctx=None, quiet=True, require_bypass=True) or {}
            else:
                heal2 = _run_silent(_heal_google_connectivity, ctx=None, quiet=True) or {}
            connectivity_ok = bool(heal2.get("connectivity_ok", connectivity_ok))
            if heal2.get("diagnosis"):
                diagnosis = str(heal2["diagnosis"])
            if heal2.get("changed"):
                actions.append("net_tls_healed" if net_ready else ("loc_400_healed" if loc_ready else "connectivity_healed"))
                _mark_broken_app_restarts(include_all_on_empty=True)

        if "sni_host_refused" in fresh_keys and _cooldown_ready("sni_refused", 120):
            _mark_cooldown("sni_refused")
            sni_err = next((k for k in found_fresh if k.key == "sni_host_refused"), None)
            sni_ctx = {"_hits_by_key": {"sni_host_refused": list(sni_err.hits) if sni_err else []}}
            if sni_err and sni_err.is_already_fixed_fn and not sni_err.is_already_fixed_fn(sni_ctx):
                if sni_err.fix_fn:
                    _run_silent(sni_err.fix_fn, sni_ctx)
                actions.append("sni_rotated")
                _mark_broken_app_restarts(include_all_on_empty=True)

        if "ide_session_blocked" in fresh_keys and t_scope in ("all", "ide") and _cooldown_ready("ide_session", 300):
            _mark_cooldown("ide_session")
            sess_err = next((k for k in found_fresh if k.key == "ide_session_blocked"), None)
            if sess_err and sess_err.is_already_fixed_fn and not sess_err.is_already_fixed_fn({}):
                fixed_sess = False
                if sess_err.fix_fn:
                    fixed_sess = bool(_run_silent(sess_err.fix_fn, {}))
                if fixed_sess:
                    actions.append("ide_session_healed")
                    restart_apps["ide"] = True

        # Do NOT force-kill/reset running Antigravity on 429 quota limits from background autopilot.
        if "sqlite_corrupt" in fresh_keys and _cooldown_ready("sqlite", 1800):
            _mark_cooldown("sqlite")
            try:
                from patcher.conversations.rebuilder import rebuild_conversations_index
                _run_silent(rebuild_conversations_index, auto_workspaces=True)
                actions.append("sqlite_repaired")
            except Exception as ex_sql:
                errors.append(f"sqlite: {ex_sql}")

        if allow_restart and any(restart_apps.values()):
            running = _detect_running_antigravity_apps()
            restart_mask = {
                "ide": bool(running.get("ide") and restart_apps.get("ide") and t_scope in ("all", "ide")),
                "manager": bool(running.get("manager") and restart_apps.get("manager") and t_scope in ("all", "manager")),
            }
            if any(restart_mask.values()):
                restarted = _run_silent(
                    _restart_antigravity_apps,
                    restart_mask,
                    main_js_path=main_js_path,
                    manager_path=manager_path,
                ) or []
                if restarted:
                    actions.append(f"restarted:{','.join(restarted)}")

        if actions:
            state["healed_count"] = int(state.get("healed_count", 0) or 0) + 1
            state["last_issue"] = ", ".join(sorted(fresh_keys)) if fresh_keys else "connectivity"
            state["last_actions"] = actions
            state["pending_issues"] = {}
            _append_log(f"autopilot healed ({t_scope}): {actions}")
    except Exception as exc:
        errors.append(str(exc))

    state["last_run_ts"] = now
    state["connectivity_ok"] = bool(connectivity_ok)
    state["diagnosis"] = diagnosis
    state["last_status"] = (
        "healed" if actions else ("warning" if (not connectivity_ok or errors) else "healthy")
    )
    _save_autopilot_state(state)

    return {
        "ok": not errors,
        "status": state["last_status"],
        "auto_heal_enabled": auto_heal_enabled,
        "target_app": t_scope,
        "connectivity_ok": bool(connectivity_ok),
        "diagnosis": diagnosis,
        "actions": actions,
        "errors": errors,
        "last_run_ts": now,
        "healed_count": int(state.get("healed_count", 0) or 0),
    }



