"""
account_switcher.py -- Authoritative Account Switcher & State Manager for Antigravity IDE
Supports real Google OAuth refresh, validated Unified State Sync protobufs, and local avatar caching.
Full parity with lbjlaq/Antigravity-Manager.
"""

from __future__ import annotations
import os
import platform
import sys
import json
import sqlite3
import base64
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
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

DAILY_COMPANION_HOST = "https://daily-cloudcode-pa.googleapis.com"
PROD_COMPANION_HOST = "https://cloudcode-pa.googleapis.com"
IDE_CLIENT_VERSION = "2.6.0"
LOAD_CODE_ASSIST_BODY = json.dumps({
    "metadata": {
        "ideType": "ANTIGRAVITY",
        "ideName": "antigravity",
        "ideVersion": IDE_CLIENT_VERSION,
    }
}).encode("utf-8")

OAUTH_CLIENTS_FILE = AG_TOOLS_DIR / "oauth_clients.json"
_OAUTH_CLIENT_ID_RE = re.compile(rb"[0-9]{10,}-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com")
_OAUTH_SECRET_PREFIX = b"GOC" + b"SPX-"
_OAUTH_SECRET_FULL_RE = re.compile(_OAUTH_SECRET_PREFIX + rb"[A-Za-z0-9_-]{28}")

SSL_CTX = ssl.create_default_context()
_INDEX_LOCK = threading.Lock()


def get_ide_user_agent() -> str:
    if sys.platform == "win32":
        go_os = "windows"
    elif sys.platform == "darwin":
        go_os = "darwin"
    else:
        go_os = "linux"
    mach = (platform.machine() or "").lower()
    if mach in ("amd64", "x86_64", "x64"):
        go_arch = "amd64"
    elif mach in ("arm64", "aarch64"):
        go_arch = "arm64"
    elif mach in ("x86", "i386", "i686", "ia32"):
        go_arch = "386"
    else:
        go_arch = mach or "amd64"
    return f"antigravity/{IDE_CLIENT_VERSION} {go_os}/{go_arch}"


def _get_active_proxy_url() -> str | None:
    if sys.platform == "win32":
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
                if int(enabled or 0) != 0:
                    server, _ = winreg.QueryValueEx(k, "ProxyServer")
                    raw = str(server or "").strip()
                    if "=" in raw:
                        m = re.search(r"(?:^|;)https?=([^;]+)", raw, re.I)
                        raw = m.group(1).strip() if m else ""
                    if raw:
                        if not raw.startswith(("http://", "https://", "socks5://")):
                            raw = "http://" + raw
                        return raw
            return None
        except Exception:
            pass
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _build_urllib_opener() -> urllib.request.OpenerDirector:
    proxy_url = _get_active_proxy_url()
    if proxy_url and proxy_url.startswith(("http://", "https://")):
        proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    else:
        proxy_handler = urllib.request.ProxyHandler({})
    return urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=SSL_CTX))


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


def _b64_decode_padded(val: str | bytes, urlsafe: bool = False) -> bytes:
    if isinstance(val, str):
        raw = val.strip().encode("ascii", errors="ignore")
    else:
        raw = val.strip()
    pad_len = (-len(raw)) % 4
    if pad_len:
        raw += b"=" * pad_len
    return base64.urlsafe_b64decode(raw) if urlsafe else base64.b64decode(raw)


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


def clean_name_from_account(acc_data: dict) -> str:
    """Extract clean display name, resolving corrupted encodings from JWT if necessary."""
    name = acc_data.get("name", "")
    email = acc_data.get("email", "")
    idt = acc_data.get("token", {}).get("id_token", "")
    if idt and (not name or "?" in name or "\ufffd" in name):
        try:
            parts = idt.split(".")
            if len(parts) >= 2:
                payload = json.loads(_b64_decode_padded(parts[1], urlsafe=True))
                jwt_name = payload.get("name")
                if jwt_name:
                    name = jwt_name
        except Exception:
            pass
    if not name:
        name = email.split("@")[0] if email else "Account"
    return name


def _find_account_by_token_or_email(refresh_token: str = "", email: str = "") -> dict | None:
    acc_dir = AG_TOOLS_DIR / "accounts"
    if not acc_dir.exists():
        return None
    rt_clean = (refresh_token or "").strip()
    em_clean = (email or "").strip().lower()
    email_match = None
    for f in acc_dir.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                cand = json.load(fp)
            if rt_clean and cand.get("token", {}).get("refresh_token") == rt_clean:
                return cand
            if not email_match and em_clean and cand.get("email", "").lower() == em_clean:
                email_match = cand
        except Exception:
            pass
    return email_match


