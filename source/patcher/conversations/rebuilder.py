"""
patcher/conversations/rebuilder.py -- Rebuild & Repair Antigravity Chat History.

Fixes:
  - Missing conversations in the sidebar (empty chat list)
  - Scrambled ordering (not sorted chronologically by date)
  - Missing or placeholder titles ("New Chat", raw UUIDs)
  - Lost workspace associations (chats detached from project folders)
  - Disconnected chats after IDE updates or folder migrations

Features:
  - Zero-dependency custom Protobuf encoder/decoder (no pip packages needed)
  - 3-tier title resolution cascade: SQLite step payload -> Brain logs -> Brain markdown
  - Automatic workspace association via URI inference and workspaceStorage cross-matching
  - Strictly non-destructive: read-only immutable access to conversation .db files
  - Automatic timestamped backup and one-click rollback
  - Process guard to prevent file corruption if IDE is running
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import quote, unquote, urlparse

from patcher.utils.console import (
    ok, warn, err, info, hint, step, cancel, color,
    print_menu_section, print_menu_row, print_menu_footer,
    COLOR_CYAN, COLOR_RED, COLOR_YELLOW, COLOR_GREEN,
    COLOR_BOLD, COLOR_DIM, COLOR_WHITE,
)

_SYSTEM = platform.system()


def is_wsl() -> bool:
    """Detect if running inside Windows Subsystem for Linux."""
    if _SYSTEM != "Linux":
        return False
    if "microsoft" in platform.release().lower():
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            if "microsoft" in f.read().lower():
                return True
    except Exception:
        pass
    return False


_IS_WSL = is_wsl()


# ─── Path Resolution ─────────────────────────────────────────────────────────

def get_db_candidates() -> List[str]:
    """Return all known paths to state.vscdb across supported platforms."""
    candidates: List[str] = []
    if _SYSTEM == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.extend([
                os.path.join(appdata, "Antigravity IDE", "User", "globalStorage", "state.vscdb"),
                os.path.join(appdata, "antigravity", "User", "globalStorage", "state.vscdb"),
                os.path.join(appdata, "Antigravity", "User", "globalStorage", "state.vscdb"),
            ])
    elif _SYSTEM == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
        candidates.extend([
            os.path.join(base, "Antigravity IDE", "User", "globalStorage", "state.vscdb"),
            os.path.join(base, "antigravity", "User", "globalStorage", "state.vscdb"),
            os.path.join(base, "Antigravity", "User", "globalStorage", "state.vscdb"),
        ])
    else:
        cfg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        candidates.extend([
            os.path.join(cfg, "Antigravity IDE", "User", "globalStorage", "state.vscdb"),
            os.path.join(cfg, "antigravity", "User", "globalStorage", "state.vscdb"),
            os.path.join(cfg, "Antigravity", "User", "globalStorage", "state.vscdb"),
        ])
    return [c for c in candidates if os.path.isfile(c)]


def get_all_conversation_dirs() -> List[str]:
    """Return all directories where conversation files (.db / .pb) might reside."""
    home = os.path.expanduser("~")
    gemini = os.path.join(home, ".gemini")
    candidates = [
        os.path.join(gemini, "antigravity-ide", "conversations"),
        os.path.join(gemini, "antigravity", "conversations"),
        os.path.join(gemini, "antigravity-backup", "conversations"),
    ]
    return [d for d in candidates if os.path.isdir(d)]


def get_all_brain_dirs() -> List[str]:
    """Return all directories where agent brain artifacts reside."""
    home = os.path.expanduser("~")
    gemini = os.path.join(home, ".gemini")
    candidates = [
        os.path.join(gemini, "antigravity-ide", "brain"),
        os.path.join(gemini, "antigravity", "brain"),
        os.path.join(gemini, "antigravity-backup", "brain"),
    ]
    return [d for d in candidates if os.path.isdir(d)]


def get_workspace_storage_dirs() -> List[str]:
    """Return directories containing VS Code workspaceStorage metadata."""
    dirs: List[str] = []
    if _SYSTEM == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            dirs.extend([
                os.path.join(appdata, "Antigravity IDE", "User", "workspaceStorage"),
                os.path.join(appdata, "antigravity", "User", "workspaceStorage"),
                os.path.join(appdata, "Antigravity", "User", "workspaceStorage"),
            ])
    elif _SYSTEM == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
        dirs.extend([
            os.path.join(base, "Antigravity IDE", "User", "workspaceStorage"),
            os.path.join(base, "antigravity", "User", "workspaceStorage"),
        ])
    else:
        cfg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        dirs.extend([
            os.path.join(cfg, "Antigravity IDE", "User", "workspaceStorage"),
            os.path.join(cfg, "antigravity", "User", "workspaceStorage"),
        ])
    return [d for d in dirs if os.path.isdir(d)]


def get_backup_dir() -> str:
    """Return directory for saving trajectorySummaries index backups."""
    if _SYSTEM == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            target = os.path.join(appdata, "Antigravity IDE", "User", "globalStorage", "backups")
            os.makedirs(target, exist_ok=True)
            return target
    target = os.path.expanduser("~/.gemini/antigravity/backups")
    os.makedirs(target, exist_ok=True)
    return target


# ─── Process Guard ───────────────────────────────────────────────────────────

_IDE_PROC_NAMES = [
    "Antigravity IDE.exe", "antigravity-ide",
    "Antigravity.exe",     "antigravity",
    "language_server.exe", "language_server",
]


def check_ide_running() -> Optional[str]:
    """Check if any Antigravity IDE process is active."""
    try:
        if _SYSTEM == "Windows":
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="ignore",
                timeout=5, creationflags=0x08000000,
            ).stdout
            running = {
                row.split(",", 1)[0].strip().strip('"').lower()
                for row in out.splitlines() if row.strip()
            }
            for name in _IDE_PROC_NAMES:
                if name.lower() in running:
                    return name
        else:
            for name in _IDE_PROC_NAMES:
                res = subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=3)
                if res.returncode == 0:
                    return name
    except Exception:
        pass
    return None


# ─── Low-Level Protobuf Engine (Zero Dependencies) ───────────────────────────

def encode_varint(value: int) -> bytes:
    """Encode an integer into a protobuf varint."""
    res = bytearray()
    while value > 0x7F:
        res.append((value & 0x7F) | 0x80)
        value >>= 7
    res.append(value & 0x7F)
    return bytes(res) or b"\x00"


def decode_varint(data: bytes, pos: int) -> Tuple[int, int]:
    """Decode a protobuf varint at pos. Returns (value, next_pos)."""
    result = 0
    shift = 0
    length = len(data)
    while pos < length:
        b = data[pos]
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result, pos + 1
        shift += 7
        pos += 1
    return result, pos


def skip_protobuf_field(data: bytes, pos: int, wire_type: int) -> int:
    """Advance pos past one protobuf field according to its wire type."""
    if wire_type == 0:  # Varint
        _, new_pos = decode_varint(data, pos)
        return new_pos
    elif wire_type == 1:  # 64-bit
        return min(pos + 8, len(data))
    elif wire_type == 2:  # Length-delimited
        length, new_pos = decode_varint(data, pos)
        return min(new_pos + length, len(data))
    elif wire_type == 5:  # 32-bit
        return min(pos + 4, len(data))
    return pos


def strip_field_from_protobuf(data: bytes, target_field_number: int) -> bytes:
    """Remove all occurrences of target_field_number from raw protobuf bytes."""
    remaining = bytearray()
    pos = 0
    data_len = len(data)
    while pos < data_len:
        start_pos = pos
        try:
            tag, pos = decode_varint(data, pos)
        except Exception:
            remaining.extend(data[start_pos:])
            break
        wire_type = tag & 7
        field_num = tag >> 3
        new_pos = skip_protobuf_field(data, pos, wire_type)
        if new_pos == pos and wire_type not in (0, 1, 2, 5):
            remaining.extend(data[start_pos:])
            break
        pos = new_pos
        if field_num != target_field_number:
            remaining.extend(data[start_pos:pos])
    return bytes(remaining)


def iter_length_delimited_fields(data: bytes, target_field_number: int) -> Iterator[bytes]:
    """Yield payload bytes for every length-delimited field matching field_number."""
    pos = 0
    data_len = len(data)
    while pos < data_len:
        try:
            tag, pos = decode_varint(data, pos)
        except Exception:
            break
        wire_type = tag & 7
        field_num = tag >> 3
        if wire_type == 2:
            length, pos = decode_varint(data, pos)
            payload = data[pos:pos + length]
            pos += length
            if field_num == target_field_number:
                yield payload
        elif wire_type in (0, 1, 5):
            pos = skip_protobuf_field(data, pos, wire_type)
        else:
            break


def encode_length_delimited(field_number: int, data: bytes) -> bytes:
    """Encode wire-type 2 length-delimited protobuf field."""
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data


def encode_string_field(field_number: int, text: str) -> bytes:
    """Encode UTF-8 string into protobuf field."""
    return encode_length_delimited(field_number, text.encode("utf-8"))


def build_workspace_field(folder_path: str) -> bytes:
    """
    Build protobuf field 9 (workspace sub-message) from folder path.
    Structure: subfield 1 = URI, subfield 2 = duplicate URI.
    """
    uri = path_to_workspace_uri(folder_path)
    sub_msg = encode_string_field(1, uri) + encode_string_field(2, uri)
    return encode_length_delimited(9, sub_msg)


def build_timestamp_field(field_number: int, epoch_seconds: float) -> bytes:
    """Build single timestamp submessage (subfield 1 = varint epoch seconds)."""
    ts_inner = encode_varint((1 << 3) | 0) + encode_varint(int(epoch_seconds))
    return encode_length_delimited(field_number, ts_inner)


def build_timestamp_fields(epoch_seconds: float) -> bytes:
    """Build fields 3 (updated), 7 (created), and 10 (activity)."""
    sec = int(epoch_seconds)
    ts_inner = encode_varint((1 << 3) | 0) + encode_varint(sec)
    return (
        encode_length_delimited(3, ts_inner)
        + encode_length_delimited(7, ts_inner)
        + encode_length_delimited(10, ts_inner)
    )


def extract_timestamp_seconds(protobuf_bytes: bytes, field_number: int = 3) -> Optional[int]:
    """Extract epoch seconds from a timestamp submessage."""
    for sub in iter_length_delimited_fields(protobuf_bytes, field_number):
        pos = 0
        while pos < len(sub):
            tag, pos = decode_varint(sub, pos)
            field_num = tag >> 3
            wire_type = tag & 7
            if field_num == 1 and wire_type == 0:
                val, _ = decode_varint(sub, pos)
                return val
            pos = skip_protobuf_field(sub, pos, wire_type)
    return None


def has_timestamp_fields(protobuf_bytes: Optional[bytes]) -> bool:
    """Check if protobuf bytes already contain timestamp fields."""
    if not protobuf_bytes:
        return False
    return extract_timestamp_seconds(protobuf_bytes, 3) is not None


def extract_workspace_hint(inner_blob: bytes) -> Optional[str]:
    """Scan inner protobuf blob for embedded file:/// or vscode-remote:// workspace URIs."""
    if not inner_blob:
        return None
    try:
        # Check if inner_blob is an outer entry (field 2 contains sub_message with base64)
        for sub_msg in iter_length_delimited_fields(inner_blob, 2):
            for b64_sub in iter_length_delimited_fields(sub_msg, 1):
                try:
                    decoded = base64.b64decode(b64_sub)
                    res = extract_workspace_hint(decoded)
                    if res:
                        return res
                except Exception:
                    pass

        pos = 0
        length = len(inner_blob)
        while pos < length:
            tag, pos = decode_varint(inner_blob, pos)
            wire_type = tag & 7
            field_num = tag >> 3
            if wire_type == 2:
                l, pos = decode_varint(inner_blob, pos)
                content = inner_blob[pos:pos + l]
                pos += l
                if field_num > 1:
                    try:
                        text = content.decode("utf-8", errors="replace")
                        m = re.search(r"(file:///[^\s\"\'\x00-\x1f>]+|vscode-remote://[^\s\"\'\x00-\x1f>]+)", text)
                        if m:
                            return m.group(1)
                    except Exception:
                        pass
            elif wire_type == 0:
                _, pos = decode_varint(inner_blob, pos)
            elif wire_type == 1:
                pos += 8
            elif wire_type == 5:
                pos += 4
            else:
                break
    except Exception:
        pass
    return None


