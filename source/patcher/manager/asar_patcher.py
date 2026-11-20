import os
import re
import sys
import json
import copy
import shutil
import struct
import hashlib
import filecmp
import tempfile
from pathlib import Path

from patcher.manager.discovery import find_asar_relative_to_manager
from patcher.utils.admin import terminate_standalone_processes
from patcher.utils.console import info, ok, warn, err, step
from patcher.utils.file import fix_posix_permissions, ensure_macos_writable
from patcher.utils.atomic import write_atomic, sweep_stale_temps


ASAR_BAK_EXT = ".backup"

_UTILS_START = "// AG_TOOLKIT_UI_INJECT_START"
_UTILS_END = "// AG_TOOLKIT_UI_INJECT_END"
_UTILS_FAIL_START = "// AG_TOOLKIT_FAIL_RECOVER_START"
_UTILS_FAIL_END = "// AG_TOOLKIT_FAIL_RECOVER_END"

_PRELOAD_START = "// AG_TOOLKIT_PRELOAD_START"
_PRELOAD_END = "// AG_TOOLKIT_PRELOAD_END"

_IPC_START = "// === AG Toolkit Desktop IPC Handlers ==="
_IPC_END = "// === End AG Toolkit Desktop IPC Handlers ==="

_LS_START = "// AG_TOOLKIT_LS_START"
_LS_END = "// AG_TOOLKIT_LS_END"
_LS_PRE_START = "// AG_TOOLKIT_LS_PRE_START"
_LS_PRE_END = "// AG_TOOLKIT_LS_PRE_END"
_LS_ENV_START = "// AG_TOOLKIT_LS_ENV_START"
_LS_ENV_END = "// AG_TOOLKIT_LS_ENV_END"
_LS_PORT_RESET_MARKER = "// AG_TOOLKIT_LS_PORT_RESET"

_UTILS_BLOCK = """                // AG_TOOLKIT_UI_INJECT_START
                try {
                    const _agOs = require('os');
                    const _agPath = require('path');
                    const _agFs = require('fs');
                    const _agUiPath = _agPath.join(_agOs.homedir(), '.antigravity_tools', 'ag_desktop_ui.js');
                    if (_agFs.existsSync(_agUiPath)) {
                        const _agJs = _agFs.readFileSync(_agUiPath, 'utf8');
                        hubContents.executeJavaScript(_agJs).catch(() => {});
                    }
                } catch (_) {}
                // AG_TOOLKIT_UI_INJECT_END"""

_UTILS_FAIL_BLOCK = r"""    // AG_TOOLKIT_FAIL_RECOVER_START
    let _agFailRetries = 0;
    let _agFailTimer = null;
    const _agClearFailTimer = () => {
        if (_agFailTimer) {
            clearTimeout(_agFailTimer);
            _agFailTimer = null;
        }
    };
    const _agOnFailFinishLoad = () => {
        if (win.isDestroyed?.() || hubContents.isDestroyed?.()) return;
        const curUrl = String(hubContents.getURL?.() || '');
        if (!curUrl || curUrl.startsWith('chrome-error://') || !/^https?:\/\//i.test(curUrl)) return;
        _agFailRetries = 0;
        _agClearFailTimer();
    };
    const _agOnDidFailLoad = (_ev, errorCode, _desc, validatedURL, isMainFrame) => {
        if (!isMainFrame || errorCode === -3) return;
        if (win.isDestroyed?.() || hubContents.isDestroyed?.()) return;
        const failedUrl = String(validatedURL || '');
        if (!/^(?:https?:\/\/(?:127\.0\.0\.1|localhost):\d+|chrome-error:\/\/)/i.test(failedUrl)) return;
        if (_agFailRetries >= 20) return;
        _agFailRetries++;
        _agClearFailTimer();
        const delayMs = _agFailRetries <= 4 ? 350 : 750;
        const _retryTick = () => {
            _agFailTimer = null;
            if (win.isDestroyed?.() || hubContents.isDestroyed?.()) return;
            try {
                const _agLsMod = require('./languageServer');
                const _agProc = _agLsMod.getLsProcess?.();
                const _agPort = Number(_agLsMod.getLsPort?.() || 0);
                if (_agPort > 0 && _agProc && !_agProc.killed && _agProc.exitCode === null) {
                    const _agTargetUrl = `https://127.0.0.1:${_agPort}/`;
                    const _agCurNow = String(hubContents.getURL?.() || '');
                    if (_agCurNow === _agTargetUrl && !hubContents.isLoading?.()) {
                        return;
                    }
                    void (win.loadURL ? win.loadURL(_agTargetUrl) : hubContents.loadURL(_agTargetUrl));
                    return;
                }
            } catch (_) {}
            if (_agFailRetries < 20) {
                _agFailRetries++;
                _agFailTimer = setTimeout(_retryTick, 750);
            }
        };
        _agFailTimer = setTimeout(_retryTick, delayMs);
    };
    const _agFallbackUiInject = () => {
        if (storageManager || win.isDestroyed?.() || hubContents.isDestroyed?.()) return;
        try {
            const _agUiPath = require('path').join(require('os').homedir(), '.antigravity_tools', 'ag_desktop_ui.js');
            if (require('fs').existsSync(_agUiPath)) {
                hubContents.executeJavaScript(require('fs').readFileSync(_agUiPath, 'utf8')).catch(() => {});
            }
        } catch (_) {}
    };
    hubContents.on('did-finish-load', _agOnFailFinishLoad);
    hubContents.on('did-fail-load', _agOnDidFailLoad);
    if (!storageManager) {
        hubContents.on('did-finish-load', _agFallbackUiInject);
    }
    win.once?.('closed', () => {
        _agClearFailTimer();
        hubContents.off?.('did-finish-load', _agOnFailFinishLoad);
        hubContents.off?.('did-fail-load', _agOnDidFailLoad);
        if (!storageManager) {
            hubContents.off?.('did-finish-load', _agFallbackUiInject);
        }
    });
    // AG_TOOLKIT_FAIL_RECOVER_END"""

