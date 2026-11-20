import os
import re
import sys
import errno
import mmap
import shutil
import contextlib
import filecmp
from enum import Enum
from patcher.ide.discovery import Version

from patcher.constants import COLOR_CYAN, MIN_ANTIGRAVITY_VERSION
from patcher.utils.console import (
    color,
    info,
    hint,
    ok,
    warn,
    err,
    step,
    print_panel,
)
from patcher.utils.file import (
    file_hash,
    file_size,
    format_bytes,
    fix_posix_permissions,
    resign_macos_bundle,
    resign_macos_binary,
    ensure_macos_writable,
    get_executable_ranges,
)
from patcher.utils.update import handle_patch_failure
from patcher.utils.admin import terminate_standalone_processes
from patcher.utils.atomic import write_atomic, atomic_replace_posix, apply_patches_to_data

BAK_EXT = ".agybak"


class VersionStatus(Enum):
    OK = "ok"
    TOO_OLD = "too_old"
    NOT_FOUND = "not_found"
    PARSE_ERROR = "parse_error"


class Gate:
    def __init__(self, sig, patched, fix, offset=0, desc=""):
        self.sig = re.compile(sig, re.S)
        self.patched = re.compile(patched, re.S)
        self.fix = fix
        self.offset = offset
        self.desc = desc

    def find(self, data, ranges=None):
        search_ranges = ranges or ((0, len(data)),)
        for start, end in search_ranges:
            m = self.patched.search(data, start, end)
            if m:
                return ("patched", m.start() + self.offset)
        matches = []
        for start, end in search_ranges:
            for m in self.sig.finditer(data, start, end):
                matches.append(m.start() + self.offset)
        if not matches:
            raise LookupError("gate signature not found (unsupported version?)")
        if len(matches) > 1:
            raise LookupError("gate signature is not unique — refusing to guess")
        return ("unpatched", matches[0])

    def resolve(self, data, ranges=None):
        """(kind, write-offset, concrete-gate). The concrete gate carries the fix bytes
        and label to apply — so a MultiGate can hand back the arch-matching sub-gate."""
        kind, off = self.find(data, ranges)
        return kind, off, self


class MultiGate:
    """One logical gate whose machine code differs per CPU arch (the Manager's auth check
    compiles to distinct amd64 vs arm64 instructions), so it declares one Gate signature
    per arch. A given binary matches exactly one — different archs share no byte pattern —
    so there's no ambiguity; the first that finds a match wins."""

    def __init__(self, *gates, desc=""):
        self.gates = gates
        self.desc = desc

    def resolve(self, data, ranges=None):
        err = None
        for g in self.gates:
            try:
                return g.resolve(data, ranges)
            except LookupError as e:
                err = e
        raise err or LookupError("no gate signature matched")


# language_server.exe / language_server (Go binary) auth validator gate:
# cmp byte[rax+8],0 ; je short  ->  mov byte[rax+8],1 ; nop*2 (x64)
# ldrb w3,[x0,#8] ; tbz w3,#0,skip  ->  mov w3,#1 ; strb w3,[x0,#8] (arm64)
# (hasValidAuth=true)
# (wildcarded/re.S displacements)
MANAGER_GATE_X64 = Gate(
    rb"\x80\x78\x08\x00\x74.\x48\x8b.\x24.\x48\x89.\x60",
    rb"\xc6\x40\x08\x01\x90\x90\x48\x8b.\x24.\x48\x89.\x60",
    b"\xc6\x40\x08\x01\x90\x90",
    offset=0,
    desc="hasValidAuth=true (x64)",
)

# arm64: force hasValidAuth and remove the token-attachment branch.
#   ldrb w3,[x0,#8] ; tbz w3,#0,skip  ->  mov w3,#1 ; strb w3,[x0,#8]
# The branch displacement spans the low byte, so accept every encoding that keeps
# Rt=w3. Builds use either one or two setup instructions before the token stp.
_ARM64_TBZ_W3_BIT0 = rb"[\x03\x23\x43\x63\x83\xa3\xc3\xe3]..\x36"
_ARM64_TOKEN_SETUP = rb"(?:....){1,2}\x03\x10\x06\xa9"
MANAGER_GATE_ARM64 = Gate(
    rb"\x03\x20\x40\x39" + _ARM64_TBZ_W3_BIT0 + _ARM64_TOKEN_SETUP,
    rb"\x23\x00\x80\x52\x03\x20\x00\x39" + _ARM64_TOKEN_SETUP,
    b"\x23\x00\x80\x52\x03\x20\x00\x39",
    offset=0,
    desc="hasValidAuth=true (arm64)",
)

