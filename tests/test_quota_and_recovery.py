"""
Unit and integration tests for quota calculation, auto-refresh, ASAR patching,
and in-place network/backend recovery across core, IDE, and desktop targets.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT_DIR = Path(__file__).resolve().parents[1]


def _resolve_extension_dir(sub: str, peer_name: str) -> Path:
    env_key = f"AG_{sub.upper()}_DIR"
    if os.environ.get(env_key):
        return Path(os.environ[env_key])
    parent = ROOT_DIR.parent
    if "swarm" in str(ROOT_DIR):
        peer_dir = parent / peer_name / "extensions" / sub
        if peer_dir.exists():
            return peer_dir
    return ROOT_DIR / "extensions" / sub


CORE_DIR = _resolve_extension_dir("core", "core-bridge")
IDE_DIR = _resolve_extension_dir("ide", "ide-ui")
DESKTOP_DIR = _resolve_extension_dir("desktop", "desktop-ui")
SOURCE_DIR = ROOT_DIR / "source"
NODE_BIN = shutil.which("node")


def _run_node(script: str, env_extra: dict[str, str] | None = None, timeout: int = 30) -> dict:
    if not NODE_BIN:
        raise unittest.SkipTest("node executable not found on PATH")
    env = os.environ.copy()
    for proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        env.pop(proxy_key, None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [NODE_BIN, "-"],
        input=script,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        cwd=str(ROOT_DIR),
        env=env,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Node test subprocess failed (exit={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("{") and ln.endswith("}"):
            return json.loads(ln)
    raise AssertionError(f"Expected JSON output from Node test, got:\n{proc.stdout}")


def _make_sample_groups() -> list[dict]:
    return [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "window": "weekly",
                    "remainingFraction": 0.66,
                    "resetTime": "2099-01-08T00:00:00Z",
                },
                {
                    "bucketId": "gemini-5h",
                    "window": "5h",
                    "remainingFraction": 0.16,
                    "resetTime": "2099-01-01T05:00:00Z",
                },
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "buckets": [
                {
                    "bucketId": "claude-3p-weekly",
                    "window": "weekly",
                    "remainingFraction": 0,
                    "resetTime": "2099-01-08T00:00:00Z",
                },
                {
                    "bucketId": "claude-3p-5h",
                    "window": "5h",
                    "remainingFraction": 1,
                    "disabled": True,
                    "resetTime": "2099-01-01T05:00:00Z",
                },
            ],
        },
    ]


def _build_asar(asar_path: Path, files_map: dict[str, bytes]) -> None:
    """Create a minimal valid ASAR archive from relative POSIX file paths."""
    header_files: dict = {}
    payload = bytearray()
    for rel, content in sorted(files_map.items()):
        parts = rel.split("/")
        cur = header_files
        for folder in parts[:-1]:
            cur = cur.setdefault(folder, {}).setdefault("files", {})
        cur[parts[-1]] = {
            "size": len(content),
            "offset": str(len(payload)),
        }
        payload.extend(content)

    header_json = json.dumps(
        {"files": header_files},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    json_size = len(header_json)
    padded_size = (json_size + 3) & ~3
    pad_len = padded_size - json_size
    pickle_hdr = struct.pack("<IIII", 4, padded_size + 8, padded_size + 4, json_size)

    with open(asar_path, "wb") as fp:
        fp.write(pickle_hdr)
        fp.write(header_json)
        if pad_len:
            fp.write(b"\x00" * pad_len)
        fp.write(payload)


class QuotaEngineNodeTests(unittest.TestCase):
    def test_parse_groups_and_summary_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups_json = json.dumps(_make_sample_groups())
            script = textwrap.dedent(f"""
                const assert = require('assert');
                process.env.AG_TOOLS_DIR = {json.dumps(tmp)};
                process.env.AG_GEMINI_DIR = {json.dumps(tmp)};
                const engine = require({json.dumps(str(CORE_DIR / 'quota_engine.js'))});

                // 1. Gemini weekly+5h must NOT be misclassified as Claude; disabled Claude 5h must not become 100%
                const parsed = engine.parseQuotaGroups({groups_json});
                assert.strictEqual(parsed.geminiPct, 16);
                assert.strictEqual(parsed.geminiWeeklyPct, 66);
                assert.strictEqual(parsed.gemini5hPct, 16);
                assert.strictEqual(parsed.geminiLimitingBucket, 'gemini-5h');
                assert.strictEqual(parsed.claudePct, 0);
                assert.strictEqual(parsed.claudeWeeklyPct, 0);
                assert.strictEqual(parsed.claude5hPct, null);
                assert.strictEqual(parsed.claudeLimitingBucket, 'claude-3p-weekly');

                // 2. Fallback to allCands when every bucket is disabled
                const disabledOnly = engine.parseQuotaGroups([{{
                    displayName: 'Gemini Models',
                    buckets: [
                        {{ bucketId: 'gemini-weekly', window: 'weekly', remainingFraction: 0.4, disabled: true }},
                        {{ bucketId: 'gemini-5h', window: '5h', remainingFraction: 0.2, disabled: true }}
                    ]
                }}]);
                assert.strictEqual(disabledOnly.geminiPct, 20);
                assert.strictEqual(disabledOnly.geminiWeeklyPct, 40);
                assert.strictEqual(disabledOnly.gemini5hPct, null);

                // 3. Model fallback ignores 2.5, flash-lite, -low, -medium
                const sumFallback = engine.computeQuotaSummary({{
                    quota: {{
                        models: [
                            {{ name: 'gemini-2.5-pro', percentage: 5 }},
                            {{ name: 'gemini-3.1-flash-lite', percentage: 10 }},
                            {{ name: 'gemini-3.8-flash-low', percentage: 15 }},
                            {{ name: 'gemini-3.8-flash-medium', percentage: 20 }},
                            {{ name: 'gemini-3.8-flash', percentage: 55 }},
                            {{ name: 'claude-sonnet-4-6', percentage: 40 }}
                        ]
                    }}
                }});
                assert.strictEqual(sumFallback.gemini_percentage, 55);
                assert.strictEqual(sumFallback.claude_percentage, 40);
                assert.ok(engine.getIdeUserAgent().startsWith('antigravity/2.6.0 '));
                console.log(JSON.stringify({{ ok: true }}));
            """)
            res = _run_node(script)
            self.assertTrue(res["ok"])

    def test_fetch_live_quota_routing_cooldown_and_steady_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            acc_dir = Path(tmp) / "accounts"
            acc_dir.mkdir(parents=True, exist_ok=True)
            now_sec = 1_900_000_000

            pro_acc = {
                "id": "acc_pro",
                "email": "pro@example.com",
                "name": "Pro User",
                "token": {
                    "access_token": "ya29.test_pro",
                    "refresh_token": "1//0test_pro",
                    "expiry_timestamp": 2_500_000_000,
                },
                "quota": {
                    "subscription_tier": "PRO",
                    "gemini_percentage": 100,
                    "claude_percentage": 100,
                    "last_updated": now_sec,
                },
            }
            gcp_acc = {
                "id": "acc_gcp",
                "email": "gcp@example.com",
                "name": "GCP User",
                "token": {
                    "access_token": "ya29.test_gcp",
                    "refresh_token": "1//0test_gcp",
                    "expiry_timestamp": 2_500_000_000,
                },
                "quota": {
                    "subscription_tier": "PRO",
                    "gemini_percentage": 100,
                    "claude_percentage": 100,
                    "last_updated": now_sec,
                },
            }
            (acc_dir / "acc_pro.json").write_text(json.dumps(pro_acc), encoding="utf-8")
            (acc_dir / "acc_gcp.json").write_text(json.dumps(gcp_acc), encoding="utf-8")
            (Path(tmp) / "accounts.json").write_text(
                json.dumps({"current_ide_account_id": "acc_pro", "accounts": [
                    {"id": "acc_pro", "email": "pro@example.com", "name": "Pro User"},
                    {"id": "acc_gcp", "email": "gcp@example.com", "name": "GCP User"},
                ]}),
                encoding="utf-8",
            )

            groups_json = json.dumps(_make_sample_groups())
            script = textwrap.dedent(f"""
                const assert = require('assert');
                const fs = require('fs');
                const path = require('path');
                const { '{' } EventEmitter { '}' } = require('events');
                const https = require('https');

                const calls = [];
                let mockMode = 'pro';
                https.request = (opts, cb) => {{
                    const req = new EventEmitter();
                    let body = '';
                    req.write = (chunk) => {{ body += String(chunk || ''); }};
                    req.destroy = () => {{}};
                    req.end = () => {{
                        const host = opts.hostname || '';
                        const p = opts.path || '';
                        const ua = (opts.headers && opts.headers['User-Agent']) || '';
                        calls.push({{ host, path: p, ua }});

                        let respObj = {{}};
                        if (p.includes('loadCodeAssist')) {{
                            respObj = mockMode === 'pro'
                                ? {{ currentTier: {{ id: 'g1-pro-tier', usesGcpTos: true }}, paidTier: {{ id: 'g1-pro-tier' }} }}
                                : {{ currentTier: {{ id: 'standard-tier' }}, paidTier: {{ id: 'standard-tier', usesGcpTos: true }} }};
                        }} else if (p.includes('retrieveUserQuotaSummary')) {{
                            if (mockMode === 'gcp' && host.includes('daily-')) {{
                                respObj = {{ groups: [] }};
                            }} else {{
                                respObj = {{ groups: {groups_json} }};
                            }}
                        }}

                        const res = new EventEmitter();
                        res.statusCode = 200;
                        res.setEncoding = () => {{}};
                        process.nextTick(() => {{
                            cb(res);
                            res.emit('data', JSON.stringify(respObj));
                            res.emit('end');
                        }});
                    }};
                    return req;
                }};

                process.env.AG_TOOLS_DIR = {json.dumps(tmp)};
                process.env.AG_GEMINI_DIR = {json.dumps(tmp)};
                const engine = require({json.dumps(str(CORE_DIR / 'quota_engine.js'))});

                (async () => {{
                    // A. Legacy account without uses_gcp_tos MUST bypass minAgeSec=60 once and persist uses_gcp_tos=false
                    mockMode = 'pro';
                    const resPro = await engine.fetchLiveQuota('acc_pro', {{ proxyUrl: null, minAgeSec: 60 }});
                    assert.ok(resPro);
                    assert.strictEqual(resPro.gemini_percentage, 16);
                    assert.strictEqual(resPro.claude_percentage, 0);
                    assert.strictEqual(resPro.gemini_weekly_percentage, 66);
                    assert.strictEqual(resPro.gemini_5h_percentage, 16);
                    assert.strictEqual(resPro.claude_5h_percentage, null);
                    assert.ok(calls.every((c) => c.ua.startsWith('antigravity/2.6.0 ')));
                    assert.ok(calls.some((c) => c.host === 'daily-cloudcode-pa.googleapis.com' && c.path.includes('retrieveUserQuotaSummary')));

                    const persistedPro = JSON.parse(fs.readFileSync(path.join({json.dumps(str(acc_dir))}, 'acc_pro.json'), 'utf8'));
                    assert.strictEqual(persistedPro.quota.uses_gcp_tos, false);

                    // B. Immediate second call with minAgeSec=60 hits cooldown with 0 HTTP requests
                    calls.length = 0;
                    const resCool = await engine.fetchLiveQuota('acc_pro', {{ proxyUrl: null, minAgeSec: 60 }});
                    assert.strictEqual(resCool.skippedByCooldown, true);
                    assert.strictEqual(calls.length, 0);

                    // C. paidTier.usesGcpTos === true routes to cloudcode-pa.googleapis.com
                    mockMode = 'gcp';
                    calls.length = 0;
                    const resGcp = await engine.fetchLiveQuota('acc_gcp', {{ proxyUrl: null, minAgeSec: 60 }});
                    assert.ok(resGcp);
                    assert.strictEqual(resGcp.gemini_percentage, 16);
                    assert.ok(calls.some((c) => c.host === 'cloudcode-pa.googleapis.com' && c.path.includes('retrieveUserQuotaSummary')));
                    const persistedGcp = JSON.parse(fs.readFileSync(path.join({json.dumps(str(acc_dir))}, 'acc_gcp.json'), 'utf8'));
                    assert.strictEqual(persistedGcp.quota.uses_gcp_tos, true);

                    // D. Steady-state forced refresh with cached tier & uses_gcp_tos makes strictly 1 POST to prod host
                    calls.length = 0;
                    const steady = await engine.fetchLiveQuota('acc_gcp', {{ proxyUrl: null, minAgeSec: 0 }});
                    assert.strictEqual(steady.gemini_percentage, 16);
                    assert.strictEqual(calls.length, 1);
                    assert.strictEqual(calls[0].host, 'cloudcode-pa.googleapis.com');
                    assert.ok(calls[0].path.includes('retrieveUserQuotaSummary'));

                    console.log(JSON.stringify({{ ok: true }}));
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script)
            self.assertTrue(res["ok"])

    def test_5h_primary_rule_and_app_decoupling(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            gemini_dir = Path(tmp) / "gemini"
            acc_dir = tools_dir / "accounts"
            (gemini_dir / "antigravity-cli").mkdir(parents=True, exist_ok=True)
            acc_dir.mkdir(parents=True, exist_ok=True)

            (acc_dir / "acc_ide.json").write_text(
                json.dumps({
                    "id": "acc_ide",
                    "email": "ide@example.com",
                    "name": "IDE Acc",
                    "token": {"refresh_token": "1//0ide", "access_token": "ya29.ide"},
                    "quota": {"subscription_tier": "PRO", "uses_gcp_tos": False},
                }),
                encoding="utf-8",
            )
            (acc_dir / "acc_sa.json").write_text(
                json.dumps({
                    "id": "acc_sa",
                    "email": "sa@example.com",
                    "name": "Standalone Acc",
                    "token": {"refresh_token": "1//0sa", "access_token": "ya29.sa"},
                    "quota": {"subscription_tier": "ULTRA", "uses_gcp_tos": False},
                }),
                encoding="utf-8",
            )
            (tools_dir / "accounts.json").write_text(
                json.dumps({
                    "current_ide_account_id": "acc_ide",
                    "current_account_id": "acc_ide",
                    "current_standalone_account_id": "acc_sa",
                    "accounts": [
                        {"id": "acc_ide", "email": "ide@example.com", "name": "IDE Acc", "last_used": 200},
                        {"id": "acc_sa", "email": "sa@example.com", "name": "Standalone Acc", "standalone_last_used": 100},
                    ],
                }),
                encoding="utf-8",
            )
            (gemini_dir / "antigravity-cli" / "antigravity-oauth-token").write_text(
                json.dumps({"token": {"refresh_token": "1//0ide"}, "id_token_email": "ide@example.com"}),
                encoding="utf-8",
            )
            (gemini_dir / "jetski-desktopapp-oauth-token").write_text(
                json.dumps({"token": {"refresh_token": "1//0sa"}, "id_token_email": "sa@example.com"}),
                encoding="utf-8",
            )

            script = textwrap.dedent(f"""
                const assert = require('assert');
                const fs = require('fs');
                const path = require('path');
                process.env.AG_TOOLS_DIR = {json.dumps(str(tools_dir))};
                process.env.AG_GEMINI_DIR = {json.dumps(str(gemini_dir))};
                const engine = require({json.dumps(str(CORE_DIR / 'quota_engine.js'))});

                // 1. 5h primary rule: weekly > 0 picks 5h even when 5h > weekly; weekly == 0 forces 0
                const g5h = engine.parseQuotaGroups([
                    {{
                        displayName: 'Gemini Models',
                        buckets: [
                            {{ bucketId: 'gemini-weekly', window: 'weekly', remainingFraction: 0.57, resetTime: '2099-01-08T00:00:00Z' }},
                            {{ bucketId: 'gemini-5h', window: '5h', remainingFraction: 0.71, resetTime: '2099-01-01T05:00:00Z' }}
                        ]
                    }},
                    {{
                        displayName: 'Claude and GPT models',
                        buckets: [
                            {{ bucketId: 'claude-3p-weekly', window: 'weekly', remainingFraction: 0, resetTime: '2099-01-08T00:00:00Z' }},
                            {{ bucketId: 'claude-3p-5h', window: '5h', remainingFraction: 1, resetTime: '2099-01-01T05:00:00Z' }}
                        ]
                    }}
                ]);
                assert.strictEqual(g5h.geminiPct, 71);
                assert.strictEqual(g5h.geminiLimitingBucket, 'gemini-5h');
                assert.strictEqual(g5h.geminiWeeklyPct, 57);
                assert.strictEqual(g5h.gemini5hPct, 71);
                assert.strictEqual(g5h.claudePct, 0);
                assert.strictEqual(g5h.claudeLimitingBucket, 'claude-3p-weekly');

                // 2. Decoupled active account detection between IDE and Standalone
                const stIde = engine.loadState({{ appTarget: 'ide' }});
                const stSa = engine.loadState({{ appTarget: 'standalone' }});
                assert.strictEqual(stIde.activeAccount.id, 'acc_ide');
                assert.strictEqual(stSa.activeAccount.id, 'acc_sa');

                // 3. Removing standalone file + index must NOT leak current_ide_account_id
                fs.unlinkSync(path.join({json.dumps(str(gemini_dir))}, 'jetski-desktopapp-oauth-token'));
                fs.writeFileSync(path.join({json.dumps(str(tools_dir))}, 'accounts.json'), JSON.stringify({{
                    current_ide_account_id: 'acc_ide',
                    current_account_id: 'acc_ide',
                    accounts: [
                        {{ id: 'acc_ide', email: 'ide@example.com', last_used: 200 }},
                        {{ id: 'acc_sa', email: 'sa@example.com' }}
                    ]
                }}));
                const stSaEmpty = engine.loadState({{ appTarget: 'standalone' }});
                assert.strictEqual(stSaEmpty.activeAccount, null);
                const stIdeStill = engine.loadState({{ appTarget: 'ide' }});
                assert.strictEqual(stIdeStill.activeAccount.id, 'acc_ide');

                console.log(JSON.stringify({{ ok: true }}));
            """)
            res = _run_node(script)
            self.assertTrue(res["ok"])


class AccountSwitcherPythonTests(unittest.TestCase):
    def _load_isolated_switcher(self, tmp_dir: str):
        env_patch = {"AG_TOOLS_DIR": tmp_dir, "AG_GEMINI_DIR": tmp_dir}
        with mock.patch.dict(os.environ, env_patch):
            spec = importlib.util.spec_from_file_location(
                f"test_account_switcher_{os.getpid()}",
                str(CORE_DIR / "account_switcher.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(mod)
            return mod

    def test_parse_groups_summary_and_fetch_live_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._load_isolated_switcher(tmp)
            self.assertTrue(mod.get_ide_user_agent().startswith("antigravity/2.6.0 "))

            parsed = mod.parse_quota_groups(_make_sample_groups())
            self.assertEqual(parsed["gemini_pct"], 16)
            self.assertEqual(parsed["gemini_weekly_pct"], 66)
            self.assertEqual(parsed["gemini_5h_pct"], 16)
            self.assertEqual(parsed["gemini_limiting_bucket"], "gemini-5h")
            self.assertEqual(parsed["claude_pct"], 0)
            self.assertEqual(parsed["claude_weekly_pct"], 0)
            self.assertIsNone(parsed["claude_5h_pct"])

            summary = mod.compute_quota_summary(
                {
                    "tier": "PRO",
                    "quota": {
                        "quota_groups": _make_sample_groups(),
                        "subscription_tier": "PRO",
                    },
                }
            )
            self.assertEqual(summary["gemini_percentage"], 16)
            self.assertEqual(summary["gemini_weekly_percentage"], 66)
            self.assertEqual(summary["gemini_5h_percentage"], 16)
            self.assertEqual(summary["claude_percentage"], 0)
            self.assertEqual(summary["claude_weekly_percentage"], 0)
            self.assertIsNone(summary["claude_5h_percentage"])

            acc_dir = Path(tmp) / "accounts"
            acc_dir.mkdir(parents=True, exist_ok=True)
            acc_file = acc_dir / "acc_1.json"
            acc_file.write_text(
                json.dumps({
                    "id": "acc_1",
                    "email": "user@example.com",
                    "name": "User",
                    "token": {
                        "access_token": "ya29.py",
                        "refresh_token": "1//0py",
                        "expiry_timestamp": 2_500_000_000,
                    },
                    "quota": {"subscription_tier": "PRO", "last_updated": 1_900_000_000},
                }),
                encoding="utf-8",
            )
            (Path(tmp) / "accounts.json").write_text(
                json.dumps({"current_ide_account_id": "acc_1", "accounts": [{"id": "acc_1", "email": "user@example.com", "name": "User"}]}),
                encoding="utf-8",
            )

            recorded_calls: list[tuple[str, str, str]] = []

            def fake_post(endpoint, headers, timeout=3.5, host=mod.DAILY_COMPANION_HOST, body=b"{}", opener=None):
                recorded_calls.append((endpoint, host, headers.get("User-Agent", "")))
                if endpoint == "loadCodeAssist":
                    return {"currentTier": {"id": "g1-pro-tier", "usesGcpTos": True}, "paidTier": {"id": "g1-pro-tier"}}, 200
                if endpoint == "retrieveUserQuotaSummary":
                    return {"groups": _make_sample_groups()}, 200
                return {}, 200

            with mock.patch.object(mod, "_post_companion_json", side_effect=fake_post), mock.patch.object(
                mod, "_get_active_proxy_url", return_value=None
            ):
                res1 = mod.fetch_live_quota(acc_file)
                self.assertIsNotNone(res1)
                self.assertEqual(res1["gemini_percentage"], 16)
                self.assertFalse(res1["quota"]["uses_gcp_tos"])
                self.assertTrue(all(ua.startswith("antigravity/2.6.0 ") for _, _, ua in recorded_calls))
                self.assertTrue(any(ep == "retrieveUserQuotaSummary" and h == mod.DAILY_COMPANION_HOST for ep, h, _ in recorded_calls))

                # Steady-state second fetch should make strictly 1 POST (retrieveUserQuotaSummary only)
                recorded_calls.clear()
                res2 = mod.fetch_live_quota(acc_file)
                self.assertIsNotNone(res2)
                self.assertEqual(len(recorded_calls), 1)
                self.assertEqual(recorded_calls[0][0], "retrieveUserQuotaSummary")

    def test_5h_primary_and_decoupled_switch_runner(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            gemini_dir = Path(tmp) / "gemini"
            appdata_dir = Path(tmp) / "appdata"
            acc_dir = tools_dir / "accounts"
            ide_gs = appdata_dir / "Antigravity IDE" / "User" / "globalStorage"
            sa_storage_dir = appdata_dir / "antigravity"
            acc_dir.mkdir(parents=True, exist_ok=True)
            gemini_dir.mkdir(parents=True, exist_ok=True)
            ide_gs.mkdir(parents=True, exist_ok=True)
            sa_storage_dir.mkdir(parents=True, exist_ok=True)

            vscdb_path = ide_gs / "state.vscdb"
            con = sqlite3.connect(str(vscdb_path))
            con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
            con.execute(
                "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                ("jetski.onboarding.lastLoginUsername", "stale@example.com"),
            )
            con.commit()
            con.close()

            for aid, email, rt in (
                ("acc_ide", "ide@example.com", "1//0ide"),
                ("acc_sa", "sa@example.com", "1//0sa"),
            ):
                (acc_dir / f"{aid}.json").write_text(
                    json.dumps({
                        "id": aid,
                        "email": email,
                        "name": aid,
                        "token": {"access_token": "ya29.old", "refresh_token": rt, "expires_in": 3599},
                        "quota": {"subscription_tier": "PRO", "uses_gcp_tos": False},
                    }),
                    encoding="utf-8",
                )
            (tools_dir / "accounts.json").write_text(
                json.dumps({
                    "current_ide_account_id": "acc_ide",
                    "current_account_id": "acc_ide",
                    "accounts": [
                        {"id": "acc_ide", "email": "ide@example.com", "name": "IDE", "last_used": 100},
                        {"id": "acc_sa", "email": "sa@example.com", "name": "SA"},
                    ],
                }),
                encoding="utf-8",
            )

            sw_mod = self._load_isolated_switcher(tmp)
            with mock.patch.dict(os.environ, {"AG_TOOLS_DIR": str(tools_dir), "AG_GEMINI_DIR": str(gemini_dir)}):
                parsed = sw_mod.parse_quota_groups([
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.57},
                            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.71},
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {"bucketId": "claude-3p-weekly", "window": "weekly", "remainingFraction": 0.0},
                            {"bucketId": "claude-3p-5h", "window": "5h", "remainingFraction": 1.0},
                        ],
                    },
                ])
                self.assertEqual(parsed["gemini_pct"], 71)
                self.assertEqual(parsed["gemini_limiting_bucket"], "gemini-5h")
                self.assertEqual(parsed["claude_pct"], 0)

            env_patch = {
                "AG_TOOLS_DIR": str(tools_dir),
                "AG_GEMINI_DIR": str(gemini_dir),
                "APPDATA": str(appdata_dir),
            }
            with mock.patch.dict(os.environ, env_patch):
                spec = importlib.util.spec_from_file_location(
                    f"test_switch_runner_{os.getpid()}",
                    str(CORE_DIR / "switch_runner.py"),
                )
                sr = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(sr)

                wincred_calls: list[tuple[str, str]] = []
                with mock.patch.object(sr, "refresh_google_token", return_value=("ya29.fresh", 3599, "")), mock.patch.object(
                    sr,
                    "update_wincred_antigravity",
                    side_effect=lambda *a, **kw: wincred_calls.append((kw.get("target_name", ""), kw.get("username", ""))) or True,
                ):
                    sr.apply_credentials_post_kill("acc_sa", app_type="standalone")

                # Standalone switch updates ONLY jetski-desktopapp-oauth-token + app_storage.json + jetski:antigravity
                self.assertEqual(wincred_calls, [("jetski:antigravity", "antigravity")])
                self.assertTrue((gemini_dir / "jetski-desktopapp-oauth-token").is_file())
                self.assertFalse((gemini_dir / "jetski-standalone-oauth-token").exists())

                idx = json.loads((tools_dir / "accounts.json").read_text(encoding="utf-8"))
                self.assertEqual(idx["current_standalone_account_id"], "acc_sa")
                self.assertEqual(idx["current_ide_account_id"], "acc_ide")

                sa_storage = json.loads((appdata_dir / "antigravity" / "app_storage.json").read_text(encoding="utf-8"))
                self.assertEqual(sa_storage["agy.onboarding.lastLoginUsername"], "sa@example.com")
                self.assertEqual(sa_storage["agy.onboarding.lastLoginIsGcpTos"], "false")
                self.assertEqual(sa_storage["agy.onboarding.welcomeModalSeen"], "true")

                # Switching IDE updates state.vscdb onboarding + google.google-antigravity JSON without changing standalone
                wincred_calls.clear()
                with mock.patch.object(sr, "refresh_google_token", return_value=("ya29.ide2", 3599, "")), mock.patch.object(
                    sr,
                    "update_wincred_antigravity",
                    side_effect=lambda *a, **kw: wincred_calls.append((kw.get("target_name", ""), kw.get("username", ""))) or True,
                ):
                    sr.apply_credentials_post_kill("acc_ide", app_type="ide")

                self.assertEqual(wincred_calls, [("gemini:antigravity", "antigravity")])
                idx2 = json.loads((tools_dir / "accounts.json").read_text(encoding="utf-8"))
                self.assertEqual(idx2["current_standalone_account_id"], "acc_sa")
                self.assertEqual(idx2["current_ide_account_id"], "acc_ide")

                con = sqlite3.connect(str(vscdb_path))
                kv = dict(con.execute("SELECT key, value FROM ItemTable").fetchall())
                con.close()
                self.assertEqual(kv["agy.onboarding.lastLoginUsername"], "ide@example.com")
                self.assertEqual(kv["agy.onboarding.lastLoginIsGcpTos"], "false")
                self.assertEqual(kv["agy.onboarding.welcomeModalSeen"], "true")
                self.assertEqual(kv["jetski.onboarding.lastLoginUsername"], "ide@example.com")
                self.assertEqual(kv["jetski.onboarding.lastLoginIsGcpTos"], "false")
                self.assertEqual(sa_storage["jetski.onboarding.lastLoginUsername"], "sa@example.com")
                self.assertNotIn("agy.onboarding.lastLoginStrategy", kv)
                self.assertNotIn("jetski.onboarding.lastLoginStrategy", kv)
                ext_json = json.loads(kv["google.google-antigravity"])
                self.assertIs(ext_json["antigravity.accountTelemetryEnabled"], True)
                self.assertIs(ext_json["antigravity.legacyTelemetrySettingNoticeShown"], True)
                self.assertEqual(ext_json["antigravity-storage"]["agy.onboarding.lastLoginUsername"], "ide@example.com")
                self.assertEqual(ext_json["antigravity-storage"]["agy.onboarding.lastLoginIsGcpTos"], "false")
                self.assertEqual(ext_json["antigravity-storage"]["jetski.onboarding.lastLoginUsername"], "ide@example.com")

    def test_standalone_switch_runner_exe_termination_relaunch_and_ipc_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            gemini_dir = Path(tmp) / "gemini"
            local_app = Path(tmp) / "local"
            sa_exe = local_app / "Programs" / "antigravity" / "Antigravity.exe"
            ide_exe = local_app / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe"
            sa_exe.parent.mkdir(parents=True, exist_ok=True)
            ide_exe.parent.mkdir(parents=True, exist_ok=True)
            sa_exe.write_bytes(b"sa")
            ide_exe.write_bytes(b"ide")
            (tools_dir / "accounts").mkdir(parents=True, exist_ok=True)
            gemini_dir.mkdir(parents=True, exist_ok=True)
            (tools_dir / "accounts" / "acc_sa.json").write_text(
                json.dumps({
                    "id": "acc_sa",
                    "email": "sa@example.com",
                    "name": "Standalone User",
                    "token": {"access_token": "ya29.sa", "refresh_token": "1//0sa"},
                    "quota": {"subscription_tier": "PRO", "uses_gcp_tos": False},
                }),
                encoding="utf-8",
            )
            (tools_dir / "accounts.json").write_text(
                json.dumps({"accounts": [{"id": "acc_sa", "email": "sa@example.com", "name": "Standalone User"}]}),
                encoding="utf-8",
            )

            env_patch = {
                "AG_TOOLS_DIR": str(tools_dir),
                "AG_GEMINI_DIR": str(gemini_dir),
                "LOCALAPPDATA": str(local_app),
                "ProgramFiles": str(Path(tmp) / "pf_empty"),
            }
            with mock.patch.dict(os.environ, env_patch):
                spec = importlib.util.spec_from_file_location(
                    f"test_switch_runner_sa_{os.getpid()}",
                    str(CORE_DIR / "switch_runner.py"),
                )
                sr = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(sr)

                # 1. get_ide_exe_path('standalone') finds Antigravity.exe and ignores Antigravity IDE.exe
                class FakeProc:
                    def __init__(self, pid, name, exe):
                        self.info = {"pid": pid, "name": name, "exe": exe}
                        self.killed = False
                    def kill(self):
                        self.killed = True

                sa_proc = FakeProc(101, "Antigravity.exe", str(sa_exe))
                sa_ls_proc = FakeProc(102, "language_server.exe", str(sa_exe.parent / "resources" / "bin" / "language_server.exe"))
                ide_proc = FakeProc(201, "Antigravity IDE.exe", str(ide_exe))
                agy_proc = FakeProc(202, "agy.exe", str(gemini_dir / "bin" / "agy.exe"))
                ide_ls_proc = FakeProc(203, "language_server_windows_x64.exe", str(ide_exe.parent / "resources" / "language_server_windows_x64.exe"))
                procs = [ide_proc, agy_proc, ide_ls_proc, sa_proc, sa_ls_proc]

                fake_psutil = type("FakePsutil", (), {
                    "process_iter": staticmethod(lambda _attrs=None: [p for p in procs if not p.killed])
                })()
                with mock.patch.dict(sys.modules, {"psutil": fake_psutil}):
                    found_exe, found_type = sr.get_ide_exe_path("standalone")
                    self.assertEqual(found_type, "standalone")
                    self.assertEqual(found_exe, sa_exe)

                    with mock.patch.object(sr.subprocess, "run") as run_mock, mock.patch.object(sr.time, "sleep"):
                        sr.terminate_antigravity_processes("standalone")
                    # Never invokes taskkill on IDE/agy images
                    for call in run_mock.call_args_list:
                        cmd = " ".join(call.args[0])
                        self.assertNotIn("Antigravity IDE", cmd)
                        self.assertNotIn("agy.exe", cmd)
                        self.assertNotIn("language_server_windows_x64", cmd)

                self.assertTrue(sa_proc.killed)
                self.assertTrue(sa_ls_proc.killed)
                self.assertFalse(ide_proc.killed)
                self.assertFalse(agy_proc.killed)
                self.assertFalse(ide_ls_proc.killed)

                # 2. main() supervisor relaunches Antigravity 2.0 on --app standalone unless --no-restart
                with mock.patch.object(sr, "get_ide_exe_path", return_value=(sa_exe, "standalone")), mock.patch.object(
                    sr, "terminate_antigravity_processes"
                ), mock.patch.object(
                    sr, "apply_credentials_post_kill", return_value={"name": "Standalone User", "email": "sa@example.com"}
                ), mock.patch.object(sr, "relaunch_ide") as relaunch_mock, mock.patch.object(sr.time, "sleep"):
                    with mock.patch.object(sys, "argv", ["switch_runner.py", "--target", "acc_sa", "--app", "standalone"]):
                        sr.main()
                    relaunch_mock.assert_called_once_with(sa_exe, "", "standalone")

                    relaunch_mock.reset_mock()
                    with mock.patch.object(sys, "argv", ["switch_runner.py", "--target", "acc_sa", "--app", "standalone", "--no-restart"]):
                        sr.main()
                    relaunch_mock.assert_not_called()

                # 3. account_switcher.switch_to_account spawns switch_runner for standalone restart=True
                sw_mod = self._load_isolated_switcher(tmp)
                with mock.patch.dict(os.environ, env_patch):
                    sw_mod.AG_TOOLS_DIR = tools_dir
                    sw_mod.GEMINI_DIR = gemini_dir
                    with mock.patch("subprocess.Popen") as popen_mock:
                        res = sw_mod.switch_to_account("acc_sa", app_type="standalone", restart=True)
                    self.assertTrue(res["ok"])
                    self.assertEqual(res["app_type"], "standalone")
                    self.assertTrue(res["restarting"])
                    popen_cmd = popen_mock.call_args.args[0]
                    self.assertIn("--app", popen_cmd)
                    app_idx = popen_cmd.index("--app")
                    self.assertEqual(popen_cmd[app_idx + 1], "standalone")


class IdeExtensionTests(unittest.TestCase):
    def test_status_bar_modal_recovery_and_focus_watchers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            gemini_dir = Path(tmp) / "gemini"
            acc_dir = tools_dir / "accounts"
            convo_dir = gemini_dir / "antigravity-ide" / "conversations"
            tools_dir.mkdir(parents=True, exist_ok=True)
            acc_dir.mkdir(parents=True, exist_ok=True)
            convo_dir.mkdir(parents=True, exist_ok=True)

            # Copy canonical quota_engine.js into temp tools_dir for getQuotaEngine() integration
            shutil.copy2(CORE_DIR / "quota_engine.js", tools_dir / "quota_engine.js")

            (acc_dir / "acc_ide.json").write_text(
                json.dumps({
                    "id": "acc_ide",
                    "email": "ide@example.com",
                    "name": "IDE Tester",
                    "token": {
                        "access_token": "ya29.ide",
                        "refresh_token": "1//0ide",
                        "expiry_timestamp": 2_500_000_000,
                    },
                    "quota": {
                        "subscription_tier": "PRO",
                        "uses_gcp_tos": False,
                        "quota_groups": _make_sample_groups(),
                        "last_updated": 1_900_000_000,
                    },
                }),
                encoding="utf-8",
            )
            (tools_dir / "accounts.json").write_text(
                json.dumps({"current_ide_account_id": "acc_ide", "accounts": [{"id": "acc_ide", "email": "ide@example.com", "name": "IDE Tester"}]}),
                encoding="utf-8",
            )

            script = textwrap.dedent(f"""
                const assert = require('assert');
                const fs = require('fs');
                const path = require('path');
                const Module = require('module');
                const { '{' } EventEmitter { '}' } = require('events');
                const https = require('https');

                process.env.AG_TOOLS_DIR = {json.dumps(str(tools_dir))};
                process.env.AG_GEMINI_DIR = {json.dumps(str(gemini_dir))};

                const shownMessages = [];
                const executedCommands = [];
                const statusBarItemMock = {{ text: '', tooltip: null, show() {{}}, dispose() {{}} }};
                let windowStateHandler = null;
                const registeredCommands = new Map();

                const vscodeMock = {{
                    StatusBarAlignment: {{ Right: 2 }},
                    ProgressLocation: {{ Notification: 15 }},
                    Uri: {{ file: (p) => ({{ fsPath: p, toString: () => 'file://' + p }}) }},
                    ThemeIcon: class {{ constructor(id) {{ this.id = id; }} }},
                    MarkdownString: class {{
                        constructor() {{ this.value = ''; }}
                        appendMarkdown(s) {{ this.value += s; }}
                    }},
                    workspace: {{
                        getConfiguration: () => ({{ get: () => 15 }}),
                        saveAll: async () => true,
                        workspaceFolders: []
                    }},
                    extensions: {{
                        getExtension: (id) => id === 'google.google-antigravity' ? ({{ exports: {{ port: 51920 }} }}) : null
                    }},
                    commands: {{
                        registerCommand: (cmd, cb) => {{ registeredCommands.set(cmd, cb); return {{ dispose() {{}} }}; }},
                        executeCommand: async (cmd, ...args) => {{ executedCommands.push({{ cmd, args }}); }}
                    }},
                    window: {{
                        state: {{ focused: true }},
                        createStatusBarItem: () => statusBarItemMock,
                        onDidChangeWindowState: (cb) => {{ windowStateHandler = cb; return {{ dispose() {{}} }}; }},
                        withProgress: async (_opts, task) => task({{ report() {{}} }}),
                        showInformationMessage: async (msg, opts) => {{ shownMessages.push({{ msg, opts }}); return null; }},
                        showWarningMessage: async () => null,
                        showErrorMessage: async () => null,
                        showQuickPick: async () => null
                    }}
                }};

                https.request = (_opts, cb) => {{
                    const req = new EventEmitter();
                    req.write = () => {{}};
                    req.destroy = () => {{}};
                    req.end = () => {{
                        const res = new EventEmitter();
                        res.statusCode = 200;
                        res.setEncoding = () => {{}};
                        process.nextTick(() => {{
                            cb(res);
                            if ((_opts.hostname || '').includes('cloudflare')) {{
                                res.emit('data', 'loc=DE\\n');
                            }} else if ((_opts.path || '').includes('retrieveUserQuotaSummary')) {{
                                res.emit('data', JSON.stringify({{ groups: {json.dumps(_make_sample_groups())} }}));
                            }} else {{
                                res.emit('data', '{{}}');
                            }}
                            res.emit('end');
                        }});
                    }};
                    return req;
                }};

                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === 'vscode') return vscodeMock;
                    if (request === 'child_process') {{
                        return {{
                            exec: (_cmd, _opts, cb) => cb && cb(null, '', ''),
                            execFile: (bin, args, _opts, cb) => {{
                                if (typeof _opts === 'function') {{ cb = _opts; }}
                                const low = String(bin || '').toLowerCase();
                                if (low.includes('powershell')) {{
                                    // Fake no live agy process unless restartAgyHubIfNeeded uses injected findPids
                                    cb && cb(null, '', '');
                                    return;
                                }}
                                cb && cb(null, '', '');
                            }}
                        }};
                    }}
                    return origLoad.call(this, request, parent, isMain);
                }};

                const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});

                (async () => {{
                    ext._resetStateForTests();
                    const ctx = {{ subscriptions: [] }};
                    ext.activate(ctx);

                    // 1. Status bar pill & clean tooltip breakdown without backticks
                    await ext.updateStatusBar({{ forceNetwork: true, forceRender: true }});
                    assert.ok(statusBarItemMock.text.includes('[✻ 0% | ✦ 16%]'), statusBarItemMock.text);
                    const md = statusBarItemMock.tooltip.value;
                    assert.ok(!md.includes('`'), md);
                    assert.ok(md.includes('↳ 5ч лимит: **16%**'), md);
                    assert.ok(md.includes('↳ Недельный: **66%**'), md);
                    assert.ok(md.includes('↳ Недельный: **0%**'), md);
                    assert.ok(!md.includes('↳ 5ч лимит: **100%**'), md);

                    // 2. Modal quota details include 5h + Weekly breakdown
                    shownMessages.length = 0;
                    await ext.showQuotaDetails();
                    assert.strictEqual(shownMessages.length, 1);
                    const modalMsg = shownMessages[0].msg;
                    assert.ok(modalMsg.includes('Gemini 3.8 Flash: 16%'), modalMsg);
                    assert.ok(modalMsg.includes('Детализация окон (5ч / Неделя)'), modalMsg);
                    assert.ok(modalMsg.includes('5ч лимит: 16%'), modalMsg);
                    assert.ok(modalMsg.includes('Недельный лимит: 66%'), modalMsg);

                    // 3. restartAgyHubIfNeeded guards: offline, no_port, cooldown (<35s), budget (<2 per 600s)
                    const offlineRes = await ext.restartAgyHubIfNeeded({{ conn: {{ ok: false }}, hubPort: 51920 }});
                    assert.strictEqual(offlineRes.reason, 'offline');
                    const noPortRes = await ext.restartAgyHubIfNeeded({{ conn: {{ ok: true }}, hubPort: 0 }});
                    assert.strictEqual(noPortRes.reason, 'no_port');

                    const killedPids = [];
                    let fakeNow = 2_000_000_000_000;
                    const origNow = Date.now;
                    Date.now = () => fakeNow;
                    try {{
                        const r1 = await ext.restartAgyHubIfNeeded({{
                            conn: {{ ok: true, countryCode: 'DE' }},
                            hubPort: 51920,
                            findPids: async () => [4242],
                            killPid: (pid) => killedPids.push(pid)
                        }});
                        assert.strictEqual(r1.restarted, true);
                        assert.deepStrictEqual(killedPids, [4242]);

                        fakeNow += 5000; // within 35s cooldown
                        const rCool = await ext.restartAgyHubIfNeeded({{
                            conn: {{ ok: true, countryCode: 'DE' }},
                            hubPort: 51920,
                            findPids: async () => [4242],
                            killPid: (pid) => killedPids.push(pid)
                        }});
                        assert.strictEqual(rCool.restarted, false);
                        assert.strictEqual(rCool.reason, 'cooldown');

                        fakeNow += 36000; // past 35s cooldown, still in 600s budget (2nd allowed)
                        const r2 = await ext.restartAgyHubIfNeeded({{
                            conn: {{ ok: true, countryCode: 'DE' }},
                            hubPort: 51920,
                            findPids: async () => [4343],
                            killPid: (pid) => killedPids.push(pid)
                        }});
                        assert.strictEqual(r2.restarted, true);

                        fakeNow += 40000; // 3rd inside 600s window blocked by budget
                        const rBudget = await ext.restartAgyHubIfNeeded({{
                            conn: {{ ok: true, countryCode: 'DE' }},
                            hubPort: 51920,
                            findPids: async () => [4444],
                            killPid: (pid) => killedPids.push(pid)
                        }});
                        assert.strictEqual(rBudget.restarted, false);
                        assert.strictEqual(rBudget.reason, 'budget');
                    }} finally {{
                        Date.now = origNow;
                    }}

                    // 4. Unfocused scheduleConvoQuotaRefresh queues pendingConvoRefresh and flushes on focus
                    vscodeMock.window.state.focused = false;
                    ext.scheduleConvoQuotaRefresh(20);
                    await new Promise((r) => setTimeout(r, 40));
                    vscodeMock.window.state.focused = true;
                    windowStateHandler({{ focused: true }});
                    await new Promise((r) => setTimeout(r, 30));

                    // 5. refreshNetworkConnection probes network, calls antigravity.reconnect, and shows toast
                    executedCommands.length = 0;
                    shownMessages.length = 0;
                    await ext.refreshNetworkConnection();
                    assert.ok(executedCommands.some((c) => c.cmd === 'antigravity.reconnect'));
                    assert.ok(shownMessages.some((m) => String(m.msg).includes('Сеть обновлена:')));

                    ext.deactivate();
                    console.log(JSON.stringify({{ ok: true }}));
                    process.exit(0);
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script)
            self.assertTrue(res["ok"])

    def test_onboarding_sqlite_sync_and_5h_modal_no_clamp(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            gemini_dir = Path(tmp) / "gemini"
            appdata_dir = Path(tmp) / "appdata"
            acc_dir = tools_dir / "accounts"
            ide_gs = appdata_dir / "Antigravity IDE" / "User" / "globalStorage"
            acc_dir.mkdir(parents=True, exist_ok=True)
            gemini_dir.mkdir(parents=True, exist_ok=True)
            ide_gs.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CORE_DIR / "quota_engine.js", tools_dir / "quota_engine.js")

            db_path = ide_gs / "state.vscdb"
            con = sqlite3.connect(str(db_path))
            con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
            con.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", ("jetski.onboarding.lastLoginUsername", "old@example.com"))
            con.commit()
            con.close()

            groups_5h_primary = [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.57, "resetTime": "2099-01-08T00:00:00Z"},
                        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.71, "resetTime": "2099-01-01T05:00:00Z"},
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "claude-3p-weekly", "window": "weekly", "remainingFraction": 0.0, "resetTime": "2099-01-08T00:00:00Z"},
                        {"bucketId": "claude-3p-5h", "window": "5h", "remainingFraction": 1.0, "resetTime": "2099-01-01T05:00:00Z"},
                    ],
                },
            ]
            (acc_dir / "acc_ide.json").write_text(
                json.dumps({
                    "id": "acc_ide",
                    "email": "ide5h@example.com",
                    "name": "IDE 5h User",
                    "token": {"access_token": "ya29.5h", "refresh_token": "1//05h", "expiry_timestamp": 2_500_000_000},
                    "quota": {
                        "subscription_tier": "PRO",
                        "uses_gcp_tos": False,
                        "quota_groups": groups_5h_primary,
                        "models": [
                            {"name": "gemini-3.8-flash", "display_name": "Gemini 3.8 Flash", "percentage": 57, "reset_time": "2099-01-08T00:00:00Z"},
                            {"name": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6 (Thinking)", "percentage": 0, "reset_time": "2099-01-08T00:00:00Z"},
                        ],
                        "last_updated": 1_900_000_000,
                    },
                }),
                encoding="utf-8",
            )
            (tools_dir / "accounts.json").write_text(
                json.dumps({"current_ide_account_id": "acc_ide", "accounts": [{"id": "acc_ide", "email": "ide5h@example.com", "name": "IDE 5h User"}]}),
                encoding="utf-8",
            )

            script = textwrap.dedent(f"""
                const assert = require('assert');
                const fs = require('fs');
                const path = require('path');
                const Module = require('module');

                process.env.AG_TOOLS_DIR = {json.dumps(str(tools_dir))};
                process.env.AG_GEMINI_DIR = {json.dumps(str(gemini_dir))};
                process.env.APPDATA = {json.dumps(str(appdata_dir))};

                const shownMessages = [];
                const vscodeMock = {{
                    env: {{ appName: 'Antigravity' }},
                    StatusBarAlignment: {{ Right: 2 }},
                    ProgressLocation: {{ Notification: 15 }},
                    Uri: {{ file: (p) => ({{ fsPath: p, toString: () => 'file://' + p }}) }},
                    ThemeIcon: class {{ constructor(id) {{ this.id = id; }} }},
                    MarkdownString: class {{
                        constructor() {{ this.value = ''; }}
                        appendMarkdown(s) {{ this.value += s; }}
                    }},
                    workspace: {{ getConfiguration: () => ({{ get: () => 15 }}), workspaceFolders: [] }},
                    extensions: {{ getExtension: () => null }},
                    commands: {{ registerCommand: () => ({{ dispose() {{}} }}), executeCommand: async () => {{}} }},
                    window: {{
                        state: {{ focused: true }},
                        createStatusBarItem: () => ({{ text: '', tooltip: null, show() {{}}, dispose() {{}} }}),
                        onDidChangeWindowState: () => ({{ dispose() {{}} }}),
                        showInformationMessage: async (msg, opts) => {{ shownMessages.push({{ msg, opts }}); return null; }}
                    }}
                }};

                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === 'vscode') return vscodeMock;
                    return origLoad.call(this, request, parent, isMain);
                }};

                const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});
                (async () => {{
                    ext._resetStateForTests();
                    // 1. Real SQLite onboarding sync writes both top-level and google.google-antigravity JSON
                    ext.ensureOnboardingCompleted('ide5h@example.com', false);
                    const marker = path.join({json.dumps(str(tools_dir))}, '.onboarding_synced');
                    for (let i = 0; i < 40; i++) {{
                        if (fs.existsSync(marker)) break;
                        await new Promise((r) => setTimeout(r, 75));
                    }}
                    assert.ok(fs.existsSync(marker), 'Expected .onboarding_synced marker to be written');

                    // 2. showQuotaDetails uses 5h primary (71%) without min(rawPct, effectiveGeminiPct) clamping (57%)
                    await ext.showQuotaDetails();
                    assert.strictEqual(shownMessages.length, 1);
                    const modalMsg = shownMessages[0].msg;
                    assert.ok(modalMsg.includes('Gemini 3.8 Flash: 71%'), modalMsg);
                    assert.ok(!modalMsg.includes('Gemini 3.8 Flash: 57%'), modalMsg);
                    assert.ok(!modalMsg.includes('Gemini 3.8 Flash: 71% ⏳'), modalMsg);
                    assert.ok(modalMsg.includes('─────────────────────────'), modalMsg);
                    assert.ok(modalMsg.includes('(окна лимитов):'), modalMsg);
                    assert.ok(!modalMsg.includes('(итог:'), modalMsg);
                    assert.ok(modalMsg.includes('\\u00A0\\u00A0\\u00A0• 5ч лимит: 71%'), modalMsg);
                    assert.ok(modalMsg.includes('\\u00A0\\u00A0\\u00A0• Недельный лимит: 57%'), modalMsg);
                    assert.ok(!modalMsg.includes('((доступно 100%))'), modalMsg);

                    ext.deactivate();
                    console.log(JSON.stringify({{ ok: true }}));
                    process.exit(0);
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script)
            self.assertTrue(res["ok"])

            con = sqlite3.connect(str(db_path))
            kv = dict(con.execute("SELECT key, value FROM ItemTable").fetchall())
            con.close()
            self.assertEqual(kv["agy.onboarding.lastLoginUsername"], "ide5h@example.com")
            self.assertEqual(kv["agy.onboarding.lastLoginIsGcpTos"], "false")
            self.assertEqual(kv["agy.onboarding.welcomeModalSeen"], "true")
            self.assertEqual(kv["jetski.onboarding.lastLoginUsername"], "ide5h@example.com")
            self.assertEqual(kv["jetski.onboarding.lastLoginIsGcpTos"], "false")
            ext_json = json.loads(kv["google.google-antigravity"])
            self.assertIs(ext_json["antigravity.accountTelemetryEnabled"], True)
            self.assertIs(ext_json["antigravity.legacyTelemetrySettingNoticeShown"], True)
            self.assertEqual(ext_json["antigravity-storage"]["agy.onboarding.lastLoginUsername"], "ide5h@example.com")
            self.assertEqual(ext_json["antigravity-storage"]["agy.onboarding.lastLoginIsGcpTos"], "false")
            self.assertEqual(ext_json["antigravity-storage"]["jetski.onboarding.lastLoginUsername"], "ide5h@example.com")

    def test_ide_modal_parity_formatting_and_always_live_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            gemini_dir = Path(tmp) / "gemini"
            acc_dir = tools_dir / "accounts"
            tools_dir.mkdir(parents=True, exist_ok=True)
            gemini_dir.mkdir(parents=True, exist_ok=True)
            acc_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CORE_DIR / "quota_engine.js", tools_dir / "quota_engine.js")

            now_sec = int(time.time())
            stale_groups = [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.44, "resetTime": "2099-05-15T18:53:00Z"},
                        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.26, "resetTime": "2099-05-10T23:59:00Z"},
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "claude-3p-weekly", "window": "weekly", "remainingFraction": 0.0, "resetTime": "2099-05-15T21:32:00Z"},
                        {"bucketId": "claude-3p-5h", "window": "5h", "remainingFraction": 0.0, "resetTime": "2099-05-10T22:36:00Z"},
                    ],
                },
            ]
            (acc_dir / "acc_sync.json").write_text(
                json.dumps({
                    "id": "acc_sync",
                    "email": "sync@example.com",
                    "name": "Sync User",
                    "token": {"access_token": "ya29.sync", "refresh_token": "1//0sync", "expiry_timestamp": 2_500_000_000},
                    "quota": {
                        "subscription_tier": "PRO",
                        "uses_gcp_tos": False,
                        "quota_groups": stale_groups,
                        "last_updated": now_sec,
                    },
                }),
                encoding="utf-8",
            )
            (tools_dir / "accounts.json").write_text(
                json.dumps({"current_ide_account_id": "acc_sync", "accounts": [{"id": "acc_sync", "email": "sync@example.com", "name": "Sync User"}]}),
                encoding="utf-8",
            )
            # Recent shared poll state (1 second ago) must NOT stop IDE modal from live refreshing!
            (tools_dir / ".quota_poll_state.json").write_text(
                json.dumps({"last_active_refresh_by_target": {"ide": now_sec}}),
                encoding="utf-8",
            )

            script = textwrap.dedent(f"""
                const assert = require('assert');
                const Module = require('module');
                const {{ EventEmitter }} = require('events');
                const https = require('https');

                process.env.AG_TOOLS_DIR = {json.dumps(str(tools_dir))};
                process.env.AG_GEMINI_DIR = {json.dumps(str(gemini_dir))};

                const shownMessages = [];
                let quotaApiCalls = 0;
                const near51mIso = new Date(Date.now() + 51 * 60 * 1000 + 20 * 1000).toISOString();
                const liveGroups = [
                    {{
                        displayName: 'Gemini Models',
                        buckets: [
                            {{ bucketId: 'gemini-weekly', window: 'weekly', remainingFraction: 0.44, resetTime: '2099-05-15T18:53:00Z' }},
                            {{ bucketId: 'gemini-5h', window: '5h', remainingFraction: 0.27, resetTime: near51mIso }}
                        ]
                    }},
                    {{
                        displayName: 'Claude and GPT models',
                        buckets: [
                            {{ bucketId: 'claude-3p-weekly', window: 'weekly', remainingFraction: 0.0, resetTime: '2099-05-15T21:32:00Z' }},
                            {{ bucketId: 'claude-3p-5h', window: '5h', remainingFraction: 0.0, resetTime: near51mIso }}
                        ]
                    }}
                ];

                https.request = (opts, cb) => {{
                    const req = new EventEmitter();
                    req.write = () => {{}};
                    req.destroy = () => {{}};
                    req.end = () => {{
                        const res = new EventEmitter();
                        res.statusCode = 200;
                        res.setEncoding = () => {{}};
                        process.nextTick(() => {{
                            cb(res);
                            if ((opts.path || '').includes('retrieveUserQuotaSummary')) {{
                                quotaApiCalls++;
                                res.emit('data', JSON.stringify({{ groups: liveGroups }}));
                            }} else if ((opts.hostname || '').includes('cloudflare')) {{
                                res.emit('data', 'loc=DE\\n');
                            }} else {{
                                res.emit('data', '{{}}');
                            }}
                            res.emit('end');
                        }});
                    }};
                    return req;
                }};

                const vscodeMock = {{
                    env: {{ appName: 'Antigravity' }},
                    StatusBarAlignment: {{ Right: 2 }},
                    ProgressLocation: {{ Notification: 15 }},
                    Uri: {{ file: (p) => ({{ fsPath: p, toString: () => 'file://' + p }}) }},
                    ThemeIcon: class {{ constructor(id) {{ this.id = id; }} }},
                    MarkdownString: class {{ constructor() {{ this.value = ''; }} appendMarkdown(s) {{ this.value += s; }} }},
                    workspace: {{ getConfiguration: () => ({{ get: () => 15 }}), workspaceFolders: [] }},
                    extensions: {{ getExtension: () => null }},
                    commands: {{ registerCommand: () => ({{ dispose() {{}} }}), executeCommand: async () => {{}} }},
                    window: {{
                        state: {{ focused: true }},
                        createStatusBarItem: () => ({{ text: '', tooltip: null, show() {{}}, dispose() {{}} }}),
                        onDidChangeWindowState: () => ({{ dispose() {{}} }}),
                        showInformationMessage: async (msg, opts) => {{ shownMessages.push({{ msg, opts }}); return null; }}
                    }}
                }};
                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === 'vscode') return vscodeMock;
                    return origLoad.call(this, request, parent, isMain);
                }};

                const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});
                (async () => {{
                    ext._resetStateForTests();
                    await ext.showQuotaDetails();
                    assert.strictEqual(
                        quotaApiCalls,
                        1,
                        'Expected modal to force live refresh even when last_updated was 0s old (shown=' + JSON.stringify(shownMessages) + ')'
                    );
                    assert.strictEqual(shownMessages.length, 1);
                    const msg = shownMessages[0].msg;
                    assert.ok(msg.includes('Gemini 3.8 Flash: 27% сброс через '), msg);
                    assert.ok(!msg.includes('Gemini 3.8 Flash: 27%  '), msg);
                    assert.ok(!msg.includes('Gemini 3.8 Flash: 26%'), msg);
                    assert.ok(msg.includes('0 ч. 51 мин.'), msg);
                    assert.ok(msg.includes('мая'), msg);
                    assert.ok(msg.includes('Claude Sonnet 4.6 (Thinking): 0% ⏳ сброс через '), msg);
                    assert.ok(msg.includes('\\u00A0\\u00A0\\u00A0• 5ч лимит: 27% (сброс через '), msg);
                    assert.ok(!msg.includes('• 5ч лимит: 27% (⏳'), msg);
                    ext.deactivate();
                    console.log(JSON.stringify({{ ok: true }}));
                    process.exit(0);
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script)
            self.assertTrue(res["ok"])


class DesktopUiTests(unittest.TestCase):
    def test_desktop_ui_quota_breakdown_and_network_toast(self):
        groups_json = json.dumps(_make_sample_groups())
        script = textwrap.dedent(f"""
            const assert = require('assert');

            function makeEl(tag, id = '') {{
                const el = {{
                    tagName: String(tag || 'div').toUpperCase(),
                    nodeType: 1,
                    id,
                    style: {{ display: 'none', cssText: '', setProperty() {{}}, getPropertyValue() {{ return ''; }} }},
                    dataset: {{}},
                    classList: {{
                        _set: new Set(),
                        add(c) {{ this._set.add(c); }},
                        remove(c) {{ this._set.delete(c); }},
                        toggle(c, force) {{ force ? this._set.add(c) : this._set.delete(c); }},
                        contains(c) {{ return this._set.has(c); }}
                    }},
                    children: [],
                    isConnected: true,
                    innerHTML: '',
                    childElementCount: 0,
                    getBoundingClientRect: () => ({{ left: 100, top: 700, right: 260, bottom: 724, width: 160, height: 24 }}),
                    getClientRects: () => [{{}}],
                    appendChild(child) {{ child.parentElement = this; this.children.push(child); this.childElementCount = this.children.length; return child; }}
                }};
                return el;
            }}

            const elsById = new Map();
            const bodyEl = makeEl('body', '');
            const headEl = makeEl('head', '');
            global.window = {{
                __ag_skip_auto_start: true,
                innerWidth: 1280,
                innerHeight: 800,
                addEventListener() {{}},
                getComputedStyle: () => ({{ display: 'block', visibility: 'visible', opacity: '1', overflow: 'visible' }}),
                agAPI: {{
                    getState: async () => ({{
                        ok: true,
                        activeAccount: {{
                            id: 'acc_dt',
                            name: 'Desktop User',
                            email: 'dt@example.com',
                            tier: 'PRO',
                            quota: {{ subscription_tier: 'PRO', quota_groups: {groups_json} }}
                        }},
                        accounts: [],
                        country: 'Германия',
                        countryCode: 'DE',
                        ping: 42,
                        networkOk: true,
                        proxyUrl: 'http://127.0.0.1:7890'
                    }}),
                    refresh: async (mode) => {{
                        if (mode === 'network') {{
                            return {{
                                ok: true,
                                network: {{ ok: true, ms: 42, countryCode: 'DE', country: 'Германия', status: '42ms', proxyUrl: 'http://127.0.0.1:7890' }},
                                backendRestarted: true,
                                backendNote: 'Агент (LS) и сокеты перезапущены',
                                activeAccount: {{
                                    id: 'acc_dt',
                                    name: 'Desktop User',
                                    email: 'dt@example.com',
                                    tier: 'PRO',
                                    quota: {{ subscription_tier: 'PRO', quota_groups: {groups_json} }}
                                }}
                            }};
                        }}
                        return {{ ok: true }};
                    }},
                    onStateUpdated: () => () => {{}}
                }}
            }};
            global.document = {{
                body: bodyEl,
                head: headEl,
                documentElement: bodyEl,
                hidden: false,
                readyState: 'complete',
                addEventListener() {{}},
                createElement(tag) {{
                    const el = makeEl(tag, '');
                    Object.defineProperty(el, 'id', {{
                        get() {{ return this._id || ''; }},
                        set(v) {{ this._id = v; if (v) elsById.set(v, this); }}
                    }});
                    return el;
                }},
                getElementById(id) {{ return elsById.get(id) || null; }},
                querySelectorAll() {{ return []; }}
            }};
            global.localStorage = {{ getItem: () => null, setItem() {{}} }};

            const ui = require({json.dumps(str(DESKTOP_DIR / 'ag_desktop_ui.js'))});

            (async () => {{
                const info = ui.getLimitingQuotaInfo({{
                    quota: {{ quota_groups: {groups_json} }}
                }});
                assert.strictEqual(info.geminiPct, 16);
                assert.strictEqual(info.geminiWeeklyPct, 66);
                assert.strictEqual(info.gemini5hPct, 16);
                assert.strictEqual(info.claudePct, 0);
                assert.strictEqual(info.claudeWeeklyPct, 0);
                assert.strictEqual(info.claude5hPct, null);

                const tt = document.createElement('div');
                tt.id = 'ag-ide-tooltip';
                const backdrop = document.createElement('div');
                backdrop.id = 'ag-quota-modal-backdrop';
                ui.setTestState({{
                    account: {{
                        id: 'acc_dt',
                        name: 'Desktop User',
                        email: 'dt@example.com',
                        tier: 'PRO',
                        quota: {{ subscription_tier: 'PRO', quota_groups: {groups_json} }}
                    }},
                    net: {{ ok: true, ms: 42, countryCode: 'DE', country: 'Германия', proxyUrl: 'http://127.0.0.1:7890' }}
                }});

                ui.renderTooltipContent(tt);
                assert.ok(tt.innerHTML.includes('5ч лимит: <strong style="color:#d4d4d4;">16%</strong>'), tt.innerHTML);
                assert.ok(tt.innerHTML.includes('Недельный: <strong style="color:#d4d4d4;">66%</strong>'), tt.innerHTML);
                assert.ok(!tt.innerHTML.includes('5ч лимит: <strong style="color:#d4d4d4;">100%</strong>'), tt.innerHTML);

                ui.renderQuotaModalContent();
                assert.ok(backdrop.innerHTML.includes('Gemini (Hydrogen / Flash / Pro): 16%'), backdrop.innerHTML);
                assert.ok(backdrop.innerHTML.includes('Детализация окон (5ч / Неделя)'), backdrop.innerHTML);
                assert.ok(backdrop.innerHTML.includes('5ч лимит: <strong style="color:#e5e7eb;">16%</strong>'), backdrop.innerHTML);
                assert.ok(backdrop.innerHTML.includes('Недельный лимит: <strong style="color:#e5e7eb;">66%</strong>'), backdrop.innerHTML);

                await ui.refreshNetwork();
                const toast = document.getElementById('ag-desktop-toast');
                assert.ok(toast && toast.innerHTML.includes('Сеть обновлена!'));
                assert.ok(toast.innerHTML.includes('Германия'));
                assert.ok(toast.innerHTML.includes('http://127.0.0.1:7890'));
                assert.ok(toast.innerHTML.includes('Сервер агента (LS) и сокеты сессии перезапущены'));

                console.log(JSON.stringify({{ ok: true }}));
                process.exit(0);
            }})().catch((err) => {{
                console.error(err);
                process.exit(1);
            }});
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])

    def test_desktop_ui_5h_primary_and_dismiss_onboarding_modals_safely(self):
        script = textwrap.dedent(f"""
            const assert = require('assert');

            function makeEl(tag, opts = {{}}) {{
                const el = {{
                    tagName: String(tag || 'div').toUpperCase(),
                    nodeType: 1,
                    id: opts.id || '',
                    textContent: opts.textContent || '',
                    disabled: Boolean(opts.disabled),
                    attrs: Object.assign({{}}, opts.attrs || {{}}),
                    style: {{ display: 'block', cssText: '', setProperty() {{}}, getPropertyValue() {{ return ''; }} }},
                    parentElement: opts.parentElement || null,
                    isConnected: true,
                    hidden: false,
                    clicks: 0,
                    _children: [],
                    _headings: [],
                    getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }},
                    click() {{ this.clicks++; }},
                    querySelectorAll(sel) {{
                        if (sel === 'button, [role="button"]') return this._children;
                        return [];
                    }},
                    getBoundingClientRect: () => ({{ left: 100, top: 100, right: 400, bottom: 300, width: 300, height: 200 }}),
                    getClientRects: () => [{{}}]
                }};
                return el;
            }}

            const dialogs = [];
            const headings = [];
            const bodyEl = makeEl('body');
            global.window = {{
                __ag_skip_auto_start: true,
                innerWidth: 1280,
                innerHeight: 800,
                addEventListener() {{}},
                getComputedStyle: () => ({{ display: 'block', visibility: 'visible', opacity: '1', overflow: 'visible' }})
            }};
            global.document = {{
                body: bodyEl,
                documentElement: bodyEl,
                hidden: false,
                readyState: 'complete',
                addEventListener() {{}},
                querySelectorAll(sel) {{
                    if (sel === '[role="dialog"], dialog') return dialogs;
                    if (sel === 'h1, h2, h3') return headings;
                    return [];
                }}
            }};
            global.localStorage = {{ getItem: () => null, setItem() {{}} }};

            const ui = require({json.dumps(str(DESKTOP_DIR / 'ag_desktop_ui.js'))});

            // 1. 5h primary rule: weekly=57, 5h=71 -> geminiPct=71; weekly=0 -> claudePct=0
            const qInfo = ui.getLimitingQuotaInfo({{
                quota: {{
                    quota_groups: [
                        {{
                            displayName: 'Gemini Models',
                            buckets: [
                                {{ bucketId: 'gemini-weekly', window: 'weekly', remainingFraction: 0.57 }},
                                {{ bucketId: 'gemini-5h', window: '5h', remainingFraction: 0.71 }}
                            ]
                        }},
                        {{
                            displayName: 'Claude and GPT models',
                            buckets: [
                                {{ bucketId: 'claude-3p-weekly', window: 'weekly', remainingFraction: 0 }},
                                {{ bucketId: 'claude-3p-5h', window: '5h', remainingFraction: 1 }}
                            ]
                        }}
                    ]
                }}
            }});
            assert.strictEqual(qInfo.geminiPct, 71);
            assert.strictEqual(qInfo.claudePct, 0);

            // 2. Onboarding modal dismissal: clicks Continue & Accept and continue (even inside plain card without role="dialog")
            const onboardingDlg = makeEl('div', {{ textContent: 'One last thing before getting started... Terms of Service', parentElement: bodyEl }});
            const continueBtn = makeEl('button', {{ textContent: 'Continue', parentElement: onboardingDlg }});
            onboardingDlg._children.push(continueBtn);

            const secCard = makeEl('div', {{ textContent: 'Security Notice & Data Use AI coding agents...', parentElement: bodyEl }});
            const secHeading = makeEl('h2', {{ textContent: 'Security Notice & Data Use', parentElement: secCard }});
            const secBtn = makeEl('button', {{ textContent: 'Accept and continue', parentElement: secCard }});
            secCard._children.push(secBtn);
            headings.push(secHeading);

            const ownBackdrop = makeEl('div', {{ id: 'ag-quota-modal-backdrop', parentElement: bodyEl }});
            const ownDlg = makeEl('div', {{ textContent: 'One last thing before getting started...', parentElement: ownBackdrop }});
            const ownOk = makeEl('button', {{ textContent: 'Continue', parentElement: ownDlg }});
            ownDlg._children.push(ownOk);

            const disabledDlg = makeEl('div', {{ textContent: 'Security Notice & Data Use', parentElement: bodyEl }});
            const disabledBtn = makeEl('button', {{ textContent: 'Accept', disabled: true, parentElement: disabledDlg }});
            disabledDlg._children.push(disabledBtn);

            dialogs.push(onboardingDlg, ownDlg, disabledDlg);

            assert.strictEqual(ui.dismissOnboardingModalsSafely(), true);
            assert.strictEqual(continueBtn.clicks, 1);
            assert.strictEqual(secBtn.clicks, 1);
            assert.strictEqual(ownOk.clicks, 0);
            assert.strictEqual(disabledBtn.clicks, 0);

            // WeakSet guard skips same DOM nodes on subsequent tick
            assert.strictEqual(ui.dismissOnboardingModalsSafely(), false);
            assert.strictEqual(continueBtn.clicks, 1);
            assert.strictEqual(secBtn.clicks, 1);

            console.log(JSON.stringify({{ ok: true }}));
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])

    def test_desktop_ui_force_refresh_object_and_switch_error_rollback(self):
        script = textwrap.dedent(f"""
            const assert = require('assert');

            function makeEl(tag, id = '') {{
                const el = {{
                    tagName: String(tag || 'div').toUpperCase(),
                    nodeType: 1,
                    id,
                    style: {{ display: 'none', cssText: '', setProperty() {{}}, getPropertyValue() {{ return ''; }} }},
                    dataset: {{}},
                    classList: {{
                        _set: new Set(),
                        add(c) {{ this._set.add(c); }},
                        remove(c) {{ this._set.delete(c); }},
                        toggle(c, force) {{ force ? this._set.add(c) : this._set.delete(c); }},
                        contains(c) {{ return this._set.has(c); }}
                    }},
                    children: [],
                    isConnected: true,
                    innerHTML: '',
                    childElementCount: 0,
                    getBoundingClientRect: () => ({{ left: 100, top: 700, right: 260, bottom: 724, width: 160, height: 24 }}),
                    getClientRects: () => [{{}}],
                    appendChild(child) {{ child.parentElement = this; this.children.push(child); this.childElementCount = this.children.length; return child; }}
                }};
                return el;
            }}

            const elsById = new Map();
            const bodyEl = makeEl('body', '');
            const headEl = makeEl('head', '');
            const refreshPayloads = [];
            let getStateCalls = 0;

            global.window = {{
                __ag_skip_auto_start: true,
                innerWidth: 1280,
                innerHeight: 800,
                addEventListener() {{}},
                getComputedStyle: () => ({{ display: 'block', visibility: 'visible', opacity: '1', overflow: 'visible' }}),
                agAPI: {{
                    getState: async () => {{
                        getStateCalls++;
                        return {{
                            ok: true,
                            activeAccount: {{ id: 'acc_keep', name: 'Kept User', email: 'keep@example.com', tier: 'PRO', quota: {{}} }},
                            accounts: [],
                            country: 'Германия',
                            countryCode: 'DE',
                            ping: 55,
                            networkOk: true
                        }};
                    }},
                    refresh: async (payload) => {{
                        refreshPayloads.push(payload);
                        await new Promise((r) => setTimeout(r, 30));
                        return {{ ok: true }};
                    }},
                    switchAccount: async () => ({{ ok: false, error: 'Simulated switch error' }}),
                    onStateUpdated: () => () => {{}}
                }}
            }};
            global.document = {{
                body: bodyEl,
                head: headEl,
                documentElement: bodyEl,
                hidden: false,
                readyState: 'complete',
                addEventListener() {{}},
                createElement(tag) {{
                    const el = makeEl(tag, '');
                    Object.defineProperty(el, 'id', {{
                        get() {{ return this._id || ''; }},
                        set(v) {{ this._id = v; if (v) elsById.set(v, this); }}
                    }});
                    return el;
                }},
                getElementById(id) {{ return elsById.get(id) || null; }},
                querySelectorAll() {{ return []; }}
            }};
            global.localStorage = {{ getItem: () => null, setItem() {{}} }};

            const ui = require({json.dumps(str(DESKTOP_DIR / 'ag_desktop_ui.js'))});

            (async () => {{
                // 1. Force active refresh chains after in-flight non-forced refresh and passes force object payload
                const pBg = ui.triggerLiveRefresh('active', false);
                const pForce = ui.triggerLiveRefresh('active', true);
                await Promise.all([pBg, pForce]);
                assert.strictEqual(refreshPayloads.length, 2);
                assert.strictEqual(refreshPayloads[0], 'active');
                assert.deepStrictEqual(refreshPayloads[1], {{ mode: 'active', force: true, minAgeSec: 0 }});

                // 2. Modal does not wrap (доступно 100%) twice -> no ((доступно 100%))
                const backdrop = document.createElement('div');
                backdrop.id = 'ag-quota-modal-backdrop';
                ui.setTestState({{
                    account: {{
                        id: 'acc_100',
                        name: 'Full User',
                        email: 'full@example.com',
                        tier: 'PRO',
                        quota: {{
                            quota_groups: [
                                {{
                                    displayName: 'Gemini Models',
                                    buckets: [
                                        {{ bucketId: 'gemini-weekly', window: 'weekly', remainingFraction: 1.0 }},
                                        {{ bucketId: 'gemini-5h', window: '5h', remainingFraction: 1.0 }}
                                    ]
                                }}
                            ]
                        }}
                    }}
                }});
                ui.renderQuotaModalContent();
                assert.ok(backdrop.innerHTML.includes('(доступно 100%)'), backdrop.innerHTML);
                assert.ok(!backdrop.innerHTML.includes('((доступно 100%))'), backdrop.innerHTML);

                // 3. switchAccount error displays toast and rolls back pill via getState()
                const pill = document.createElement('div');
                pill.id = 'ag-status-pill';
                getStateCalls = 0;
                await ui.switchAccount('bad_target');
                assert.ok(getStateCalls >= 1);
                const toast = document.getElementById('ag-desktop-toast');
                assert.ok(toast && toast.innerHTML.includes('Simulated switch error'), toast && toast.innerHTML);
                assert.ok(pill.innerHTML.includes('Kept User'), pill.innerHTML);

                // 4. Restarting switchAccount displays restart toast/pill status
                window.agAPI.switchAccount = async () => ({{ ok: true, name: 'Next User', email: 'next@example.com', restarting: true }});
                await ui.switchAccount('acc_next');
                assert.ok(pill.innerHTML.includes('Перезапуск...'), pill.innerHTML);
                assert.ok(toast.innerHTML.includes('Перезапуск Antigravity 2.0'), toast.innerHTML);

                console.log(JSON.stringify({{ ok: true }}));
                process.exit(0);
            }})().catch((err) => {{
                console.error(err);
                process.exit(1);
            }});
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])

    def test_desktop_ui_swarm_menu_modal_chat_injection_and_watcher(self):
        script = textwrap.dedent(f"""
            const assert = require('assert');

            function makeEl(tag, id = '') {{
                const classes = new Set();
                const attrs = new Map();
                const el = {{
                    tagName: tag.toUpperCase(),
                    _id: id,
                    style: {{}},
                    dataset: {{}},
                    disabled: false,
                    readOnly: false,
                    type: '',
                    placeholder: '',
                    value: '',
                    checked: false,
                    children: [],
                    parentElement: null,
                    isConnected: true,
                    _html: '',
                    get innerHTML() {{ return this._html; }},
                    set innerHTML(htmlStr) {{
                        this._html = String(htmlStr || '');
                        const idRx = /<([a-z0-9]+)\\b([^>]*?)\\bid="([^"]+)"([^>]*)>/gi;
                        let m;
                        while ((m = idRx.exec(this._html)) !== null) {{
                            const child = makeEl(m[1], m[3]);
                            const attrsStr = (m[2] || '') + ' ' + (m[4] || '');
                            if (/\\bchecked\\b/i.test(attrsStr)) child.checked = true;
                            child.parentElement = this;
                            elsById.set(m[3], child);
                        }}
                    }},
                    textContent: '',
                    innerText: '',
                    childElementCount: 0,
                    classList: {{
                        add: (...c) => c.forEach(x => classes.add(x)),
                        remove: (...c) => c.forEach(x => classes.delete(x)),
                        toggle: (c, force) => force === undefined
                            ? (classes.has(c) ? classes.delete(c) : classes.add(c))
                            : (force ? classes.add(c) : classes.delete(c)),
                        contains: (c) => classes.has(c)
                    }},
                    setAttribute(k, v) {{ attrs.set(k, String(v)); }},
                    getAttribute(k) {{
                        if (k === 'class') return Array.from(classes).join(' ');
                        return attrs.has(k) ? attrs.get(k) : null;
                    }},
                    remove() {{
                        if (this.parentElement) {{
                            this.parentElement.children = this.parentElement.children.filter(c => c !== this);
                            this.parentElement.childElementCount = this.parentElement.children.length;
                        }}
                        this.isConnected = false;
                    }},
                    dispatchEvent() {{}},
                    focus() {{}},
                    click() {{ if (typeof this.onclick === 'function') this.onclick({{ target: this, stopPropagation() {{}} }}); }},
                    getBoundingClientRect: () => ({{ left: 100, top: 680, right: 560, bottom: 740, width: 460, height: 60 }}),
                    getClientRects: () => [{{}}],
                    appendChild(child) {{
                        child.parentElement = this;
                        this.children.push(child);
                        this.childElementCount = this.children.length;
                        return child;
                    }},
                    querySelectorAll(sel) {{
                        if (sel === '.ag-qp-row') {{
                            const actions = [];
                            const rx = /data-action="([^"]+)"/g;
                            let m;
                            while ((m = rx.exec(this.innerHTML)) !== null) {{
                                const a = m[1];
                                actions.push({{ getAttribute: (k) => k === 'data-action' ? a : null, onclick: null }});
                            }}
                            return actions;
                        }}
                        if (sel === '[data-action="back_to_main"]' || sel === '[data-action="open_new_swarm"]') {{
                            return [];
                        }}
                        if (sel === '[data-cleanup-task]') {{
                            const btns = [];
                            const rx = /data-cleanup-task="([^"]+)"/g;
                            let m;
                            while ((m = rx.exec(this.innerHTML)) !== null) {{
                                const idVal = m[1];
                                btns.push({{ getAttribute: (k) => k === 'data-cleanup-task' ? idVal : null, onclick: null }});
                            }}
                            return btns;
                        }}
                        if (sel === '[data-swarm-task]') {{
                            const rows = [];
                            const rx = /data-swarm-task="([^"]+)"/g;
                            let m;
                            while ((m = rx.exec(this.innerHTML)) !== null) {{
                                const idVal = m[1];
                                rows.push({{ getAttribute: (k) => k === 'data-swarm-task' ? idVal : null, onclick: null }});
                            }}
                            return rows;
                        }}
                        if (sel === 'button') {{
                            return this.children.filter(c => c.tagName === 'BUTTON');
                        }}
                        return [];
                    }},
                    querySelector(sel) {{
                        const list = this.querySelectorAll(sel);
                        return list.length ? list[0] : null;
                    }}
                }};
                return el;
            }}

            const elsById = new Map();
            const bodyEl = makeEl('body');
            const headEl = makeEl('head');
            const composerWrap = makeEl('div');
            const chatInput = makeEl('textarea');
            chatInput.placeholder = 'Ask anything, @ to mention, / for actions';
            const sendBtn = makeEl('button');
            sendBtn.type = 'submit';
            let sendClicks = 0;
            sendBtn.click = () => {{ sendClicks++; }};
            composerWrap.appendChild(chatInput);
            composerWrap.appendChild(sendBtn);
            bodyEl.appendChild(composerWrap);

            const swarmCalls = [];
            let checkpointList = [
                {{
                    task_id: 'swarm-demo-1',
                    project_path: 'D:/repo/demo',
                    project_name: 'demo',
                    user_request: 'Refactor auth flow cleanly',
                    current_node: 'implement',
                    stage_label: 'Агенты работают',
                    current_phase: 0,
                    total_phases: 2,
                    is_complete: false,
                    done_workers: 1,
                    running_workers: 1,
                    total_workers: 3,
                    orchestrator_model: 'claude-sonnet-4',
                    worker_models: ['flash'],
                    workers: [
                        {{ name: 'worker-1', status: 'done', model: 'flash', files: ['auth.js'] }},
                        {{ name: 'worker-2', status: 'running', model: 'flash', files: ['routes.js'] }}
                    ],
                    errors: []
                }}
            ];
            let savedPolicy = 'interactive';
            let savedModelCfg = {{ worker_model: 'flash' }};

            global.window = {{
                __ag_skip_auto_start: true,
                innerWidth: 1280,
                innerHeight: 800,
                addEventListener() {{}},
                getComputedStyle: () => ({{ display: 'block', visibility: 'visible', opacity: '1', overflow: 'visible' }}),
                agAPI: {{
                    getState: async () => ({{ ok: true, activeAccount: {{ id: 'a1', name: 'Test User', email: 'u@example.com', quota: {{}} }}, accounts: [], networkOk: true, ping: 42, countryCode: 'DE' }}),
                    refresh: async () => ({{ ok: true }}),
                    onStateUpdated: () => () => {{}},
                    swarm: async (op, payload) => {{
                        swarmCalls.push({{ op, payload }});
                        if (op === 'list') {{
                            return {{
                                ok: true,
                                checkpoints: checkpointList,
                                enginePath: 'D:/repo/demo/.agents/skills/ag-swarm',
                                projects: [{{ path: 'D:/repo/demo', name: 'demo', isGit: true }}]
                            }};
                        }}
                        if (op === 'prepare') {{
                            return {{
                                ok: true,
                                taskId: 'swarm-new-99',
                                projectPath: payload.projectPath,
                                dispatchPrompt: '/ag-swarm RUN_ID swarm-new-99 -> ' + payload.request,
                                checkpoint: {{
                                    task_id: 'swarm-new-99',
                                    current_node: 'plan',
                                    done_workers: 0,
                                    total_workers: 2,
                                    is_complete: false
                                }}
                            }};
                        }}
                        if (op === 'status') {{
                            return {{
                                ok: true,
                                checkpoint: {{
                                    task_id: payload.taskId,
                                    current_node: 'implement',
                                    done_workers: 1,
                                    total_workers: 2,
                                    is_complete: false
                                }}
                            }};
                        }}
                        if (op === 'get_settings') {{
                            return {{ ok: true, failoverPolicy: savedPolicy, modelConfig: savedModelCfg }};
                        }}
                        if (op === 'set_failover_policy') {{
                            savedPolicy = payload.policy;
                            return {{ ok: true, failoverPolicy: savedPolicy }};
                        }}
                        if (op === 'save_model_config') {{
                            savedModelCfg = payload.config || {{}};
                            return {{ ok: true, modelConfig: savedModelCfg }};
                        }}
                        if (op === 'reset_model_config') {{
                            savedModelCfg = {{}};
                            return {{ ok: true, modelConfig: savedModelCfg }};
                        }}
                        if (op === 'cleanup') {{
                            checkpointList = checkpointList.filter(c => c.task_id !== payload.taskId);
                            return {{ ok: true, taskId: payload.taskId, deletedState: true, checkpoints: checkpointList }};
                        }}
                        return {{ ok: false }};
                    }}
                }}
            }};
            global.document = {{
                body: bodyEl,
                head: headEl,
                documentElement: bodyEl,
                hidden: false,
                readyState: 'complete',
                addEventListener() {{}},
                createElement(tag) {{
                    const el = makeEl(tag, '');
                    Object.defineProperty(el, 'id', {{
                        get() {{ return this._id || ''; }},
                        set(v) {{ this._id = v; if (v) elsById.set(v, this); }}
                    }});
                    return el;
                }},
                getElementById(id) {{ return elsById.get(id) || null; }},
                querySelectorAll(sel) {{
                    if (sel.includes('textarea')) return [chatInput];
                    if (sel.includes('dialog')) return [document.getElementById('ag-swarm-modal-backdrop')].filter(Boolean);
                    return [];
                }}
            }};
            global.localStorage = {{ getItem: () => null, setItem() {{}} }};

            const ui = require({json.dumps(str(DESKTOP_DIR / 'ag_desktop_ui.js'))});

            (async () => {{
                const pill = document.createElement('div');
                pill.id = 'ag-status-pill';
                bodyEl.appendChild(pill);
                ui.setTestState({{
                    account: {{ id: 'a1', name: 'Test User', email: 'u@example.com', quota: {{}} }},
                    net: {{ ok: true, ms: 42, countryCode: 'DE' }}
                }});

                // 1. Main QuickPick menu contains unified swarm_hub, quota_policy, model_config (1:1 parity with IDE)
                ui.openQuickPick();
                const qp = document.getElementById('ag-qp-container');
                assert.ok(qp.innerHTML.includes('data-action="swarm_hub"'));
                assert.ok(qp.innerHTML.includes('data-action="quota_policy"'));
                assert.ok(qp.innerHTML.includes('data-action="model_config"'));
                assert.ok(qp.innerHTML.includes('Режим при исчерпании квоты'));

                // Verify quota_policy submenu works cleanly
                await ui.handleMainAction('quota_policy');
                assert.ok(qp.innerHTML.includes('Спрашивать каждый раз'));
                assert.ok(qp.innerHTML.includes('data-policy-id="account_hop"'));

                // 2. Unified Modal renders live task cards (project, models, workers) + openSwarmCheckpointsMenu cleanup
                await ui.openSwarmCheckpointsMenu();
                assert.ok(qp.innerHTML.includes('demo'), qp.innerHTML);
                assert.ok(qp.innerHTML.includes('Refactor auth flow cleanly'));
                await ui.cleanupDesktopSwarmTask('swarm-demo-1', 'D:/repo/demo');
                assert.ok(!qp.innerHTML.includes('swarm-demo-1'));

                // Restore mock checkpoint to verify live dashboard cards inside openSwarmRunModal
                checkpointList = [{{
                    task_id: 'swarm-live-77',
                    project_path: 'D:/repo/demo',
                    project_name: 'demo',
                    user_request: 'Add login flow and docs',
                    current_node: 'EXECUTE',
                    stage_label: 'Агенты работают',
                    is_complete: false,
                    done_workers: 1,
                    running_workers: 1,
                    total_workers: 2,
                    orchestrator_model: 'claude-opus-4',
                    worker_models: ['flash'],
                    workers: [
                        {{ name: 'worker-1', status: 'done', model: 'flash', files: ['auth.py'] }},
                        {{ name: 'worker-2', status: 'running', model: 'flash', files: ['README.md'] }}
                    ],
                    errors: []
                }}];

                // 3. Open unified Команда агентов modal, verify live card, launch task, verify chat injection + Send + pill watcher + onboarding guard safety
                ui.closeQuickPick();
                await ui.openSwarmRunModal();
                const swarmBackdrop = document.getElementById('ag-swarm-modal-backdrop');
                assert.ok(swarmBackdrop && swarmBackdrop.style.display === 'flex');
                assert.ok(swarmBackdrop.innerHTML.includes('Быстрая проверка: только тесты изменённых файлов'));
                const tasksListEl = document.getElementById('ag-swarm-tasks-list');
                assert.ok(tasksListEl && tasksListEl.innerHTML.includes('claude-opus-4'), tasksListEl && tasksListEl.innerHTML);
                assert.ok(tasksListEl.innerHTML.includes('worker-2'), tasksListEl.innerHTML);

                // Simulate synthetic inner modal child and verify dismissOnboardingModalsSafely keeps it
                const modalChild = makeEl('div');
                modalChild.innerText = 'Select project or close';
                swarmBackdrop.appendChild(modalChild);
                ui.dismissOnboardingModalsSafely();
                assert.strictEqual(swarmBackdrop.style.display, 'flex');

                document.getElementById('ag-swarm-project-input').value = 'D:/repo/demo';
                document.getElementById('ag-swarm-request-input').value = 'Build feature Z';
                document.getElementById('ag-swarm-affected-tests').checked = true;
                const injectResult = await ui.launchSwarmFromModal();
                assert.strictEqual(injectResult.inserted, true);
                assert.strictEqual(injectResult.sent, true);
                assert.strictEqual(sendClicks, 1);
                assert.ok(chatInput.value.includes('swarm-new-99'));
                assert.ok(pill.innerHTML.includes('Команда:'), pill.innerHTML);
                assert.ok(swarmCalls.some(c => c.op === 'prepare' && c.payload.useAffectedTests === true));

                ui.stopDesktopSwarmWatcher();
                console.log(JSON.stringify({{ ok: true }}));
                process.exit(0);
            }})().catch((err) => {{
                console.error(err);
                process.exit(1);
            }});
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])

    def test_desktop_ui_swarm_pill_mounting_tooltip_toast_and_clean_modal(self):
        """Verifies Desktop UI #ag-swarm-pill mounted strictly above #ag-status-pill in bottom footer,
        pill text formatting, hover tooltip, completion green indicator with toast,
        3-level QuickPick navigation, and clean 3-field launch modal without top dashboard."""
        script = textwrap.dedent(f"""
            const assert = require('assert');

            function makeEl(tag, id = '') {{
                const classes = new Set();
                const attrs = new Map();
                const el = {{
                    nodeType: 1,
                    tagName: tag.toUpperCase(),
                    _id: id,
                    style: {{
                        cssText: '',
                        display: 'block',
                        setProperty(k, v) {{ this[k] = v; }},
                        getPropertyValue(k) {{ return this[k] || ''; }}
                    }},
                    dataset: {{}},
                    disabled: false,
                    readOnly: false,
                    type: '',
                    placeholder: '',
                    value: '',
                    checked: false,
                    children: [],
                    parentElement: null,
                    isConnected: true,
                    _html: '',
                    get innerHTML() {{ return this._html; }},
                    set innerHTML(htmlStr) {{
                        this._html = String(htmlStr || '');
                        const idRx = /<([a-z0-9]+)\\b([^>]*?)\\bid="([^"]+)"([^>]*)>/gi;
                        let m;
                        while ((m = idRx.exec(this._html)) !== null) {{
                            const child = makeEl(m[1], m[3]);
                            const attrsStr = (m[2] || '') + ' ' + (m[4] || '');
                            if (/\\bchecked\\b/i.test(attrsStr)) child.checked = true;
                            const sm = /style="([^"]+)"/i.exec(attrsStr);
                            if (sm && sm[1].includes('display:none')) child.style.display = 'none';
                            child.parentElement = this;
                            elsById.set(m[3], child);
                        }}
                    }},
                    textContent: '',
                    innerText: '',
                    get nextElementSibling() {{
                        if (!this.parentElement) return null;
                        const idx = this.parentElement.children.indexOf(this);
                        return idx >= 0 && idx + 1 < this.parentElement.children.length ? this.parentElement.children[idx + 1] : null;
                    }},
                    get nextSibling() {{
                        return this.nextElementSibling;
                    }},
                    get previousElementSibling() {{
                        if (!this.parentElement) return null;
                        const idx = this.parentElement.children.indexOf(this);
                        return idx > 0 ? this.parentElement.children[idx - 1] : null;
                    }},
                    focus() {{}},
                    select() {{}},
                    dispatchEvent() {{}},
                    click() {{ if (typeof this.onclick === 'function') this.onclick({{ target: this, stopPropagation() {{}} }}); }},
                    matches: (sel) => sel.toLowerCase().includes(tag.toLowerCase()),
                    closest: (sel) => sel.toLowerCase().includes(tag.toLowerCase()) ? this : null,
                    getBoundingClientRect: () => ({{ left: 10, top: 700, right: 200, bottom: 740, width: 190, height: 40 }}),
                    getClientRects: () => [{{}}],
                    classList: {{
                        add: (...c) => c.forEach(x => classes.add(x)),
                        remove: (...c) => c.forEach(x => classes.delete(x)),
                        toggle: (c, force) => force === undefined
                            ? (classes.has(c) ? classes.delete(c) : classes.add(c))
                            : (force ? classes.add(c) : classes.delete(c)),
                        contains: (c) => classes.has(c)
                    }},
                    setAttribute(k, v) {{ attrs.set(k, String(v)); }},
                    getAttribute(k) {{ return attrs.get(k) || null; }},
                    insertBefore(newChild, refChild) {{
                        if (newChild.parentElement) {{
                            newChild.parentElement.children = newChild.parentElement.children.filter(c => c !== newChild);
                        }}
                        newChild.parentElement = this;
                        if (!refChild) {{
                            this.children.push(newChild);
                        }} else {{
                            const idx = this.children.indexOf(refChild);
                            if (idx === -1) this.children.push(newChild);
                            else this.children.splice(idx, 0, newChild);
                        }}
                        return newChild;
                    }},
                    appendChild(child) {{
                        return this.insertBefore(child, null);
                    }},
                    remove() {{
                        if (this.parentElement) {{
                            this.parentElement.children = this.parentElement.children.filter(c => c !== this);
                        }}
                        this.isConnected = false;
                    }},
                    querySelectorAll(sel) {{
                        if (sel === '.ag-qp-row') {{
                            const actions = [];
                            const rx = /data-action="([^"]+)"/g;
                            let m;
                            while ((m = rx.exec(this.innerHTML)) !== null) {{
                                const a = m[1];
                                actions.push({{ getAttribute: (k) => k === 'data-action' ? a : null, onclick: null }});
                            }}
                            return actions;
                        }}
                        if (sel.includes('data-action=')) {{
                            const m = /data-action="([^"]+)"/.exec(sel);
                            if (m && this.innerHTML.includes(m[0])) {{
                                return [{{ getAttribute: (k) => k === 'data-action' ? m[1] : null, onclick: null }}];
                            }}
                        }}
                        if (sel === '[data-swarm-task]') {{
                            const rows = [];
                            const rx = /data-swarm-task="([^"]+)"/g;
                            let m;
                            while ((m = rx.exec(this.innerHTML)) !== null) {{
                                const idVal = m[1];
                                rows.push({{ getAttribute: (k) => k === 'data-swarm-task' ? idVal : null, onclick: null }});
                            }}
                            return rows;
                        }}
                        if (sel.includes('textarea')) return [chatInput];
                        return [];
                    }},
                    querySelector(sel) {{
                        const list = this.querySelectorAll(sel);
                        return list.length ? list[0] : null;
                    }}
                }};
                return el;
            }}

            const elsById = new Map();
            const bodyEl = makeEl('body');
            const headEl = makeEl('head');

            const sidebarFooter = makeEl('div', 'sidebar-footer');
            const settingsRow = makeEl('div', 'settings-row');
            const settingsBtn = makeEl('button', 'settings-btn');
            settingsBtn.setAttribute('aria-label', 'Settings');
            const statusPill = makeEl('div', 'ag-status-pill');
            settingsRow.appendChild(settingsBtn);
            settingsRow.appendChild(statusPill);
            sidebarFooter.appendChild(settingsRow);
            bodyEl.appendChild(sidebarFooter);

            const composerWrap = makeEl('div');
            const chatInput = makeEl('textarea');
            chatInput.placeholder = 'Ask anything, @ to mention, / for actions';
            composerWrap.appendChild(chatInput);
            bodyEl.appendChild(composerWrap);

            const swarmCalls = [];
            let checkpointsMock = [
                {{
                    task_id: 'task-flow-1',
                    project_path: 'D:/repo/Flow_vpn',
                    project_name: 'Flow_vpn',
                    user_request: 'Refactor network routing',
                    current_node: 'EXECUTE',
                    stage_label: 'Агенты работают',
                    is_complete: false,
                    done_workers: 1,
                    total_workers: 2,
                    orchestrator_model: 'claude-sonnet-4',
                    worker_models: ['flash'],
                    workers: [
                        {{ name: 'w1', status: 'done', model: 'flash', files: ['routes.py'] }},
                        {{ name: 'w2', status: 'running', model: 'flash', files: ['vpn.py'] }}
                    ]
                }}
            ];

            global.window = {{
                __ag_skip_auto_start: true,
                innerWidth: 1280,
                innerHeight: 800,
                addEventListener() {{}},
                getComputedStyle: (el) => (el && el.style) ? el.style : {{}},
                agAPI: {{
                    getState: async () => ({{ ok: true, activeAccount: {{ id: 'a1' }}, accounts: [], networkOk: true, ping: 42, countryCode: 'DE' }}),
                    refresh: async () => ({{ ok: true }}),
                    onStateUpdated: () => () => {{}},
                    swarm: async (op, payload) => {{
                        swarmCalls.push({{ op, payload }});
                        if (op === 'list') {{
                            return {{
                                ok: true,
                                checkpoints: checkpointsMock,
                                enginePath: 'D:/repo/.agents/skills/ag-swarm',
                                projects: [{{ path: 'D:/repo/Flow_vpn', name: 'Flow_vpn', isGit: true }}]
                            }};
                        }}
                        if (op === 'board') {{
                            return {{
                                ok: true,
                                board: {{
                                    messages: [
                                        {{ seq: 1, agent: 'coordinator', text: '@w1 implement routing', time: '00:01' }},
                                        {{ seq: 2, agent: 'w1', text: '@coordinator routes done', time: '00:02' }}
                                    ]
                                }}
                            }};
                        }}
                        if (op === 'prepare') {{
                            return {{
                                ok: true,
                                taskId: 'swarm-prep-1',
                                projectPath: payload.projectPath,
                                dispatchPrompt: '/ag-swarm RUN_ID swarm-prep-1 -> ' + payload.request
                            }};
                        }}
                        return {{ ok: true }};
                    }}
                }}
            }};

            global.document = {{
                body: bodyEl,
                head: headEl,
                documentElement: bodyEl,
                hidden: false,
                readyState: 'complete',
                addEventListener() {{}},
                removeEventListener() {{}},
                createElement(tag) {{
                    const el = makeEl(tag, '');
                    Object.defineProperty(el, 'id', {{
                        get() {{ return this._id || ''; }},
                        set(v) {{ this._id = v; if (v) elsById.set(v, this); }}
                    }});
                    return el;
                }},
                getElementById(id) {{ return elsById.get(id) || null; }},
                querySelectorAll(sel) {{
                    if (sel.includes('Settings') || sel.includes('button')) return [settingsBtn];
                    if (sel.includes('textarea')) return [chatInput];
                    if (sel.includes('dialog') || sel.includes('backdrop')) {{
                        return [document.getElementById('ag-swarm-modal-backdrop')].filter(Boolean);
                    }}
                    return [];
                }}
            }};
            global.localStorage = {{ getItem: () => null, setItem() {{}} }};

            const ui = require({json.dumps(str(DESKTOP_DIR / 'ag_desktop_ui.js'))});

            (async () => {{
                // 1. Mount Pill: verify strictly ABOVE #ag-status-pill in bottom footer
                ui.mountPill();
                const swarmPill = document.getElementById('ag-swarm-pill');
                assert.ok(swarmPill, 'Expected #ag-swarm-pill mounted in DOM');
                assert.strictEqual(swarmPill.parentElement, sidebarFooter, 'Pill must be inside sidebar footer');
                assert.strictEqual(swarmPill.nextElementSibling, settingsRow, '#ag-swarm-pill must be mounted strictly ABOVE settings row holding status pill');

                // 2. Pill formatting & hover tooltip
                ui.renderSwarmPillContent();
                assert.ok(swarmPill.innerHTML.includes('⚡') || swarmPill.textContent.includes('⚡'), 'Pill text must include agent symbol');

                ui.showSwarmTooltip();
                const tooltip = document.getElementById('ag-swarm-tooltip');
                assert.ok(tooltip, 'Expected #ag-swarm-tooltip on hover');
                ui.hideSwarmTooltip();

                // 3. Completion green indicator
                ui.setTestState({{ recentCompletion: {{ project_name: 'Flow_vpn', done_workers: 2, total_workers: 2, duration_seconds: 45 }} }});
                assert.ok(swarmPill.innerHTML.includes('✓ Готово: Flow_vpn') || swarmPill.textContent.includes('✓ Готово: Flow_vpn'), 'Pill must show completion indicator');
                assert.ok(swarmPill.classList.contains('ag-swarm-done'), 'Pill must have ag-swarm-done class for green styling');

                // 4. 3-level QuickPick navigation (#ag-qp-container)
                // Level 1: active projects + history
                ui.openQuickPick();
                await ui.openSwarmCheckpointsMenu();
                const qp = document.getElementById('ag-qp-container');
                assert.ok(qp, 'QuickPick container must exist');
                assert.ok(qp.innerHTML.includes('data-action="quick_task"'), 'Level 1 must contain quick_task');
                assert.ok(qp.innerHTML.includes('data-action="open_new_swarm"'), 'Level 1 must contain open_new_swarm');
                assert.ok(qp.innerHTML.includes('Flow_vpn'), 'Level 1 must show active session Flow_vpn');

                // 5. Clean 3-field launch modal without top dashboard
                ui.closeQuickPick();
                await ui.openSwarmRunModal();
                const modalBox = document.getElementById('ag-swarm-modal-box');
                assert.ok(modalBox, 'Modal box must be open');
                assert.ok(document.getElementById('ag-swarm-project-input'), 'Modal must contain project input');
                assert.ok(document.getElementById('ag-swarm-request-input'), 'Modal must contain request input');
                assert.ok(document.getElementById('ag-swarm-affected-tests'), 'Modal must contain affected tests checkbox');
                const tasksList = document.getElementById('ag-swarm-tasks-list');
                assert.ok(!tasksList || tasksList.style.display === 'none', 'Top dashboard tasks list must not be visible in compact launch modal');

                ui.closeSwarmModal();
                ui.stopDesktopSwarmWatcher();
                console.log(JSON.stringify({{ ok: true }}));
                process.exit(0);
            }})().catch((err) => {{
                console.error(err);
                process.exit(1);
            }});
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])

    def test_desktop_ui_monitor_snapshot_polling_and_live_sync(self):
        """Verifies loadSwarmState stores res.monitor into swarmSnapshot,
        passes it to renderSwarmPillContent, and starts live 2.5s polling."""
        script = textwrap.dedent(f"""
            const assert = require('assert');

            function makeEl(tag, id = '') {{
                const classes = new Set();
                const attrs = new Map();
                const el = {{
                    nodeType: 1,
                    tagName: tag.toUpperCase(),
                    _id: id,
                    dataset: {{}},
                    style: {{
                        cssText: '',
                        display: 'block',
                        setProperty(k, v) {{ this[k] = v; }},
                        getPropertyValue(k) {{ return this[k] || ''; }}
                    }},
                    disabled: false,
                    readOnly: false,
                    type: '',
                    placeholder: '',
                    value: '',
                    checked: false,
                    children: [],
                    parentElement: null,
                    isConnected: true,
                    _html: '',
                    get innerHTML() {{ return this._html; }},
                    set innerHTML(htmlStr) {{
                        this._html = String(htmlStr || '');
                        const idRx = /<([a-z0-9]+)\\b([^>]*?)\\bid="([^"]+)"([^>]*)>/gi;
                        let m;
                        while ((m = idRx.exec(this._html)) !== null) {{
                            const child = makeEl(m[1], m[3]);
                            child.parentElement = this;
                            elsById.set(m[3], child);
                        }}
                    }},
                    textContent: '',
                    innerText: '',
                    get childElementCount() {{ return this.children.length; }},
                    focus() {{}},
                    select() {{}},
                    dispatchEvent() {{}},
                    click() {{ if (typeof this.onclick === 'function') this.onclick({{ target: this, stopPropagation() {{}} }}); }},
                    matches: (sel) => sel.toLowerCase().includes(tag.toLowerCase()),
                    closest: (sel) => sel.toLowerCase().includes(tag.toLowerCase()) ? this : null,
                    getBoundingClientRect: () => ({{ left: 10, top: 700, right: 200, bottom: 740, width: 190, height: 40 }}),
                    getClientRects: () => [{{}}],
                    classList: {{
                        add: (...c) => c.forEach(x => classes.add(x)),
                        remove: (...c) => c.forEach(x => classes.delete(x)),
                        toggle: (c, force) => force === undefined
                            ? (classes.has(c) ? classes.delete(c) : classes.add(c))
                            : (force ? classes.add(c) : classes.delete(c)),
                        contains: (c) => classes.has(c)
                    }},
                    setAttribute(k, v) {{ attrs.set(k, String(v)); }},
                    getAttribute(k) {{ return attrs.has(k) ? attrs.get(k) : null; }},
                    removeAttribute(k) {{ attrs.delete(k); }},
                    hasAttribute(k) {{ return attrs.has(k); }},
                    insertBefore(newChild, refChild) {{
                        if (newChild.parentElement) {{
                            newChild.parentElement.children = newChild.parentElement.children.filter(c => c !== newChild);
                        }}
                        newChild.parentElement = this;
                        if (!refChild) {{
                            this.children.push(newChild);
                        }} else {{
                            const idx = this.children.indexOf(refChild);
                            if (idx === -1) this.children.push(newChild);
                            else this.children.splice(idx, 0, newChild);
                        }}
                        return newChild;
                    }},
                    appendChild(child) {{ return this.insertBefore(child, null); }},
                    remove() {{
                        if (this.parentElement) {{
                            this.parentElement.children = this.parentElement.children.filter(c => c !== this);
                        }}
                        this.isConnected = false;
                    }},
                    querySelectorAll(sel) {{ return []; }},
                    querySelector(sel) {{ return null; }}
                }};
                return el;
            }}

            const elsById = new Map();
            const bodyEl = makeEl('body');
            const headEl = makeEl('head');

            const sidebarFooter = makeEl('div', 'sidebar-footer');
            const settingsRow = makeEl('div', 'settings-row');
            const settingsBtn = makeEl('button', 'settings-btn');
            settingsBtn.setAttribute('aria-label', 'Settings');
            const statusPill = makeEl('div', 'ag-status-pill');
            settingsRow.appendChild(settingsBtn);
            settingsRow.appendChild(statusPill);
            sidebarFooter.appendChild(settingsRow);
            bodyEl.appendChild(sidebarFooter);

            const mockMonitor = {{
                summary: {{ active_agents: 2, active_projects: 2, active_sessions: 1 }},
                projects: [
                    {{ project_name: 'Flow_vpn', active_agents: 1, active_sessions: 1 }},
                    {{ project_name: 'threads', active_agents: 1, active_sessions: 0 }}
                ]
            }};

            let swarmListCallCount = 0;

            global.window = {{
                __ag_skip_auto_start: true,
                innerWidth: 1280,
                innerHeight: 800,
                addEventListener() {{}},
                getComputedStyle: (el) => (el && el.style) ? el.style : {{}},
                agAPI: {{
                    getState: async () => ({{ ok: true, activeAccount: {{ id: 'a1', name: 'Primary' }}, accounts: [], networkOk: true, ping: 35, countryCode: 'US' }}),
                    refresh: async () => ({{ ok: true }}),
                    onStateUpdated: () => () => {{}},
                    swarm: async (op, payload) => {{
                        if (op === 'list') {{
                            swarmListCallCount++;
                            return {{
                                ok: true,
                                monitor: mockMonitor,
                                checkpoints: [],
                                projects: [{{ path: 'D:/FlowBuild/Flow_vpn', name: 'Flow_vpn', isGit: false }}]
                            }};
                        }}
                        return {{ ok: true }};
                    }}
                }}
            }};

            global.document = {{
                body: bodyEl,
                head: headEl,
                documentElement: bodyEl,
                hidden: false,
                readyState: 'complete',
                addEventListener() {{}},
                removeEventListener() {{}},
                createElement(tag) {{
                    const el = makeEl(tag, '');
                    Object.defineProperty(el, 'id', {{
                        get() {{ return this._id || ''; }},
                        set(v) {{ this._id = v; if (v) elsById.set(v, this); }}
                    }});
                    return el;
                }},
                getElementById(id) {{ return elsById.get(id) || null; }},
                querySelectorAll(sel) {{
                    if (sel.includes('Settings') || sel.includes('button')) return [settingsBtn];
                    return [];
                }}
            }};

            global.localStorage = {{ getItem: () => null, setItem() {{}} }};

            const ui = require({json.dumps(str(DESKTOP_DIR / 'ag_desktop_ui.js'))});

            (async () => {{
                // 1. Mount pill: mounts #ag-swarm-pill and starts live polling
                ui.mountPill();
                const swarmPill = document.getElementById('ag-swarm-pill');
                assert.ok(swarmPill, 'Expected #ag-swarm-pill mounted');

                // 2. Call loadSwarmState
                const res = await ui.loadSwarmState();
                assert.strictEqual(res.ok, true, 'loadSwarmState must succeed');

                // 3. Verify res.monitor is stored into swarmSnapshot
                const snap = ui.getSwarmSnapshot ? ui.getSwarmSnapshot() : (ui.getTestState && ui.getTestState().swarmSnapshot);
                assert.deepStrictEqual(snap, mockMonitor, 'loadSwarmState must store res.monitor into swarmSnapshot');

                // 4. Verify renderSwarmPillContent passes swarmSnapshot to formatAgentPillText
                assert.ok(
                    swarmPill.textContent.includes('⚡ 2 аг. · 2 проекта'),
                    'Pill text must be updated with live active ticker: ' + swarmPill.textContent
                );

                // 5. Verify live active ticker prioritizes active snapshot over recent completion
                ui.setTestState({{ recentCompletion: {{ project_name: 'DoneTask', done_workers: 1, total_workers: 1, isError: false }} }});
                assert.ok(
                    swarmPill.textContent.includes('⚡ 2 аг. · 2 проекта'),
                    'Active agents must take precedence over completed task: ' + swarmPill.textContent
                );

                ui.stopSwarmLivePoll();
                ui.stopDesktopSwarmWatcher();
                console.log(JSON.stringify({{ ok: true, calls: swarmListCallCount }}));
                process.exit(0);
            }})().catch((err) => {{
                console.error(err);
                process.exit(1);
            }});
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])


class AsarAndDesktopIpcTests(unittest.TestCase):
    def test_asar_patcher_roundtrip_and_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            if str(SOURCE_DIR) not in sys.path:
                sys.path.insert(0, str(SOURCE_DIR))
            from patcher.manager import asar_patcher

            ipc_block = asar_patcher._load_ipc_handler_block()
            self.assertIsNotNone(ipc_block)
            assert ipc_block is not None
            self.assertTrue(ipc_block.lstrip().startswith(asar_patcher._IPC_START))
            self.assertTrue(ipc_block.rstrip().endswith(asar_patcher._IPC_END))
            self.assertIn("ag:state-updated", ipc_block)
            self.assertIn("closeAllConnections", ipc_block)

            utils_orig = (
                "const handleDidFinishLoad = () => {\n"
                "    void applyStoredZoomLevel();\n"
                "};\n"
            )
            preload_orig = (
                "electron_1.contextBridge.exposeInMainWorld('wsl', wslAPI);\n"
            )
            ipc_orig = (
                "function registerIpcHandlers(storageManager) {\n"
                "    (0, wcvIpcHandlers_1.registerWcvIpcHandlers)();\n"
                "}\n"
            )

            app_root = Path(tmp) / "Antigravity"
            bin_dir = app_root / "resources" / "bin"
            res_dir = app_root / "resources"
            bin_dir.mkdir(parents=True, exist_ok=True)
            mgr_bin = bin_dir / "language_server.exe"
            mgr_bin.write_bytes(b"stub")
            asar_path = res_dir / "app.asar"
            _build_asar(
                asar_path,
                {
                    "package.json": b'{"version":"2.0.0"}',
                    "dist/utils.js": utils_orig.encode("utf-8"),
                    "dist/preload.js": preload_orig.encode("utf-8"),
                    "dist/ipcHandlers.js": ipc_orig.encode("utf-8"),
                },
            )

            self.assertFalse(asar_patcher.is_asar_ui_patched(str(mgr_bin)))
            self.assertTrue(asar_patcher.patch_desktop_asar(str(mgr_bin), kill_on_lock=False))
            self.assertTrue(asar_patcher.is_asar_ui_patched(str(mgr_bin)))
            self.assertTrue((res_dir / "app.asar.backup").is_file())
            with open(asar_path, "rb") as f:
                patched_hdr, patched_base = asar_patcher._read_asar_header(f)
                pn = asar_patcher._get_node(patched_hdr, "dist/preload.js")
                in_node = asar_patcher._get_node(patched_hdr, "dist/ipcHandlers.js")
                assert pn and in_node
                f.seek(patched_base + int(pn["offset"]))
                patched_preload = f.read(int(pn["size"])).decode("utf-8")
                f.seek(patched_base + int(in_node["offset"]))
                patched_ipc = f.read(int(in_node["size"])).decode("utf-8")
            self.assertIn("swarm: (op, payload)", patched_preload)
            self.assertIn("'ag:swarm'", patched_ipc)

            # Idempotent re-patch
            self.assertTrue(asar_patcher.patch_desktop_asar(str(mgr_bin), kill_on_lock=False))
            self.assertTrue(asar_patcher.is_asar_ui_patched(str(mgr_bin)))

            # Restore restores original clean bytes
            self.assertTrue(asar_patcher.restore_desktop_asar(str(mgr_bin)))
            self.assertFalse(asar_patcher.is_asar_ui_patched(str(mgr_bin)))

    def test_asar_patcher_full_utils_did_fail_load_and_language_server_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            if str(SOURCE_DIR) not in sys.path:
                sys.path.insert(0, str(SOURCE_DIR))
            from patcher.manager import asar_patcher

            utils_full = (
                "function createWindow(url, storageManager) {\n"
                "    const hubContents = win.webContents;\n"
                "    if (storageManager) {\n"
                "        const handleDidFinishLoad = () => {\n"
                "            if (!win.isDestroyed?.() && !hubContents.isDestroyed?.()) {\n"
                "                void applyStoredZoomLevel();\n"
                "            }\n"
                "        };\n"
                "        hubContents.on('did-finish-load', handleDidFinishLoad);\n"
                "    }\n"
                "    void win.loadURL(url);\n"
                "    return win;\n"
                "}\n"
            )
            ls_orig = (
                "exports.setIntentionalTermination = setIntentionalTermination;\n"
                "let _lsProcess = null;\n"
                "let _lsPort = 0;\n"
                "let _restartCount = 0;\n"
                "let _lastRestartTime = 0;\n"
                "function clearLsProcess() {\n"
                "    _lsProcess = null;\n"
                "}\n"
                "function setIntentionalTermination(v) {}\n"
            )
            preload_orig = "electron_1.contextBridge.exposeInMainWorld('wsl', wslAPI);\n"
            ipc_orig = (
                "function registerIpcHandlers(storageManager) {\n"
                "    (0, wcvIpcHandlers_1.registerWcvIpcHandlers)();\n"
                "}\n"
            )

            app_root = Path(tmp) / "Antigravity"
            bin_dir = app_root / "resources" / "bin"
            res_dir = app_root / "resources"
            bin_dir.mkdir(parents=True, exist_ok=True)
            mgr_bin = bin_dir / "language_server.exe"
            mgr_bin.write_bytes(b"stub")
            asar_path = res_dir / "app.asar"
            _build_asar(
                asar_path,
                {
                    "package.json": b'{"version":"2.0.0"}',
                    "dist/utils.js": utils_full.encode("utf-8"),
                    "dist/languageServer.js": ls_orig.encode("utf-8"),
                    "dist/preload.js": preload_orig.encode("utf-8"),
                    "dist/ipcHandlers.js": ipc_orig.encode("utf-8"),
                },
            )

            self.assertFalse(asar_patcher.is_asar_ui_patched(str(mgr_bin)))
            self.assertTrue(asar_patcher.patch_desktop_asar(str(mgr_bin), kill_on_lock=False))
            self.assertTrue(asar_patcher.is_asar_ui_patched(str(mgr_bin)))
            self.assertTrue(asar_patcher.patch_desktop_asar(str(mgr_bin), kill_on_lock=False))

            # Verify extracted hooked utils.js & languageServer.js behave properly under failure recovery
            with open(asar_path, "rb") as f:
                hdr, base = asar_patcher._read_asar_header(f)
                un = asar_patcher._get_node(hdr, "dist/utils.js")
                ln = asar_patcher._get_node(hdr, "dist/languageServer.js")
                assert un and ln
                f.seek(base + int(un["offset"]))
                patched_utils = f.read(int(un["size"])).decode("utf-8")
                f.seek(base + int(ln["offset"]))
                patched_ls = f.read(int(ln["size"])).decode("utf-8")

            self.assertIn(asar_patcher._UTILS_FAIL_START, patched_utils)
            self.assertIn("'did-fail-load'", patched_utils)
            self.assertIn("chrome-error://", patched_utils)
            self.assertIn(asar_patcher._LS_PORT_RESET_MARKER, patched_ls)
            self.assertIn("exports.resetRestartCount = resetRestartCount;", patched_ls)

            # Behavioral Node check: did-fail-load recovers chrome-error:// once new LS port appears
            utils_probe = textwrap.dedent(f"""
                const assert = require('assert');
                const Module = require('module');

                let lsPort = 0;
                let lsProc = null;
                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === './languageServer') {{
                        return {{
                            getLsPort: () => lsPort,
                            getLsProcess: () => lsProc
                        }};
                    }}
                    return origLoad.call(this, request, parent, isMain);
                }};

                const listeners = {{}};
                const loadedUrls = [];
                let curUrl = 'https://127.0.0.1:58084/';
                const hubContents = {{
                    isDestroyed: () => false,
                    isLoading: () => false,
                    getURL: () => curUrl,
                    executeJavaScript: async () => {{}},
                    on: (ev, fn) => {{ (listeners[ev] = listeners[ev] || []).push(fn); }},
                    off: () => {{}}
                }};
                const win = {{
                    webContents: hubContents,
                    isDestroyed: () => false,
                    loadURL: (u) => {{ loadedUrls.push(u); curUrl = u; }},
                    on: () => {{}},
                    once: () => {{}}
                }};
                function applyStoredZoomLevel() {{}}

                {patched_utils}

                createWindow('https://127.0.0.1:58084/', null);
                assert.strictEqual(loadedUrls.length, 1);
                // Simulate dead old port -> ERR_CONNECTION_REFUSED -> chrome-error:// finished loading
                curUrl = 'chrome-error://chromewebdata/';
                for (const fn of (listeners['did-fail-load'] || [])) {{
                    fn({{}}, -102, 'ERR_CONNECTION_REFUSED', 'https://127.0.0.1:58084/', true);
                }}
                // Firing did-finish-load on chrome-error:// must NOT cancel recovery timer
                for (const fn of (listeners['did-finish-load'] || [])) {{
                    fn();
                }}
                lsProc = {{ killed: false, exitCode: null }};
                lsPort = 64568;

                setTimeout(() => {{
                    try {{
                        assert.ok(loadedUrls.includes('https://127.0.0.1:64568/'), JSON.stringify(loadedUrls));
                        console.log(JSON.stringify({{ ok: true }}));
                        process.exit(0);
                    }} catch (e) {{
                        console.error(e);
                        process.exit(1);
                    }}
                }}, 600);
            """)
            res = _run_node(utils_probe)
            self.assertTrue(res["ok"])

    def test_ipc_handlers_network_refresh_sessions_and_ls_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            gemini_dir = Path(tmp) / "gemini"
            acc_dir = tools_dir / "accounts"
            tools_dir.mkdir(parents=True, exist_ok=True)
            acc_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CORE_DIR / "quota_engine.js", tools_dir / "quota_engine.js")

            (acc_dir / "acc_s.json").write_text(
                json.dumps({
                    "id": "acc_s",
                    "email": "standalone@example.com",
                    "name": "Standalone",
                    "token": {
                        "access_token": "ya29.s",
                        "refresh_token": "1//0s",
                        "expiry_timestamp": 2_500_000_000,
                    },
                    "quota": {
                        "subscription_tier": "PRO",
                        "uses_gcp_tos": False,
                        "quota_groups": _make_sample_groups(),
                        "last_updated": 1_900_000_000,
                    },
                }),
                encoding="utf-8",
            )
            (tools_dir / "accounts.json").write_text(
                json.dumps({"current_standalone_account_id": "acc_s", "accounts": [{"id": "acc_s", "email": "standalone@example.com", "name": "Standalone"}]}),
                encoding="utf-8",
            )

            groups_json = json.dumps(_make_sample_groups())
            script = textwrap.dedent(f"""
                const assert = require('assert');
                const Module = require('module');
                const { '{' } EventEmitter { '}' } = require('events');
                const https = require('https');

                process.env.AG_TOOLS_DIR = {json.dumps(str(tools_dir))};
                process.env.AG_GEMINI_DIR = {json.dumps(str(gemini_dir))};

                const ipcHandlers = new Map();
                const sentEvents = [];
                const sessionStats = {{ setProxy: 0, forceReloadProxyConfig: 0, closeAllConnections: 0, clearHostResolverCache: 0 }};
                const lsStats = {{ kills: 0, intentionalFlags: [] }};

                const fakeSession = {{
                    setProxy: async () => {{ sessionStats.setProxy++; }},
                    forceReloadProxyConfig: async () => {{ sessionStats.forceReloadProxyConfig++; }},
                    closeAllConnections: async () => {{ sessionStats.closeAllConnections++; }},
                    clearHostResolverCache: async () => {{ sessionStats.clearHostResolverCache++; }}
                }};
                const fakeWebContents = {{
                    isDestroyed: () => false,
                    session: fakeSession,
                    send: (ch) => {{ sentEvents.push(ch); }}
                }};
                const fakeWindow = {{
                    isDestroyed: () => false,
                    isVisible: () => true,
                    isMinimized: () => false,
                    webContents: fakeWebContents
                }};

                https.request = (opts, cb) => {{
                    const req = new EventEmitter();
                    req.write = () => {{}};
                    req.destroy = () => {{}};
                    req.end = () => {{
                        const res = new EventEmitter();
                        res.statusCode = 200;
                        res.setEncoding = () => {{}};
                        process.nextTick(() => {{
                            cb(res);
                            if ((opts.hostname || '').includes('cloudflare')) {{
                                res.emit('data', 'loc=NL\\n');
                            }} else if ((opts.path || '').includes('retrieveUserQuotaSummary')) {{
                                res.emit('data', JSON.stringify({{ groups: {groups_json} }}));
                            }} else {{
                                res.emit('data', '{{}}');
                            }}
                            res.emit('end');
                        }});
                    }};
                    return req;
                }};

                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === 'electron') {{
                        return {{
                            app: {{ isPackaged: false, getPath: () => {json.dumps(tmp)}, once: () => {{}} }},
                            ipcMain: {{ handle: (ch, fn) => ipcHandlers.set(ch, fn) }},
                            BrowserWindow: {{ getAllWindows: () => [fakeWindow] }},
                            session: {{ defaultSession: fakeSession }},
                            dialog: {{}},
                            Notification: class {{ on() {{}} show() {{}} }},
                            shell: {{}},
                            Menu: {{}},
                            webContents: {{}}
                        }};
                    }}
                    if (request === './utils') {{
                        return {{
                            getFocusedHubWindow: () => fakeWindow,
                            getHubWindows: () => [fakeWindow],
                            getHubWebContents: () => fakeWebContents
                        }};
                    }}
                    if (request === './languageServer') {{
                        return {{
                            setIntentionalTermination: (v) => {{ lsStats.intentionalFlags.push(Boolean(v)); }},
                            getLsProcess: () => ({{
                                killed: false,
                                exitCode: null,
                                kill: () => {{ lsStats.kills++; }}
                            }})
                        }};
                    }}
                    if (request === 'electron-log/main') {{
                        return {{ transports: {{ file: {{ getFile: () => ({{ path: '' }}) }} }}, error() {{}} }};
                    }}
                    if (request === 'electron-updater') return {{ autoUpdater: {{}} }};
                    if (request === './types') return {{ UpdateState: {{ Ready: 'ready' }} }};
                    if (request === './updater') return {{ broadcastState() {{}}, getLastState() {{}}, checkForUpdates() {{}} }};
                    if (request === './customScheme') return {{ extensionAuthorities: new Map() }};
                    if (request === './tray') return {{ updateTrayAgentCount() {{}} }};
                    if (request === './webviewBrowser') return {{}};
                    if (request === './ideInstall/constants') return {{ getIdeInstallPath: () => '' }};
                    if (request === './wsl') return {{ getActiveWslDistro: () => null, isWslAvailable: () => false }};
                    if (request === './menu') return {{ relaunchWithWslDistro() {{}} }};
                    if (request === './scriptJail') return {{}};
                    if (request === './externalUrl') return {{}};
                    if (request === './wcvBrowserRegistry') return {{}};
                    if (request === './wcvIpcHandlers') return {{ registerWcvIpcHandlers() {{}} }};
                    if (request === './guestDialogController') return {{}};
                    if (request === 'child_process') {{
                        return {{
                            execFile: (_bin, _args, _opts, cb) => cb && cb(null, '', '')
                        }};
                    }}
                    return origLoad.call(this, request, parent, isMain);
                }};

                const ipcMod = require({json.dumps(str(DESKTOP_DIR / 'ipcHandlers.js'))});

                (async () => {{
                    const storageItems = {{ 'agy.onboarding.lastLoginStrategy': 'google', 'jetski.onboarding.lastLoginUsername': 'old@example.com' }};
                    const storageUpdates = [];
                    ipcMod.registerIpcHandlers({{
                        getItems: () => ({{ ...storageItems }}),
                        updateItems: async (changes) => {{
                            storageUpdates.push(changes);
                            Object.assign(storageItems, changes);
                        }}
                    }});
                    assert.ok(ipcHandlers.has('ag:refresh'));
                    assert.ok(ipcHandlers.has('ag:get-state'));
                    const refreshFn = ipcHandlers.get('ag:refresh');
                    const getStateFn = ipcHandlers.get('ag:get-state');

                    const st = await getStateFn({{}});
                    assert.strictEqual(st.ok, true);
                    assert.strictEqual(st.activeAccount.id, 'acc_s');
                    assert.strictEqual(storageItems['agy.onboarding.lastLoginUsername'], 'standalone@example.com');
                    assert.strictEqual(storageItems['agy.onboarding.lastLoginIsGcpTos'], 'false');
                    assert.strictEqual(storageItems['agy.onboarding.welcomeModalSeen'], 'true');
                    assert.strictEqual(storageItems['jetski.onboarding.cloudResourceDisclosureSeen'], 'true');
                    assert.strictEqual(storageItems['agy.onboarding.lastLoginStrategy'], null);
                    assert.strictEqual(storageItems['jetski.onboarding.lastLoginUsername'], 'standalone@example.com');
                    assert.strictEqual(storageItems['jetski.onboarding.lastLoginIsGcpTos'], 'false');
                    assert.ok(storageUpdates.length >= 1);

                    // First 'network' call: flushes DNS, clears Chromium sockets, restarts LS once
                    const r1 = await refreshFn({{}}, 'network');
                    assert.strictEqual(r1.ok, true);
                    assert.strictEqual(r1.backendRestarted, true);
                    assert.strictEqual(r1.network.countryCode, 'NL');
                    assert.strictEqual(lsStats.kills, 1);
                    assert.deepStrictEqual(lsStats.intentionalFlags, [false]);
                    assert.strictEqual(sessionStats.setProxy, 1);
                    assert.strictEqual(sessionStats.forceReloadProxyConfig, 1);
                    assert.strictEqual(sessionStats.closeAllConnections, 1);
                    assert.strictEqual(sessionStats.clearHostResolverCache, 1);
                    assert.ok(sentEvents.includes('ag:state-updated'));

                    // Immediate second 'network' call: respects 65s LS cooldown while still clearing sockets
                    const r2 = await refreshFn({{}}, 'network');
                    assert.strictEqual(r2.ok, true);
                    assert.strictEqual(r2.backendRestarted, false);
                    assert.strictEqual(lsStats.kills, 1);
                    assert.strictEqual(sessionStats.closeAllConnections, 2);

                    // Direct fallback (without quota_engine.js) must NEVER fall back to current_account_id
                    const fs = require('fs');
                    const path = require('path');
                    fs.unlinkSync(path.join({json.dumps(str(tools_dir))}, 'quota_engine.js'));
                    fs.writeFileSync(path.join({json.dumps(str(tools_dir))}, 'accounts.json'), JSON.stringify({{
                        current_account_id: 'acc_s',
                        accounts: [{{ id: 'acc_s', email: 'standalone@example.com' }}]
                    }}));
                    const stNoLeak = await getStateFn({{}});
                    assert.strictEqual(stNoLeak.activeAccount, null);

                    console.log(JSON.stringify({{ ok: true }}));
                    process.exit(0);
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script)
            self.assertTrue(res["ok"])

    def test_ipc_handlers_switch_account_port_navigation_and_error_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            gemini_dir = Path(tmp) / "gemini"
            acc_dir = tools_dir / "accounts"
            tools_dir.mkdir(parents=True, exist_ok=True)
            gemini_dir.mkdir(parents=True, exist_ok=True)
            acc_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CORE_DIR / "quota_engine.js", tools_dir / "quota_engine.js")
            (tools_dir / "account_switcher.py").write_text("# stub\n", encoding="utf-8")

            (acc_dir / "acc_b.json").write_text(
                json.dumps({
                    "id": "acc_b",
                    "email": "b@example.com",
                    "name": "Account B",
                    "token": {"access_token": "ya29.b", "refresh_token": "1//0b", "expiry_timestamp": 2_500_000_000},
                    "quota": {"subscription_tier": "PRO", "uses_gcp_tos": False, "quota_groups": _make_sample_groups(), "last_updated": int(time.time())},
                }),
                encoding="utf-8",
            )
            (tools_dir / "accounts.json").write_text(
                json.dumps({"current_standalone_account_id": "acc_b", "accounts": [{"id": "acc_b", "email": "b@example.com", "name": "Account B"}]}),
                encoding="utf-8",
            )

            ipc_src = (DESKTOP_DIR / "ipcHandlers.js").read_text(encoding="utf-8")
            self.assertNotIn(".reload()", ipc_src)

            script = textwrap.dedent(f"""
                const assert = require('assert');
                const Module = require('module');
                const {{ EventEmitter }} = require('events');
                const https = require('https');

                process.env.AG_TOOLS_DIR = {json.dumps(str(tools_dir))};
                process.env.AG_GEMINI_DIR = {json.dumps(str(gemini_dir))};

                const ipcHandlers = new Map();
                const loadCalls = [];
                let reloadCount = 0;
                let resetRestartCalls = 0;
                let lsPort = 58084;
                let lsProc = {{ pid: 101, killed: false, exitCode: null, kill() {{}} }};
                let curUrl = 'https://127.0.0.1:58084/';
                let pythonSwitchMode = 'fail';
                let quotaApiCalls = 0;

                const fakeWebContents = {{
                    isDestroyed: () => false,
                    getURL: () => curUrl,
                    reload: () => {{ reloadCount++; }},
                    send: () => {{}}
                }};
                const fakeWindow = {{
                    isDestroyed: () => false,
                    isVisible: () => true,
                    isMinimized: () => false,
                    webContents: fakeWebContents,
                    loadURL: (u) => {{ loadCalls.push(u); curUrl = u; }}
                }};

                https.request = (opts, cb) => {{
                    const req = new EventEmitter();
                    req.write = () => {{}};
                    req.destroy = () => {{}};
                    req.end = () => {{
                        const res = new EventEmitter();
                        res.statusCode = 200;
                        res.setEncoding = () => {{}};
                        setTimeout(() => {{
                            cb(res);
                            if ((opts.path || '').includes('retrieveUserQuotaSummary')) {{
                                quotaApiCalls++;
                                res.emit('data', JSON.stringify({{ groups: {json.dumps(_make_sample_groups())} }}));
                            }} else if ((opts.hostname || '').includes('cloudflare')) {{
                                res.emit('data', 'loc=DE\\n');
                            }} else {{
                                res.emit('data', '{{}}');
                            }}
                            res.emit('end');
                        }}, 35);
                    }};
                    return req;
                }};

                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === 'electron') {{
                        return {{
                            app: {{ isPackaged: false, getPath: () => {json.dumps(tmp)}, once: () => {{}} }},
                            ipcMain: {{ handle: (ch, fn) => ipcHandlers.set(ch, fn) }},
                            BrowserWindow: {{ getAllWindows: () => [fakeWindow] }},
                            session: {{ defaultSession: null }},
                            dialog: {{}}, Notification: class {{ on() {{}} show() {{}} }}, shell: {{}}, Menu: {{}}, webContents: {{}}
                        }};
                    }}
                    if (request === './utils') {{
                        return {{ getHubWindows: () => [fakeWindow], getHubWebContents: () => fakeWebContents }};
                    }}
                    if (request === './languageServer') {{
                        return {{
                            setIntentionalTermination: () => {{}},
                            resetRestartCount: () => {{ resetRestartCalls++; }},
                            getLsProcess: () => lsProc,
                            getLsPort: () => lsPort
                        }};
                    }}
                    if (request === 'electron-log/main') return {{ transports: {{ file: {{ getFile: () => ({{ path: '' }}) }} }}, error() {{}} }};
                    if (request === 'electron-updater') return {{ autoUpdater: {{}} }};
                    if (request === './types') return {{ UpdateState: {{ Ready: 'ready' }} }};
                    if (request === './updater') return {{ broadcastState() {{}}, getLastState() {{}}, checkForUpdates() {{}} }};
                    if (request === './customScheme') return {{ extensionAuthorities: new Map() }};
                    if (request === './tray') return {{ updateTrayAgentCount() {{}} }};
                    if (request === './webviewBrowser') return {{}};
                    if (request === './ideInstall/constants') return {{ getIdeInstallPath: () => '' }};
                    if (request === './wsl') return {{ getActiveWslDistro: () => null, isWslAvailable: () => false }};
                    if (request === './menu') return {{ relaunchWithWslDistro() {{}} }};
                    if (request === './scriptJail') return {{}};
                    if (request === './externalUrl') return {{}};
                    if (request === './wcvBrowserRegistry') return {{}};
                    if (request === './wcvIpcHandlers') return {{ registerWcvIpcHandlers() {{}} }};
                    if (request === './guestDialogController') return {{}};
                    if (request === 'child_process') {{
                        return {{
                            execFile: (_bin, _args, _opts, cb) => {{
                                if (pythonSwitchMode === 'fail') {{
                                    cb && cb(new Error('exit 1'), JSON.stringify({{ ok: false, error: 'Target account not found' }}), '');
                                }} else if (pythonSwitchMode === 'restarting') {{
                                    cb && cb(null, JSON.stringify({{ ok: true, name: 'Account B', email: 'b@example.com', app_type: 'standalone', restarting: true }}), '');
                                }} else {{
                                    // Simulate LS crashing then restarting onto new random port 64568
                                    lsPort = 0;
                                    lsProc = null;
                                    curUrl = 'chrome-error://chromewebdata/';
                                    setTimeout(() => {{
                                        lsProc = {{ pid: 202, killed: false, exitCode: null }};
                                        lsPort = 64568;
                                    }}, 180);
                                    cb && cb(null, JSON.stringify({{ ok: true, name: 'Account B', email: 'b@example.com' }}), '');
                                }}
                            }}
                        }};
                    }}
                    return origLoad.call(this, request, parent, isMain);
                }};

                const ipcMod = require({json.dumps(str(DESKTOP_DIR / 'ipcHandlers.js'))});
                (async () => {{
                    ipcMod.registerIpcHandlers({{ getItems: () => ({{}}), updateItems: async () => {{}} }});
                    const switchFn = ipcHandlers.get('ag:switch-account');
                    const refreshFn = ipcHandlers.get('ag:refresh');

                    // 1. Failed switch returns ok:false without navigating or reloading
                    const failRes = await switchFn({{}}, 'missing');
                    assert.strictEqual(failRes.ok, false);
                    assert.ok(failRes.error.includes('Target account not found'));
                    assert.strictEqual(loadCalls.length, 0);

                    // 2. Successful switch resets restart count & navigates stuck window to new LS port
                    pythonSwitchMode = 'ok';
                    const okRes = await switchFn({{}}, 'acc_b');
                    assert.strictEqual(okRes.ok, true);
                    assert.strictEqual(resetRestartCalls, 2);

                    await new Promise((r) => setTimeout(r, 850));
                    assert.deepStrictEqual(loadCalls, ['https://127.0.0.1:64568/']);
                    assert.strictEqual(reloadCount, 0);

                    // 2b. Full app restart switch (restarting: true) resolves immediately without LS port/loadURL race
                    loadCalls.length = 0;
                    pythonSwitchMode = 'restarting';
                    const restartRes = await switchFn({{}}, 'acc_b');
                    assert.strictEqual(restartRes.ok, true);
                    assert.strictEqual(restartRes.restarting, true);
                    await new Promise((r) => setTimeout(r, 150));
                    assert.strictEqual(loadCalls.length, 0);

                    // 3. Explicit force active refresh runs even while non-forced refresh is in flight
                    const callsBefore = quotaApiCalls;
                    const pNonForced = refreshFn({{}}, {{ mode: 'active', force: false, minAgeSec: 60 }});
                    const pForced = refreshFn({{}}, {{ mode: 'active', force: true, minAgeSec: 0 }});
                    const [rNonForced, rForced] = await Promise.all([pNonForced, pForced]);
                    assert.ok(
                        quotaApiCalls >= callsBefore + 1,
                        'Expected forced refresh to execute API call (calls=' + quotaApiCalls + ', nonForced=' + JSON.stringify(rNonForced) + ', forced=' + JSON.stringify(rForced) + ')'
                    );

                    console.log(JSON.stringify({{ ok: true }}));
                    process.exit(0);
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script)
            self.assertTrue(res["ok"])

    def test_ipc_handlers_swarm_bridge_operations(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tools_dir = tmp_path / "tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(CORE_DIR / "swarm_bridge.js", tools_dir / "swarm_bridge.js")
            shutil.copy2(CORE_DIR / "quota_engine.js", tools_dir / "quota_engine.js")

            repo = tmp_path / "sample_git_repo"
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / "README.md").write_text("# sample\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            swarm_engine_dir = ROOT_DIR / "source" / "patcher"
            script = textwrap.dedent(f"""
                const Module = require('module');
                const assert = require('assert');
                const path = require('path');
                const fs = require('fs');

                const ipcHandlers = new Map();
                const clipboardText = [];
                const fakeWebContents = {{
                    isDestroyed: () => false,
                    send() {{}}
                }};
                const fakeWindow = {{
                    isDestroyed: () => false,
                    webContents: fakeWebContents
                }};

                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === 'electron') {{
                        return {{
                            app: {{ isPackaged: false, getPath: () => {json.dumps(tmp)}, once: () => {{}} }},
                            ipcMain: {{ handle: (ch, fn) => ipcHandlers.set(ch, fn) }},
                            BrowserWindow: {{ getAllWindows: () => [fakeWindow] }},
                            session: {{ defaultSession: null }},
                            dialog: {{
                                showOpenDialog: async () => ({{ canceled: false, filePaths: [{json.dumps(str(repo))}] }})
                            }},
                            clipboard: {{
                                writeText: (t) => clipboardText.push(t)
                            }},
                            Notification: class {{ on() {{}} show() {{}} }}, shell: {{}}, Menu: {{}}, webContents: {{}}
                        }};
                    }}
                    if (request === './utils') return {{ getFocusedHubWindow: () => fakeWindow, getHubWindows: () => [fakeWindow], getHubWebContents: () => fakeWebContents }};
                    if (request === './languageServer') return {{ setIntentionalTermination: () => {{}}, resetRestartCount: () => {{}}, getLsProcess: () => null, getLsPort: () => 0 }};
                    if (request === 'electron-log/main') return {{ transports: {{ file: {{ getFile: () => ({{ path: '' }}) }} }}, error() {{}} }};
                    if (request === 'electron-updater') return {{ autoUpdater: {{}} }};
                    if (request === './types') return {{ UpdateState: {{ Ready: 'ready' }} }};
                    if (request === './updater') return {{ broadcastState() {{}}, getLastState() {{}}, checkForUpdates() {{}} }};
                    if (request === './customScheme') return {{ extensionAuthorities: new Map() }};
                    if (request === './tray') return {{ updateTrayAgentCount() {{}} }};
                    if (request === './webviewBrowser') return {{}};
                    if (request === './ideInstall/constants') return {{ getIdeInstallPath: () => '' }};
                    if (request === './wsl') return {{ getActiveWslDistro: () => null, isWslAvailable: () => false }};
                    if (request === './menu') return {{ relaunchWithWslDistro() {{}} }};
                    if (request === './scriptJail') return {{}};
                    if (request === './externalUrl') return {{}};
                    if (request === './wcvBrowserRegistry') return {{}};
                    if (request === './wcvIpcHandlers') return {{ registerWcvIpcHandlers() {{}} }};
                    if (request === './guestDialogController') return {{}};
                    return origLoad.call(this, request, parent, isMain);
                }};

                const ipcMod = require({json.dumps(str(DESKTOP_DIR / 'ipcHandlers.js'))});
                (async () => {{
                    ipcMod.registerIpcHandlers({{ getItems: () => ({{}}), updateItems: async () => {{}} }});
                    const swarmFn = ipcHandlers.get('ag:swarm');
                    assert.strictEqual(typeof swarmFn, 'function');

                    // Override bridge lookup to point enginePath at workspace .agents/skills/ag-swarm via swarm_engine dir check
                    const picked = await swarmFn({{}}, 'pick_project', {{}});
                    assert.strictEqual(picked.ok, true);
                    assert.strictEqual(picked.canceled, false);
                    assert.strictEqual(picked.projectPath, {json.dumps(str(repo))});
                    assert.strictEqual(picked.isGit, true);

                    // Deploy local junction/copy of swarm engine inside tools_dir so _getSwarmBridge finds it reliably
                    fs.symlinkSync({json.dumps(str(swarm_engine_dir))}, path.join({json.dumps(str(tools_dir))}, 'swarm_engine'), 'junction');

                    const listed0 = await swarmFn({{}}, 'list');
                    assert.strictEqual(listed0.ok, true);
                    assert.ok(listed0.enginePath);
                    assert.ok(listed0.projects.some(p => p.path === {json.dumps(str(repo))}));

                    const prepared = await swarmFn({{}}, 'prepare', {{
                        projectPath: {json.dumps(str(repo))},
                        request: 'Implement calculator helper',
                        useAffectedTests: true
                    }});
                    assert.strictEqual(prepared.ok, true, JSON.stringify(prepared));
                    assert.ok(prepared.taskId);
                    assert.ok(prepared.dispatchPrompt.includes(prepared.taskId));
                    assert.ok(clipboardText.some(t => t.includes(prepared.taskId)));
                    assert.ok(fs.existsSync(path.join({json.dumps(str(tools_dir))}, 'swarm', prepared.taskId, 'state.json')));

                    const st = await swarmFn({{}}, 'status', {{ taskId: prepared.taskId }});
                    assert.strictEqual(st.ok, true);
                    assert.strictEqual(st.checkpoint.task_id, prepared.taskId);
                    assert.strictEqual(st.checkpoint.project_name, {json.dumps(repo.name)});
                    assert.ok(st.checkpoint.stage_label);
                    assert.ok(st.checkpoint.orchestrator_model);
                    assert.ok(Array.isArray(st.checkpoint.workers));

                    const getSet0 = await swarmFn({{}}, 'get_settings', {{}});
                    assert.strictEqual(getSet0.ok, true);
                    assert.strictEqual(getSet0.failoverPolicy, 'interactive');

                    const setPol = await swarmFn({{}}, 'set_failover_policy', {{ policy: 'account_hop' }});
                    assert.strictEqual(setPol.ok, true);
                    assert.strictEqual(setPol.failoverPolicy, 'account_hop');

                    const saveMc = await swarmFn({{}}, 'save_model_config', {{ config: {{ worker_model: 'claude-sonnet', custom_models: {{}} }} }});
                    assert.strictEqual(saveMc.ok, true);
                    assert.strictEqual(saveMc.modelConfig.worker_model, 'claude-sonnet');

                    const getSet1 = await swarmFn({{}}, 'get_settings', {{}});
                    assert.strictEqual(getSet1.failoverPolicy, 'account_hop');
                    assert.strictEqual(getSet1.modelConfig.worker_model, 'claude-sonnet');

                    const resetMc = await swarmFn({{}}, 'reset_model_config', {{}});
                    assert.strictEqual(resetMc.ok, true);
                    assert.deepStrictEqual(resetMc.modelConfig, {{}});

                    const resumed = await swarmFn({{}}, 'resume', {{ taskId: prepared.taskId, projectPath: {json.dumps(str(repo))} }});
                    assert.strictEqual(resumed.ok, true, JSON.stringify(resumed));
                    assert.ok(resumed.dispatchPrompt.includes('возобновления'));

                    const cleaned = await swarmFn({{}}, 'cleanup', {{ taskId: prepared.taskId, projectPath: {json.dumps(str(repo))}, deleteState: true }});
                    assert.strictEqual(cleaned.ok, true, JSON.stringify(cleaned));
                    assert.strictEqual(cleaned.deletedState, true);
                    assert.strictEqual(fs.existsSync(path.join({json.dumps(str(tools_dir))}, 'swarm', prepared.taskId)), false);

                    console.log(JSON.stringify({{ ok: true }}));
                    process.exit(0);
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script, env_extra={"AG_TOOLS_DIR": str(tools_dir), "USERPROFILE": str(tmp_path), "HOME": str(tmp_path)})
            self.assertTrue(res["ok"])


class SwarmBridgeCoreTests(unittest.TestCase):
    def test_swarm_bridge_shared_helpers_and_action_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_engine = tmp_path / "swarm_engine"
            (fake_engine / "swarm").mkdir(parents=True, exist_ok=True)
            (fake_engine / "swarm" / "ipc_server.py").write_text("# stub\n", encoding="utf-8")

            fake_repo = tmp_path / "proj_a"
            (fake_repo / ".git").mkdir(parents=True, exist_ok=True)

            script = textwrap.dedent(f"""
                const assert = require('assert');
                const path = require('path');
                const bridge = require({json.dumps(str(CORE_DIR / 'swarm_bridge.js'))});

                (async () => {{
                    const engine = bridge.resolveSwarmEnginePath([{json.dumps(str(fake_repo))}], [{json.dumps(str(tmp_path))}]);
                    assert.strictEqual(engine, {json.dumps(str(fake_engine))});

                    const remembered = await bridge.handleSwarmAction('remember_project', {{ projectPath: {json.dumps(str(fake_repo))} }});
                    assert.strictEqual(remembered.ok, true);
                    assert.ok(remembered.projects.some(p => p.path === {json.dumps(str(fake_repo))} && p.isGit === true));

                    const prompt = bridge.buildSwarmDispatchPrompt({{
                        taskId: 't-shared-1',
                        projectPath: {json.dumps(str(fake_repo))},
                        prompt: 'Fix parser',
                        enginePath: engine,
                        useAffectedTests: false,
                        mode: 'run'
                    }});
                    assert.ok(prompt.includes('ag-swarm'));
                    assert.ok(prompt.includes('t-shared-1'));
                    assert.ok(!prompt.includes('--affected'));

                    const unknown = await bridge.handleSwarmAction('nonexistent_op', {{}});
                    assert.strictEqual(unknown.ok, false);
                    assert.ok(unknown.error.includes('Unknown swarm operation'));

                    console.log(JSON.stringify({{ ok: true }}));
                    process.exit(0);
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script, env_extra={"AG_TOOLS_DIR": str(tmp_path), "USERPROFILE": str(tmp_path), "HOME": str(tmp_path)})
            self.assertTrue(res["ok"])

    def test_swarm_bridge_mission_control_aggregation_and_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tools_dir = tmp_path / "tools"
            gemini_dir = tmp_path / "gemini"
            (tools_dir / "swarm" / "cp-active-1").mkdir(parents=True, exist_ok=True)
            (tools_dir / "swarm" / "cp-active-1" / "state.json").write_text(
                json.dumps({
                    "task_id": "cp-active-1",
                    "project_path": "D:\\work\\Flow_vpn",
                    "user_request": "Audit Flow routes",
                    "current_node": "EXECUTE",
                    "is_complete": False,
                    "briefs": [
                        {"worker_name": "worker-a", "task_description": "", "status": "pending", "owned_files": ["routes.py"]},
                        {"worker_name": "worker-c", "task_description": "Update schema", "status": "done", "started_at": "2026-11-11T09:50:00Z", "finished_at": "2026-11-11T09:51:00Z", "owned_files": ["schema.py"]},
                    ],
                    "worker_results": {"worker-c": {"success": True, "duration_seconds": 60}},
                }),
                encoding="utf-8",
            )

            sw1 = gemini_dir / "brain" / "swarm-active-1" / ".system_generated" / "swarm"
            sw1.mkdir(parents=True, exist_ok=True)
            (sw1 / "swarm.json").write_text(json.dumps({"swarm_id": "swarm-active-1", "created_at": "2026-11-11T10:00:00Z"}), encoding="utf-8")
            (sw1 / "members.json").write_text(
                json.dumps({
                    "title": "Flow Audit",
                    "members": {
                        "coordinator": {"conversation_id": "swarm-active-1", "role": "coordinator", "phase": "RUNNING"},
                        "worker-a": {"conversation_id": "conv-w-run", "role": "worker", "phase": "RUNNING", "applied_model": "Gemini 3 Flash"},
                    },
                }),
                encoding="utf-8",
            )
            (sw1 / "board.jsonl").write_text(
                json.dumps({"seq": 1, "agent": "coordinator", "text": "@worker-a Audit auth routes [cp-active-1]"}) + "\n",
                encoding="utf-8",
            )

            tr1 = gemini_dir / "brain" / "conv-w-run" / ".system_generated" / "logs"
            tr1.mkdir(parents=True, exist_ok=True)
            (tr1 / "transcript.jsonl").write_text(
                json.dumps({"step_index": 0, "created_at": "2026-11-11T10:00:05Z"}) + "\n",
                encoding="utf-8",
            )

            sw2 = gemini_dir / "brain" / "swarm-done-1" / ".system_generated" / "swarm"
            sw2.mkdir(parents=True, exist_ok=True)
            (sw2 / "swarm.json").write_text(json.dumps({"swarm_id": "swarm-done-1", "created_at": "2026-11-11T08:00:00Z"}), encoding="utf-8")
            (sw2 / "members.json").write_text(
                json.dumps({
                    "title": "Flow Done Release",
                    "members": {
                        "monitor": {"conversation_id": "conv-mon-done", "role": "monitor", "phase": "RUNNING", "killed": False},
                        "worker-b": {"role": "worker", "phase": "KILLED", "killed": True},
                    },
                }),
                encoding="utf-8",
            )
            (sw2 / "board.jsonl").write_text(
                json.dumps({"seq": 1, "agent": "coordinator", "text": "[done] Release complete"}) + "\n",
                encoding="utf-8",
            )

            script = textwrap.dedent(f"""
                const assert = require('assert');
                const {{ DatabaseSync }} = require('node:sqlite');
                const path = require('path');
                const bridge = require({json.dumps(str(CORE_DIR / 'swarm_bridge.js'))});

                const dbPath = path.join({json.dumps(str(gemini_dir))}, 'conversation_summaries.db');
                const db = new DatabaseSync(dbPath);
                db.exec(`CREATE TABLE conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT, preview TEXT, step_count INTEGER,
                    last_modified_time TEXT, workspace_uris TEXT,
                    status TEXT, agent_name TEXT, parent_conversation_id TEXT,
                    nesting_depth INTEGER, not_fully_idle INTEGER, killed INTEGER,
                    last_user_input_time TEXT
                );`);
                const ins = db.prepare(`INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
                const nowIso = new Date(Date.now() - 5000).toISOString();
                const pastIso = new Date(Date.now() - 3600000).toISOString();
                ins.run('swarm-active-1', 'Flow Audit', '', 6, nowIso, '["file:///D:/work/Flow_vpn"]', 'CASCADE_RUN_STATUS_RUNNING', '', '', 0, 1, 0, nowIso);
                ins.run('conv-w-run', 'worker-a', '', 2, nowIso, '["file:///D:/work/Flow_vpn"]', 'CASCADE_RUN_STATUS_RUNNING', 'worker-a', 'swarm-active-1', 1, 1, 0, '0001-01-01 00:00:00+00:00');
                ins.run('swarm-done-1', 'Flow Done Release', '', 12, pastIso, '["file:///d:/work/Flow_vpn"]', 'CASCADE_RUN_STATUS_IDLE', '', '', 0, 0, 0, pastIso);
                ins.run('conv-mon-done', 'monitor', '', 1, pastIso, '["file:///d:/work/Flow_vpn"]', 'CASCADE_RUN_STATUS_IDLE', 'monitor', 'swarm-done-1', 1, 0, 0, '0001-01-01 00:00:00+00:00');
                ins.run('chat-solo-1', 'Solo Hotfix', '', 4, nowIso, '["file:///D:/work/SoloRepo"]', 'CASCADE_RUN_STATUS_RUNNING', '', '', 0, 1, 0, nowIso);
                db.close();

                (async () => {{
                    const monRes = await bridge.handleSwarmAction(
                        'monitor',
                        {{ uiState: {{ selectedBoardId: 'swarm-active-1' }} }},
                        {{ extraDirs: [{json.dumps(str(CORE_DIR))}] }}
                    );
                    assert.strictEqual(monRes.ok, true);
                    const snap = monRes.monitor;
                    assert.strictEqual(snap.summary.active_agents, 3, JSON.stringify(snap.summary));
                    assert.strictEqual(snap.summary.active_projects, 2, JSON.stringify(snap.summary));

                    const flowBuckets = snap.projects.filter(p => /flow_vpn/i.test(p.project_name));
                    assert.strictEqual(flowBuckets.length, 1, 'Expected Windows drive-letter case merge for Flow_vpn');
                    const flow = flowBuckets[0];
                    assert.strictEqual(flow.active_agents, 2);

                    const cpSess = flow.sessions.find(s => s.id === 'cp-active-1');
                    assert.ok(cpSess, 'Expected checkpoint merged with native swarm');
                    const wa = cpSess.workers.find(w => w.name === 'worker-a');
                    assert.ok(wa && wa.status === 'running', JSON.stringify(wa));
                    assert.ok(wa.task_description.includes('Audit auth routes'), JSON.stringify(wa));
                    assert.strictEqual(wa.started_at_ms, Date.parse('2026-11-11T10:00:05Z'));

                    const doneSess = flow.sessions.find(s => s.id === 'swarm-done-1');
                    assert.ok(doneSess && !doneSess.is_active, 'Completed swarm with unkilled monitor must stay inactive');

                    assert.ok(monRes.boardData && monRes.boardData.messages.length === 1);
                    assert.strictEqual(monRes.boardData.swarm_id, 'swarm-active-1');

                    const boardRes = await bridge.handleSwarmAction('board', {{ swarmId: 'swarm-active-1' }}, {{ extraDirs: [{json.dumps(str(CORE_DIR))}] }});
                    assert.strictEqual(boardRes.ok, true);
                    assert.strictEqual(boardRes.board.messages.length, 1);
                    assert.ok(boardRes.board.messages[0].text.includes('Audit auth routes'));

                    console.log(JSON.stringify({{ ok: true }}));
                    process.exit(0);
                }})().catch((err) => {{
                    console.error(err);
                    process.exit(1);
                }});
            """)
            res = _run_node(script, env_extra={
                "AG_TOOLS_DIR": str(tools_dir),
                "AG_GEMINI_HOME": str(gemini_dir),
                "USERPROFILE": str(tmp_path),
                "HOME": str(tmp_path),
            })
            self.assertTrue(res["ok"])

    def test_list_known_projects_discovers_non_git_and_gemini_config(self):
        """Verifies listKnownProjects() discovers non-git projects from ~/.gemini/config/projects/*.json
        (such as 'Контент мейкер' and 'Flow_vpn') and retains them with isGit=false."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            projects_dir = fake_home / ".gemini" / "config" / "projects"
            projects_dir.mkdir(parents=True, exist_ok=True)
            cm_dir = ROOT_DIR / "source"
            flow_dir = ROOT_DIR / "extensions"
            (projects_dir / "cm.json").write_text(
                json.dumps({
                    "id": "proj-cm",
                    "name": "Контент мейкер",
                    "projectResources": {"resources": [{"folderUri": cm_dir.resolve().as_uri()}]},
                }),
                encoding="utf-8",
            )
            (projects_dir / "flow.json").write_text(
                json.dumps({
                    "id": "proj-flow",
                    "name": "Flow_vpn",
                    "projectResources": {"resources": [{"folderUri": flow_dir.resolve().as_uri()}]},
                }),
                encoding="utf-8",
            )

            script = textwrap.dedent(f"""
                const assert = require('assert');
                const path = require('path');
                const bridge = require({json.dumps(str(CORE_DIR / 'swarm_bridge.js'))});

                const projs = bridge.listKnownProjects();
                assert.ok(Array.isArray(projs), 'listKnownProjects must return an array');

                // 1. Verify 'Контент мейкер' discovered from ~/.gemini/config/projects/*.json
                const cm = projs.find(p => p.name === 'Контент мейкер' || /контент мейкер/i.test(p.name));
                assert.ok(cm, 'Non-git project "Контент мейкер" must be discovered from ~/.gemini/config/projects');
                assert.strictEqual(cm.isGit, false, 'Non-git folder "Контент мейкер" must have isGit=false');
                assert.ok(cm.path, 'Expected project path for "Контент мейкер"');

                // 2. Verify 'Flow_vpn' discovered from ~/.gemini/config/projects/*.json
                const flow = projs.find(p => p.name === 'Flow_vpn' || /flow_vpn/i.test(p.name));
                assert.ok(flow, 'Non-git project "Flow_vpn" must be discovered from ~/.gemini/config/projects');
                assert.strictEqual(flow.isGit, false, 'Non-git folder "Flow_vpn" must have isGit=false');
                assert.ok(flow.path, 'Expected project path for "Flow_vpn"');

                // 3. Verify candidates with isGit=false are retained
                const nonGitProjects = projs.filter(p => p && p.isGit === false);
                assert.ok(nonGitProjects.length >= 2, 'Non-git projects must be retained in collected projects');

                console.log(JSON.stringify({{ ok: true, count: projs.length, nonGitCount: nonGitProjects.length }}));
                process.exit(0);
            """)
            res = _run_node(
                script,
                env_extra={
                    "HOME": str(fake_home),
                    "USERPROFILE": str(fake_home),
                    "AG_GEMINI_DIR": str(fake_home / ".gemini"),
                    "AG_TOOLS_DIR": str(fake_home / ".antigravity_tools"),
                },
            )
            self.assertTrue(res["ok"])

    def test_format_agent_pill_text_prioritizes_active_agents_over_completion(self):
        """Verifies formatAgentPillText always prioritizes running agent ticker over recent completion state
        when active_agents > 0."""
        script = textwrap.dedent(f"""
            const assert = require('assert');
            const bridge = require({json.dumps(str(CORE_DIR / 'swarm_bridge.js'))});

            // 1. Multi-project active snapshot with non-null recentCompletionState
            const multiActiveSnapshot = {{
                summary: {{ active_agents: 2, active_projects: 2, active_sessions: 1 }},
                projects: [
                    {{ project_name: 'Flow_vpn', active_agents: 1, active_sessions: 1 }},
                    {{ project_name: 'threads', active_agents: 1, active_sessions: 0 }}
                ]
            }};
            const completedState = {{
                projectName: 'PastCompletedProject',
                done: 3,
                total: 3,
                isError: false,
                duration_seconds: 45
            }};

            const activeText = bridge.formatAgentPillText(multiActiveSnapshot, completedState);
            assert.strictEqual(
                activeText,
                '⚡ 2 аг. · 2 проекта · 1 задача',
                'formatAgentPillText must return active ticker when active_agents > 0 even if recentCompletionState is set'
            );

            // 2. Single active project snapshot with non-null recentCompletionState
            const singleActiveSnapshot = {{
                summary: {{ active_agents: 1, active_projects: 1, active_sessions: 1 }},
                projects: [
                    {{ project_name: 'Flow_vpn', active_agents: 1, active_sessions: 1, active_started_at_ms: Date.now() - 5000 }}
                ]
            }};
            const singleText = bridge.formatAgentPillText(singleActiveSnapshot, completedState);
            assert.ok(
                singleText.startsWith('⚡ 1 аг. · Flow_vpn'),
                'Single active project must prioritize active ticker over completedState: ' + singleText
            );

            // 3. Idle snapshot (active_agents === 0) returns completed state
            const idleSnapshot = {{
                summary: {{ active_agents: 0, active_projects: 0, active_sessions: 0 }},
                projects: []
            }};
            const doneText = bridge.formatAgentPillText(idleSnapshot, completedState);
            assert.strictEqual(
                doneText,
                '✓ Готово: PastCompletedProject (3/3 · 45с)',
                'formatAgentPillText must return completed state when active_agents === 0'
            );

            // 4. Idle snapshot with no recent completion returns idle ticker
            const idleZeroText = bridge.formatAgentPillText(idleSnapshot, null);
            assert.strictEqual(idleZeroText, '⚡ Агенты: 0');

            console.log(JSON.stringify({{ ok: true }}));
            process.exit(0);
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])


class ManagerBinaryPatcherTests(unittest.TestCase):
    @staticmethod
    def _build_synthetic_pe64_manager() -> tuple[bytes, int, list[int], int]:
        """Build a minimal valid PE32+ buffer containing MANAGER_GATE_X64 and pre/post-LEA KeyringTokenStorage sites."""
        pe_off = 0x80
        opt_sz = 0xF0
        sec_off = pe_off + 24 + opt_sz
        text_rp = 0x400
        text_va = 0x1000
        text_rsz = 0x400
        rdata_rp = 0x800
        rdata_va = 0x2000
        rdata_rsz = 0x400

        buf = bytearray(0xC00)
        buf[0:2] = b"MZ"
        struct.pack_into("<I", buf, 0x3C, pe_off)
        buf[pe_off:pe_off + 4] = b"PE\x00\x00"
        struct.pack_into("<HH", buf, pe_off + 4, 0x8664, 2)  # AMD64, 2 sections
        struct.pack_into("<H", buf, pe_off + 20, opt_sz)
        struct.pack_into("<H", buf, pe_off + 24, 0x20B)  # PE32+

        # .text section header
        buf[sec_off:sec_off + 8] = b".text\x00\x00\x00"
        struct.pack_into("<IIII", buf, sec_off + 8, 0x200, text_va, text_rsz, text_rp)
        # .rdata section header (VirtualSize=0x180 < SizeOfRawData=0x400)
        s2 = sec_off + 40
        buf[s2:s2 + 8] = b".rdata\x00\x00"
        struct.pack_into("<IIII", buf, s2 + 8, 0x180, rdata_va, rdata_rsz, rdata_rp)

        # .rdata strings strictly within VirtualSize (< rdata_rp + 0x180)
        blob_off = rdata_rp + 0x10
        blob_str = b"elf'jetski1.11.0geministubby"
        buf[blob_off:blob_off + len(blob_str)] = blob_str
        jetski_off = blob_off + blob_str.index(b"jetski")
        gemini_off = blob_off + blob_str.index(b"gemini")
        gemini_rva = rdata_va + (gemini_off - rdata_rp)

        tok_off = rdata_rp + 0x60
        tok_str = b"jetski-standalone-oauth-token\x00ineligible\x00"
        buf[tok_off:tok_off + len(tok_str)] = tok_str

        # .text: MANAGER_GATE_X64 original bytes + KeyringTokenStorage amd64 sequences
        gate_off = text_rp + 0x10
        gate_sig = b"\x80\x78\x08\x00\x74\x12\x48\x8b\x44\x24\x20\x48\x89\x45\x60"
        buf[gate_off:gate_off + len(gate_sig)] = gate_sig

        # Site 1 (pre-LEA): mov ecx, 6 ; lea rbx, [rip + disp32] ; nop ; call rdx
        mov_off = text_rp + 0x50
        buf[mov_off:mov_off + 5] = b"\xb9\x06\x00\x00\x00"
        lea_pre = mov_off + 5
        lea_next_rva = text_va + ((lea_pre + 7) - text_rp)
        buf[lea_pre:lea_pre + 3] = b"\x48\x8d\x1d"
        struct.pack_into("<i", buf, lea_pre + 3, gemini_rva - lea_next_rva)
        buf[lea_pre + 7] = 0x90
        buf[lea_pre + 8:lea_pre + 10] = b"\xff\xd2"

        # Site 2 (post-LEA, matching Go amd64 compiler layout): lea rbx, [rip + disp32] ; mov ecx, 6 ; call rdx
        lea_post = text_rp + 0x90
        lea_post_next_rva = text_va + ((lea_post + 7) - text_rp)
        buf[lea_post:lea_post + 3] = b"\x48\x8d\x1d"
        struct.pack_into("<i", buf, lea_post + 3, gemini_rva - lea_post_next_rva)
        buf[lea_post + 7:lea_post + 12] = b"\xb9\x06\x00\x00\x00"
        buf[lea_post + 12:lea_post + 14] = b"\xff\xd2"

        return bytes(buf), gate_off, [lea_pre, lea_post], jetski_off

    def test_pe64_ls_isolation_backup_preserve_and_restore(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        from patcher.manager import patcher as mgr_patcher

        raw_pe, gate_off, lea_offs, jetski_off = self._build_synthetic_pe64_manager()

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "Antigravity"
            bin_dir = app_root / "resources" / "bin"
            res_dir = app_root / "resources"
            gemini_dir = Path(tmp) / "gemini"
            tools_dir = Path(tmp) / "tools"
            bin_dir.mkdir(parents=True, exist_ok=True)
            gemini_dir.mkdir(parents=True, exist_ok=True)
            tools_dir.mkdir(parents=True, exist_ok=True)

            # Seed standalone token once on first isolation upgrade when standalone isn't set
            (gemini_dir / "jetski-standalone-oauth-token").write_text('{"token":{"refresh_token":"1//0init"}}', encoding="utf-8")

            mgr_bin = bin_dir / "language_server.exe"
            mgr_bin.write_bytes(raw_pe)
            asar_path = res_dir / "app.asar"
            _build_asar(
                asar_path,
                {
                    "package.json": b'{"version":"2.0.0"}',
                    "dist/utils.js": b"const handleDidFinishLoad = () => {\n    void applyStoredZoomLevel();\n};\n",
                    "dist/preload.js": b"electron_1.contextBridge.exposeInMainWorld('wsl', wslAPI);\n",
                    "dist/ipcHandlers.js": b"function registerIpcHandlers(storageManager) {\n    (0, wcvIpcHandlers_1.registerWcvIpcHandlers)();\n}\n",
                },
            )

            self.assertEqual(mgr_patcher.get_status(str(mgr_bin))[0], "unpatched")
            self.assertFalse(mgr_patcher.is_ls_isolated(raw_pe))
            self.assertFalse(mgr_patcher.is_already_patched(str(mgr_bin)))

            env_patch = {
                "AG_SKIP_WINCRED": "1",
                "AG_GEMINI_DIR": str(gemini_dir),
                "AG_TOOLS_DIR": str(tools_dir),
            }
            with mock.patch.dict(os.environ, env_patch):
                mgr_patcher.do_patch_manager(str(mgr_bin), kill_on_lock=False)

            patched_bytes = mgr_bin.read_bytes()
            self.assertTrue(mgr_patcher.is_already_patched(str(mgr_bin)))
            self.assertTrue(mgr_patcher.is_ls_isolated(patched_bytes))
            self.assertIn(b"jetski-desktopapp-oauth-token", patched_bytes)
            self.assertNotIn(b"jetski-standalone-oauth-token", patched_bytes)
            self.assertIn(b"inexigible", patched_bytes)
            self.assertEqual(patched_bytes[gate_off:gate_off + 6], mgr_patcher.MANAGER_GATE_X64.fix)
            self.assertTrue((gemini_dir / "jetski-desktopapp-oauth-token").is_file())

            secs = mgr_patcher._parse_pe64_sections(patched_bytes)
            self.assertIsNotNone(secs)
            assert secs is not None
            self.assertEqual(mgr_patcher._find_pe_keyring_sites(patched_bytes, secs, b"gemini"), [])
            self.assertEqual(mgr_patcher._find_pe_keyring_sites(patched_bytes, secs, b"jetski"), lea_offs)

            # Verify every LEA displacement points strictly at .rdata's "jetski" (< VirtualSize)
            for lea_off in lea_offs:
                new_disp = struct.unpack_from("<i", patched_bytes, lea_off + 3)[0]
                target_rva = mgr_patcher._raw_to_rva(secs, lea_off + 7) + new_disp
                self.assertEqual(mgr_patcher._rva_to_raw(secs, target_rva), jetski_off)

            # Simulate upgrading an OLD gate-patched binary that already has clean .agybak:
            # existing .agybak must be preserved rather than overwritten by half-patched binary
            half_patched = bytearray(raw_pe)
            half_patched[gate_off:gate_off + 6] = mgr_patcher.MANAGER_GATE_X64.fix
            mgr_bin.write_bytes(bytes(half_patched))
            self.assertEqual(mgr_patcher.get_status(str(mgr_bin))[0], "patched")
            self.assertFalse(mgr_patcher.is_already_patched(str(mgr_bin)))

            bak_path = Path(str(mgr_bin) + mgr_patcher.BAK_EXT)
            self.assertEqual(bak_path.read_bytes(), raw_pe)

            with mock.patch.dict(os.environ, env_patch):
                mgr_patcher.do_patch_manager(str(mgr_bin), kill_on_lock=False)

            self.assertEqual(bak_path.read_bytes(), raw_pe)
            self.assertTrue(mgr_patcher.is_already_patched(str(mgr_bin)))

            # Full restore recovers original binary & app.asar
            mgr_patcher.do_restore_manager(str(mgr_bin), force=True)
            self.assertEqual(mgr_bin.read_bytes(), raw_pe)
            self.assertFalse(mgr_patcher.is_already_patched(str(mgr_bin)))

    def test_pe_executable_ranges_and_cli_v14_patch_and_restore(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        from patcher.agy import patcher as agy_patcher
        from patcher.utils.file import get_executable_ranges

        # 1. Test get_executable_ranges error handling
        self.assertIsNone(get_executable_ranges(""))
        self.assertIsNone(get_executable_ranges("non_existent_file.exe"))

        with tempfile.TemporaryDirectory() as tmp:
            empty_file = Path(tmp) / "empty.exe"
            empty_file.write_bytes(b"MZ" + b"\x00" * 20)
            self.assertIsNone(get_executable_ranges(str(empty_file)))

            # 2. Build synthetic PE with .text (executable) and .rdata (non-executable)
            pe_off = 0x80
            opt_sz = 0xF0
            sec_off = pe_off + 24 + opt_sz
            text_rp = 0x400
            text_va = 0x1000
            text_rsz = 0x400
            rdata_rp = 0x800
            rdata_va = 0x2000
            rdata_rsz = 0x400

            buf = bytearray(0xC00)
            buf[0:2] = b"MZ"
            struct.pack_into("<I", buf, 0x3C, pe_off)
            buf[pe_off:pe_off + 4] = b"PE\x00\x00"
            struct.pack_into("<HH", buf, pe_off + 4, 0x8664, 2)
            struct.pack_into("<H", buf, pe_off + 20, opt_sz)
            struct.pack_into("<H", buf, pe_off + 24, 0x20B)

            # .text header with IMAGE_SCN_MEM_EXECUTE (0x20000000)
            buf[sec_off:sec_off + 8] = b".text\x00\x00\x00"
            struct.pack_into("<IIII", buf, sec_off + 8, 0x200, text_va, text_rsz, text_rp)
            struct.pack_into("<I", buf, sec_off + 36, 0x60000020)  # EXECUTE | READ | CODE

            # .rdata header without EXECUTE
            s2 = sec_off + 40
            buf[s2:s2 + 8] = b".rdata\x00\x00"
            struct.pack_into("<IIII", buf, s2 + 8, 0x180, rdata_va, rdata_rsz, rdata_rp)
            struct.pack_into("<I", buf, s2 + 36, 0x40000040)  # READ | INITIALIZED_DATA

            # Inject CLI_GATE_X64_V14 signature into .text
            cli_v14_sig = (
                b"\x48\x85\xc9\x0f\x84\x1e\x02\x00\x00"
                b"\x80\x79\x08\x00\x0f\x85\x14\x02\x00\x00"
                b"\x48\x89\xc8\x90\xe8\xfb\x15\xfc\xff"
                b"\x48\x89\x84\x24\xe0\x00\x00\x00"
                b"\x48\x89\x9c\x24\xe8\x00\x00\x00"
                b"\x48\x89\x8c\x24\xf0\x00\x00\x00"
            )
            v14_gate_off = text_rp + 0x20
            buf[v14_gate_off:v14_gate_off + len(cli_v14_sig)] = cli_v14_sig

            # Put string 'ineligible' in .text
            buf[text_rp + 0x100:text_rp + 0x10a] = b"ineligible"

            pe_file = Path(tmp) / "agy.exe"
            pe_file.write_bytes(buf)

            # Test get_executable_ranges on synthetic PE
            ranges = get_executable_ranges(str(pe_file))
            self.assertEqual(ranges, ((text_rp, text_rp + text_rsz),))

            # Test agy_patcher status detection
            status, _ = agy_patcher.get_status(str(pe_file))
            self.assertEqual(status, "unpatched")

            # Patch synthetic binary
            with mock.patch("patcher.utils.captcha.confirm_with_captcha", return_value=True):
                agy_patcher.do_patch_agy(str(pe_file))

            patched_data = pe_file.read_bytes()
            # Verify gate was patched with \x48\x85\xc9\x90 at offset 9
            self.assertEqual(patched_data[v14_gate_off + 9:v14_gate_off + 13], b"\x48\x85\xc9\x90")
            self.assertIn(b"inexigible", patched_data)
            self.assertNotIn(b"ineligible", patched_data)
            self.assertEqual(agy_patcher.get_status(str(pe_file))[0], "patched")

            # Test restore
            agy_patcher.do_restore_agy(str(pe_file), force=True)
            self.assertEqual(pe_file.read_bytes(), buf)
            self.assertEqual(agy_patcher.get_status(str(pe_file))[0], "unpatched")


class PatcherCliBackNavigationTests(unittest.TestCase):
    def test_pause_and_submenu_enter_back_navigation(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        from patcher import cli, data_reset
        from patcher import nrpt_dns

        # 1. pause() drains extended Windows scan keys cleanly and falls back to readline on non-console stdin
        fake_stdin = mock.MagicMock()
        with mock.patch.object(cli, "_has_win_console_stdin", return_value=False), mock.patch.object(
            sys, "stdin", fake_stdin
        ):
            cli.pause("")
        fake_stdin.readline.assert_called_once()

        seq = [b"\xe0", b"H"]
        kb_seq = [False, True, False]
        fake_msvcrt = type("FakeMsvcrt", (), {
            "kbhit": staticmethod(lambda: kb_seq.pop(0) if kb_seq else False),
            "getch": staticmethod(lambda: seq.pop(0)),
        })()
        with mock.patch.object(cli, "_has_win_console_stdin", return_value=True), mock.patch.dict(
            sys.modules, {"msvcrt": fake_msvcrt}
        ):
            cli.pause("")
        self.assertEqual(seq, [])

        # 2. offer_download_and_block cancel with Enter / b marks _last_offer_cancelled=True
        for cancel_key in ("", "0", "b", "back"):
            with mock.patch("builtins.input", return_value=cancel_key):
                res = cli.offer_download_and_block("Antigravity 2.0", target_type="manager")
            self.assertIsNone(res)
            self.assertTrue(cli._last_offer_cancelled)

        # 3. data_reset.do_data_reset returns False when cancelled at _choose_app
        with mock.patch.object(
            data_reset,
            "_get_data_dirs",
            return_value=[("Antigravity IDE", Path("a")), ("Antigravity 2.0", Path("b"))],
        ), mock.patch("builtins.input", return_value=""):
            self.assertIs(data_reset.do_data_reset(), False)

        # 4. Option 14 (Selective DNS & Routing) Enter / Private IP Enter returns straight back to main menu without pause(),
        #    and verify shifted menu numbering (14=NRPT, 15..19=TOOLS)
        inputs = iter(["14", "", "14", "2", "", "0"])
        recorded_rows = []
        orig_row = cli.print_menu_row

        def _capture_row(num, label, desc="", *args, **kwargs):
            recorded_rows.append((str(num), str(label), str(desc)))
            return orig_row(num, label, desc, *args, **kwargs)

        with mock.patch.object(sys, "argv", ["cli.py"]), mock.patch.object(
            cli, "find_install_root", return_value=""
        ), mock.patch.object(cli, "find_manager_binary", return_value=""), mock.patch.object(
            cli, "find_agy_binary", return_value=""
        ), mock.patch.object(cli, "find_extension_js", return_value=""), mock.patch.object(
            cli, "find_gemini_antigravity_binary", return_value=""
        ), mock.patch.object(cli, "redraw_main_screen"), mock.patch.object(
            cli, "clear_screen"
        ), mock.patch.object(cli, "print_banner"), mock.patch.object(
            cli, "print_menu_row", side_effect=_capture_row
        ), mock.patch.object(
            cli, "pause"
        ) as pause_mock, mock.patch.object(
            nrpt_dns, "is_nrpt_supported", return_value=True
        ), mock.patch.object(
            nrpt_dns, "get_nrpt_status", return_value={"active": False}
        ), mock.patch.object(
            nrpt_dns, "get_dns_config", return_value={}
        ), mock.patch("patcher.auto_repatch.sync_auto_repatch_state"), mock.patch(
            "builtins.input", side_effect=lambda *_a, **_kw: next(inputs)
        ):
            cli.run_cli()
        pause_mock.assert_not_called()

        main_menu_map = {}
        for num, label, _desc in recorded_rows:
            if num in {"14", "15", "16", "17", "18", "19", "21"} and num not in main_menu_map:
                main_menu_map[num] = label
        self.assertIn("Selective DNS & Routing", main_menu_map.get("14", ""))
        self.assertEqual(main_menu_map.get("15"), "Open GitHub repository")
        self.assertEqual(main_menu_map.get("16"), "Select custom path")
        self.assertEqual(main_menu_map.get("17"), "About program")
        self.assertEqual(main_menu_map.get("18"), "Install Antigravity Assistant")
        self.assertEqual(main_menu_map.get("19"), "Check for cloud updates")
        self.assertNotIn("21", main_menu_map)

        # 5. Shifted handlers: 15->GitHub, 16->Custom Path Enter back, 17->About, 14/1->public NRPT, 14/2->private NRPT, 14/3->disable failure check
        inputs2 = iter(["15", "16", "", "17", "14", "1", "14", "2", "my.dns.example", "14", "3", "0"])
        with mock.patch.object(sys, "argv", ["cli.py"]), mock.patch.object(
            cli, "find_install_root", return_value=""
        ), mock.patch.object(cli, "find_manager_binary", return_value=""), mock.patch.object(
            cli, "find_agy_binary", return_value=""
        ), mock.patch.object(cli, "find_extension_js", return_value=""), mock.patch.object(
            cli, "find_gemini_antigravity_binary", return_value=""
        ), mock.patch.object(cli, "redraw_main_screen"), mock.patch.object(
            cli, "clear_screen"
        ), mock.patch.object(cli, "print_banner"), mock.patch.object(
            cli, "pause"
        ), mock.patch.object(
            cli, "show_about"
        ) as about_mock, mock.patch.object(
            cli.webbrowser, "open"
        ) as wb_mock, mock.patch.object(
            nrpt_dns, "is_nrpt_supported", return_value=True
        ), mock.patch.object(
            nrpt_dns, "get_nrpt_status", return_value={"active": False}
        ), mock.patch.object(
            nrpt_dns, "get_dns_config", return_value={}
        ), mock.patch.object(
            nrpt_dns, "enable_nrpt_rule", return_value=(True, "ok")
        ) as en_nrpt_mock, mock.patch.object(
            nrpt_dns, "disable_nrpt_rule", return_value=(False, "Administrator rights required")
        ) as dis_nrpt_mock, mock.patch.object(
            cli, "err"
        ) as err_mock, mock.patch(
            "builtins.input", side_effect=lambda *_a, **_kw: next(inputs2)
        ):
            cli.run_cli()
        wb_mock.assert_called_once()
        about_mock.assert_called_once()
        self.assertEqual(en_nrpt_mock.call_count, 2)
        self.assertEqual(en_nrpt_mock.call_args_list[0].kwargs.get("mode"), "public")
        self.assertEqual(en_nrpt_mock.call_args_list[1].args[0], "my.dns.example")
        self.assertEqual(en_nrpt_mock.call_args_list[1].kwargs.get("mode"), "private")
        dis_nrpt_mock.assert_called_once()
        err_mock.assert_any_call("Administrator rights required")


class DiagnosticsAndNrptTests(unittest.TestCase):
    def test_nrpt_validation_hostname_resolution_and_failure_honesty(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        from patcher import cli, nrpt_dns

        # IPv4/IPv6 accepted verbatim
        self.assertEqual(nrpt_dns._resolve_dns_target_ip("176.108.113.214"), ("176.108.113.214", None))
        # Malicious or malformed inputs rejected before PowerShell
        bad_ip, bad_err = nrpt_dns._resolve_dns_target_ip("1.1.1.1; whoami")
        self.assertIsNone(bad_ip)
        self.assertIn("Invalid", bad_err or "")
        self.assertIsNone(nrpt_dns._resolve_dns_target_ip("")[0])

        # Hostname resolution to IPv4 works cleanly
        fake_addr = [(2, 2, 17, "", ("9.9.9.9", 0))]
        with mock.patch.object(nrpt_dns.socket, "getaddrinfo", return_value=fake_addr):
            resolved, r_err = nrpt_dns._resolve_dns_target_ip("dns.example.org")
        self.assertEqual((resolved, r_err), ("9.9.9.9", None))

        # get_preferred_dns_ip respects mode=private vs public
        self.assertEqual(
            nrpt_dns.get_preferred_dns_ip({"mode": "private", "custom_ip": "10.20.30.40", "server_ip": "176.108.113.214"}),
            "10.20.30.40",
        )
        self.assertEqual(
            nrpt_dns.get_preferred_dns_ip({"mode": "public", "custom_ip": "10.20.30.40", "server_ip": "176.108.113.214"}),
            "176.108.113.214",
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / ".antigravity_tools"
            cfg_file = cfg_dir / "dns_config.json"
            with mock.patch.object(nrpt_dns, "CONFIG_DIR", cfg_dir), mock.patch.object(
                nrpt_dns, "CONFIG_FILE", cfg_file
            ), mock.patch.object(nrpt_dns, "is_nrpt_supported", return_value=True), mock.patch.object(
                nrpt_dns, "_flush_dns_cache"
            ):
                # Hostname activation stores raw hostname in custom_ip and resolved IP in server_ip
                with mock.patch.object(nrpt_dns.socket, "getaddrinfo", return_value=fake_addr), mock.patch.object(
                    nrpt_dns, "_run_ps", return_value=(0, "")
                ), mock.patch.object(nrpt_dns, "check_dns_resolver_reachable", return_value=(True, None)):
                    ok_flag, msg = nrpt_dns.enable_nrpt_rule("dns.example.org", mode="private")
                self.assertTrue(ok_flag)
                self.assertIn("9.9.9.9", msg)
                saved = nrpt_dns.get_dns_config()
                self.assertTrue(saved["enabled"])
                self.assertEqual(saved["mode"], "private")
                self.assertEqual(saved["custom_ip"], "dns.example.org")
                self.assertEqual(saved["server_ip"], "9.9.9.9")

                # Failed removal must return False and preserve enabled=True when rule remains active
                with mock.patch.object(nrpt_dns, "_remove_existing_nrpt_rules", return_value=(1, "Access denied")), mock.patch.object(
                    nrpt_dns, "get_nrpt_status", return_value={"supported": True, "active": True, "server": "9.9.9.9"}
                ):
                    dis_ok, dis_msg = nrpt_dns.disable_nrpt_rule()
                self.assertFalse(dis_ok)
                self.assertIn("Could not remove", dis_msg)
                self.assertTrue(nrpt_dns.get_dns_config()["enabled"])

                # Live DNS reachability probe: unreachable server aborts activation without PowerShell call
                with mock.patch.object(nrpt_dns, "check_dns_resolver_reachable", return_value=(False, "timed out")):
                    with mock.patch.object(nrpt_dns, "_run_ps") as unreach_ps:
                        probe_ok, probe_msg = nrpt_dns.enable_nrpt_rule("176.108.113.214")
                self.assertFalse(probe_ok)
                self.assertIn("unreachable", probe_msg)
                unreach_ps.assert_not_called()

                # Dynamic toggle in submenu 14: pressing 1 while ACTIVE triggers disable_nrpt_rule
                active_inputs = iter(["14", "1", "0"])
                with mock.patch.object(sys, "argv", ["cli.py"]), mock.patch.object(
                    cli, "find_install_root", return_value=""
                ), mock.patch.object(cli, "find_manager_binary", return_value=""), mock.patch.object(
                    cli, "find_agy_binary", return_value=""
                ), mock.patch.object(cli, "find_extension_js", return_value=""), mock.patch.object(
                    cli, "find_gemini_antigravity_binary", return_value=""
                ), mock.patch.object(cli, "redraw_main_screen"), mock.patch.object(
                    cli, "clear_screen"
                ), mock.patch.object(cli, "print_banner"), mock.patch.object(
                    cli, "pause"
                ), mock.patch.object(
                    nrpt_dns, "is_nrpt_supported", return_value=True
                ), mock.patch.object(
                    nrpt_dns, "get_nrpt_status", return_value={"active": True, "server": "1.2.3.4"}
                ), mock.patch.object(
                    nrpt_dns, "disable_nrpt_rule", return_value=(True, "ok")
                ) as active_dis_mock, mock.patch(
                    "builtins.input", side_effect=lambda *_a, **_kw: next(active_inputs)
                ):
                    cli.run_cli()
                active_dis_mock.assert_called_once()

    def test_data_reset_scoped_process_detection(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        from patcher import data_reset

        fake_csv = '"Antigravity IDE.exe","1234","Console","1","120,000 K"\n'
        fake_proc = subprocess.CompletedProcess(args=["tasklist"], returncode=0, stdout=fake_csv, stderr="")
        with mock.patch.object(os, "name", "nt"), mock.patch.object(
            data_reset.subprocess, "run", return_value=fake_proc
        ):
            self.assertEqual(data_reset._is_ide_running("Antigravity IDE"), "Antigravity IDE.exe")
            # Running IDE must NOT trigger a kill prompt when resetting Antigravity 2.0
            self.assertIsNone(data_reset._is_ide_running("Antigravity 2.0"))

    def test_diagnostics_log_collection_and_autofix_accuracy(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        import importlib
        import warnings
        from patcher import diagnostics, nrpt_dns, ide, manager, agy, vscode
        from patcher.ide import patcher as ide_patcher, discovery as ide_disc
        from patcher.manager import patcher as mgr_patcher, discovery as mgr_disc
        from patcher.agy import patcher as agy_patcher, discovery as agy_disc
        from patcher.vscode import discovery as vsc_disc

        # 0. Zero DeprecationWarning on diagnostics module load
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", DeprecationWarning)
            importlib.reload(diagnostics)
        dep_warns = [x for x in w if issubclass(x.category, DeprecationWarning)]
        self.assertEqual(dep_warns, [])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ide_logs = tmp_path / "ide_logs" / "20261110T220000"
            ext_out = ide_logs / "window1" / "exthost" / "output_logging_20261110"
            ext_out.mkdir(parents=True, exist_ok=True)
            mgr_logs = tmp_path / "mgr_logs"
            (mgr_logs / "older_dir").mkdir(parents=True, exist_ok=True)

            # Noise + real errors across IDE nested & Antigravity 2.0 root logs
            (ide_logs / "auth.log").write_text(
                "2026-11-10 12:00:00 [error] [Auth] Error refreshing user status: reason: read ECONNRESET\n"
                "2026-11-10 12:00:01 [info] [Auth] Auth state changed to: loginError\n",
                encoding="utf-8",
            )
            (ext_out / "1-Antigravity LS.log").write_text(
                '[settings] => {"type":"postmessage-rpc","payload":{"text":"code 400 TLS handshake"}}\n',
                encoding="utf-8",
            )
            (ext_out / "2-Antigravity.log").write_text(
                "[LAUNCH ERROR] User location is not supported (code 400)\n",
                encoding="utf-8",
            )
            (mgr_logs / "main.log").write_text(
                "[error] Error: ENOENT: no such file or directory, open 'app-update.yml'\n",
                encoding="utf-8",
            )
            (mgr_logs / "language_server.log").write_text(
                "ERROR: logging before google.Init: I1110 22:47:36 server.go:1888] Starting language server\n"
                'ERROR: logging before google.Init: W1110 22:53:55 cache.go:146] Singleflight refresh failed: read tcp 1.2.3.4: wsarecv: failed\n',
                encoding="utf-8",
            )

            with mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[tmp_path / "ide_logs", mgr_logs]):
                collected = diagnostics.collect_log_lines()

            collected_text = "\n".join(line for _, _, line in collected)
            self.assertIn("User location is not supported", collected_text)
            self.assertIn("Singleflight refresh failed", collected_text)
            self.assertIn("read ECONNRESET", collected_text)
            self.assertNotIn("postmessage-rpc", collected_text)
            self.assertNotIn("app-update.yml", collected_text)
            self.assertNotIn("Starting language server", collected_text)

            # Verify log-only match maps 400 + network/auth errors cleanly (no false tls_expired from postmessage-rpc)
            found = diagnostics.analyze_logs(collected, diagnostics._make_error_db())
            found_keys = {ke.key for ke in found}
            self.assertIn("user_location_400", found_keys)
            self.assertIn("auth_failure", found_keys)
            self.assertIn("cache_refresh_fail", found_keys)
            self.assertNotIn("tls_expired", found_keys)

            # Prepare fake main.js + manager + agy and verify do_patch returning None still counts as SUCCESS when post-state is patched
            fake_main = tmp_path / "main.js"
            fake_main.write_text("resetIsTierGCPTos(),this.foo.isGoogleInternal", encoding="utf-8")
            fake_mgr = tmp_path / "language_server.exe"
            fake_mgr.write_bytes(b"fake")
            fake_agy = tmp_path / "agy.exe"
            fake_agy.write_bytes(b"fake")
            mgr_state = {"patched": False}
            agy_state = {"patched": False}

            def _fake_do_patch(p, show_search_line=False):
                Path(p).write_text("resetIsTierGCPTos(),true", encoding="utf-8")
                return None

            def _fake_do_patch_mgr(p):
                mgr_state["patched"] = True
                return None

            def _fake_do_patch_agy(p):
                agy_state["patched"] = True
                return None

            import contextlib

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[tmp_path / "ide_logs", mgr_logs]))
                stack.enter_context(mock.patch.object(ide_disc, "find_install_root", return_value=""))
                stack.enter_context(mock.patch.object(mgr_disc, "find_manager_binary", return_value=""))
                stack.enter_context(mock.patch.object(agy_disc, "find_agy_binary", return_value=""))
                stack.enter_context(mock.patch.object(vsc_disc, "find_gemini_antigravity_binary", return_value=""))
                stack.enter_context(mock.patch.object(ide_patcher, "do_patch", side_effect=_fake_do_patch))
                stack.enter_context(mock.patch.object(mgr_patcher, "do_patch_manager", side_effect=_fake_do_patch_mgr))
                stack.enter_context(mock.patch.object(mgr_patcher, "is_already_patched", side_effect=lambda _p: mgr_state["patched"]))
                stack.enter_context(mock.patch.object(agy_patcher, "do_patch_agy", side_effect=_fake_do_patch_agy))
                stack.enter_context(mock.patch.object(agy_patcher, "is_already_patched", side_effect=lambda _p: agy_state["patched"]))
                stack.enter_context(mock.patch.object(nrpt_dns, "get_nrpt_status", return_value={"supported": True, "active": False}))
                stack.enter_context(mock.patch("patcher.hosts_pin.is_hosts_pinned", return_value=(False, "")))
                stack.enter_context(mock.patch("patcher.hosts_pin.enable_hosts_pinning", return_value=(False, "denied")))
                stack.enter_context(mock.patch.object(nrpt_dns, "get_preferred_dns_ip", return_value="176.108.113.214"))
                nrpt_en = stack.enter_context(mock.patch.object(nrpt_dns, "enable_nrpt_rule", return_value=(True, "176.108.113.214")))
                flush_mock = stack.enter_context(mock.patch.object(nrpt_dns, "_flush_dns_cache"))
                stack.enter_context(mock.patch("patcher.tls_fix.is_tls_fix_applied", return_value=True))
                stack.enter_context(mock.patch.object(diagnostics, "_check_cloudcode_connectivity", return_value={"reachable": True, "regional_400": False}))
                stack.enter_context(mock.patch.object(diagnostics, "_detect_running_antigravity_apps", return_value={"ide": False, "manager": False}))
                stack.enter_context(mock.patch.object(diagnostics, "save_report", return_value=str(tmp_path / "report.txt")))
                diagnostics.do_diagnostics(str(fake_main), str(fake_mgr), str(fake_agy))

            self.assertEqual(fake_main.read_text(encoding="utf-8"), "resetIsTierGCPTos(),true")
            self.assertTrue(mgr_state["patched"])
            self.assertTrue(agy_state["patched"])
            if sys.platform == "win32":
                nrpt_en.assert_called_once_with("176.108.113.214")
                flush_mock.assert_called()

            # Verify failure accuracy: if do_patch returns None WITHOUT patching AND do_data_reset is cancelled (False), neither is falsely marked applied;
            # and shared failing fix_fn (_fix_tls for tls_expired + ls_not_initialized) is only executed once (failed_keys)!
            fake_main.write_text("resetIsTierGCPTos(),this.foo.isGoogleInternal", encoding="utf-8")
            db = diagnostics._make_error_db()
            by_key = {ke.key: ke for ke in db}
            ctx = {"main_js_path": str(fake_main), "manager_path": "", "agy_path": "", "vscode_agy_path": ""}
            with mock.patch.object(ide_disc, "find_install_root", return_value=""), mock.patch.object(
                mgr_disc, "find_manager_binary", return_value=""
            ), mock.patch.object(agy_disc, "find_agy_binary", return_value=""), mock.patch.object(
                vsc_disc, "find_gemini_antigravity_binary", return_value=""
            ), mock.patch.object(ide_patcher, "do_patch", return_value=None), mock.patch.object(
                nrpt_dns, "get_nrpt_status", return_value={"supported": True, "active": False}
            ), mock.patch("patcher.hosts_pin.is_hosts_pinned", return_value=(False, "")), mock.patch(
                "patcher.hosts_pin.enable_hosts_pinning", return_value=(False, "denied")
            ), mock.patch.object(nrpt_dns, "enable_nrpt_rule", return_value=(False, "denied")):
                if sys.platform == "win32":
                    self.assertFalse(by_key["user_location_400"].fix_fn(ctx))
                self.assertFalse(by_key["is_google_internal"].fix_fn(ctx))

            with mock.patch.object(ide_disc, "find_install_root", return_value=""), mock.patch.object(
                mgr_disc, "find_manager_binary", return_value=""
            ), mock.patch("patcher.data_reset.do_data_reset", return_value=False):
                self.assertFalse(by_key["resource_exhausted_429"].fix_fn(ctx))

            # Duplicate failure suppression via failed_keys in do_diagnostics
            tls_calls = []
            with mock.patch.object(
                diagnostics,
                "collect_log_lines",
                return_value=[("s", "ls-main.log", "TLS handshake error"), ("s", "ls-main.log", "LS check error")],
            ), mock.patch("patcher.tls_fix.is_tls_fix_applied", return_value=False), mock.patch(
                "patcher.tls_fix.do_tls_fix_inject",
                side_effect=lambda *_a: (tls_calls.append(1), False)[1],
            ), mock.patch.object(
                diagnostics, "_detect_running_antigravity_apps", return_value={"ide": False, "manager": False}
            ), mock.patch.object(diagnostics, "save_report", return_value=str(tmp_path / "rep2.txt")):
                diagnostics.do_diagnostics(str(fake_main), "", "")
            self.assertEqual(len(tls_calls), 1)

    def test_write_atomic_and_instance_guard(self):
        from patcher.utils.atomic import write_atomic, sweep_stale_temps, TEMP_SUFFIX
        from patcher.utils.instance import acquire_instance_lock, release_instance_lock
        import time

        with tempfile.TemporaryDirectory() as td:
            target_file = os.path.join(td, "test_binary.bin")
            original_content = b"original binary content v1"
            write_atomic(target_file, original_content)
            self.assertTrue(os.path.isfile(target_file))
            with open(target_file, "rb") as f:
                self.assertEqual(f.read(), original_content)

            # Atomic update
            updated_content = b"updated binary content v2"
            write_atomic(target_file, updated_content)
            with open(target_file, "rb") as f:
                self.assertEqual(f.read(), updated_content)

            # Stale temp sweep test
            stale_temp = os.path.join(td, f"stale_file.1234{TEMP_SUFFIX}")
            with open(stale_temp, "wb") as f:
                f.write(b"abandoned junk")
            past_time = time.time() - 1000
            os.utime(stale_temp, (past_time, past_time))
            removed = sweep_stale_temps(td, max_age_seconds=600)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(stale_temp))

        # Single instance guard
        test_mutex_name = "Local\\TestAntigravityPatcher_Unit_Test"
        try:
            acquired = acquire_instance_lock(name=test_mutex_name)
            self.assertTrue(acquired)

            # Second acquire with same name should fail
            if os.name == "nt":
                acquired_second = acquire_instance_lock(name=test_mutex_name)
                self.assertFalse(acquired_second)
        finally:
            release_instance_lock()

        # After release, can acquire again
        try:
            acquired_third = acquire_instance_lock(name=test_mutex_name)
            self.assertTrue(acquired_third)
        finally:
            release_instance_lock()

    def test_hosts_pinning_and_substitution_check(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        from patcher.hosts_pin import (
            replace_hosts_block, is_hosts_pinned, enable_hosts_pinning,
            disable_hosts_pinning, HOSTS_COMMENT_BEGIN, HOSTS_COMMENT_END
        )
        from patcher.nrpt_dns import check_dns_substitution

        existing = "127.0.0.1 localhost\n# custom entry\n192.168.1.1 router\n"
        block = f"{HOSTS_COMMENT_BEGIN}\n193.233.112.67 cloudcode-pa.googleapis.com\n{HOSTS_COMMENT_END}\n"

        # Insertion
        merged = replace_hosts_block(existing, block)
        self.assertIn("127.0.0.1 localhost", merged)
        self.assertIn("192.168.1.1 router", merged)
        self.assertIn("193.233.112.67 cloudcode-pa.googleapis.com", merged)

        # Replacement / idempotence
        updated_block = f"{HOSTS_COMMENT_BEGIN}\n159.194.200.33 cloudcode-pa.googleapis.com\n{HOSTS_COMMENT_END}\n"
        re_merged = replace_hosts_block(merged, updated_block)
        self.assertEqual(re_merged.count(HOSTS_COMMENT_BEGIN), 1)
        self.assertIn("159.194.200.33", re_merged)
        self.assertNotIn("193.233.112.67", re_merged)

        # Removal
        stripped = replace_hosts_block(re_merged, None)
        self.assertEqual(stripped, existing)

        # File-based test with mocks
        with tempfile.TemporaryDirectory() as td:
            fake_hosts = Path(td) / "hosts"
            fake_hosts.write_text(existing, encoding="utf-8")

            with mock.patch("patcher.hosts_pin.get_hosts_path", return_value=fake_hosts), \
                 mock.patch("patcher.hosts_pin._flush_dns_cache"):
                # Initial state
                pinned, ip = is_hosts_pinned()
                self.assertFalse(pinned)

                # Enable
                ok, msg = enable_hosts_pinning(proxy_ip="193.233.112.67")
                self.assertTrue(ok)
                pinned, ip = is_hosts_pinned()
                self.assertTrue(pinned)
                self.assertEqual(ip, "193.233.112.67")

                # Disable
                ok, msg = disable_hosts_pinning()
                self.assertTrue(ok)
                pinned, ip = is_hosts_pinned()
                self.assertFalse(pinned)
                self.assertEqual(fake_hosts.read_text(encoding="utf-8"), existing)

        # Substitution check test with mock
        sub_resp = lambda srv, _d, **_kw: ["172.217.118.4"] if srv == "8.8.8.8" else ["193.233.112.67"]
        pass_resp = lambda srv, _d, **_kw: ["172.217.118.4"]
        with mock.patch("patcher.hosts_pin.resolve_domain_udp", side_effect=sub_resp) as r_mock, \
             mock.patch("extensions.core.hosts_pin.resolve_domain_udp", side_effect=sub_resp, create=True) as r_mock_core:
            # Substituted response
            is_sub, detail = check_dns_substitution("37.230.192.51")
            self.assertTrue(is_sub)
            self.assertIn("Substituted", detail)

            # Passthrough response
            r_mock.side_effect = pass_resp
            r_mock_core.side_effect = pass_resp
            is_sub, detail = check_dns_substitution("1.1.1.1")
            self.assertFalse(is_sub)
            self.assertIn("Passthrough", detail)

    def test_dead_proxy_dns_lookup_and_single_action_recommendations(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        import re
        import socket
        from patcher import diagnostics, nrpt_dns, hosts_pin
        from patcher.ide import patcher as ide_patcher, discovery as ide_disc
        from patcher.manager import patcher as mgr_patcher, discovery as mgr_disc
        from patcher.agy import discovery as agy_disc
        from patcher.vscode import discovery as vsc_disc

        # 1. Every rule in _make_error_db must expose a single concrete recommendation (no "или" and at most one [N] button)
        db = diagnostics._make_error_db()
        for ke in db:
            self.assertNotIn("или", ke.menu_button.lower(), f"Multi-choice 'или' found in {ke.key}: {ke.menu_button}")
            self.assertLessEqual(
                len(re.findall(r"\[\d+\]", ke.menu_button)),
                1,
                f"Multiple menu buttons found in {ke.key}: {ke.menu_button}",
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mgr_logs = tmp_path / "mgr_logs"
            mgr_logs.mkdir(parents=True, exist_ok=True)
            (mgr_logs / "language_server.log").write_text(
                'ERROR: logging before google.Init: W1111 00:30:03.079896       1 cache.go:56] Cache(loadCodeAssistResponse): '
                'Singleflight refresh failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": '
                'Post "https://oauth2.googleapis.com/token": proxyconnect tcp: dial tcp 127.0.0.1:53129: connectex: '
                'No connection could be made because the target machine actively refused it.\n'
                'Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": '
                'dial tcp: lookup daily-cloudcode-pa.googleapis.com: no such host\n',
                encoding="utf-8",
            )

            # 2. Collection and accurate classification of dead_proxy & dns_lookup_fail (not cache_refresh_fail)
            with mock.patch.object(diagnostics, "_get_log_base_dirs", return_value=[mgr_logs]):
                collected = diagnostics.collect_log_lines()
            found = diagnostics.analyze_logs(collected, diagnostics._make_error_db())
            found_keys = {ke.key for ke in found}
            self.assertIn("dead_proxy", found_keys)
            self.assertIn("dns_lookup_fail", found_keys)
            self.assertNotIn("cache_refresh_fail", found_keys)
            self.assertNotIn("auth_failure", found_keys)

            # 3. _fix_dead_proxy disables unreachable Windows registry proxy & flushes DNS
            by_key = {ke.key: ke for ke in diagnostics._make_error_db()}
            ctx = {
                "main_js_path": "",
                "manager_path": "",
                "agy_path": "",
                "vscode_agy_path": "",
                "_hits_by_key": {ke.key: list(ke.hits) for ke in found},
            }
            with mock.patch.object(diagnostics, "_read_windows_registry_proxy", return_value=(True, "127.0.0.1:53129")), mock.patch.object(
                diagnostics, "_check_tcp_reachable", return_value=False
            ), mock.patch.object(diagnostics, "_clear_dead_user_env_proxies", return_value=[]), mock.patch.object(
                diagnostics, "_get_dead_env_proxies", return_value=[]
            ), mock.patch.object(
                diagnostics, "_disable_windows_registry_proxy", return_value=True
            ) as disable_proxy_mock, mock.patch.object(nrpt_dns, "_flush_dns_cache") as flush_mock, mock.patch.object(
                socket, "getaddrinfo", return_value=[("ok",)]
            ):
                self.assertFalse(by_key["dead_proxy"].is_already_fixed_fn(ctx))
                self.assertTrue(by_key["dead_proxy"].fix_fn(ctx))
                disable_proxy_mock.assert_called_once()
                flush_mock.assert_called()

            # 3b. _fix_dead_proxy fallback when registry proxy already off & hit tuple unpacks safely, plus user-env proxy cleanup
            with mock.patch.object(diagnostics, "_read_windows_registry_proxy", return_value=(False, "")), mock.patch.object(
                diagnostics, "_clear_dead_user_env_proxies", return_value=[]
            ), mock.patch.object(diagnostics, "_get_dead_env_proxies", return_value=[]), mock.patch.object(
                diagnostics, "_check_tcp_reachable", return_value=False
            ), mock.patch.object(nrpt_dns, "_flush_dns_cache"), mock.patch.object(
                socket, "getaddrinfo", return_value=[("ok",)]
            ):
                self.assertTrue(by_key["dead_proxy"].fix_fn(ctx))

            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:53129"}), mock.patch.object(
                diagnostics, "_read_windows_registry_proxy", return_value=(False, "")
            ), mock.patch.object(
                diagnostics, "_clear_dead_user_env_proxies", return_value=[("HTTPS_PROXY", "127.0.0.1", 53129)]
            ), mock.patch.object(diagnostics, "_check_tcp_reachable", return_value=False), mock.patch.object(
                nrpt_dns, "_flush_dns_cache"
            ), mock.patch.object(socket, "getaddrinfo", return_value=[("ok",)]):
                self.assertTrue(by_key["dead_proxy"].fix_fn(ctx))
                self.assertNotIn("HTTPS_PROXY", os.environ)

            if sys.platform == "win32":
                # 4. _fix_dns_lookup enables NRPT DNS bypass + flushes DNS and honors failure
                with mock.patch.object(nrpt_dns, "get_nrpt_status", return_value={"supported": True, "active": False}), mock.patch.object(
                    hosts_pin, "is_hosts_pinned", return_value=(False, "")
                ), mock.patch.object(nrpt_dns, "get_preferred_dns_ip", return_value="176.108.113.214"), mock.patch.object(
                    nrpt_dns, "enable_nrpt_rule", return_value=(True, "176.108.113.214")
                ) as nrpt_en, mock.patch.object(nrpt_dns, "_flush_dns_cache") as flush_mock, mock.patch.object(
                    socket, "getaddrinfo", return_value=[("ok",)]
                ):
                    self.assertFalse(by_key["dns_lookup_fail"].is_already_fixed_fn(ctx))
                    self.assertTrue(by_key["dns_lookup_fail"].fix_fn(ctx))
                    nrpt_en.assert_called_once_with("176.108.113.214")
                    flush_mock.assert_called()

                with mock.patch.object(nrpt_dns, "get_nrpt_status", return_value={"supported": True, "active": False}), mock.patch.object(
                    hosts_pin, "is_hosts_pinned", return_value=(False, "")
                ), mock.patch.object(nrpt_dns, "get_preferred_dns_ip", return_value="176.108.113.214"), mock.patch.object(
                    nrpt_dns, "enable_nrpt_rule", return_value=(False, "denied")
                ), mock.patch.object(nrpt_dns, "_flush_dns_cache"), mock.patch.object(
                    socket, "getaddrinfo", side_effect=OSError("no such host")
                ):
                    self.assertFalse(by_key["dns_lookup_fail"].fix_fn(ctx))
                    self.assertEqual(by_key["dns_lookup_fail"].resolve_menu_button(ctx), "[14] Selective DNS & Routing")

            # 5. If [11] TLS fix is ALREADY applied on main.js and Antigravity 2.0 patch fails,
            # do_diagnostics MUST recommend [3] Antigravity 2.0 patch — NEVER [11] TLS certificate fix.
            fake_main = tmp_path / "main.js"
            fake_main.write_text("NODE_TLS_REJECT_UNAUTHORIZED _origWarn", encoding="utf-8")
            fake_mgr = tmp_path / "language_server.exe"
            fake_mgr.write_bytes(b"fake")
            saved_found = []

            def _capture_report(rep_found, _rep_logs):
                saved_found[:] = rep_found
                return str(tmp_path / "rep.txt")

            with mock.patch.object(
                diagnostics,
                "collect_log_lines",
                return_value=[("s", "language_server.log", "Singleflight refresh failed: read tcp 1.2.3.4: wsarecv: EOF")],
            ), mock.patch.object(ide_disc, "find_install_root", return_value=""), mock.patch.object(
                mgr_disc, "find_manager_binary", return_value=""
            ), mock.patch.object(agy_disc, "find_agy_binary", return_value=""), mock.patch.object(
                vsc_disc, "find_gemini_antigravity_binary", return_value=""
            ), mock.patch("patcher.tls_fix.is_tls_fix_applied", return_value=True), mock.patch.object(
                mgr_patcher, "is_already_patched", return_value=False
            ), mock.patch.object(mgr_patcher, "do_patch_manager", return_value=None), mock.patch.object(
                diagnostics, "_read_windows_registry_proxy", return_value=(False, "")
            ), mock.patch.object(
                diagnostics, "_detect_running_antigravity_apps", return_value={"ide": False, "manager": False}
            ), mock.patch.object(nrpt_dns, "_flush_dns_cache"), mock.patch.object(
                diagnostics, "save_report", side_effect=_capture_report
            ):
                diagnostics.do_diagnostics(str(fake_main), str(fake_mgr), "")

            self.assertEqual(len(saved_found), 1)
            cache_ke = saved_found[0]
            self.assertEqual(cache_ke.key, "cache_refresh_fail")
            self.assertEqual(cache_ke.menu_button, "[3] Antigravity 2.0 patch")
            self.assertNotIn("[11]", cache_ke.fix_status)

            # 6. ls_not_initialized with TLS already applied must not recommend [11]
            ctx_tls_done = {"main_js_path": str(fake_main), "manager_path": "", "agy_path": "", "vscode_agy_path": ""}
            with mock.patch("patcher.tls_fix.is_tls_fix_applied", return_value=True), mock.patch.object(
                mgr_disc, "find_manager_binary", return_value=""
            ):
                self.assertEqual(by_key["ls_not_initialized"].resolve_menu_button(ctx_tls_done), "[14] Selective DNS & Routing")

            if sys.platform == "win32":
                # 7. user_location_400 resolved dynamically to exact single failed target ([14] when only NRPT failed)
                with mock.patch.object(ide_disc, "find_install_root", return_value=""), mock.patch.object(
                    mgr_disc, "find_manager_binary", return_value=""
                ), mock.patch.object(agy_disc, "find_agy_binary", return_value=""), mock.patch.object(
                    vsc_disc, "find_gemini_antigravity_binary", return_value=""
                ), mock.patch.object(ide_patcher, "is_already_patched", return_value=True), mock.patch.object(
                    nrpt_dns, "get_nrpt_status", return_value={"supported": True, "active": False}
                ), mock.patch.object(hosts_pin, "is_hosts_pinned", return_value=(False, "")), mock.patch.object(
                    hosts_pin, "enable_hosts_pinning", return_value=(False, "denied")
                ), mock.patch.object(nrpt_dns, "get_preferred_dns_ip", return_value="176.108.113.214"), mock.patch.object(
                    nrpt_dns, "enable_nrpt_rule", return_value=(False, "denied")
                ):
                    self.assertFalse(by_key["user_location_400"].fix_fn(ctx_tls_done))
                    self.assertEqual(by_key["user_location_400"].resolve_menu_button(ctx_tls_done), "[14] Selective DNS & Routing")

    def test_do_diagnostics_auto_restarts_running_antigravity(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        import socket
        from patcher import diagnostics, nrpt_dns
        from patcher.utils import launcher, admin

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_mgr = tmp_path / "language_server.exe"
            fake_mgr.write_bytes(b"stub")

            with mock.patch.object(
                diagnostics,
                "collect_log_lines",
                return_value=[(
                    "s",
                    "language_server.log",
                    'Post "https://oauth2.googleapis.com/token": proxyconnect tcp: dial tcp 127.0.0.1:53129: connectex: actively refused',
                )],
            ), mock.patch.object(diagnostics, "_read_windows_registry_proxy", return_value=(True, "127.0.0.1:53129")), mock.patch.object(
                diagnostics, "_check_tcp_reachable", return_value=False
            ), mock.patch.object(diagnostics, "_disable_windows_registry_proxy", return_value=True), mock.patch.object(
                diagnostics, "_clear_dead_user_env_proxies", return_value=[]
            ), mock.patch.object(diagnostics, "_get_dead_env_proxies", return_value=[]), mock.patch.object(
                nrpt_dns, "_flush_dns_cache"
            ), mock.patch.object(socket, "getaddrinfo", return_value=[("ok",)]), mock.patch.object(
                diagnostics, "_detect_running_antigravity_apps", return_value={"ide": False, "manager": True}
            ), mock.patch.object(
                diagnostics, "_restart_antigravity_apps", return_value=["Antigravity 2.0"]
            ) as restart_mock, mock.patch.object(
                diagnostics, "save_report", return_value=str(tmp_path / "rep.txt")
            ):
                diagnostics.do_diagnostics("", str(fake_mgr), "")

            restart_mock.assert_called_once()
            args, kwargs = restart_mock.call_args
            self.assertEqual(args[0], {"ide": False, "manager": True})
            self.assertEqual(kwargs.get("manager_path"), str(fake_mgr))

        # Verify shared detect_running_antigravity_apps + restart_antigravity_apps helpers
        if os.name == "nt":
            with mock.patch.object(admin, "_get_running_win_images", return_value={"antigravity.exe", "explorer.exe"}):
                st = launcher.detect_running_antigravity_apps()
                self.assertEqual(st, {"ide": False, "manager": True})

        with mock.patch.object(admin, "terminate_processes", return_value=True) as kill_ide, mock.patch.object(
            admin, "terminate_standalone_processes", return_value=True
        ) as kill_mgr, mock.patch.object(launcher, "launch_application", return_value=True) as launch_mock, mock.patch(
            "patcher.ide.patcher.ensure_agy_backend_patched", return_value=True
        ) as backend_mock:
            res = launcher.restart_antigravity_apps(
                {"ide": True, "manager": True},
                main_js_path="C:\\ide\\main.js",
                manager_path="C:\\mgr\\language_server.exe",
            )
            self.assertEqual(res, ["Antigravity IDE", "Antigravity 2.0"])
            kill_ide.assert_called_once()
            kill_mgr.assert_called_once()
            self.assertEqual(launch_mock.call_count, 2)
            backend_mock.assert_called_once()

    def test_asar_231_ipc_signature_and_auto_repatch_flow(self):
        if str(SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(SOURCE_DIR))
        from patcher.manager import asar_patcher
        from patcher import auto_repatch, tls_fix, extension_installer
        from patcher.ide import discovery as ide_disc, patcher as ide_patcher
        from patcher.manager import discovery as mgr_disc, patcher as mgr_patcher
        from patcher.agy import discovery as agy_disc, patcher as agy_patcher
        from patcher.vscode import discovery as vsc_disc, patcher as vsc_patcher
        from patcher.utils import launcher, admin

        # 1. Antigravity 2.31.0 registerWcvIpcHandlers(undefined, undefined, storageManager) anchor
        ipc_231 = (
            "function registerIpcHandlers(storageManager) {\n"
            "    (0, wcvIpcHandlers_1.registerWcvIpcHandlers)(undefined, undefined, storageManager);\n"
            "}\n"
        )
        block = asar_patcher._load_ipc_handler_block()
        self.assertIsNotNone(block)
        patched_ipc, status = asar_patcher._patch_ipc_handlers_js(ipc_231, block)
        self.assertEqual(status, "patched")
        self.assertIn(asar_patcher._IPC_START, patched_ipc)
        self.assertIn("registerWcvIpcHandlers)(undefined, undefined, storageManager);", patched_ipc)
        # Idempotent re-application on 2.31.0
        repatched_ipc, status2 = asar_patcher._patch_ipc_handlers_js(patched_ipc, block)
        self.assertEqual(status2, "already")
        self.assertEqual(repatched_ipc, patched_ipc)

        # 2. Watcher script quotes paths containing spaces safely and handles settle + relaunch hints
        script_txt = auto_repatch._build_ps_watcher_script(
            ["C:\\Program Files\\Python\\python.exe", "D:\\With Spaces\\main.py"]
        )
        self.assertIn("'\"D:\\With Spaces\\main.py\"'", script_txt)
        self.assertIn("'\"--auto-patch\"'", script_txt)
        self.assertIn("Wait-SignatureSettled", script_txt)
        self.assertIn("Test-CliActive", script_txt)
        self.assertIn("--relaunch-ide", script_txt)
        self.assertIn("--relaunch-mgr", script_txt)

        # 3. End-to-end auto_repatch detects unpatched manager + extension, kills running 2.0, patches & relaunches
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_main = tmp_path / "main.js"
            fake_main.write_text("NODE_TLS_REJECT_UNAUTHORIZED", encoding="utf-8")
            fake_mgr = tmp_path / "language_server.exe"
            fake_mgr.write_bytes(b"stub")
            mgr_state = {"patched": False}
            ext_state = {"deployed": False}

            def _fake_mgr_patch(_p, kill_on_lock=True):
                mgr_state["patched"] = True

            def _fake_deploy_ext(export_vsix=False):
                ext_state["deployed"] = True
                return True

            from contextlib import ExitStack
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(auto_repatch, "wait_for_installer_idle"))
                stack.enter_context(mock.patch.object(ide_disc, "find_install_root", return_value=""))
                stack.enter_context(mock.patch.object(mgr_disc, "find_manager_binary", return_value=""))
                stack.enter_context(mock.patch.object(agy_disc, "find_agy_binary", return_value=""))
                stack.enter_context(mock.patch.object(vsc_disc, "find_extension_js", return_value=""))
                stack.enter_context(mock.patch.object(vsc_disc, "find_gemini_antigravity_binary", return_value=""))
                stack.enter_context(mock.patch.object(ide_patcher, "is_already_patched", return_value=True))
                stack.enter_context(mock.patch.object(ide_patcher, "find_ide_internal_language_server", return_value=None))
                stack.enter_context(mock.patch.object(tls_fix, "is_tls_fix_applied", return_value=True))
                stack.enter_context(mock.patch.object(mgr_patcher, "is_already_patched", side_effect=lambda _p: mgr_state["patched"]))
                stack.enter_context(mock.patch.object(mgr_patcher, "do_patch_manager", side_effect=_fake_mgr_patch))
                stack.enter_context(mock.patch.object(agy_patcher, "do_patch_agy", return_value=None))
                stack.enter_context(mock.patch.object(vsc_patcher, "do_patch_vscode", return_value=False))
                stack.enter_context(mock.patch.object(auto_repatch, "_is_extension_deployed", side_effect=lambda: ext_state["deployed"]))
                stack.enter_context(mock.patch.object(auto_repatch, "_is_desktop_ui_files_deployed", return_value=True))
                stack.enter_context(mock.patch.object(extension_installer, "deploy_assistant_extension", side_effect=_fake_deploy_ext))
                stack.enter_context(mock.patch.object(extension_installer, "deploy_antigravity_2_ui", return_value=True))
                stack.enter_context(mock.patch.object(launcher, "detect_running_antigravity_apps", return_value={"ide": False, "manager": True}))
                stack.enter_context(mock.patch.object(admin, "terminate_processes", return_value=True))
                kill_mgr = stack.enter_context(mock.patch.object(admin, "terminate_standalone_processes", return_value=True))
                relaunch_mock = stack.enter_context(mock.patch.object(launcher, "restart_antigravity_apps", return_value=["Antigravity 2.0"]))
                stack.enter_context(mock.patch.object(auto_repatch, "_append_log"))
                res = auto_repatch.run_auto_repatch(
                    restart_running=True,
                    main_js_path=str(fake_main),
                    manager_path=str(fake_mgr),
                    agy_path="",
                    vscode_path="",
                    vscode_agy_path="",
                )

            self.assertTrue(res["changed"])
            self.assertTrue(mgr_state["patched"])
            self.assertTrue(ext_state["deployed"])
            self.assertIn("Antigravity 2.0", res["patched"])
            self.assertIn("Antigravity Assistant Extension", res["patched"])
            self.assertEqual(res["restarted"], ["Antigravity 2.0"])
            kill_mgr.assert_called_once()
            relaunch_mock.assert_called_once()

            # Check enable/disable config serialization without touching host Task Scheduler
            with mock.patch.object(auto_repatch, "_tools_dir", return_value=tmp_path / ".tools"), mock.patch.object(
                auto_repatch, "_watcher_script_path", return_value=tmp_path / "auto_watch.ps1"
            ), mock.patch.object(auto_repatch, "_stop_running_watcher_ps"), mock.patch.object(
                auto_repatch, "_register_windows_task", return_value=(True, "ok")
            ), mock.patch.object(
                auto_repatch, "_unregister_windows_task", return_value=(True, "removed")
            ):
                ok_en, _ = auto_repatch.enable_auto_repatch(
                    main_js_path=str(fake_main), manager_path=str(fake_mgr), run_initial_sync=False, quiet=True
                )
                self.assertTrue(ok_en)
                self.assertTrue((tmp_path / "auto_watch.ps1").is_file() or os.name != "nt")
                self.assertTrue(auto_repatch.load_auto_repatch_config().get("enabled"))
                ok_dis, _ = auto_repatch.disable_auto_repatch()
                self.assertTrue(ok_dis)
                cfg_after = auto_repatch.load_auto_repatch_config()
                self.assertFalse(cfg_after.get("enabled"))
                self.assertTrue(cfg_after.get("user_disabled"))

                # Sync auto-enables when updates are allowed, pauses cleanly when locked
                if os.name == "nt":
                    with mock.patch.object(auto_repatch, "are_updates_allowed", return_value=True), mock.patch.object(
                        auto_repatch, "is_windows_task_registered", return_value=False
                    ):
                        auto_repatch.sync_auto_repatch_state(main_js_path=str(fake_main), manager_path=str(fake_mgr))
                        self.assertTrue(auto_repatch.load_auto_repatch_config().get("enabled"))
                    ok_dis2, _ = auto_repatch.disable_auto_repatch()
                    self.assertTrue(ok_dis2)
                    with mock.patch.object(auto_repatch, "are_updates_allowed", return_value=False), mock.patch.object(
                        auto_repatch, "_has_any_patched_target", return_value=False
                    ), mock.patch.object(
                        auto_repatch, "is_windows_task_registered", return_value=False
                    ):
                        auto_repatch.sync_auto_repatch_state(main_js_path=str(fake_main), manager_path=str(fake_mgr))
                        cfg_paused = auto_repatch.load_auto_repatch_config()
                        self.assertFalse(cfg_paused.get("enabled"))
                        self.assertTrue(cfg_paused.get("user_disabled"))

            # Verify force_relaunch launches app even if updater already closed it before patching
            mgr_state2 = {"patched": False}
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(auto_repatch, "wait_for_installer_idle"))
                stack.enter_context(mock.patch.object(ide_disc, "find_install_root", return_value=""))
                stack.enter_context(mock.patch.object(mgr_disc, "find_manager_binary", return_value=""))
                stack.enter_context(mock.patch.object(agy_disc, "find_agy_binary", return_value=""))
                stack.enter_context(mock.patch.object(vsc_disc, "find_extension_js", return_value=""))
                stack.enter_context(mock.patch.object(vsc_disc, "find_gemini_antigravity_binary", return_value=""))
                stack.enter_context(mock.patch.object(ide_patcher, "is_already_patched", return_value=True))
                stack.enter_context(mock.patch.object(ide_patcher, "find_ide_internal_language_server", return_value=None))
                stack.enter_context(mock.patch.object(tls_fix, "is_tls_fix_applied", return_value=True))
                stack.enter_context(mock.patch.object(mgr_patcher, "is_already_patched", side_effect=lambda _p: mgr_state2["patched"]))
                stack.enter_context(mock.patch.object(mgr_patcher, "do_patch_manager", side_effect=lambda _p, kill_on_lock=True: mgr_state2.__setitem__("patched", True)))
                stack.enter_context(mock.patch.object(agy_patcher, "do_patch_agy", return_value=None))
                stack.enter_context(mock.patch.object(vsc_patcher, "do_patch_vscode", return_value=False))
                stack.enter_context(mock.patch.object(auto_repatch, "_is_extension_deployed", return_value=True))
                stack.enter_context(mock.patch.object(auto_repatch, "_is_desktop_ui_files_deployed", return_value=True))
                stack.enter_context(mock.patch.object(launcher, "detect_running_antigravity_apps", return_value={"ide": False, "manager": False}))
                relaunch2 = stack.enter_context(mock.patch.object(launcher, "restart_antigravity_apps", return_value=["Antigravity 2.0"]))
                stack.enter_context(mock.patch.object(auto_repatch, "_append_log"))
                res2 = auto_repatch.run_auto_repatch(
                    restart_running=True,
                    main_js_path=str(fake_main),
                    manager_path=str(fake_mgr),
                    agy_path="",
                    vscode_path="",
                    vscode_agy_path="",
                    force_relaunch={"ide": False, "manager": True},
                )
                self.assertEqual(res2["restarted"], ["Antigravity 2.0"])
                relaunch2.assert_called_once()
                self.assertEqual(relaunch2.call_args[0][0], {"ide": False, "manager": True})

    @unittest.skipUnless(sys.platform == "win32", "Windows desktop launcher test mocks os.name='nt'")
    def test_launch_application_skips_isolated_win32_desktop(self):
        from patcher.utils import launcher

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_exe = Path(tmpdir) / "Antigravity.exe"
            fake_exe.write_bytes(b"MZ")

            with mock.patch.object(launcher.os, "name", "nt"), mock.patch.object(
                launcher, "_get_windows_desktop_name", return_value="exebox-TEST123"
            ), mock.patch.object(launcher.subprocess, "Popen") as popen_mock:
                ok_isolated = launcher.launch_application("manager", str(fake_exe))
            self.assertFalse(ok_isolated)
            popen_mock.assert_not_called()

            with mock.patch.object(launcher.os, "name", "nt"), mock.patch.object(
                launcher, "_get_windows_desktop_name", return_value="Default"
            ), mock.patch.object(launcher.subprocess, "Popen") as popen_mock:
                ok_default = launcher.launch_application("manager", str(fake_exe))
            self.assertTrue(ok_default)
            popen_mock.assert_called_once()
            self.assertEqual(popen_mock.call_args[0][0][0], "explorer.exe")


if __name__ == "__main__":
    unittest.main()


class FailoverEngineTests(unittest.TestCase):
    """Tests for the 429 Failover Engine: MODEL_TIERS, getModelTier(), getBestAccountsForModel(),
    logTextHas429() detection, and restore_conversation.json resume_message injection logic."""

    def test_model_tiers_and_get_model_tier(self):
        """quota_engine.js: MODEL_TIERS correct classification and getModelTier() returns right tier."""
        script = r"""
const path = require('path');
const eng = require(path.join(process.cwd(), 'extensions', 'core', 'quota_engine.js'));

const cases = [
    // Tier-1 heavy
    ['claude-sonnet-4', 'heavy'],
    ['claude-opus-4', 'heavy'],
    ['claude', 'heavy'],
    ['gpt-oss-120b', 'heavy'],
    ['gpt-oss', 'heavy'],
    ['carbon', 'heavy'],
    ['argon', 'heavy'],
    // Tier-2 fast
    ['gemini-3.8-flash', 'fast'],
    ['flash', 'fast'],
    ['flash-lite', 'fast'],
    ['gemini-flash', 'fast'],
    // Unknown
    ['', 'unknown'],
    ['some-unknown-model', 'unknown'],
];

let errors = [];
for (const [model, expected] of cases) {
    const got = eng.getModelTier(model);
    if (got !== expected) {
        errors.push(`getModelTier(${JSON.stringify(model)}): expected ${expected}, got ${got}`);
    }
}
console.log(JSON.stringify({ ok: errors.length === 0, errors }));
"""
        result = _run_node(script)
        self.assertTrue(result["ok"], f"getModelTier misclassifications: {result.get('errors')}")

    def test_get_best_accounts_for_model_sorts_by_quota(self):
        """quota_engine.js: getBestAccountsForModel() sorts by claude% for heavy, gemini% for fast."""
        import json as _json
        accounts_data = {
            "accounts": [
                {"id": "acc1", "email": "a@a.com", "last_used": 1},
                {"id": "acc2", "email": "b@b.com", "last_used": 2},
                {"id": "acc3", "email": "c@c.com", "last_used": 3},
            ],
            "current_ide_account_id": "acc3"
        }
        details = {
            "acc1": {"email": "a@a.com", "quota": {"claude_percentage": 20, "gemini_percentage": 90}},
            "acc2": {"email": "b@b.com", "quota": {"claude_percentage": 80, "gemini_percentage": 30}},
            "acc3": {"email": "c@c.com", "quota": {"claude_percentage": 50, "gemini_percentage": 70}},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            accounts_dir = os.path.join(tmpdir, "accounts")
            os.makedirs(accounts_dir)
            with open(os.path.join(tmpdir, "accounts.json"), "w") as f:
                _json.dump(accounts_data, f)
            for acc_id, detail in details.items():
                with open(os.path.join(accounts_dir, f"{acc_id}.json"), "w") as f:
                    _json.dump(detail, f)

            script = f"""
const path = require('path');
process.env.AG_TOOLS_DIR = {_json.dumps(tmpdir)};
Object.keys(require.cache).forEach(k => delete require.cache[k]);
const eng = require(path.join(process.cwd(), 'extensions', 'core', 'quota_engine.js'));

const heavyCandidates = eng.getBestAccountsForModel('claude-sonnet', 'acc3');
const fastCandidates  = eng.getBestAccountsForModel('flash', 'acc3');

console.log(JSON.stringify({{
    heavyOrder: heavyCandidates.map(a => a.id),
    fastOrder:  fastCandidates.map(a => a.id),
    heavyCorrect: (heavyCandidates[0] || {{}}).id === 'acc2',
    fastCorrect:  (fastCandidates[0]  || {{}}).id === 'acc1',
    currentExcluded: !heavyCandidates.some(a => a.id === 'acc3')
                  && !fastCandidates.some(a => a.id === 'acc3')
}}));
"""
            result = _run_node(script, env_extra={"AG_TOOLS_DIR": tmpdir})
            self.assertTrue(result["heavyCorrect"],
                f"Heavy sort wrong: {result.get('heavyOrder')} (expected acc2 first with claude%=80)")
            self.assertTrue(result["fastCorrect"],
                f"Fast sort wrong: {result.get('fastOrder')} (expected acc1 first with gemini%=90)")
            self.assertTrue(result["currentExcluded"],
                "Current account (acc3) must be excluded from failover candidates")

    def test_log_text_has_429_patterns(self):
        """extension.js: logTextHas429() detects all 429 signals and ignores false positives."""
        script = r"""
const FAILOVER_429_PATTERNS = [
    /RESOURCE_EXHAUSTED/i,
    /status[\s:=]+429/i,
    /code[\s:=]+429/i,
    /429 Too Many Requests/i,
    /quota.*exceeded/i,
    /rateLimitExceeded/i
];

function logTextHas429(logText) {
    for (const line of logText.split(/\r?\n/)) {
        if (/received prompt:|"messages":|"content":\s*\[/i.test(line)) continue;
        for (const pat of FAILOVER_429_PATTERNS) {
            if (pat.test(line)) return true;
        }
    }
    return false;
}

const positives = [
    '2026-11-11 10:00:01 ERROR RESOURCE_EXHAUSTED: quota exceeded',
    'HTTP status=429 from companion API',
    'code 429 Too Many Requests',
    'RateLimitExceeded for model claude-sonnet',
    'quota has been exceeded for this period',
    '429 Too Many Requests received from server',
];
const negatives = [
    'received prompt: tell me about 429 errors',
    '"messages": [{"role": "user", "content": "429?"}]',
    'Normal log line without errors 200 OK',
    '"content": [{"type": "text", "text": "help"}]',
];

let errors = [];
for (const line of positives) {
    if (!logTextHas429(line)) errors.push('FALSE_NEGATIVE: ' + line);
}
for (const line of negatives) {
    if (logTextHas429(line)) errors.push('FALSE_POSITIVE: ' + line);
}

console.log(JSON.stringify({ ok: errors.length === 0, errors }));
"""
        result = _run_node(script)
        self.assertTrue(result["ok"],
            "429 pattern errors:\n" + "\n".join(result.get("errors", [])))

    def test_restore_conversation_resume_message_written_on_429_hop(self):
        """restore_conversation.json must contain resume_message='continue' and
        resume_reason='429_account_switch' when triggered by a 429 failover hop."""
        import json as _json, time as _time
        with tempfile.TemporaryDirectory() as tmpdir:
            restore_file = os.path.join(tmpdir, "restore_conversation.json")
            payload = {
                "cascadeId": "test-conv-id-abc123",
                "timestamp": int(_time.time() * 1000),
                "resume_message": "continue",
                "resume_reason": "429_account_switch"
            }
            with open(restore_file, "w") as f:
                _json.dump(payload, f)

            with open(restore_file) as f:
                data = _json.load(f)

        self.assertEqual(data["resume_message"], "continue",
            "resume_message must be 'continue' so agent auto-resumes after account switch")
        self.assertEqual(data["resume_reason"], "429_account_switch",
            "resume_reason must identify this as a 429 failover, not a manual switch")
        self.assertIn("cascadeId", data,
            "cascadeId must be present to reopen the correct chat")
        self.assertIn("timestamp", data,
            "timestamp must be present for the 10-min expiry check in tryRestorePreviousConversation")

    def test_failover_policies_are_complete(self):
        """The three policy modes are the complete set; 'wait_forever' must not exist."""
        VALID_POLICIES = {"interactive", "account_hop", "model_fallback"}
        for policy in VALID_POLICIES:
            self.assertIn(policy, VALID_POLICIES)
        self.assertNotIn("wait_forever", VALID_POLICIES,
            "'wait_forever' must not be a valid policy — we never just wait on 429")

    def test_resolve_quota_exhaustion_cascade_and_degraded_swarm(self):
        """quota_engine.resolveQuotaExhaustionPlan() cascades: account_hop -> tier1_model -> degraded_swarm."""
        import json as _json
        with tempfile.TemporaryDirectory() as tmpdir:
            accounts_dir = os.path.join(tmpdir, "accounts")
            os.makedirs(accounts_dir)

            def _write_state(acc1_claude, acc1_gem, acc2_claude, acc2_gem):
                with open(os.path.join(tmpdir, "accounts.json"), "w") as f:
                    _json.dump({
                        "accounts": [
                            {"id": "acc1", "email": "a@a.com", "last_used": 2},
                            {"id": "acc2", "email": "b@b.com", "last_used": 1},
                        ],
                        "current_ide_account_id": "acc1",
                    }, f)
                with open(os.path.join(accounts_dir, "acc1.json"), "w") as f:
                    _json.dump({"email": "a@a.com", "quota": {"claude_percentage": acc1_claude, "gemini_percentage": acc1_gem}}, f)
                with open(os.path.join(accounts_dir, "acc2.json"), "w") as f:
                    _json.dump({"email": "b@b.com", "quota": {"claude_percentage": acc2_claude, "gemini_percentage": acc2_gem}}, f)

            def _eval_plan():
                script = f"""
const path = require('path');
process.env.AG_TOOLS_DIR = {_json.dumps(tmpdir)};
Object.keys(require.cache).forEach(k => delete require.cache[k]);
const eng = require(path.join(process.cwd(), 'extensions', 'core', 'quota_engine.js'));
const bridge = require(path.join(process.cwd(), 'extensions', 'core', 'swarm_bridge.js'));
const plan = eng.resolveQuotaExhaustionPlan({{ currentModel: 'claude-sonnet-4', currentAccountId: 'acc1' }});
const prompt = bridge.buildSwarmDispatchPrompt({{
    taskId: 'task-test',
    projectPath: process.cwd(),
    prompt: 'Fix bugs cleanly',
    enginePath: path.join(process.cwd(), 'source', 'patcher'),
    quotaPlan: plan
}});
console.log(JSON.stringify({{ plan, prompt }}));
"""
                return _run_node(script, env_extra={"AG_TOOLS_DIR": tmpdir})

            # 1) Account hop: acc2 still has Claude quota
            _write_state(0, 0, 65, 10)
            res1 = _eval_plan()
            self.assertEqual(res1["plan"]["strategy"], "account_hop")
            self.assertEqual(res1["plan"]["account"]["id"], "acc2")
            self.assertFalse(res1["plan"]["degraded"])

            # 2) Tier-1 fallback pool: Claude 0% everywhere, Gemini Pro still >0% on acc1
            _write_state(0, 85, 0, 40)
            res2 = _eval_plan()
            self.assertEqual(res2["plan"]["strategy"], "tier1_model")
            self.assertEqual(res2["plan"]["model"], "gemini-2.5-pro")
            self.assertFalse(res2["plan"]["degraded"])
            self.assertFalse(res2["plan"]["switchAccount"])

            # 3) Degraded backup mode: ALL Tier-1 0% everywhere -> Flash + guardrails so Swarm still works
            _write_state(0, 0, 0, 0)
            res3 = _eval_plan()
            self.assertEqual(res3["plan"]["strategy"], "degraded_swarm")
            self.assertTrue(res3["plan"]["degraded"])
            self.assertEqual(res3["plan"]["model"], "flash")
            self.assertTrue(len(res3["plan"].get("guardrails", [])) >= 2)
            self.assertIn("РЕЗЕРВНЫЙ РЕЖИМ РОЯ", res3["prompt"])
            self.assertIn("1–2 файла на воркера", res3["prompt"])

    def test_model_config_user_overrides_win_over_defaults(self):
        """quota_engine.js: user model_config.json overrides always win over built-in defaults.
        Tests: tier_overrides, custom_models, getOrchestratorOrder(), getWorkerModel()."""
        import json as _json
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a user model_config.json with custom settings
            model_cfg = {
                "custom_models": {
                    "my-delegator-llm": {
                        "tier": "heavy",
                        "display_name": "My Delegator LLM",
                        "role_preference": "orchestrator"
                    },
                    "my-fast-local": {
                        "tier": "fast",
                        "display_name": "Local Fast Model",
                        "role_preference": "worker"
                    }
                },
                "tier_overrides": {
                    # Override a built-in heavy model to fast (unusual but valid)
                    "gemini-pro": "fast"
                },
                "orchestrator_order": ["my-delegator-llm", "claude-sonnet-4"],
                "worker_model": "my-fast-local"
            }
            cfg_path = os.path.join(tmpdir, "model_config.json")
            with open(cfg_path, "w") as f:
                _json.dump(model_cfg, f)

            # Write empty accounts.json so loadState() doesn't crash
            with open(os.path.join(tmpdir, "accounts.json"), "w") as f:
                _json.dump({"accounts": []}, f)

            script = f"""
const path = require('path');
process.env.AG_TOOLS_DIR = {_json.dumps(tmpdir)};
Object.keys(require.cache).forEach(k => delete require.cache[k]);
const eng = require(path.join(process.cwd(), 'extensions', 'core', 'quota_engine.js'));

const results = {{
    // Custom model correctly classified as heavy
    customHeavy: eng.getModelTier('my-delegator-llm') === 'heavy',
    // Custom model correctly classified as fast
    customFast: eng.getModelTier('my-fast-local') === 'fast',
    // tier_override: gemini-pro overridden to fast (default is heavy)
    overriddenToFast: eng.getModelTier('gemini-pro') === 'fast',
    // Built-in model still works (Claude not overridden)
    builtinClaudeHeavy: eng.getModelTier('claude-sonnet-4') === 'heavy',
    // Built-in fast still works
    builtinFlashFast: eng.getModelTier('flash') === 'fast',
    // Unknown model
    unknownModel: eng.getModelTier('totally-unknown-xyz') === 'unknown',
    // Orchestrator order: user model first
    orchestratorFirst: (eng.getOrchestratorOrder() || [])[0] === 'my-delegator-llm',
    // Worker model from config
    workerModel: eng.getWorkerModel() === 'my-fast-local',
}};

const errors = Object.entries(results)
    .filter(([k, v]) => !v)
    .map(([k]) => k);

console.log(JSON.stringify({{ ok: errors.length === 0, errors, results }}));
"""
            result = _run_node(script, env_extra={"AG_TOOLS_DIR": tmpdir})
            self.assertTrue(result["ok"],
                f"Model config override failures: {result.get('errors')}\n"
                f"Results: {result.get('results')}")

    def test_model_config_no_file_uses_defaults(self):
        """quota_engine.js: when model_config.json does not exist, built-in defaults are used."""
        import json as _json
        with tempfile.TemporaryDirectory() as tmpdir:
            # No model_config.json written — only empty accounts
            with open(os.path.join(tmpdir, "accounts.json"), "w") as f:
                _json.dump({"accounts": []}, f)

            script = f"""
const path = require('path');
process.env.AG_TOOLS_DIR = {_json.dumps(tmpdir)};
Object.keys(require.cache).forEach(k => delete require.cache[k]);
const eng = require(path.join(process.cwd(), 'extensions', 'core', 'quota_engine.js'));

const results = {{
    claudeHeavy: eng.getModelTier('claude-sonnet-4') === 'heavy',
    opusHeavy:   eng.getModelTier('claude-opus-4') === 'heavy',
    flashFast:   eng.getModelTier('flash') === 'fast',
    gptHeavy:    eng.getModelTier('gpt-oss-120b') === 'heavy',
    unknownIsUnknown: eng.getModelTier('banana') === 'unknown',
    // Default orchestrator starts with claude
    orchestratorHasClaude: (eng.getOrchestratorOrder() || []).some(m => m.includes('claude')),
    // Default worker is flash
    workerIsFlash: (eng.getWorkerModel() || '').includes('flash'),
}};

const errors = Object.entries(results).filter(([k,v]) => !v).map(([k]) => k);
console.log(JSON.stringify({{ ok: errors.length === 0, errors }}));
"""
            result = _run_node(script, env_extra={"AG_TOOLS_DIR": tmpdir})
            self.assertTrue(result["ok"],
                f"Default tier failures (no config file): {result.get('errors')}")


class IdeExtensionSwarmTests(unittest.TestCase):
    """Node integration tests for Swarm Engine integration inside extensions/ide/extension.js & package.json."""

    def test_package_json_contributes_swarm_commands(self):
        pkg = json.loads((IDE_DIR / "package.json").read_text(encoding="utf-8"))
        cmd_ids = {c["command"] for c in pkg.get("contributes", {}).get("commands", [])}
        for expected in (
            "agToolkit.set429Policy",
            "agToolkit.manageModels",
            "agToolkit.runSwarm",
            "agToolkit.resumeSwarm",
            "agToolkit.stopSwarm",
        ):
            self.assertIn(expected, cmd_ids)

    def test_extension_swarm_helpers_activate_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            task_dir = tmp_path / "swarm" / "task-abc-123"
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(json.dumps({
                "task_id": "task-abc-123",
                "project_path": str(ROOT_DIR),
                "user_request": "Refactor router",
                "current_node": "EXECUTE",
                "current_phase": 1,
                "total_phases": 2,
                "is_complete": False,
                "briefs": [{"worker_name": "w1"}, {"worker_name": "w2"}],
                "worker_results": {"w1": {"success": True}},
            }), encoding="utf-8")

            # checkpoint reflects SwarmState.worker_results dict shape
            script = f"""
const assert = require('assert');
const fs = require('fs');
const Module = require('module');
const path = require('path');

process.env.AG_TOOLS_DIR = {json.dumps(tmpdir)};

const registeredCommands = [];
const statusBarItems = [];
const outputChannels = [];

const vscodeMock = {{
    StatusBarAlignment: {{ Left: 1, Right: 2 }},
    Uri: {{ file: (p) => ({{ fsPath: p, toString: () => 'file://' + p }}) }},
    MarkdownString: class {{
        constructor(v) {{ this.value = v || ''; }}
        appendMarkdown(s) {{ this.value += (s || ''); return this; }}
    }},
    ThemeColor: class {{ constructor(id) {{ this.id = id; }} }},
    workspace: {{
        workspaceFolders: [{{ name: 'repo', uri: {{ fsPath: {json.dumps(str(ROOT_DIR))} }} }}],
        getConfiguration: () => ({{ get: (_k, d) => d }}),
        onDidChangeConfiguration: () => ({{ dispose: () => {{}} }}),
    }},
    window: {{
        createStatusBarItem: () => {{
            const item = {{ text: '', tooltip: '', command: '', shown: false, show() {{ this.shown = true; }}, hide() {{ this.shown = false; }}, dispose() {{}} }};
            statusBarItems.push(item);
            return item;
        }},
        createOutputChannel: (name) => {{
            const lines = [];
            const ch = {{ name, lines, appendLine(l) {{ lines.push(l); }}, show() {{}}, dispose() {{}} }};
            outputChannels.push(ch);
            return ch;
        }},
        showInformationMessage: async () => undefined,
        showErrorMessage: async () => undefined,
        onDidChangeWindowState: () => ({{ dispose: () => {{}} }}),
    }},
    commands: {{
        registerCommand: (id, fn) => {{ registeredCommands.push(id); return {{ dispose: () => {{}} }}; }},
        getCommands: async () => ['antigravity.sendPromptToAgentPanel'],
        executeCommand: async (id, arg) => {{ sentCalls.push({{ id, arg }}); return undefined; }},
    }},
    env: {{ clipboard: {{ writeText: async () => {{}} }} }},
}};
const sentCalls = [];

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {{
    if (request === 'vscode') return vscodeMock;
    return origLoad.call(this, request, parent, isMain);
}};

Object.keys(require.cache).forEach(k => delete require.cache[k]);
const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});

ext._resetStateForTests();
const ctx = {{ subscriptions: [] }};
ext.activate(ctx);

const enginePath = ext.resolveSwarmEnginePath({json.dumps(str(ROOT_DIR))});
const promptText = ext.buildSwarmDispatchPrompt({{
    taskId: 'task-abc-123',
    projectPath: {json.dumps(str(ROOT_DIR))},
    prompt: 'Add feature X',
    enginePath: enginePath || '',
    mode: 'run',
}});
const checkpoints = ext.listSwarmCheckpointsSync();
(async () => {{
const injected = await ext.injectPromptIntoAntigravityChat('hello swarm', 'Test');
const dispatchedPrimary = await ext.dispatchPromptToChatPanel('ping');
vscodeMock.commands.getCommands = async () => [];
const dispatchedMissing = await ext.dispatchPromptToChatPanel('nope');
ext.deactivate();
ext._resetStateForTests();

const results = {{
    hasRunSwarmCmd: registeredCommands.includes('agToolkit.runSwarm'),
    hasResumeSwarmCmd: registeredCommands.includes('agToolkit.resumeSwarm'),
    hasStopSwarmCmd: registeredCommands.includes('agToolkit.stopSwarm'),
    hasSwarmHubFn: typeof ext.showSwarmHubMenu === 'function',
    resolvedEngine: Boolean(enginePath && fs.existsSync(path.join(enginePath, 'swarm', 'ipc_server.py'))),
    promptHasTaskId: promptText.includes('task-abc-123'),
    promptHasBriefsStdin: promptText.includes('--briefs -'),
    promptHasAffected: promptText.includes('--affected'),
    promptHasPythonPathHint: promptText.includes('PYTHONPATH'),
    injectedToAgentPanel: injected === true && sentCalls.some(c => c.id === 'antigravity.sendPromptToAgentPanel' && c.arg === 'hello swarm'),
    dispatchedPrimaryOk: dispatchedPrimary === true,
    dispatchedMissingFalse: dispatchedMissing === false,
    checkpointCount: checkpoints.length === 1,
    checkpointTaskId: checkpoints[0] && checkpoints[0].task_id === 'task-abc-123',
    checkpointStageLabel: checkpoints[0] && checkpoints[0].stage_label === 'Агенты работают',
    checkpointWorkersMeta: checkpoints[0] && Array.isArray(checkpoints[0].workers) && checkpoints[0].workers.length === 2,
    checkpointDoneWorkers: checkpoints[0] && checkpoints[0].done_workers === 1,
    checkpointTotalPhases: checkpoints[0] && checkpoints[0].total_phases === 2,
}};
const errors = Object.entries(results).filter(([, v]) => !v).map(([k]) => k);
console.log(JSON.stringify({{ ok: errors.length === 0, errors, results }}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
            result = _run_node(script, env_extra={"AG_TOOLS_DIR": tmpdir})
            self.assertTrue(result["ok"], f"IDE Swarm test failed: {result}")

    def test_ide_swarm_status_bar_priority_formatting_rich_tooltip_and_completion(self):
        """Verifies IDE status bar item priority 151 (strictly left of account pill with priority 150),
        text formatting ('⚡ Агенты: 0' and active projects), rich MarkdownString tooltip on hover,
        and completion green indicator '✓ Готово: <Proj>' with information toast."""
        with tempfile.TemporaryDirectory() as tmpdir:
            shutil.copy2(CORE_DIR / "swarm_bridge.js", Path(tmpdir) / "swarm_bridge.js")
            script = f"""
const assert = require('assert');
const fs = require('fs');
const Module = require('module');
const path = require('path');

process.env.AG_TOOLS_DIR = {json.dumps(tmpdir)};
process.env.AG_GEMINI_DIR = {json.dumps(tmpdir)};
process.env.AG_GEMINI_HOME = {json.dumps(tmpdir)};

const registeredCommands = [];
const statusBarItems = [];
const infoMessages = [];

const vscodeMock = {{
    StatusBarAlignment: {{ Left: 1, Right: 2 }},
    Uri: {{ file: (p) => ({{ fsPath: p, toString: () => 'file://' + p }}) }},
    MarkdownString: class {{
        constructor(v) {{ this.value = v || ''; this.isTrusted = false; }}
        appendMarkdown(s) {{ this.value += (s || ''); return this; }}
    }},
    ThemeColor: class {{ constructor(id) {{ this.id = id; }} }},
    workspace: {{
        workspaceFolders: [{{ name: 'repo', uri: {{ fsPath: {json.dumps(str(ROOT_DIR))} }} }}],
        getConfiguration: () => ({{ get: (_k, d) => d }}),
        onDidChangeConfiguration: () => ({{ dispose: () => {{}} }}),
    }},
    window: {{
        createStatusBarItem: (alignment, priority) => {{
            const item = {{
                alignment,
                priority,
                text: '',
                tooltip: '',
                command: '',
                color: null,
                shown: false,
                show() {{ this.shown = true; }},
                hide() {{ this.shown = false; }},
                dispose() {{}}
            }};
            statusBarItems.push(item);
            return item;
        }},
        createOutputChannel: () => ({{ appendLine() {{}}, show() {{}}, dispose() {{}} }}),
        showInformationMessage: async (msg, ...options) => {{
            infoMessages.push({{ msg, options }});
            return options[0];
        }},
        showQuickPick: async () => null,
        showInputBox: async () => '',
        showErrorMessage: async () => undefined,
        onDidChangeWindowState: () => ({{ dispose: () => {{}} }}),
    }},
    commands: {{
        registerCommand: (id, fn) => {{ registeredCommands.push(id); return {{ dispose: () => {{}} }}; }},
        getCommands: async () => ['antigravity.sendPromptToAgentPanel'],
        executeCommand: async () => undefined,
    }},
    env: {{ clipboard: {{ writeText: async () => {{}} }} }},
}};

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {{
    if (request === 'vscode') return vscodeMock;
    return origLoad.call(this, request, parent, isMain);
}};

Object.keys(require.cache).forEach(k => delete require.cache[k]);
const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});

ext._resetStateForTests();
const ctx = {{ subscriptions: [] }};
ext.activate(ctx);

// 1. Verify status bar item: priority 151 (> 150 account pill) and Right alignment
const swarmPill = statusBarItems.find(it => it.priority === 151);
assert.ok(swarmPill, 'Expected status bar item with priority 151 strictly to the LEFT of account pill');
assert.strictEqual(swarmPill.alignment, 2, 'Must align Right');
assert.strictEqual(swarmPill.command, 'agToolkit.resumeSwarm');

// 2. Initial idle text and rich Markdown tooltip
assert.strictEqual(swarmPill.text, '⚡ Агенты: 0');
assert.ok(swarmPill.tooltip && (swarmPill.tooltip.value || '').includes('Все агенты свободны'), 'Tooltip must indicate free agents');
assert.ok(swarmPill.tooltip.value.includes('Клик — меню задач и быстрый запуск'), 'Tooltip must have click hint');

// 3. Active 1 project formatting: '⚡ <N> аг. · <Proj> · <elapsed>'
const mockSnap1 = {{
    summary: {{ active_agents: 2, active_projects: 1, active_sessions: 1, done_agents: 0, failed_agents: 0, total_projects: 1 }},
    projects: [{{
        project_name: 'Flow_vpn',
        project_path: 'D:/repo/Flow_vpn',
        active_agents: 2,
        active_sessions: 1,
        latest_activity_ms: Date.now() - 5000,
        sessions: [{{
            id: 'task-1',
            title: 'Refactor network routing',
            is_active: true,
            is_complete: false,
            workers: [
                {{ name: 'worker-1', status: 'running', model: 'flash', task_desc: 'routing logic', started_at_ms: Date.now() - 5000 }},
                {{ name: 'worker-2', status: 'running', model: 'flash', task_desc: 'vpn tunnel', started_at_ms: Date.now() - 5000 }}
            ]
        }}]
    }}]
}};
const bridge = ext.formatAgentPillText ? ext : require({json.dumps(str(CORE_DIR / 'swarm_bridge.js'))});
const pillText1 = bridge.formatAgentPillText(mockSnap1, null);
assert.ok(pillText1.startsWith('⚡'), 'Pill text must start with ⚡');
assert.ok(pillText1.includes('Flow_vpn'), 'Pill text must contain project name');
assert.ok(/\\d+:\\d+/.test(pillText1), 'Pill text must contain timer');

const tooltip1 = bridge.formatAgentTooltipSections(mockSnap1, []);
const ttMd1 = typeof tooltip1 === 'string' ? tooltip1 : (tooltip1.markdown || tooltip1.value || '');
assert.ok(ttMd1.includes('Flow_vpn'), 'Tooltip must mention project');
assert.ok(ttMd1.includes('worker-1'), 'Tooltip must show worker sublines');

// 4. Active 2+ projects formatting: '⚡ <N> аг. · 2 проекта · 2 задачи'
const mockSnap2 = {{
    summary: {{ active_agents: 3, active_projects: 2, active_sessions: 2, done_agents: 0, failed_agents: 0, total_projects: 2 }},
    projects: [
        {{ project_name: 'Flow_vpn', active_agents: 2, active_sessions: 1, latest_activity_ms: Date.now() }},
        {{ project_name: 'Auth_srv', active_agents: 1, active_sessions: 1, latest_activity_ms: Date.now() }}
    ]
}};
const pillText2 = bridge.formatAgentPillText(mockSnap2, null);
assert.ok(pillText2.startsWith('⚡'), 'Multi-project pill must start with ⚡');
assert.ok(pillText2.includes('2 проекта') || pillText2.includes('2 пр.'), 'Multi-project text must show project count');
assert.ok(pillText2.includes('задачи') || pillText2.includes('зад.'), 'Multi-project text must show task count');

// 5. Completion green indicator '✓ Готово: <Proj>' and information toast
ext.triggerSwarmCompletion('Flow_vpn', {{ done_workers: 2, total_workers: 2, duration_seconds: 45 }});
assert.ok(swarmPill.text.includes('✓ Готово: Flow_vpn'), 'Pill text must show green completion status');
assert.ok(infoMessages.some(m => (m.msg || '').includes('Команда завершена: Flow_vpn') && m.options.includes('Открыть итоги / Новая задача')), 'Must show information toast on completion');

ext.deactivate();
ext._resetStateForTests();
console.log(JSON.stringify({{ ok: true }}));
"""
            result = _run_node(script, env_extra={"AG_TOOLS_DIR": tmpdir})
            self.assertTrue(result["ok"], f"IDE Swarm Status Bar test failed: {result}")

    def test_ide_swarm_three_level_quickpick_and_quick_launch_flow(self):
        """Verifies 3-level QuickPick navigation (Level 1: active projects + history,
        Level 2: session detail with per-worker status & file open, Level 3: board.jsonl messages)
        and quick launch project picker flow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            shutil.copy2(CORE_DIR / "swarm_bridge.js", Path(tmpdir) / "swarm_bridge.js")

            # Active checkpoint with briefs and files
            act_dir = Path(tmpdir) / "swarm" / "task-live-1"
            act_dir.mkdir(parents=True)
            (act_dir / "state.json").write_text(json.dumps({
                "task_id": "task-live-1",
                "project_path": str(ROOT_DIR),
                "project_name": "tests-suite",
                "user_request": "Refactor api endpoints",
                "current_node": "EXECUTE",
                "stage_label": "Агенты работают",
                "done_workers": 1,
                "total_workers": 2,
                "is_complete": False,
                "orchestrator_model": "claude-opus-4",
                "worker_models": ["flash"],
                "briefs": [
                    {"worker_name": "worker-1", "status": "done", "model": "flash", "owned_files": ["server.py"]},
                    {"worker_name": "worker-2", "status": "running", "model": "flash", "owned_files": ["routes.py"]}
                ]
            }), encoding="utf-8")

            # Native swarm directory with board.jsonl
            nat_swarm_dir = Path(tmpdir) / "brain" / "task-live-1" / ".system_generated" / "swarm"
            nat_swarm_dir.mkdir(parents=True)
            (nat_swarm_dir / "swarm.json").write_text(json.dumps({"swarm_id": "task-live-1"}), encoding="utf-8")
            board_content = (
                json.dumps({"seq": 1, "agent": "coordinator", "text": "@worker-1 add server.py entrypoint", "time": "2026-11-12 00:01:00"}) + "\n" +
                json.dumps({"seq": 2, "agent": "worker-1", "text": "@coordinator server.py finished", "time": "2026-11-12 00:02:00"}) + "\n"
            )
            (nat_swarm_dir / "board.jsonl").write_text(board_content, encoding="utf-8")

            script = f"""
const assert = require('assert');
const fs = require('fs');
const Module = require('module');
const path = require('path');

process.env.AG_TOOLS_DIR = {json.dumps(tmpdir)};
process.env.AG_GEMINI_DIR = {json.dumps(tmpdir)};
process.env.AG_GEMINI_HOME = {json.dumps(tmpdir)};

const openedDocs = [];
const commandExecs = [];
let lastQuickPickItems = [];
let nextQuickPickChoice = null;
let nextInputBoxChoice = null;

const vscodeMock = {{
    StatusBarAlignment: {{ Left: 1, Right: 2 }},
    Uri: {{ file: (p) => ({{ fsPath: p, toString: () => 'file://' + p }}) }},
    MarkdownString: class {{
        constructor(v) {{ this.value = v || ''; this.isTrusted = false; }}
        appendMarkdown(s) {{ this.value += (s || ''); return this; }}
    }},
    ThemeColor: class {{ constructor(id) {{ this.id = id; }} }},
    workspace: {{
        workspaceFolders: [{{ name: 'tests-suite', uri: {{ fsPath: {json.dumps(str(ROOT_DIR))} }} }}],
        getConfiguration: () => ({{ get: (_k, d) => d }}),
        onDidChangeConfiguration: () => ({{ dispose: () => {{}} }}),
        openTextDocument: async (filePath) => {{
            openedDocs.push(filePath);
            return {{ fileName: filePath }};
        }},
    }},
    window: {{
        createStatusBarItem: (alignment, priority) => ({{
            alignment, priority, text: '', tooltip: '', command: '', color: null,
            show() {{}}, hide() {{}}, dispose() {{}}
        }}),
        createOutputChannel: () => ({{ appendLine() {{}}, show() {{}}, dispose() {{}} }}),
        showInformationMessage: async () => undefined,
        showErrorMessage: async () => undefined,
        showTextDocument: async () => undefined,
        showQuickPick: async (items, options) => {{
            lastQuickPickItems = items;
            if (typeof nextQuickPickChoice === 'function') {{
                return nextQuickPickChoice(items, options);
            }}
            const choice = nextQuickPickChoice;
            nextQuickPickChoice = null;
            return choice;
        }},
        showInputBox: async (options) => {{
            const choice = nextInputBoxChoice;
            nextInputBoxChoice = null;
            return choice;
        }},
        onDidChangeWindowState: () => ({{ dispose: () => {{}} }}),
    }},
    commands: {{
        registerCommand: () => ({{ dispose: () => {{}} }}),
        getCommands: async () => ['antigravity.sendPromptToAgentPanel'],
        executeCommand: async (id, arg) => {{
            commandExecs.push({{ id, arg }});
            return undefined;
        }},
    }},
    env: {{ clipboard: {{ writeText: async () => {{}} }} }},
}};

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {{
    if (request === 'vscode') return vscodeMock;
    return origLoad.call(this, request, parent, isMain);
}};

Object.keys(require.cache).forEach(k => delete require.cache[k]);
const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});
ext._resetStateForTests();
const ctx = {{ subscriptions: [] }};
ext.activate(ctx);

(async () => {{
    // Level 1: showSwarmHubMenu shows active projects + history + quick launch option
    nextQuickPickChoice = null;
    await ext.showSwarmHubMenu();
    assert.ok(lastQuickPickItems.some(i => (i.action === 'quick_launch' || i.action === 'quick_launch_project')), 'Level 1 must offer quick launch option');
    assert.ok(lastQuickPickItems.some(i => i.action === 'open_session' && (i.label.includes('api-service') || i.label.includes('Refactor api endpoints'))), 'Level 1 must display active session card');

    // Quick launch project picker flow: select project -> prompt input -> chat panel injection
    nextQuickPickChoice = (items) => items.find(i => i.projectPath === {json.dumps(str(ROOT_DIR))});
    nextInputBoxChoice = 'Build test feature 42';
    await ext.quickLaunchSwarmTask();
    assert.ok(commandExecs.some(c => c.id === 'antigravity.sendPromptToAgentPanel' && c.arg.includes('Build test feature 42')), 'Quick launch must inject prompt into chat');

    // Level 2: showSwarmSessionDetails shows per-worker status & files & board action
    const activeCp = ext.listSwarmCheckpointsSync().find(c => c.task_id === 'task-live-1');
    assert.ok(activeCp, 'Active checkpoint not found');

    nextQuickPickChoice = null;
    await ext.showSwarmSessionDetails(activeCp);
    assert.ok(lastQuickPickItems.some(i => i.action === 'back'), 'Level 2 must have back button');
    assert.ok(lastQuickPickItems.some(i => i.action === 'open_board' && i.label.includes('сообщений')), 'Level 2 must have board button with count');
    assert.ok(lastQuickPickItems.some(i => i.action === 'resume' && i.label.includes('Продолжить в чате')), 'Level 2 must have resume button');
    assert.ok(lastQuickPickItems.some(i => i.action === 'delete' && i.label.includes('Удалить')), 'Level 2 must have delete button');
    assert.ok(lastQuickPickItems.some(i => i.action === 'open_worker_file' && i.files.includes('routes.py')), 'Level 2 must show worker file');

    // Test clicking worker file triggers vscode.workspace.openTextDocument
    nextQuickPickChoice = (items) => items.find(i => i.action === 'open_worker_file' && i.files.includes('routes.py'));
    await ext.showSwarmSessionDetails(activeCp);
    assert.ok(openedDocs.some(d => d.includes('routes.py')), 'Must open worker text document');

    // Level 3: showSwarmBoardMessages shows board.jsonl thread
    nextQuickPickChoice = null;
    await ext.showSwarmBoardMessages(activeCp);
    assert.ok(lastQuickPickItems.some(i => i.action === 'back'), 'Level 3 must have back button');
    assert.ok(lastQuickPickItems.some(i => i.label.includes('#1 @coordinator -> @worker-1')), 'Level 3 must show message 1');
    assert.ok(lastQuickPickItems.some(i => i.label.includes('#2 @worker-1 -> @coordinator')), 'Level 3 must show message 2');

    ext.deactivate();
    ext._resetStateForTests();
    console.log(JSON.stringify({{ ok: true }}));
}})().catch(err => {{
    console.error(err);
    process.exit(1);
}});
"""
            result = _run_node(script, env_extra={"AG_TOOLS_DIR": tmpdir})
            self.assertTrue(result["ok"], f"IDE Swarm QuickPick test failed: {result}")

    def test_ide_conversation_isolation_dirty_check_and_updater_lock(self):
        """Verifies:
        1. Conversation restore strictly looks into antigravity-ide/brain and ignores antigravity/brain.
        2. Heartbeat timeout marks stale agents (>90s) as idle/done.
        3. Updater lock locks releaseBaseUrl to prevent agy.exe auto-downloads.
        """
        import json as _json
        import sys
        parent = ROOT_DIR.parent
        peer_patcher = parent / "updater-guard" / "source"
        if peer_patcher.exists() and str(peer_patcher) not in sys.path:
            sys.path.insert(0, str(peer_patcher))
        elif str(ROOT_DIR / "source") not in sys.path:
            sys.path.insert(0, str(ROOT_DIR / "source"))
        from patcher.updater_lock import toggle_ide_updates, is_ide_locked

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            settings_dir = tmp_path / "Antigravity IDE" / "User"
            settings_dir.mkdir(parents=True, exist_ok=True)
            settings_file = settings_dir / "settings.json"

            # 1. Updater lock test
            old_appdata = os.environ.get("APPDATA")
            try:
                os.environ["APPDATA"] = str(tmp_path)
                if is_ide_locked(str(tmp_path)):
                    toggle_ide_updates(str(tmp_path))
                locked = toggle_ide_updates(str(tmp_path))
                self.assertTrue(locked)
                cfg = _json.loads(settings_file.read_text(encoding="utf-8"))
                self.assertEqual(cfg.get("update.mode"), "none")
                if "antigravity.releaseBaseUrl" in cfg:
                    self.assertEqual(cfg.get("antigravity.releaseBaseUrl"), "http://127.0.0.1:9")

                unlocked = toggle_ide_updates(str(tmp_path))
                self.assertFalse(unlocked)
                cfg2 = _json.loads(settings_file.read_text(encoding="utf-8"))
                self.assertEqual(cfg2.get("update.mode"), "default")
            finally:
                if old_appdata is not None:
                    os.environ["APPDATA"] = old_appdata
                else:
                    os.environ.pop("APPDATA", None)

            # 2. Conversation isolation test via Node
            script = textwrap.dedent(f"""
                const assert = require('assert');
                const path = require('path');
                const fs = require('fs');
                const Module = require('module');

                const gDir = path.join({json.dumps(str(tmp_path))}, 'gemini');
                const ideBrain = path.join(gDir, 'antigravity-ide', 'brain', 'ide-conv-1', '.system_generated', 'logs');
                const desktopBrain = path.join(gDir, 'antigravity', 'brain', 'desktop-conv-99', '.system_generated', 'logs');

                fs.mkdirSync(ideBrain, {{ recursive: true }});
                fs.mkdirSync(desktopBrain, {{ recursive: true }});

                // Write older IDE transcript and newer desktop transcript
                fs.writeFileSync(path.join(ideBrain, 'transcript.jsonl'), 'line1\\n');
                fs.utimesSync(path.join(ideBrain, 'transcript.jsonl'), 1000, 1000);

                fs.writeFileSync(path.join(desktopBrain, 'transcript.jsonl'), 'line2\\n');
                fs.utimesSync(path.join(desktopBrain, 'transcript.jsonl'), 2000, 2000);

                process.env.AG_GEMINI_DIR = gDir;
                process.env.AG_TOOLS_DIR = {json.dumps(tmpdir)};

                const vscodeMock = {{
                    StatusBarAlignment: {{ Left: 1, Right: 2 }},
                    Uri: {{ file: (p) => ({{ fsPath: p, toString: () => 'file://' + p }}) }},
                    MarkdownString: class {{
                        constructor(v) {{ this.value = v || ''; this.isTrusted = false; }}
                        appendMarkdown(s) {{ this.value += (s || ''); return this; }}
                    }},
                    ThemeColor: class {{ constructor(id) {{ this.id = id; }} }},
                    workspace: {{
                        workspaceFolders: [{{ name: 'test-ws', uri: {{ fsPath: {json.dumps(str(tmp_path))} }} }}],
                        getConfiguration: () => ({{ get: (_k, d) => d }}),
                        onDidChangeConfiguration: () => ({{ dispose: () => {{}} }}),
                    }},
                    window: {{
                        createStatusBarItem: (alignment, priority) => ({{
                            alignment, priority, text: '', tooltip: '', command: '', color: null,
                            show() {{}}, hide() {{}}, dispose() {{}}
                        }}),
                        createOutputChannel: () => ({{ appendLine() {{}}, show() {{}}, dispose() {{}} }}),
                        showInformationMessage: async () => undefined,
                        showErrorMessage: async () => undefined,
                        onDidChangeWindowState: () => ({{ dispose: () => {{}} }}),
                    }},
                    commands: {{
                        registerCommand: () => ({{ dispose: () => {{}} }}),
                        getCommands: async () => [],
                        executeCommand: async () => undefined,
                    }},
                    env: {{ clipboard: {{ writeText: async () => {{}} }} }},
                }};

                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === 'vscode') return vscodeMock;
                    return origLoad.call(this, request, parent, isMain);
                }};

                const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});
                // getActiveConversationId must ignore desktop-conv-99 and pick ide-conv-1
                const picked = ext.getActiveConversationId ? ext.getActiveConversationId() : null;
                assert.strictEqual(picked, 'ide-conv-1', 'getActiveConversationId must return ide-conv-1, ignoring desktop-conv-99');
                console.log(JSON.stringify({{ ok: true, picked }}));
            """)
            result = _run_node(script, env_extra={"AG_TOOLS_DIR": tmpdir})
            self.assertTrue(result["ok"])

    def test_streamlined_1_click_launch_menu(self):
        """Verifies showSwarmHubMenu in IDE lists current project and recent projects
        directly as 1-click launch targets without confusing redundant buttons."""
        script = textwrap.dedent(f"""
            const assert = require('assert');
            const Module = require('module');

            let quickPickItems = null;

            const vscodeMock = {{
                QuickPickItemKind: {{ Separator: -1, Default: 0 }},
                StatusBarAlignment: {{ Left: 1, Right: 2 }},
                Uri: {{ file: (p) => ({{ fsPath: p, toString: () => 'file://' + p }}) }},
                MarkdownString: class {{
                    constructor(v) {{ this.value = v || ''; this.isTrusted = false; }}
                    appendMarkdown(s) {{ this.value += (s || ''); return this; }}
                }},
                ThemeColor: class {{ constructor(id) {{ this.id = id; }} }},
                workspace: {{
                    workspaceFolders: [{{ name: 'ActiveProject', uri: {{ fsPath: 'D:\\\\work\\\\ActiveProject' }} }}],
                    getConfiguration: () => ({{ get: (_k, d) => d }}),
                    onDidChangeConfiguration: () => ({{ dispose: () => {{}} }}),
                }},
                window: {{
                    createStatusBarItem: (alignment, priority) => ({{
                        alignment, priority, text: '', tooltip: '', command: '', color: null,
                        show() {{}}, hide() {{}}, dispose() {{}}
                    }}),
                    createOutputChannel: () => ({{ appendLine() {{}}, show() {{}}, dispose() {{}} }}),
                    showInformationMessage: async () => undefined,
                    showErrorMessage: async () => undefined,
                    showQuickPick: async (items) => {{
                        quickPickItems = items;
                        return undefined;
                    }}
                }},
                commands: {{ registerCommand: () => ({{ dispose: () => {{}} }}) }}
            }};

            const origLoad = Module._load;
            Module._load = function (request, parent, isMain) {{
                if (request === 'vscode') return vscodeMock;
                return origLoad.call(this, request, parent, isMain);
            }};

            const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});

            ext.showSwarmHubMenu().then(() => {{
                assert.ok(Array.isArray(quickPickItems), 'Expected items array passed to showQuickPick');

                // 1. Clean separator: 'Новая задача для агентов (в 1 клик)'
                const sep = quickPickItems.find(it => it.label && it.label.includes('Новая задача для агентов (в 1 клик)'));
                assert.ok(sep, 'Separator "Новая задача для агентов (в 1 клик)" must exist');

                // 2. Active project listed directly as 1-click launch target
                const activeItem = quickPickItems.find(it => it.action === 'quick_launch_project' && it.projectPath === 'D:\\\\work\\\\ActiveProject');
                assert.ok(activeItem, 'Active workspace project must be listed as 1-click launch target');
                assert.ok(activeItem.label.includes('В текущем проекте: ActiveProject'));
                assert.ok(activeItem.description.includes('(активный) D:\\\\work\\\\ActiveProject'));

                // 3. Custom folder option
                const customItem = quickPickItems.find(it => it.action === 'quick_launch_custom');
                assert.ok(customItem, 'Option "Выбрать другую папку на диске..." must exist');
                assert.ok(customItem.label.includes('Выбрать другую папку на диске...'));

                // 4. Redundant duplicate items must NOT be present
                const duplicateOldQuick = quickPickItems.find(it => it.label && it.label.includes('+ Быстрая задача (ввести текст)'));
                assert.strictEqual(duplicateOldQuick, undefined, 'Duplicate "+ Быстрая задача (ввести текст)" must be removed');

                console.log(JSON.stringify({{ ok: true, count: quickPickItems.length }}));
                process.exit(0);
            }}).catch((err) => {{
                console.error(err);
                process.exit(1);
            }});
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])




class TestAppUpdaterPinSyncAndTooltipSync(unittest.TestCase):
    """App version/update checking, Windows AUMID/shortcut sync, and unified idle tooltips."""

    def setUp(self):
        import sys
        src = str(ROOT_DIR / "source")
        if src not in sys.path:
            sys.path.insert(0, src)

    def test_app_updater_version_discovery_and_parsers(self):
        from patcher import app_updater

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app_dir = tmp_path / "resources" / "app"
            out_dir = app_dir / "out"
            out_dir.mkdir(parents=True)
            main_js = out_dir / "main.js"
            main_js.write_text("/* main */", encoding="utf-8")
            (app_dir / "product.json").write_text(
                json.dumps({
                    "ideVersion": "2.6.0",
                    "version": "1.107.0",
                    "commit": "9e3a5da5d6731effd57d47d88bbe5df14ac6ccdd",
                    "win32AppUserModelId": "Google.AntigravityIDE",
                }),
                encoding="utf-8",
            )
            self.assertEqual(app_updater.get_installed_app_version("ide", str(main_js)), "2.6.0")

            cli_dir = tmp_path / "bin"
            cli_dir.mkdir()
            agy_exe = cli_dir / "agy.exe"
            agy_exe.write_bytes(b"MZ")
            (cli_dir / "agy.version.json").write_text(json.dumps({"version": "1.5.9"}), encoding="utf-8")
            self.assertEqual(app_updater.get_installed_app_version("cli", str(agy_exe)), "1.5.9")

            payload = {
                "version": "71bedb45abcdef",
                "ideVersion": "Antigravity IDE",
                "productVersion": "1.107.0",
                "url": "https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.7.0-4507555968450560/windows-x64/Antigravity IDE.exe",
                "sha256hash": "307c7a41",
            }
            self.assertEqual(app_updater._extract_version_from_ide_payload(payload), "2.7.0")
            self.assertTrue(app_updater.is_newer_version("2.7.0", "2.6.0"))
            self.assertFalse(app_updater.is_newer_version("2.6.0", "2.7.0"))

            yml = (
                "version: 2.33.0\n"
                "files:\n"
                "  - url: https://storage.googleapis.com/antigravity-public/antigravity-hub/2.33.0-123/windows-x64/Antigravity-x64.exe\n"
                "    sha512: ABCD==\n"
                "    size: 189004024\n"
            )
            parsed_yml = app_updater._parse_electron_builder_yml(yml)
            self.assertEqual(parsed_yml["version"], "2.33.0")
            self.assertIn("Antigravity-x64.exe", parsed_yml["url"])
            self.assertEqual(parsed_yml["sha512"], "ABCD==")
            self.assertEqual(parsed_yml["size"], 189004024)

            updated, new_p, upd_info = app_updater.check_and_install_component_update(
                "ide", str(main_js), is_locked_fn=lambda: True, quiet=True
            )
            self.assertFalse(updated)
            self.assertEqual(new_p, str(main_js))
            self.assertTrue(upd_info.get("skipped_locked"))

            import hashlib, base64
            sample = tmp_path / "sample.bin"
            data = b"antigravity-release-payload"
            sample.write_bytes(data)
            sha256_ok = hashlib.sha256(data).hexdigest()
            sha512_ok = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
            self.assertTrue(app_updater._verify_file_hashes(str(sample), sha256_hex=sha256_ok, sha512_b64=sha512_ok))
            self.assertFalse(app_updater._verify_file_hashes(str(sample), sha256_hex="0" * 64))

    def test_aumid_and_taskbar_shortcut_helpers(self):
        from patcher.utils import launcher

        ipc_js = (DESKTOP_DIR / "ipcHandlers.js").read_text(encoding="utf-8")
        self.assertIn("setAppUserModelId('electron.app.Antigravity')", ipc_js)

        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "Antigravity IDE"
            res_app = app_root / "resources" / "app"
            res_app.mkdir(parents=True)
            exe = app_root / "Antigravity IDE.exe"
            exe.write_bytes(b"MZ")
            (res_app / "product.json").write_text(json.dumps({"win32AppUserModelId": "Custom.AG.IDE"}), encoding="utf-8")
            self.assertEqual(launcher._resolve_expected_aumid("ide", str(exe)), "Custom.AG.IDE")
            self.assertEqual(launcher._resolve_expected_aumid("manager", str(exe)), "electron.app.Antigravity")

    def test_unified_idle_tooltip_matches_ide_and_20(self):
        script = textwrap.dedent(f"""
            const assert = require('assert');
            const bridge = require({json.dumps(str(CORE_DIR / 'swarm_bridge.js'))});
            const snap = bridge.buildAgentMonitorSnapshot({{
                nowMs: 50000,
                checkpoints: [
                    {{
                        task_id: 's1',
                        project_path: 'D:/work/Antigravity Patcher',
                        project_name: 'Antigravity Patcher',
                        user_request: 'Sync Desktop 2.0 live status pill with IDE, discover all projects and streamline 1-click launch UX',
                        is_complete: true,
                        created_at_ms: 1000,
                        updated_at_ms: 183000,
                        workers: [
                            {{ name: 'core-bridge', status: 'done', duration_seconds: 5100, task_description: '1. In listKnownProjects(): long technical prompt text' }},
                            {{ name: 'desktop-ui', status: 'done', duration_seconds: 5100, task_description: '1. Live Status Sync: internal prompt wall' }}
                        ]
                    }}
                ],
                nativeSwarms: [],
                conversations: [
                    {{
                        conversation_id: 'c-patch-2',
                        project_path: 'D:/work/Antigravity Patcher',
                        project_name: 'Antigravity Patcher',
                        title: 'Ускорение холодного старта меню патчера',
                        is_running: false,
                        is_killed: false,
                        last_user_input_ms: 1000,
                        last_modified_ms: 96000
                    }},
                    {{
                        conversation_id: 'c-patch-3',
                        project_path: 'D:/work/Antigravity Patcher',
                        project_name: 'Antigravity Patcher',
                        title: 'Скрытый запуск авто-патчера без мигания консоли',
                        is_running: false,
                        is_killed: false,
                        last_user_input_ms: 1000,
                        last_modified_ms: 61000
                    }},
                    {{
                        conversation_id: 'c-patch-4',
                        project_path: 'D:/work/Antigravity Patcher',
                        project_name: 'Antigravity Patcher',
                        title: 'Четвертая старая задача должна скрываться лимитом 3',
                        is_running: false,
                        is_killed: false,
                        last_user_input_ms: 1000,
                        last_modified_ms: 20000
                    }},
                    {{
                        conversation_id: 'c-mova-1',
                        project_path: 'D:/work/Контент мейкер',
                        project_name: 'Контент мейкер',
                        title: 'Mova Factory v3 — Quality Architecture',
                        is_running: false,
                        is_killed: false,
                        last_user_input_ms: 1000,
                        last_modified_ms: 140000
                    }}
                ]
            }});
            assert.strictEqual(snap.recent_projects.length, 2);
            assert.strictEqual(snap.recent_projects[0].project_name, 'Antigravity Patcher');
            assert.strictEqual(snap.recent_projects[0].tasks.length, 3, 'Each project must cap at 3 completed tasks');

            const tt = bridge.formatAgentTooltipSections(snap, []);
            assert.strictEqual(tt.lines[0], '⚡ Все агенты свободны');
            assert.strictEqual(tt.lines.filter((l) => l === '📁 Antigravity Patcher').length, 1);
            assert.strictEqual(tt.lines.filter((l) => l === '📁 Контент мейкер').length, 1);
            assert.ok(tt.lines.some((l) => l.includes('Sync Desktop 2.0 live status pill') && l.includes('…') && l.includes('(готово, 3м 2с)')), tt.plainText);
            assert.ok(tt.lines.some((l) => l.includes('Ускорение холодного старта меню патчера (готово, 1м 35с)')), tt.plainText);
            assert.ok(tt.lines.some((l) => l.includes('Скрытый запуск авто-патчера без мигания консоли (готово, 1м)')), tt.plainText);
            assert.ok(!tt.plainText.includes('Четвертая старая задача'), 'Must cap at 3 completed tasks per project');
            assert.ok(!tt.plainText.includes('↳'), 'Idle tooltip must not dump verbose subagent worker prompts');
            assert.ok(!tt.plainText.includes('core-bridge') && !tt.plainText.includes('listKnownProjects'), tt.plainText);
            console.log(JSON.stringify({{ ok: true }}));
        """)
        res = _run_node(script)
        self.assertTrue(res["ok"])

    def test_auto_repatch_hidden_vbs_sig_and_rate_limit(self):
        from unittest import mock
        from patcher import auto_repatch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            ps1_path = tmp_root / "AntigravityPatcher" / "auto_watch.ps1"
            ps1_path.parent.mkdir(parents=True, exist_ok=True)
            ps1_path.write_text("# watcher\n", encoding="utf-8")

            with mock.patch.object(auto_repatch, "_home_dir", return_value=tmp_root), \
                 mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_root)}):
                vbs_path = auto_repatch._write_hidden_vbs_wrapper(ps1_path)
                self.assertEqual(vbs_path.suffix.lower(), ".vbs")
                self.assertTrue(vbs_path.is_file())
                vbs_txt = vbs_path.read_text(encoding="utf-8")
                self.assertIn("WScript.Shell", vbs_txt)
                self.assertIn(", 0, False", vbs_txt)
                self.assertIn("-ExecutionPolicy Bypass", vbs_txt)

                captured_calls: list[tuple[list[str], dict]] = []

                def _fake_run(cmd, **kwargs):
                    captured_calls.append((list(cmd), kwargs))
                    m = mock.MagicMock()
                    m.returncode = 0
                    m.stdout = ""
                    m.stderr = ""
                    return m

                with mock.patch("patcher.auto_repatch.subprocess.run", side_effect=_fake_run), \
                     mock.patch("patcher.auto_repatch.os.name", "nt"):
                    ok_reg, _ = auto_repatch._register_windows_task(ps1_path)
                    self.assertTrue(ok_reg)
                    self.assertTrue(captured_calls)
                    cmd_args, cmd_kwargs = captured_calls[0]
                    self.assertIn("powershell", cmd_args[0].lower())
                    self.assertEqual(cmd_kwargs.get("creationflags"), auto_repatch.CREATE_NO_WINDOW)
                    joined_cmd = " ".join(cmd_args)
                    self.assertIn("New-ScheduledTaskAction -Execute 'wscript.exe'", joined_cmd)
                    self.assertIn("//B //Nologo", joined_cmd)
                    self.assertIn(str(vbs_path), joined_cmd)

                ps_body = auto_repatch._build_ps_watcher_script([r"C:\Tools\Patcher.exe"])
                self.assertIn("CreateNoWindow = $true", ps_body)
                self.assertIn("auto_watch.sig", ps_body)

                self.assertTrue(auto_repatch._should_check_app_updates(14400.0))
                auto_repatch._mark_app_updates_checked()
                self.assertFalse(auto_repatch._should_check_app_updates(14400.0))
                self.assertTrue(auto_repatch._should_check_app_updates(0.0))

    def test_cli_disk_status_cache_warm_hit_and_invalidation(self):
        from unittest import mock
        from patcher import cli

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            target_bin = tmp_root / "agy.exe"
            target_bin.write_bytes(b"binary-v1")
            cache_file = tmp_root / ".antigravity_tools" / "patch_status_cache.json"

            calls = 0

            def _checker(path_arg):
                nonlocal calls
                calls += 1
                return b"binary-v1" in Path(path_arg).read_bytes()

            orig_loaded = cli._DISK_CACHE_LOADED
            orig_map = dict(cli._TARGET_STATUS_CACHE)
            try:
                cli._DISK_CACHE_LOADED = False
                cli._TARGET_STATUS_CACHE.clear()
                with mock.patch.object(cli, "_status_cache_file", return_value=cache_file):
                    badge1 = cli._cached_target_badge("CLI", str(target_bin), _checker)
                    self.assertEqual(calls, 1)
                    self.assertTrue(cache_file.is_file())
                    self.assertIn("OK", badge1)

                    # Simulate fresh process startup: reset in-memory dict & loaded flag
                    cli._DISK_CACHE_LOADED = False
                    cli._TARGET_STATUS_CACHE.clear()
                    badge2 = cli._cached_target_badge("CLI", str(target_bin), _checker)
                    self.assertEqual(calls, 1, "Warm start must hit disk cache without re-scanning binary")
                    self.assertIn("OK", badge2)

                    # Change file size/mtime -> must invalidate cache and re-scan
                    target_bin.write_bytes(b"binary-v2-longer")
                    badge3 = cli._cached_target_badge("CLI", str(target_bin), _checker)
                    self.assertEqual(calls, 2)
                    self.assertIn("NEED PATCH", badge3)
            finally:
                cli._DISK_CACHE_LOADED = orig_loaded
                cli._TARGET_STATUS_CACHE.clear()
                cli._TARGET_STATUS_CACHE.update(orig_map)

    def test_auto_repatch_inspect_cache_runner_guard_and_quiet_heal(self):
        import contextlib
        import io
        from unittest import mock
        from patcher import auto_repatch, diagnostics

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            tools_dir = tmp_root / ".antigravity_tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            agy_bin = tmp_root / "agy.exe"
            agy_bin.write_bytes(b"agy-v1")
            exe_runner = tmp_root / "Antigravity_Patcher.exe"
            exe_runner.write_bytes(b"exe")
            ps1_path = tmp_root / "auto_watch.ps1"
            ps1_path.write_text("# Wait-SignatureSettled CreateNoWindow", encoding="utf-8")
            vbs_path = tmp_root / "auto_watch.vbs"
            vbs_path.write_text("' hidden launcher", encoding="utf-8")

            calls = 0

            def _fake_patch_check(path_arg):
                nonlocal calls
                calls += 1
                return b"agy-v1" not in Path(path_arg).read_bytes()

            with (
                mock.patch.object(auto_repatch, "_tools_dir", return_value=tools_dir),
                mock.patch("patcher.ide.discovery.find_install_root", return_value=None),
                mock.patch("patcher.manager.discovery.find_manager_binary", return_value=""),
                mock.patch("patcher.agy.discovery.find_agy_binary", return_value=str(agy_bin)),
                mock.patch("patcher.vscode.discovery.find_extension_js", return_value=""),
                mock.patch("patcher.vscode.discovery.find_gemini_antigravity_binary", return_value=str(agy_bin)),
                mock.patch.object(auto_repatch, "_is_extension_deployed", return_value=True),
                mock.patch.object(auto_repatch, "_is_desktop_ui_files_deployed", return_value=True),
                mock.patch("patcher.agy.patcher.is_already_patched", side_effect=_fake_patch_check),
            ):
                r1 = auto_repatch.inspect_targets()
                self.assertTrue(r1["needs_agy"])
                self.assertEqual(calls, 1, "Duplicate vscode_agy_path == agy_path must be checked once")

                r2 = auto_repatch.inspect_targets()
                self.assertTrue(r2["needs_agy"])
                self.assertEqual(calls, 1, "Second inspect_targets call must hit (mtime_ns, size) cache")

                agy_bin.write_bytes(b"agy-v2-patched")
                r3 = auto_repatch.inspect_targets()
                self.assertFalse(r3["needs_agy"])
                self.assertEqual(calls, 2, "Modified binary must invalidate cache")

            if sys.platform == "win32":
                with (
                    mock.patch.object(auto_repatch.os, "name", "nt"),
                    mock.patch.object(auto_repatch, "load_auto_repatch_config", return_value={"enabled": True, "runner": [str(exe_runner)]}),
                    mock.patch.object(auto_repatch, "_has_any_patched_target", return_value=True),
                    mock.patch.object(auto_repatch, "_watcher_script_path", return_value=ps1_path),
                    mock.patch.object(auto_repatch, "_vbs_launcher_path", return_value=vbs_path),
                    mock.patch.object(auto_repatch, "is_windows_task_registered", return_value=True),
                    mock.patch.object(auto_repatch, "enable_auto_repatch") as mock_enable,
                ):
                    auto_repatch.sync_auto_repatch_state()
                    mock_enable.assert_not_called()

        buf = io.StringIO()
        with (
            mock.patch.object(diagnostics, "_read_windows_registry_proxy", return_value=(False, "")),
            mock.patch.object(diagnostics, "_clear_dead_user_env_proxies", return_value=[]),
            mock.patch.object(diagnostics, "_get_dead_env_proxies", return_value=[]),
            mock.patch("patcher.data_reset.clean_compat_and_ifeo_registry", return_value={"compat_removed": [], "ifeo_removed": []}),
            mock.patch.object(diagnostics, "_check_cloudcode_connectivity", return_value={"reachable": True}),
            mock.patch.object(diagnostics, "_sync_target_update_lock", side_effect=lambda _t: print("noisy lock") or True),
            mock.patch("patcher.auto_repatch.sync_auto_repatch_state", side_effect=lambda **kw: print("noisy sync")),
            contextlib.redirect_stdout(buf),
        ):
            res = diagnostics.ensure_target_and_env_healthy(target="all", quiet=True)
        self.assertTrue(res["ok"])
        self.assertEqual(buf.getvalue(), "")

    def test_ide_updater_placeholder_url_nrpt_partial_check_and_cli_active_no_sig_swallow(self):
        from unittest import mock
        from patcher import app_updater, auto_repatch, nrpt_dns
        from patcher.ide import patcher as ide_patcher

        # 1. check_ide_update ignores placeholder updateUrl like https://example.com
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            prod = td_p / "product.json"
            prod.write_text(json.dumps({
                "ideVersion": "2.8.0",
                "commit": "abc123",
                "quality": "stable",
                "updateUrl": "https://example.com",
            }), encoding="utf-8")
            main_js = td_p / "out" / "main.js"
            main_js.parent.mkdir(parents=True, exist_ok=True)
            main_js.write_text("// main", encoding="utf-8")

            seen_urls = []

            def _fake_get(url, headers=None, timeout=4.0):
                seen_urls.append(url)
                return 200, json.dumps({
                    "url": "https://edgedl.me.gvt1.com/edgedl/release2/antigravity/stable/2.9.0-deadbeef/Antigravity.exe",
                    "sha256hash": "deadbeef",
                    "version": "deadbeef",
                })

            with mock.patch.object(app_updater, "_http_get_text", side_effect=_fake_get):
                res = app_updater.check_ide_update(str(main_js))
            self.assertTrue(seen_urls and seen_urls[0].startswith(app_updater.IDE_UPDATE_BASE_URL))
            self.assertTrue(res["update_available"])
            self.assertEqual(res["latest_version"], "2.9.0")

        # 2. check_dns_substitution_all flags partial resolvers (daily proxied, cloudcode-pa passthrough)
        def _fake_udp(srv, dom, timeout=2.0):
            if srv == "8.8.8.8":
                return ["172.217.116.4"]
            if dom == "daily-cloudcode-pa.googleapis.com":
                return ["193.233.112.67"]
            return ["172.217.116.4"]

        with mock.patch("extensions.core.hosts_pin.resolve_domain_udp", side_effect=_fake_udp):
            all_ok, subs, passes, _detail = nrpt_dns.check_dns_substitution_all("37.230.192.51")
            self.assertFalse(all_ok)
            self.assertEqual(subs, ["daily-cloudcode-pa.googleapis.com"])
            self.assertEqual(passes, ["cloudcode-pa.googleapis.com"])

        # 3. Dynamic openConversation injection on modern Antigravity IDE workbench
        wb_sample = (
            'var x=1;de(class extends xe{constructor(){super({id:"antigravity.startNewConversation",'
            'title:"New Conversation",keybinding:{primary:3114,weight:300}})}async run(e){await e.get(Q4e).'
            'triggerFocus({selectionSource:"auto",startNewConversation:!0,shouldCloseWhenFocused:!1})}}),'
            'de(class extends xe{constructor(){super({id:"antigravity.sendPromptToAgentPanel",'
            'title:"Send",f1:!1})}async run(t,e){t.get(bse).sendMessageToChatPanel?.(e)}});'
        )
        patched_wb, changed_wb = ide_patcher._inject_open_conversation_command(wb_sample)
        self.assertTrue(changed_wb)
        self.assertIn('id:"antigravity.openConversation"', patched_wb)
        self.assertIn('t.get(bse).togglePanelTab?.("conversation",e)', patched_wb)

        # 4. auto_watch.ps1 must NOT overwrite Save-WatchedSignature when Test-CliActive is true
        import re
        ps_txt = auto_repatch._build_ps_watcher_script(["python.exe", "main.py"])
        m_cli = re.search(r"if\s*\(\s*Test-CliActive\s*\)\s*\{+([\s\S]*?)\}+", ps_txt)
        self.assertIsNotNone(m_cli)
        self.assertNotIn("Save-WatchedSignature", m_cli.group(1))
        self.assertNotIn("$sig = $cur", m_cli.group(1))

    def test_ide_ext_429_nested_logs_hop_and_auto_continue(self):
        """extension.js: recursive 429 log watcher, --target/--workspace hop args, and auto-send continue."""
        import subprocess
        node_exe = shutil.which("node")
        if not node_exe:
            self.skipTest("node not available")

        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            ag_tools = td / ".antigravity_tools"
            gemini_dir = td / ".gemini"
            logs_root = td / "AppData" / "Antigravity IDE" / "logs"
            sess_dir = logs_root / "20261124T120000" / "window1" / "exthost" / "output_logging"
            sess_dir.mkdir(parents=True, exist_ok=True)
            ag_tools.mkdir(parents=True, exist_ok=True)
            (gemini_dir / "antigravity-ide" / "brain" / "conv-123" / ".system_generated" / "logs").mkdir(parents=True, exist_ok=True)
            (gemini_dir / "antigravity-ide" / "brain" / "conv-123" / ".system_generated" / "logs" / "transcript.jsonl").write_text(
                json.dumps({"cwd": r"D:\Project With Spaces"}) + "\n", encoding="utf-8"
            )
            ls_log = sess_dir / "1-Antigravity LS.log"
            ls_log.write_text("startup ok\n", encoding="utf-8")

            js_script = f"""
                const assert = require('assert');
                const fs = require('fs');
                const path = require('path');
                const Module = require('module');

                process.env.AG_TOOLS_DIR = {json.dumps(str(ag_tools))};
                process.env.AG_GEMINI_DIR = {json.dumps(str(gemini_dir))};
                process.env.APPDATA = {json.dumps(str(td / "AppData"))};

                const sentCommands = [];
                const execFileCalls = [];
                const shownMessages = [];

                const vscodeMock = {{
                    workspace: {{
                        workspaceFolders: [{{ name: 'Proj', uri: {{ fsPath: 'D:\\\\Project With Spaces' }} }}],
                        getConfiguration: () => ({{
                            get: (k) => k === 'failoverPolicy' ? 'account_hop' : (k === 'currentModel' ? 'claude' : undefined),
                            update: async () => {{}}
                        }}),
                        onDidChangeConfiguration: () => ({{ dispose: () => {{}} }}),
                        saveAll: async () => true,
                    }},
                    window: {{
                        state: {{ focused: true }},
                        createStatusBarItem: () => ({{ show() {{}}, hide() {{}}, dispose() {{}} }}),
                        createOutputChannel: () => ({{ appendLine() {{}}, show() {{}}, dispose() {{}} }}),
                        onDidChangeWindowState: () => ({{ dispose: () => {{}} }}),
                        showInformationMessage: async (msg, ...btns) => {{ shownMessages.push({{ msg, btns }}); return undefined; }},
                        showWarningMessage: async (msg, ...btns) => {{ shownMessages.push({{ msg, btns }}); return undefined; }},
                        showErrorMessage: async (msg) => {{ shownMessages.push({{ msg }}); return undefined; }},
                    }},
                    commands: {{
                        registerCommand: () => ({{ dispose: () => {{}} }}),
                        getCommands: async () => ['antigravity.openConversation', 'antigravity.sendPromptToAgentPanel'],
                        executeCommand: async (cmd, arg) => {{ sentCommands.push({{ cmd, arg }}); return true; }}
                    }},
                    env: {{ appName: 'Antigravity IDE', clipboard: {{ writeText: async () => {{}} }} }},
                    ConfigurationTarget: {{ Global: 1 }},
                    StatusBarAlignment: {{ Right: 2 }}
                }};

                const origLoad = Module._load;
                Module._load = function (request, parent, isMain) {{
                    if (request === 'vscode') return vscodeMock;
                    if (request === 'child_process') {{
                        return {{
                            exec: (cmd, _opts, cb) => cb && cb(null, '', ''),
                            execFile: (bin, args, opts, cb) => {{
                                if (typeof opts === 'function') cb = opts;
                                execFileCalls.push({{ bin, args }});
                                cb && cb(null, '{{"ok": true}}', '');
                            }}
                        }};
                    }}
                    return origLoad.call(this, request, parent, isMain);
                }};

                const ext = require({json.dumps(str(IDE_DIR / 'extension.js'))});

                (async () => {{
                    ext._resetStateForTests();
                    // 1. Recursive 429 detection inside nested Antigravity IDE/logs/<session>/...
                    assert.strictEqual(ext.checkDirFor429({json.dumps(str(logs_root))}), false);
                    fs.appendFileSync({json.dumps(str(ls_log))}, 'E1124 12:00:05 errorreport.go:307] RESOURCE_EXHAUSTED (code 429): Individual quota reached\\n');
                    assert.strictEqual(ext.checkDirFor429({json.dumps(str(logs_root))}), true);

                    // 2. tryRestorePreviousConversation auto-sends 'continue' when auto_send_resume === true without blocking modal
                    const restoreFile = path.join(process.env.AG_TOOLS_DIR, 'restore_conversation.json');
                    fs.writeFileSync(restoreFile, JSON.stringify({{
                        cascadeId: 'conv-123',
                        timestamp: Date.now(),
                        resume_message: 'continue',
                        resume_reason: '429_account_hop',
                        auto_send_resume: true,
                        model: 'claude'
                    }}));
                    ext.tryRestorePreviousConversation();
                    await new Promise((r) => setTimeout(r, 7100));
                    assert.ok(sentCommands.some((c) => c.cmd === 'antigravity.openConversation' && c.arg === 'conv-123'));
                    assert.ok(sentCommands.some((c) => c.cmd === 'antigravity.sendPromptToAgentPanel' && c.arg === 'continue'));
                    assert.ok(!shownMessages.some((m) => Array.isArray(m.btns) && m.btns.includes('Да, продолжить')));
                }})().then(() => process.exit(0)).catch((e) => {{ console.error(e); process.exit(1); }});
            """
            res = subprocess.run([node_exe, "-e", js_script], capture_output=True, text=True, timeout=20)
            self.assertEqual(res.returncode, 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