_LS_BLOCK = """// AG_TOOLKIT_LS_START
function resetRestartCount() {
    _restartCount = 0;
    _lastRestartTime = 0;
}
exports.resetRestartCount = resetRestartCount;
// AG_TOOLKIT_LS_END"""

_LS_PRE_BLOCK = r"""    // AG_TOOLKIT_LS_PRE_START
    try {
        const _agFs = require('fs');
        const _agPath = require('path');
        const _agOs = require('os');
        const _agNet = require('net');
        const _agCp = require('child_process');
        const _agCfgPath = _agPath.join(_agOs.homedir(), '.antigravity_tools', 'proxy_config.json');
        const _agProbeHost = (host, port, ms = 120) => new Promise((res) => {
            let done = false;
            const s = new _agNet.Socket();
            const finish = (ok) => {
                if (done) return;
                done = true;
                try { s.destroy(); } catch (_) {}
                res(ok);
            };
            s.setTimeout(ms);
            s.once('connect', () => finish(true));
            s.once('timeout', () => finish(false));
            s.once('error', () => finish(false));
            try { s.connect(port, host || '127.0.0.1'); } catch (_) { finish(false); }
        });
        if (process.platform === 'win32') {
            await new Promise((resKill) => {
                const _ps = "Get-CimInstance Win32_Process -Filter \"Name='language_server.exe'\" -ErrorAction SilentlyContinue | ForEach-Object { if (-not (Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue)) { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }";
                _agCp.execFile('powershell', ['-NoProfile', '-NonInteractive', '-Command', _ps], { timeout: 900, windowsHide: true }, () => resKill());
            });
        }
        let _agPreProxy = String(process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || '').trim();
        if (!_agPreProxy && _agFs.existsSync(_agCfgPath)) {
            try {
                const _cfg = JSON.parse(_agFs.readFileSync(_agCfgPath, 'utf8'));
                const _saved = String((_cfg && (_cfg.proxy_url || _cfg.url)) || '').trim();
                const _src = String((_cfg && _cfg.source) || '').trim();
                if (_saved) {
                    const _full = _saved.includes('://') ? _saved : ('http://' + _saved);
                    let _okSaved = true;
                    try {
                        const _u = new URL(_full);
                        const _hp = parseInt(_u.port, 10);
                        if (_u.hostname && _hp) _okSaved = await _agProbeHost(_u.hostname, _hp, 120);
                    } catch (_) {}
                    if (_okSaved) {
                        _agPreProxy = _full;
                    } else if (_src.startsWith('Local VPN')) {
                        try { _agFs.unlinkSync(_agCfgPath); } catch (_) {}
                    }
                }
            } catch (_) {}
        }
        if (!_agPreProxy) {
            const _ports = [10808, 10809, 2080, 7890, 2081, 7891, 8889, 12334];
            const _found = (await Promise.all(_ports.map(async (p) => (await _agProbeHost('127.0.0.1', p, 120)) ? p : 0))).find((p) => p > 0);
            if (_found) {
                _agPreProxy = `http://127.0.0.1:${_found}`;
                try {
                    _agFs.mkdirSync(_agPath.dirname(_agCfgPath), { recursive: true });
                    _agFs.writeFileSync(_agCfgPath, JSON.stringify({ proxy_url: _agPreProxy, source: `Local VPN (${_found})`, updated_at: Math.floor(Date.now() / 1000) }, null, 2), 'utf8');
                } catch (_) {}
            }
        }
        if (_agPreProxy) {
            process.env.HTTP_PROXY = _agPreProxy;
            process.env.HTTPS_PROXY = _agPreProxy;
            process.env.ALL_PROXY = _agPreProxy;
            process.env.http_proxy = _agPreProxy;
            process.env.https_proxy = _agPreProxy;
            process.env.all_proxy = _agPreProxy;
        }
    } catch (_) {}
    // AG_TOOLKIT_LS_PRE_END"""