MANAGER_GATE = MultiGate(MANAGER_GATE_X64, MANAGER_GATE_ARM64, desc="hasValidAuth=true")


from patcher.manager.discovery import find_asar_relative_to_manager, read_package_json_from_asar
from patcher.manager.asar_patcher import patch_desktop_asar, restore_desktop_asar, is_asar_ui_patched

_LS_FILE_TOKEN_ORIG = b"jetski-standalone-oauth-token"
_LS_FILE_TOKEN_ISO = b"jetski-desktopapp-oauth-token"
_LS_KEYRING_SVC_ORIG = b"gemini"
_LS_KEYRING_SVC_ISO = b"jetski"


def _parse_pe64_sections(data):
    """Parse PE32+ section table into [(name, va, vsz, rp, rsz), ...], or None if not PE32+."""
    import struct
    if len(data) < 0x100 or data[:2] != b"MZ":
        return None
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != b"PE\x00\x00":
        return None
    num_sec = struct.unpack_from("<H", data, pe_off + 6)[0]
    opt_sz = struct.unpack_from("<H", data, pe_off + 20)[0]
    opt_off = pe_off + 24
    if opt_off + 2 > len(data):
        return None
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic != 0x20B:
        return None
    sec_off = opt_off + opt_sz
    if sec_off + num_sec * 40 > len(data):
        return None
    secs = []
    for i in range(num_sec):
        off = sec_off + i * 40
        name = data[off:off + 8].rstrip(b"\x00").decode("ascii", errors="ignore")
        vsz, va, rsz, rp = struct.unpack_from("<IIII", data, off + 8)
        secs.append((name, va, vsz, rp, rsz))
    return secs


def _rva_to_raw(secs, rva):
    for _, va, vsz, rp, rsz in secs:
        span = max(vsz, rsz)
        if va <= rva < va + span:
            raw = rp + (rva - va)
            if raw < rp + rsz:
                return raw
            return None
    return None


def _raw_to_rva(secs, raw):
    for _, va, vsz, rp, rsz in secs:
        if rp <= raw < rp + rsz:
            return va + (raw - rp)
    return None


def _find_pe_keyring_sites(data, secs, svc_bytes):
    """Locate KeyringTokenStorage amd64 LEA rbx,[rip+disp32] sites passing svc_bytes."""
    import struct
    text_sec = next((s for s in secs if s[0] == ".text"), None)
    rdata_sec = next((s for s in secs if s[0] == ".rdata"), None)
    if not text_sec or not rdata_sec:
        return []
    _, _, _, text_rp, text_rsz = text_sec
    _, _, rdata_vsz, rdata_rp, rdata_rsz = rdata_sec
    rdata_end = rdata_rp + min(rdata_vsz, rdata_rsz)
    text_end = text_rp + text_rsz

    needle = b"\x48\x8d\x1d"
    mov_ecx_len = b"\xb9" + struct.pack("<I", len(svc_bytes))
    sites = []
    pos = text_rp
    while True:
        idx = data.find(needle, pos, text_end - 7)
        if idx == -1:
            break
        pos = idx + 1
        win_start = max(text_rp, idx - 32)
        win_end = min(text_end, idx + 24)
        if data.find(mov_ecx_len, win_start, win_end) == -1:
            continue
        call_end = min(text_end, idx + 24)
        if data.find(b"\xff\xd2", idx + 7, call_end) == -1:
            continue
        disp = struct.unpack_from("<i", data, idx + 3)[0]
        src_rva = _raw_to_rva(secs, idx + 7)
        if src_rva is None:
            continue
        traw = _rva_to_raw(secs, src_rva + disp)
        if traw is None or not (rdata_rp <= traw <= rdata_end - len(svc_bytes)):
            continue
        if data[traw:traw + len(svc_bytes)] == svc_bytes:
            sites.append(idx)
    return sites


