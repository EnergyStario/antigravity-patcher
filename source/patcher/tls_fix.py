"""
patcher/tls_fix.py -- Fix expired TLS cert + VPN proxy for Antigravity IDE.

Problem 1 (TLS cert):
  language_server uses a hardcoded cert for 127.0.0.1 that expired Nov 2026.
  Causes: TLS handshake error from 127.0.0.1 / agent execution terminated after 1s.

Problem 2 (Proxy / EOF):
  Go net/http does NOT read Windows registry for proxy settings.
  When a VPN sets a system proxy in HKCU, language_server ignores it, causing:
  "Singleflight refresh failed ... Post googleapis.com: EOF"

Solution -- inject into main.js (Electron process):
  language_server inherits env vars from parent Electron process, so this single
  injection in main.js fixes both problems:

  1. NODE_TLS_REJECT_UNAUTHORIZED=0  -- skip expired cert check for 127.0.0.1
  2. NO_PROXY=localhost,127.0.0.1,::1  -- prevent proxying local IDE<->server traffic
  3. Auto-read Windows registry proxy -> set HTTP_PROXY / HTTPS_PROXY for Go
"""
from __future__ import annotations
import re
import shutil
from pathlib import Path

from patcher.utils.console import ok, warn, err, info, hint, step

TLS_MARKER  = "NODE_TLS_REJECT_UNAUTHORIZED"
TLS_BAK_EXT = ".js.tlsbak"

