"""
switch_runner.py -- Standalone detached supervisor for Antigravity IDE account switching.
Implements the authoritative 1:1 workflow from lbjlaq/Antigravity-Manager:
1. Wait briefly for caller IPC to complete.
2. Snapshots executable path & workspace.
3. Completely terminates Antigravity IDE and all language_server processes.
4. Waits for all locks, ports, and processes to fully terminate.
5. Injects fresh credentials into SQLite state.vscdb, system Credential Manager, and ~/.gemini/ files.
6. Relaunches Antigravity IDE cleanly on the interactive desktop.
"""

from __future__ import annotations
import os
import sys
import time
import json
import sqlite3
import base64
import re
import shutil
import ssl
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force UTF-8 for stdio on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

HOME_DIR = Path.home()
AG_TOOLS_DIR = Path(os.environ.get("AG_TOOLS_DIR") or (HOME_DIR / ".antigravity_tools"))
STANDALONE_PROFILES_DIR = HOME_DIR / ".antigravity-profiles"
AVATARS_DIR = AG_TOOLS_DIR / "avatars"
APPDATA_DIR = Path(os.environ.get("APPDATA", str(HOME_DIR / "AppData" / "Roaming")))
IDE_GLOBAL_STORAGE_DIR = APPDATA_DIR / "Antigravity IDE" / "User" / "globalStorage"
CODE_GLOBAL_STORAGE_DIR = APPDATA_DIR / "Code" / "User" / "globalStorage"
GEMINI_DIR = Path(os.environ.get("AG_GEMINI_DIR") or (HOME_DIR / ".gemini"))
VSCDB_PATH = IDE_GLOBAL_STORAGE_DIR / "state.vscdb"
VSCDB_BACKUP_PATH = IDE_GLOBAL_STORAGE_DIR / "state.vscdb.backup"
CODE_VSCDB_PATH = CODE_GLOBAL_STORAGE_DIR / "state.vscdb"
CODE_VSCDB_BACKUP_PATH = CODE_GLOBAL_STORAGE_DIR / "state.vscdb.backup"
LOG_FILE = AG_TOOLS_DIR / "switch.log"
IDE_WINCRED_TARGET = "gemini:antigravity"
STANDALONE_WINCRED_TARGET = "jetski:antigravity"
STANDALONE_WINCRED_USER = "antigravity"

# Purge Electron / VS Code runtime environment variables that cause spawned IDE apps to execute as headless Node CLI
for _k in list(os.environ.keys()):
    _uk = _k.upper()
    if _uk.startswith(('ELECTRON_', 'VSCODE_', 'ATOM_SHELL_')) or _uk in ('NODE_OPTIONS', 'NODE_ENV', 'ORIGINAL_XDG_CURRENT_DESKTOP'):
        os.environ.pop(_k, None)

OAUTH_CLIENTS_FILE = AG_TOOLS_DIR / "oauth_clients.json"
_OAUTH_CLIENT_ID_RE = re.compile(rb"[0-9]{10,}-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com")
_OAUTH_SECRET_PREFIX = b"GOC" + b"SPX-"
_OAUTH_SECRET_FULL_RE = re.compile(_OAUTH_SECRET_PREFIX + rb"[A-Za-z0-9_-]{28}")

SSL_CTX = ssl._create_unverified_context()


def _atomic_write_json(path: Path, data: dict | list) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 2:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            time.sleep(0.015)


def _scan_file_for_oauth_strings(
    file_path: Path,
    cids: list[str],
    secs: list[str],
    cid_seen: set[str],
    sec_seen: set[str],
) -> None:
    try:
        st = file_path.stat()
        if not file_path.is_file() or st.st_size <= 0 or st.st_size > 400 * 1024 * 1024:
            return
        cid_marker = b".apps.googleusercontent.com"
        chunk_size = 8 * 1024 * 1024
        overlap = 256
        with open(file_path, "rb") as fp:
            pos = 0
            while pos < st.st_size:
                fp.seek(pos)
                buf = fp.read(chunk_size + overlap)
                if not buf:
                    break
                if cid_marker in buf:
                    for m in _OAUTH_CLIENT_ID_RE.finditer(buf):
                        val = m.group(0).decode("ascii", errors="ignore")
                        if val and val not in cid_seen:
                            cid_seen.add(val)
                            cids.append(val)
                if _OAUTH_SECRET_PREFIX in buf:
                    for m in _OAUTH_SECRET_FULL_RE.finditer(buf):
                        val = m.group(0).decode("ascii", errors="ignore")
                        if val and val not in sec_seen:
                            sec_seen.add(val)
                            secs.append(val)
                if len(buf) < chunk_size + overlap:
                    break
                pos += chunk_size
    except Exception:
        pass


def _discover_oauth_clients_from_binaries() -> list[dict]:
    local_app = Path(os.environ.get("LOCALAPPDATA", str(HOME_DIR / "AppData" / "Local")))
    prog_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    candidates = [
        local_app / "Programs" / "Antigravity IDE" / "resources" / "app" / "out" / "main.js",
        prog_files / "Antigravity IDE" / "resources" / "app" / "out" / "main.js",
        Path("/Applications/Antigravity IDE.app/Contents/Resources/app/out/main.js"),
        Path("/usr/share/antigravity-ide/resources/app/out/main.js"),
        GEMINI_DIR / "bin" / "agy.exe",
        GEMINI_DIR / "bin" / "antigravity.exe",
        GEMINI_DIR / "bin" / "agy",
        GEMINI_DIR / "bin" / "antigravity",
        local_app / "Programs" / "antigravity" / "resources" / "bin" / "language_server.exe",
        Path("/Applications/Antigravity.app/Contents/Resources/bin/language_server"),
        Path("/opt/antigravity/resources/bin/language_server"),
    ]
    cids: list[str] = []
    secs: list[str] = []
    cid_seen: set[str] = set()
    sec_seen: set[str] = set()
    sources_scanned = 0

    for cand in candidates:
        if not cand.is_file():
            continue
        prev = len(cids) + len(secs)
        _scan_file_for_oauth_strings(cand, cids, secs, cid_seen, sec_seen)
        if len(cids) + len(secs) > prev:
            sources_scanned += 1
        if sources_scanned >= 2 and cids and secs:
            break

    if not cids or not secs:
        return []

    pairs: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for cid, sec in zip(cids, secs):
        pairs.append({"client_id": cid, "client_secret": sec, "verified": False})
        seen_pairs.add((cid, sec))
    for cid in cids[:4]:
        for sec in secs[:4]:
            if (cid, sec) not in seen_pairs:
                pairs.append({"client_id": cid, "client_secret": sec, "verified": False})
                seen_pairs.add((cid, sec))
    return pairs[:12]