def is_ls_isolated(data):
    """Return True if language_server uses isolated desktopapp token file and jetski WinCred service."""
    if data.find(_LS_FILE_TOKEN_ORIG) != -1 or data.find(_LS_FILE_TOKEN_ISO) == -1:
        return False
    secs = _parse_pe64_sections(data)
    if secs:
        if _find_pe_keyring_sites(data, secs, _LS_KEYRING_SVC_ORIG):
            return False
    return True


def apply_ls_isolation_to_data(bdata):
    """Isolate standalone language_server token file and WinCred service from Antigravity IDE."""
    import struct
    if bdata.find(_LS_FILE_TOKEN_ORIG) != -1:
        bdata = bdata.replace(_LS_FILE_TOKEN_ORIG, _LS_FILE_TOKEN_ISO)
    secs = _parse_pe64_sections(bdata)
    if secs:
        rdata_sec = next((s for s in secs if s[0] == ".rdata"), None)
        gem_hits = _find_pe_keyring_sites(bdata, secs, _LS_KEYRING_SVC_ORIG)
        if rdata_sec and gem_hits:
            _, _, rdata_vsz, rdata_rp, rdata_rsz = rdata_sec
            rdata_end = rdata_rp + min(rdata_vsz, rdata_rsz)
            iso_raw = bdata.find(_LS_KEYRING_SVC_ISO, rdata_rp, rdata_end)
            if iso_raw != -1:
                iso_rva = _raw_to_rva(secs, iso_raw)
                if iso_rva is not None:
                    for idx in gem_hits:
                        src_rva = _raw_to_rva(secs, idx + 7)
                        if src_rva is not None:
                            struct.pack_into("<i", bdata, idx + 3, iso_rva - src_rva)
    return bdata


def _migrate_wincred_target_if_missing(src_target, dst_target):
    if sys.platform != "win32" or os.environ.get("AG_SKIP_WINCRED"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        PCREDENTIAL = ctypes.POINTER(CREDENTIAL)
        advapi32 = ctypes.windll.advapi32
        CredReadW = advapi32.CredReadW
        CredReadW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIAL)]
        CredReadW.restype = wintypes.BOOL
        CredWriteW = advapi32.CredWriteW
        CredWriteW.argtypes = [PCREDENTIAL, wintypes.DWORD]
        CredWriteW.restype = wintypes.BOOL
        CredFree = advapi32.CredFree
        CredFree.argtypes = [ctypes.c_void_p]
        CredFree.restype = None

        dst_ptr = PCREDENTIAL()
        if CredReadW(dst_target, 1, 0, ctypes.byref(dst_ptr)):
            CredFree(dst_ptr)
            return True

        src_ptr = PCREDENTIAL()
        if not CredReadW(src_target, 1, 0, ctypes.byref(src_ptr)):
            return False

        try:
            src_cred = src_ptr.contents
            sz = int(src_cred.CredentialBlobSize)
            if sz <= 0 or not src_cred.CredentialBlob:
                return False
            raw_bytes = ctypes.string_at(src_cred.CredentialBlob, sz)
            blob_buf = (ctypes.c_byte * len(raw_bytes)).from_buffer_copy(raw_bytes)

            new_cred = CREDENTIAL()
            new_cred.Flags = 0
            new_cred.Type = src_cred.Type or 1
            new_cred.TargetName = dst_target
            new_cred.Comment = None
            new_cred.CredentialBlobSize = len(raw_bytes)
            new_cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_byte))
            new_cred.Persist = src_cred.Persist or 2
            new_cred.AttributeCount = 0
            new_cred.Attributes = None
            new_cred.TargetAlias = None
            new_cred.UserName = src_cred.UserName or "antigravity"
            return bool(CredWriteW(ctypes.byref(new_cred), 0))
        finally:
            CredFree(src_ptr)
    except Exception:
        return False