# ─── Workspace and URI Utilities ─────────────────────────────────────────────

def is_remote_uri(uri: str) -> bool:
    """Return True if uri is a remote scheme (vscode-remote://, etc.)."""
    return uri.startswith("vscode-remote://") or uri.startswith("file:///")


def path_to_workspace_uri(folder_path: str) -> str:
    """
    Convert a filesystem path to an Antigravity workspace URI.
    Keeps remote URIs intact and formats local paths as file:///d:/Repo.
    """
    if is_remote_uri(folder_path):
        return folder_path

    if _IS_WSL and folder_path.startswith("/mnt/"):
        parts = folder_path.split("/")
        if len(parts) >= 3 and len(parts[2]) == 1:
            drive = parts[2].lower()
            rest = "/".join(parts[3:])
            return f"file:///{drive}:/{rest}"

    p = folder_path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        rest = p[2:]
        return f"file:///{drive}:{rest}"
    return f"file:///{p.lstrip('/')}"


def uri_to_local_path(file_uri: str) -> Optional[str]:
    """Convert file:/// URI to local path, unquoting special characters."""
    if not file_uri.startswith("file:///"):
        return None
    raw = file_uri[len("file:///"):]
    raw = unquote(raw)
    if _SYSTEM == "Windows" and len(raw) >= 2 and raw[1] == ":":
        return raw[0].upper() + raw[1:].replace("/", "\\")
    return "/" + raw.lstrip("/")