def _extract_from_vscdb(db_path: Path) -> tuple[str, str]:
    """Extract refresh_token and email from nested base64 protobuf entries in state.vscdb."""
    rt = ""
    email = ""
    if not db_path.exists():
        return rt, email
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        c = conn.cursor()
        c.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.oauthToken'")
        row = c.fetchone()
        if row and row[0]:
            b1 = _b64_decode_padded(row[0])
            m = re.search(rb"1//0[A-Za-z0-9_-]+", b1)
            if m:
                rt = m.group(0).decode("utf-8", errors="ignore")
            if not rt:
                for inner in re.finditer(rb"[A-Za-z0-9+/]{24,}={0,2}", b1):
                    try:
                        b2 = _b64_decode_padded(inner.group(0))
                        m2 = re.search(rb"1//0[A-Za-z0-9_-]+", b2)
                        if m2:
                            rt = m2.group(0).decode("utf-8", errors="ignore")
                            break
                    except Exception:
                        pass
        c.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.userStatus'")
        row2 = c.fetchone()
        if row2 and row2[0]:
            b1 = _b64_decode_padded(row2[0])
            m = re.search(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", b1)
            if m:
                email = m.group(0).decode("utf-8", errors="ignore")
            if not email:
                for inner in re.finditer(rb"[A-Za-z0-9+/]{24,}={0,2}", b1):
                    try:
                        b2 = _b64_decode_padded(inner.group(0))
                        m2 = re.search(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", b2)
                        if m2:
                            email = m2.group(0).decode("utf-8", errors="ignore")
                            break
                    except Exception:
                        pass
        conn.close()
    except Exception:
        pass
    return rt, email


def _sync_index_active_id(key: str, acc_id: str) -> None:
    acc_index_file = AG_TOOLS_DIR / "accounts.json"
    if not acc_index_file.exists() or not acc_id:
        return
    try:
        with open(acc_index_file, "r", encoding="utf-8") as f:
            idx = json.load(f)
        if idx.get(key) != acc_id:
            idx[key] = acc_id
            _atomic_write_json(acc_index_file, idx)
    except Exception:
        pass


def _ensure_account_registered(
    refresh_token: str = "",
    email: str = "",
    name: str = "",
    id_token: str = "",
    app_target: str = "ide",
    set_active: bool = True,
) -> dict | None:
    """Auto-register or refresh a live Google account inside accounts.json."""
    em_clean = (email or "").strip().lower()
    rt_clean = (refresh_token or "").strip()
    display_name = (name or "").strip()
    if not display_name and id_token:
        try:
            parts = id_token.split(".")
            if len(parts) >= 2:
                payload = json.loads(_b64_decode_padded(parts[1], urlsafe=True))
                display_name = payload.get("name", "") or display_name
                if not em_clean and payload.get("email"):
                    em_clean = str(payload.get("email", "")).strip().lower()
        except Exception:
            pass

    matched = _find_account_by_token_or_email(rt_clean, em_clean)
    if matched:
        dirty = False
        tok = matched.setdefault("token", {})
        if rt_clean and tok.get("refresh_token") != rt_clean:
            tok["refresh_token"] = rt_clean
            tok["access_token"] = ""
            tok["expiry_timestamp"] = 0
            dirty = True
        if id_token and tok.get("id_token") != id_token:
            tok["id_token"] = id_token
            dirty = True
        if display_name and (not matched.get("name") or "?" in str(matched.get("name"))):
            matched["name"] = display_name
            dirty = True
        if dirty and matched.get("id"):
            try:
                _atomic_write_json(AG_TOOLS_DIR / "accounts" / f"{matched['id']}.json", matched)
            except Exception:
                pass
        return matched

    if not em_clean and not rt_clean:
        return None

    new_id = str(uuid.uuid4())
    if not display_name:
        display_name = em_clean.split("@")[0] if em_clean else "Account"

    now_ts = int(time.time())
    ide_ts = now_ts if app_target != "standalone" else 0
    sa_ts = now_ts if app_target == "standalone" else 0
    new_acc = {
        "id": new_id,
        "email": em_clean,
        "name": display_name,
        "disabled": False,
        "proxy_disabled": False,
        "created_at": now_ts,
        "last_used": ide_ts,
        "standalone_last_used": sa_ts,
        "token": {
            "refresh_token": rt_clean,
            "access_token": "",
            "expires_in": 3599,
            **({"id_token": id_token} if id_token else {}),
        }
    }
    try:
        q = fetch_live_quota(new_acc, update_index=False) or {}
        if isinstance(q, dict) and q.get("quota"):
            new_acc["quota"] = q.get("quota", {})
    except Exception:
        q = {"tier": "PRO", "claude_percentage": 0, "gemini_percentage": 100}

    detail_file = AG_TOOLS_DIR / "accounts" / f"{new_id}.json"
    AG_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    (AG_TOOLS_DIR / "accounts").mkdir(parents=True, exist_ok=True)
    _atomic_write_json(detail_file, new_acc)

    index_file = AG_TOOLS_DIR / "accounts.json"
    try:
        with _INDEX_LOCK:
            index_data = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {"accounts": []}
            item = {
                "id": new_id,
                "email": em_clean,
                "name": display_name,
                "disabled": False,
                "proxy_disabled": False,
                "created_at": now_ts,
                "last_used": ide_ts,
                "standalone_last_used": sa_ts,
                "tier": q.get("tier", "PRO"),
                "claude_percentage": q.get("claude_percentage", 0),
                "gemini_percentage": q.get("gemini_percentage", 100),
            }
            index_data.setdefault("accounts", []).append(item)
            if app_target == "standalone":
                if set_active or not index_data.get("current_standalone_account_id"):
                    index_data["current_standalone_account_id"] = new_id
            else:
                if set_active or not index_data.get("current_ide_account_id"):
                    index_data["current_ide_account_id"] = new_id
            _atomic_write_json(index_file, index_data)
    except Exception:
        pass
    return new_acc


def sync_live_auth_file() -> dict:
    """Write authoritative live auth state to ~/.antigravity_tools/live_auth.json for fast sync with Node.js."""
    try:
        sa = get_active_standalone_account()
        ide = get_active_ide_account()
        payload = {
            "standalone": {
                "id": sa.get("id"),
                "email": sa.get("email"),
                "name": sa.get("name"),
            },
            "ide": {
                "id": ide.get("id"),
                "email": ide.get("email"),
                "name": ide.get("name"),
            },
            "updated_at": int(time.time())
        }
        live_file = AG_TOOLS_DIR / "live_auth.json"
        AG_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(live_file, payload)
        return payload
    except Exception:
        return {}


_ANTIGRAVITY_OAUTH_SCOPES = (
    "openid "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile "
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/cclog "
    "https://www.googleapis.com/auth/experimentsandconfigs "
    "https://www.googleapis.com/auth/aicode"
)


def add_account_interactive(app_target: str = "ide", timeout_sec: int = 180) -> dict:
    """Open Google OAuth PKCE loopback login in the user's browser and register the account in accounts.json."""
    import hashlib
    import http.server
    import secrets
    import webbrowser

    clients = _get_oauth_clients()
    if not clients:
        return {"ok": False, "error": "OAuth-клиент Google не найден. Запустите Antigravity IDE или 2.0 хотя бы один раз."}

    client = next((c for c in clients if c.get("verified")), clients[0])
    cid = client.get("client_id", "")
    csec = client.get("client_secret", "")
    if not cid or not csec:
        return {"ok": False, "error": "Некорректные данные OAuth-клиента."}

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    result_box: dict = {"done": False, "code": "", "error": ""}
    done_ev = threading.Event()

    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/oauth-callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            err = (qs.get("error") or [""])[0]
            cb_state = (qs.get("state") or [""])[0]
            code = (qs.get("code") or [""])[0]
            if err:
                result_box["error"] = f"Авторизация отклонена: {err}"
                html = "<html><body style='font-family:sans-serif;background:#111;color:#f88;padding:40px;text-align:center'><h2>Ошибка входа</h2><p>Эту вкладку можно закрыть.</p></body></html>"
            elif cb_state != state or not code:
                result_box["error"] = "Некорректный state или отсутствует код авторизации."
                html = "<html><body style='font-family:sans-serif;background:#111;color:#f88;padding:40px;text-align:center'><h2>Некорректный ответ OAuth</h2><p>Эту вкладку можно закрыть.</p></body></html>"
            else:
                result_box["code"] = code
                html = "<html><body style='font-family:sans-serif;background:#111;color:#6f6;padding:40px;text-align:center'><h2>✅ Аккаунт добавлен в Antigravity!</h2><p style='color:#ccc'>Вкладку браузера можно закрыть и вернуться в приложение.</p><script>setTimeout(()=>window.close(),1500);</script></body></html>"
            result_box["done"] = True
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done_ev.set()

    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = 0.5
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/oauth-callback"

    auth_params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _ANTIGRAVITY_OAUTH_SCOPES,
        "access_type": "offline",
        "prompt": "consent select_account",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{auth_params}"

    srv_thread = threading.Thread(target=lambda: _serve_until_done(server, done_ev, timeout_sec), daemon=True)
    srv_thread.start()

    try:
        webbrowser.open(auth_url, new=1)
    except Exception as e:
        done_ev.set()
        try:
            server.server_close()
        except Exception:
            pass
        return {"ok": False, "error": f"Не удалось открыть браузер: {e}"}

    finished = done_ev.wait(timeout=timeout_sec)
    try:
        server.server_close()
    except Exception:
        pass

    if not finished or not result_box["code"]:
        return {
            "ok": False,
            "error": result_box["error"] or "Время ожидания входа истекло (180 сек).",
        }

    opener = _build_urllib_opener()
    token_data = None
    last_err = ""
    candidates = [client] + [c for c in clients if c is not client]
    for cand in candidates[:4]:
        c_id = cand.get("client_id", "")
        c_sec = cand.get("client_secret", "")
        if not c_id or not c_sec:
            continue
        try:
            post_body = urllib.parse.urlencode({
                "code": result_box["code"],
                "client_id": c_id,
                "client_secret": c_sec,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=post_body,
                headers={"User-Agent": get_ide_user_agent()},
            )
            with opener.open(req, timeout=12) as resp:
                token_data = json.loads(resp.read().decode("utf-8"))
                if token_data.get("access_token") or token_data.get("refresh_token"):
                    _persist_verified_oauth_client(c_id, c_sec)
                    break
        except Exception as exc:
            last_err = str(exc)
            continue

    if not token_data or not token_data.get("access_token"):
        return {"ok": False, "error": f"Не удалось обменять OAuth-код на токен: {last_err}"}

    acc_tok = token_data.get("access_token", "")
    ref_tok = token_data.get("refresh_token", "")
    id_tok = token_data.get("id_token", "")
    email = ""
    name = ""

    if id_tok:
        try:
            parts = id_tok.split(".")
            if len(parts) >= 2:
                claims = json.loads(_b64_decode_padded(parts[1], urlsafe=True))
                email = str(claims.get("email") or "").strip().lower()
                name = str(claims.get("name") or "").strip()
        except Exception:
            pass

    if (not email or not name) and acc_tok:
        try:
            ureq = urllib.request.Request(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {acc_tok}"},
            )
            with opener.open(ureq, timeout=8) as uresp:
                uinfo = json.loads(uresp.read().decode("utf-8"))
                email = email or str(uinfo.get("email") or "").strip().lower()
                name = name or str(uinfo.get("name") or "").strip()
        except Exception:
            pass

    if not email and not ref_tok:
        return {"ok": False, "error": "Google не вернул email и refresh_token."}

    # If refresh_token was already present in index for this email and consent omitted it, preserve existing one
    existing = _find_account_by_token_or_email(ref_tok, email)
    if existing and not ref_tok:
        ref_tok = (existing.get("token") or {}).get("refresh_token", "")

    acc = _ensure_account_registered(
        refresh_token=ref_tok,
        email=email,
        name=name,
        id_token=id_tok,
        app_target=app_target,
        set_active=False,
    )
    if not acc:
        return {"ok": False, "error": "Не удалось сохранить профиль аккаунта."}

    if acc_tok and email:
        try:
            avatar_url = fetch_and_cache_avatar(acc_tok, email)
            if avatar_url and acc.get("id"):
                det_path = AG_TOOLS_DIR / "accounts" / f"{acc['id']}.json"
                if det_path.exists():
                    det = json.loads(det_path.read_text(encoding="utf-8"))
                    det["avatar_url"] = avatar_url
                    _atomic_write_json(det_path, det)
        except Exception:
            pass

    try:
        fetch_live_quota(acc.get("id"), update_index=True)
    except Exception:
        pass
    sync_live_auth_file()

    all_accs = get_all_accounts(app_target=app_target)
    saved = next((a for a in all_accs if a.get("id") == acc.get("id")), None) or acc
    return {
        "ok": True,
        "account": {
            "id": saved.get("id"),
            "email": saved.get("email", email),
            "name": saved.get("name", name),
            "tier": saved.get("tier", "PRO"),
            "claude_percentage": saved.get("claude_percentage"),
            "gemini_percentage": saved.get("gemini_percentage"),
        },
        "total_accounts": len(all_accs),
    }


def _serve_until_done(server, done_ev: threading.Event, timeout_sec: int) -> None:
    deadline = time.time() + max(5, int(timeout_sec))
    while not done_ev.is_set() and time.time() < deadline:
        try:
            server.handle_request()
        except Exception:
            break


def _get_active_gemini_dir_account() -> dict | None:
    gemini_token_files = [
        GEMINI_DIR / "antigravity-cli" / "antigravity-oauth-token",
        GEMINI_DIR / "jetski-standalone-oauth-token",
        GEMINI_DIR / "oauth_creds.json",
        GEMINI_DIR / "google_accounts.json",
    ]
    for gtf in gemini_token_files:
        if not gtf.exists():
            continue
        try:
            gdata = json.loads(gtf.read_text(encoding="utf-8"))
            tok = gdata.get("token") if isinstance(gdata.get("token"), dict) else gdata
            ref_tok = tok.get("refresh_token", "") if isinstance(tok, dict) else ""
            em = gdata.get("id_token_email") or gdata.get("email") or gdata.get("active") or ""
            matched = _find_account_by_token_or_email(ref_tok, em)
            if not matched and (ref_tok or em):
                matched = _ensure_account_registered(
                    ref_tok, em,
                    id_token=gdata.get("id_token") or (tok.get("id_token") if isinstance(tok, dict) else ""),
                    app_target="ide"
                )
            if matched:
                return matched
        except Exception:
            pass
    return None


def _get_active_wincred_account(target_name: str = "gemini:antigravity") -> dict | None:
    if sys.platform != "win32":
        return None
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
        CredReadW = ctypes.windll.advapi32.CredReadW
        CredReadW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIAL)]
        CredReadW.restype = wintypes.BOOL
        CredFree = ctypes.windll.advapi32.CredFree
        CredFree.argtypes = [ctypes.c_void_p]
        CredFree.restype = None
        pcred = PCREDENTIAL()
        if CredReadW(target_name, 1, 0, ctypes.byref(pcred)):
            try:
                if not pcred.contents.CredentialBlob or pcred.contents.CredentialBlobSize <= 0:
                    return None
                b = bytes(ctypes.string_at(pcred.contents.CredentialBlob, pcred.contents.CredentialBlobSize))
            finally:
                CredFree(pcred)
            d = json.loads(b.decode('utf-8'))
            tok = d.get('token') if isinstance(d.get('token'), dict) else d
            ref_tok = tok.get('refresh_token', '') if isinstance(tok, dict) else ''
            em = d.get('id_token_email', '') or d.get('email', '')
            if not em and d.get('id_token'):
                parts = d['id_token'].split('.')
                if len(parts) >= 2:
                    payload = json.loads(_b64_decode_padded(parts[1], urlsafe=True))
                    em = payload.get('email', '')
            matched = _find_account_by_token_or_email(ref_tok, em)
            if not matched and (ref_tok or em):
                matched = _ensure_account_registered(
                    ref_tok, em,
                    id_token=d.get('id_token', ''),
                    app_target='standalone' if 'jetski' in target_name else 'ide'
                )
            if matched:
                return matched
    except Exception:
        pass
    return None


def _get_active_standalone_gemini_file() -> dict | None:
    gtf = GEMINI_DIR / "jetski-desktopapp-oauth-token"
    if not gtf.exists():
        return None
    try:
        gdata = json.loads(gtf.read_text(encoding="utf-8"))
        tok = gdata.get("token") if isinstance(gdata.get("token"), dict) else gdata
        ref_tok = tok.get("refresh_token", "") if isinstance(tok, dict) else ""
        em = gdata.get("id_token_email") or gdata.get("email") or gdata.get("active") or ""
        matched = _find_account_by_token_or_email(ref_tok, em)
        if not matched and (ref_tok or em):
            matched = _ensure_account_registered(
                ref_tok, em,
                id_token=gdata.get("id_token") or (tok.get("id_token") if isinstance(tok, dict) else ""),
                app_target="standalone"
            )
        if matched:
            return matched
    except Exception:
        pass
    return None


def _get_account_by_index_key(key: str) -> dict | None:
    acc_index_file = AG_TOOLS_DIR / "accounts.json"
    if not acc_index_file.exists():
        return None
    try:
        with open(acc_index_file, "r", encoding="utf-8") as f:
            index = json.load(f)
        target_id = index.get(key)
        if target_id:
            detail_file = AG_TOOLS_DIR / "accounts" / f"{target_id}.json"
            if detail_file.exists():
                with open(detail_file, "r", encoding="utf-8") as df:
                    return json.load(df)
        if key == "current_standalone_account_id" and index.get("accounts"):
            used_sa = [a for a in index["accounts"] if (a.get("standalone_last_used") or 0) > 0]
            used_sa.sort(key=lambda a: a.get("standalone_last_used", 0), reverse=True)
            if used_sa:
                cand_id = used_sa[0].get("id")
                detail_file = AG_TOOLS_DIR / "accounts" / f"{cand_id}.json"
                if detail_file.exists():
                    with open(detail_file, "r", encoding="utf-8") as df:
                        return json.load(df)
                return used_sa[0]
            return None
        if key == "current_account_id" and index.get("accounts"):
            used_ide = [a for a in index["accounts"] if (a.get("last_used") or 0) > 0]
            used_ide.sort(key=lambda a: a.get("last_used", 0), reverse=True)
            if used_ide:
                cand_id = used_ide[0].get("id")
                detail_file = AG_TOOLS_DIR / "accounts" / f"{cand_id}.json"
                if detail_file.exists():
                    with open(detail_file, "r", encoding="utf-8") as df:
                        return json.load(df)
                return used_ide[0]
            return None
    except Exception:
        pass
    return None


def _ide_uses_agy_hub() -> bool:
    if (GEMINI_DIR / "bin" / "agy.exe").exists():
        return True
    for ext_root in (HOME_DIR / ".vscode" / "extensions", HOME_DIR / ".antigravity-ide" / "extensions"):
        try:
            if ext_root.exists() and any(ext_root.glob("google.google-antigravity*")):
                return True
        except Exception:
            pass
    return False


def get_active_ide_account() -> dict:
    """Resolve authoritative active account for Antigravity IDE."""
    active_account = None

    # Check WinCred gemini:antigravity first
    active_account = _get_active_wincred_account("gemini:antigravity")

    # When google.google-antigravity (agy.exe --hub) is active, IDE chat uses ~/.gemini/
    if not active_account and _ide_uses_agy_hub():
        active_account = _get_active_gemini_dir_account()

    if not active_account:
        for db_path in (VSCDB_PATH, CODE_VSCDB_PATH):
            rt, em = _extract_from_vscdb(db_path)
            if rt or em:
                active_account = _find_account_by_token_or_email(rt, em)
                if active_account:
                    break

    if not active_account:
        active_account = _get_account_by_index_key("current_ide_account_id")

    if not active_account:
        active_account = _get_active_gemini_dir_account()

    if not active_account:
        active_account = _get_account_by_index_key("current_account_id")

    if not active_account:
        return {"id": None, "name": "Default", "email": "", "avatar_url": ""}

    if active_account.get("id"):
        _sync_index_active_id("current_ide_account_id", active_account["id"])

    name = clean_name_from_account(active_account)
    email = active_account.get("email", "")

    return {
        "id": active_account.get("id"),
        "name": name,
        "email": email,
        "avatar_url": active_account.get("avatar_url", ""),
        "quota": active_account.get("quota")
    }


def get_active_standalone_account() -> dict:
    """Resolve authoritative active account for Antigravity 2.0 (Standalone)."""
    # 1. Authoritative check on Windows: jetski:antigravity in Credential Manager
    active_account = _get_active_wincred_account("jetski:antigravity")

    # 2. Check token file
    if not active_account:
        active_account = _get_active_standalone_gemini_file()

    # 3. Check Roaming/Antigravity app_storage.json
    if not active_account:
        app_storage = Path(os.path.expandvars("%APPDATA%/Antigravity/app_storage.json"))
        if app_storage.exists():
            try:
                sdata = json.loads(app_storage.read_text(encoding="utf-8"))
                last_user = sdata.get("jetski.onboarding.lastLoginUsername") or sdata.get("agy.onboarding.lastLoginUsername")
                if last_user:
                    active_account = _find_account_by_token_or_email("", last_user)
            except Exception:
                pass

    # 4. Fallback to index key
    if not active_account:
        active_account = _get_account_by_index_key("current_standalone_account_id")

    if not active_account:
        return {"id": None, "name": "Default", "email": "", "avatar_url": ""}

    if active_account.get("id"):
        _sync_index_active_id("current_standalone_account_id", active_account["id"])

    name = clean_name_from_account(active_account)
    email = active_account.get("email", "")

    return {
        "id": active_account.get("id"),
        "name": name,
        "email": email,
        "avatar_url": active_account.get("avatar_url", ""),
        "quota": active_account.get("quota")
    }


def refresh_google_token(refresh_token: str) -> tuple[str, int, str] | None:
    if not refresh_token:
        return None
    clients = _get_oauth_clients()
    opener = _build_urllib_opener()
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
                req = urllib.request.Request(
                    "https://oauth2.googleapis.com/token",
                    data=data,
                    headers={"User-Agent": get_ide_user_agent()},
                )
                with opener.open(req, timeout=10) as resp:
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


def make_circular_png(src_bytes: bytes) -> bytes:
    """Crop image bytes to an antialiased circular PNG with transparent background."""
    try:
        from PIL import Image, ImageDraw
        import io
        img = Image.open(io.BytesIO(src_bytes)).convert("RGBA")
        size = min(img.size)
        if size <= 0:
            return src_bytes
        left = (img.width - size) // 2
        top = (img.height - size) // 2
        img = img.crop((left, top, left + size, top + size))

        # 4x supersampling for high-quality antialiased circular edge
        scale = 4
        mask = Image.new("L", (size * scale, size * scale), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, size * scale, size * scale), fill=255)
        mask = mask.resize((size, size), Image.Resampling.LANCZOS)

        img.putalpha(mask)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return src_bytes


