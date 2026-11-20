"""Tests for zero-prompt Autofix, CMD QuickEdit copyability, post-fix launch, and startup Autopilot."""
from __future__ import annotations

import builtins
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "source"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def test_setup_console_enables_quick_edit_for_copying(monkeypatch):
    from patcher.utils import console

    if os.name != "nt":
        pytest.skip("Windows console mode check only applies on Windows")

    captured_modes: list[int] = []

    class _FakeKernel32:
        def GetStdHandle(self, _std_id):
            return 123

        def GetConsoleMode(self, _h, mode_ptr):
            mode_ptr._obj.value = 0x0000
            return 1

        def SetConsoleMode(self, _h, new_mode):
            captured_modes.append(int(new_mode))
            return 1

    fake_windll = mock.MagicMock()
    fake_windll.kernel32 = _FakeKernel32()
    with mock.patch("patcher.utils.console.ctypes.windll", fake_windll):
        console.setup_console()

    assert captured_modes, "SetConsoleMode should be called"
    # ENABLE_QUICK_EDIT_MODE (0x0040) MUST be enabled so mouse copy in CMD works
    assert (captured_modes[0] & 0x0040) == 0x0040


def test_data_reset_auto_confirm_zero_prompts_cleans_all(tmp_path: Path, monkeypatch):
    from patcher import data_reset

    ide_dir = tmp_path / "Antigravity IDE"
    ag2_dir = tmp_path / "Antigravity"
    for d in (ide_dir, ag2_dir):
        (d / "CachedData").mkdir(parents=True)
        (d / "CachedData" / "stale.bin").write_bytes(b"123")
        (d / "User").mkdir(parents=True)
        (d / "User" / "settings.json").write_text('{"update.mode":"none"}', encoding="utf-8")

    # Ensure input() would fail the test if ever invoked in auto_confirm=True mode
    monkeypatch.setattr(builtins, "input", lambda *_a, **_kw: pytest.fail("input() must not be called in auto_confirm mode"))

    with mock.patch.object(data_reset, "_get_data_dirs", return_value=[("Antigravity IDE", ide_dir), ("Antigravity 2.0", ag2_dir)]), \
         mock.patch.object(data_reset, "_is_ide_running", return_value=None), \
         mock.patch.object(data_reset, "clean_compat_and_ifeo_registry", return_value={"compat_removed": [], "ifeo_removed": []}):
        res = data_reset.do_data_reset("all", auto_confirm=True)

    assert res is True
    assert not (ide_dir / "CachedData").exists()
    assert not (ag2_dir / "CachedData").exists()
    assert (ide_dir / "User" / "settings.json").is_file()
    assert (ag2_dir / "User" / "settings.json").is_file()


def test_do_diagnostics_auto_launches_fixed_real_bundle_even_if_idle(tmp_path: Path):
    from patcher import diagnostics
    from patcher.utils import launcher

    # Simulate an actual IDE install folder containing Antigravity IDE.exe + main.js
    ide_root = tmp_path / "Antigravity IDE"
    (ide_root / "resources" / "app" / "out").mkdir(parents=True)
    exe_file = ide_root / "Antigravity IDE.exe"
    exe_file.write_bytes(b"MZ")
    main_js = ide_root / "resources" / "app" / "out" / "main.js"
    main_js.write_text("resetIsTierGCPTos(),this.foo.isGoogleInternal", encoding="utf-8")

    with mock.patch.object(diagnostics, "collect_log_lines", return_value=[("s", "ls.log", "TLS handshake error")]), \
         mock.patch("patcher.tls_fix.is_tls_fix_applied", return_value=False), \
         mock.patch("patcher.tls_fix.do_tls_fix_inject", return_value=True), \
         mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": False, "manager": False}), \
         mock.patch.object(diagnostics, "save_report", return_value=str(tmp_path / "rep.txt")), \
         mock.patch.object(launcher, "launch_application", return_value=True) as launch_mock:
        # Patch _is_tls_applied check post-fix by letting is_tls_fix_applied return False -> do_tls runs -> returns True
        with mock.patch("patcher.tls_fix.is_tls_fix_applied", side_effect=[False, True, True]):
            diagnostics.do_diagnostics(str(main_js), "", "")

    assert launch_mock.called
    assert any(call.args[0] == "ide" for call in launch_mock.call_args_list)


