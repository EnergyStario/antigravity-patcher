"""Tests for patcher.studio_api headless bridge."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "source"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from patcher import studio_api


def test_extract_account_summary_strips_tokens_and_extracts_weekly(tmp_path: Path):
    raw_acc = {
        "id": "acc-1",
        "email": "demo@example.com",
        "name": "Demo",
        "tier": "Pro",
        "token": {"access_token": "SECRET_ACCESS", "refresh_token": "SECRET_REFRESH"},
        "quota": {
            "claude_percentage": 100,
            "gemini_percentage": 64,
            "argon_percentage": 88,
            "quota_groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.64},
                        {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.82},
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 1.0},
                        {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 0.45},
                    ],
                },
                {
                    "displayName": "Gemini Argon",
                    "buckets": [
                        {"bucketId": "argon-5h", "window": "5h", "remainingFraction": 0.88},
                        {"bucketId": "argon-weekly", "window": "weekly", "remainingFraction": 0.91},
                    ],
                },
            ],
        },
    }
    summary = studio_api._extract_account_summary(raw_acc)
    dumped = json.dumps(summary)
    assert "SECRET_ACCESS" not in dumped
    assert "SECRET_REFRESH" not in dumped
    assert "token" not in summary

    by_id = {b["id"]: b for b in summary["buckets"]}
    assert by_id["claude"]["pct_5h"] == 100
    assert by_id["claude"]["pct_weekly"] == 45
    assert by_id["gemini"]["pct_5h"] == 64
    assert by_id["gemini"]["pct_weekly"] == 82
    assert by_id["argon"]["pct_5h"] == 88
    assert by_id["argon"]["pct_weekly"] == 91


def test_collect_accounts_preserves_independent_ide_and_standalone_slots(tmp_path: Path, monkeypatch):
    tools = tmp_path / ".antigravity_tools"
    (tools / "accounts").mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    (tools / "accounts" / "ide1.json").write_text(
        json.dumps({"id": "ide-id", "email": "ide@test.dev", "quota": {"claude_percentage": 90}}),
        encoding="utf-8",
    )
    (tools / "accounts" / "ag2.json").write_text(
        json.dumps({"id": "ag2-id", "email": "ag2@test.dev", "quota": {"gemini_percentage": 55}}),
        encoding="utf-8",
    )
    (tools / "accounts.json").write_text(
        json.dumps({
            "current_ide_account_id": "ide-id",
            "current_standalone_account_id": "ag2-id",
        }),
        encoding="utf-8",
    )

    acc_state = studio_api._collect_accounts()
    assert acc_state["active_ide_id"] == "ide-id"
    assert acc_state["active_standalone_id"] == "ag2-id"
    assert len(acc_state["items"]) == 2


def test_switch_account_keeps_slots_independent(tmp_path: Path, monkeypatch):
    tools = tmp_path / ".antigravity_tools"
    tools.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    (tools / "accounts.json").write_text(
        json.dumps({
            "current_ide_account_id": "ide-orig",
            "current_standalone_account_id": "ag2-orig",
        }),
        encoding="utf-8",
    )
    dummy_runner = tools / "switch_runner.py"
    dummy_runner.write_text("# dummy\n", encoding="utf-8")

    with mock.patch("patcher.studio_api.subprocess.Popen") as popen_mock:
        # Force fallback to tools/switch_runner.py cleanly via non-existent repo path or mock Path.is_file
        res = studio_api.run_studio_action("switch_account", {"account_id": "new-ag2", "app": "standalone"})

    assert res["ok"] is True
    assert popen_mock.called
    saved = json.loads((tools / "accounts.json").read_text(encoding="utf-8"))
    assert saved["current_ide_account_id"] == "ide-orig"
    assert saved["current_standalone_account_id"] == "new-ag2"

    with mock.patch("patcher.studio_api.subprocess.Popen") as popen_cli:
        res_cli = studio_api.run_studio_action("switch_account", {"account_id": "cli-spare", "app": "cli"})
    assert res_cli["ok"] is True
    assert popen_cli.called
    saved2 = json.loads((tools / "accounts.json").read_text(encoding="utf-8"))
    assert saved2["current_ide_account_id"] == "ide-orig"
    assert saved2["current_standalone_account_id"] == "new-ag2"
    assert saved2["current_cli_account_id"] == "cli-spare"


def test_set_auto_rotate_persists_gui_config(tmp_path: Path, monkeypatch):
    tools = tmp_path / ".antigravity_tools"
    tools.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    res = studio_api.run_studio_action("set_auto_rotate", {"enabled": True})
    assert res == {"ok": True, "auto_rotate_429": True}
    cfg = json.loads((tools / "gui_config.json").read_text(encoding="utf-8"))
    assert cfg["auto_switch_classic_depleted"] is True
    assert cfg["classic_auto_switch"]["enabled"] is True


def test_get_studio_snapshot_with_mocked_targets(tmp_path: Path, monkeypatch):
    tools = tmp_path / ".antigravity_tools"
    tools.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    fake_targets = {
        "ide": {"installed": True, "path": "/a", "version": "2.7.0", "patched": True, "needs_patch": False, "locked": True},
        "manager": {"installed": True, "path": "/b", "version": "2.32.1", "patched": True, "needs_patch": False, "locked": False},
        "cli": {"installed": False, "path": "", "version": "", "patched": False, "needs_patch": False, "locked": False},
        "vscode": {"installed": False, "path": "", "version": "", "patched": False, "needs_patch": False, "locked": False},
        "needs_any": False,
    }
    fake_net = {
        "platform": "win32",
        "os_label": "Windows",
        "dns_method": "Windows NRPT",
        "nrpt_supported": True,
        "nrpt_active": True,
        "nrpt_dns_ip": "77.88.8.8",
        "hosts_pinned": False,
        "hosts_ip": "",
        "split_tunnel_available": False,
    }
    with mock.patch("patcher.studio_api._inspect_targets_safe", return_value=fake_targets), \
         mock.patch("patcher.studio_api._collect_network_status", return_value=fake_net):
        snap = studio_api.get_studio_snapshot()

    assert snap["ok"] is True
    assert snap["targets"]["ide"]["version"] == "2.7.0"
    assert snap["network"]["nrpt_active"] is True
    assert isinstance(snap["accounts"]["items"], list)
    assert isinstance(snap["projects"], list)


def test_collect_projects_ignores_stale_incomplete_swarm_and_resolves_git_root(tmp_path: Path, monkeypatch):
    import os
    import time

    tools = tmp_path / ".antigravity_tools"
    swarm_dir = tools / "swarm"
    stale_dir = swarm_dir / "stale-task"
    live_dir = swarm_dir / "live-task"
    stale_dir.mkdir(parents=True)
    live_dir.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    # Real repo with .git at parent and nested subdir "repo"
    real_repo = tmp_path / "MyRealRepo"
    nested_sub = real_repo / "repo"
    (real_repo / ".git").mkdir(parents=True)
    nested_sub.mkdir(parents=True)

    now = time.time()
    stale_file = stale_dir / "state.json"
    stale_file.write_text(
        json.dumps({
            "task_id": "stale-task",
            "project_path": str(tmp_path / "3"),
            "is_complete": False,
            "created_at": now - 70 * 3600,
            "briefs": {"a": "x"},
            "user_request": "напиши текстовый фалй",
        }),
        encoding="utf-8",
    )
    os.utime(stale_file, (now - 68 * 3600, now - 68 * 3600))

    live_file = live_dir / "state.json"
    live_file.write_text(
        json.dumps({
            "task_id": "live-task",
            "project_path": str(nested_sub),
            "is_complete": False,
            "created_at": now - 125,
            "briefs": {"w1": "do 1", "w2": "do 2"},
            "worker_results": {},
        }),
        encoding="utf-8",
    )

    projects = studio_api._collect_projects()
    by_id = {p["id"]: p for p in projects}
    # Fresh project exists -> stale >24h archived task is filtered out and NEVER reported as running
    assert "stale-task" not in by_id
    assert "live-task" in by_id
    assert by_id["live-task"]["name"] == "MyRealRepo"
    assert by_id["live-task"]["status"] == "running"
    assert by_id["live-task"]["active_agents"] == 2
    assert 100 <= by_id["live-task"]["elapsed_sec"] <= 300


def test_collect_projects_marks_moderately_old_incomplete_swarm_paused_without_fake_timer(tmp_path: Path, monkeypatch):
    import os
    import time

    tools = tmp_path / ".antigravity_tools"
    stale_dir = tools / "swarm" / "old-2h"
    stale_dir.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    now = time.time()
    sf = stale_dir / "state.json"
    sf.write_text(
        json.dumps({
            "task_id": "old-2h",
            "project_path": str(tmp_path / "3"),
            "is_complete": False,
            "created_at": now - 2 * 3600,
            "briefs": {"w1": "x"},
        }),
        encoding="utf-8",
    )
    os.utime(sf, (now - 2 * 3600, now - 2 * 3600))

    projects = studio_api._collect_projects()
    assert len(projects) == 1
    assert projects[0]["status"] == "paused"
    assert projects[0]["active_agents"] == 0
    assert projects[0]["elapsed_sec"] == 0


def test_collect_projects_detects_multiple_live_conversations_and_aggregates_same_project(tmp_path: Path, monkeypatch):
    import sqlite3
    from datetime import datetime, timezone

    tools = tmp_path / ".antigravity_tools"
    tools.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    gemini_dir = tmp_path / ".gemini" / "antigravity"
    gemini_dir.mkdir(parents=True)
    monkeypatch.setenv("AG_GEMINI_DIR", str(gemini_dir))

    proj_a = tmp_path / "ProjAlpha"
    proj_b = tmp_path / "ProjBeta"
    (proj_a / ".git").mkdir(parents=True)
    (proj_b / ".git").mkdir(parents=True)

    db_path = gemini_dir / "conversation_summaries.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE conversation_summaries (
            conversation_id text PRIMARY KEY,
            title text,
            workspace_uris text,
            status text,
            not_fully_idle integer,
            killed integer,
            last_modified_time text,
            last_user_input_time text,
            parent_conversation_id text,
            agent_name text
        )
        """
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    uri_a = proj_a.as_uri()
    uri_b = proj_b.as_uri()
    rows = [
        ("conv-a1", "Task A1", json.dumps([uri_a]), "CASCADE_RUN_STATUS_RUNNING", 1, 0, now_iso, now_iso, "", ""),
        ("conv-a2", "Task A2", json.dumps([uri_a]), "CASCADE_RUN_STATUS_IDLE", 1, 0, now_iso, now_iso, "", ""),
        ("conv-b1", "Task B1", json.dumps([uri_b]), "CASCADE_RUN_STATUS_RUNNING", 1, 0, now_iso, now_iso, "", ""),
        ("conv-dead", "Dead", json.dumps([uri_b]), "CASCADE_RUN_STATUS_IDLE", 0, 1, now_iso, now_iso, "", ""),
    ]
    con.executemany("INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    projects = studio_api._collect_projects()
    by_name = {p["name"]: p for p in projects}
    assert "ProjAlpha" in by_name
    assert "ProjBeta" in by_name
    assert by_name["ProjAlpha"]["status"] == "running"
    assert by_name["ProjAlpha"]["active_agents"] == 2
    assert by_name["ProjBeta"]["status"] == "running"
    assert by_name["ProjBeta"]["active_agents"] == 1


def test_studio_actions_set_proxy_custom_path_and_snapshot_fields(tmp_path: Path, monkeypatch):
    tools = tmp_path / ".antigravity_tools"
    tools.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    res_proxy = studio_api.run_studio_action("set_proxy", {"proxy_url": "http://127.0.0.1:10808", "enabled": True})
    assert res_proxy["ok"] is True
    assert res_proxy["proxy_enabled"] is True

    res_path = studio_api.run_studio_action("set_custom_path", {"target": "ide", "path": "C:/Custom/IDE"})
    assert res_path["ok"] is True
    saved_paths = json.loads((tools / "custom_paths.json").read_text(encoding="utf-8"))
    assert saved_paths["ide"] == "C:/Custom/IDE"

    with mock.patch("patcher.studio_api._inspect_targets_safe", return_value={"needs_any": False}), \
         mock.patch("patcher.hosts_pin.is_hosts_pinned", return_value=(False, "")), \
         mock.patch("patcher.nrpt_dns.get_nrpt_status", return_value={"supported": True, "active": False}):
        snap = studio_api.get_studio_snapshot(include_accounts=False, include_projects=False)
    assert "is_admin" in snap
    assert isinstance(snap["logs"], list)
    assert snap["network"]["proxy_url"] == "http://127.0.0.1:10808"
    assert snap["network"]["proxy_enabled"] is True


def test_create_project_registers_folder_and_reads_live_activity(tmp_path: Path, monkeypatch):
    tools = tmp_path / ".antigravity_tools"
    tools.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    new_proj = tmp_path / "MyNewProj"
    res = studio_api.run_studio_action("create_project_and_run", {
        "path": str(new_proj),
        "prompt": "",
        "git_init": False,
    })
    assert res["ok"] is True
    assert res["created"] is True
    assert new_proj.is_dir()
    saved_list = json.loads((tools / "swarm_projects.json").read_text(encoding="utf-8"))
    assert str(new_proj) in saved_list

    gdir = tmp_path / ".gemini" / "antigravity"
    tpath = gdir / "brain" / "cid-1" / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True)
    tpath.write_text(
        json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "edit_file", "args": {"Path": "C:/foo/bar.py"}}]}) + "\n",
        encoding="utf-8",
    )
    assert studio_api._read_live_activity(gdir, "cid-1") == "Правка: bar.py"