def get_cached_avatar_path(email: str) -> str:
    """Return circular PNG avatar path, automatically converting legacy JPG if needed."""
    if not email:
        return ""
    png_file = AVATARS_DIR / f"{email}.png"
    jpg_file = AVATARS_DIR / f"{email}.jpg"
    if png_file.exists() and png_file.stat().st_size > 0:
        return str(png_file)
    if jpg_file.exists() and jpg_file.stat().st_size > 0:
        try:
            png_data = make_circular_png(jpg_file.read_bytes())
            png_file.write_bytes(png_data)
            return str(png_file)
        except Exception:
            return str(jpg_file)
    return ""


def fetch_and_cache_avatar(access_token: str, email: str) -> str:
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    local_avatar_file = AVATARS_DIR / f"{email}.jpg"
    local_png_file = AVATARS_DIR / f"{email}.png"
    opener = _build_urllib_opener()

    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        with opener.open(req, timeout=6) as resp:
            uinfo = json.loads(resp.read().decode("utf-8"))
            pic_url = uinfo.get("picture", "")
            if pic_url:
                pic_req = urllib.request.Request(pic_url, headers={"User-Agent": "Mozilla/5.0"})
                with opener.open(pic_req, timeout=6) as pr:
                    raw_bytes = pr.read()
                    local_avatar_file.write_bytes(raw_bytes)
                    try:
                        png_bytes = make_circular_png(raw_bytes)
                        local_png_file.write_bytes(png_bytes)
                    except Exception:
                        pass
                return pic_url
    except Exception:
        pass

    if local_png_file.exists() and local_png_file.stat().st_size > 0:
        return f"file:///{local_png_file.as_posix()}"
    if local_avatar_file.exists() and local_avatar_file.stat().st_size > 0:
        return f"file:///{local_avatar_file.as_posix()}"
    return ""