# Full injection block (see module docstring for explanation).
# Try/catch makes it safe -- never breaks IDE startup on failure.
TLS_INJECT = (
    '// === ag-patcher: tls-cert-fix + proxy-fix ===\n'
    'process.env.NODE_TLS_REJECT_UNAUTHORIZED = \'0\';\n'
    'process.env.AGY_PROXY_STRICT_SSL = \'false\';\n'
    'process.env.NO_PROXY = \'localhost,127.0.0.1,::1\';\n'
    'process.env.no_proxy = \'localhost,127.0.0.1,::1\';\n'
    'process.env.GODEBUG = process.env.GODEBUG ? (process.env.GODEBUG.includes(\'http2client=\') ? process.env.GODEBUG : process.env.GODEBUG + \',http2client=0\') : \'http2client=0\';\n'
    'try {\n'
    '  const _origWarn = process.emitWarning;\n'
    '  process.emitWarning = function(w, ...args) {\n'
    '    if (typeof w === \'string\' && w.includes(\'NODE_TLS_REJECT_UNAUTHORIZED\')) return;\n'
    '    if (w && w.message && w.message.includes(\'NODE_TLS_REJECT_UNAUTHORIZED\')) return;\n'
    '    return _origWarn.call(process, w, ...args);\n'
    '  };\n'
    '} catch (e) {}\n'
    'try {\n'
    '  const _setAgProxy = (_pu) => {\n'
    '    if (!_pu) return;\n'
    '    if (!_pu.includes(\'://\')) _pu = \'http://\' + _pu;\n'
    '    process.env.HTTP_PROXY = _pu; process.env.HTTPS_PROXY = _pu; process.env.ALL_PROXY = _pu;\n'
    '    process.env.http_proxy = _pu; process.env.https_proxy = _pu; process.env.all_proxy = _pu;\n'
    '  };\n'
    '  const _agProbeUrl = (_pu, cb) => {\n'
    '    try {\n'
    '      const _full = _pu.includes(\'://\') ? _pu : (\'http://\' + _pu);\n'
    '      const _u = new URL(_full);\n'
    '      const _hp = parseInt(_u.port, 10);\n'
    '      if (!_u.hostname || !_hp) return cb(true, _full);\n'
    '      const _net = require(\'net\');\n'
    '      const _s = new _net.Socket();\n'
    '      let _done = false;\n'
    '      const _fin = (_ok) => { if (_done) return; _done = true; try { _s.destroy(); } catch (_) {} cb(_ok, _full); };\n'
    '      _s.setTimeout(180);\n'
    '      _s.once(\'connect\', () => _fin(true));\n'
    '      _s.once(\'timeout\', () => _fin(false));\n'
    '      _s.once(\'error\', () => _fin(false));\n'
    '      _s.connect(_hp, _u.hostname);\n'
    '    } catch (_) { cb(false, _pu); }\n'
    '  };\n'
    '  const _agProbeLocalVpn = () => {\n'
    '    const _net = require(\'net\');\n'
    '    [10808, 10809, 2080, 7890, 2081, 7891, 8889, 12334].forEach((_port) => {\n'
    '      const _s = new _net.Socket();\n'
    '      _s.setTimeout(150);\n'
    '      _s.once(\'connect\', () => {\n'
    '        try { _s.destroy(); } catch (_) {}\n'
    '        if (!process.env.HTTP_PROXY && !process.env.HTTPS_PROXY) _setAgProxy(\'http://127.0.0.1:\' + _port);\n'
    '      });\n'
    '      _s.once(\'timeout\', () => { try { _s.destroy(); } catch (_) {} });\n'
    '      _s.once(\'error\', () => { try { _s.destroy(); } catch (_) {} });\n'
    '      try { _s.connect(_port, \'127.0.0.1\'); } catch (_) {}\n'
    '    });\n'
    '  };\n'
    '  let _foundProxy = String(process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.ALL_PROXY || \'\').trim();\n'
    '  let _foundSrc = \'env\';\n'
    '  if (!_foundProxy && process.platform === \'win32\') {\n'
    '    const { execFileSync } = require(\'child_process\');\n'
    '    const _rk = \'HKCU\\\\Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Internet Settings\';\n'
    '    const _ro = execFileSync(\'reg\', [\'query\', _rk], { encoding: \'utf8\', timeout: 1200, windowsHide: true });\n'
    '    if (/ProxyEnable\\s+REG_DWORD\\s+(?:0x1|1)\\b/i.test(_ro)) {\n'
    '      const _m = _ro.match(/ProxyServer\\s+REG_(?:EXPAND_)?SZ\\s+([^\\r\\n]+)/i);\n'
    '      if (_m && _m[1]) {\n'
    '        let _pu = _m[1].trim();\n'
    '        if (_pu.includes(\'=\')) { const _hm = _pu.match(/https?=([^;]+)/i); if (_hm) _pu = _hm[1]; }\n'
    '        if (_pu) { _foundProxy = _pu; _foundSrc = \'reg\'; }\n'
    '      }\n'
    '    }\n'
    '  }\n'
    '  const _fs = require(\'fs\'), _path = require(\'path\'), _os = require(\'os\');\n'
    '  const _cfgFile = _path.join(_os.homedir(), \'.antigravity_tools\', \'proxy_config.json\');\n'
    '  let _cfgSrc = \'\';\n'
    '  if (!_foundProxy && _fs.existsSync(_cfgFile)) {\n'
    '    try {\n'
    '      const _cfg = JSON.parse(_fs.readFileSync(_cfgFile, \'utf8\'));\n'
    '      const _pu = String((_cfg && (_cfg.proxy_url || _cfg.url)) || \'\').trim();\n'
    '      _cfgSrc = String((_cfg && _cfg.source) || \'\').trim();\n'
    '      if (_pu) { _foundProxy = _pu; _foundSrc = \'cfg\'; }\n'
    '    } catch (_) {}\n'
    '  }\n'
    '  if (_foundProxy) {\n'
    '    if (_foundSrc === \'env\') {\n'
    '      _setAgProxy(_foundProxy);\n'
    '    } else {\n'
    '      _agProbeUrl(_foundProxy, (_ok, _full) => {\n'
    '        if (_ok) {\n'
    '          _setAgProxy(_full);\n'
    '        } else {\n'
    '          if (_foundSrc === \'cfg\' && _cfgSrc.startsWith(\'Local VPN\')) { try { _fs.unlinkSync(_cfgFile); } catch (_) {} }\n'
    '          _agProbeLocalVpn();\n'
    '        }\n'
    '      });\n'
    '    }\n'
    '  } else {\n'
    '    _agProbeLocalVpn();\n'
    '  }\n'
    '} catch (e) {}\n'
    '// === end ag-patcher ===\n'
)