def test_nrpt_dns_ignores_stale_legacy_ip_and_fails_over():
    from patcher import nrpt_dns

    stale_cfg = {"enabled": False, "mode": "public", "server_ip": "176.108.113.214"}
    assert nrpt_dns.get_preferred_dns_ip(stale_cfg) == "176.108.113.214"

    probed: list[str] = []

    def _fake_probe(ip: str, timeout: float = 2.0):
        probed.append(ip)
        if ip == "37.230.192.51":
            return True, "Reachable"
        return False, "timed out after 2.0s"

    with mock.patch.object(nrpt_dns, "is_nrpt_supported", return_value=True), \
         mock.patch.object(nrpt_dns, "get_dns_config", return_value=dict(stale_cfg)), \
         mock.patch.object(nrpt_dns, "check_dns_resolver_reachable", side_effect=_fake_probe), \
         mock.patch.object(nrpt_dns, "_remove_existing_nrpt_rules", return_value=(0, "")), \
         mock.patch.object(nrpt_dns, "_run_ps", return_value=(0, "")), \
         mock.patch.object(nrpt_dns, "_flush_dns_cache"), \
         mock.patch.object(nrpt_dns, "save_dns_config"):
        ok_flag, msg = nrpt_dns.enable_nrpt_rule("176.108.113.214", check_live=True)

    assert ok_flag is True
    assert "37.230.192.51" in msg
    assert "176.108.113.214" in probed
    assert "37.230.192.51" in probed


def test_collect_log_lines_drops_archived_cli_logs_older_than_7d(tmp_path: Path):
    import time
    from patcher import diagnostics

    log_dir = tmp_path / "cli_logs"
    log_dir.mkdir(parents=True)
    old_cli = log_dir / "cli-20261019_123443.log"
    old_cli.write_text("W1019 Account ineligible: Your current account is not eligible\n", encoding="utf-8")
    old_ts = time.time() - (20 * 86400)
    os.utime(old_cli, (old_ts, old_ts))

    with mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[log_dir]):
        lines = diagnostics.collect_log_lines(max_lines_per_file=500)

    assert lines == []


def test_diagnostics_does_not_claim_fixed_on_live_tls_timeout_and_falls_back_to_hosts(tmp_path: Path):
    from patcher import diagnostics

    ide_root = tmp_path / "Antigravity IDE"
    (ide_root / "resources" / "app" / "out").mkdir(parents=True)
    main_js = ide_root / "resources" / "app" / "out" / "main.js"
    main_js.write_text("NODE_TLS_REJECT_UNAUTHORIZED=0\n_origWarn\n", encoding="utf-8")

    # Simulate fresh loadCodeAssist TLS handshake timeout
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    cli_log = log_dir / "cli-20261115_223855.log"
    cli_log.write_text(
        'E1115 22:40:05.453866 42 errorreport.go:307] failed to get load code assist response: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": net/http: TLS handshake timeout\n',
        encoding="utf-8",
    )

    with mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[log_dir]):
        collected = diagnostics.collect_log_lines(max_lines_per_file=500)

    assert len(collected) == 1
    found = diagnostics.analyze_logs(collected, diagnostics._make_error_db())
    keys = {ke.key for ke in found}
    assert "cache_refresh_fail" in keys

    ke_cache = next(ke for ke in found if ke.key == "cache_refresh_fail")
    ctx = {"main_js_path": str(main_js), "manager_path": "", "agy_path": "", "vscode_path": "", "vscode_agy_path": ""}

    # Before fix: static TLS string exists, BUT live connectivity fails with TLS timeout -> must NOT claim already fixed
    with mock.patch.object(diagnostics, "_check_cloudcode_connectivity", return_value={"reachable": False, "tls_timeout": True, "regional_400": False, "detail": "TLS handshake timeout"}):
        assert ke_cache.is_already_fixed_fn(ctx) is False

    # During heal: NRPT fails -> falls back to VPN-proof Hosts-Pinning -> live check succeeds!
    probe_seq = iter([
        {"reachable": False, "tls_timeout": True, "regional_400": False, "detail": "TLS handshake timeout"},
        {"reachable": True, "tls_timeout": False, "regional_400": False, "detail": "HTTP 401"},
    ])
    with mock.patch("patcher.diagnostics.sys.platform", "win32"), \
         mock.patch.object(diagnostics, "_read_windows_registry_proxy", return_value=(False, "")), \
         mock.patch.object(diagnostics, "_clear_dead_user_env_proxies", return_value=[]), \
         mock.patch.object(diagnostics, "_get_dead_env_proxies", return_value=[]), \
         mock.patch.object(diagnostics, "_check_cloudcode_connectivity", side_effect=lambda timeout=2.5: next(probe_seq)), \
         mock.patch("patcher.nrpt_dns.is_nrpt_supported", return_value=True), \
         mock.patch("patcher.nrpt_dns.get_nrpt_status", return_value={"active": False, "server": ""}), \
         mock.patch("patcher.nrpt_dns.get_preferred_dns_ip", return_value="37.230.192.51"), \
         mock.patch("patcher.nrpt_dns.enable_nrpt_rule", return_value=(False, "NRPT blocked")), \
         mock.patch("patcher.nrpt_dns._flush_dns_cache"), \
         mock.patch("patcher.hosts_pin.is_hosts_pinned", return_value=(False, "")), \
         mock.patch("patcher.hosts_pin.enable_hosts_pinning", return_value=(True, "Pinned 193.233.112.67")) as hp_mock:
        heal = diagnostics._heal_google_connectivity(ctx=ctx, quiet=True)

    assert hp_mock.called
    assert heal["hosts_auto_enabled"] is True
    assert heal["connectivity_ok"] is True