_LS_ENV_BLOCK = r"""        // AG_TOOLKIT_LS_ENV_START
        try {
            env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
            env.AGY_PROXY_STRICT_SSL = 'false';
            env.NO_PROXY = 'localhost,127.0.0.1,::1';
            env.no_proxy = 'localhost,127.0.0.1,::1';
            env.GODEBUG = env.GODEBUG
                ? (env.GODEBUG.includes('http2client=') ? env.GODEBUG : env.GODEBUG + ',http2client=0')
                : 'http2client=0';
            process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
            process.env.AGY_PROXY_STRICT_SSL = 'false';
            process.env.NO_PROXY = 'localhost,127.0.0.1,::1';
            process.env.no_proxy = 'localhost,127.0.0.1,::1';
            process.env.GODEBUG = env.GODEBUG;
            let _agProxy = String(env.HTTPS_PROXY || env.https_proxy || env.HTTP_PROXY || env.http_proxy || env.ALL_PROXY || env.all_proxy || process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || '').trim();
            const _agCp = require('child_process');
            if (!_agProxy && process.platform === 'win32') {
                try {
                    const _regOut = _agCp.execFileSync('reg', ['query', 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings'], { encoding: 'utf8', timeout: 1200, windowsHide: true });
                    const _enM = _regOut.match(/^\s*ProxyEnable\s+REG_DWORD\s+(0x[0-9a-fA-F]+|\d+)/im);
                    if (_enM && parseInt(_enM[1], _enM[1].toLowerCase().startsWith('0x') ? 16 : 10) !== 0) {
                        const _srvM = _regOut.match(/^\s*ProxyServer\s+REG_(?:EXPAND_)?SZ\s+([^\r\n]+)/im);
                        if (_srvM && _srvM[1]) {
                            let _raw = _srvM[1].trim();
                            if (_raw.includes('=')) {
                                const _hm = _raw.match(/(?:^|;)https?=([^;]+)/i);
                                _raw = _hm ? _hm[1].trim() : '';
                            }
                            if (_raw) _agProxy = _raw.includes('://') ? _raw : ('http://' + _raw);
                        }
                    }
                } catch (_) {}
            }
            if (_agProxy) {
                if (!_agProxy.includes('://')) _agProxy = 'http://' + _agProxy;
                env.HTTP_PROXY = _agProxy;
                env.HTTPS_PROXY = _agProxy;
                env.ALL_PROXY = _agProxy;
                env.http_proxy = _agProxy;
                env.https_proxy = _agProxy;
                env.all_proxy = _agProxy;
                process.env.HTTP_PROXY = _agProxy;
                process.env.HTTPS_PROXY = _agProxy;
                process.env.ALL_PROXY = _agProxy;
                process.env.http_proxy = _agProxy;
                process.env.https_proxy = _agProxy;
                process.env.all_proxy = _agProxy;
            }
        } catch (_) {}
        // AG_TOOLKIT_LS_ENV_END"""

_PRELOAD_BLOCK = """// AG_TOOLKIT_PRELOAD_START
const agAPI = {
    getState: () => electron_1.ipcRenderer.invoke('ag:get-state'),
    switchAccount: (targetId) => electron_1.ipcRenderer.invoke('ag:switch-account', targetId),
    fix400: () => electron_1.ipcRenderer.invoke('ag:fix-400'),
    refresh: (mode) => electron_1.ipcRenderer.invoke('ag:refresh', mode),
    openLog: () => electron_1.ipcRenderer.invoke('ag:open-log'),
    swarm: (op, payload) => electron_1.ipcRenderer.invoke('ag:swarm', op, payload),
    guardian: (op, payload) => electron_1.ipcRenderer.invoke('ag:guardian', op, payload),
    onStateUpdated: (cb) => {
        if (typeof cb !== 'function') return () => {};
        const handler = (_event, data) => cb(data);
        electron_1.ipcRenderer.on('ag:state-updated', handler);
        return () => electron_1.ipcRenderer.removeListener('ag:state-updated', handler);
    },
    onGuardianAlert: (cb) => {
        if (typeof cb !== 'function') return () => {};
        const handler = (_event, data) => cb(data);
        electron_1.ipcRenderer.on('ag:guardian-alert', handler);
        return () => electron_1.ipcRenderer.removeListener('ag:guardian-alert', handler);
    }
};
electron_1.contextBridge.exposeInMainWorld('agAPI', agAPI);
// AG_TOOLKIT_PRELOAD_END"""


