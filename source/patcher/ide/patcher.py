import os
import sys
import re
import mmap
import time
import shutil
from pathlib import Path

from patcher.constants import (
    MIN_AG_VERSION,
)
from patcher.utils.console import (
    info,
    hint,
    ok,
    warn,
    err,
    step,
    print_panel,
)
from patcher.utils.update import handle_patch_failure
from patcher.utils.admin import terminate_processes
from patcher.utils.atomic import write_atomic
from patcher.utils.file import (
    file_hash,
    file_size,
    format_bytes,
    fix_posix_permissions,
    resign_macos_bundle,
    remove_macos_immutable_flags,
    remove_macos_quarantine,
    find_app_bundle,
)
from patcher.ide.discovery import (
    check_ag_version,
    parse_version_safe,
    VersionStatus,
    Version,
)

IDE_RE = re.compile(r"(resetIsTierGCPTos\(\),)this\.[A-Za-z_$0-9]+\.isGoogleInternal")
IDE_DONE = "resetIsTierGCPTos(),true"

TIER_RE = re.compile(r"const\s+s\s*=\s*!![A-Za-z_$0-9]+\|\|![A-Za-z_$0-9]+")
TIER_DONE = "const s=!0"

SET_INEL_RE = re.compile(r'SET_INELIGIBLE:\{target:[\x22\x27]\.?loginError[\x22\x27]')
SET_INEL_DONE = 'SET_INELIGIBLE:{target:".success"'

CATCH_TARGET = 'this._sendAuthErrorEvent(r?.response?.data)'
CATCH_REPLACEMENT = 'this._authActor.send({type:"AUTH_SUCCESS",tokenInfo:t})'


def _ide_cache_dirs():
    """VS Code CachedData / Code Cache dirs to drop after patching main.js, so the IDE
    recompiles the patched bytes instead of replaying a stale compile cache. The user-data
    folder is the product nameLong ('Antigravity IDE') under each OS's app-data root."""
    home = os.path.expanduser("~")
    if os.name == "nt":
        bases = [os.path.expandvars(p) for p in
                 (r"%USERPROFILE%\scoop\persist\antigravity-ide\data\user-data",
                  r"%APPDATA%\Antigravity IDE",
                  r"%LOCALAPPDATA%\Programs\Antigravity IDE")]
    elif sys.platform == "darwin":
        bases = [os.path.join(home, "Library", "Application Support", "Antigravity IDE")]
    else:                                                  # Linux (respect XDG_CONFIG_HOME)
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        bases = [os.path.join(cfg, "Antigravity IDE")]
    dirs = []
    for base in bases:
        dirs += [os.path.join(base, "CachedData"),
                 os.path.join(base, "Code Cache", "js"),
                 os.path.join(base, "Code Cache")]
    return dirs


CONV_TARGET = 'async run(t){t.get(AJ).togglePanelTab?.("customization")}});'
CONV_PATCH = 've(class extends ke{constructor(){super({id:"antigravity.openConversation",title:"Open Conversation",f1:!1})}async run(t,e){t.get(AJ).togglePanelTab?.("conversation",e)}});'
CONV_DONE = 'id:"antigravity.openConversation"'