def _seed_standalone_token_if_missing():
    """Seed isolated standalone token file and WinCred target once without overwriting existing state."""
    try:
        gemini_dir = os.environ.get("AG_GEMINI_DIR") or os.path.join(os.path.expanduser("~"), ".gemini")
        tools_dir = os.environ.get("AG_TOOLS_DIR") or os.path.join(os.path.expanduser("~"), ".antigravity_tools")
        iso_tok = os.path.join(gemini_dir, _LS_FILE_TOKEN_ISO.decode("ascii"))
        orig_tok = os.path.join(gemini_dir, _LS_FILE_TOKEN_ORIG.decode("ascii"))
        accounts_file = os.path.join(tools_dir, "accounts.json")

        standalone_configured = os.path.exists(iso_tok)
        if not standalone_configured and os.path.isfile(accounts_file):
            try:
                import json
                with open(accounts_file, "r", encoding="utf-8") as af:
                    adata = json.load(af)
                if adata.get("current_standalone_account_id"):
                    standalone_configured = True
            except Exception:
                pass

        if not os.path.exists(iso_tok) and not standalone_configured and os.path.isfile(orig_tok):
            os.makedirs(gemini_dir, exist_ok=True)
            shutil.copy2(orig_tok, iso_tok)

        _migrate_wincred_target_if_missing("gemini:antigravity", "jetski:antigravity")
    except Exception:
        pass

def check_antigravity_version(asar_path):
    ver_str = read_package_json_from_asar(asar_path)
    if ver_str is None:
        return VersionStatus.NOT_FOUND, None

    try:
        detected = Version(ver_str)
        minimum = Version(MIN_ANTIGRAVITY_VERSION)
        status = VersionStatus.OK if detected >= minimum else VersionStatus.TOO_OLD
        return status, ver_str
    except Exception:
        return VersionStatus.PARSE_ERROR, ver_str


@contextlib.contextmanager
def _scan_data(f):
    # Reading a modified signed Mach-O through mmap can cause an uncatchable
    # CODESIGNING SIGKILL on macOS, including during post-write verification.
    if sys.platform == "darwin":
        f.seek(0)
        yield f.read()
        return
    if os.fstat(f.fileno()).st_size == 0:
        yield b""
        return
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        yield mm
    finally:
        mm.close()


@contextlib.contextmanager
def _mapped(path):
    """Read-only bytes-view для сканирования сигнатур.

    На macOS не маппит страницы подписанного файла (SIGKILL-риск),
    а читает через _scan_data; на остальных ОС — zero-copy mmap."""
    with open(path, "rb") as f:
        with _scan_data(f) as data:
            yield data


def is_locked(path):
    """
    True, если файл занят (приложение запущено).

    На POSIX замена файла через os.replace возможна даже при работающем процессе,
    поэтому блокирующим считаем только Windows.

    Примечание: на POSIX это не гарантирует, что файл не используется.
    Если нужна проверка на занятость, требуется более сложная логика
    (например, проверка процессов или файловые блокировки).
    """
    if os.name != "nt":
        return False

    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


def get_status(path):
    if not path or not os.path.isfile(path):
        return ("unknown", None)
    ranges = get_executable_ranges(path)
    try:
        with _mapped(path) as d:
            try:
                state, off, g = MANAGER_GATE.resolve(d, ranges)
                return (state, g)
            except LookupError:
                return ("unknown", None)
    except OSError:
        return ("unknown", None)


def is_already_patched(path):
    if get_status(path)[0] != "patched":
        return False
    try:
        with _mapped(path) as d:
            if not is_ls_isolated(d):
                return False
    except OSError:
        return False
    asar_path = find_asar_relative_to_manager(path)
    if asar_path and os.path.isfile(asar_path) and not is_asar_ui_patched(path):
        return False
    return True


def _make_backup(path, preserve_existing_if_patched=False):
    bak = path + BAK_EXT
    if os.path.exists(bak):
        if preserve_existing_if_patched:
            return
        if filecmp.cmp(path, bak, shallow=False):
            return
        info(f"Backup is stale (app updated) — refreshing {os.path.basename(path)}{BAK_EXT}")
    else:
        info(f"Creating backup -> {os.path.basename(path)}{BAK_EXT}")
    if sys.platform == "darwin":
        # Файлы внутри .app часто несут uchg-флаг: даже root получает
        # Errno 1 Operation not permitted при copy2 без предварительного chflags.
        ensure_macos_writable(path)
    try:
        shutil.copy2(path, bak)
    except (PermissionError, OSError) as e:
        # EPERM (errno 1) на macOS — почти всегда immutable-флаг/владелец.
        # Одна повторная попытка после снятия флагов, затем проброс наружу,
        # где do_patch_manager покажет понятную подсказку вместо traceback.
        need_retry = (
            sys.platform == "darwin"
            and isinstance(e, OSError)
            and e.errno in (errno.EPERM, errno.EACCES)
        )
        if need_retry:
            warn("Backup blocked by macOS flags, retrying after removing flags...")
            ensure_macos_writable(path)
            shutil.copy2(path, bak)
        else:
            raise
    fix_posix_permissions(bak)
    ok(f"Backup: {os.path.basename(bak)} ({format_bytes(file_size(bak))})")