def _load_ipc_handler_block() -> str | None:
    """Load canonical IPC handler block from bundled extensions/desktop/ipcHandlers.js."""
    from patcher.extension_installer import _find_extensions_root

    ext_root = _find_extensions_root()
    if not ext_root:
        return None
    src = ext_root / "desktop" / "ipcHandlers.js"
    if not src.is_file():
        return None
    try:
        text = src.read_text(encoding="utf-8")
    except Exception:
        return None

    start_idx = text.find(_IPC_START)
    if start_idx == -1:
        return None

    # Include leading spaces on the marker line
    line_start = text.rfind("\n", 0, start_idx)
    block_start = line_start + 1 if line_start != -1 else start_idx

    end_idx = text.find(_IPC_END, start_idx)
    if end_idx != -1:
        block_end = end_idx + len(_IPC_END)
        return text[block_start:block_end]

    wcv_m = re.search(r"\n[ \t]*\(0,\s*wcvIpcHandlers_1\.registerWcvIpcHandlers\)\([^)]*\);", text[start_idx:])
    if wcv_m:
        raw_block = text[block_start : start_idx + wcv_m.start()].rstrip()
        return raw_block + "\n    " + _IPC_END
    return None


def _read_asar_header(f):
    f.seek(0)
    raw_prefix = f.read(16)
    if len(raw_prefix) < 16:
        raise ValueError("Invalid ASAR header (file too small)")
    u0, header_size, h2, json_size = struct.unpack("<IIII", raw_prefix)
    if u0 != 4 or json_size <= 0:
        raise ValueError("Unsupported ASAR pickle header")
    json_bytes = f.read(json_size)
    if len(json_bytes) != json_size:
        raise ValueError("Truncated ASAR JSON header")
    header = json.loads(json_bytes.decode("utf-8"))
    payload_offset = 8 + header_size
    return header, payload_offset


def _get_node(header: dict, rel_path: str) -> dict | None:
    node = header
    for part in rel_path.split("/"):
        files = node.get("files")
        if not isinstance(files, dict) or part not in files:
            return None
        node = files[part]
    return node


def _collect_packed_nodes(node: dict, prefix: str = "") -> list[tuple[str, dict]]:
    out = []
    files = node.get("files")
    if not isinstance(files, dict):
        return out
    for name, child in files.items():
        child_path = f"{prefix}/{name}" if prefix else name
        if "files" in child:
            out.extend(_collect_packed_nodes(child, child_path))
        elif "offset" in child and not child.get("unpacked") and "link" not in child:
            out.append((child_path, child))
    return out


def _compute_integrity(data: bytes, block_size: int = 4194304) -> dict:
    full_hash = hashlib.sha256(data).hexdigest()
    if len(data) == 0:
        blocks = [full_hash]
    else:
        blocks = [
            hashlib.sha256(data[i : i + block_size]).hexdigest()
            for i in range(0, len(data), block_size)
        ]
    return {
        "algorithm": "SHA256",
        "hash": full_hash,
        "blockSize": block_size,
        "blocks": blocks,
    }