def apply_patches(content, ag_version=None):
    """Применяет патч isGoogleInternal для IDE из manager.py и команду восстановления диалога."""
    results = []
    already = is_already_patched(content)
    matches = [m.group(0) for m in IDE_RE.finditer(content)]
    new_content = IDE_RE.sub(r"\1true", content)
    applied = new_content != content

    if applied:
        detail = f"replaced {len(matches)} occurrences"
    elif IDE_DONE in content:
        detail = "already patched"
    else:
        detail = "pattern not found"

    results.append({
        "Name": "isGoogleInternal -> true (auth)",
        "Applied": True if (applied or (IDE_DONE in content)) else False,
        "Detail": detail,
    })

    # Дополнительный обход региональной проверки ineligibleTiers
    tier_matches = [m.group(0) for m in TIER_RE.finditer(new_content)]
    if tier_matches:
        new_content = TIER_RE.sub(TIER_DONE, new_content)
        tier_applied = True
        tier_detail = f"replaced {len(tier_matches)} occurrences (s=!0)"
    elif TIER_DONE in new_content:
        tier_applied = True
        tier_detail = "already patched"
    else:
        tier_applied = True
        tier_detail = "not needed for this build"

    results.append({
        "Name": "ineligibleTiers bypass (region guard)",
        "Applied": tier_applied,
        "Detail": tier_detail,
    })

    # 3. antigravity.openConversation command for active dialog restoration
    conv_already = CONV_DONE in new_content
    if not conv_already and CONV_TARGET in new_content:
        new_content = new_content.replace(CONV_TARGET, CONV_TARGET + CONV_PATCH, 1)
        conv_applied = True
        conv_detail = "registered antigravity.openConversation command"
    elif conv_already:
        conv_applied = True
        conv_detail = "already patched"
    elif (ag_version and ag_version >= Version("2.6.0")) or "setVisibleConversation" in new_content:
        conv_applied = True
        conv_detail = "native in v2.6.0+ (no patch needed)"
    else:
        conv_applied = False
        conv_detail = "pattern not found"

    results.append({
        "Name": "antigravity.openConversation (dialog restore)",
        "Applied": conv_applied,
        "Detail": conv_detail,
    })

    
    # 4. State Machine SET_INELIGIBLE -> .success bypass
    inel_matches = list(SET_INEL_RE.finditer(new_content))
    if inel_matches:
        new_content = SET_INEL_RE.sub(SET_INEL_DONE, new_content)
        inel_applied = True
        inel_detail = f"redirected {len(inel_matches)} loginError targets to .success"
    elif SET_INEL_DONE in new_content:
        inel_applied = True
        inel_detail = "already patched"
    else:
        inel_applied = True
        inel_detail = "not found / not needed"

    results.append({
        "Name": "SET_INELIGIBLE state bypass",
        "Applied": inel_applied,
        "Detail": inel_detail,
    })

    # 5. catch error in confirmUserForService -> AUTH_SUCCESS
    if CATCH_TARGET in new_content:
        new_content = new_content.replace(CATCH_TARGET, CATCH_REPLACEMENT, 1)
        catch_applied = True
        catch_detail = "safe AUTH_SUCCESS fallback"
    elif CATCH_REPLACEMENT in new_content:
        catch_applied = True
        catch_detail = "already patched"
    else:
        catch_applied = True
        catch_detail = "not needed for this build"

    results.append({
        "Name": "confirmUserForService error guard",
        "Applied": catch_applied,
        "Detail": catch_detail,
    })

    return new_content, results