def test_run_autopilot_guardian_cursor_and_cooldown(tmp_path: Path):
    from patcher import auto_repatch, diagnostics

    state_file = tmp_path / "autopilot_state.json"
    log_dir = tmp_path / "cli_logs"
    log_dir.mkdir(parents=True)
    log_f = log_dir / "cli-20261115_224500.log"
    log_f.write_text(
        'W1115 22:45:01 cache.go:157] Singleflight refresh failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": net/http: TLS handshake timeout\n',
        encoding="utf-8",
    )

    heal_calls = 0

    def _fake_heal(ctx=None, quiet=True):
        nonlocal heal_calls
        heal_calls += 1
        return {"proxy_healed": False, "nrpt_auto_enabled": False, "hosts_auto_enabled": True, "connectivity_ok": True, "changed": True, "diagnosis": ""}

    with mock.patch.object(diagnostics, "_autopilot_state_path", return_value=state_file), \
         mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[log_dir]), \
         mock.patch.object(auto_repatch, "inspect_targets", return_value={"needs_any": False}), \
         mock.patch.object(auto_repatch, "_append_log"), \
         mock.patch.object(diagnostics, "ensure_target_and_env_healthy", return_value={"connectivity_ok": True}), \
         mock.patch.object(diagnostics, "_heal_google_connectivity", side_effect=_fake_heal), \
         mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": True, "manager": False}), \
         mock.patch.object(diagnostics, "_restart_antigravity_apps", return_value=["Antigravity IDE"]) as rest_mock:
        res1 = diagnostics.run_autopilot_guardian(quiet=True)
        # Append another line inside cooldown window -> must not restart-loop
        with open(log_f, "a", encoding="utf-8") as fh:
            fh.write('E1115 22:45:10 errorreport.go:307] Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": net/http: TLS handshake timeout\n')
        res2 = diagnostics.run_autopilot_guardian(quiet=True)

    assert res1["status"] == "healed"
    assert "net_tls_healed" in res1["actions"]
    assert heal_calls == 1
    assert rest_mock.call_count == 1
    assert res2["status"] == "healthy"


def test_watcher_script_and_task_include_autopilot_and_repeat_trigger(tmp_path: Path):
    from patcher import auto_repatch

    ps1 = auto_repatch._build_ps_watcher_script([r"C:\Tools\Patcher.exe"])
    assert "--autopilot-heal" in ps1
    assert "Invoke-AutopilotHeal" in ps1
    assert "Get-RuntimeHeartbeatSignature" in ps1

    captured = []

    def _fake_run(cmd, **kwargs):
        captured.append(" ".join(cmd))
        m = mock.MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with mock.patch.object(auto_repatch, "_home_dir", return_value=tmp_path), \
         mock.patch("patcher.auto_repatch.subprocess.run", side_effect=_fake_run), \
         mock.patch("patcher.auto_repatch.os.name", "nt"):
        ok_reg, _ = auto_repatch._register_windows_task(tmp_path / "auto_watch.ps1")

    assert ok_reg is True
    assert captured
    assert "RepetitionInterval" in captured[0]