def _get_oauth_clients() -> list[dict]:
    try:
        if OAUTH_CLIENTS_FILE.is_file():
            parsed = json.loads(OAUTH_CLIENTS_FILE.read_text(encoding="utf-8"))
            if isinstance(parsed, list) and parsed:
                valid = [
                    p for p in parsed
                    if isinstance(p, dict) and p.get("client_id") and p.get("client_secret")
                ]
                if valid:
                    return valid
    except Exception:
        pass

    discovered = _discover_oauth_clients_from_binaries()
    if discovered:
        try:
            AG_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(OAUTH_CLIENTS_FILE, discovered)
        except Exception:
            pass
    return discovered


def _persist_verified_oauth_client(cid: str, csec: str) -> None:
    if not cid or not csec:
        return
    try:
        current = _get_oauth_clients()
        other_verified = [
            p for p in current
            if not (p.get("client_id") == cid and p.get("client_secret") == csec) and p.get("verified")
        ]
        other_unverified = [
            p for p in current
            if not (p.get("client_id") == cid and p.get("client_secret") == csec) and not p.get("verified")
        ]
        ordered = [{"client_id": cid, "client_secret": csec, "verified": True}] + other_verified + other_unverified
        AG_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(OAUTH_CLIENTS_FILE, ordered[:12])
    except Exception:
        pass


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        AG_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def encode_varint(n: int) -> bytes:
    res = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            res.append(b | 0x80)
        else:
            res.append(b)
            break
    return bytes(res)


def encode_len_delim_field(field_num: int, data: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data


def encode_string_field(field_num: int, s: str) -> bytes:
    return encode_len_delim_field(field_num, s.encode("utf-8"))


def create_unified_state_entry(sentinel_key: str, payload: bytes) -> str:
    """Matches upstream Antigravity-Manager protobuf::create_unified_state_entry:
    Topic(Field 1 data map) -> DataEntry(Field 1 key, Field 2 Row) -> Row(Field 1 base64 payload)
    """
    b64_payload = base64.b64encode(payload).decode("utf-8")
    row = encode_string_field(1, b64_payload)
    data_entry = encode_string_field(1, sentinel_key) + encode_len_delim_field(2, row)
    topic = encode_len_delim_field(1, data_entry)
    return base64.b64encode(topic).decode("utf-8")


def build_user_status_entry(email: str) -> str:
    """Matches upstream Antigravity-Manager protobuf::create_minimal_user_status_payload:
    [encode_string_field(3, email), encode_string_field(7, email)].concat()
    """
    payload = encode_string_field(3, email) + encode_string_field(7, email)
    return create_unified_state_entry("userStatusSentinelKey", payload)


def build_oauth_info(access_token: str, refresh_token: str, expiry_secs: int, id_token: str = "") -> bytes:
    """Matches upstream Antigravity-Manager protobuf::create_oauth_info"""
    f1 = encode_string_field(1, access_token)
    f2 = encode_string_field(2, "Bearer")
    f3 = encode_string_field(3, refresh_token)
    # Field 4: expiry Timestamp message (seconds = field 1 varint, nanos = field 2 varint 0)
    ts = encode_varint((1 << 3) | 0) + encode_varint(expiry_secs) + encode_varint((2 << 3) | 0) + encode_varint(0)
    f4 = encode_len_delim_field(4, ts)
    info = f1 + f2 + f3 + f4
    if id_token:
        info += encode_string_field(5, id_token)
    return info


def build_outer_oauth(oauth_info: bytes, project_id: str = "", is_gcp_tos: bool = False) -> str:
    f1_key = "authStateWithContextSentinelKey"
    f1_val = json.dumps({
        "state": "signedIn",
        "context": {
            "project": project_id,
            "showProjectError": False,
            "errorMessage": "",
            "ineligibleMessage": "",
            "verificationUrl": "",
            "isGcpTos": bool(is_gcp_tos),
            "browserOpenFailed": False,
            "appealUrl": "",
            "appealLinkText": ""
        }
    }, separators=(',', ':'))
    row1 = encode_string_field(1, f1_val)
    data1 = encode_string_field(1, f1_key) + encode_len_delim_field(2, row1)
    entry1 = encode_len_delim_field(1, data1)

    b64_info = base64.b64encode(oauth_info).decode("utf-8")
    row2 = encode_string_field(1, b64_info)
    data2 = encode_string_field(1, "oauthTokenInfoSentinelKey") + encode_len_delim_field(2, row2)
    entry2 = encode_len_delim_field(1, data2)

    return base64.b64encode(entry1 + entry2).decode("utf-8")


def get_ide_exe_path(requested_app: str = "") -> tuple[Path | None, str]:
    """Returns (exe_path, app_type) where app_type is 'ide', 'code', 'standalone', or 'cli'."""
    req = (requested_app or "").strip().lower()
    local_app = Path(os.environ.get("LOCALAPPDATA", str(HOME_DIR / "AppData" / "Local")))
    prog_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))

    if req == "standalone":
        # 1. Check running standalone Antigravity 2.0 process (strictly excluding Antigravity IDE / helpers)
        try:
            import psutil
            for p in psutil.process_iter(['name', 'exe']):
                name = (p.info['name'] or '').lower()
                exe_str = p.info['exe'] or ''
                exe_low = exe_str.lower()
                if (
                    name in ('antigravity.exe', 'antigravity')
                    and 'antigravity ide' not in exe_low
                    and 'antigravity-ide' not in exe_low
                    and 'helper' not in exe_low
                    and exe_str
                ):
                    exe = Path(exe_str)
                    if exe.exists():
                        if sys.platform == "darwin" and ".app/" in exe_str:
                            app_path = Path(exe_str.split(".app/", 1)[0] + ".app")
                            if app_path.exists():
                                return app_path, 'standalone'
                        return exe, 'standalone'
        except Exception:
            pass

        # 2. Check standard standalone install paths
        candidates = [
            local_app / "Programs" / "antigravity" / "Antigravity.exe",
            prog_files / "antigravity" / "Antigravity.exe",
            prog_files / "Antigravity" / "Antigravity.exe",
        ]
        if sys.platform == "darwin":
            candidates.extend([
                Path("/Applications/Antigravity.app"),
                HOME_DIR / "Applications" / "Antigravity.app",
            ])
        elif os.name != "nt":
            candidates.extend([
                Path("/opt/antigravity/Antigravity"),
                Path("/usr/share/antigravity/Antigravity"),
                Path("/opt/Antigravity/Antigravity"),
            ])
        for cand in candidates:
            if cand.exists():
                return cand, 'standalone'
        return None, "standalone"

    check_ide = req in ("", "ide")
    check_code = req in ("", "code")

    # 1. Check running process
    try:
        import psutil
        if check_ide:
            for p in psutil.process_iter(['name', 'exe']):
                name = (p.info['name'] or '').lower()
                if 'antigravity ide' in name and p.info['exe']:
                    exe = Path(p.info['exe'])
                    if exe.exists():
                        return exe, 'ide'
        if check_code:
            for p in psutil.process_iter(['name', 'exe']):
                name = (p.info['name'] or '').lower()
                if name in ('code.exe', 'code') and p.info['exe']:
                    exe = Path(p.info['exe'])
                    if exe.exists():
                        return exe, 'code'
    except Exception:
        pass

    # 2. Check standard install paths
    if check_ide:
        candidate_ide = local_app / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe"
        if candidate_ide.exists():
            return candidate_ide, 'ide'
        candidate_pf = Path("C:/Program Files/Antigravity IDE/Antigravity IDE.exe")
        if candidate_pf.exists():
            return candidate_pf, 'ide'

    if check_code:
        candidate_code = local_app / "Programs" / "Microsoft VS Code" / "Code.exe"
        if candidate_code.exists():
            return candidate_code, 'code'

    return None, (req if req in ("ide", "code") else 'cli')