def _rewrite_asar(asar_path: str, replacements: dict[str, bytes]) -> None:
    """Atomically rebuild an ASAR archive with replaced file payloads and updated integrity."""
    asar_dir = os.path.dirname(os.path.abspath(asar_path))
    fd, tmp_payload = tempfile.mkstemp(prefix=".ag_payload_", dir=asar_dir)
    tmp_asar = None
    try:
        with open(asar_path, "rb") as src:
            orig_header, orig_base = _read_asar_header(src)
            new_header = copy.deepcopy(orig_header)

            orig_nodes = _collect_packed_nodes(orig_header)
            orig_nodes.sort(key=lambda item: int(item[1]["offset"]))

            new_nodes_map = dict(_collect_packed_nodes(new_header))

            running_offset = 0
            with os.fdopen(fd, "wb") as pf:
                fd = -1
                for rel_path, orig_meta in orig_nodes:
                    target_meta = new_nodes_map[rel_path]
                    if rel_path in replacements:
                        data = replacements[rel_path]
                        target_meta["size"] = len(data)
                        if "integrity" in target_meta:
                            bs = target_meta["integrity"].get("blockSize", 4194304)
                            target_meta["integrity"] = _compute_integrity(data, bs)
                        pf.write(data)
                        target_meta["offset"] = str(running_offset)
                        running_offset += len(data)
                    else:
                        size = int(orig_meta["size"])
                        src.seek(orig_base + int(orig_meta["offset"]))
                        remaining = size
                        while remaining > 0:
                            chunk = src.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                raise IOError(f"Unexpected EOF reading {rel_path} from ASAR")
                            pf.write(chunk)
                            remaining -= len(chunk)
                        target_meta["offset"] = str(running_offset)
                        running_offset += size

            json_bytes = json.dumps(new_header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            json_size = len(json_bytes)
            padded_size = (json_size + 3) & ~3
            pad_len = padded_size - json_size
            pickle_header = struct.pack(
                "<IIII",
                4,
                padded_size + 8,
                padded_size + 4,
                json_size,
            )

            sweep_stale_temps(asar_dir)
            out_fd, tmp_asar = tempfile.mkstemp(prefix=".ag_asar_", suffix=".agtmp", dir=asar_dir)
            with os.fdopen(out_fd, "wb") as dst, open(tmp_payload, "rb") as pf:
                dst.write(pickle_header)
                dst.write(json_bytes)
                if pad_len:
                    dst.write(b"\x00" * pad_len)
                shutil.copyfileobj(pf, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())

        fix_posix_permissions(tmp_asar)
        if os.path.exists(asar_path):
            shutil.copymode(asar_path, tmp_asar)
        os.replace(tmp_asar, asar_path)
        tmp_asar = None
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_payload):
            try:
                os.unlink(tmp_payload)
            except OSError:
                pass
        if tmp_asar and os.path.exists(tmp_asar):
            try:
                os.unlink(tmp_asar)
            except OSError:
                pass


def _patch_utils_js(text: str) -> tuple[str, str]:
    eol = "\r\n" if "\r\n" in text else "\n"
    block = _UTILS_BLOCK.replace("\n", eol)

    # 1. Inject/upgrade UI injection inside handleDidFinishLoad
    new_text = text
    marked_re = re.compile(
        r"[ \t]*" + re.escape(_UTILS_START) + r"[\s\S]{0,1200}?" + re.escape(_UTILS_END)
    )
    legacy_re = re.compile(
        r"[ \t]*try\s*\{\s*(?:const|let|var)\s+_uiPath\s*=[^\n]*ag_desktop_ui\.js[\s\S]{0,500}?\}\s*catch\s*\([^)]*\)\s*\{\s*\}"
    )
    anchor_re = re.compile(
        r"(const\s+handleDidFinishLoad\s*=\s*\([^)]*\)\s*=>\s*\{[\s\S]{0,300}?void\s+applyStoredZoomLevel\(\);)"
    )

    if marked_re.search(new_text):
        new_text = marked_re.sub(lambda _m: block, new_text, count=1)
    elif legacy_re.search(new_text):
        new_text = legacy_re.sub(lambda _m: block, new_text, count=1)
    else:
        m = anchor_re.search(new_text)
        if not m:
            return text, "not-found"
        new_text = new_text[: m.end(1)] + eol + block + new_text[m.end(1) :]

    # 2. Inject/upgrade did-fail-load auto-recovery in createWindow when full utils.js is present
    load_url_re = re.compile(r"([ \t]*void\s+win\.loadURL\(\s*url\s*\);)")
    if "hubContents.on('did-finish-load'" in new_text and load_url_re.search(new_text):
        fail_block = _UTILS_FAIL_BLOCK.replace("\n", eol)
        fail_marked_re = re.compile(
            r"[ \t]*" + re.escape(_UTILS_FAIL_START) + r"[\s\S]{0,4000}?" + re.escape(_UTILS_FAIL_END)
        )
        if fail_marked_re.search(new_text):
            new_text = fail_marked_re.sub(lambda _m: fail_block, new_text, count=1)
        else:
            lm = load_url_re.search(new_text)
            if not lm:
                return text, "not-found"
            new_text = new_text[: lm.start(1)] + fail_block + eol + new_text[lm.start(1) :]

    return new_text, ("already" if new_text == text else "patched")