def test_autopilot_guardian_no_spurious_kills_on_ephemeral_env_429_or_ui_deploy(tmp_path: Path, monkeypatch):
    from patcher import auto_repatch, data_reset, diagnostics

    monkeypatch.setenv("AG_DISABLE_LOCAL_PROXY_PROBE", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:59999")

    # 1. Popping an ephemeral os.environ dead proxy must NOT mark proxy_healed=True or changed=True
    with mock.patch("patcher.diagnostics.sys.platform", "win32"), \
         mock.patch.object(diagnostics, "_read_windows_registry_proxy", return_value=(False, "")), \
         mock.patch.object(diagnostics, "_clear_dead_user_env_proxies", return_value=[]), \
         mock.patch.object(diagnostics, "_get_dead_env_proxies", return_value=[("HTTPS_PROXY", "127.0.0.1", 59999)]), \
         mock.patch.object(diagnostics, "_check_cloudcode_connectivity", return_value={"reachable": True, "tls_timeout": False, "regional_400": False, "detail": "HTTP 401"}), \
         mock.patch("patcher.nrpt_dns.is_nrpt_supported", return_value=False), \
         mock.patch("patcher.hosts_pin.is_hosts_pinned", return_value=(False, "")):
        heal = diagnostics._heal_google_connectivity(ctx={}, quiet=True)

    assert "HTTPS_PROXY" not in os.environ
    assert heal["proxy_healed"] is False
    assert heal["changed"] is False
    assert heal["connectivity_ok"] is True

    # 2. run_autopilot_guardian with 429 in fresh log + needs_ui/needs_ext only must NOT restart or reset Antigravity
    state_file = tmp_path / "autopilot_state.json"
    log_dir = tmp_path / "cli_logs"
    log_dir.mkdir(parents=True)
    log_f = log_dir / "cli-20261116_010000.log"
    log_f.write_text(
        "E1116 01:00:01 errorreport.go:307] generating and executing: RESOURCE_EXHAUSTED (code 429): Individual quota reached.\n",
        encoding="utf-8",
    )

    tgt_status = {
        "needs_any": True,
        "needs_ide": False,
        "needs_tls": False,
        "needs_mgr": False,
        "needs_agy": False,
        "needs_vsc": False,
        "needs_ui": True,
        "needs_ext": True,
    }

    with mock.patch.object(diagnostics, "_autopilot_state_path", return_value=state_file), \
         mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[log_dir]), \
         mock.patch.object(auto_repatch, "inspect_targets", return_value=tgt_status), \
         mock.patch.object(auto_repatch, "run_auto_repatch", return_value={"changed": True, "patched": ["Antigravity 2.0 UI"], "restarted": []}) as rep_mock, \
         mock.patch.object(auto_repatch, "_append_log"), \
         mock.patch.object(diagnostics, "ensure_target_and_env_healthy", return_value={"connectivity_ok": True, "proxy_healed": False, "tls_healed": False, "nrpt_auto_enabled": False, "hosts_auto_enabled": False}), \
         mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": True, "manager": True}), \
         mock.patch.object(diagnostics, "_restart_antigravity_apps") as rest_mock, \
         mock.patch.object(data_reset, "do_data_reset") as reset_mock:
        res = diagnostics.run_autopilot_guardian(quiet=True)

    rep_mock.assert_called_once()
    assert rep_mock.call_args.kwargs["restart_running"] is False
    rest_mock.assert_not_called()
    reset_mock.assert_not_called()
    assert res["status"] == "healed"
    assert res["actions"] == ["repatch:Antigravity 2.0 UI"]


def test_auto_repatch_skips_kill_and_relaunch_for_ui_only_even_with_force_relaunch():
    from patcher import auto_repatch, extension_installer
    from patcher.utils import admin, launcher

    ui_only_status = {
        "needs_any": True,
        "needs_ide": False,
        "needs_tls": False,
        "needs_mgr": False,
        "needs_agy": False,
        "needs_vsc": False,
        "needs_ext": False,
        "needs_ui": True,
        "main_js_path": "",
        "manager_path": "",
        "agy_path": "",
        "vscode_path": "",
        "vscode_agy_path": "",
    }

    with mock.patch.object(auto_repatch, "wait_for_installer_idle"), \
         mock.patch.object(auto_repatch, "inspect_targets", return_value=ui_only_status), \
         mock.patch.object(launcher, "detect_running_antigravity_apps", return_value={"ide": True, "manager": True}), \
         mock.patch.object(admin, "terminate_processes") as term_ide_mock, \
         mock.patch.object(admin, "terminate_standalone_processes") as term_mgr_mock, \
         mock.patch.object(extension_installer, "deploy_antigravity_2_ui", return_value=True) as ui_mock, \
         mock.patch.object(auto_repatch, "_restore_active_session_after_patch", return_value=[]), \
         mock.patch.object(launcher, "restart_antigravity_apps") as relaunch_mock, \
         mock.patch.object(auto_repatch, "_append_log"):
        res = auto_repatch.run_auto_repatch(
            restart_running=True,
            force_relaunch={"ide": True, "manager": True},
        )

    ui_mock.assert_called_once()
    term_ide_mock.assert_not_called()
    term_mgr_mock.assert_not_called()
    relaunch_mock.assert_not_called()
    assert res["patched"] == ["Antigravity 2.0 UI"]
    assert res["restarted"] == []