def _compute_expiry(expires_in: int | float | None) -> tuple[str, int]:
    ttl = max(60, int(expires_in or 3599) - 30)
    exp_dt = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return exp_dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z"), int(exp_dt.timestamp() * 1000)


def terminate_antigravity_processes(app_type: str = 'ide'):
    log(f"Terminating processes for app_type '{app_type}'...")
    my_pid = os.getpid()

    if app_type == 'cli':
        log("CLI-only slot switch: keeping Antigravity IDE and Antigravity 2.0 running untouched.")
        return

    if app_type in ('ide', 'code'):
        targets = ["Antigravity IDE.exe", "agy.exe"] if app_type == 'ide' else ["Code.exe"]
        if sys.platform == "win32":
            for t in targets:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", t, "/T"],
                        capture_output=True,
                        creationflags=0x08000000,
                    )
                except Exception as e:
                    log(f"taskkill {t} error: {e}")

        for _ in range(40):
            still_running = False
            try:
                import psutil
                for p in psutil.process_iter(['pid', 'name']):
                    if p.info['pid'] == my_pid:
                        continue
                    n = (p.info['name'] or '').lower()
                    target_matched = (
                        ('antigravity ide' in n or n in ('agy.exe', 'agy'))
                        if app_type == 'ide'
                        else n in ('code.exe', 'code')
                    )
                    if target_matched:
                        still_running = True
                        try:
                            p.kill()
                        except Exception:
                            pass
            except Exception:
                pass
            if not still_running:
                break
            time.sleep(0.1)
    elif app_type == 'standalone':
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "Antigravity.exe", "/T"],
                    capture_output=True,
                    creationflags=0x08000000,
                )
            except Exception as e:
                log(f"taskkill Antigravity.exe error: {e}")

        psutil_checked = False
        for _ in range(40):
            still_running = False
            try:
                import psutil
                psutil_checked = True
                for p in psutil.process_iter(['pid', 'name', 'exe']):
                    if p.info['pid'] == my_pid:
                        continue
                    n = (p.info['name'] or '').lower()
                    exe = (p.info['exe'] or '').lower()
                    if (
                        'antigravity ide' in n
                        or 'antigravity ide' in exe
                        or 'antigravity-ide' in exe
                        or n in ('agy.exe', 'agy')
                    ):
                        continue
                    is_sa_app = (
                        (n in ('antigravity.exe', 'antigravity') and ('antigravity' in exe or not exe))
                        or 'antigravity.app/contents/' in exe
                    )
                    is_sa_ls = (
                        n in ('language_server.exe', 'language_server')
                        and 'agy' not in n
                        and ('antigravity' in exe or 'programs' in exe or 'resources' in exe)
                    )
                    if is_sa_app or is_sa_ls:
                        still_running = True
                        try:
                            p.kill()
                        except Exception:
                            pass
            except Exception:
                break
            if not still_running:
                break
            time.sleep(0.1)

        if not psutil_checked:
            if sys.platform == "win32":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", "language_server.exe"],
                        capture_output=True,
                        creationflags=0x08000000,
                    )
                except Exception as e:
                    log(f"taskkill language_server.exe error: {e}")
            else:
                for pat in ("Antigravity.app/Contents/", "/antigravity/Antigravity", "/antigravity/resources/bin/language_server"):
                    try:
                        subprocess.run(["pkill", "-f", pat], capture_output=True)
                    except Exception:
                        pass
    else:
        # Decoupled CLI termination: inspect paths via psutil first so Antigravity IDE processes stay untouched
        psutil_checked = False
        for _ in range(25):
            still_running = False
            try:
                import psutil
                psutil_checked = True
                for p in psutil.process_iter(['pid', 'name', 'exe']):
                    if p.info['pid'] == my_pid:
                        continue
                    n = (p.info['name'] or '').lower()
                    exe = (p.info['exe'] or '').lower()
                    if (
                        'language_server' in n
                        and 'antigravity ide' not in exe
                        and 'agy' not in n
                        and ('antigravity' in exe or 'programs' in exe or 'resources' in exe)
                    ):
                        still_running = True
                        try:
                            p.kill()
                        except Exception:
                            pass
            except Exception:
                break
            if not still_running:
                break
            time.sleep(0.1)

        if not psutil_checked and sys.platform == "win32":
            for t in ("language_server_windows_x64.exe", "language_server.exe"):
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/IM", t],
                        capture_output=True,
                        creationflags=0x08000000,
                    )
                except Exception as e:
                    log(f"taskkill {t} error: {e}")

    time.sleep(0.6)
    log("Processes terminated, lock release window finished.")