def _atomic_write_json(path: Path, data: dict | list) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
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


def parse_quota_groups(groups: list | None) -> dict:
    out = {
        "claude_pct": None,
        "claude_reset": None,
        "claude_limiting_bucket": None,
        "claude_weekly_pct": None,
        "claude_weekly_reset": None,
        "claude_5h_pct": None,
        "claude_5h_reset": None,
        "gemini_pct": None,
        "gemini_reset": None,
        "gemini_limiting_bucket": None,
        "gemini_weekly_pct": None,
        "gemini_weekly_reset": None,
        "gemini_5h_pct": None,
        "gemini_5h_reset": None,
        "argon_pct": None,
        "argon_reset": None,
        "argon_limiting_bucket": None,
        "argon_weekly_pct": None,
        "argon_weekly_reset": None,
        "argon_5h_pct": None,
        "argon_5h_reset": None,
        "gemini_groups": [],
    }
    if not isinstance(groups, list):
        return out

    for g in groups:
        if not isinstance(g, dict):
            continue
        dname = str(g.get("displayName") or g.get("display_name") or "")
        dname_low = dname.lower()
        buckets = g.get("buckets") or []

        # Classify Gemini strictly before Claude because both groups share weekly/5h windows
        is_gemini = ("gemini" in dname_low) or ("argon" in dname_low) or ("hydrogen" in dname_low) or any(
            any(k in str((b or {}).get("bucketId") or (b or {}).get("bucket_id") or "").lower() for k in ("gemini", "argon", "hydrogen"))
            for b in buckets if isinstance(b, dict)
        )
        is_claude = (not is_gemini) and (
            "claude" in dname_low
            or "3p" in dname_low
            or "gpt" in dname_low
            or any(
                "3p" in str((b or {}).get("bucketId") or (b or {}).get("bucket_id") or "").lower()
                or str((b or {}).get("window") or "").lower() in ("weekly", "5h")
                for b in buckets if isinstance(b, dict)
            )
        )

        if not is_gemini and not is_claude:
            continue

        all_cands = []
        enabled_cands = []
        weekly_pct = None
        weekly_reset = None
        h5_pct = None
        h5_reset = None
        h5_bid = ""

        for b in buckets:
            if not isinstance(b, dict):
                continue
            rf = b.get("remainingFraction")
            if rf is None:
                rf = b.get("remaining_fraction")
            if rf is None:
                continue
            try:
                rf_val = float(rf)
            except (TypeError, ValueError):
                continue
            pct_val = max(0, min(100, round(rf_val * 100)))
            r_time = b.get("resetTime") or b.get("reset_time")
            bid = str(b.get("bucketId") or b.get("bucket_id") or b.get("window") or "")
            bid_low = bid.lower()
            win_low = str(b.get("window") or "").lower()

            if win_low == "weekly" or bid_low.endswith("-weekly") or bid_low == "weekly":
                if not b.get("disabled") or weekly_pct is None:
                    weekly_pct = pct_val
                    weekly_reset = r_time
            elif win_low == "5h" or bid_low.endswith("-5h") or bid_low == "5h":
                if not b.get("disabled"):
                    h5_pct = pct_val
                    h5_reset = r_time
                    h5_bid = bid

            entry = (rf_val, pct_val, r_time, bid)
            all_cands.append(entry)
            if not b.get("disabled"):
                enabled_cands.append(entry)

        cands = enabled_cands or all_cands
        if not cands:
            continue
        limiting = min(cands, key=lambda x: x[0])
        use_5h = (h5_pct is not None) and (weekly_pct is None or weekly_pct > 0)
        primary_pct = h5_pct if use_5h else limiting[1]
        primary_reset = h5_reset if use_5h else limiting[2]
        primary_bid = (h5_bid or limiting[3]) if use_5h else limiting[3]

        if is_gemini:
            is_argon = ("argon" in dname_low) or any(
                "argon" in str((b or {}).get("bucketId") or (b or {}).get("bucket_id") or "").lower()
                for b in buckets if isinstance(b, dict)
            )
            is_main_gem = (not is_argon) and (
                "gemini models" in dname_low
                or "hydrogen" in dname_low
                or any(
                    str((b or {}).get("bucketId") or (b or {}).get("bucket_id") or "").lower().startswith("gemini-")
                    for b in buckets if isinstance(b, dict)
                )
            )
            out["gemini_groups"].append({
                "id": "gemini_argon" if is_argon else ("gemini_models" if is_main_gem else f"gemini_{len(out['gemini_groups'])}"),
                "display_name": dname or ("Gemini 4 Argon" if is_argon else "Gemini Models"),
                "short_label": "Argon" if is_argon else "Gemini",
                "description": str(g.get("description") or ""),
                "pct": primary_pct,
                "reset_time": primary_reset,
                "limiting_bucket": primary_bid or None,
                "weekly_pct": weekly_pct,
                "weekly_reset": weekly_reset,
                "h5_pct": h5_pct,
                "h5_reset": h5_reset,
                "is_argon": is_argon,
                "is_main": is_main_gem,
            })
            if is_argon:
                out["argon_pct"] = primary_pct
                out["argon_reset"] = primary_reset
                out["argon_limiting_bucket"] = primary_bid or None
                out["argon_weekly_pct"] = weekly_pct
                out["argon_weekly_reset"] = weekly_reset
                out["argon_5h_pct"] = h5_pct
                out["argon_5h_reset"] = h5_reset
            elif out["gemini_pct"] is None or is_main_gem:
                out["gemini_pct"] = primary_pct
                out["gemini_reset"] = primary_reset
                out["gemini_limiting_bucket"] = primary_bid or None
                out["gemini_weekly_pct"] = weekly_pct
                out["gemini_weekly_reset"] = weekly_reset
                out["gemini_5h_pct"] = h5_pct
                out["gemini_5h_reset"] = h5_reset
        else:
            out["claude_pct"] = primary_pct
            out["claude_reset"] = primary_reset
            out["claude_limiting_bucket"] = primary_bid or None
            out["claude_weekly_pct"] = weekly_pct
            out["claude_weekly_reset"] = weekly_reset
            out["claude_5h_pct"] = h5_pct
            out["claude_5h_reset"] = h5_reset

    if out["gemini_pct"] is None and out["argon_pct"] is not None:
        out["gemini_pct"] = out["argon_pct"]
        out["gemini_reset"] = out["argon_reset"]
        out["gemini_limiting_bucket"] = out["argon_limiting_bucket"]
        out["gemini_weekly_pct"] = out["argon_weekly_pct"]
        out["gemini_weekly_reset"] = out["argon_weekly_reset"]
        out["gemini_5h_pct"] = out["argon_5h_pct"]
        out["gemini_5h_reset"] = out["argon_5h_reset"]

    return out


