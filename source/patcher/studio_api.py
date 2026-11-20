"""
patcher/studio_api.py -- Headless JSON API bridge for Antigravity Studio (Tauri v2).

Provides clean, cross-platform, side-effect-safe inspection and action execution
for the Tauri desktop app and Quotty HUD without interactive console prompts.
"""
from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any


def _tools_dir() -> Path:
    env_dir = os.environ.get("AG_TOOLS_DIR", "").strip()
    return Path(env_dir) if env_dir else (Path.home() / ".antigravity_tools")


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _safe_str(val: Any) -> str:
    return str(val or "").strip()


def _format_reset_eta(reset_iso: str) -> str:
    txt = _safe_str(reset_iso)
    if not txt or txt.startswith("0001-01-01"):
        return ""
    ts = _parse_iso_ts(txt, 0.0)
    if ts <= 0:
        return ""
    rem = int(ts - time.time())
    if rem <= 0:
        return "Сейчас"
    mins = rem // 60
    hours = mins // 60
    days = hours // 24
    if days > 0:
        return f"{days}д {hours % 24}ч"
    if hours > 0:
        return f"{hours}ч {mins % 60}м"
    return f"{max(1, mins)}м"


def _extract_account_summary(acc: dict[str, Any]) -> dict[str, Any]:
    """Build sanitized account payload (never exposes tokens or secrets)."""
    q = acc.get("quota") if isinstance(acc.get("quota"), dict) else {}
    groups = q.get("quota_groups") if isinstance(q.get("quota_groups"), list) else []
    models = q.get("models") if isinstance(q.get("models"), list) else []

    buckets: list[dict[str, Any]] = []
    seen_families: set[str] = set()

    def _pct_from_frac(val: Any) -> int | None:
        if val is None:
            return None
        try:
            f = float(val)
            if f <= 1.0:
                return max(0, min(100, int(round(f * 100))))
            return max(0, min(100, int(round(f))))
        except Exception:
            return None

    # Parse quota_groups (contains both 5h and weekly windows when present)
    claude_5h = q.get("claude_percentage", acc.get("claude_percentage"))
    gemini_5h = q.get("gemini_percentage", acc.get("gemini_percentage"))
    argon_5h = q.get("argon_percentage", acc.get("argon_percentage"))
    carbon_5h = q.get("carbon_percentage", acc.get("carbon_percentage"))
    claude_weekly = q.get("claude_weekly_percentage")
    gemini_weekly = q.get("gemini_weekly_percentage")
    argon_weekly = q.get("argon_weekly_percentage")
    carbon_weekly = q.get("carbon_weekly_percentage")
    claude_reset = _safe_str(q.get("claude_reset_time"))
    gemini_reset = _safe_str(q.get("gemini_reset_time"))
    argon_reset = _safe_str(q.get("argon_reset_time"))
    carbon_reset = _safe_str(q.get("carbon_reset_time"))
    claude_weekly_reset = ""
    gemini_weekly_reset = ""
    argon_weekly_reset = ""
    carbon_weekly_reset = ""

    claude_label = "Claude 4.6"
    gemini_label = "Gemini 3.8"
    argon_label = "Gemini Argon"
    carbon_label = "Gemini Carbon"
    gemini_desc = ""
    claude_desc = ""
    argon_desc = ""
    carbon_desc = ""
    extra_buckets: list[dict[str, Any]] = []

    for grp in groups:
        if not isinstance(grp, dict):
            continue
        gname = _safe_str(grp.get("displayName"))
        gname_low = gname.lower()
        gdesc = _safe_str(grp.get("description"))
        gdesc_low = gdesc.lower()
        gbuckets = grp.get("buckets") if isinstance(grp.get("buckets"), list) else []

        matched_known = False
        grp_5h = None
        grp_weekly = None
        grp_5h_rt = ""
        grp_weekly_rt = ""

        for b in gbuckets:
            if not isinstance(b, dict):
                continue
            bid = _safe_str(b.get("bucketId")).lower()
            win = _safe_str(b.get("window")).lower()
            pct = _pct_from_frac(b.get("remainingFraction"))
            rt = _safe_str(b.get("resetTime"))
            is_weekly = "weekly" in bid or win == "weekly"
            disabled = bool(b.get("disabled"))

            if "carbon" in bid or ("carbon" in gname_low and "gemini models" not in gname_low):
                matched_known = True
                carbon_desc = carbon_desc or gdesc
                if is_weekly:
                    if carbon_weekly is None:
                        carbon_weekly = pct
                    carbon_weekly_reset = carbon_weekly_reset or rt
                else:
                    if carbon_5h is None and not disabled:
                        carbon_5h = pct
                    carbon_reset = carbon_reset or rt
            elif "argon" in bid or "argon" in gname_low:
                matched_known = True
                argon_desc = argon_desc or gdesc
                if is_weekly:
                    if argon_weekly is None or (not disabled and argon_weekly is not None):
                        argon_weekly = pct if argon_weekly is None else argon_weekly
                    argon_weekly_reset = argon_weekly_reset or rt
                else:
                    if argon_5h is None and not disabled:
                        argon_5h = pct
                    argon_reset = argon_reset or rt
            elif "3p" in bid or "claude" in gname_low or "claude" in bid:
                matched_known = True
                claude_desc = claude_desc or gdesc
                if "gpt" in gname_low or "gpt" in gdesc_low:
                    claude_label = "Claude · GPT"
                if is_weekly:
                    if claude_weekly is None:
                        claude_weekly = pct
                    claude_weekly_reset = claude_weekly_reset or rt
                else:
                    if claude_5h is None and not disabled:
                        claude_5h = pct
                    claude_reset = claude_reset or rt
            elif "gemini" in bid or "gemini" in gname_low or "hydrogen" in gname_low:
                matched_known = True
                gemini_desc = gemini_desc or gdesc
                if "carbon" in gdesc_low or "hydrogen" in gdesc_low or "gemini 4" in gdesc_low:
                    gemini_label = "Hydrogen · 3.8" if ("carbon" in gdesc_low or "argon" in gdesc_low or argon_5h is not None) else "Gemini 4 · 3.8"
                if is_weekly:
                    if gemini_weekly is None:
                        gemini_weekly = pct
                    gemini_weekly_reset = gemini_weekly_reset or rt
                else:
                    if gemini_5h is None and not disabled:
                        gemini_5h = pct
                    gemini_reset = gemini_reset or rt
            else:
                if is_weekly:
                    if grp_weekly is None:
                        grp_weekly = pct
                    grp_weekly_rt = grp_weekly_rt or rt
                else:
                    if grp_5h is None and not disabled:
                        grp_5h = pct
                    grp_5h_rt = grp_5h_rt or rt

        if not matched_known and (grp_5h is not None or grp_weekly is not None):
            extra_id = "codex" if ("gpt" in gname_low or "openai" in gname_low or "codex" in gname_low) else f"extra-{len(extra_buckets)}"
            extra_family = "codex" if extra_id == "codex" else "gemini"
            eff_r5 = grp_5h_rt if grp_5h is not None and grp_5h_rt else grp_weekly_rt
            eff_rw = grp_weekly_rt or grp_5h_rt
            extra_buckets.append({
                "id": extra_id,
                "label": gname or "Extra",
                "family": extra_family,
                "pct_5h": grp_5h if grp_5h is not None else grp_weekly,
                "pct_weekly": grp_weekly,
                "reset_5h": eff_r5,
                "reset_weekly": eff_rw,
                "reset_5h_label": _format_reset_eta(eff_r5),
                "reset_weekly_label": _format_reset_eta(eff_rw),
                "models_desc": gdesc,
            })

    # If weekly pool hit 0%, treat 5h usable remaining as 0% too (matches extension quota_engine.js)
    if claude_weekly == 0 and claude_5h is not None:
        claude_5h = 0
        claude_reset = claude_weekly_reset or claude_reset
    if gemini_weekly == 0 and gemini_5h is not None:
        gemini_5h = 0
        gemini_reset = gemini_weekly_reset or gemini_reset
    if argon_weekly == 0 and argon_5h is not None:
        argon_5h = 0
        argon_reset = argon_weekly_reset or argon_reset
    if carbon_weekly == 0 and carbon_5h is not None:
        carbon_5h = 0
        carbon_reset = carbon_weekly_reset or carbon_reset

    # Also build per-model sanitized list for UI flexibility
    model_rows: list[dict[str, Any]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        name = _safe_str(m.get("name"))
        disp = _safe_str(m.get("display_name") or name)
        pct = m.get("percentage")
        try:
            pct_val = max(0, min(100, int(round(float(pct))))) if pct is not None else None
        except Exception:
            pct_val = None
        if name:
            model_rows.append({
                "id": name,
                "label": disp,
                "percentage": pct_val,
                "reset_time": _safe_str(m.get("reset_time")),
            })

    def _add_bucket(bid: str, label: str, family: str, pct: Any, weekly: Any, rt: str, wrt: str, mdesc: str = "") -> None:
        if pct is None and weekly is None:
            return
        try:
            p5 = max(0, min(100, int(round(float(pct))))) if pct is not None else None
        except Exception:
            p5 = None
        try:
            pw = max(0, min(100, int(round(float(weekly))))) if weekly is not None else None
        except Exception:
            pw = None
        if p5 is None and pw is None:
            return
        eff_r5 = rt if (p5 is not None and rt) else wrt
        eff_rw = wrt or rt
        buckets.append({
            "id": bid,
            "label": label,
            "family": family,
            "pct_5h": p5 if p5 is not None else pw,
            "pct_weekly": pw,
            "reset_5h": eff_r5,
            "reset_weekly": eff_rw,
            "reset_5h_label": _format_reset_eta(eff_r5),
            "reset_weekly_label": _format_reset_eta(eff_rw),
            "models_desc": mdesc,
        })
        seen_families.add(family)

    gemini_desc_low = gemini_desc.lower()
    if argon_5h is not None or argon_weekly is not None:
        gemini_label = "Hydrogen · 3.8" if gemini_label in ("Gemini 3.8", "Gemini 4 · 3.8") else gemini_label

    _add_bucket("claude", claude_label, "claude", claude_5h, claude_weekly, claude_reset, claude_weekly_reset, claude_desc)
    _add_bucket("gemini", gemini_label, "gemini", gemini_5h, gemini_weekly, gemini_reset, gemini_weekly_reset, gemini_desc)
    if argon_5h is not None or argon_weekly is not None:
        _add_bucket("argon", argon_label, "gemini-argon", argon_5h, argon_weekly, argon_reset, argon_weekly_reset, argon_desc)
    elif "argon" in gemini_desc_low:
        _add_bucket("argon", argon_label, "gemini-argon", gemini_5h, gemini_weekly, gemini_reset, gemini_weekly_reset, "Пул Ultra (единый с Gemini 4)")
    if carbon_5h is not None or carbon_weekly is not None:
        _add_bucket("carbon", carbon_label, "gemini-carbon", carbon_5h, carbon_weekly, carbon_reset, carbon_weekly_reset, carbon_desc)
    elif "carbon" in gemini_desc_low:
        _add_bucket("carbon", carbon_label, "gemini-carbon", gemini_5h, gemini_weekly, gemini_reset, gemini_weekly_reset, "Пул Ultra (единый с Gemini 4)")
    for eb in extra_buckets:
        buckets.append(eb)

    return {
        "id": _safe_str(acc.get("id")),
        "email": _safe_str(acc.get("email")),
        "name": _safe_str(acc.get("name")),
        "tier": _safe_str(acc.get("tier") or q.get("subscription_tier") or "Free"),
        "disabled": bool(acc.get("disabled") or acc.get("validation_blocked")),
        "is_forbidden": bool(q.get("is_forbidden")),
        "forbidden_reason": _safe_str(q.get("forbidden_reason")),
        "last_updated": q.get("quota_updated_at") or q.get("last_updated") or acc.get("last_used"),
        "buckets": buckets,
        "models": model_rows,
    }


def _collect_accounts() -> dict[str, Any]:
    tools = _tools_dir()
    index = _read_json(tools / "accounts.json", {})
    if not isinstance(index, dict):
        index = {}
    live_auth = _read_json(tools / "live_auth.json", {})
    if not isinstance(live_auth, dict):
        live_auth = {}
    gui_cfg = _read_json(tools / "gui_config.json", {})
    if not isinstance(gui_cfg, dict):
        gui_cfg = {}

    accounts_dir = tools / "accounts"
    items_by_id: dict[str, dict[str, Any]] = {}

    if accounts_dir.is_dir():
        for fp in sorted(accounts_dir.glob("*.json")):
            raw = _read_json(fp)
            if isinstance(raw, dict) and raw.get("id"):
                items_by_id[str(raw["id"])] = _extract_account_summary(raw)

    # Also merge any entries present only in accounts.json index
    idx_list = index.get("accounts")
    if isinstance(idx_list, list):
        for entry in idx_list:
            if isinstance(entry, dict) and entry.get("id"):
                aid = str(entry["id"])
                if aid not in items_by_id:
                    items_by_id[aid] = _extract_account_summary(entry)

    active_ide_id = _safe_str(
        (live_auth.get("ide") or {}).get("id")
        if isinstance(live_auth.get("ide"), dict)
        else ""
    ) or _safe_str(index.get("current_ide_account_id") or index.get("current_account_id"))

    active_standalone_id = _safe_str(
        (live_auth.get("standalone") or {}).get("id")
        if isinstance(live_auth.get("standalone"), dict)
        else ""
    ) or _safe_str(index.get("current_standalone_account_id") or index.get("current_account_id"))

    active_cli_id = _safe_str(index.get("current_cli_account_id"))
    if not active_cli_id and not os.environ.get("PYTEST_CURRENT_TEST"):
        cli_tok_file = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        cli_tok = _read_json(cli_tok_file, {})
        if isinstance(cli_tok, dict):
            cli_email = _safe_str(cli_tok.get("id_token_email")).lower()
            if cli_email:
                for aid, aitem in items_by_id.items():
                    if aitem.get("email", "").lower() == cli_email:
                        active_cli_id = aid
                        break
    if not active_cli_id:
        active_cli_id = active_ide_id

    _maybe_refresh_stale_active_tokens([active_ide_id, active_standalone_id, active_cli_id])

    classic_auto = gui_cfg.get("classic_auto_switch") if isinstance(gui_cfg.get("classic_auto_switch"), dict) else {}
    auto_rotate_429 = bool(gui_cfg.get("auto_switch_classic_depleted") or classic_auto.get("enabled"))

    # Sort: active IDE / standalone / CLI first, then alphabetical email
    items = sorted(
        items_by_id.values(),
        key=lambda a: (
            0 if a["id"] in (active_ide_id, active_standalone_id, active_cli_id) else 1,
            a["email"].lower(),
        ),
    )

    return {
        "active_ide_id": active_ide_id,
        "active_standalone_id": active_standalone_id,
        "active_cli_id": active_cli_id,
        "auto_rotate_429": auto_rotate_429,
        "items": items,
    }


_TOKEN_REFRESH_COOLDOWN: dict[str, float] = {}


def _import_account_switcher() -> Any | None:
    search_dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        search_dirs.append(Path(meipass) / "extensions" / "core")
    search_dirs.append(Path(__file__).resolve().parent.parent.parent / "extensions" / "core")
    search_dirs.append(_tools_dir())

    acs_dir = next((d for d in search_dirs if (d / "account_switcher.py").is_file()), None)
    if acs_dir is None:
        return None
    if str(acs_dir) not in sys.path:
        sys.path.insert(0, str(acs_dir))
    try:
        import account_switcher  # type: ignore
        return account_switcher
    except Exception:
        return None


def _maybe_refresh_stale_active_tokens(active_ids: list[str]) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    tools = _tools_dir()
    acc_dir = tools / "accounts"
    if not acc_dir.is_dir():
        return

    now = time.time()
    stale_ids: list[str] = []
    for aid in dict.fromkeys(x for x in active_ids if x):
        if now - _TOKEN_REFRESH_COOLDOWN.get(aid, 0.0) < 45.0:
            continue
        fp = acc_dir / f"{aid}.json"
        if not fp.is_file():
            continue
        raw = _read_json(fp, {})
        if not isinstance(raw, dict):
            continue
        tok = raw.get("token") if isinstance(raw.get("token"), dict) else {}
        q = raw.get("quota") if isinstance(raw.get("quota"), dict) else {}
        exp_ts = float(tok.get("expiry_timestamp") or 0.0)
        q_ts = float(q.get("quota_updated_at") or q.get("last_updated") or 0.0)
        has_ref = bool(_safe_str(tok.get("refresh_token")))
        if has_ref and (exp_ts < now + 180.0 or q_ts < now - 240.0):
            _TOKEN_REFRESH_COOLDOWN[aid] = now
            stale_ids.append(aid)

    if not stale_ids:
        return

    import threading

    def _bg_refresh(ids: list[str]) -> None:
        acs = _import_account_switcher()
        if acs is None:
            return
        for target_aid in ids:
            try:
                acs.fetch_live_quota(target_aid, update_index=True)
            except Exception:
                pass

    threading.Thread(target=_bg_refresh, args=(stale_ids,), daemon=True).start()


_STALE_SWARM_ACTIVE_SEC = 20 * 60
_ARCHIVE_SWARM_SEC = 24 * 3600


def _uri_to_local_path(uri: str) -> str:
    raw = _safe_str(uri)
    if not raw:
        return ""
    if raw.startswith("file:///"):
        unquoted = urllib.parse.unquote(raw[len("file:///"):])
        if os.name == "nt":
            if len(unquoted) >= 2 and unquoted[1] == ":":
                unquoted = unquoted[0].upper() + unquoted[1:]
            return unquoted.replace("/", "\\")
        return "/" + unquoted.lstrip("/")
    return raw


def _resolve_repo_root_and_name(raw_path: str) -> tuple[str, str]:
    p_str = _safe_str(raw_path)
    if not p_str:
        return "", ""
    p = Path(p_str)
    if p.exists():
        # Walk up to 4 levels to find a .git root so subdirs / worktrees don't show up as "repo" or "1"
        cur = p
        for _ in range(5):
            if (cur / ".git").exists():
                return str(cur), cur.name or str(cur)
            if cur.parent == cur:
                break
            cur = cur.parent
    name = p.name or p_str
    generic_names = {"repo", "src", "app", "worktree", "main"}
    if (name.lower() in generic_names or (len(name) <= 2 and name.isdigit())) and p.parent.name:
        name = f"{p.parent.name}/{name}"
    return p_str, name


def _ide_storage_dirs() -> list[Path]:
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        return [base / "Antigravity IDE" / "User" / "globalStorage", base / "Antigravity" / "User" / "globalStorage"]
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        return [base / "Antigravity IDE" / "User" / "globalStorage", base / "Antigravity" / "User" / "globalStorage"]
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    return [base / "Antigravity IDE" / "User" / "globalStorage", base / "Antigravity" / "User" / "globalStorage"]


def _collect_ide_workspaces() -> list[dict[str, Any]]:
    # Skip scanning user-wide IDE storage when tests explicitly isolate AG_TOOLS_DIR under pytest tmp
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return []

    found: list[dict[str, Any]] = []
    seen_norm: set[str] = set()

    def _push(raw_uri: str, is_open_win: bool, idx: int) -> None:
        local = _uri_to_local_path(raw_uri)
        if not local or not Path(local).exists():
            return
        canon, name = _resolve_repo_root_and_name(local)
        if not canon:
            return
        norm = os.path.normcase(canon)
        if norm in seen_norm:
            return
        seen_norm.add(norm)
        found.append({
            "id": f"ide:{name}:{idx}",
            "name": name,
            "path": canon,
            "title": "IDE · открыт" if is_open_win else "IDE · проект",
            "active_agents": 0,
            "total_agents": 0,
            "status": "open" if is_open_win else "idle",
            "elapsed_sec": 0,
            "updated_at": time.time() - idx if is_open_win else 0.0,
            "_priority": 1 if is_open_win else 2,
        })

    for gs_dir in _ide_storage_dirs():
        if not gs_dir.is_dir():
            continue
        st = _read_json(gs_dir / "storage.json", {})
        if isinstance(st, dict):
            ws = st.get("windowsState") if isinstance(st.get("windowsState"), dict) else {}
            opened = ws.get("openedWindows") if isinstance(ws.get("openedWindows"), list) else []
            for i, win in enumerate(opened[:6]):
                if isinstance(win, dict):
                    _push(_safe_str(win.get("folder")), True, i)
            # lastActiveWindow is NOT necessarily open right now; treat as idle recent workspace
            last_win = ws.get("lastActiveWindow")
            if isinstance(last_win, dict):
                _push(_safe_str(last_win.get("folder")), False, len(found))

        vsc_db = gs_dir / "state.vscdb"
        if vsc_db.is_file():
            con = None
            try:
                con = sqlite3.connect(f"file:{vsc_db.as_posix()}?mode=ro", uri=True, timeout=0.4)
                row = con.execute("SELECT value FROM ItemTable WHERE key='history.recentlyOpenedPathsList'").fetchone()
                if row and row[0]:
                    entries = json.loads(row[0]).get("entries", [])
                    if isinstance(entries, list):
                        for i, e in enumerate(entries[:8]):
                            if isinstance(e, dict):
                                f_uri = _safe_str(e.get("folderUri") or (e.get("workspace") or {}).get("configPath"))
                                _push(f_uri, False, 10 + i)
            except Exception:
                pass
            finally:
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        pass
    return found


def _gemini_app_data_dirs() -> list[Path]:
    env_dir = os.environ.get("AG_GEMINI_DIR", "").strip()
    if env_dir:
        return [Path(env_dir)]
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    home = Path.home() / ".gemini"
    return [d for d in (home / "antigravity", home / "antigravity-ide") if d.is_dir()]


def _parse_iso_ts(raw: Any, fallback: float = 0.0) -> float:
    txt = _safe_str(raw)
    if not txt or txt.startswith("0001-01-01"):
        return fallback
    try:
        from datetime import datetime
        norm = txt.replace("Z", "+00:00")
        # Handle >6 fractional digits from Go/RFC3339Nano timestamps (e.g. .0824611+00:00)
        if "." in norm:
            head, rest = norm.split(".", 1)
            tz_pos = max(rest.find("+"), rest.find("-"))
            if tz_pos != -1:
                frac = rest[:tz_pos][:6]
                tz = rest[tz_pos:]
                norm = f"{head}.{frac}{tz}"
            else:
                norm = f"{head}.{rest[:6]}"
        return datetime.fromisoformat(norm).timestamp()
    except Exception:
        return fallback


def _is_transcript_actively_streaming(gdir: Path, cid: str, now_ts: float) -> bool:
    tpath = gdir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        st = tpath.stat()
    except OSError:
        return False
    if now_ts - st.st_mtime > 25.0 or st.st_size <= 0:
        return False
    try:
        with tpath.open("rb") as f:
            f.seek(max(0, st.st_size - 2048))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for raw_line in reversed(tail):
            s = raw_line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if obj.get("type") == "PLANNER_RESPONSE" and obj.get("status") == "DONE":
                    return False
                return True
            except Exception:
                # Truncated or non-JSON tail written very recently means active streaming
                return True
    except OSError:
        return False
    return False


def _read_live_activity(gdir: Path, cid: str) -> str:
    tpath = gdir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        st = tpath.stat()
        if st.st_size <= 0:
            return ""
        with tpath.open("rb") as f:
            f.seek(max(0, st.st_size - 4096))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for raw_line in reversed(tail):
            s = raw_line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            tcalls = obj.get("tool_calls")
            if isinstance(tcalls, list) and tcalls:
                last_tc = tcalls[-1] if isinstance(tcalls[-1], dict) else {}
                name = _safe_str(last_tc.get("name"))
                args = last_tc.get("args") if isinstance(last_tc.get("args"), dict) else {}
                if name == "run_command":
                    cmd = _safe_str(args.get("CommandLine") or args.get("toolSummary"))[:52]
                    return f"Команда: {cmd}" if cmd else "Выполняет команду"
                if name in ("edit_file", "write_file", "replace_file_content"):
                    fp = _safe_str(args.get("Path") or args.get("TargetFile"))
                    return f"Правка: {Path(fp).name}" if fp else "Редактирует файл"
                if name in ("read_file", "view_file"):
                    fp = _safe_str(args.get("Path") or args.get("AbsolutePath"))
                    return f"Чтение: {Path(fp).name}" if fp else "Читает файл"
                if name in ("search_web", "grep_search"):
                    q = _safe_str(args.get("query") or args.get("Query"))[:44]
                    return f"Поиск: {q}" if q else "Поиск"
                if name:
                    return f"Инструмент: {name}"
            if obj.get("type") == "USER_INPUT":
                txt = _safe_str(obj.get("content"))[:52]
                if txt:
                    return f"Запрос: {txt}"
            if obj.get("type") == "PLANNER_RESPONSE" and obj.get("status") == "DONE":
                return "Шаг завершён, ждёт ответа"
            if obj.get("type") == "PLANNER_RESPONSE":
                return "Думает / отвечает..."
    except OSError:
        return ""
    return ""


def _detect_app_process_label(gdir: Path) -> str:
    if gdir.name == "antigravity-ide":
        return "Antigravity IDE"
    daemon_dir = gdir / "daemon"
    if daemon_dir.is_dir():
        try:
            hubs = sorted(daemon_dir.glob("hub_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for hb in hubs[:2]:
                data = _read_json(hb, {})
                if isinstance(data, dict) and data.get("pid"):
                    return f"Antigravity 2.0 (PID {data['pid']})"
        except Exception:
            pass
    return "Antigravity 2.0"


def _collect_live_conversations(now_ts: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for gdir in _gemini_app_data_dirs():
        db_path = gdir / "conversation_summaries.db"
        if not db_path.is_file():
            continue
        app_proc_label = _detect_app_process_label(gdir)
        con = None
        rows: list[tuple[Any, ...]] = []
        try:
            con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=0.5)
            rows = con.execute(
                """
                SELECT conversation_id, title, workspace_uris, status, not_fully_idle,
                       killed, last_modified_time, last_user_input_time,
                       parent_conversation_id, agent_name
                FROM conversation_summaries
                ORDER BY last_modified_time DESC
                LIMIT 120
                """
            ).fetchall()
        except Exception:
            rows = []
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

        if not rows:
            continue

        row_map: dict[str, dict[str, Any]] = {}
        children_by_parent: dict[str, list[dict[str, Any]]] = {}

        for r in rows:
            cid = _safe_str(r[0])
            if not cid:
                continue
            is_killed = bool(r[5])
            status_str = _safe_str(r[3])
            not_idle = int(r[4] or 0) == 1
            mod_ts = _parse_iso_ts(r[6], 0.0)
            input_ts = _parse_iso_ts(r[7], 0.0)
            parent_id = _safe_str(r[8])
            agent_name = _safe_str(r[9])

            is_running = not is_killed and (
                status_str == "CASCADE_RUN_STATUS_RUNNING"
                or (not_idle and (now_ts - mod_ts) <= _STALE_SWARM_ACTIVE_SEC)
                or _is_transcript_actively_streaming(gdir, cid, now_ts)
            )

            proj_path = ""
            proj_name = ""
            try:
                uris = json.loads(r[2] or "[]")
                if isinstance(uris, list) and uris:
                    local_p = _uri_to_local_path(_safe_str(uris[0]))
                    if local_p:
                        proj_path, proj_name = _resolve_repo_root_and_name(local_p)
            except Exception:
                pass

            info = {
                "cid": cid,
                "title": _safe_str(r[1]) or agent_name or "Агент",
                "proj_path": proj_path,
                "proj_name": proj_name or "Antigravity",
                "is_running": is_running,
                "is_killed": is_killed,
                "mod_ts": mod_ts,
                "input_ts": input_ts,
                "parent_id": parent_id,
            }
            row_map[cid] = info
            if parent_id:
                children_by_parent.setdefault(parent_id, []).append(info)

        consumed_cids: set[str] = set()

        # 1. Active native swarms inside brain/<cid>/.system_generated/swarm
        brain_dir = gdir / "brain"
        if brain_dir.is_dir():
            for sw_dir in brain_dir.glob("*/.system_generated/swarm"):
                cid = sw_dir.parent.parent.name
                members_path = sw_dir / "members.json"
                members_meta = _read_json(members_path, {})
                raw_members = members_meta.get("members") if isinstance(members_meta, dict) else None
                if not isinstance(raw_members, dict) or not raw_members:
                    continue

                board_path = sw_dir / "board.jsonl"
                try:
                    sw_mtime = max(
                        members_path.stat().st_mtime if members_path.is_file() else 0.0,
                        board_path.stat().st_mtime if board_path.is_file() else 0.0,
                    )
                except OSError:
                    sw_mtime = 0.0
                swarm_is_fresh = sw_mtime > 0 and (now_ts - sw_mtime) <= _STALE_SWARM_ACTIVE_SEC

                board_done = False
                if board_path.is_file():
                    try:
                        btxt = board_path.read_text(encoding="utf-8", errors="replace")
                        for line in reversed(btxt.splitlines()[-12:]):
                            try:
                                bobj = json.loads(line)
                                if bobj.get("agent") == "coordinator" and str(bobj.get("text", "")).strip().lower().startswith("[done]"):
                                    board_done = True
                                    break
                            except Exception:
                                pass
                    except OSError:
                        pass

                parent_info = row_map.get(cid)
                parent_running = bool(parent_info and parent_info["is_running"])
                running_workers = 0
                active_member_cids: list[str] = []

                for m_name, m_val in raw_members.items():
                    if not isinstance(m_val, dict):
                        continue
                    mcid = _safe_str(m_val.get("conversation_id"))
                    if m_val.get("killed") or _safe_str(m_val.get("phase")).upper() == "KILLED":
                        continue
                    m_info = row_map.get(mcid) if mcid else None
                    if m_name == "coordinator" or m_val.get("role") == "coordinator":
                        if parent_running and swarm_is_fresh and not board_done:
                            running_workers += 1
                    elif m_info and m_info["is_running"]:
                        running_workers += 1
                        if mcid:
                            active_member_cids.append(mcid)
                    elif m_info is None and parent_running and swarm_is_fresh and not board_done and _safe_str(m_val.get("phase")).upper() == "RUNNING":
                        running_workers += 1

                # Only claim parent_info as a Swarm if fresh/non-done OR live swarm workers exist;
                # otherwise let standalone live conversation tracking handle any later chat in the same ID.
                if running_workers > 0 and parent_info and (swarm_is_fresh or active_member_cids) and not board_done:
                    consumed_cids.add(cid)
                    for mcid in active_member_cids:
                        consumed_cids.add(mcid)
                    started = parent_info["input_ts"] or parent_info["mod_ts"] or now_ts
                    elapsed = max(1, min(6 * 3600, int(now_ts - started)))
                    swarm_agents: list[dict[str, Any]] = []
                    for m_name, m_val in raw_members.items():
                        if not isinstance(m_val, dict):
                            continue
                        mcid = _safe_str(m_val.get("conversation_id"))
                        m_info = row_map.get(mcid) if mcid else None
                        is_m_run = (
                            (m_name == "coordinator" or m_val.get("role") == "coordinator") and parent_running
                        ) or bool(m_info and m_info["is_running"]) or (
                            m_info is None and parent_running and _safe_str(m_val.get("phase")).upper() == "RUNNING"
                        )
                        if is_m_run:
                            swarm_agents.append({
                                "id": mcid or f"{cid}:{m_name}",
                                "title": f"@{m_name}: {(m_info or {}).get('title') or _safe_str(m_val.get('role') or 'worker')}",
                                "activity": _read_live_activity(gdir, mcid) if mcid else "Работает в Swarm",
                                "process": app_proc_label,
                                "status": "running",
                                "elapsed_sec": elapsed,
                            })
                    candidates.append({
                        "id": f"conv:{cid}",
                        "name": parent_info["proj_name"],
                        "path": parent_info["proj_path"],
                        "title": _safe_str(members_meta.get("title")) or parent_info["title"],
                        "activity": _read_live_activity(gdir, cid) or "Координирует рой агентов",
                        "process": app_proc_label,
                        "active_agents": running_workers,
                        "total_agents": len(raw_members),
                        "status": "running",
                        "elapsed_sec": elapsed,
                        "updated_at": parent_info["mod_ts"] or now_ts,
                        "agents": swarm_agents,
                        "_priority": 0,
                        "_is_live_agent": True,
                    })

        # 2. Standalone & subagent-tree live conversations
        for cid, info in row_map.items():
            if cid in consumed_cids or info["parent_id"]:
                continue
            children = [ch for ch in children_by_parent.get(cid, []) if ch["cid"] not in consumed_cids]
            active_children = sum(1 for ch in children if ch["is_running"])
            if not info["is_running"] and active_children == 0:
                continue

            consumed_cids.add(cid)
            for ch in children:
                consumed_cids.add(ch["cid"])

            active_count = (1 if info["is_running"] else 0) + active_children
            total_count = 1 + len(children)
            started = info["input_ts"] or info["mod_ts"] or now_ts
            elapsed = max(1, min(6 * 3600, int(now_ts - started)))
            conv_agents: list[dict[str, Any]] = []
            parent_act = _read_live_activity(gdir, cid)
            if info["is_running"]:
                conv_agents.append({
                    "id": cid,
                    "title": info["title"],
                    "activity": parent_act or "Активная сессия",
                    "process": app_proc_label,
                    "status": "running",
                    "elapsed_sec": elapsed,
                })
            for ch in children:
                if ch["is_running"]:
                    ch_start = ch["input_ts"] or ch["mod_ts"] or now_ts
                    conv_agents.append({
                        "id": ch["cid"],
                        "title": ch["title"],
                        "activity": _read_live_activity(gdir, ch["cid"]) or "Субагент работает",
                        "process": app_proc_label,
                        "status": "running",
                        "elapsed_sec": max(1, min(6 * 3600, int(now_ts - ch_start))),
                    })
            candidates.append({
                "id": f"conv:{cid}",
                "name": info["proj_name"],
                "path": info["proj_path"],
                "title": info["title"],
                "activity": parent_act or (conv_agents[0]["activity"] if conv_agents else "Активен"),
                "process": app_proc_label,
                "active_agents": active_count,
                "total_agents": total_count,
                "status": "running",
                "elapsed_sec": elapsed,
                "updated_at": info["mod_ts"] or now_ts,
                "agents": conv_agents,
                "_priority": 0,
                "_is_live_agent": True,
            })

    return candidates


def _collect_projects() -> list[dict[str, Any]]:
    tools = _tools_dir()
    candidates: list[dict[str, Any]] = []
    now_ts = time.time()

    # 1. Live active Antigravity chats / native swarms / subagents
    candidates.extend(_collect_live_conversations(now_ts))

    # 2. Swarm Engine checkpoints
    swarm_dir = tools / "swarm"
    if swarm_dir.is_dir():
        state_files = sorted(
            swarm_dir.glob("*/state.json"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
            reverse=True,
        )
        for sf in state_files[:12]:
            st = _read_json(sf)
            if not isinstance(st, dict):
                continue
            raw_path = _safe_str(st.get("project_path"))
            canon_path, proj_name = _resolve_repo_root_and_name(raw_path)
            if not proj_name:
                proj_name = _safe_str(st.get("task_id"))[:8] or "Workspace"
            briefs = st.get("briefs") if isinstance(st.get("briefs"), dict) else {}
            results = st.get("worker_results") if isinstance(st.get("worker_results"), dict) else {}
            is_complete = bool(st.get("is_complete"))

            try:
                sf_mtime = float(sf.stat().st_mtime)
            except Exception:
                sf_mtime = now_ts
            age_sec = max(0.0, now_ts - sf_mtime)

            created_at = st.get("created_at") or sf_mtime
            try:
                created_ts = float(created_at)
            except Exception:
                created_ts = sf_mtime

            total_agents = len(briefs) or len(results)
            if not is_complete and age_sec <= _STALE_SWARM_ACTIVE_SEC:
                active_workers = max(1, len(briefs) - len(results)) if briefs else 1
                status = "running"
                elapsed_sec = max(1, min(_STALE_SWARM_ACTIVE_SEC * 3, int(now_ts - created_ts)))
                title = "Swarm"
                priority = 0
            elif not is_complete:
                # Abandoned/stale checkpoint older than 20 minutes: never report as actively running
                active_workers = 0
                status = "paused"
                elapsed_sec = 0
                title = "Swarm · пауза"
                priority = 4 if age_sec > _ARCHIVE_SWARM_SEC else 2
            else:
                active_workers = 0
                status = "done"
                elapsed_sec = 0
                title = f"Swarm · {total_agents} аг." if total_agents > 0 else "Swarm · завершён"
                priority = 4 if age_sec > _ARCHIVE_SWARM_SEC else 3

            candidates.append({
                "id": _safe_str(st.get("task_id") or sf.parent.name),
                "name": proj_name,
                "path": canon_path or raw_path,
                "title": title,
                "active_agents": active_workers,
                "total_agents": total_agents,
                "status": status,
                "elapsed_sec": elapsed_sec,
                "updated_at": sf_mtime,
                "agents": [
                    {
                        "id": f"{sf.parent.name}:{w_name}",
                        "title": f"@{w_name}: {_safe_str(w_brief)[:60]}",
                        "status": "done" if w_name in results else ("running" if status == "running" else "paused"),
                        "elapsed_sec": elapsed_sec if status == "running" else 0,
                    }
                    for w_name, w_brief in briefs.items()
                ],
                "_priority": priority,
            })

    # 3. Live / recent Antigravity IDE workspaces take precedence over archived swarm test leftovers
    candidates.extend(_collect_ide_workspaces())

    recent_list = _read_json(tools / "swarm_projects.json", [])
    if isinstance(recent_list, list):
        for raw_p in recent_list[:8]:
            canon_path, proj_name = _resolve_repo_root_and_name(_safe_str(raw_p))
            if not canon_path:
                continue
            candidates.append({
                "id": f"proj:{proj_name}",
                "name": proj_name,
                "path": canon_path,
                "title": "Workspace",
                "active_agents": 0,
                "total_agents": 0,
                "status": "idle",
                "elapsed_sec": 0,
                "updated_at": 0,
                "_priority": 3,
            })

    # Aggregate active agents by normalized project path while preserving primary project entry
    candidates.sort(key=lambda item: (item.get("_priority", 5), -(float(item.get("updated_at") or 0.0))))
    projects: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    has_fresh = any(c.get("_priority", 5) < 4 for c in candidates)

    for item in candidates:
        prio = item.pop("_priority", 5)
        item.pop("_is_live_agent", None)
        if has_fresh and prio >= 4:
            continue
        p_norm = os.path.normcase(_safe_str(item.get("path"))) or item.get("id", "")
        existing = by_path.get(p_norm)
        if existing is None:
            if not isinstance(item.get("agents"), list):
                item["agents"] = []
            by_path[p_norm] = item
            projects.append(item)
        else:
            # Sum concurrent live agents running in the same workspace
            if item.get("status") == "running" and item.get("active_agents", 0) > 0:
                existing["active_agents"] = int(existing.get("active_agents") or 0) + int(item.get("active_agents") or 0)
                existing["total_agents"] = int(existing.get("total_agents") or 0) + int(item.get("total_agents") or 0)
                existing["status"] = "running"
                existing["updated_at"] = max(float(existing.get("updated_at") or 0.0), float(item.get("updated_at") or 0.0))
                if isinstance(item.get("agents"), list):
                    existing.setdefault("agents", []).extend(item["agents"])

    _enrich_projects_with_studio_tasks(projects, tools)
    return projects[:10]


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            exit_code = ctypes.c_ulong(0)
            ok_flag = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return bool(ok_flag) and exit_code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _enrich_projects_with_studio_tasks(projects: list[dict[str, Any]], tools: Path) -> None:
    tasks_map = _read_json(tools / "studio_tasks.json", {})
    if not isinstance(tasks_map, dict) or not tasks_map:
        return
    for proj in projects:
        p_norm = os.path.normcase(_safe_str(proj.get("path")))
        if not p_norm or p_norm not in tasks_map:
            continue
        tinfo = tasks_map.get(p_norm)
        if not isinstance(tinfo, dict):
            continue
        pid = int(tinfo.get("pid") or 0)
        alive = _is_pid_alive(pid)
        log_f = _safe_str(tinfo.get("log_file"))
        if log_f and os.path.isfile(log_f):
            proj["task_log_file"] = log_f
        proj["task_pid"] = pid if alive else 0
        proj["task_running"] = alive
        if alive and proj.get("status") != "running":
            proj["status"] = "running"
            proj["active_agents"] = max(1, int(proj.get("active_agents") or 0))
            proj["process"] = f"agy CLI (PID {pid})"
            started = float(tinfo.get("started_at") or time.time())
            proj["elapsed_sec"] = max(1, int(time.time() - started))


def _resolve_custom_target_file(target: str, raw_path: str) -> str:
    p = Path(_safe_str(raw_path))
    if not p.exists():
        return ""
    if p.is_file():
        return str(p)
    if target == "ide":
        for rel in ("resources/app/out/main.js", "app/out/main.js", "out/main.js", "main.js"):
            cand = p / rel
            if cand.is_file():
                return str(cand)
    elif target == "manager":
        for rel in (
            "resources/bin/language_server.exe",
            "resources/bin/language_server",
            "Contents/Resources/bin/language_server",
            "language_server.exe",
            "language_server",
        ):
            cand = p / rel
            if cand.is_file():
                return str(cand)
    elif target == "cli":
        for rel in ("agy.exe", "agy", "bin/agy.exe", "bin/agy"):
            cand = p / rel
            if cand.is_file():
                return str(cand)
    elif target == "vscode":
        for rel in ("extension.js", "out/extension.js"):
            cand = p / rel
            if cand.is_file():
                return str(cand)
    return ""


def _inspect_targets_safe() -> dict[str, Any]:
    from patcher.app_updater import get_installed_app_version
    from patcher.auto_repatch import inspect_targets
    from patcher.cli import _resolve_ide_root_for_lock
    from patcher.updater_lock import (
        is_cli_locked,
        is_ide_locked,
        is_manager_locked,
        is_vscode_locked,
    )

    custom_cfg = _read_json(_tools_dir() / "custom_paths.json", {})
    if not isinstance(custom_cfg, dict):
        custom_cfg = {}

    state = inspect_targets(
        main_js_path=_resolve_custom_target_file("ide", custom_cfg.get("ide", "")),
        manager_path=_resolve_custom_target_file("manager", custom_cfg.get("manager", "")),
        agy_path=_resolve_custom_target_file("cli", custom_cfg.get("cli", "")),
        vscode_path=_resolve_custom_target_file("vscode", custom_cfg.get("vscode", "")),
    )
    main_js = _safe_str(state.get("main_js_path"))
    mgr_path = _safe_str(state.get("manager_path"))
    agy_path = _safe_str(state.get("agy_path"))
    vsc_path = _safe_str(state.get("vscode_path"))
    vsc_agy_path = _safe_str(state.get("vscode_agy_path"))

    ide_root = _resolve_ide_root_for_lock(main_js) if main_js else ""

    def _ver(target: str, p: str) -> str:
        if not p:
            return ""
        try:
            return _safe_str(get_installed_app_version(target, p))
        except Exception:
            return ""

    return {
        "ide": {
            "installed": bool(main_js),
            "path": main_js,
            "version": _ver("ide", main_js),
            "patched": bool(main_js) and not bool(state.get("needs_ide")) and not bool(state.get("needs_tls")),
            "needs_patch": bool(state.get("needs_ide") or state.get("needs_tls") or state.get("needs_ext")),
            "locked": bool(main_js) and bool(is_ide_locked(ide_root or None)),
        },
        "manager": {
            "installed": bool(mgr_path),
            "path": mgr_path,
            "version": _ver("manager", mgr_path),
            "patched": bool(mgr_path) and not bool(state.get("needs_mgr")) and not bool(state.get("needs_ui")),
            "needs_patch": bool(state.get("needs_mgr") or state.get("needs_ui")),
            "locked": bool(mgr_path) and bool(is_manager_locked()),
        },
        "cli": {
            "installed": bool(agy_path),
            "path": agy_path,
            "version": _ver("cli", agy_path),
            "patched": bool(agy_path) and not bool(state.get("needs_agy")),
            "needs_patch": bool(state.get("needs_agy")),
            "locked": bool(agy_path) and bool(is_cli_locked()),
        },
        "vscode": {
            "installed": bool(vsc_path or vsc_agy_path),
            "path": vsc_path or vsc_agy_path,
            "version": "",
            "patched": bool(vsc_path or vsc_agy_path) and not bool(state.get("needs_vsc")),
            "needs_patch": bool(state.get("needs_vsc")),
            "locked": bool(vsc_path or vsc_agy_path) and bool(is_vscode_locked()),
        },
        "needs_any": bool(state.get("needs_any")),
    }


def _collect_network_status() -> dict[str, Any]:
    from patcher.hosts_pin import is_hosts_pinned
    from patcher.nrpt_dns import (
        PUBLIC_UNBLOCK_SERVERS,
        get_dns_config,
        get_nrpt_status,
        get_preferred_dns_ip,
    )

    nrpt = {}
    try:
        nrpt = get_nrpt_status() or {}
    except Exception:
        nrpt = {}

    dns_cfg = {}
    try:
        dns_cfg = get_dns_config() or {}
    except Exception:
        dns_cfg = {}

    pinned = False
    hosts_ip = ""
    try:
        pinned, hosts_ip = is_hosts_pinned()
    except Exception:
        pinned, hosts_ip = False, ""

    proxy_cfg = _read_json(_tools_dir() / "proxy_config.json", {})
    if not isinstance(proxy_cfg, dict):
        proxy_cfg = {}
    proxy_url = _safe_str(proxy_cfg.get("proxy_url") or proxy_cfg.get("url"))
    proxy_enabled = bool(proxy_cfg.get("enabled", bool(proxy_url)))

    plat = sys.platform
    if plat == "win32":
        dns_method = "Windows NRPT"
    elif plat == "darwin":
        dns_method = "macOS /etc/resolver + DoH"
    else:
        dns_method = "systemd-resolved / Hosts"

    # Collect real executable paths for VPN split-tunneling exclusion copy
    vpn_paths: list[str] = []
    if plat == "win32":
        local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        for cand in [
            local_app / "Programs" / "antigravity" / "Antigravity.exe",
            local_app / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe",
            local_app / "AntigravityPatcher" / "Antigravity_Patcher_Windows_x64.exe",
            Path.home() / ".gemini" / "bin" / "agy.exe",
        ]:
            if cand.exists():
                vpn_paths.append(str(cand))

    return {
        "platform": plat,
        "os_label": platform.system(),
        "dns_method": dns_method,
        "nrpt_supported": bool(nrpt.get("supported", plat == "win32")),
        "nrpt_active": bool(nrpt.get("active") or nrpt.get("enabled")),
        "nrpt_dns_ip": _safe_str(nrpt.get("server") or nrpt.get("dns_ip") or get_preferred_dns_ip(dns_cfg)),
        "custom_dns": _safe_str(dns_cfg.get("custom_ip") or (dns_cfg.get("server_ip") if dns_cfg.get("mode") == "private" else "")),
        "dns_mode": _safe_str(dns_cfg.get("mode") or "public"),
        "public_servers": [{"name": s.get("name", ""), "ip": s.get("ip", "")} for s in PUBLIC_UNBLOCK_SERVERS],
        "hosts_pinned": bool(pinned),
        "hosts_ip": _safe_str(hosts_ip),
        "proxy_enabled": proxy_enabled,
        "proxy_url": proxy_url,
        "vpn_exclusion_paths": vpn_paths,
        "split_tunnel_available": False,  # Stage 3 preview
    }


def _collect_recent_logs(max_lines: int = 30) -> list[str]:
    tools = _tools_dir()
    lines_out: list[str] = []
    for fname in ("auto_repatch.log", "autopilot.log"):
        lp = tools / fname
        if lp.is_file():
            try:
                raw = lp.read_text(encoding="utf-8", errors="replace").splitlines()
                lines_out.extend(raw[-max_lines:])
            except OSError:
                pass
    return lines_out[-max_lines:]


_AUTOSTART_TASK_NAME = "AntigravityStudioAutostart"


def _is_autostart_enabled() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        cfg = _read_json(_tools_dir() / "autostart_state.json", {})
        return bool(cfg.get("enabled")) if isinstance(cfg, dict) else False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", _AUTOSTART_TASK_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,
                timeout=3,
            )
            return r.returncode == 0
        except Exception:
            return False
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / "com.antigravity.studio.plist").is_file()
    return (Path.home() / ".config" / "autostart" / "antigravity-studio.desktop").is_file()


_HEAVY_SNAPSHOT_CACHE: dict[str, Any] = {"ts": 0.0, "tools_dir": "", "data": None}


def _invalidate_heavy_snapshot_cache() -> None:
    _HEAVY_SNAPSHOT_CACHE["ts"] = 0.0
    _HEAVY_SNAPSHOT_CACHE["data"] = None


def get_studio_snapshot(
    include_accounts: bool = True,
    include_projects: bool = True,
    fast: bool = False,
) -> dict[str, Any]:
    """Collect full system, target, network, account, and project status as a JSON dict."""
    from patcher.constants import VERSION

    tools_str = str(_tools_dir())
    now = time.time()
    use_cache = (
        not os.environ.get("PYTEST_CURRENT_TEST")
        and _HEAVY_SNAPSHOT_CACHE.get("data") is not None
        and _HEAVY_SNAPSHOT_CACHE.get("tools_dir") == tools_str
        and (fast or (now - float(_HEAVY_SNAPSHOT_CACHE.get("ts") or 0.0)) < 45.0)
    )

    if use_cache:
        heavy = dict(_HEAVY_SNAPSHOT_CACHE["data"])
    else:
        from patcher.auto_repatch import get_auto_repatch_status
        from patcher.diagnostics import load_auto_heal_config
        from patcher.utils.admin import is_admin

        ap_raw = _read_json(_tools_dir() / "autopilot_state.json", {})
        heal_cfg = load_auto_heal_config()
        try:
            repatch_st = get_auto_repatch_status() or {}
        except Exception:
            repatch_st = {}

        ap_summary = {
            "last_run_ts": ap_raw.get("last_run_ts", 0) if isinstance(ap_raw, dict) else 0,
            "last_status": _safe_str(ap_raw.get("last_status", "idle")) if isinstance(ap_raw, dict) else "idle",
            "connectivity_ok": bool(ap_raw.get("connectivity_ok", True)) if isinstance(ap_raw, dict) else True,
            "diagnosis": _safe_str(ap_raw.get("diagnosis", "")) if isinstance(ap_raw, dict) else "",
            "last_issue": _safe_str(ap_raw.get("last_issue", "")) if isinstance(ap_raw, dict) else "",
            "last_actions": ap_raw.get("last_actions", []) if isinstance(ap_raw, dict) else [],
            "healed_count": int(ap_raw.get("healed_count", 0) or 0) if isinstance(ap_raw, dict) else 0,
            "auto_heal_enabled": bool(heal_cfg.get("enabled", True)),
            "auto_repatch_enabled": bool(repatch_st.get("enabled", False)),
        }

        try:
            admin_flag = bool(is_admin())
        except Exception:
            admin_flag = False

        heavy = {
            "is_admin": admin_flag,
            "autostart_enabled": _is_autostart_enabled(),
            "targets": _inspect_targets_safe(),
            "network": _collect_network_status(),
            "autopilot": ap_summary,
            "logs": _collect_recent_logs(25),
        }
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            _HEAVY_SNAPSHOT_CACHE["ts"] = now
            _HEAVY_SNAPSHOT_CACHE["tools_dir"] = tools_str
            _HEAVY_SNAPSHOT_CACHE["data"] = dict(heavy)

    snap: dict[str, Any] = {
        "ok": True,
        "patcher_version": VERSION,
        "platform": sys.platform,
        "is_admin": heavy["is_admin"],
        "autostart_enabled": heavy.get("autostart_enabled", False),
        "generated_at": int(now),
        "targets": heavy["targets"],
        "network": heavy["network"],
        "autopilot": heavy["autopilot"],
        "logs": heavy["logs"],
    }
    if include_accounts:
        snap["accounts"] = _collect_accounts()
    if include_projects:
        snap["projects"] = _collect_projects()
    return snap


def _spawn_account_switch(account_id: str, app: str, no_restart: bool = False) -> dict[str, Any]:
    if app not in ("ide", "standalone", "code", "cli"):
        return {"ok": False, "error": f"Unsupported target app: {app}"}

    is_frozen = bool(getattr(sys, "frozen", False))
    runner: Path | None = None
    if not is_frozen:
        candidates = [
            Path(__file__).resolve().parent.parent.parent / "extensions" / "core" / "switch_runner.py",
            _tools_dir() / "switch_runner.py",
        ]
        runner = next((c for c in candidates if c.is_file()), None)
        if runner is None:
            return {"ok": False, "error": "switch_runner.py not found"}

    # Update active slot immediately & independently (IDE != Ag 2.0 != CLI invariant)
    tools = _tools_dir()
    idx_path = tools / "accounts.json"
    idx = _read_json(idx_path, {})
    if isinstance(idx, dict):
        if app == "ide":
            idx["current_ide_account_id"] = account_id
        elif app == "standalone":
            idx["current_standalone_account_id"] = account_id
        elif app == "cli":
            idx["current_cli_account_id"] = account_id
            no_restart = True
        _write_json_atomic(idx_path, idx)

    if is_frozen:
        cmd = [sys.executable, "--switch-account", account_id, app]
    else:
        cmd = [sys.executable, str(runner), "--target", account_id, "--app", app]
    if no_restart:
        cmd.append("--no-restart")

    creationflags = 0x08000000 if os.name == "nt" else 0
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
        return {"ok": True, "account_id": account_id, "app": app, "spawned": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_studio_action(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a Studio backend action non-interactively and return JSON result."""
    data = payload if isinstance(payload, dict) else {}
    act = _safe_str(action).lower()
    if act not in ("autopilot_tick", "read_task_log", "open_project_folder", "open_in_ide"):
        _invalidate_heavy_snapshot_cache()

    if act == "autopilot_tick":
        from patcher.diagnostics import run_autopilot_guardian
        from patcher.utils.instance import acquire_instance_lock, release_instance_lock

        if not acquire_instance_lock("Local\\AntigravityPatcher_AutopilotHeal"):
            return {"ok": True, "action": act, "skipped": "already_running", "guardian": {"actions": []}}
        try:
            res = run_autopilot_guardian(
                quiet=True,
                target_app=_safe_str(data.get("target") or "all").lower() or "all",
                force=bool(data.get("force", False)),
                allow_restart=bool(data.get("allow_restart", True)),
            )
            return {"ok": True, "action": act, "guardian": res}
        except Exception as exc:
            return {"ok": False, "action": act, "error": str(exc)}
        finally:
            release_instance_lock()

    if act == "toggle_auto_heal":
        from patcher.diagnostics import load_auto_heal_config, save_auto_heal_config

        cur = load_auto_heal_config()
        want = (not bool(cur.get("enabled", True))) if "enabled" not in data else bool(data.get("enabled"))
        saved = save_auto_heal_config({"enabled": want})
        return {"ok": True, "action": act, "auto_heal_enabled": bool(saved.get("enabled", True))}

    if act == "toggle_auto_repatch":
        from patcher.auto_repatch import (
            disable_auto_repatch,
            enable_auto_repatch,
            get_auto_repatch_status,
        )

        st = get_auto_repatch_status() or {}
        want = (not bool(st.get("enabled", False))) if "enabled" not in data else bool(data.get("enabled"))
        if want:
            ok_flag, msg = enable_auto_repatch()
        else:
            ok_flag, msg = disable_auto_repatch(user_initiated=True)
        return {"ok": bool(ok_flag), "action": act, "auto_repatch_enabled": bool(want if ok_flag else st.get("enabled", False)), "message": _safe_str(msg)}

    if act in ("patch", "autopilot"):
        from patcher.auto_repatch import run_auto_repatch
        from patcher.diagnostics import ensure_target_and_env_healthy, run_autopilot_guardian

        restart = bool(data.get("restart", False))
        target = _safe_str(data.get("target") or "all").lower() or "all"
        res = run_auto_repatch(restart_running=restart)
        health = {}
        guardian = {}
        if act == "autopilot":
            try:
                health = ensure_target_and_env_healthy(target=target, quiet=True) or {}
            except Exception as exc:
                health = {"error": str(exc)}
            try:
                guardian = run_autopilot_guardian(quiet=True, target_app=target) or {}
            except Exception as exc:
                guardian = {"error": str(exc)}
        return {"ok": True, "action": act, "target": target, "patch_result": res, "health": health, "guardian": guardian}

    if act == "set_custom_path":
        target = _safe_str(data.get("target")).lower()
        custom_path = _safe_str(data.get("path"))
        if target not in ("ide", "manager", "cli", "vscode"):
            return {"ok": False, "error": f"Unsupported target: {target}"}
        cfg_path = _tools_dir() / "custom_paths.json"
        cfg = _read_json(cfg_path, {})
        if not isinstance(cfg, dict):
            cfg = {}
        if custom_path:
            cfg[target] = custom_path
        else:
            cfg.pop(target, None)
        _write_json_atomic(cfg_path, cfg)
        return {"ok": True, "target": target, "path": custom_path}

    if act == "toggle_lock":
        from patcher.cli import _resolve_ide_root_for_lock
        from patcher.ide.discovery import find_install_root, find_main_js
        from patcher.updater_lock import (
            toggle_cli_updates,
            toggle_ide_updates,
            toggle_manager_updates,
            toggle_vscode_updates,
        )

        target = _safe_str(data.get("target")).lower()
        try:
            if target == "ide":
                root = find_install_root()
                main_js = find_main_js(root) if root else ""
                ide_root = _resolve_ide_root_for_lock(main_js)
                locked = toggle_ide_updates(ide_root or None)
            elif target == "manager":
                locked = toggle_manager_updates()
            elif target == "cli":
                locked = toggle_cli_updates()
            elif target == "vscode":
                locked = toggle_vscode_updates()
            else:
                return {"ok": False, "error": f"Unknown target: {target}"}
            return {"ok": True, "target": target, "locked": bool(locked)}
        except PermissionError as exc:
            return {"ok": False, "requires_admin": True, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act == "toggle_dns":
        from patcher.nrpt_dns import (
            disable_nrpt_rule,
            enable_nrpt_rule,
            get_dns_config,
            get_nrpt_status,
            get_preferred_dns_ip,
        )

        enable = data.get("enable")
        try:
            cur = get_nrpt_status() or {}
            want_enable = (not bool(cur.get("active") or cur.get("enabled"))) if enable is None else bool(enable)
            if want_enable:
                cfg = get_dns_config() or {}
                dns_ip = _safe_str(data.get("dns_ip")) or get_preferred_dns_ip(cfg)
                ok_flag, msg = enable_nrpt_rule(dns_ip)
            else:
                ok_flag, msg = disable_nrpt_rule()
            after = get_nrpt_status() or {}
            need_admin = (not bool(ok_flag)) and ("admin" in _safe_str(msg).lower() or "администратор" in _safe_str(msg).lower() or "access" in _safe_str(msg).lower() or "отказано" in _safe_str(msg).lower() or "прав" in _safe_str(msg).lower())
            return {
                "ok": bool(ok_flag),
                "active": bool(after.get("active") or after.get("enabled")),
                "requires_admin": need_admin,
                "error": "" if ok_flag else _safe_str(msg),
            }
        except PermissionError as exc:
            return {"ok": False, "requires_admin": True, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act == "set_dns":
        from patcher.nrpt_dns import enable_nrpt_rule, get_dns_config, save_dns_config

        dns_ip = _safe_str(data.get("dns_ip"))
        mode = _safe_str(data.get("mode")) or ("private" if dns_ip else "public")
        try:
            cfg = get_dns_config() or {}
            if not isinstance(cfg, dict):
                cfg = {}
            cfg["mode"] = mode
            if dns_ip:
                cfg["custom_ip"] = dns_ip
                cfg["server_ip"] = dns_ip
            save_dns_config(cfg)
            if bool(data.get("apply", True)) and dns_ip:
                ok_flag, msg = enable_nrpt_rule(dns_ip, mode=mode)
                return {"ok": bool(ok_flag), "dns_ip": dns_ip, "error": "" if ok_flag else _safe_str(msg)}
            return {"ok": True, "dns_ip": dns_ip}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act == "set_proxy":
        proxy_url = _safe_str(data.get("proxy_url"))
        enabled = bool(data.get("enabled", bool(proxy_url)))
        if not proxy_url:
            enabled = False
        cfg_path = _tools_dir() / "proxy_config.json"
        _write_json_atomic(cfg_path, {"enabled": enabled, "proxy_url": proxy_url})
        return {"ok": True, "proxy_enabled": enabled, "proxy_url": proxy_url}

    if act == "toggle_hosts":
        from patcher.hosts_pin import disable_hosts_pinning, enable_hosts_pinning, is_hosts_pinned

        enable = data.get("enable")
        try:
            cur_pinned, _ = is_hosts_pinned()
            want_enable = (not cur_pinned) if enable is None else bool(enable)
            res = enable_hosts_pinning() if want_enable else disable_hosts_pinning()
            after_pinned, after_ip = is_hosts_pinned()
            return {"ok": bool(res) if res is not None else True, "hosts_pinned": after_pinned, "hosts_ip": after_ip}
        except PermissionError as exc:
            return {"ok": False, "requires_admin": True, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act == "switch_account":
        account_id = _safe_str(data.get("account_id"))
        app = _safe_str(data.get("app") or "ide").lower()
        no_restart = bool(data.get("no_restart", False))
        if not account_id:
            return {"ok": False, "error": "account_id required"}
        return _spawn_account_switch(account_id, app, no_restart=no_restart)

    if act == "set_auto_rotate":
        enabled = bool(data.get("enabled", False))
        cfg_path = _tools_dir() / "gui_config.json"
        cfg = _read_json(cfg_path, {})
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["auto_switch_classic_depleted"] = enabled
        classic = cfg.get("classic_auto_switch")
        if not isinstance(classic, dict):
            classic = {}
        classic["enabled"] = enabled
        cfg["classic_auto_switch"] = classic
        _write_json_atomic(cfg_path, cfg)
        return {"ok": True, "auto_rotate_429": enabled}

    if act == "health_check":
        from patcher.diagnostics import ensure_target_and_env_healthy

        target = _safe_str(data.get("target") or "all")
        res = ensure_target_and_env_healthy(target=target, quiet=True) or {}
        return {"ok": True, "health": res}

    if act == "open_project_folder":
        proj_path = _safe_str(data.get("path"))
        if not proj_path or not os.path.exists(proj_path):
            return {"ok": False, "error": "Папка проекта не найдена"}
        try:
            if sys.platform == "win32":
                os.startfile(proj_path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", proj_path])
            else:
                subprocess.Popen(["xdg-open", proj_path])
            return {"ok": True, "path": proj_path}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act == "open_in_ide":
        proj_path = _safe_str(data.get("path"))
        if not proj_path or not os.path.exists(proj_path):
            return {"ok": False, "error": "Папка проекта не найдена"}
        try:
            if sys.platform == "win32":
                local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
                for exe_cand in (
                    local_app / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe",
                    local_app / "Programs" / "antigravity" / "Antigravity.exe",
                ):
                    if exe_cand.is_file():
                        subprocess.Popen([str(exe_cand), proj_path], start_new_session=True)
                        return {"ok": True, "path": proj_path, "launched": exe_cand.name}
            return {"ok": False, "error": "Antigravity IDE / App не найден"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act in ("run_project_task", "create_project_and_run"):
        from patcher.agy.discovery import find_agy_binary

        prompt_txt = _safe_str(data.get("prompt"))
        mode = _safe_str(data.get("mode") or "accept-edits")
        if mode not in ("accept-edits", "auto", "plan"):
            mode = "accept-edits"

        raw_path = _safe_str(data.get("path"))
        proj_name = _safe_str(data.get("name"))
        if act == "create_project_and_run":
            if not raw_path:
                if not proj_name:
                    return {"ok": False, "error": "Укажите путь или название нового проекта"}
                raw_path = str(Path.home() / "Projects" / proj_name)
            p_obj = Path(raw_path)
            p_obj.mkdir(parents=True, exist_ok=True)
            if bool(data.get("git_init", True)) and not (p_obj / ".git").exists():
                try:
                    subprocess.run(["git", "init"], cwd=str(p_obj), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                except Exception:
                    pass
        else:
            if not raw_path or not os.path.isdir(raw_path):
                return {"ok": False, "error": "Папка проекта не найдена"}

        # Register project in swarm_projects.json so it stays pinned in Projects tab
        tools = _tools_dir()
        sp_file = tools / "swarm_projects.json"
        sp_list = _read_json(sp_file, [])
        if not isinstance(sp_list, list):
            sp_list = []
        norm_new = os.path.normcase(os.path.abspath(raw_path))
        if not any(os.path.normcase(os.path.abspath(_safe_str(x))) == norm_new for x in sp_list):
            sp_list.insert(0, os.path.abspath(raw_path))
            _write_json_atomic(sp_file, sp_list[:20])

        if not prompt_txt:
            return {"ok": True, "path": os.path.abspath(raw_path), "created": act == "create_project_and_run", "spawned": False}

        agy_bin = find_agy_binary() or ""
        if not agy_bin or not os.path.isfile(agy_bin):
            return {"ok": False, "error": "CLI-агент agy не найден (~/.gemini/bin/agy.exe)"}

        log_dir = tools / "task_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"task_{int(time.time())}.log"

        executor = _safe_str(data.get("executor") or "agy").lower()
        model_id = _safe_str(data.get("model"))
        if executor == "swarm":
            effective_mode = "auto"
            effective_prompt = (
                f"Используй навык ag-swarm (/swarm) для этой задачи, распредели независимые файлы по параллельным воркерам и доведи до сборки/тестов:\n\n{prompt_txt}"
            )
        else:
            effective_mode = mode
            effective_prompt = prompt_txt

        cmd = [agy_bin, "--mode", effective_mode, "--log-file", str(log_file)]
        if model_id:
            cmd.extend(["--model", model_id])
        if act == "create_project_and_run":
            cmd.append("--new-project")
        cmd.extend(["-p", effective_prompt])

        creationflags = 0x08000000 if os.name == "nt" else 0
        task_env = dict(os.environ)
        task_env["AGY_PROXY_STRICT_SSL"] = "false"
        task_env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        task_env["GODEBUG"] = "http2client=0"
        proxy_cfg = _read_json(tools / "proxy_config.json", {})
        if isinstance(proxy_cfg, dict) and proxy_cfg.get("enabled", True):
            pu = _safe_str(proxy_cfg.get("proxy_url") or proxy_cfg.get("url"))
            if pu:
                if "://" not in pu:
                    pu = f"http://{pu}"
                task_env["HTTPS_PROXY"] = pu
                task_env["HTTP_PROXY"] = pu
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=os.path.abspath(raw_path),
                env=task_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=(os.name != "nt"),
            )
            tasks_file = tools / "studio_tasks.json"
            tasks_map = _read_json(tasks_file, {})
            if not isinstance(tasks_map, dict):
                tasks_map = {}
            tasks_map[norm_new] = {
                "pid": int(proc.pid or 0),
                "path": os.path.abspath(raw_path),
                "log_file": str(log_file),
                "started_at": time.time(),
                "executor": executor,
                "model": model_id,
                "prompt": prompt_txt[:120],
            }
            _write_json_atomic(tasks_file, tasks_map)
            return {
                "ok": True,
                "path": os.path.abspath(raw_path),
                "spawned": True,
                "pid": int(proc.pid or 0),
                "log_file": str(log_file),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act == "refresh_all_accounts":
        acs = _import_account_switcher()
        if acs is None:
            return {"ok": False, "error": "Модуль account_switcher не найден"}
        tools = _tools_dir()
        acc_dir = tools / "accounts"
        if not acc_dir.is_dir():
            return {"ok": True, "refreshed": 0}
        ids = [fp.stem for fp in sorted(acc_dir.glob("*.json")) if fp.is_file()]
        from concurrent.futures import ThreadPoolExecutor
        refreshed = 0
        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = [pool.submit(acs.fetch_live_quota, aid, True) for aid in ids]
            for f in futs:
                try:
                    if f.result(timeout=12) is not None:
                        refreshed += 1
                except Exception:
                    pass
        return {"ok": True, "refreshed": refreshed, "total": len(ids)}

    if act == "read_task_log":
        log_path_str = _safe_str(data.get("log_file"))
        proj_path = _safe_str(data.get("path"))
        if not log_path_str and proj_path:
            tasks_map = _read_json(_tools_dir() / "studio_tasks.json", {})
            if isinstance(tasks_map, dict):
                tinfo = tasks_map.get(os.path.normcase(os.path.abspath(proj_path)))
                if isinstance(tinfo, dict):
                    log_path_str = _safe_str(tinfo.get("log_file"))
        if not log_path_str or not os.path.isfile(log_path_str):
            return {"ok": False, "error": "Лог-файл задачи пока не создан"}
        try:
            lp = Path(log_path_str)
            st = lp.stat()
            with lp.open("rb") as f:
                f.seek(max(0, st.st_size - 16384))
                lines = f.read().decode("utf-8", errors="replace").splitlines()[-80:]
            return {"ok": True, "log_file": log_path_str, "lines": lines}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act == "stop_project_task":
        pid = int(data.get("pid") or 0)
        proj_path = _safe_str(data.get("path"))
        tasks_file = _tools_dir() / "studio_tasks.json"
        tasks_map = _read_json(tasks_file, {})
        if not isinstance(tasks_map, dict):
            tasks_map = {}
        p_norm = os.path.normcase(os.path.abspath(proj_path)) if proj_path else ""
        if pid <= 0 and p_norm and isinstance(tasks_map.get(p_norm), dict):
            pid = int(tasks_map[p_norm].get("pid") or 0)
        if pid <= 0:
            return {"ok": False, "error": "Активный процесс задачи не найден"}
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                    timeout=5,
                )
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            if p_norm and isinstance(tasks_map.get(p_norm), dict):
                tasks_map[p_norm]["pid"] = 0
                _write_json_atomic(tasks_file, tasks_map)
            return {"ok": True, "stopped_pid": pid}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    if act == "toggle_autostart":
        from patcher.utils.admin import is_admin

        want_enable = bool(data.get("enable", not _is_autostart_enabled()))
        studio_exe = _safe_str(data.get("studio_exe"))
        if os.environ.get("PYTEST_CURRENT_TEST"):
            _write_json_atomic(_tools_dir() / "autostart_state.json", {"enabled": want_enable, "studio_exe": studio_exe})
            return {"ok": True, "autostart_enabled": want_enable}

        if os.name == "nt":
            if not is_admin():
                return {"ok": False, "requires_admin": True, "error": "Для настройки автозапуска без UAC нужны права администратора"}
            if want_enable:
                if not studio_exe or not os.path.isfile(studio_exe):
                    cand = Path(__file__).resolve().parent.parent.parent / "release_out" / "Antigravity_Studio_Windows_x64.exe"
                    if cand.is_file():
                        studio_exe = str(cand)
                    else:
                        return {"ok": False, "error": "Не найден исполняемый файл Antigravity Studio"}
                exe_esc = studio_exe.replace("'", "''")
                ps_script = (
                    f"$act = New-ScheduledTaskAction -Execute '{exe_esc}' -Argument '--autostart'; "
                    "$trig = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
                    "$prin = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest; "
                    "$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero); "
                    f"Register-ScheduledTask -TaskName '{_AUTOSTART_TASK_NAME}' -Action $act -Trigger $trig -Principal $prin -Settings $set -Force | Out-Null"
                )
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    capture_output=True,
                    text=True,
                    creationflags=0x08000000,
                    timeout=10,
                )
                if r.returncode != 0:
                    return {"ok": False, "error": _safe_str(r.stderr) or "Не удалось создать задачу планировщика"}
                return {"ok": True, "autostart_enabled": True}
            else:
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", _AUTOSTART_TASK_NAME, "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                    timeout=6,
                )
                return {"ok": True, "autostart_enabled": False}
        elif sys.platform == "darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / "com.antigravity.studio.plist"
            if want_enable and studio_exe:
                plist.parent.mkdir(parents=True, exist_ok=True)
                plist.write_text(
                    f'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key><string>com.antigravity.studio</string>'
                    f'<key>ProgramArguments</key><array><string>{studio_exe}</string><string>--autostart</string></array>'
                    f'<key>RunAtLoad</key><true/></dict></plist>\n',
                    encoding="utf-8",
                )
                return {"ok": True, "autostart_enabled": True}
            plist.unlink(missing_ok=True)
            return {"ok": True, "autostart_enabled": False}
        else:
            desk = Path.home() / ".config" / "autostart" / "antigravity-studio.desktop"
            if want_enable and studio_exe:
                desk.parent.mkdir(parents=True, exist_ok=True)
                desk.write_text(
                    f"[Desktop Entry]\nType=Application\nName=Antigravity Studio\nExec=\"{studio_exe}\" --autostart\nX-GNOME-Autostart-enabled=true\n",
                    encoding="utf-8",
                )
                return {"ok": True, "autostart_enabled": True}
            desk.unlink(missing_ok=True)
            return {"ok": True, "autostart_enabled": False}

    if act in ("rotate_429_token", "hot_pin_account"):
        return _select_and_refresh_hot_token(data, is_manual_pin=(act == "hot_pin_account"))

    return {"ok": False, "error": f"Unknown studio action: {action}"}


def _select_and_refresh_hot_token(data: dict[str, Any], is_manual_pin: bool = False) -> dict[str, Any]:
    tools = _tools_dir()
    acc_dir = tools / "accounts"
    if not acc_dir.is_dir():
        return {"ok": False, "error": "Папка аккаунтов пуста"}

    failed_tok = _safe_str(data.get("failed_token"))
    target_id = _safe_str(data.get("account_id"))
    raw_excl = data.get("exclude_ids")
    excl_ids: set[str] = set(str(x) for x in raw_excl) if isinstance(raw_excl, list) else set()
    family_hint = _safe_str(data.get("family") or "gemini").lower()

    raw_accounts: list[tuple[Path, dict[str, Any]]] = []
    failed_acc_id = ""
    for fp in sorted(acc_dir.glob("*.json")):
        d = _read_json(fp)
        if not isinstance(d, dict) or not d.get("id"):
            continue
        aid = _safe_str(d["id"])
        tok_obj = d.get("token") if isinstance(d.get("token"), dict) else {}
        if failed_tok and _safe_str(tok_obj.get("access_token")) == failed_tok:
            failed_acc_id = aid
            excl_ids.add(aid)
        raw_accounts.append((fp, d))

    chosen_pair: tuple[Path, dict[str, Any]] | None = None
    if is_manual_pin and target_id:
        chosen_pair = next(((fp, d) for fp, d in raw_accounts if _safe_str(d.get("id")) == target_id), None)
    else:
        scored: list[tuple[tuple[int, int, str], Path, dict[str, Any]]] = []
        for fp, d in raw_accounts:
            aid = _safe_str(d.get("id"))
            if aid in excl_ids or d.get("disabled") or d.get("validation_blocked"):
                continue
            q = d.get("quota") if isinstance(d.get("quota"), dict) else {}
            if q.get("is_forbidden"):
                continue
            summary = _extract_account_summary(d)
            tier_up = summary["tier"].upper()
            tier_rank = 2 if "ULTRA" in tier_up else (1 if "PRO" in tier_up else 0)
            fam_pct = 50
            for b in summary.get("buckets", []):
                if ("claude" in family_hint and b["family"] == "claude") or ("claude" not in family_hint and b["family"] == "gemini"):
                    p5 = int(b.get("pct_5h") or 0)
                    pw = int(b["pct_weekly"]) if b.get("pct_weekly") is not None else p5
                    fam_pct = min(p5, pw) if pw <= 15 else p5
                    break
            if fam_pct <= 0:
                continue
            scored.append(((fam_pct, tier_rank, summary["email"]), fp, d))
        if scored:
            scored.sort(key=lambda x: (x[0][0] > 15, x[0][1], x[0][0]), reverse=True)
            chosen_pair = (scored[0][1], scored[0][2])

    if chosen_pair is None:
        return {"ok": False, "error": "Нет доступных запасных аккаунтов со свободной квотой"}

    fp, detail = chosen_pair
    aid = _safe_str(detail.get("id"))
    tok_obj = detail.setdefault("token", {})
    acc_tok = _safe_str(tok_obj.get("access_token"))
    ref_tok = _safe_str(tok_obj.get("refresh_token"))
    exp_ts = float(tok_obj.get("expiry_timestamp") or 0.0)
    now = time.time()

    if ref_tok and (not acc_tok or exp_ts < now + 300.0) and not os.environ.get("PYTEST_CURRENT_TEST"):
        acs = _import_account_switcher()
        if acs is not None:
            try:
                ref_res = acs.refresh_google_token(ref_tok)
                if ref_res:
                    acc_tok, exp_in, id_tok = ref_res
                    tok_obj["access_token"] = acc_tok
                    tok_obj["expires_in"] = exp_in
                    tok_obj["expiry_timestamp"] = int(now) + int(exp_in)
                    if id_tok:
                        tok_obj["id_token"] = id_tok
                    _write_json_atomic(fp, detail)
            except Exception:
                pass

    if not acc_tok:
        return {"ok": False, "error": "У выбранного аккаунта отсутствует access_token"}

    # Update active slot in accounts.json so HUD & Studio reflect the hot-rotated account immediately
    idx_path = tools / "accounts.json"
    idx = _read_json(idx_path, {})
    if isinstance(idx, dict):
        slot = _safe_str(data.get("app")).lower()
        if slot == "ide" or (not slot and failed_acc_id and idx.get("current_ide_account_id") == failed_acc_id):
            idx["current_ide_account_id"] = aid
        if slot == "standalone" or (not slot and failed_acc_id and idx.get("current_standalone_account_id") == failed_acc_id):
            idx["current_standalone_account_id"] = aid
        if slot == "cli" or (not slot and failed_acc_id and idx.get("current_cli_account_id") == failed_acc_id):
            idx["current_cli_account_id"] = aid
        _write_json_atomic(idx_path, idx)

    return {
        "ok": True,
        "account_id": aid,
        "email": _safe_str(detail.get("email")),
        "tier": _safe_str(detail.get("tier") or (detail.get("quota") or {}).get("subscription_tier") or "PRO"),
        "access_token": acc_tok,
    }