def test_heal_google_connectivity_handles_dynamic_vpn_on_off_and_nrpt_vpn_bypass(tmp_path: Path, monkeypatch):
    from patcher import diagnostics

    monkeypatch.setenv("AG_TOOLS_DIR", str(tmp_path))
    monkeypatch.setenv("AG_PROBE_LOCAL_PROXY", "1")
    monkeypatch.delenv("AG_DISABLE_LOCAL_PROXY_PROBE", raising=False)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    vpn_on = {"port": 10808}

    def _fake_tcp(host, port, timeout=0.5):
        return vpn_on["port"] == port

    with mock.patch("patcher.diagnostics.sys.platform", "win32"), \
         mock.patch.object(diagnostics, "_read_windows_registry_proxy", return_value=(False, "")), \
         mock.patch.object(diagnostics, "_clear_dead_user_env_proxies", return_value=[]), \
         mock.patch.object(diagnostics, "_check_tcp_reachable", side_effect=_fake_tcp), \
         mock.patch.object(diagnostics, "_check_cloudcode_connectivity", return_value={"reachable": True, "tls_timeout": False, "regional_400": False, "detail": "HTTP 401"}), \
         mock.patch("patcher.nrpt_dns.is_nrpt_supported", return_value=False), \
         mock.patch("patcher.nrpt_dns._flush_dns_cache"), \
         mock.patch("patcher.hosts_pin.is_hosts_pinned", return_value=(False, "")):
        # 1. VPN turns ON -> proxy_config.json created & changed=True
        h1 = diagnostics._heal_google_connectivity(ctx={}, quiet=True)
        assert h1["proxy_healed"] is True
        assert h1["changed"] is True
        assert (tmp_path / "proxy_config.json").is_file()

        # 2. VPN stays ON on same port -> idempotent (changed=False, no restart loop)
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(var, raising=False)
        h2 = diagnostics._heal_google_connectivity(ctx={}, quiet=True)
        assert h2["proxy_healed"] is False
        assert h2["changed"] is False

        # 3. VPN turns OFF -> stale proxy_config.json removed & changed=True
        vpn_on["port"] = 0
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(var, raising=False)
        h3 = diagnostics._heal_google_connectivity(ctx={}, quiet=True)
        assert h3["proxy_healed"] is True
        assert h3["changed"] is True
        assert not (tmp_path / "proxy_config.json").exists()

    # 4. require_bypass=True when NRPT active but VPN bypassed DNS to standard Google IP -> Hosts-Pinning activates
    monkeypatch.setenv("AG_DISABLE_LOCAL_PROXY_PROBE", "1")
    with mock.patch("patcher.diagnostics.sys.platform", "win32"), \
         mock.patch.object(diagnostics, "_read_windows_registry_proxy", return_value=(False, "")), \
         mock.patch.object(diagnostics, "_clear_dead_user_env_proxies", return_value=[]), \
         mock.patch.object(diagnostics, "_get_dead_env_proxies", return_value=[]), \
         mock.patch.object(diagnostics, "_check_cloudcode_connectivity", return_value={"reachable": True, "tls_timeout": False, "regional_400": False, "detail": "HTTP 401"}), \
         mock.patch("patcher.nrpt_dns.is_nrpt_supported", return_value=True), \
         mock.patch("patcher.nrpt_dns.get_nrpt_status", return_value={"active": True, "server": "37.230.192.51"}), \
         mock.patch("patcher.nrpt_dns._flush_dns_cache"), \
         mock.patch("patcher.hosts_pin.is_hosts_pinned", return_value=(False, "")), \
         mock.patch("patcher.diagnostics.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("142.250.74.10", 443))]), \
         mock.patch("patcher.hosts_pin.enable_hosts_pinning", return_value=(True, "Pinned 193.233.112.67")) as hp_mock:
        h4 = diagnostics._heal_google_connectivity(ctx={}, quiet=True, require_bypass=True)

    hp_mock.assert_called_once()
    assert h4["hosts_auto_enabled"] is True
    assert h4["changed"] is True


def test_autopilot_guardian_single_connectivity_pass_with_require_bypass_for_400(tmp_path: Path):
    from patcher import auto_repatch, diagnostics

    state_file = tmp_path / "autopilot_state.json"
    log_dir = tmp_path / "cli_logs"
    log_dir.mkdir(parents=True)
    log_f = log_dir / "cli-20261116_020000.log"
    log_f.write_text(
        'E1116 02:00:01 errorreport.go:307] Singleflight refresh failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": net/http: TLS handshake timeout\n'
        'E1116 02:00:02 errorreport.go:307] FAILED_PRECONDITION (code 400): User location is not supported for the API use.\n',
        encoding="utf-8",
    )

    with mock.patch.object(diagnostics, "_autopilot_state_path", return_value=state_file), \
         mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[log_dir]), \
         mock.patch.object(auto_repatch, "inspect_targets", return_value={"needs_any": False}), \
         mock.patch.object(auto_repatch, "_append_log"), \
         mock.patch.object(diagnostics, "ensure_target_and_env_healthy", return_value={"connectivity_ok": True}) as env_mock, \
         mock.patch.object(diagnostics, "_heal_google_connectivity", return_value={"connectivity_ok": True, "changed": True, "hosts_auto_enabled": True, "diagnosis": ""}) as heal_mock, \
         mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": False, "manager": True}), \
         mock.patch.object(diagnostics, "_restart_antigravity_apps", return_value=["Antigravity 2.0"]) as rest_mock:
        res1 = diagnostics.run_autopilot_guardian(quiet=True)

        # Follow-up 429 only -> must NOT bypass env_health_probe cooldown
        with open(log_f, "a", encoding="utf-8") as fh:
            fh.write("E1116 02:00:10 errorreport.go:307] RESOURCE_EXHAUSTED (code 429): Individual quota reached.\n")
        res2 = diagnostics.run_autopilot_guardian(quiet=True)

    assert env_mock.call_count == 1
    assert env_mock.call_args.kwargs.get("run_connectivity") is False
    heal_mock.assert_called_once()
    assert heal_mock.call_args.kwargs.get("require_bypass") is True
    assert res1["status"] == "healed"
    assert rest_mock.call_count == 1
    assert res2["status"] == "healthy"