def load_known_workspace_uris() -> List[str]:
    """Load all workspace URIs known to VS Code from workspaceStorage/*/workspace.json."""
    uris: List[str] = []
    for ws_dir in get_workspace_storage_dirs():
        if not os.path.isdir(ws_dir):
            continue
        try:
            for entry in os.listdir(ws_dir):
                cfg_file = os.path.join(ws_dir, entry, "workspace.json")
                if os.path.isfile(cfg_file):
                    try:
                        with open(cfg_file, "r", encoding="utf-8", errors="ignore") as f:
                            data = json.load(f)
                        uri = data.get("folder") or data.get("workspace")
                        if uri and uri not in uris:
                            uris.append(uri)
                    except Exception:
                        pass
        except Exception:
            pass
    # Sort longest-first for most specific prefix match
    uris.sort(key=len, reverse=True)
    return uris


# ─── Title & Prompt Parsing ──────────────────────────────────────────────────

def open_sqlite_readonly(db_path: str) -> sqlite3.Connection:
    """Open SQLite database in strict read-only mode with query_only pragma."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)
    abs_path = os.path.abspath(db_path).replace("\\", "/")
    uri = "file:" + quote(abs_path, safe="/:") + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only=ON")
    except Exception:
        conn.close()
        raise
    return conn


def normalize_prompt_text(text: Optional[str]) -> str:
    """Collapse excess whitespace and trim text."""
    if not text or not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def collapse_prompt_urls(prompt: str) -> str:
    """Replace long HTTP(S) URLs with their hostnames to keep titles clean."""
    def _rep(match: re.Match) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,!?;:)]}":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        host = urlparse(raw).hostname
        return (host or raw) + trailing
    return re.sub(r"https?://[^\s<>()]+", _rep, prompt, flags=re.IGNORECASE)


def truncate_prompt_title(text: str, max_chars: int = 60, max_words: int = 10) -> str:
    """Truncate text at clean word boundary within length and word count limits."""
    words = text.split()
    if len(words) > max_words:
        words = words[:max_words]
    res = ""
    for w in words:
        candidate = (res + " " + w).strip() if res else w
        if len(candidate) > max_chars:
            break
        res = candidate
    if res and res != text:
        return res.rstrip(".,;:!- ") + "..."
    return res


def format_prompt_title(prompt: Optional[str]) -> Optional[str]:
    """Derive a clean, concise conversation title from a prompt."""
    norm = normalize_prompt_text(prompt)
    if not norm:
        return None
    norm = normalize_prompt_text(collapse_prompt_urls(norm))
    norm = re.sub(r"^#{1,6}\s+", "", norm)

    # If first sentence is short enough, use it
    m = re.match(r"^(.+?[.!?])(?:\s|$)", norm)
    if m:
        sent = m.group(1).strip()
        if len(sent) <= 60 and len(sent.split()) <= 10:
            return sent
    return truncate_prompt_title(norm) or None


def unwrap_user_request_text(val: Optional[str]) -> str:
    """Remove Antigravity <USER_REQUEST> tags and metadata envelope."""
    if not isinstance(val, str):
        return ""
    start_tag = "<USER_REQUEST>"
    idx = val.find(start_tag)
    if idx >= 0:
        val = val[idx + len(start_tag):]
        for end_tag in ("</USER_REQUEST>", "<ADDITIONAL_METADATA>", "<USER_SETTINGS_CHANGE>", "<truncated"):
            e_idx = val.find(end_tag)
            if e_idx >= 0:
                val = val[:e_idx]
    return normalize_prompt_text(val)


def extract_prompt_from_conversation_db(db_path: str) -> Optional[str]:
    """
    Extract first user prompt from a conversation SQLite database.
    Inspects tables with step_payload and step_type=14 (user prompt step).
    """
    conn = None
    try:
        conn = open_sqlite_readonly(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]

        target_table = None
        has_idx = False
        for t in tables:
            quoted = f'"{t}"'
            cols = {r[1] for r in cur.execute(f"PRAGMA table_info({quoted})").fetchall()}
            if {"step_type", "step_payload"}.issubset(cols):
                target_table = quoted
                has_idx = "idx" in cols
                break

        if not target_table:
            return None

        order_by = '"idx"' if has_idx else "rowid"
        cur.execute(
            f"SELECT step_payload FROM {target_table} WHERE step_type=14 ORDER BY {order_by}"
        )
        for (payload,) in cur.fetchall():
            if not payload:
                continue
            if isinstance(payload, memoryview):
                payload = payload.tobytes()
            if not isinstance(payload, bytes):
                continue

            # Field 19 (title container) -> Field 2 (prompt string)
            for container in iter_length_delimited_fields(payload, 19):
                for title_bytes in iter_length_delimited_fields(container, 2):
                    try:
                        raw = title_bytes.decode("utf-8")
                        unwrapped = unwrap_user_request_text(raw)
                        clean = format_prompt_title(unwrapped)
                        if clean:
                            return clean
                    except Exception:
                        pass
        return None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_title_from_brain_logs(conversation_id: str) -> Optional[str]:
    """Look for user prompt in brain/.system_generated/logs/transcript.jsonl."""
    for brain_dir in get_all_brain_dirs():
        logs_dir = os.path.join(brain_dir, conversation_id, ".system_generated", "logs")
        for fn in ("transcript.jsonl", "overview.txt"):
            log_file = os.path.join(logs_dir, fn)
            if not os.path.isfile(log_file):
                continue
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        if fn.endswith(".jsonl"):
                            try:
                                data = json.loads(line)
                                if isinstance(data, dict):
                                    text = data.get("content") or data.get("prompt") or data.get("text")
                                    if text and isinstance(text, str):
                                        clean = format_prompt_title(unwrap_user_request_text(text))
                                        if clean:
                                            return clean
                            except Exception:
                                pass
            except Exception:
                pass
    return None


def get_title_from_brain_markdown(conversation_id: str) -> Optional[str]:
    """Extract first # Markdown heading from brain artifact files."""
    for brain_dir in get_all_brain_dirs():
        bp = os.path.join(brain_dir, conversation_id)
        if not os.path.isdir(bp):
            continue
        try:
            for item in sorted(os.listdir(bp)):
                if item.startswith(".") or not item.endswith(".md"):
                    continue
                fp = os.path.join(bp, item)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        for idx, line in enumerate(f):
                            if idx > 100:
                                break
                            s = line.strip()
                            if s.startswith("# "):
                                title = s.lstrip("# ").strip()[:80]
                                if title:
                                    return title
                except Exception:
                    pass
        except Exception:
            pass
    return None