def compute_quota_summary(detail: dict | None, index_item: dict | None = None) -> dict:
    detail = detail or {}
    q = detail.get("quota") or {}
    groups = q.get("quota_groups") or []
    models = q.get("models") or []

    parsed = parse_quota_groups(groups)
    claude_pct = parsed["claude_pct"]
    claude_reset_time = parsed["claude_reset"]
    claude_limiting_bucket = parsed["claude_limiting_bucket"] or q.get("claude_limiting_bucket")
    gemini_pct = parsed["gemini_pct"]
    gemini_reset_time = parsed["gemini_reset"]
    gemini_limiting_bucket = parsed["gemini_limiting_bucket"] or q.get("gemini_limiting_bucket")
    argon_pct = parsed["argon_pct"]
    argon_reset_time = parsed["argon_reset"]
    argon_limiting_bucket = parsed["argon_limiting_bucket"] or q.get("argon_limiting_bucket")

    gem_model_cands = []
    argon_model_cands = []
    claude_model_cands = []
    for m in models:
        mname = (m.get("name") or "").lower()
        dname = (m.get("display_name") or "").lower()
        pct = m.get("percentage")
        if pct is None:
            continue
        if (
            "2.5" in mname
            or "2.5" in dname
            or "flash-lite" in mname
            or "flash_lite" in mname
            or "-low" in mname
            or "-medium" in mname
        ):
            continue
        if "argon" in mname or "argon" in dname:
            argon_model_cands.append((int(pct), m.get("reset_time")))
        elif "gemini" in mname:
            gem_model_cands.append((int(pct), m.get("reset_time")))
        elif "claude" in mname:
            claude_model_cands.append((int(pct), m.get("reset_time")))

    if gemini_pct is None and gem_model_cands:
        gemini_pct, gemini_reset_time = min(gem_model_cands, key=lambda x: x[0])

    if argon_pct is None:
        if q.get("argon_percentage") is not None:
            argon_pct = int(q["argon_percentage"])
            argon_reset_time = argon_reset_time or q.get("argon_reset_time")
        elif detail.get("argon_percentage") is not None:
            argon_pct = int(detail["argon_percentage"])
            argon_reset_time = argon_reset_time or detail.get("argon_reset_time")
        elif argon_model_cands:
            argon_pct, argon_reset_time = min(argon_model_cands, key=lambda x: x[0])
        elif index_item and index_item.get("argon_percentage") is not None:
            argon_pct = int(index_item["argon_percentage"])
            argon_reset_time = argon_reset_time or index_item.get("argon_reset_time")

    if not argon_reset_time:
        argon_reset_time = q.get("argon_reset_time") or detail.get("argon_reset_time") or (index_item.get("argon_reset_time") if index_item else None)

    gemini_groups = list(parsed["gemini_groups"])
    if argon_pct is not None and not any(g.get("is_argon") for g in gemini_groups):
        gemini_groups.append({
            "id": "gemini_argon",
            "display_name": "Gemini 4 Argon",
            "short_label": "Argon",
            "description": "",
            "pct": argon_pct,
            "reset_time": argon_reset_time,
            "limiting_bucket": argon_limiting_bucket,
            "weekly_pct": q.get("argon_weekly_percentage", detail.get("argon_weekly_percentage")),
            "weekly_reset": q.get("argon_weekly_reset_time", detail.get("argon_weekly_reset_time")),
            "h5_pct": q.get("argon_5h_percentage", detail.get("argon_5h_percentage")),
            "h5_reset": q.get("argon_5h_reset_time", detail.get("argon_5h_reset_time")),
            "is_argon": True,
            "is_main": False,
        })

    # Fallbacks ONLY when quota_groups is empty/missing (never mix stale caches with fresh groups)
    if claude_pct is None:
        if q.get("claude_percentage") is not None:
            claude_pct = int(q["claude_percentage"])
        elif detail.get("claude_percentage") is not None:
            claude_pct = int(detail["claude_percentage"])
        elif claude_model_cands:
            claude_pct, claude_reset_time = min(claude_model_cands, key=lambda x: x[0])
        elif index_item and index_item.get("claude_percentage") is not None:
            claude_pct = int(index_item["claude_percentage"])

    if not claude_reset_time:
        claude_reset_time = q.get("claude_reset_time") or detail.get("claude_reset_time")

    if gemini_pct is None:
        if q.get("gemini_percentage") is not None:
            gemini_pct = int(q["gemini_percentage"])
        elif detail.get("gemini_percentage") is not None:
            gemini_pct = int(detail["gemini_percentage"])
        elif index_item and index_item.get("gemini_percentage") is not None:
            gemini_pct = int(index_item["gemini_percentage"])

    if not gemini_reset_time:
        gemini_reset_time = q.get("gemini_reset_time") or detail.get("gemini_reset_time")

    tier = (
        q.get("subscription_tier")
        or detail.get("tier")
        or (index_item.get("tier") if index_item else "")
        or ""
    )

    return {
        "tier": tier,
        "claude_percentage": claude_pct,
        "gemini_percentage": gemini_pct,
        "claude_reset_time": claude_reset_time,
        "gemini_reset_time": gemini_reset_time,
        "claude_limiting_bucket": claude_limiting_bucket,
        "claude_weekly_percentage": parsed["claude_weekly_pct"],
        "claude_weekly_reset_time": parsed["claude_weekly_reset"],
        "claude_5h_percentage": parsed["claude_5h_pct"],
        "claude_5h_reset_time": parsed["claude_5h_reset"],
        "gemini_limiting_bucket": gemini_limiting_bucket,
        "gemini_weekly_percentage": parsed["gemini_weekly_pct"],
        "gemini_weekly_reset_time": parsed["gemini_weekly_reset"],
        "gemini_5h_percentage": parsed["gemini_5h_pct"],
        "gemini_5h_reset_time": parsed["gemini_5h_reset"],
        "argon_percentage": argon_pct,
        "argon_reset_time": argon_reset_time,
        "argon_limiting_bucket": argon_limiting_bucket,
        "argon_weekly_percentage": parsed["argon_weekly_pct"] if parsed["argon_weekly_pct"] is not None else q.get("argon_weekly_percentage", detail.get("argon_weekly_percentage")),
        "argon_weekly_reset_time": parsed["argon_weekly_reset"] or q.get("argon_weekly_reset_time") or detail.get("argon_weekly_reset_time"),
        "argon_5h_percentage": parsed["argon_5h_pct"] if parsed["argon_5h_pct"] is not None else q.get("argon_5h_percentage", detail.get("argon_5h_percentage")),
        "argon_5h_reset_time": parsed["argon_5h_reset"] or q.get("argon_5h_reset_time") or detail.get("argon_5h_reset_time"),
        "gemini_groups": gemini_groups,
    }


AICODE_REMOTE_QUOTA_URL = "https://aicode.googleapis.com/v1alpha/remote:getUserQuotaSummary"


def _quota_group_key(g: dict, fallback_idx: int) -> str:
    buckets = g.get("buckets") or []
    dname_low = str(g.get("displayName") or g.get("display_name") or "").lower()
    has_argon = ("argon" in dname_low) or any(
        "argon" in str((b or {}).get("bucketId") or (b or {}).get("bucket_id") or "").lower()
        for b in buckets if isinstance(b, dict)
    )
    if has_argon:
        return "argon"
    if "gemini" in dname_low or "hydrogen" in dname_low:
        return "gemini_models"
    if "claude" in dname_low or "gpt" in dname_low or "3p" in dname_low:
        return "claude"
    return dname_low or f"group_{fallback_idx}"


def _merge_quota_groups(primary_groups: list, fallback_groups: list) -> list:
    merged = []
    seen = set()
    for src in (primary_groups, fallback_groups):
        if not isinstance(src, list):
            continue
        for g in src:
            if not isinstance(g, dict):
                continue
            key = _quota_group_key(g, len(merged))
            if key in seen:
                continue
            seen.add(key)
            merged.append(g)
    return merged