def test_per_app_isolation_ide_error_never_restarts_healthy_manager(tmp_path: Path, monkeypatch):
    from patcher import auto_repatch, diagnostics

    monkeypatch.setenv("AG_TOOLS_DIR", str(tmp_path / "ag_tools"))

    ide_logs = tmp_path / "Antigravity IDE" / "logs" / "20261116T120000"
    mgr_logs = tmp_path / "Antigravity" / "logs" / "20261116T120000"
    ide_logs.mkdir(parents=True)
    mgr_logs.mkdir(parents=True)

    (ide_logs / "language_server.log").write_text(
        'E1116 12:00:01 errorreport.go:307] Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": net/http: TLS handshake timeout\n',
        encoding="utf-8",
    )
    (mgr_logs / "main.log").write_text("[info] Antigravity 2.0 healthy\n", encoding="utf-8")

    ide_base = tmp_path / "Antigravity IDE" / "logs"
    mgr_base = tmp_path / "Antigravity" / "logs"
    state_file = tmp_path / "ag_tools" / "autopilot_state.json"

    with mock.patch.object(diagnostics, "_autopilot_state_path", return_value=state_file), \
         mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[ide_base, mgr_base]), \
         mock.patch.object(auto_repatch, "inspect_targets", return_value={"needs_any": False}), \
         mock.patch.object(auto_repatch, "_append_log"), \
         mock.patch.object(diagnostics, "ensure_target_and_env_healthy", return_value={"connectivity_ok": True}), \
         mock.patch.object(diagnostics, "_heal_google_connectivity", return_value={"connectivity_ok": True, "changed": True, "hosts_auto_enabled": True, "diagnosis": ""}), \
         mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": True, "manager": True}), \
         mock.patch.object(diagnostics, "_restart_antigravity_apps", return_value=["Antigravity IDE"]) as rest_mock:
        res = diagnostics.run_autopilot_guardian(quiet=True)

    assert res["status"] == "healed"
    rest_mock.assert_called_once()
    mask = rest_mock.call_args.args[0]
    assert mask == {"ide": True, "manager": False}

    # Explicit target_app="ide" inside do_diagnostics also must never restart Antigravity 2.0
    fake_main_js = tmp_path / "Antigravity IDE" / "resources" / "app" / "out" / "main.js"
    fake_main_js.parent.mkdir(parents=True)
    fake_main_js.write_text("patched", encoding="utf-8")

    tls_state = {"applied": False}

    def _fake_inject(_path):
        tls_state["applied"] = True
        return True

    with mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[ide_base, mgr_base]), \
         mock.patch.object(diagnostics, "_heal_google_connectivity", return_value={"connectivity_ok": True, "changed": True}), \
         mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": True, "manager": True}), \
         mock.patch.object(diagnostics, "_restart_antigravity_apps", return_value=["Antigravity IDE"]) as diag_rest_mock, \
         mock.patch.object(diagnostics, "save_report", return_value="diag.txt"), \
         mock.patch("patcher.tls_fix.is_tls_fix_applied", side_effect=lambda _p: tls_state["applied"]), \
         mock.patch("patcher.tls_fix.do_tls_fix_inject", side_effect=_fake_inject):
        diagnostics.do_diagnostics(main_js_path=str(fake_main_js), manager_path="", agy_path="")

    diag_rest_mock.assert_called_once()
    assert diag_rest_mock.call_args.args[0] == {"ide": True, "manager": False}