def do_patch_manager(path, kill_on_lock=True):
    from patcher.cli import confirmed
    from patcher.utils.captcha import confirm_with_captcha

    if not path or not os.path.isfile(path):
        from patcher.cli import offer_download_and_block
        offer_download_and_block("Antigravity 2.0")
        return

    hash_before = file_hash(path)
    info(f"Target: {color(path, COLOR_CYAN)}")
    hint(f"Size: {color(format_bytes(file_size(path)), COLOR_CYAN)}")

    # Проверка версии Antigravity (считывается из package.json в app.asar)
    asar_path = find_asar_relative_to_manager(path)
    if asar_path:
        ver_status, ver_str = check_antigravity_version(asar_path)
        if ver_status == VersionStatus.TOO_OLD:
            err(f"Unsupported version: {ver_str}")
            err(f"Minimum required: {MIN_ANTIGRAVITY_VERSION}")
            hint("Proceeding anyway...")
        elif ver_status == VersionStatus.NOT_FOUND:
            warn("Could not detect Antigravity version (package.json not found in ASAR).")
        elif ver_status == VersionStatus.PARSE_ERROR:
            warn(f"Could not parse version string: {ver_str}")
    print()

    write_success = False
    off = 0
    for attempt in range(2):
        if is_locked(path):
            if attempt == 0 and kill_on_lock:
                warn("Binary is locked (Antigravity Manager is running). Closing processes and retrying...")
                terminate_standalone_processes()
                import time
                time.sleep(1.5)
                continue
            err("File is locked — close the app first.")
            return

        matched_gate = None
        needs_ls = False
        ranges = get_executable_ranges(path)
        try:
            with _mapped(path) as d:
                try:
                    kind, off, matched_gate = MANAGER_GATE.resolve(d, ranges)
                except LookupError as e:
                    err(f"{e}")
                    handle_patch_failure()
                    return
                needs_ls = not is_ls_isolated(d)
        except OSError as e:
            err(f"Read error: {e}")
            return

        asar_exists = bool(asar_path and os.path.isfile(asar_path))
        needs_asar = asar_exists and (not is_asar_ui_patched(path))
        if kind == "patched":
            if not needs_ls and not needs_asar:
                hint("Antigravity Manager already patched.")
                if not confirm_with_captcha("Apply patch anyway?"):
                    _seed_standalone_token_if_missing()
                    return
            elif not needs_ls and needs_asar:
                info("Antigravity 2.0 binary already patched — updating app.asar Desktop UI bridge...")
                asar_ok = patch_desktop_asar(path, kill_on_lock=kill_on_lock)
                _seed_standalone_token_if_missing()
                resign_macos_bundle(path)
                print()
                panel_rows = [
                    ("Target", os.path.basename(path)),
                    ("Gate", f"{matched_gate.desc} @ 0x{off:x}"),
                    ("Desktop UI", "app.asar bridge active" if asar_ok else "skipped"),
                ]
                print_panel("PATCH COMPLETE", panel_rows)
                hint("Restart Antigravity/Antigravity IDE for the changes to take effect.")
                return
            else:
                info("Upgrading existing Antigravity 2.0 patch with standalone account isolation...")

        try:
            _make_backup(path, preserve_existing_if_patched=(kind == "patched"))
        except (PermissionError, OSError) as e:
            err(f"Backup error: {e}")
            if sys.platform == "darwin":
                hint("macOS blocked writing inside Antigravity.app (Errno 1 Operation not permitted).")
                hint("Close Antigravity, then re-run the patcher with sudo:")
                hint("  sudo python source/main.py   (or sudo ./Antigravity_Patcher_macOS_arm64)")
                hint("If it still fails: ls -lO shows 'uchg' flag -> patcher already tried 'chflags nouchg'.")
            else:
                hint("No write access — re-run as admin/root and close Antigravity first.")
            handle_patch_failure()
            return

        try:
            with open(path, "rb") as f:
                bdata = bytearray(f.read())

            # Валидация (offset, границы, размер) и применение патча
            bdata = apply_patches_to_data(bdata, [(off, matched_gate)])
            if b"ineligible" in bdata:
                bdata = bdata.replace(b"ineligible", b"inexigible")
            bdata = apply_ls_isolation_to_data(bdata)

            if sys.platform == "darwin":
                ensure_macos_writable(path)

            write_atomic(path, bytes(bdata), kill_on_lock=kill_on_lock)
            write_success = True
            break
        except PermissionError as e:
            if attempt == 0 and kill_on_lock:
                if sys.platform == "darwin":
                    warn(f"Write blocked by macOS (flags/owner): {e}")
                    ensure_macos_writable(path)
                    import time
                    time.sleep(0.5)
                    continue
                warn(f"Permission denied (file locked): {e}. Closing processes and retrying...")
                terminate_standalone_processes()
                import time
                time.sleep(1.5)
                continue
            err(f"Write error (Permission denied): {e}")
            if sys.platform == "darwin":
                hint("Close Antigravity and re-run with sudo: sudo python main.py")
            handle_patch_failure()
            return
        except OSError as e:
            # EPERM вне PermissionError (на всякий случай) — та же подсказка про sudo/флаги
            if e.errno in (errno.EPERM, errno.EACCES) and attempt == 0 and sys.platform == "darwin":
                warn(f"Write blocked by macOS (flags/owner): {e}")
                ensure_macos_writable(path)
                import time
                time.sleep(0.5)
                continue
            err(f"Write error: {e}")
            handle_patch_failure()
            return
        except Exception as e:
            err(f"Write error: {e}")
            handle_patch_failure()
            return

    if not write_success:
        handle_patch_failure()
        return

    hash_after = file_hash(path)
    asar_ok = patch_desktop_asar(path, kill_on_lock=kill_on_lock)
    _seed_standalone_token_if_missing()
    resign_macos_bundle(path)
    resign_macos_binary(path)
    print()
    step("Patch manager binary", True, matched_gate.desc)
    print()
    panel_rows = [
        ("Target", os.path.basename(path)),
        ("Gate", f"{matched_gate.desc} @ 0x{off:x}"),
        ("Desktop UI", "app.asar bridge active" if asar_ok else "skipped"),
    ]
    if hash_before and hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    print_panel("PATCH COMPLETE", panel_rows)
    hint("Restart Antigravity/Antigravity IDE for the changes to take effect.")