def refresh_google_token(refresh_token: str) -> tuple[str, int, str] | None:
    if not refresh_token:
        return None
    clients = _get_oauth_clients()
    tried_rediscover = False
    for _ in range(2):
        for pair in clients:
            cid = pair.get("client_id", "")
            csec = pair.get("client_secret", "")
            if not cid or not csec:
                continue
            try:
                data = urllib.parse.urlencode({
                    "client_id": cid,
                    "client_secret": csec,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token"
                }).encode("utf-8")
                req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
                with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    access_tok = res.get("access_token")
                    expires_in = res.get("expires_in", 3599)
                    id_token = res.get("id_token", "")
                    if access_tok:
                        first = clients[0] if clients else None
                        if (
                            not pair.get("verified")
                            or not OAUTH_CLIENTS_FILE.is_file()
                            or not first
                            or first.get("client_id") != cid
                            or first.get("client_secret") != csec
                        ):
                            _persist_verified_oauth_client(cid, csec)
                        return access_tok, int(expires_in), id_token
            except Exception:
                continue
        if tried_rediscover:
            break
        tried_rediscover = True
        fresh = _discover_oauth_clients_from_binaries()
        merged = list(clients)
        seen = {(p.get("client_id"), p.get("client_secret")) for p in merged}
        for item in fresh:
            key = (item.get("client_id"), item.get("client_secret"))
            if key not in seen:
                merged.append(item)
                seen.add(key)
        if len(merged) <= len(clients):
            break
        clients = merged[:12]
    return None


def update_wincred_antigravity(
    access_token: str,
    refresh_token: str,
    id_token: str = "",
    email: str = "",
    expires_in: int = 3599,
    target_name: str = IDE_WINCRED_TARGET,
    username: str = STANDALONE_WINCRED_USER,
) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ('Flags', wintypes.DWORD),
                ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME),
                ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
                ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD),
                ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR),
                ('UserName', wintypes.LPWSTR),
            ]

        PCREDENTIAL = ctypes.POINTER(CREDENTIAL)
        advapi32 = ctypes.windll.advapi32
        CredWriteW = advapi32.CredWriteW
        CredWriteW.argtypes = [PCREDENTIAL, wintypes.DWORD]
        CredWriteW.restype = wintypes.BOOL

        exp_iso, _ = _compute_expiry(expires_in)
        payload = {
            "token": {
                "access_token": access_token,
                "token_type": "Bearer",
                "refresh_token": refresh_token,
                "expiry": exp_iso,
                "expires_in": int(expires_in or 3599),
            },
            "auth_method": "consumer"
        }
        if email:
            payload["id_token_email"] = email
        if id_token:
            payload["id_token"] = id_token

        blob_bytes = json.dumps(payload).encode('utf-8')
        blob_buf = (ctypes.c_byte * len(blob_bytes)).from_buffer_copy(blob_bytes)

        cred = CREDENTIAL()
        cred.Flags = 0
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = target_name
        cred.Comment = None
        cred.CredentialBlobSize = len(blob_bytes)
        cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = username

        res = bool(CredWriteW(ctypes.byref(cred), 0))
        log(f"Updated Windows Credential {target_name} (expiry={exp_iso}): {res}")
        return res
    except Exception as e:
        log(f"Error updating Windows Credential {target_name}: {e}")
        return False