def test_auto_heal_config_defaults_on_and_respects_pause_and_force(tmp_path: Path, monkeypatch):
    from patcher import auto_repatch, diagnostics

    monkeypatch.setenv("AG_TOOLS_DIR", str(tmp_path / "ag_tools"))

    # Default out of the box is ON
    cfg0 = diagnostics.load_auto_heal_config()
    assert cfg0["enabled"] is True

    # Disabling pauses background auto-heal until user clicks Fix (--force)
    diagnostics.save_auto_heal_config({"enabled": False})
    ide_logs = tmp_path / "Antigravity IDE" / "logs" / "20261116T120000"
    ide_logs.mkdir(parents=True)
    (ide_logs / "ls-main.log").write_text(
        'E1116 12:00:01 errorreport.go:307] Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": net/http: TLS handshake timeout\n',
        encoding="utf-8",
    )
    ide_base = tmp_path / "Antigravity IDE" / "logs"

    with mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[ide_base]), \
         mock.patch.object(auto_repatch, "inspect_targets", return_value={"needs_any": False}), \
         mock.patch.object(auto_repatch, "_append_log"), \
         mock.patch.object(diagnostics, "ensure_target_and_env_healthy") as env_mock, \
         mock.patch.object(diagnostics, "_heal_google_connectivity", return_value={"connectivity_ok": True, "changed": True}), \
         mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": True, "manager": True}), \
         mock.patch.object(diagnostics, "_restart_antigravity_apps", return_value=["Antigravity IDE"]) as rest_mock:
        pending_res = diagnostics.run_autopilot_guardian(quiet=True, target_app="ide")
        assert pending_res["status"] == "pending"
        assert "ide" in (pending_res.get("pending_issues") or {})
        env_mock.assert_not_called()
        rest_mock.assert_not_called()

        # Clicking in-app "Починить" runs with force=True even when auto-mode is paused
        forced_res = diagnostics.run_autopilot_guardian(quiet=True, target_app="ide", force=True)

    assert forced_res["status"] == "healed"
    rest_mock.assert_called_once()
    assert rest_mock.call_args.args[0] == {"ide": True, "manager": False}


def test_error_guardian_js_rules_and_out_of_box_default(tmp_path: Path):
    import json
    import subprocess

    js_path = Path(__file__).resolve().parent.parent / "extensions" / "core" / "error_guardian.js"
    env = os.environ.copy()
    env["AG_TOOLS_DIR"] = str(tmp_path / "ag_tools")
    script = f"""
const g = require({json.dumps(str(js_path))});
const cfg0 = g.loadAutoHealConfig();
if (cfg0.enabled !== true) throw new Error('Expected auto-heal ON by default');
g.saveAutoHealConfig({{ enabled: false }});
if (g.loadAutoHealConfig().enabled !== false) throw new Error('Expected saved enabled=false');
const tls = g.classifyErrorText('net/http: TLS handshake timeout');
if (!tls || tls.key !== 'tls_expired') throw new Error('Expected tls_expired');
const geo = g.classifyErrorText('FAILED_PRECONDITION (code 400): User location is not supported');
if (!geo || geo.key !== 'user_location_400') throw new Error('Expected user_location_400');
const px = g.classifyErrorText('proxyconnect tcp: dial tcp 127.0.0.1:10808: connectex');
if (!px || px.key !== 'dead_proxy') throw new Error('Expected dead_proxy');
if (g.classifyErrorText('RESOURCE_EXHAUSTED (code 429)') !== null) throw new Error('429 must stay in failover UI');
if (g.classifyErrorText('received prompt: TLS handshake timeout') !== null) throw new Error('Must ignore prompt echo');
console.log('OK');
"""
    proc = subprocess.run(["node", "-e", script], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "OK" in proc.stdout


def test_renderer_log_detection_and_ide_restricted_session_heal(tmp_path: Path, monkeypatch):
    import sqlite3
    from patcher import auto_repatch, diagnostics

    monkeypatch.setenv("AG_TOOLS_DIR", str(tmp_path / "ag_tools"))
    user_data = tmp_path / "Antigravity IDE"
    monkeypatch.setenv("AG_IDE_USER_DATA_DIR", str(user_data))

    ws_id = "1795154873881"
    ws_dir = user_data / "User" / "workspaceStorage" / ws_id
    ws_dir.mkdir(parents=True)
    vscdb = ws_dir / "state.vscdb"
    con = sqlite3.connect(str(vscdb))
    con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    con.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("memento/workspaceTrust", json.dumps({"isEmptyWorkspaceTrusted": False})),
    )
    con.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        ("antigravity.agentViewContainerId.state", json.dumps({"antigravity.panel": {"collapsed": False, "isHidden": True, "size": 759}})),
    )
    con.commit()
    con.close()

    g_dir = user_data / "User" / "globalStorage"
    g_dir.mkdir(parents=True)
    storage_file = g_dir / "storage.json"
    storage_file.write_text(
        json.dumps(
            {
                "backupWorkspaces": {"workspaces": [], "folders": [], "emptyWindows": [{"backupFolder": ws_id}]},
                "windowsState": {
                    "lastActiveWindow": {
                        "backupPath": str(user_data / "Backups" / ws_id),
                        "uiState": {"mode": 1, "x": 396, "y": 328, "width": 524, "height": 340},
                    },
                    "openedWindows": [],
                },
            }
        ),
        encoding="utf-8",
    )

    rend_dir = user_data / "logs" / "20261120T172607" / "window1"
    rend_dir.mkdir(parents=True)
    rend_log = rend_dir / "renderer.log"
    rend_log.write_text(
        "2026-11-20 17:31:08.849 [error] command 'antigravity.handleAuthRefresh' not found\n",
        encoding="utf-8",
    )
    # Simulate long-running Windows NTFS session dir whose folder mtime is >2h old while renderer.log is fresh
    old_ts = os.path.getmtime(rend_log) - 7200
    os.utime(user_data / "logs" / "20261120T172607", (old_ts, old_ts))

    with mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[user_data / "logs"]), \
         mock.patch.object(auto_repatch, "inspect_targets", return_value={"needs_any": False}), \
         mock.patch.object(auto_repatch, "_append_log"), \
         mock.patch("patcher.extension_installer.deploy_assistant_extension", return_value=True), \
         mock.patch.object(diagnostics, "ensure_target_and_env_healthy", return_value={"connectivity_ok": True}), \
         mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": True, "manager": True}), \
         mock.patch.object(diagnostics, "_restart_antigravity_apps", return_value=["Antigravity IDE"]) as rest_mock:
        res = diagnostics.run_autopilot_guardian(quiet=True, target_app="all")

    assert "ide_session_healed" in res["actions"]
    rest_mock.assert_called_once()
    assert rest_mock.call_args.args[0] == {"ide": True, "manager": False}

    con2 = sqlite3.connect(str(vscdb))
    rows = dict(con2.execute("SELECT key, value FROM ItemTable").fetchall())
    con2.close()
    assert json.loads(rows["memento/workspaceTrust"])["isEmptyWorkspaceTrusted"] is True
    assert json.loads(rows["antigravity.agentViewContainerId.state"])["antigravity.panel"]["isHidden"] is False

    repaired_storage = json.loads(storage_file.read_text(encoding="utf-8"))
    assert repaired_storage["backupWorkspaces"]["emptyWindows"] == []
    assert repaired_storage["windowsState"]["lastActiveWindow"]["uiState"]["width"] == 1280
    assert "backupPath" not in repaired_storage["windowsState"]["lastActiveWindow"]