def resolve_title(
    conversation_id: str,
    existing_titles: Dict[str, str],
    conv_path: Optional[str] = None
) -> Tuple[str, str]:
    """
    Resolve title using 3-tier cascade:
      1. SQLite prompt step
      2. Brain logs / transcript
      3. Brain markdown heading
      4. Preserved existing title
      5. Fallback
    """
    # 1. From conversation DB
    if conv_path and conv_path.lower().endswith(".db"):
        title = extract_prompt_from_conversation_db(conv_path)
        if title:
            return title, "database"

    # 2. From brain logs
    title = get_title_from_brain_logs(conversation_id)
    if title:
        return title, "brain_log"

    # 3. From brain markdown
    title = get_title_from_brain_markdown(conversation_id)
    if title:
        return title, "brain_md"

    # 4. Existing title from index
    existing = existing_titles.get(conversation_id)
    if existing and not existing.startswith("Chat ") and len(existing) > 3:
        return existing, "preserved"

    # 5. Fallback
    short_id = conversation_id[:8]
    return f"Conversation ({short_id})", "fallback"


def infer_workspace_from_brain(conversation_id: str, known_ws_uris: List[str]) -> Optional[str]:
    """Infer workspace folder by matching URLs in brain markdown with known workspaces."""
    file_pattern = re.compile(r"file:///([^\)\s\"'\]>]+)")
    remote_pattern = re.compile(r"(vscode-remote://[^\)\s\"'\]>]+)")

    found_uris: List[str] = []
    for brain_dir in get_all_brain_dirs():
        bp = os.path.join(brain_dir, conversation_id)
        if not os.path.isdir(bp):
            continue
        try:
            for fn in os.listdir(bp):
                if not fn.endswith(".md") or fn.startswith("."):
                    continue
                fp = os.path.join(bp, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(16384)
                    for m in remote_pattern.finditer(content):
                        found_uris.append(m.group(1))
                    for m in file_pattern.finditer(content):
                        found_uris.append("file:///" + m.group(1))
                except Exception:
                    pass
        except Exception:
            pass

    if not found_uris:
        return None

    if known_ws_uris:
        counts: Dict[str, int] = {}
        for f_uri in found_uris:
            norm = unquote(f_uri).replace("%3A", ":").replace("%3a", ":").replace("\\", "/")
            for ws_uri in known_ws_uris:
                ws_norm = unquote(ws_uri).replace("%3A", ":").replace("%3a", ":").replace("\\", "/")
                if norm.lower().startswith(ws_norm.lower() + "/") or norm.lower() == ws_norm.lower():
                    counts[ws_uri] = counts.get(ws_uri, 0) + 1
                    break
        if counts:
            best_ws = max(counts, key=counts.get)
            local = uri_to_local_path(best_ws)
            return local if local else best_ws

    return None


# ─── Metadata Extraction from Existing Index ─────────────────────────────────

def extract_existing_metadata(db_path: str) -> Tuple[Dict[str, str], Dict[str, bytes]]:
    """
    Read current trajectorySummaries from state.vscdb.
    Returns:
      titles: {conversation_id: title}
      inner_blobs: {conversation_id: raw_inner_protobuf_bytes}
    """
    titles: Dict[str, str] = {}
    inner_blobs: Dict[str, bytes] = {}
    if not os.path.isfile(db_path):
        return titles, inner_blobs

    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.trajectorySummaries'"
        )
        row = cur.fetchone()
        conn.close()

        if not row or not row[0]:
            return titles, inner_blobs

        raw_bytes = base64.b64decode(row[0])
        pos = 0
        total_len = len(raw_bytes)

        while pos < total_len:
            tag, pos = decode_varint(raw_bytes, pos)
            wire_type = tag & 7
            if wire_type != 2:
                break
            length, pos = decode_varint(raw_bytes, pos)
            entry_bytes = raw_bytes[pos:pos + length]
            pos += length

            # Parse entry: field 1 = UUID (string), field 2 = sub-message
            epos = 0
            cid = None
            info_b64 = None
            while epos < len(entry_bytes):
                etag, epos = decode_varint(entry_bytes, epos)
                efield = etag >> 3
                ewire = etag & 7
                if ewire == 2:
                    el, epos = decode_varint(entry_bytes, epos)
                    val = entry_bytes[epos:epos + el]
                    epos += el
                    if efield == 1:
                        cid = val.decode("utf-8", errors="ignore")
                    elif efield == 2:
                        # sub-message field 1 is base64 string
                        for b64_sub in iter_length_delimited_fields(val, 1):
                            info_b64 = b64_sub.decode("utf-8", errors="ignore")
                else:
                    epos = skip_protobuf_field(entry_bytes, epos, ewire)

            if cid and info_b64:
                try:
                    inner_bytes = base64.b64decode(info_b64)
                    inner_blobs[cid] = inner_bytes
                    # Title is field 1 in inner_bytes
                    for title_bytes in iter_length_delimited_fields(inner_bytes, 1):
                        t = title_bytes.decode("utf-8", errors="ignore").strip()
                        if t:
                            titles[cid] = t
                            break
                except Exception:
                    pass
    except Exception:
        pass
    return titles, inner_blobs