def is_already_patched(content):
    if isinstance(content, str) and os.path.isfile(content):
        try:
            with open(content, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return False
    return (IDE_DONE in content or TIER_DONE in content) and not IDE_RE.search(content)


def warn_about_unsafe_backup(main_js_path, installed_version_str=None, current_content=None):
    backup_path = main_js_path + ".bak"
    if not os.path.exists(backup_path):
        return True, False

    backup_size = file_size(backup_path)
    current_size = file_size(main_js_path)
    warnings = []

    if backup_size <= 2048:
        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                backup_content = f.read()
            if len(backup_content.strip()) <= 512:
                warnings.append(
                    f"backup size is only {format_bytes(backup_size)} and it looks almost empty"
                )
        except Exception as e:
            warn(f"Backup check error: {e}")
            return False, False
    elif backup_size < 4096 or (current_size > 0 and backup_size < current_size // 10):
        warnings.append(
            f"backup is much smaller than expected "
            f"({format_bytes(backup_size)} vs {format_bytes(current_size)})"
        )

    if not warnings:
        return True, False

    for warning in warnings:
        warn(f"Backup warning: {warning}")
    warn("Restoring this backup may break Antigravity IDE.")
    warn(f"Backup kept: {os.path.basename(backup_path)}")
    return True, True




def disable_ide_telemetry() -> bool:
    """
    Disable Google/VS Code telemetry and crash reporting in settings.json (Anti-Ban).
    Exact flags matching industry standard anti-tracking:
      telemetry.telemetryLevel: 'off'
      telemetry.enableCrashReporter: false
      telemetry.enableTelemetry: false
    """
    settings_paths = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            settings_paths.append(Path(appdata) / "Antigravity IDE" / "User" / "settings.json")
            settings_paths.append(Path(appdata) / "antigravity" / "User" / "settings.json")
    elif sys.platform == "darwin":
        home = Path.home()
        settings_paths.append(home / "Library" / "Application Support" / "Antigravity IDE" / "User" / "settings.json")
        settings_paths.append(home / "Library" / "Application Support" / "Antigravity" / "User" / "settings.json")
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        settings_paths.append(cfg / "Antigravity IDE" / "User" / "settings.json")
        settings_paths.append(cfg / "antigravity" / "User" / "settings.json")

    success = False
    import json
    for p in settings_paths:
        try:
            if not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data["telemetry.telemetryLevel"] = "off"
            data["telemetry.enableCrashReporter"] = False
            data["telemetry.enableTelemetry"] = False
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            success = True
        except Exception:
            pass
    return success


def _inject_open_conversation_command(wb_content: str) -> tuple[str, bool]:
    """Ensure antigravity.openConversation command is registered in workbench.desktop.main.js."""
    if CONV_DONE in wb_content:
        return wb_content, False
    idx_svc = wb_content.find('id:"antigravity.sendPromptToAgentPanel"')
    idx_cmd = wb_content.find('id:"antigravity.startNewConversation"')
    if idx_svc == -1 or idx_cmd == -1:
        return wb_content, False
    svc_win = wb_content[idx_svc : idx_svc + 240]
    cmd_win_start = max(0, idx_cmd - 80)
    cmd_win = wb_content[cmd_win_start : idx_cmd + 320]
    m_svc = re.search(
        r'id:"antigravity\.sendPromptToAgentPanel"[\s\S]{0,160}?\.get\(([A-Za-z_$0-9]+)\)\.sendMessageToChatPanel',
        svc_win,
    )
    m_cmd = re.search(
        r'([A-Za-z_$0-9]+)\(class extends ([A-Za-z_$0-9]+)\{constructor\(\)\{super\(\{id:"antigravity\.startNewConversation"[\s\S]{0,280}?\}\)\}\}\),',
        cmd_win,
    )
    if m_svc and m_cmd:
        reg_fn, base_cls = m_cmd.group(1), m_cmd.group(2)
        svc_tok = m_svc.group(1)
        patch_snippet = (
            f'{reg_fn}(class extends {base_cls}{{constructor(){{super({{id:"antigravity.openConversation",'
            f'title:"Open Conversation",f1:!1}})}}async run(t,e){{t.get({svc_tok}).togglePanelTab?.("conversation",e)}}}}),'
        )
        anchor = m_cmd.group(0)
        abs_start = cmd_win_start + m_cmd.start()
        return wb_content[:abs_start] + anchor + patch_snippet + wb_content[abs_start + len(anchor) :], True
    return wb_content, False


def patch_workbench_js(main_js_path):
    """Патчит workbench.desktop.main.js для отключения экрана JUd (Sorry, this account is ineligible) и регистрации antigravity.openConversation."""
    wb_path = os.path.normpath(os.path.join(os.path.dirname(main_js_path), "vs", "workbench", "workbench.desktop.main.js"))
    if not os.path.isfile(wb_path):
        return False
    try:
        with open(wb_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return False

    target1 = 'case"signedIn":return v(KEn,'
    target2 = 'case"loginError":return O.verificationUrl||O.ineligibleMessage?v(JUd,'
    target3 = 'guard:({context:t})=>t.errorMessage!==""||t.ineligibleMessage!==""'

    changed = False
    new_content = content
    if target1 in new_content and 'case"loginError":case"signedIn":' not in new_content:
        new_content = new_content.replace(target1, 'case"loginError":case"signedIn":return v(KEn,', 1)
        changed = True
    if target2 in new_content:
        new_content = new_content.replace(target2, 'case"_disabled_loginError":return O.verificationUrl||O.ineligibleMessage?v(JUd,', 1)
        changed = True
    if target3 in new_content:
        new_content = new_content.replace(target3, 'guard:({context:t})=>t.errorMessage!==""&&!1', 1)
        changed = True
    new_content, conv_injected = _inject_open_conversation_command(new_content)
    if conv_injected:
        changed = True

    if changed:
        bak = wb_path + ".bak"
        if not os.path.exists(bak):
            try:
                shutil.copy2(wb_path, bak)
            except Exception:
                pass
        try:
            write_atomic(wb_path, new_content, kill_on_lock=False)
            if conv_injected:
                step("workbench openConversation command registered", True, "conversation restore enabled")
            else:
                step("workbench UI ineligibility screen bypassed", True, "JUd screen bypassed")
            return True
        except Exception as ex:
            warn(f"Could not patch workbench.desktop.main.js: {ex}")
            return False
    elif 'case"loginError":case"signedIn":' in content or CONV_DONE in content:
        step("workbench UI patches", True, "already patched")
        return True
    return False


def restore_workbench_js(main_js_path):
    wb_path = os.path.normpath(os.path.join(os.path.dirname(main_js_path), "vs", "workbench", "workbench.desktop.main.js"))
    bak = wb_path + ".bak"
    if os.path.isfile(bak):
        try:
            shutil.copy2(bak, wb_path)
            os.remove(bak)
            ok("Restored workbench.desktop.main.js from backup.")
            return True
        except Exception as ex:
            warn(f"Could not restore workbench.desktop.main.js: {ex}")
    return False


def find_ide_internal_language_server(main_js_path: str):
    """Находит бинарник language_server внутри расширения Antigravity IDE."""
    if not main_js_path:
        return None
    try:
        p = Path(main_js_path).resolve()
        for _ in range(5):
            for sub_name in ("agy", "antigravity"):
                ext_bin = p / "extensions" / sub_name / "bin"
                if ext_bin.is_dir():
                    for f in ext_bin.glob("language_server*"):
                        if f.is_file() and not f.name.endswith((".agybak", ".bak", ".old", ".agtmp")):
                            return str(f)
                    for f in ext_bin.glob("agy*"):
                        if f.is_file() and not f.name.endswith((".agybak", ".bak", ".old", ".agtmp")):
                            return str(f)
            # Также проверяем общую папку extensions
            exts_dir = p / "extensions"
            if exts_dir.is_dir():
                for sub in exts_dir.iterdir():
                    if sub.is_dir() and ("agy" in sub.name.lower() or "antigravity" in sub.name.lower()):
                        for b_cand in (sub / "bin", sub):
                            if b_cand.is_dir():
                                for f in b_cand.glob("language_server*"):
                                    if f.is_file() and not f.name.endswith((".agybak", ".bak", ".old", ".agtmp")):
                                        return str(f)
            p = p.parent
    except Exception:
        pass
    return None


def patch_binary_backend(bin_path: str, bin_label: str = "Language Server") -> bool:
    """Патчит Go-бинарник (language_server или agy): гейт авторизации + замена строки ineligible -> inexigible."""
    if not bin_path or not os.path.isfile(bin_path):
        return False
    try:
        from patcher.manager.patcher import MANAGER_GATE, _make_backup
        from patcher.agy.patcher import ALL_GATES as AGY_GATES
        from patcher.utils.file import fix_posix_permissions, resign_macos_binary, get_executable_ranges

        ranges = get_executable_ranges(bin_path)
        pending_writes: list[tuple[int, bytes, str]] = []
        status_steps: list[tuple[str, object, str]] = []

        if os.path.getsize(bin_path) == 0:
            return False

        # Fast read-only mmap scan (avoids copying 232 MB into RAM when already patched)
        with open(bin_path, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                try:
                    kind, off, gate = MANAGER_GATE.resolve(mm, ranges)
                    if kind == "unpatched":
                        pending_writes.append((off, gate.fix, gate.desc))
                        status_steps.append((f"{bin_label} auth gate", True, gate.desc))
                    else:
                        status_steps.append((f"{bin_label} auth gate", None, "already patched"))
                except LookupError:
                    pass

                for g_obj, g_label in AGY_GATES:
                    try:
                        for kind, offsets, g in g_obj.resolve_all(mm, ranges):
                            if kind == "unpatched":
                                for off in offsets:
                                    pending_writes.append((off, g.fix, g.desc))
                                status_steps.append((f"{bin_label} {g_label}", True, g.desc))
                            else:
                                status_steps.append((f"{bin_label} {g_label}", None, f"{g.desc} (already patched)"))
                    except Exception:
                        pass

                has_ineligible = mm.find(b"ineligible") != -1
                has_inexigible = False if has_ineligible else (mm.find(b"inexigible") != -1)

        if not pending_writes and not has_ineligible:
            for lbl, state, desc in status_steps:
                step(lbl, state, desc)
            if has_inexigible:
                step(f"{bin_label} region guard", None, "already bypassed")
            return True

        with open(bin_path, "rb") as f:
            bdata = bytearray(f.read())

        for off, fix, _desc in pending_writes:
            bdata[off:off + len(fix)] = fix

        inel_count = 0
        if has_ineligible:
            inel_count = bdata.count(b"ineligible")
            if inel_count > 0:
                bdata = bdata.replace(b"ineligible", b"inexigible")

        for lbl, state, desc in status_steps:
            step(lbl, state, desc)
        if inel_count > 0:
            step(f"{bin_label} region guard", True, f"bypassed {inel_count} ineligible check(s)")
        elif has_inexigible:
            step(f"{bin_label} region guard", None, "already bypassed")

        # 3. Бэкап
        try:
            _make_backup(bin_path)
        except Exception:
            pass

        # 4. Атомарная запись через .agtmp
        write_atomic(bin_path, bdata, kill_on_lock=True)

        fix_posix_permissions(bin_path)
        resign_macos_binary(bin_path)
        ok(f"{bin_label} ({os.path.basename(bin_path)}) successfully patched!")
        return True
    except Exception as ex:
        warn(f"Could not patch {bin_label} ({bin_path}): {ex}")
        return False


def fix_product_checksums(main_js_path: str):
    """Remove product.json checksums to permanently disable the
    'Your Antigravity IDE installation appears to be corrupt' warning banner."""
    import json
    p = Path(main_js_path).resolve()
    app_dir = None
    for parent in [p.parent, p.parent.parent, p.parent.parent.parent, p.parent.parent.parent.parent]:
        candidate = parent / "product.json"
        if candidate.is_file():
            app_dir = parent
            break
    if not app_dir:
        return
    pjson_path = app_dir / "product.json"
    try:
        data = json.loads(pjson_path.read_text(encoding="utf-8"))
        if "checksums" in data:
            del data["checksums"]
            pjson_path.write_text(json.dumps(data, indent="\t", ensure_ascii=False), encoding="utf-8")
            ok("Product checksums removed (corrupt installation banner permanently disabled).")
    except Exception as e:
        warn(f"Could not synchronize product checksums: {e}")


def ensure_agy_backend_patched(wait_seconds: float = 3.0) -> bool:
    """
    Проверяет и пропатчивает бинарник agy (Agent Engine) после запуска IDE.
    На чистой установке IDE скачивает agy.exe в ~/.gemini/bin/ только во время первого запуска.
    Если бинарь появляется или уже существует, автоматически накладывает патчи гейтов и региона.
    """
    from patcher.agy.discovery import find_agy_binary
    import time

    agy_bin = find_agy_binary()
    if agy_bin and os.path.isfile(agy_bin):
        return patch_binary_backend(agy_bin, "Agent Engine (agy)")

    start = time.time()
    while time.time() - start < wait_seconds:
        time.sleep(0.5)
        agy_bin = find_agy_binary()
        if agy_bin and os.path.isfile(agy_bin):
            info(f"Detected newly downloaded agent backend: {os.path.basename(agy_bin)}")
            return patch_binary_backend(agy_bin, "Agent Engine (agy)")

    return False


def do_patch(main_js_path, show_search_line=False) -> bool:
    from patcher.cli import confirmed
    from patcher.utils.captcha import confirm_with_captcha

    if not main_js_path or not os.path.isfile(main_js_path):
        from patcher.cli import offer_download_and_block
        offer_download_and_block("Antigravity IDE")
        return False

    ver_status, ver_str = check_ag_version(main_js_path)
    parsed_version = parse_version_safe(ver_str)

    if ver_status == VersionStatus.TOO_OLD:
        err(f"Unsupported version: {ver_str}")
        err(f"Minimum required: {MIN_AG_VERSION}")
        hint("Proceeding anyway...")
    elif ver_status == VersionStatus.NOT_FOUND:
        warn("Could not detect Antigravity IDE version (registry key not found).")
    elif ver_status == VersionStatus.PARSE_ERROR:
        warn(f"Could not parse version string: {ver_str}")
    # VersionStatus.OK — продолжаем без вопросов

    try:
        with open(main_js_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        err(f"Read error: {e}")
        return False

    from patcher.utils.admin import is_process_running
    proc_names = ["Antigravity IDE", "antigravity-ide"]
    if is_process_running(proc_names):
        info("Closing running Antigravity IDE processes to apply patches cleanly...")
        terminate_processes(proc_names)
        time.sleep(1.5)

    current_is_patched = is_already_patched(content)

    if current_is_patched:
        info("File appears already patched. Re-applying main.js patches...")

    # --- БЭКАП ---
    backup_path = main_js_path + ".bak"

    if not os.path.exists(backup_path) and not current_is_patched:
        info("Creating backup...")
        if sys.platform == "darwin":
            app_path = find_app_bundle(main_js_path)
            if app_path:
                remove_macos_immutable_flags(app_path)
                remove_macos_quarantine(app_path)
        try:
            shutil.copy2(main_js_path, backup_path)
            fix_posix_permissions(backup_path)
            ok(f"Backup: {os.path.basename(backup_path)} "
               f"({format_bytes(file_size(backup_path))})")
        except PermissionError as e:
            if sys.platform == "darwin":
                warn(f"Permission denied, retrying after removing flags...")
                app_path = find_app_bundle(main_js_path)
                if app_path:
                    remove_macos_immutable_flags(app_path)
                    remove_macos_quarantine(app_path)
                try:
                    shutil.copy2(main_js_path, backup_path)
                    fix_posix_permissions(backup_path)
                    ok(f"Backup: {os.path.basename(backup_path)} "
                       f"({format_bytes(file_size(backup_path))})")
                except Exception as e2:
                    err(f"Backup error: {e2}")
                    return False
            else:
                err(f"Backup error: {e}")
                return False
        except Exception as e:
            err(f"Backup error: {e}")
            return False
    elif os.path.exists(backup_path):
        hint("Backup already exists — skipping")
    elif current_is_patched:
        warn("main.js is already patched — no backup needed")

    try:
        restore_workbench_js(main_js_path)
    except Exception:
        pass

    hash_before = file_hash(main_js_path)

    info("Applying patches...")
    print()

    new_content, results = apply_patches(content, ag_version=parsed_version)

    applied = 0
    for r in results:
        if r.get("Applied"):
            applied += 1
        step(r["Name"], r.get("Applied", False), r.get("Detail", ""))
    print()

    if new_content == content and current_is_patched:
        # Патчим UI экраны (включая обход экрана Sorry, this account is ineligible)
        try:
            patch_workbench_js(main_js_path)
        except Exception:
            pass
        # Синхронизация контрольных сумм в product.json
        try:
            fix_product_checksums(main_js_path)
        except Exception:
            pass
        # Автоматически проверяем и применяем TLS fix + VPN proxy fix "из коробки"
        try:
            from patcher.tls_fix import is_tls_fix_applied, do_tls_fix_inject
            if not is_tls_fix_applied(main_js_path):
                do_tls_fix_inject(main_js_path)
        except Exception:
            pass
        # Проверяем и патчим внутренний language_server IDE и agy.exe
        try:
            ide_ls = find_ide_internal_language_server(main_js_path)
            if ide_ls:
                patch_binary_backend(ide_ls, "IDE Language Server")
            from patcher.agy.discovery import find_agy_binary
            agy_bin = find_agy_binary()
            if agy_bin:
                patch_binary_backend(agy_bin, "Agent Engine (agy)")
        except Exception:
            pass
        # Отключение внешней телеметрии Google/MS (Anti-Ban)
        try:
            disable_ide_telemetry()
        except Exception:
            pass
        ok("File is already patched — no changes needed.")
        return True

    if applied == 0:
        err("No patches applied.")
        handle_patch_failure()
        return False

    write_success = False
    for attempt in range(2):
        try:
            if sys.platform == "darwin":
                app_path = find_app_bundle(main_js_path)
                if app_path:
                    remove_macos_immutable_flags(app_path)
                    remove_macos_quarantine(app_path)
            write_atomic(main_js_path, new_content, kill_on_lock=True)
            fix_posix_permissions(main_js_path)
            write_success = True
            break
        except PermissionError as e:
            if attempt == 0:
                if sys.platform == "darwin":
                    warn(f"Permission denied, retrying after removing flags...")
                    time.sleep(0.5)
                    continue
                warn(f"Permission denied (file locked): {e}. Closing processes and retrying...")
                terminate_processes(["Antigravity IDE", "antigravity-ide"])
                time.sleep(1.5)
                continue
            err(f"Write error (Permission denied): {e}")
            handle_patch_failure()
            return False
        except Exception as e:
            err(f"Write error: {e}")
            handle_patch_failure()
            return False

    if not write_success:
        handle_patch_failure()
        return False

    # Очистка кэша скомпилированных JS файлов VS Code (CachedData / Code Cache)
    info("Clearing IDE compile caches...")
    for c in _ide_cache_dirs():
        try:
            if os.path.isdir(c):
                shutil.rmtree(c, ignore_errors=True)
        except Exception:
            pass

    hash_after = file_hash(main_js_path)
    resign_macos_bundle(main_js_path)

    # Патчим UI экраны (включая обход экрана Sorry, this account is ineligible)
    try:
        patch_workbench_js(main_js_path)
    except Exception as ex:
        warn(f"Workbench patch warning: {ex}")

    # Автоматически проверяем и патчим внутренний language_server IDE
    try:
        ide_ls = find_ide_internal_language_server(main_js_path)
        if ide_ls:
            info(f"Checking IDE internal language server: {os.path.basename(ide_ls)}...")
            patch_binary_backend(ide_ls, "IDE Language Server")
    except Exception as ex:
        warn(f"Auto-patch IDE language server warning: {ex}")

    # Автоматически проверяем и патчим бинарник agy (движок агента), если найден
    try:
        from patcher.agy.discovery import find_agy_binary
        agy_bin = find_agy_binary()
        if agy_bin:
            info(f"Checking agent engine backend: {os.path.basename(agy_bin)}...")
            patch_binary_backend(agy_bin, "Agent Engine (agy)")
    except Exception as ex:
        warn(f"Auto-patch agy warning: {ex}")

    # Автоматически внедряем TLS fix + VPN proxy fix "из коробки"
    try:
        from patcher.tls_fix import is_tls_fix_applied, do_tls_fix_inject
        if not is_tls_fix_applied(main_js_path):
            do_tls_fix_inject(main_js_path)
    except Exception as ex:
        warn(f"TLS fix auto-inject warning: {ex}")

    # Синхронизация контрольных сумм в product.json (убирает баннер 'installation appears to be corrupt')
    try:
        fix_product_checksums(main_js_path)
    except Exception:
        pass

    # Отключение внешней телеметрии Google/MS (Anti-Ban & Privacy)
    try:
        disable_ide_telemetry()
    except Exception:
        pass

    panel_rows = [
        ("Target", os.path.basename(main_js_path)),
        ("Patches", f"{applied}/{len(results)} applied"),
    ]
    if os.path.exists(backup_path):
        panel_rows.append(
            ("Backup", f"{os.path.basename(backup_path)} ({format_bytes(file_size(backup_path))})")
        )
    if hash_before and hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    panel_rows.append(("Done", time.strftime('%H:%M:%S')))
    print_panel("PATCH COMPLETE", panel_rows)
    hint("Restart Antigravity IDE and sign in.")
    return True


def do_restore(main_js_path, show_search_line=False, force=False):
    from patcher.cli import confirmed

    current_content = None
    try:
        with open(main_js_path, "r", encoding="utf-8") as f:
            current_content = f.read()
    except Exception:
        pass

    backup_ok, backup_has_warnings = warn_about_unsafe_backup(
        main_js_path, current_content=current_content
    )
    if not backup_ok and not force:
        return

    backup_path = main_js_path + ".bak"

    if not os.path.exists(backup_path):
        err(f"Backup file not found: {backup_path}")
        return
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            data = f.read()
    except Exception as e:
        err(f"Could not read backup: {e}")
        return

    backup_size = file_size(backup_path)
    if backup_size <= 2048:
        err("Backup looks too small — may be corrupted!")
        if not force:
            return

    if is_already_patched(data):
        warn("Backup itself appears to be patched!")

    hash_before = file_hash(main_js_path)

    try:
        if sys.platform == "darwin":
            app_path = find_app_bundle(main_js_path)
            if app_path:
                remove_macos_immutable_flags(app_path)
                remove_macos_quarantine(app_path)
        write_atomic(main_js_path, data, meta_source=backup_path, kill_on_lock=True)
        fix_posix_permissions(main_js_path)
    except Exception as e:
        err(f"Restore error: {e}")
        return

    hash_after = file_hash(main_js_path)
    resign_macos_bundle(main_js_path)

    # Очищаем также бэкап TLS-фикса, если был
    tls_bak = main_js_path + ".js.tlsbak"
    if os.path.exists(tls_bak):
        try:
            os.remove(tls_bak)
        except Exception:
            pass

    try:
        restore_workbench_js(main_js_path)
    except Exception:
        pass

    try:
        ide_ls = find_ide_internal_language_server(main_js_path)
        if ide_ls and os.path.exists(ide_ls + ".agybak"):
            shutil.copy2(ide_ls + ".agybak", ide_ls)
            os.remove(ide_ls + ".agybak")
            ok(f"Restored {os.path.basename(ide_ls)} from backup.")
    except Exception:
        pass

    try:
        fix_product_checksums(main_js_path)
    except Exception:
        pass

    panel_rows = [("Target", os.path.basename(main_js_path))]
    if hash_before and hash_after and hash_before != hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    panel_rows.append(("Done", time.strftime('%H:%M:%S')))
    print_panel("RESTORE COMPLETE", panel_rows)