def test_sni_host_refused_rotates_pinned_ip_excluding_bad_node():
    from patcher import diagnostics, hosts_pin

    # 1. Verify hosts_pin.discover_sni_proxy_ip skips excluded IP
    with mock.patch.object(hosts_pin, "DISCOVERY_RESOLVERS", []), \
         mock.patch.object(hosts_pin, "VERIFIED_SNI_PROXIES", ["193.233.112.68", "193.233.112.67"]), \
         mock.patch.object(hosts_pin, "test_tls_sni", return_value=True):
        picked = hosts_pin.discover_sni_proxy_ip(exclude_ips={"193.233.112.68"})
        assert picked == "193.233.112.67"

    # 2. Verify KnownError sni_host_refused detects ECONNREFUSED on pinned IP and triggers rotation
    err_db = diagnostics._make_error_db()
    by_key = {ke.key: ke for ke in err_db}
    sni_ke = by_key["sni_host_refused"]
    sample_line = "2026-11-20 20:46:39.968 [error] [Auth] request failed, reason: connect ECONNREFUSED 193.233.112.68:443"
    assert sni_ke.matches(sample_line)
    # Must NOT match localhost proxies (handled by dead_proxy)
    assert not sni_ke.matches("connect ECONNREFUSED 127.0.0.1:10808")

    ctx = {"_hits_by_key": {"sni_host_refused": [f"[auth.log] {sample_line}"]}}
    if sys.platform == "win32":
        with mock.patch.object(hosts_pin, "is_hosts_pinned", return_value=(True, "193.233.112.68")), \
             mock.patch.object(hosts_pin, "enable_hosts_pinning", return_value=(True, "ok")) as pin_mock, \
             mock.patch.object(diagnostics, "_heal_google_connectivity", return_value={"connectivity_ok": True}):
            assert sni_ke.is_already_fixed_fn(ctx) is False
            assert sni_ke.fix_fn(ctx) is True
            pin_mock.assert_called_once()
            assert "193.233.112.68" in (pin_mock.call_args.kwargs.get("exclude_ips") or set())


def test_ide_extension_manifest_supports_untrusted_workspaces():
    pkg_path = Path(__file__).resolve().parent.parent / "extensions" / "ide" / "package.json"
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    caps = pkg.get("capabilities") or {}
    assert (caps.get("untrustedWorkspaces") or {}).get("supported") is True