# ─── Building Trajectory Protobuf Entries ─────────────────────────────────────

def build_trajectory_entry(
    conversation_id: str,
    title: str,
    existing_inner_data: Optional[bytes] = None,
    workspace_path: Optional[str] = None,
    conversation_mtime: Optional[float] = None,
    source_is_db: bool = False,
) -> bytes:
    """Build single trajectory entry (field 1 = UUID, field 2 = submessage with inner info)."""
    if existing_inner_data:
        # Preserve existing metadata, update title
        preserved = strip_field_from_protobuf(existing_inner_data, 1)
        inner_info = encode_string_field(1, title) + preserved

        # Workspace injection / cleanup
        if not workspace_path:
            existing_ws = extract_workspace_hint(inner_info)
            if existing_ws and ("%20" in existing_ws or "%3A" in existing_ws or "%3a" in existing_ws):
                decoded_ws = unquote(existing_ws)
                inner_info = strip_field_from_protobuf(inner_info, 9)
                inner_info += build_workspace_field(decoded_ws)
        elif workspace_path:
            inner_info = strip_field_from_protobuf(inner_info, 9)
            inner_info += build_workspace_field(workspace_path)

        # Timestamps injection
        if source_is_db and conversation_mtime:
            old_ts = extract_timestamp_seconds(existing_inner_data, 3)
            if old_ts is None or int(conversation_mtime) > old_ts:
                inner_info = strip_field_from_protobuf(inner_info, 3)
                inner_info += build_timestamp_field(3, conversation_mtime)
        elif conversation_mtime and not has_timestamp_fields(existing_inner_data):
            inner_info += build_timestamp_fields(conversation_mtime)
    else:
        inner_info = encode_string_field(1, title)
        if workspace_path:
            inner_info += build_workspace_field(workspace_path)
        if conversation_mtime:
            inner_info += build_timestamp_fields(conversation_mtime)

    info_b64 = base64.b64encode(inner_info).decode("utf-8")
    sub_msg = encode_string_field(1, info_b64)

    entry = encode_string_field(1, conversation_id)
    entry += encode_length_delimited(2, sub_msg)
    return entry