def is_tls_fix_applied(main_js_path: str) -> bool:
    """Return True if TLS/proxy fix is already present in main.js."""
    try:
        txt = Path(main_js_path).read_text(encoding="utf-8", errors="replace")
        return TLS_MARKER in txt and "http2client=0" in txt and "_agProbeUrl" in txt
    except Exception:
        return False


def do_tls_fix_inject(main_js_path: str) -> bool:
    """Inject TLS certificate fix + VPN proxy fix into the top of main.js."""
    info("Applying TLS certificate fix + VPN proxy fix...")
    print()

    if not main_js_path or not Path(main_js_path).is_file():
        err(f"main.js not found: {main_js_path}")
        return False

    try:
        content = Path(main_js_path).read_text(encoding="utf-8", errors="replace")
    except Exception as ex:
        err(f"Cannot read main.js: {ex}")
        return False

    if TLS_MARKER in content:
        if "_origWarn" in content and "http2client=0" in content and "_agProbeUrl" in content:
            step("TLS/proxy fix", None, "already applied")
            warn("TLS/proxy fix is already present. No changes made.")
            return True
        # Upgrade old TLS fix to new warning-suppressed TLS fix
        content = re.sub(
            r"// === ag-patcher: tls-cert-fix.*?// === end ag-patcher ===\n",
            "", content, flags=re.DOTALL
        )

    bak = Path(main_js_path + TLS_BAK_EXT)
    if not bak.exists():
        try:
            shutil.copy2(main_js_path, bak)
            step(f"Backup: {bak.name}", True)
        except Exception as ex:
            warn(f"Could not create backup: {ex}")

    try:
        from patcher.utils.atomic import write_atomic
        write_atomic(main_js_path, TLS_INJECT + content, kill_on_lock=False)
    except PermissionError:
        err("Permission denied writing main.js - run as Administrator.")
        return False
    except Exception as ex:
        err(f"Failed to write main.js: {ex}")
        return False

    step("NODE_TLS_REJECT_UNAUTHORIZED=0 injected", True)
    step("NO_PROXY=localhost,127.0.0.1,::1 set", True)
    step("Windows registry proxy auto-detect injected", True)

    try:
        from patcher.ide.patcher import fix_product_checksums
        fix_product_checksums(main_js_path)
    except Exception:
        pass

    print()
    ok("TLS certificate fix + VPN proxy fix applied.")
    hint("language_server now inherits HTTP_PROXY/HTTPS_PROXY from Windows registry.")
    hint("Restart Antigravity IDE for changes to take effect.")
    return True


def do_tls_fix_restore(main_js_path: str) -> bool:
    """Remove TLS/proxy fix from main.js (restore from .tlsbak or strip injection)."""
    info("Removing TLS/proxy fix from main.js...")
    print()

    bak = Path(main_js_path + TLS_BAK_EXT)
    if bak.is_file():
        try:
            shutil.copy2(bak, main_js_path)
            bak.unlink()
            step("Restored from .tlsbak backup", True)
            print()
            ok("TLS/proxy fix removed.")
            return True
        except Exception as ex:
            err(f"Could not restore from backup: {ex}")

    if Path(main_js_path).is_file():
        try:
            content = Path(main_js_path).read_text(encoding="utf-8", errors="replace")
            if TLS_MARKER not in content:
                step("TLS fix not found in main.js", None, "already clean")
                return True
            # Try removing the whole marker block
            new = re.sub(
                r"// === ag-patcher: tls-cert-fix.*?// === end ag-patcher ===\n",
                "", content, flags=re.DOTALL,
            )
            # Fallback: remove just lines containing the marker
            if TLS_MARKER in new:
                new = "\n".join(line for line in new.splitlines()
                                if TLS_MARKER not in line) + "\n"
            from patcher.utils.atomic import write_atomic
            write_atomic(main_js_path, new, kill_on_lock=False)
            step("TLS/proxy inject block removed", True)

            try:
                from patcher.ide.patcher import fix_product_checksums
                fix_product_checksums(main_js_path)
            except Exception:
                pass

            print()
            ok("TLS/proxy fix removed.")
            return True
        except Exception as ex:
            err(f"Could not strip TLS inject: {ex}")

    err("Could not remove TLS/proxy fix.")
    return False