def sync_gemini_credentials(
    access_token: str,
    refresh_token: str,
    email: str,
    id_token: str = "",
    expires_in: int = 3599,
    app_type: str = 'ide',
    is_gcp_tos: bool = False,
):
    try:
        GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        exp_iso, exp_ms = _compute_expiry(expires_in)
        ttl_val = int(expires_in or 3599)
        gcp_str = "true" if is_gcp_tos else "false"

        token_data = {
            "token": {
                "access_token": access_token,
                "token_type": "Bearer",
                "refresh_token": refresh_token,
                "expiry": exp_iso,
                "expires_in": ttl_val,
            },
            "auth_method": "consumer",
            "id_token_email": email,
            "id_token": id_token
        }

        if app_type == 'standalone':
            (GEMINI_DIR / "jetski-desktopapp-oauth-token").write_text(
                json.dumps(token_data, indent=2), encoding="utf-8"
            )
            try:
                appdata = Path(os.environ.get("APPDATA", str(APPDATA_DIR)))
                ag_storage_dir = appdata / "antigravity"
                ag_storage = ag_storage_dir / "app_storage.json"
                if ag_storage_dir.exists() or ag_storage.exists():
                    sdata = {}
                    if ag_storage.exists():
                        try:
                            sdata = json.loads(ag_storage.read_text(encoding="utf-8")) or {}
                        except Exception:
                            sdata = {}
                    sdata["agy.onboarding.lastLoginUsername"] = email
                    sdata["agy.onboarding.lastLoginIsGcpTos"] = gcp_str
                    sdata["agy.onboarding.welcomeModalSeen"] = "true"
                    sdata["jetski.onboarding.lastLoginUsername"] = email
                    sdata["jetski.onboarding.lastLoginIsGcpTos"] = gcp_str
                    sdata["jetski.onboarding.welcomeModalSeen"] = "true"
                    sdata["jetski.onboarding.cloudResourceDisclosureSeen"] = "true"
                    for stale_key in (
                        "agy.onboarding.lastLoginStrategy",
                        "jetski.onboarding.lastLoginStrategy",
                    ):
                        sdata.pop(stale_key, None)
                    ag_storage_dir.mkdir(parents=True, exist_ok=True)
                    _atomic_write_json(ag_storage, sdata)
            except Exception:
                pass
            log(f"Synced standalone ~/.gemini/jetski-desktopapp-oauth-token (email={email}, expiry={exp_iso}).")
            return

        cli_dir = GEMINI_DIR / "antigravity-cli"
        cli_dir.mkdir(parents=True, exist_ok=True)
        (cli_dir / "antigravity-oauth-token").write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        if app_type != 'cli':
            (GEMINI_DIR / "jetski-standalone-oauth-token").write_text(json.dumps(token_data, indent=2), encoding="utf-8")

        creds_data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "email": email,
            "expiry_date": exp_ms,
            "scope": "https://www.googleapis.com/auth/userinfo.email openid https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.profile"
        }
        (GEMINI_DIR / "oauth_creds.json").write_text(json.dumps(creds_data, indent=2), encoding="utf-8")

        try:
            google_accs = {"active": email, "old": []}
            (GEMINI_DIR / "google_accounts.json").write_text(json.dumps(google_accs, indent=2), encoding="utf-8")
        except Exception:
            pass

        log(f"Synced {app_type.upper()} ~/.gemini/ credentials (email={email}, expiry={exp_iso}).")
    except Exception as e:
        log(f"Error syncing ~/.gemini credentials: {e}")


def _apply_ide_onboarding_to_db(conn: sqlite3.Connection, email: str, is_gcp_tos: bool = False) -> None:
    gcp_str = "true" if is_gcp_tos else "false"
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
    )

    onboarding_rows = [
        ("agy.onboarding.lastLoginUsername", email),
        ("agy.onboarding.lastLoginIsGcpTos", gcp_str),
        ("agy.onboarding.welcomeModalSeen", "true"),
        ("jetski.onboarding.lastLoginUsername", email),
        ("jetski.onboarding.lastLoginIsGcpTos", gcp_str),
        ("jetski.onboarding.welcomeModalSeen", "true"),
        ("jetski.onboarding.cloudResourceDisclosureSeen", "true"),
        ("antigravityOnboarding", "true"),
        ("antigravity.hasShownOnboarding", "true"),
    ]
    cur.executemany("INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", onboarding_rows)
    cur.execute(
        "DELETE FROM ItemTable WHERE key IN ("
        "'jetskiStateSync.agentManagerInitState',"
        "'agy.onboarding.lastLoginStrategy',"
        "'jetski.onboarding.lastLoginStrategy'"
        ")"
    )

    row = cur.execute("SELECT value FROM ItemTable WHERE key = 'google.google-antigravity'").fetchone()
    gobj = {}
    if row and row[0]:
        raw_val = row[0].decode("utf-8", errors="ignore") if isinstance(row[0], (bytes, bytearray)) else str(row[0])
        try:
            parsed = json.loads(raw_val)
            if isinstance(parsed, dict):
                gobj = parsed
        except Exception:
            gobj = {}

    gobj["antigravity.accountTelemetryEnabled"] = True
    gobj["antigravity.legacyTelemetrySettingNoticeShown"] = True
    storage = gobj.get("antigravity-storage")
    if not isinstance(storage, dict):
        storage = {}
        gobj["antigravity-storage"] = storage
    storage["agy.onboarding.lastLoginUsername"] = email
    storage["agy.onboarding.lastLoginIsGcpTos"] = gcp_str
    storage["agy.onboarding.welcomeModalSeen"] = "true"
    storage["jetski.onboarding.lastLoginUsername"] = email
    storage["jetski.onboarding.lastLoginIsGcpTos"] = gcp_str
    storage["jetski.onboarding.welcomeModalSeen"] = "true"
    storage["jetski.onboarding.cloudResourceDisclosureSeen"] = "true"
    for stale_key in (
        "agy.onboarding.lastLoginStrategy",
        "jetski.onboarding.lastLoginStrategy",
    ):
        storage.pop(stale_key, None)

    cur.execute(
        "INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('google.google-antigravity', ?)",
        (json.dumps(gobj, ensure_ascii=False, separators=(",", ":")),),
    )