def _patch_language_server_js(text: str) -> tuple[str, str]:
    eol = "\r\n" if "\r\n" in text else "\n"
    new_text = text

    if _LS_PORT_RESET_MARKER not in new_text:
        clear_re = re.compile(
            r"(function\s+clearLsProcess\s*\(\s*\)\s*\{\s*\r?\n[ \t]*_lsProcess\s*=\s*null;)"
        )
        m_clr = clear_re.search(new_text)
        if not m_clr:
            return text, "not-found"
        new_text = (
            new_text[: m_clr.end(1)]
            + eol
            + "    _lsPort = 0; "
            + _LS_PORT_RESET_MARKER
            + new_text[m_clr.end(1) :]
        )

    ls_pre_block = _LS_PRE_BLOCK.replace("\n", eol)
    pre_marked_re = re.compile(
        r"[ \t]*" + re.escape(_LS_PRE_START) + r"[\s\S]{0,4500}?" + re.escape(_LS_PRE_END)
    )
    if pre_marked_re.search(new_text):
        new_text = pre_marked_re.sub(lambda _m: ls_pre_block, new_text, count=1)
    else:
        pre_anchor_re = re.compile(
            r"(const\s+loginShellEnv\s*=\s*await\s+shellEnvReads\.take\(\);)"
        )
        m_pre = pre_anchor_re.search(new_text)
        if m_pre:
            new_text = new_text[: m_pre.end(1)] + eol + ls_pre_block + new_text[m_pre.end(1) :]

    ls_env_block = _LS_ENV_BLOCK.replace("\n", eol)
    env_marked_re = re.compile(
        r"[ \t]*" + re.escape(_LS_ENV_START) + r"[\s\S]{0,6000}?" + re.escape(_LS_ENV_END)
    )
    if env_marked_re.search(new_text):
        new_text = env_marked_re.sub(lambda _m: ls_env_block, new_text, count=1)
    else:
        env_anchor_re = re.compile(
            r"(const\s+env\s*=\s*\{\s*\.\.\.process\.env\s*,\s*\.\.\.loginShellEnv\s*\};)"
        )
        m_env = env_anchor_re.search(new_text)
        if m_env:
            new_text = new_text[: m_env.end(1)] + eol + ls_env_block + new_text[m_env.end(1) :]

    ls_block = _LS_BLOCK.replace("\n", eol)
    marked_re = re.compile(
        re.escape(_LS_START) + r"[\s\S]{0,600}?" + re.escape(_LS_END)
    )
    if marked_re.search(new_text):
        new_text = marked_re.sub(lambda _m: ls_block, new_text, count=1)
    else:
        anchor_re = re.compile(
            r"(exports\.setIntentionalTermination\s*=\s*setIntentionalTermination;)"
        )
        m_exp = anchor_re.search(new_text)
        if not m_exp:
            return text, "not-found"
        new_text = new_text[: m_exp.end(1)] + eol + ls_block + new_text[m_exp.end(1) :]

    return new_text, ("already" if new_text == text else "patched")


def _patch_preload_js(text: str) -> tuple[str, str]:
    eol = "\r\n" if "\r\n" in text else "\n"
    block = _PRELOAD_BLOCK.replace("\n", eol)

    # 1. Replace marked block
    marked_re = re.compile(
        re.escape(_PRELOAD_START) + r"[\s\S]*?" + re.escape(_PRELOAD_END)
    )
    if marked_re.search(text):
        new_text = marked_re.sub(lambda _m: block, text, count=1)
        return new_text, ("already" if new_text == text else "patched")

    # 2. Upgrade unmarked legacy agAPI block
    legacy_re = re.compile(
        r"const\s+agAPI\s*=\s*\{[\s\S]*?\};\s*electron_1\.contextBridge\.exposeInMainWorld\(\s*['\"]agAPI['\"]\s*,\s*agAPI\s*\);"
    )
    if legacy_re.search(text):
        new_text = legacy_re.sub(lambda _m: block, text, count=1)
        return new_text, ("already" if new_text == text else "patched")

    # 3. Fresh insertion after wslAPI exposure
    anchor_re = re.compile(
        r"(electron_1\.contextBridge\.exposeInMainWorld\(\s*['\"]wsl['\"]\s*,\s*wslAPI\s*\);)"
    )
    m = anchor_re.search(text)
    if not m:
        return text, "not-found"
    new_text = text[: m.end(1)] + eol + block + text[m.end(1) :]
    return new_text, "patched"


def _patch_ipc_handlers_js(text: str, ipc_block: str) -> tuple[str, str]:
    eol = "\r\n" if "\r\n" in text else "\n"
    norm_block = ipc_block.replace("\r\n", "\n").replace("\n", eol)

    wcv_re = re.compile(
        r"[ \t]*\(0,\s*wcvIpcHandlers_1\.registerWcvIpcHandlers\)\([^)]*\);"
    )
    existing_re = re.compile(
        r"[ \t]*" + re.escape(_IPC_START) + r"[\s\S]*?(?=[ \t]*\(0,\s*wcvIpcHandlers_1\.registerWcvIpcHandlers\)\([^)]*\);)"
    )

    if _IPC_START in text and wcv_re.search(text):
        stripped = existing_re.sub("", text, count=1)
        m = wcv_re.search(stripped)
        if not m:
            return text, "not-found"
        new_text = stripped[: m.start()] + norm_block + eol + eol + stripped[m.start() :]
        return new_text, ("already" if new_text == text else "patched")

    m = wcv_re.search(text)
    if not m:
        return text, "not-found"
    new_text = text[: m.start()] + norm_block + eol + eol + text[m.start() :]
    return new_text, "patched"