# ─── Catalog Discovery ───────────────────────────────────────────────────────

def collect_all_conversations() -> Dict[str, str]:
    """
    Catalog all conversation files on disk (.db and .pb).
    Returns: {conversation_id: file_path}
    """
    catalog: Dict[str, str] = {}
    for conv_dir in get_all_conversation_dirs():
        try:
            folder_map: Dict[str, str] = {}
            for name in sorted(os.listdir(conv_dir)):
                lname = name.lower()
                if lname.endswith(".db") and not lname.endswith((".db-shm", ".db-wal")):
                    cid = name[:-3]
                    folder_map[cid] = os.path.join(conv_dir, name)
                elif lname.endswith(".pb"):
                    cid = name[:-3]
                    if cid not in folder_map:
                        folder_map[cid] = os.path.join(conv_dir, name)
            for cid, path in folder_map.items():
                if cid not in catalog:
                    catalog[cid] = path
        except Exception:
            pass

    # Also catalog any orphaned brain directories that don't have a .db
    for brain_dir in get_all_brain_dirs():
        try:
            for item in os.listdir(brain_dir):
                bp = os.path.join(brain_dir, item)
                if os.path.isdir(bp) and item not in catalog:
                    catalog[item] = bp
        except Exception:
            pass

    return catalog