def test_ultra_carbon_argon_bucket_and_frozen_switch_and_task_log(tmp_path: Path, monkeypatch):
    tools = tmp_path / ".antigravity_tools"
    tools.mkdir(parents=True)
    monkeypatch.setenv("AG_TOOLS_DIR", str(tools))

    ultra_acc = {
        "id": "ultra-1",
        "email": "ultra@example.com",
        "tier": "ULTRA",
        "quota": {
            "quota_groups": [
                {
                    "displayName": "Gemini Models",
                    "description": "Models within this group: Gemini 4 Carbon, Gemini 4 Argon, Gemini 4 Hydrogen, Gemini 3.8 Flash",
                    "buckets": [
                        {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.93, "resetTime": "2099-01-05T00:00:00Z"},
                        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.80, "resetTime": "2099-01-01T02:00:00Z"},
                    ],
                },
            ],
        },
    }
    s = studio_api._extract_account_summary(ultra_acc)
    by_id = {b["id"]: b for b in s["buckets"]}
    assert by_id["gemini"]["label"] == "Hydrogen · 3.8"
    assert by_id["gemini"]["pct_5h"] == 80
    assert by_id["gemini"]["pct_weekly"] == 93
    assert by_id["argon"]["label"] == "Gemini Argon"
    assert by_id["argon"]["pct_5h"] == 80
    assert by_id["carbon"]["label"] == "Gemini Carbon"
    assert by_id["carbon"]["pct_5h"] == 80
    assert by_id["gemini"]["reset_5h_label"] != ""

    # Verify rotate_429_token picks the healthiest non-excluded account (ULTRA preferred)
    acc_dir = tools / "accounts"
    acc_dir.mkdir(parents=True, exist_ok=True)
    ultra_with_tok = dict(ultra_acc, token={"access_token": "TOK_ULTRA_OK", "refresh_token": "REF_U"})
    depleted_acc = {
        "id": "dep-1",
        "email": "dep@example.com",
        "tier": "PRO",
        "token": {"access_token": "TOK_DEPLETED"},
        "quota": {"claude_percentage": 0, "gemini_percentage": 0},
    }
    (acc_dir / "ultra-1.json").write_text(json.dumps(ultra_with_tok), encoding="utf-8")
    (acc_dir / "dep-1.json").write_text(json.dumps(depleted_acc), encoding="utf-8")
    (tools / "accounts.json").write_text(json.dumps({"current_ide_account_id": "dep-1"}), encoding="utf-8")

    rot = studio_api.run_studio_action("rotate_429_token", {"failed_token": "TOK_DEPLETED", "family": "gemini"})
    assert rot["ok"] is True
    assert rot["account_id"] == "ultra-1"
    assert rot["access_token"] == "TOK_ULTRA_OK"

    # Verify toggle_autostart state persistence in test mode
    auto_on = studio_api.run_studio_action("toggle_autostart", {"enable": True, "studio_exe": "C:/Fake/Studio.exe"})
    assert auto_on == {"ok": True, "autostart_enabled": True}
    assert studio_api._is_autostart_enabled() is True

    # Verify frozen binary uses --switch-account instead of passing switch_runner.py path
    (tools / "accounts.json").write_text(json.dumps({"current_ide_account_id": "old"}), encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Fake/Antigravity_Patcher_Windows_x64.exe")
    with mock.patch("patcher.studio_api.subprocess.Popen") as popen_frozen:
        res_sw = studio_api.run_studio_action("switch_account", {"account_id": "ultra-1", "app": "cli"})
    assert res_sw["ok"] is True
    spawned_cmd = popen_frozen.call_args[0][0]
    assert spawned_cmd == ["C:/Fake/Antigravity_Patcher_Windows_x64.exe", "--switch-account", "ultra-1", "cli", "--no-restart"]

    # Verify read_task_log reads tail lines cleanly
    lf = tools / "sample_task.log"
    lf.write_text("line-1\nline-2\ndone\n", encoding="utf-8")
    res_log = studio_api.run_studio_action("read_task_log", {"log_file": str(lf)})
    assert res_log["ok"] is True
    assert res_log["lines"] == ["line-1", "line-2", "done"]