def do_restore_manager(path, force=False):
    from patcher.cli import confirmed

    if not os.path.isfile(path):
        err(f"Target is not a file: {path}")
        return

    bak = path + BAK_EXT
    if not os.path.exists(bak):
        warn(f"No backup for {os.path.basename(path)} (nothing to restore).")
        return

    status, _ = get_status(path)
    if status != "patched":
        warn("Manager is not patched — continuing restore anyway.")

    if is_locked(path):
        err("Binary is locked — close the app first.")
        return

    hash_before = file_hash(path)
    try:
        with open(bak, "rb") as f:
            bak_data = f.read()

        write_atomic(path, bak_data, meta_source=bak)
        fix_posix_permissions(path)
    except Exception as e:
        err(f"Restore error: {e}")
        return

    restore_desktop_asar(path)
    hash_after = file_hash(path)
    resign_macos_bundle(path)
    resign_macos_binary(path)
    print()
    panel_rows = [("Target", os.path.basename(path))]
    if hash_before and hash_after and hash_before != hash_after:
        panel_rows.append(("Before", f"{hash_before[:8]}...{hash_before[56:]}"))
        panel_rows.append(("After", f"{hash_after[:8]}...{hash_after[56:]}"))
    print_panel("RESTORE COMPLETE", panel_rows)