# ─── Index Writer with Timestamped Snapshot ──────────────────────────────────

def write_index_to_database(db_path: str, encoded_value: str, suffix: str = "") -> Optional[str]:
    """Write rebuilt Base64 index to state.vscdb with prior backup."""
    if not os.path.isfile(db_path):
        return None

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(get_backup_dir(), f"trajectorySummaries_{suffix}_{stamp}.txt")

    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT value FROM ItemTable WHERE key='antigravityUnifiedStateSync.trajectorySummaries'"
        )
        row = cur.fetchone()
        if row and row[0]:
            try:
                with open(backup_file, "w", encoding="utf-8") as f:
                    f.write(row[0])
            except Exception:
                pass

        cur.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) "
            "VALUES ('antigravityUnifiedStateSync.trajectorySummaries', ?)",
            (encoded_value,),
        )
        conn.commit()
        return backup_file if (row and row[0]) else None
    finally:
        conn.close()


def list_backups() -> List[Tuple[str, str, float]]:
    """Return available backups [(filename, path, mtime)]."""
    b_dir = get_backup_dir()
    if not os.path.isdir(b_dir):
        return []
    res = []
    for fn in os.listdir(b_dir):
        if fn.startswith("trajectorySummaries_") and fn.endswith(".txt"):
            fp = os.path.join(b_dir, fn)
            res.append((fn, fp, os.path.getmtime(fp)))
    res.sort(key=lambda x: x[2], reverse=True)
    return res


def restore_latest_backup() -> bool:
    """Restore index from the most recent backup file."""
    backups = list_backups()
    if not backups:
        err("No previous trajectory index backups found.")
        return False

    latest_name, latest_path, _ = backups[0]
    try:
        with open(latest_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            err("Backup file is empty.")
            return False

        dbs = get_db_candidates()
        if not dbs:
            err("No state.vscdb found to restore into.")
            return False

        restored_count = 0
        for db in dbs:
            conn = sqlite3.connect(db, timeout=10.0)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO ItemTable (key, value) "
                    "VALUES ('antigravityUnifiedStateSync.trajectorySummaries', ?)",
                    (content,),
                )
                conn.commit()
                restored_count += 1
            finally:
                conn.close()

        ok(f"Restored previous conversation index from {latest_name} into {restored_count} database(s).")
        return True
    except Exception as ex:
        err(f"Rollback failed: {ex}")
        return False


# ─── Rebuild Orchestration Engine ────────────────────────────────────────────