def _check_asar_needles(asar_path: str, checks: tuple[tuple[str, tuple[str, ...]], ...]) -> bool:
    if not asar_path or not os.path.isfile(asar_path):
        return False
    try:
        with open(asar_path, "rb") as f:
            hdr, base = _read_asar_header(f)
            for rel_path, needles in checks:
                node = _get_node(hdr, rel_path)
                if not node or "offset" not in node:
                    return False
                f.seek(base + int(node["offset"]))
                content = f.read(int(node["size"])).decode("utf-8", errors="ignore")
                for needle in needles:
                    if needle not in content:
                        return False
        return True
    except Exception:
        return False


def _has_any_asar_ui_markers(asar_path: str) -> bool:
    """Return True if app.asar already carries any version of AG Toolkit UI injection."""
    return _check_asar_needles(
        asar_path,
        (
            ("dist/utils.js", ("ag_desktop_ui.js",)),
            ("dist/preload.js", ("'agAPI'",)),
            ("dist/ipcHandlers.js", ("'ag:get-state'",)),
        ),
    )


def _is_asar_file_patched(asar_path: str) -> bool:
    """Return True only when all current AG Toolkit bridge hooks are present in app.asar."""
    base_ok = _check_asar_needles(
        asar_path,
        (
            ("dist/utils.js", (_UTILS_START, "ag_desktop_ui.js")),
            ("dist/preload.js", (_PRELOAD_START, "invoke('ag:refresh', mode)", "invoke('ag:swarm', op, payload)", "invoke('ag:guardian', op, payload)", "ag:state-updated", "ag:guardian-alert")),
            (
                "dist/ipcHandlers.js",
                (
                    _IPC_START,
                    "quota_engine.js",
                    "'ag:get-state'",
                    "'ag:swarm'",
                    "'ag:guardian'",
                    "closeAllConnections",
                    "_syncStandaloneOnboarding",
                    "jetski.onboarding.lastLoginUsername",
                    "res.restarting === true",
                    "resetRestartCount",
                    "sync_accounts",
                    "add_account",
                ),
            ),
        ),
    )
    if not base_ok:
        return False
    try:
        with open(asar_path, "rb") as f:
            hdr, base = _read_asar_header(f)
            utils_node = _get_node(hdr, "dist/utils.js")
            if utils_node and "offset" in utils_node:
                f.seek(base + int(utils_node["offset"]))
                utils_txt = f.read(int(utils_node["size"])).decode("utf-8", errors="ignore")
                if "hubContents.on('did-finish-load'" in utils_txt and "void win.loadURL(" in utils_txt:
                    if _UTILS_FAIL_START not in utils_txt or "'did-fail-load'" not in utils_txt:
                        return False
            ls_node = _get_node(hdr, "dist/languageServer.js")
            if ls_node and "offset" in ls_node:
                f.seek(base + int(ls_node["offset"]))
                ls_txt = f.read(int(ls_node["size"])).decode("utf-8", errors="ignore")
                for needle in (_LS_START, _LS_PORT_RESET_MARKER, "resetRestartCount"):
                    if needle not in ls_txt:
                        return False
                if "loginShellEnv" in ls_txt and (_LS_ENV_START not in ls_txt or "await _agProbePort" in ls_txt):
                    return False
                if "shellEnvReads.take()" in ls_txt and (_LS_PRE_START not in ls_txt or "_agProbeHost" not in ls_txt):
                    return False
        return True
    except Exception:
        return False


def is_asar_ui_patched(manager_path: str) -> bool:
    asar_path = find_asar_relative_to_manager(manager_path)
    return _is_asar_file_patched(asar_path)


def _ensure_asar_backup(asar_path: str) -> None:
    bak_path = asar_path + ASAR_BAK_EXT
    has_injection = _has_any_asar_ui_markers(asar_path)

    if os.path.exists(bak_path):
        if has_injection:
            return
        if filecmp.cmp(asar_path, bak_path, shallow=False):
            return
        info(f"ASAR backup is stale (app updated) — refreshing {os.path.basename(bak_path)}")
    elif has_injection:
        return
    else:
        info(f"Creating ASAR backup -> {os.path.basename(bak_path)}")

    if sys.platform == "darwin":
        ensure_macos_writable(asar_path)
    shutil.copy2(asar_path, bak_path)
    fix_posix_permissions(bak_path)