def apply_credentials_post_kill(target_id_or_email: str, app_type: str = 'ide') -> dict:
    log(f"Applying credentials for '{target_id_or_email}' post-kill (app_type: '{app_type}')...")
    target_clean = target_id_or_email.strip().lower()

    accounts_file = AG_TOOLS_DIR / "accounts.json"
    if not accounts_file.exists():
        raise RuntimeError("accounts.json not found in ~/.antigravity_tools")

    with open(accounts_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    accounts = data.get("accounts", [])
    target = None
    for a in accounts:
        if a.get("id", "").lower() == target_clean or a.get("email", "").lower() == target_clean:
            target = a
            break

    if not target:
        raise RuntimeError(f"Account '{target_id_or_email}' not found in accounts.json")

    detail_file = AG_TOOLS_DIR / "accounts" / f"{target['id']}.json"
    if not detail_file.exists():
        raise RuntimeError(f"Detail file for '{target['id']}' not found.")

    with open(detail_file, "r", encoding="utf-8") as f:
        detail = json.load(f)

    # Resolve true display name (avoid corrupted ? strings)
    name = detail.get("name") or target.get("name") or ""
    tok = detail.get("token", {})
    id_tok = tok.get("id_token", "")
    if id_tok and (not name or "?" in name):
        try:
            parts = id_tok.split(".")
            if len(parts) >= 2:
                pad = "=" * (-len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
                jwt_name = payload.get("name")
                if jwt_name:
                    name = jwt_name
        except Exception:
            pass
    if not name:
        name = target["email"].split("@")[0]

    target["name"] = name
    detail["name"] = name

    ref_token = tok.get("refresh_token", "")
    if not ref_token:
        raise RuntimeError(f"No refresh_token found for {target['email']}")

    # Refresh token with Google
    fresh_res = refresh_google_token(ref_token)
    if fresh_res:
        fresh_access_token, expires_in, fresh_id_token = fresh_res
    else:
        fresh_access_token = tok.get("access_token", "")
        expires_in = tok.get("expires_in", 3599)
        fresh_id_token = id_tok

    expires_in = int(expires_in or 3599)
    if not fresh_id_token:
        fresh_id_token = id_tok

    expiry_secs = int(time.time()) + expires_in

    # Save refreshed tokens back to account file
    detail["token"]["access_token"] = fresh_access_token
    detail["token"]["expires_in"] = expires_in
    detail["token"]["expiry_timestamp"] = expiry_secs
    if fresh_id_token:
        detail["token"]["id_token"] = fresh_id_token
    _atomic_write_json(detail_file, detail)

    avatar_url = detail.get("avatar_url", "")
    uses_gcp_tos = bool((detail.get("quota") or {}).get("uses_gcp_tos"))

    # Update decoupled active indicators in accounts.json
    now_ts = int(time.time())
    if app_type == 'standalone':
        data["current_standalone_account_id"] = target["id"]
    elif app_type == 'cli':
        data["current_cli_account_id"] = target["id"]
    else:
        data["current_ide_account_id"] = target["id"]
        data["current_account_id"] = target["id"]
        data["current_target_ide"] = app_type if app_type in ("ide", "code") else "ide"

    for acc in data.get("accounts", []):
        if acc.get("id") == target["id"]:
            acc["name"] = name
            if app_type == 'standalone':
                acc["standalone_last_used"] = now_ts
            elif app_type == 'cli':
                acc["cli_last_used"] = now_ts
            else:
                acc["last_used"] = now_ts

    _atomic_write_json(accounts_file, data)

    if app_type == 'cli':
        sync_gemini_credentials(
            fresh_access_token,
            ref_token,
            target["email"],
            fresh_id_token,
            expires_in=expires_in,
            app_type='cli',
            is_gcp_tos=uses_gcp_tos,
        )
        log(f"CLI switch complete for '{target['email']}'.")
        return {"name": name, "email": target["email"], "app_type": "cli"}

    if app_type == 'standalone':
        sync_gemini_credentials(
            fresh_access_token,
            ref_token,
            target["email"],
            fresh_id_token,
            expires_in=expires_in,
            app_type='standalone',
            is_gcp_tos=uses_gcp_tos,
        )
        update_wincred_antigravity(
            fresh_access_token,
            ref_token,
            fresh_id_token,
            email=target["email"],
            expires_in=expires_in,
            target_name=STANDALONE_WINCRED_TARGET,
            username=STANDALONE_WINCRED_USER,
        )
        log(f"Standalone switch complete for '{target['email']}'.")
        return {"name": name, "email": target["email"], "app_type": "standalone"}

    # Build protobufs with 100% parity to Antigravity-Manager for IDE
    outer_user_status = build_user_status_entry(target["email"])
    oauth_info = build_oauth_info(fresh_access_token, ref_token, expiry_secs, id_token=fresh_id_token)
    project_id = detail.get("token", {}).get("project_id", "")
    outer_oauth = build_outer_oauth(oauth_info, project_id=project_id, is_gcp_tos=uses_gcp_tos)

    # Inject cleanly into target IDE state.vscdb without touching standalone
    vscdb_targets = []
    if app_type != 'code' and VSCDB_PATH.exists():
        vscdb_targets.append((VSCDB_PATH, VSCDB_BACKUP_PATH))
    if app_type != 'ide' and CODE_VSCDB_PATH.exists():
        vscdb_targets.append((CODE_VSCDB_PATH, CODE_VSCDB_BACKUP_PATH))

    if not vscdb_targets:
        log("Warning: neither Antigravity IDE nor VS Code state.vscdb found.")

    for db_p, bak_p in vscdb_targets:
        dev_prof = detail.get("device_profile")
        try:
            conn = sqlite3.connect(str(db_p))
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('antigravityUnifiedStateSync.oauthToken', ?)", (outer_oauth,))
            c.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('antigravityUnifiedStateSync.userStatus', ?)", (outer_user_status,))
            _apply_ide_onboarding_to_db(conn, target["email"], is_gcp_tos=uses_gcp_tos)
            if avatar_url:
                c.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('antigravity.profileUrl', ?)", (avatar_url,))
            if dev_prof and dev_prof.get("mac_machine_id"):
                c.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('storage.serviceMachineId', ?)", (dev_prof["mac_machine_id"],))
                c.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('telemetry.serviceMachineId', ?)", (dev_prof["mac_machine_id"],))
            conn.commit()
            conn.close()
            log(f"SQLite {db_p} updated successfully.")
        except Exception as e:
            log(f"Error updating {db_p}: {e}")

        if bak_p and bak_p.exists():
            try:
                conn_b = sqlite3.connect(str(bak_p))
                cb = conn_b.cursor()
                cb.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('antigravityUnifiedStateSync.oauthToken', ?)", (outer_oauth,))
                cb.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('antigravityUnifiedStateSync.userStatus', ?)", (outer_user_status,))
                _apply_ide_onboarding_to_db(conn_b, target["email"], is_gcp_tos=uses_gcp_tos)
                if avatar_url:
                    cb.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('antigravity.profileUrl', ?)", (avatar_url,))
                if dev_prof and dev_prof.get("mac_machine_id"):
                    cb.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('storage.serviceMachineId', ?)", (dev_prof["mac_machine_id"],))
                    cb.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('telemetry.serviceMachineId', ?)", (dev_prof["mac_machine_id"],))
                conn_b.commit()
                conn_b.close()
                log(f"{bak_p.name} updated.")
            except Exception as e:
                log(f"Backup db update error for {bak_p}: {e}")

    # Sync IDE ~/.gemini/ files and IDE Windows Credential Manager target without touching standalone
    sync_gemini_credentials(
        fresh_access_token,
        ref_token,
        target["email"],
        fresh_id_token,
        expires_in=expires_in,
        app_type=app_type,
        is_gcp_tos=uses_gcp_tos,
    )
    update_wincred_antigravity(
        fresh_access_token,
        ref_token,
        fresh_id_token,
        email=target["email"],
        expires_in=expires_in,
        target_name=IDE_WINCRED_TARGET,
        username="antigravity",
    )
    log("IDE switch complete: state.vscdb, ~/.gemini/, and WinCred synchronized.")

    return {"name": name, "email": target["email"]}


def relaunch_ide(exe_path: Path, workspace: str = "", app_type: str = 'ide'):
    norm_exe = os.path.normpath(str(exe_path))
    norm_cwd = os.path.normpath(str(exe_path.parent))
    if app_type == "code":
        app_label = "VS Code"
    elif app_type == "standalone":
        app_label = "Antigravity 2.0"
    else:
        app_label = "Antigravity IDE"
    log(f"Relaunching {app_label}: '{norm_exe}' (working dir: '{norm_cwd}')")

    # Settle file locks & single-instance mutex release
    time.sleep(2.0)

    # Prepare pristine environment free of Electron/VS Code runtime flags
    clean_env = os.environ.copy()
    for k in list(clean_env.keys()):
        uk = k.upper()
        if uk.startswith(('ELECTRON_', 'VSCODE_', 'ATOM_SHELL_', 'ANTIGRAVITY_VSCODE_')) or uk in ('NODE_OPTIONS', 'NODE_ENV', 'ORIGINAL_XDG_CURRENT_DESKTOP'):
            clean_env.pop(k, None)
            os.environ.pop(k, None)

    started = False
    args_str = f'"{os.path.normpath(workspace)}"' if workspace and Path(workspace).exists() else ""

    # 0. macOS .app bundle launch
    if sys.platform == "darwin" and norm_exe.endswith(".app"):
        try:
            cmd = ["open", norm_exe]
            if args_str:
                cmd.append(os.path.normpath(workspace))
            subprocess.Popen(cmd, cwd=norm_cwd, env=clean_env, close_fds=True)
            started = True
            log(f"Spawned {app_label} via open.")
        except Exception as e:
            log(f"macOS open failed: {e}")

    # 1. Primary on Windows: os.startfile (ShellExecute) executes on interactive user desktop
    if not started and hasattr(os, "startfile") and sys.platform == "win32":
        try:
            log("Attempting launch via os.startfile (interactive user desktop)...")
            if args_str:
                os.startfile(norm_exe, arguments=args_str)
            else:
                os.startfile(norm_exe)
            started = True
            log("os.startfile launched successfully.")
        except Exception as e:
            log(f"os.startfile failed: {e}")

    # 2. Secondary: Detached subprocess.Popen
    if not started:
        try:
            log("Attempting launch via subprocess.Popen...")
            cmd = [norm_exe]
            if args_str:
                cmd.append(os.path.normpath(workspace))
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(cmd, cwd=norm_cwd, env=clean_env, creationflags=flags, close_fds=True)
            started = True
            log(f"Spawned {app_label} (PID: {proc.pid}) via subprocess.Popen.")
        except Exception as e:
            log(f"subprocess.Popen failed: {e}")

    time.sleep(1.5)
    log(f"{app_label} relaunch routine finished.")


def ensure_workbench_patched(exe_path: str):
    """Ensures that workbench.desktop.main.js has antigravity.openConversation registered."""
    try:
        exe = Path(exe_path)
        if not exe.exists():
            return
        wb = exe.parent / "resources" / "app" / "out" / "vs" / "workbench" / "workbench.desktop.main.js"
        if not wb.exists():
            return

        content = wb.read_text(encoding="utf-8")
        if 'id:"antigravity.openConversation"' in content:
            log("workbench.desktop.main.js already has openConversation patch.")
            return

        new_content = None
        idx_svc = content.find('id:"antigravity.sendPromptToAgentPanel"')
        idx_cmd = content.find('id:"antigravity.startNewConversation"')
        m_svc = re.search(
            r'id:"antigravity\.sendPromptToAgentPanel"[\s\S]{0,160}?\.get\(([A-Za-z_$0-9]+)\)\.sendMessageToChatPanel',
            content[idx_svc : idx_svc + 240],
        ) if idx_svc != -1 else None
        cmd_win_start = max(0, idx_cmd - 80) if idx_cmd != -1 else 0
        m_cmd = re.search(
            r'([A-Za-z_$0-9]+)\(class extends ([A-Za-z_$0-9]+)\{constructor\(\)\{super\(\{id:"antigravity\.startNewConversation"[\s\S]{0,280}?\}\)\}\}\),',
            content[cmd_win_start : idx_cmd + 320],
        ) if idx_cmd != -1 else None
        if m_svc and m_cmd:
            reg_fn, base_cls, svc_tok = m_cmd.group(1), m_cmd.group(2), m_svc.group(1)
            dyn_patch = (
                f'{reg_fn}(class extends {base_cls}{{constructor(){{super({{id:"antigravity.openConversation",'
                f'title:"Open Conversation",f1:!1}})}}async run(t,e){{t.get({svc_tok}).togglePanelTab?.("conversation",e)}}}}),'
            )
            anchor_dyn = m_cmd.group(0)
            abs_start = cmd_win_start + m_cmd.start()
            new_content = content[:abs_start] + anchor_dyn + dyn_patch + content[abs_start + len(anchor_dyn) :]
            wb.write_text(new_content, encoding="utf-8")
            log("ensure_workbench_patched: successfully patched workbench.desktop.main.js (dynamic anchor)!")

        # Modern Antigravity IDE (anchor right after startNewConversation)
        anchor1 = 'de(class extends xe{constructor(){super({id:"antigravity.startNewConversation",title:"New Conversation",keybinding:{primary:3114,weight:300}})}async run(e){await e.get(Q4e).triggerFocus({selectionSource:"auto",startNewConversation:!0,shouldCloseWhenFocused:!1})}}),'
        patch1 = 'de(class extends xe{constructor(){super({id:"antigravity.openConversation",title:"Open Conversation",f1:!1})}async run(t,e){t.get(bse).togglePanelTab?.("conversation",e)}}),'

        # Legacy Antigravity IDE builds
        anchor2 = 'async run(t){t.get(AJ).togglePanelTab?.("customization")}});'
        patch2 = 've(class extends ke{constructor(){super({id:"antigravity.openConversation",title:"Open Conversation",f1:!1})}async run(t,e){t.get(AJ).togglePanelTab?.("conversation",e)}});'

        if new_content is not None:
            pass
        elif anchor1 in content:
            new_content = content.replace(anchor1, anchor1 + patch1, 1)
            wb.write_text(new_content, encoding="utf-8")
            log("ensure_workbench_patched: successfully patched workbench.desktop.main.js (modern anchor)!")
        elif anchor2 in content:
            new_content = content.replace(anchor2, anchor2 + patch2, 1)
            wb.write_text(new_content, encoding="utf-8")
            log("ensure_workbench_patched: successfully patched workbench.desktop.main.js (legacy anchor)!")
        else:
            log("ensure_workbench_patched: target string not found.")
            return

        # Wipe code cache so V8 compiles the new JS
        appdata = APPDATA_DIR / "Antigravity IDE"
        for d in [appdata / "CachedData", appdata / "Code Cache" / "js"]:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        log("ensure_workbench_patched: cleared V8 code cache.")
    except Exception as e:
        log(f"ensure_workbench_patched failed: {e}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('--help', '-h', '--version'):
        print("switch_runner.py --target <account_id_or_email> [--app ide|code|standalone] [--no-restart] [--workspace <path>]")
        return

    log(f"switch_runner started with args: {sys.argv[1:]}")
    target = ""
    workspace = ""
    requested_app = ""
    no_restart = False

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--target" and i + 1 < len(sys.argv):
            target = sys.argv[i + 1]
            i += 2
        elif arg == "--workspace" and i + 1 < len(sys.argv):
            workspace = sys.argv[i + 1]
            i += 2
        elif arg == "--app" and i + 1 < len(sys.argv):
            requested_app = sys.argv[i + 1].strip().lower()
            i += 2
        elif arg == "--no-restart":
            no_restart = True
            i += 1
        elif arg == "--restart":
            no_restart = False
            i += 1
        else:
            if not target:
                target = arg
            elif not workspace:
                workspace = arg
            i += 1

    if not target:
        log("No target specified. Exiting.")
        return

    # 1. Delay to let caller IPC finish cleanly
    time.sleep(0.5)

    # 2. Snapshot IDE executable path before killing
    exe_path, detected_app = get_ide_exe_path(requested_app)
    app_type = requested_app if requested_app in ("ide", "code", "standalone", "cli") else detected_app
    log(f"Target IDE executable path: {exe_path} (app_type: {app_type})")

    # 3. Terminate running processes for requested target app only
    terminate_antigravity_processes(app_type)

    # 4. Ensure workbench has openConversation command patched (Antigravity IDE only)
    if exe_path and app_type == 'ide':
        ensure_workbench_patched(str(exe_path))

    # 5. Clean write credentials while processes are dead
    try:
        acc_info = apply_credentials_post_kill(target, app_type=app_type)
        log(f"Credentials asserted post-kill for {acc_info['name']} ({acc_info['email']}).")
    except Exception as e:
        log(f"CRITICAL: Failed to apply credentials post-kill: {e}")

    # 6. Relaunch IDE / Antigravity 2.0 if applicable
    if not no_restart and exe_path and app_type != 'cli':
        time.sleep(0.6)
        relaunch_ide(exe_path, workspace, app_type)
    log("switch_runner supervisor completed successfully.")


if __name__ == "__main__":
    main()
