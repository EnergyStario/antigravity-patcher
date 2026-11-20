/**
 * error_guardian.js -- Shared In-App Runtime Error Detector & Scoped Self-Healer
 * Shared by Antigravity IDE (Extension Host) and Antigravity 2.0 (Electron Main).
 * Keeps Auto-Mode ON out of the box (auto_heal_config.json -> enabled: true)
 * and runs scoped `--autopilot-heal --target <ide|manager>` without touching
 * healthy sibling apps or forcing the user to open the console patcher.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFile } = require('child_process');

function getAgToolsDir() {
    return process.env.AG_TOOLS_DIR || path.join(os.homedir(), '.antigravity_tools');
}

function getAutoHealConfigFile() {
    return path.join(getAgToolsDir(), 'auto_heal_config.json');
}

function loadAutoHealConfig() {
    const defaults = { enabled: true, auto_relaunch_broken_app: true };
    const cfgFile = getAutoHealConfigFile();
    try {
        if (fs.existsSync(cfgFile)) {
            const parsed = JSON.parse(fs.readFileSync(cfgFile, 'utf8'));
            if (parsed && typeof parsed === 'object') {
                return Object.assign({}, defaults, parsed);
            }
        }
    } catch (_) {}
    return defaults;
}

function saveAutoHealConfig(patch) {
    const next = Object.assign({}, loadAutoHealConfig(), patch && typeof patch === 'object' ? patch : {});
    const cfgFile = getAutoHealConfigFile();
    const dir = path.dirname(cfgFile);
    try {
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir, { recursive: true });
        }
        const tmpFile = path.join(dir, `.auto_heal_config.${process.pid}.tmp`);
        fs.writeFileSync(tmpFile, JSON.stringify(next, null, 2), 'utf8');
        fs.renameSync(tmpFile, cfgFile);
    } catch (_) {
        try {
            fs.writeFileSync(cfgFile, JSON.stringify(next, null, 2), 'utf8');
        } catch (_err2) {}
    }
    return next;
}

const ERROR_RULES = [
    {
        key: 'user_location_400',
        title: 'Региональная блокировка API (HTTP 400)',
        description: 'Google API отклоняет регион — нужен патч обхода + NRPT DNS / Hosts-Pinning.',
        patterns: [
            /User location is not supported/i,
            /FAILED_PRECONDITION.*\(code 400\)/i,
            /Account ineligible/i,
            /not currently available in your location/i,
            /SET_INELIGIBLE/i,
            /Eligibility check failed/i,
        ],
    },
    {
        key: 'dead_proxy',
        title: 'Недоступен локальный прокси VPN',
        description: 'Остался мёртвый порт прокси от VPN — соединение с Google отклоняется.',
        patterns: [
            /proxyconnect\s+tcp:/i,
            /ERR_PROXY_CONNECTION_FAILED/i,
            /ECONNREFUSED\s+127\.0\.0\.1:\d+/i,
            /dial tcp 127\.0\.0\.1:\d+:.*actively refused/i,
        ],
    },
    {
        key: 'tls_expired',
        title: 'Сбой TLS / защищённого соединения',
        description: 'Разорван TLS handshake до Google API или локального сервера.',
        patterns: [
            /TLS handshake (?:timeout|error)/i,
            /remote error:\s*tls:/i,
            /CERT_HAS_EXPIRED/i,
            /UNABLE_TO_VERIFY_LEAF_SIGNATURE/i,
            /unable to get local issuer/i,
            /SELF_SIGNED_CERT/i,
            /x509:/i,
            /certificate has expired/i,
        ],
    },
    {
        key: 'dns_lookup_fail',
        title: 'Сбой DNS для Google API',
        description: 'Системный DNS не разрешает домен Google API (no such host / ENOTFOUND).',
        patterns: [
            /dial tcp: lookup .+no such host/i,
            /lookup [^:\s]+googleapis\.com[^:]*:\s*no such host/i,
            /getaddrinfo\s+(?:ENOTFOUND|EAI_AGAIN)/i,
            /ERR_NAME_NOT_RESOLVED/i,
        ],
    },
    {
        key: 'auth_failure',
        title: 'Сбой авторизации / инициализации',
        description: 'Ошибка настройки аккаунта или запуска фонового сервиса модели.',
        patterns: [
            /unexpected issue setting up your account/i,
            /Singleflight refresh failed/i,
            /Language server has not been initialized/i,
        ],
    },
    {
        key: 'sqlite_corrupt',
        title: 'Повреждена локальная база сессий',
        description: 'Ошибка чтения базы state.vscdb — требуется восстановление индекса.',
        patterns: [
            /SQLITE_CORRUPT/i,
            /database disk image is malformed/i,
            /Corrupt sqlite/i,
        ],
    },
];

function classifyErrorText(text) {
    if (!text || typeof text !== 'string') return null;
    const lines = text.split(/\r?\n/);
    for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) continue;
        if (/"postmessage-rpc"/i.test(line)) continue;
        if (/received prompt:|prompt=|\"prompt\":|\"messages\":|request body:/i.test(line)) continue;
        for (const rule of ERROR_RULES) {
            if (rule.patterns.some((pat) => pat.test(line))) {
                return {
                    key: rule.key,
                    title: rule.title,
                    description: rule.description,
                    matchedLine: line.slice(-220),
                };
            }
        }
    }
    return null;
}

function resolvePatcherRunner() {
    const toolsDir = getAgToolsDir();
    // 1. Registered runner from ~/.antigravity_tools/auto_repatch.json
    try {
        const repatchCfg = path.join(toolsDir, 'auto_repatch.json');
        if (fs.existsSync(repatchCfg)) {
            const parsed = JSON.parse(fs.readFileSync(repatchCfg, 'utf8'));
            if (parsed && Array.isArray(parsed.runner) && parsed.runner.length > 0) {
                const exe = String(parsed.runner[0] || '').trim();
                if (exe && fs.existsSync(exe)) {
                    return parsed.runner.map((x) => String(x));
                }
            }
        }
    } catch (_) {}

    // 2. Standard local appdata binary
    const localApp = process.env.LOCALAPPDATA || '';
    if (localApp) {
        const localExe = path.join(localApp, 'AntigravityPatcher', 'Antigravity_Patcher_Windows_x64.exe');
        if (fs.existsSync(localExe)) return [localExe];
    }

    // 3. Repo / packaged relative binary or Python entrypoint
    const devRoots = [
        path.resolve(__dirname, '..', '..'),
        path.resolve(__dirname, '..'),
    ];
    for (const root of devRoots) {
        const exeCand = path.join(root, 'Antigravity_Patcher_Windows_x64.exe');
        if (fs.existsSync(exeCand)) return [exeCand];
        const distExe = path.join(root, 'dist', 'Antigravity_Patcher_Windows_x64.exe');
        if (fs.existsSync(distExe)) return [distExe];
        const mainPy = path.join(root, 'source', 'main.py');
        if (fs.existsSync(mainPy)) {
            return ['python', mainPy];
        }
    }
    return null;
}

function runScopedHeal(opts = {}) {
    const target = ['ide', 'manager', 'cli', 'all'].includes(opts.target) ? opts.target : 'all';
    const force = opts.force !== false;
    const allowRestart = opts.allowRestart !== false;
    const timeoutMs = Number(opts.timeoutMs || 45000);
    const runner = Array.isArray(opts.runnerOverride) ? opts.runnerOverride : resolvePatcherRunner();
    if (!runner || runner.length === 0) {
        return Promise.resolve({
            ok: false,
            missingRunner: true,
            error: 'Исполняемый файл Antigravity Patcher не найден',
        });
    }

    const cmd = runner[0];
    const args = runner.slice(1).concat(['--autopilot-heal', '--target', target]);
    if (force) args.push('--force');
    if (!allowRestart) args.push('--no-restart');

    return new Promise((resolve) => {
        try {
            execFile(
                cmd,
                args,
                {
                    windowsHide: true,
                    timeout: timeoutMs,
                    maxBuffer: 1024 * 1024,
                    env: Object.assign({}, process.env, {
                        AG_AUTO_REPATCH_SILENT: '1',
                        SKIP_ELEVATION: '1',
                        PYTHONIOENCODING: 'utf-8',
                    }),
                },
                (err, stdout, stderr) => {
                    const outText = String(stdout || '').trim();
                    const lines = outText ? outText.split(/\r?\n/) : [];
                    for (let i = lines.length - 1; i >= 0; i--) {
                        const raw = lines[i].trim();
                        if (!raw.startsWith('{')) continue;
                        try {
                            const parsed = JSON.parse(raw);
                            if (parsed && typeof parsed === 'object') {
                                resolve(parsed);
                                return;
                            }
                        } catch (_) {}
                    }
                    if (err) {
                        resolve({
                            ok: false,
                            error: String((stderr && String(stderr).trim()) || err.message || err),
                        });
                    } else {
                        resolve({ ok: true, actions: [] });
                    }
                }
            );
        } catch (ex) {
            resolve({ ok: false, error: String((ex && ex.message) || ex) });
        }
    });
}

function collectRecentLogFiles(baseDirs, maxFiles = 6, maxAgeMs = 15 * 60 * 1000) {
    const found = [];
    const now = Date.now();
    const walk = (dir, depth) => {
        if (depth > 4) return;
        let entries;
        try {
            entries = fs.readdirSync(dir, { withFileTypes: true });
        } catch (_) {
            return;
        }
        const subDirs = [];
        for (const ent of entries) {
            const full = path.join(dir, ent.name);
            try {
                if (ent.isFile() && /\.log$/i.test(ent.name)) {
                    const st = fs.statSync(full);
                    if ((now - st.mtimeMs) <= maxAgeMs) {
                        found.push({ path: full, mtimeMs: st.mtimeMs, size: st.size });
                    }
                } else if (ent.isDirectory() && depth < 3) {
                    const st = fs.statSync(full);
                    if ((now - st.mtimeMs) <= maxAgeMs) {
                        subDirs.push({ path: full, mtimeMs: st.mtimeMs });
                    }
                }
            } catch (_) {}
        }
        subDirs.sort((a, b) => b.mtimeMs - a.mtimeMs);
        for (const sd of subDirs.slice(0, 2)) {
            walk(sd.path, depth + 1);
        }
    };
    for (const d of (baseDirs || [])) {
        if (d && fs.existsSync(d)) walk(d, 0);
    }
    found.sort((a, b) => b.mtimeMs - a.mtimeMs);
    return found.slice(0, maxFiles);
}

function createLogErrorMonitor(opts = {}) {
    const logDirs = Array.isArray(opts.logDirs) ? opts.logDirs : [];
    const onError = typeof opts.onError === 'function' ? opts.onError : () => {};
    const cooldownMs = Number(opts.cooldownMs || 120000);
    const offsets = new Map();
    const lastAlertAt = new Map();

    // Seed initial offsets: scan only last 16KB of files active in the last 90s on startup
    const startupInit = () => {
        const now = Date.now();
        const files = collectRecentLogFiles(logDirs, 6, 15 * 60 * 1000);
        for (const f of files) {
            if ((now - f.mtimeMs) <= 90000 && f.size > 0) {
                offsets.set(f.path, Math.max(0, f.size - 16384));
            } else {
                offsets.set(f.path, f.size);
            }
        }
    };
    startupInit();

    const scanNow = () => {
        const now = Date.now();
        const files = collectRecentLogFiles(logDirs, 6, 15 * 60 * 1000);
        for (const f of files) {
            let prevOffset = offsets.get(f.path);
            if (prevOffset === undefined) {
                prevOffset = Math.max(0, f.size - 16384);
            }
            if (f.size <= prevOffset) {
                offsets.set(f.path, f.size);
                continue;
            }
            try {
                const toRead = Math.min(f.size - prevOffset, 64 * 1024);
                const fd = fs.openSync(f.path, 'r');
                const buf = Buffer.allocUnsafe(toRead);
                fs.readSync(fd, buf, 0, toRead, prevOffset);
                fs.closeSync(fd);
                offsets.set(f.path, prevOffset + toRead);
                const chunk = buf.toString('utf8', 0, toRead);
                const hit = classifyErrorText(chunk);
                if (hit) {
                    const lastTs = lastAlertAt.get(hit.key) || 0;
                    if ((now - lastTs) >= cooldownMs) {
                        lastAlertAt.set(hit.key, now);
                        onError(Object.assign({}, hit, { sourceFile: path.basename(f.path), detectedAt: now }));
                    }
                }
            } catch (_) {}
        }
        return null;
    };

    return {
        scanNow,
        resetCooldowns: () => lastAlertAt.clear(),
    };
}

module.exports = {
    loadAutoHealConfig,
    saveAutoHealConfig,
    classifyErrorText,
    resolvePatcherRunner,
    runScopedHeal,
    collectRecentLogFiles,
    createLogErrorMonitor,
};