def patch_desktop_asar(manager_path: str, kill_on_lock: bool = True) -> bool:
    """Inject or upgrade Antigravity 2.0 Desktop UI hooks in resources/app.asar."""
    asar_path = find_asar_relative_to_manager(manager_path)
    if not asar_path or not os.path.isfile(asar_path):
        warn("Antigravity 2.0 app.asar not found — skipping Desktop UI injection.")
        return False

    ipc_block = _load_ipc_handler_block()
    if not ipc_block:
        warn("Desktop IPC handler template not found — skipping app.asar UI injection.")
        return False

    try:
        with open(asar_path, "rb") as f:
            hdr, base = _read_asar_header(f)
            targets = {}
            for rel in ("dist/utils.js", "dist/preload.js", "dist/ipcHandlers.js"):
                node = _get_node(hdr, rel)
                if not node or "offset" not in node:
                    warn(f"ASAR entry not found ({rel}) — skipping Desktop UI injection.")
                    return False
                f.seek(base + int(node["offset"]))
                targets[rel] = f.read(int(node["size"])).decode("utf-8")
            ls_node = _get_node(hdr, "dist/languageServer.js")
            if ls_node and "offset" in ls_node:
                f.seek(base + int(ls_node["offset"]))
                targets["dist/languageServer.js"] = f.read(int(ls_node["size"])).decode("utf-8")
    except Exception as e:
        warn(f"Could not read app.asar for Desktop UI injection: {e}")
        return False

    new_utils, s_utils = _patch_utils_js(targets["dist/utils.js"])
    new_preload, s_preload = _patch_preload_js(targets["dist/preload.js"])
    new_ipc, s_ipc = _patch_ipc_handlers_js(targets["dist/ipcHandlers.js"], ipc_block)
    new_ls = None
    s_ls = "skipped"
    if "dist/languageServer.js" in targets:
        new_ls, s_ls = _patch_language_server_js(targets["dist/languageServer.js"])

    if "not-found" in (s_utils, s_preload, s_ipc, s_ls):
        warn(
            f"Desktop UI anchors incomplete in app.asar "
            f"(utils={s_utils}, preload={s_preload}, ipc={s_ipc}, ls={s_ls}) — skipping."
        )
        return False

    replacements: dict[str, bytes] = {}
    if new_utils != targets["dist/utils.js"]:
        replacements["dist/utils.js"] = new_utils.encode("utf-8")
    if new_preload != targets["dist/preload.js"]:
        replacements["dist/preload.js"] = new_preload.encode("utf-8")
    if new_ipc != targets["dist/ipcHandlers.js"]:
        replacements["dist/ipcHandlers.js"] = new_ipc.encode("utf-8")
    if new_ls is not None and new_ls != targets["dist/languageServer.js"]:
        replacements["dist/languageServer.js"] = new_ls.encode("utf-8")

    if not replacements:
        step("Patch Desktop UI bridge (app.asar)", True, "already up to date")
        return True

    for attempt in range(2):
        try:
            _ensure_asar_backup(asar_path)
            if sys.platform == "darwin":
                ensure_macos_writable(asar_path)
            _rewrite_asar(asar_path, replacements)
            step("Patch Desktop UI bridge (app.asar)", True, f"{len(replacements)} module(s) updated")
            return True
        except PermissionError as e:
            if attempt == 0 and kill_on_lock:
                warn(f"app.asar is locked ({e}). Closing Antigravity processes and retrying...")
                terminate_standalone_processes()
                import time
                time.sleep(1.2)
                continue
            warn(f"Could not update app.asar (close Antigravity and retry): {e}")
            return False
        except Exception as e:
            warn(f"Desktop UI ASAR injection skipped: {e}")
            return False
    return False


def restore_desktop_asar(manager_path: str) -> bool:
    """Restore resources/app.asar from app.asar.backup when present."""
    asar_path = find_asar_relative_to_manager(manager_path)
    if not asar_path:
        return False
    bak_path = asar_path + ASAR_BAK_EXT
    if not os.path.isfile(bak_path):
        return False

    try:
        if sys.platform == "darwin":
            ensure_macos_writable(asar_path)
        with open(bak_path, "rb") as f:
            bak_data = f.read()
        write_atomic(asar_path, bak_data, kill_on_lock=False, meta_source=bak_path)
        fix_posix_permissions(asar_path)
        ok(f"Restored Desktop ASAR from {os.path.basename(bak_path)}")
        return True
    except PermissionError as e:
        err(f"Could not restore app.asar (file locked — close Antigravity first): {e}")
        return False
    except Exception as e:
        err(f"Could not restore app.asar: {e}")
        return False