def _fetch_remote_quota_summary(
    headers: dict,
    timeout: float = 3.5,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[list, int]:
    get_headers = {
        "Authorization": headers.get("Authorization", ""),
        "User-Agent": headers.get("User-Agent") or get_ide_user_agent(),
    }
    req = urllib.request.Request(AICODE_REMOTE_QUOTA_URL, headers=get_headers)
    active_opener = opener or _build_urllib_opener()
    try:
        with active_opener.open(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
            resp_obj = ((parsed or {}).get("summary") or {}).get("response") or (parsed or {}).get("response") or parsed or {}
            groups = resp_obj.get("groups")
            return (groups if isinstance(groups, list) else []), resp.status
    except urllib.error.HTTPError as he:
        return [], he.code
    except Exception:
        return [], 0


def _post_companion_json(
    endpoint: str,
    headers: dict,
    timeout: float = 3.5,
    host: str = DAILY_COMPANION_HOST,
    body: bytes = b"{}",
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[dict, int]:
    clean_host = (host or DAILY_COMPANION_HOST).rstrip("/")
    req = urllib.request.Request(
        f"{clean_host}/v1internal:{endpoint}",
        data=body if body is not None else b"{}",
        headers=headers,
    )
    active_opener = opener or _build_urllib_opener()
    try:
        with active_opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as he:
        return {}, he.code
    except Exception:
        return {}, 0


def _update_accounts_index(updates_by_id: dict[str, dict]) -> None:
    if not updates_by_id:
        return
    with _INDEX_LOCK:
        try:
            acc_index = AG_TOOLS_DIR / "accounts.json"
            if not acc_index.exists():
                return
            idx_data = json.loads(acc_index.read_text(encoding="utf-8"))
            changed = False
            for item in idx_data.get("accounts", []):
                upd = updates_by_id.get(item.get("id"))
                if upd:
                    item["claude_percentage"] = upd["claude_percentage"]
                    item["gemini_percentage"] = upd["gemini_percentage"]
                    if upd.get("argon_percentage") is not None:
                        item["argon_percentage"] = upd["argon_percentage"]
                    else:
                        item.pop("argon_percentage", None)
                    if upd.get("argon_reset_time"):
                        item["argon_reset_time"] = upd["argon_reset_time"]
                    else:
                        item.pop("argon_reset_time", None)
                    if upd.get("tier"):
                        item["tier"] = upd["tier"]
                    changed = True
            if changed:
                _atomic_write_json(acc_index, idx_data)
        except Exception:
            pass


def fetch_live_quota(detail_file_or_id, update_index: bool = True, include_tier: bool | None = None):
    """Fetch real-time quota metrics concurrently from Google Cloud Code Companion APIs."""
    try:
        if isinstance(detail_file_or_id, (str, Path)):
            p = Path(detail_file_or_id)
            if not p.exists():
                p = AG_TOOLS_DIR / "accounts" / f"{detail_file_or_id}.json"
            if not p.exists():
                return None
            with open(p, "r", encoding="utf-8") as f:
                detail = json.load(f)
            dest_file = p
        elif isinstance(detail_file_or_id, dict):
            detail = detail_file_or_id
            dest_file = AG_TOOLS_DIR / "accounts" / f"{detail.get('id')}.json"
        else:
            return None

        tok = detail.setdefault("token", {})
        acc_tok = tok.get("access_token", "")
        ref_tok = tok.get("refresh_token", "")
        exp_ts = tok.get("expiry_timestamp", 0)
        opener = _build_urllib_opener()

        # Proactively refresh if missing or within 120s of expiry
        if (not acc_tok or not exp_ts or time.time() > (exp_ts - 120)) and ref_tok:
            ref_res = refresh_google_token(ref_tok)
            if ref_res:
                acc_tok, exp_in, id_tok = ref_res
                tok["access_token"] = acc_tok
                tok["expires_in"] = exp_in
                tok["expiry_timestamp"] = int(time.time()) + exp_in
                if id_tok:
                    tok["id_token"] = id_tok

        if not acc_tok:
            return None

        em_avatar = (detail.get("email") or "").strip().lower()
        if em_avatar and not get_cached_avatar_path(em_avatar):
            try:
                pic = fetch_and_cache_avatar(acc_tok, em_avatar)
                if pic:
                    detail["avatar_url"] = pic
            except Exception:
                pass

        cached_q = detail.get("quota") or {}
        has_gcp_tos_flag = isinstance(cached_q.get("uses_gcp_tos"), bool)
        cached_tier = cached_q.get("subscription_tier") or detail.get("tier") or ""
        last_lu = int(cached_q.get("last_updated") or 0)
        if include_tier is None:
            fetch_tier = (not has_gcp_tos_flag) or (not cached_tier) or ((time.time() - last_lu) > 86400)
        else:
            fetch_tier = bool(include_tier)
        preferred_host = PROD_COMPANION_HOST if cached_q.get("uses_gcp_tos") else DAILY_COMPANION_HOST
        has_remote_flag = isinstance(cached_q.get("remote_quota_supported"), bool)
        remote_checked_at = int(cached_q.get("remote_quota_checked_at") or 0)
        should_query_remote = (
            (bool(cached_q.get("remote_quota_supported")) or ((time.time() - remote_checked_at) > 21600) or (include_tier is True))
            if has_remote_flag
            else True
        )
        skip_companion_on_primary = has_remote_flag and bool(cached_q.get("remote_quota_supported")) and has_gcp_tos_flag and (not fetch_tier)

        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {acc_tok}",
                "Content-Type": "application/json",
                "User-Agent": get_ide_user_agent(),
            }

            pool_workers = (1 if skip_companion_on_primary else 2) + (1 if fetch_tier else 0) + (1 if should_query_remote else 0)
            with ThreadPoolExecutor(max_workers=max(1, pool_workers)) as pool:
                f_qs = None if skip_companion_on_primary else pool.submit(_post_companion_json, "retrieveUserQuotaSummary", headers, 3.5, preferred_host, b"{}", opener)
                f_ca = pool.submit(_post_companion_json, "loadCodeAssist", headers, 3.5, preferred_host, LOAD_CODE_ASSIST_BODY, opener) if fetch_tier else None
                f_remote = pool.submit(_fetch_remote_quota_summary, headers, 3.5, opener) if should_query_remote else None

                qs_data, qs_code = f_qs.result() if f_qs else ({}, 200)
                ca_data, ca_code = f_ca.result() if f_ca else ({}, 200)
                remote_groups, remote_code = f_remote.result() if f_remote else ([], 0)

            if attempt == 0 and ref_tok and (qs_code in (401, 403) or ca_code in (401, 403) or remote_code == 401):
                ref_res = refresh_google_token(ref_tok)
                if ref_res:
                    acc_tok, exp_in, id_tok = ref_res
                    tok["access_token"] = acc_tok
                    tok["expires_in"] = exp_in
                    tok["expiry_timestamp"] = int(time.time()) + exp_in
                    if id_tok:
                        tok["id_token"] = id_tok
                    continue

            if fetch_tier and not ca_data:
                alt_host = PROD_COMPANION_HOST if preferred_host == DAILY_COMPANION_HOST else DAILY_COMPANION_HOST
                alt_ca, alt_ca_code = _post_companion_json("loadCodeAssist", headers, 3.5, alt_host, LOAD_CODE_ASSIST_BODY, opener)
                if attempt == 0 and ref_tok and alt_ca_code in (401, 403):
                    ref_res = refresh_google_token(ref_tok)
                    if ref_res:
                        acc_tok, exp_in, id_tok = ref_res
                        tok["access_token"] = acc_tok
                        tok["expires_in"] = exp_in
                        tok["expiry_timestamp"] = int(time.time()) + exp_in
                        if id_tok:
                            tok["id_token"] = id_tok
                        continue
                if alt_ca:
                    ca_data = alt_ca

            uses_gcp_tos = (
                bool((ca_data.get("paidTier") or {}).get("usesGcpTos") is True)
                if ca_data
                else (bool(cached_q.get("uses_gcp_tos")) if has_gcp_tos_flag else False)
            )
            target_host = PROD_COMPANION_HOST if uses_gcp_tos else DAILY_COMPANION_HOST

            groups = (qs_data or {}).get("groups") or []
            need_companion_fallback = skip_companion_on_primary and (not remote_groups)
            if target_host != preferred_host or need_companion_fallback or (not groups and not remote_groups and not has_gcp_tos_flag):
                retry_host = target_host if target_host != preferred_host else (PROD_COMPANION_HOST if preferred_host == DAILY_COMPANION_HOST else DAILY_COMPANION_HOST)
                retry_qs, retry_code = _post_companion_json("retrieveUserQuotaSummary", headers, 3.5, retry_host, b"{}", opener)
                if attempt == 0 and ref_tok and retry_code in (401, 403):
                    ref_res = refresh_google_token(ref_tok)
                    if ref_res:
                        acc_tok, exp_in, id_tok = ref_res
                        tok["access_token"] = acc_tok
                        tok["expires_in"] = exp_in
                        tok["expiry_timestamp"] = int(time.time()) + exp_in
                        if id_tok:
                            tok["id_token"] = id_tok
                        continue
                if retry_qs and retry_qs.get("groups"):
                    qs_data = retry_qs
                    groups = retry_qs.get("groups") or []

            tier_str = cached_tier or "PRO"
            if ca_data:
                paid_tier = ca_data.get("paidTier") or {}
                cur_tier = ca_data.get("currentTier") or {}
                paid_id = (paid_tier.get("id") or "").lower()
                cur_id = (cur_tier.get("id") or "").lower()
                if "ultra" in paid_id or "ultra" in cur_id:
                    tier_str = "ULTRA"
                elif "pro" in paid_id or "pro" in cur_id:
                    tier_str = "PRO"
                elif paid_tier.get("name"):
                    tier_str = paid_tier.get("name")
                elif "free" in cur_id:
                    tier_str = "FREE"
                elif "standard" in cur_id:
                    tier_str = "PRO"

            groups = _merge_quota_groups(remote_groups, groups)
            parsed_groups = parse_quota_groups(groups)
            claude_pct = parsed_groups["claude_pct"]
            claude_reset_time = parsed_groups["claude_reset"]
            claude_limiting_bucket = parsed_groups["claude_limiting_bucket"]
            gemini_pct = parsed_groups["gemini_pct"]
            gemini_reset_time = parsed_groups["gemini_reset"]
            gemini_limiting_bucket = parsed_groups["gemini_limiting_bucket"]
            argon_pct = parsed_groups["argon_pct"]
            argon_reset_time = parsed_groups["argon_reset"]
            argon_limiting_bucket = parsed_groups["argon_limiting_bucket"]

            models_data = {}
            if gemini_pct is None or claude_pct is None:
                models_data, m_code = _post_companion_json("fetchAvailableModels", headers, 3.5, target_host, b"{}", opener)
                if attempt == 0 and ref_tok and m_code in (401, 403):
                    ref_res = refresh_google_token(ref_tok)
                    if ref_res:
                        acc_tok, exp_in, id_tok = ref_res
                        tok["access_token"] = acc_tok
                        tok["expires_in"] = exp_in
                        tok["expiry_timestamp"] = int(time.time()) + exp_in
                        if id_tok:
                            tok["id_token"] = id_tok
                        continue

                gem_models = (models_data or {}).get("models", {})
                if isinstance(gem_models, dict):
                    if argon_pct is None:
                        argon_cands = []
                        for mkey, mval in gem_models.items():
                            ml = mkey.lower()
                            if "argon" in ml and isinstance(mval, dict):
                                qi = mval.get("quotaInfo", {})
                                rf = qi.get("remainingFraction")
                                if rf is not None and isinstance(rf, (int, float)):
                                    argon_cands.append((float(rf), qi.get("resetTime")))
                        if argon_cands:
                            min_arf, min_art = min(argon_cands, key=lambda x: x[0])
                            argon_pct = round(min_arf * 100)
                            argon_reset_time = min_art
                    if gemini_pct is None:
                        gem_cands = []
                        for mkey, mval in gem_models.items():
                            ml = mkey.lower()
                            if (
                                "gemini" in ml
                                and "argon" not in ml
                                and isinstance(mval, dict)
                                and "2.5" not in ml
                                and "flash-lite" not in ml
                                and "flash_lite" not in ml
                                and "image" not in ml
                                and "-low" not in ml
                                and "-medium" not in ml
                            ):
                                qi = mval.get("quotaInfo", {})
                                rf = qi.get("remainingFraction")
                                if rf is not None and isinstance(rf, (int, float)):
                                    prio = 0 if ("3.8" in ml or "3-flash" in ml) else 1
                                    gem_cands.append((float(rf), prio, qi.get("resetTime")))
                        if gem_cands:
                            min_g = min(gem_cands, key=lambda x: (x[0], x[1]))
                            gemini_pct = round(min_g[0] * 100)
                            gemini_reset_time = min_g[2]

                    if claude_pct is None:
                        cl_cands = []
                        for mkey, mval in gem_models.items():
                            if "claude" in mkey.lower() and isinstance(mval, dict):
                                qi = mval.get("quotaInfo", {})
                                rf = qi.get("remainingFraction")
                                if rf is not None and isinstance(rf, (int, float)):
                                    cl_cands.append((float(rf), qi.get("resetTime")))
                        if cl_cands:
                            min_rf, min_rt = min(cl_cands, key=lambda x: x[0])
                            claude_pct = round(min_rf * 100)
                            claude_reset_time = min_rt

            if not groups and not models_data:
                return None

            if gemini_pct is None:
                prev_g = cached_q.get("gemini_percentage")
                gemini_pct = int(prev_g) if prev_g is not None else 100
                gemini_reset_time = gemini_reset_time or cached_q.get("gemini_reset_time")
            if claude_pct is None:
                prev_c = cached_q.get("claude_percentage")
                claude_pct = int(prev_c) if prev_c is not None else 100
                claude_reset_time = claude_reset_time or cached_q.get("claude_reset_time")

            models_list = [
                {
                    "name": "gemini-3.8-flash",
                    "display_name": "Gemini 3.8 Flash",
                    "percentage": gemini_pct,
                    "reset_time": gemini_reset_time
                },
                {
                    "name": "claude-sonnet-4-6",
                    "display_name": "Claude Sonnet 4.6 (Thinking)",
                    "percentage": claude_pct,
                    "reset_time": claude_reset_time
                },
                {
                    "name": "claude-opus-4-6-thinking",
                    "display_name": "Claude Opus 4.6 (Thinking)",
                    "percentage": claude_pct,
                    "reset_time": claude_reset_time
                }
            ]
            if argon_pct is not None:
                models_list.append({
                    "name": "gemini-4-argon",
                    "display_name": "Gemini 4 Argon",
                    "percentage": argon_pct,
                    "reset_time": argon_reset_time,
                })

            remote_quota_supported = bool(remote_groups) if should_query_remote else bool(cached_q.get("remote_quota_supported"))
            if not detail.get("quota"):
                detail["quota"] = {}
            detail["quota"]["subscription_tier"] = tier_str
            detail["quota"]["uses_gcp_tos"] = uses_gcp_tos
            detail["quota"]["remote_quota_supported"] = remote_quota_supported
            if should_query_remote or not detail["quota"].get("remote_quota_checked_at"):
                detail["quota"]["remote_quota_checked_at"] = int(time.time())
            detail["quota"]["models"] = models_list
            detail["quota"]["claude_percentage"] = claude_pct
            detail["quota"]["gemini_percentage"] = gemini_pct
            if argon_pct is not None:
                detail["quota"]["argon_percentage"] = argon_pct
                detail["quota"]["argon_reset_time"] = argon_reset_time
            else:
                detail["quota"].pop("argon_percentage", None)
                detail["quota"].pop("argon_reset_time", None)
            if groups:
                detail["quota"]["quota_groups"] = groups
            if claude_limiting_bucket:
                detail["quota"]["claude_limiting_bucket"] = claude_limiting_bucket
            else:
                detail["quota"].pop("claude_limiting_bucket", None)
            if gemini_limiting_bucket:
                detail["quota"]["gemini_limiting_bucket"] = gemini_limiting_bucket
            else:
                detail["quota"].pop("gemini_limiting_bucket", None)
            if argon_limiting_bucket:
                detail["quota"]["argon_limiting_bucket"] = argon_limiting_bucket
            else:
                detail["quota"].pop("argon_limiting_bucket", None)
            detail["quota"]["claude_reset_time"] = claude_reset_time
            detail["quota"]["gemini_reset_time"] = gemini_reset_time
            detail["quota"]["last_updated"] = int(time.time())

            detail["claude_percentage"] = claude_pct
            detail["gemini_percentage"] = gemini_pct
            if argon_pct is not None:
                detail["argon_percentage"] = argon_pct
                detail["argon_reset_time"] = argon_reset_time
            else:
                detail.pop("argon_percentage", None)
                detail.pop("argon_reset_time", None)
            detail["tier"] = tier_str

            if dest_file and dest_file.parent.exists():
                _atomic_write_json(dest_file, detail)

            summary = {
                "id": detail.get("id"),
                "email": detail.get("email"),
                "name": detail.get("name"),
                "tier": tier_str,
                "claude_percentage": claude_pct,
                "gemini_percentage": gemini_pct,
                "claude_reset_time": claude_reset_time,
                "gemini_reset_time": gemini_reset_time,
                "claude_limiting_bucket": claude_limiting_bucket,
                "claude_weekly_percentage": parsed_groups["claude_weekly_pct"],
                "claude_weekly_reset_time": parsed_groups["claude_weekly_reset"],
                "claude_5h_percentage": parsed_groups["claude_5h_pct"],
                "claude_5h_reset_time": parsed_groups["claude_5h_reset"],
                "gemini_limiting_bucket": gemini_limiting_bucket,
                "gemini_weekly_percentage": parsed_groups["gemini_weekly_pct"],
                "gemini_weekly_reset_time": parsed_groups["gemini_weekly_reset"],
                "gemini_5h_percentage": parsed_groups["gemini_5h_pct"],
                "gemini_5h_reset_time": parsed_groups["gemini_5h_reset"],
                "argon_percentage": argon_pct,
                "argon_reset_time": argon_reset_time,
                "argon_limiting_bucket": argon_limiting_bucket,
                "argon_weekly_percentage": parsed_groups["argon_weekly_pct"],
                "argon_weekly_reset_time": parsed_groups["argon_weekly_reset"],
                "argon_5h_percentage": parsed_groups["argon_5h_pct"],
                "argon_5h_reset_time": parsed_groups["argon_5h_reset"],
                "gemini_groups": parsed_groups["gemini_groups"],
                "quota": detail["quota"]
            }

            if update_index and detail.get("id"):
                _update_accounts_index({detail["id"]: summary})

            return summary
    except Exception:
        pass
    return None


def refresh_all_quotas():
    acc_dir = AG_TOOLS_DIR / "accounts"
    if not acc_dir.exists():
        return []
    files = list(acc_dir.glob("*.json"))
    if not files:
        return []
    workers = min(16, max(1, len(files)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda f: fetch_live_quota(f, update_index=False), files))
    valid = [r for r in results if r]
    updates = {r["id"]: r for r in valid if r.get("id")}
    if updates:
        _update_accounts_index(updates)
    return valid


def _build_active_payload(act: dict, app_target: str) -> dict:
    email = act.get("email", "")
    quota = act.get("quota")
    avatar_path = get_cached_avatar_path(email)
    ddata = {}

    if act.get("id"):
        df = AG_TOOLS_DIR / "accounts" / f"{act.get('id')}.json"
        if df.exists():
            try:
                with open(df, "r", encoding="utf-8") as dfp:
                    ddata = json.load(dfp)
                    if ddata.get("quota"):
                        quota = ddata.get("quota")
            except Exception:
                pass

    summary = compute_quota_summary({"quota": quota, **ddata})
    act["quota"] = quota
    act["tier"] = summary["tier"]
    act["claude_percentage"] = summary["claude_percentage"]
    act["gemini_percentage"] = summary["gemini_percentage"]
    act["claude_reset_time"] = summary["claude_reset_time"]
    act["gemini_reset_time"] = summary["gemini_reset_time"]
    act["claude_limiting_bucket"] = summary["claude_limiting_bucket"]
    act["claude_weekly_percentage"] = summary["claude_weekly_percentage"]
    act["claude_weekly_reset_time"] = summary["claude_weekly_reset_time"]
    act["claude_5h_percentage"] = summary["claude_5h_percentage"]
    act["claude_5h_reset_time"] = summary["claude_5h_reset_time"]
    act["gemini_limiting_bucket"] = summary["gemini_limiting_bucket"]
    act["gemini_weekly_percentage"] = summary["gemini_weekly_percentage"]
    act["gemini_weekly_reset_time"] = summary["gemini_weekly_reset_time"]
    act["gemini_5h_percentage"] = summary["gemini_5h_percentage"]
    act["gemini_5h_reset_time"] = summary["gemini_5h_reset_time"]
    act["argon_percentage"] = summary["argon_percentage"]
    act["argon_reset_time"] = summary["argon_reset_time"]
    act["argon_limiting_bucket"] = summary["argon_limiting_bucket"]
    act["argon_weekly_percentage"] = summary["argon_weekly_percentage"]
    act["argon_weekly_reset_time"] = summary["argon_weekly_reset_time"]
    act["argon_5h_percentage"] = summary["argon_5h_percentage"]
    act["argon_5h_reset_time"] = summary["argon_5h_reset_time"]
    act["gemini_groups"] = summary["gemini_groups"]
    act["avatar_path"] = avatar_path
    act["app_target"] = app_target
    return act


def refresh_active_quota(app_target: str = "ide") -> dict:
    act = get_active_standalone_account() if app_target == "standalone" else get_active_ide_account()
    acc_id = act.get("id")
    if acc_id:
        df = AG_TOOLS_DIR / "accounts" / f"{acc_id}.json"
        if df.exists():
            res = fetch_live_quota(df, update_index=True)
            if res and res.get("quota"):
                act["quota"] = res["quota"]
    return _build_active_payload(act, app_target)


def get_all_accounts(app_target: str = "ide") -> list[dict]:
    active = get_active_standalone_account() if app_target == "standalone" else get_active_ide_account()
    active_email = (active.get("email") or "").lower()
    active_id = active.get("id")

    accounts_file = AG_TOOLS_DIR / "accounts.json"
    result = []
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)

    if accounts_file.exists():
        try:
            with open(accounts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_accs = data.get("accounts", [])
            for a in raw_accs:
                acc_id = a.get("id")
                email = a.get("email", "")
                detail = {}
                detail_file = AG_TOOLS_DIR / "accounts" / f"{acc_id}.json"
                avatar_url = ""
                name = a.get("name") or ""

                if detail_file.exists():
                    try:
                        with open(detail_file, "r", encoding="utf-8") as df:
                            detail = json.load(df)
                            avatar_url = detail.get("avatar_url", "")
                            name = clean_name_from_account(detail)
                    except Exception:
                        pass

                summary = compute_quota_summary(detail, a)
                if not name:
                    name = clean_name_from_account(a)

                is_active = (acc_id == active_id) or (email.lower() == active_email if active_email else False)
                avatar_path = get_cached_avatar_path(email)

                result.append({
                    "id": acc_id,
                    "email": email,
                    "name": name,
                    "avatar_path": avatar_path,
                    "avatar_url": avatar_url,
                    "is_active": is_active,
                    "tier": summary["tier"],
                    "claude_percentage": summary["claude_percentage"],
                    "gemini_percentage": summary["gemini_percentage"],
                    "claude_reset_time": summary["claude_reset_time"],
                    "gemini_reset_time": summary["gemini_reset_time"],
                    "claude_limiting_bucket": summary["claude_limiting_bucket"],
                    "claude_weekly_percentage": summary["claude_weekly_percentage"],
                    "claude_weekly_reset_time": summary["claude_weekly_reset_time"],
                    "claude_5h_percentage": summary["claude_5h_percentage"],
                    "claude_5h_reset_time": summary["claude_5h_reset_time"],
                    "gemini_limiting_bucket": summary["gemini_limiting_bucket"],
                    "gemini_weekly_percentage": summary["gemini_weekly_percentage"],
                    "gemini_weekly_reset_time": summary["gemini_weekly_reset_time"],
                    "gemini_5h_percentage": summary["gemini_5h_percentage"],
                    "gemini_5h_reset_time": summary["gemini_5h_reset_time"],
                    "argon_percentage": summary["argon_percentage"],
                    "argon_reset_time": summary["argon_reset_time"],
                    "argon_limiting_bucket": summary["argon_limiting_bucket"],
                    "argon_weekly_percentage": summary["argon_weekly_percentage"],
                    "argon_weekly_reset_time": summary["argon_weekly_reset_time"],
                    "argon_5h_percentage": summary["argon_5h_percentage"],
                    "argon_5h_reset_time": summary["argon_5h_reset_time"],
                    "gemini_groups": summary["gemini_groups"],
                    "priority": a.get("priority", 50)
                })
        except Exception:
            pass

    return result


def save_active_conversation_if_needed():
    """Snapshots the active conversation ID from the latest modified brain transcript."""
    try:
        restore_file = AG_TOOLS_DIR / "restore_conversation.json"
        if restore_file.exists():
            return
        brain_dirs = [
            Path.home() / ".gemini" / "antigravity-ide" / "brain",
            Path.home() / ".gemini" / "antigravity" / "brain"
        ]
        latest_id = None
        latest_time = 0
        for bdir in brain_dirs:
            if not bdir.exists():
                continue
            for d in bdir.iterdir():
                if not d.is_dir():
                    continue
                log_p = d / ".system_generated" / "logs" / "transcript.jsonl"
                if log_p.exists():
                    mtime = log_p.stat().st_mtime
                    if mtime > latest_time:
                        latest_time = mtime
                        latest_id = d.name
        if latest_id and (time.time() - latest_time < 7200):
            AG_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            with open(restore_file, "w", encoding="utf-8") as f:
                json.dump({"cascadeId": latest_id, "timestamp": int(time.time() * 1000)}, f)
    except Exception:
        pass


def kill_standalone_language_server():
    """Kill Antigravity 2.0 language_server.exe cleanly and reliably."""
    psutil_checked = False
    try:
        import psutil
        psutil_checked = True
        for p in psutil.process_iter(['pid', 'name', 'exe']):
            try:
                n = (p.info['name'] or '').lower()
                exe = (p.info['exe'] or '').lower()
                if (
                    'language_server' in n
                    and 'antigravity ide' not in exe
                    and 'agy' not in n
                    and ('antigravity' in exe or 'programs' in exe or 'resources' in exe)
                ):
                    p.kill()
            except Exception:
                pass
    except Exception:
        pass

    if not psutil_checked:
        try:
            import subprocess
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/IM", "language_server.exe"],
                    capture_output=True,
                    creationflags=0x08000000,
                )
            else:
                subprocess.run(["pkill", "-f", "antigravity/resources/bin/language_server"], capture_output=True)
        except Exception:
            pass


def switch_to_account(target_id_or_email: str, app_type: str = "ide", restart: bool = True, workspace_path: str = "") -> dict:
    target_clean = target_id_or_email.strip().lower()
    accounts = get_all_accounts(app_target=app_type)
    target = None
    for a in accounts:
        if a["id"].lower() == target_clean or a.get("email", "").lower() == target_clean or a.get("name", "").lower() == target_clean:
            target = a
            break

    if not target:
        return {"ok": False, "error": f"Аккаунт '{target_id_or_email}' не найден."}

    if restart:
        if app_type != "standalone":
            save_active_conversation_if_needed()
        # Launch switch_runner.py using CREATE_NO_WINDOW so it retains interactive desktop connection
        runner_script = Path(__file__).parent / "switch_runner.py"
        cmd = [sys.executable, str(runner_script), "--target", target["id"]]
        if app_type in ("ide", "code", "standalone"):
            cmd.extend(["--app", app_type])
        if workspace_path:
            cmd.extend(["--workspace", workspace_path])

        # Purge any Electron/VS Code environment variables that poison external launches
        clean_env = os.environ.copy()
        for k in list(clean_env.keys()):
            uk = k.upper()
            if uk.startswith(('ELECTRON_', 'VSCODE_', 'ATOM_SHELL_')) or uk in ('NODE_OPTIONS', 'NODE_ENV', 'ORIGINAL_XDG_CURRENT_DESKTOP'):
                clean_env.pop(k, None)

        import subprocess
        flags = 0x08000000  # CREATE_NO_WINDOW
        if sys.platform == "win32":
            flags |= 0x00000200  # CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            cmd,
            env=clean_env,
            creationflags=flags,
            close_fds=True
        )
        res = {"ok": True, "name": target["name"], "email": target["email"], "avatar_url": target.get("avatar_url", ""), "restarting": True}
        if app_type == "standalone":
            res["app_type"] = "standalone"
        return res
    else:
        core_dir = str(Path(__file__).parent)
        if core_dir not in sys.path:
            sys.path.insert(0, core_dir)
        from switch_runner import apply_credentials_post_kill
        apply_credentials_post_kill(target["id"], app_type=app_type)
        res = {"ok": True, "name": target["name"], "email": target["email"], "avatar_url": target.get("avatar_url", ""), "restarting": False}
        if app_type == "standalone":
            res["app_type"] = "standalone"
        return res


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No command provided"}))
        return

    # Extract flags first
    app_target = "ide"
    if "--app" in sys.argv:
        idx = sys.argv.index("--app")
        if idx + 1 < len(sys.argv):
            app_target = sys.argv[idx + 1].lower()

    # Find the primary command (first non-flag argument)
    args = [a for a in sys.argv[1:] if not a.startswith("--") and sys.argv[sys.argv.index(a) - 1] != "--app"]
    if not args:
        print(json.dumps({"error": "No action specified"}))
        return

    action = args[0].lower()

    if action == "list":
        sync_live_auth_file()
        if "--refresh" in sys.argv:
            try:
                refresh_all_quotas()
            except Exception:
                pass
        accs = get_all_accounts(app_target=app_target)
        print(json.dumps(accs, ensure_ascii=False))

    elif action in ("refresh_quota", "refresh_quotas"):
        sync_live_auth_file()
        refresh_all_quotas()
        accs = get_all_accounts(app_target=app_target)
        print(json.dumps(accs, ensure_ascii=False))

    elif action in ("refresh_active", "refresh_active_quota"):
        sync_live_auth_file()
        res = refresh_active_quota(app_target=app_target)
        print(json.dumps(res, ensure_ascii=False))

    elif action == "get_active":
        sync_live_auth_file()
        act = get_active_standalone_account() if app_target == "standalone" else get_active_ide_account()
        print(json.dumps(_build_active_payload(act, app_target), ensure_ascii=False))

    elif action == "sync_live":
        live = sync_live_auth_file()
        accs = get_all_accounts(app_target=app_target)
        print(json.dumps({"ok": True, "live": live, "count": len(accs)}, ensure_ascii=False))

    elif action in ("add_account", "login"):
        res = add_account_interactive(app_target=app_target)
        print(json.dumps(res, ensure_ascii=False))

    elif action == "switch":
        if len(sys.argv) < 3:
            print(json.dumps({"ok": False, "error": "No target account specified"}))
            return
        target = sys.argv[2]
        restart = True
        app_target = "ide"
        workspace_path = ""
        skip_next = False
        for i, a in enumerate(sys.argv[3:], start=3):
            if skip_next:
                skip_next = False
                continue
            if a == "--app" and i + 1 < len(sys.argv):
                app_target = sys.argv[i + 1].lower()
                skip_next = True
            elif a == "--no-restart":
                restart = False
            elif a == "--restart":
                restart = True
            elif not a.startswith("--"):
                workspace_path = a

        res = switch_to_account(target, app_type=app_target, restart=restart, workspace_path=workspace_path)
        print(json.dumps(res, ensure_ascii=False))

    else:
        print(json.dumps({"error": f"Unknown action: {action}"}))


if __name__ == "__main__":
    main()