def rebuild_conversations_index(auto_workspaces: bool = True) -> Dict[str, int]:
    """
    Core function to rebuild the entire Antigravity trajectorySummaries index.
    Returns summary statistics dictionary.
    """
    dbs = get_db_candidates()
    if not dbs:
        return {"error": -1, "total": 0}

    catalog = collect_all_conversations()
    if not catalog:
        return {"error": 0, "total": 0}

    # Sort conversations newest first based on file mtime
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except Exception:
            return 0.0

    sorted_cids = sorted(catalog.keys(), key=lambda cid: _mtime(catalog[cid]), reverse=True)

    # Read existing metadata across all DBs
    all_titles: Dict[str, str] = {}
    all_blobs: Dict[str, bytes] = {}
    for db in dbs:
        t, b = extract_existing_metadata(db)
        all_titles.update(t)
        all_blobs.update(b)

    known_workspaces = load_known_workspace_uris()

    stats = {
        "total": len(sorted_cids),
        "database": 0,
        "brain_log": 0,
        "brain_md": 0,
        "preserved": 0,
        "fallback": 0,
        "workspaces": 0,
    }

    result_bytes = bytearray()
    for cid in sorted_cids:
        path = catalog[cid]
        title, source = resolve_title(cid, all_titles, path)
        stats[source] = stats.get(source, 0) + 1

        inner = all_blobs.get(cid)
        has_ws = bool(inner and extract_workspace_hint(inner))

        ws_path = None
        if not has_ws and auto_workspaces:
            inferred = infer_workspace_from_brain(cid, known_workspaces)
            if inferred:
                ws_path = inferred
                stats["workspaces"] += 1
        elif has_ws:
            stats["workspaces"] += 1

        mtime = _mtime(path) if os.path.exists(path) else None
        source_is_db = bool(path and path.lower().endswith(".db"))

        entry = build_trajectory_entry(
            conversation_id=cid,
            title=title,
            existing_inner_data=inner,
            workspace_path=ws_path,
            conversation_mtime=mtime,
            source_is_db=source_is_db,
        )
        result_bytes.extend(encode_length_delimited(1, entry))

    encoded_b64 = base64.b64encode(bytes(result_bytes)).decode("utf-8")

    # Write to all state.vscdb instances
    for idx, db in enumerate(dbs):
        suffix = f"db_{idx}"
        write_index_to_database(db, encoded_b64, suffix)

    return stats


# ─── Interactive CLI Wizard ──────────────────────────────────────────────────

def run_rebuild_wizard() -> bool:
    """Full interactive wizard for repairing and ordering conversation history."""
    print_menu_section("REBUILD & REPAIR CONVERSATION HISTORY")
    print()
    info("Repairs missing, out-of-order, or broken conversation histories.")
    info("Scans disk, restores titles and project bindings, and sorts chronologically.")
    print()

    # Process Guard
    proc = check_ide_running()
    if proc:
        warn(f"Antigravity IDE appears to be running ({proc}).")
        warn("Please close Antigravity IDE completely (File -> Exit) before rebuilding.")
        print()
        hint("If Antigravity is open, it may overwrite the index when closing.")
        print()
        yn = input(color("  Proceed anyway? (y/n) > ", COLOR_YELLOW, COLOR_BOLD)).strip().lower()
        if yn not in ("y", "yes", "д", "да"):
            cancel("Operation cancelled.")
            return False

    dbs = get_db_candidates()
    if not dbs:
        err("No Antigravity state.vscdb databases found.")
        hint(r"Expected at: %APPDATA%\Antigravity IDE\User\globalStorage\state.vscdb")
        return False

    catalog = collect_all_conversations()
    if not catalog:
        warn("No conversation records found on disk (~/.gemini/antigravity/conversations).")
        return False

    step("Discovered databases", True, f"{len(dbs)} instance(s) found")
    step("Discovered conversations", True, f"{len(catalog)} conversation(s) located on disk")

    backups = list_backups()
    if backups:
        print()
        hint(f"Found {len(backups)} existing backup(s). You can rollback anytime.")

    print()
    print_menu_row("1", "Auto-rebuild and re-order all conversations (Recommended)", "auto-detect titles & workspaces", COLOR_GREEN)
    if backups:
        print_menu_row("2", "Rollback to previous index backup", f"restore from {backups[0][0]}", COLOR_YELLOW)
    print_menu_row("0", "Cancel", "return to menu", COLOR_RED)
    print()

    choice = input(color("  Select option > ", COLOR_CYAN, COLOR_BOLD)).strip().lower()
    if choice in ("0", "", "b", "back", "q", "quit", "exit"):
        cancel("Cancelled.")
        return False

    if choice == "2" and backups:
        return restore_latest_backup()

    if choice == "1":
        print()
        step("Processing", True, "Analyzing conversation databases & brain logs...")
        stats = rebuild_conversations_index(auto_workspaces=True)
        print()
        ok("Successfully rebuilt conversation trajectory index!")
        print()
        print(f"  {color('Total conversations indexed:', COLOR_WHITE, COLOR_BOLD)} {stats['total']}")
        print(f"    - Titles extracted from chat prompts:    {stats.get('database', 0)}")
        print(f"    - Titles extracted from brain transcripts: {stats.get('brain_log', 0)}")
        print(f"    - Titles extracted from task markdown:     {stats.get('brain_md', 0)}")
        print(f"    - Titles preserved from existing index:    {stats.get('preserved', 0)}")
        print(f"    - Workspaces attached to projects:         {stats.get('workspaces', 0)}")
        print()
        hint("Restart Antigravity IDE to see your conversations restored in chronological order!")
        return True

    return False
