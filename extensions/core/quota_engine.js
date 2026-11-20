/**
 * quota_engine.js -- Unified pure-Node.js Quota, Auth & Network Engine
 * Shared by Antigravity IDE (VS Code Extension Host) and Antigravity 2.0 (Electron Main).
 * Uses persistent HTTP Keep-Alive connections and mtime-cached fs reads (zero Python spawns).
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const http = require('http');
const https = require('https');
const url = require('url');
const { execFile } = require('child_process');

const HOME_DIR = os.homedir();
const AG_TOOLS_DIR = process.env.AG_TOOLS_DIR || path.join(HOME_DIR, '.antigravity_tools');

// ---------------------------------------------------------------------------
// Model configuration system
//
// Built-in defaults cover all known Antigravity IDE models.
// User config at ~/.antigravity_tools/model_config.json ALWAYS wins over defaults.
//
// model_config.json schema:
// {
//   "custom_models": {           // models not in defaults (e.g. via Delegator)
//     "my-delegator-llm": {
//       "tier": "heavy",          // "heavy" | "fast" | "unknown"
//       "display_name": "My LLM via Delegator",
//       "role_preference": "orchestrator"  // "orchestrator" | "worker" | "any"
//     }
//   },
//   "tier_overrides": {          // change tier of any existing model
//     "gemini-pro": "fast"
//   },
//   "orchestrator_order": [      // preferred orchestrator model priority (index 0 = first tried)
//     "my-delegator-llm",
//     "claude-sonnet-4",
//     "claude-opus-4"
//   ],
//   "worker_model": "flash"      // preferred model for workers/verifier
// }
// ---------------------------------------------------------------------------

/** Built-in default tier patterns (substring match, case-insensitive) */
const MODEL_TIERS_DEFAULT = {
    tier1_heavy: [
        // Claude family (Anthropic via Google Cloud)
        'claude-sonnet-4', 'claude-sonnet', 'claude',
        'claude-opus-4', 'claude-opus',
        // GPT via Google Cloud
        'gpt-oss-120b', 'gpt-oss', 'gpt',
        // Gemini heavy (Carbon/Argon — Unavailable now, included as future-proof)
        'gemini-4-carbon', 'gemini-carbon', 'carbon',
        'gemini-4-argon', 'gemini-argon', 'argon',
        // Gemini Pro
        'gemini-2.5-pro', 'gemini-pro'
    ],
    tier2_fast: [
        // Flash family — virtually unlimited quota
        'gemini-3.8-flash', 'gemini-3-flash', 'gemini-flash',
        'flash-medium', 'flash-lite', 'flash',
        'gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-2.5-flash'
    ]
};

/** Default orchestrator preference order */
const DEFAULT_ORCHESTRATOR_ORDER = [
    'claude-sonnet-4', 'claude-opus-4', 'gpt-oss-120b',
    'claude-sonnet', 'claude-opus', 'claude',
    'gemini-2.5-pro', 'argon', 'carbon'
];

/** Default worker/verifier model (fast tier preferred) */
const DEFAULT_WORKER_MODEL = 'flash';

// MODEL_CONFIG_FILE is resolved dynamically so AG_TOOLS_DIR env override works in tests.
// Do NOT cache as a module-level constant.
function getModelConfigFile() {
    const agDir = process.env.AG_TOOLS_DIR || path.join(HOME_DIR, '.antigravity_tools');
    return path.join(agDir, 'model_config.json');
}

function getAssistantSettingsFile() {
    const agDir = process.env.AG_TOOLS_DIR || path.join(HOME_DIR, '.antigravity_tools');
    return path.join(agDir, 'assistant_settings.json');
}

function loadAssistantSettings() {
    const file = getAssistantSettingsFile();
    try {
        const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
        return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_) {
        return {};
    }
}

async function saveAssistantSettings(patch) {
    const agDir = process.env.AG_TOOLS_DIR || path.join(HOME_DIR, '.antigravity_tools');
    const file = getAssistantSettingsFile();
    if (!fs.existsSync(agDir)) {
        fs.mkdirSync(agDir, { recursive: true });
    }
    const merged = Object.assign({}, loadAssistantSettings(), patch || {});
    await atomicWriteJson(file, merged);
    return merged;
}

let _modelConfigCache = null;
let _modelConfigMtimeMs = 0;
let _modelConfigFilePath = '';  // tracks which file was last cached

/**
 * Load model_config.json with mtime caching (zero I/O if file unchanged).
 * Returns the parsed config or {} if file doesn't exist or is malformed.
 */
function loadModelConfig() {
    const cfgFile = getModelConfigFile();
    try {
        const st = fs.statSync(cfgFile);
        if (_modelConfigCache && cfgFile === _modelConfigFilePath && st.mtimeMs === _modelConfigMtimeMs) {
            return _modelConfigCache;
        }
        const parsed = JSON.parse(fs.readFileSync(cfgFile, 'utf8'));
        _modelConfigCache = parsed && typeof parsed === 'object' ? parsed : {};
        _modelConfigMtimeMs = st.mtimeMs;
        _modelConfigFilePath = cfgFile;
        return _modelConfigCache;
    } catch (_) {
        _modelConfigCache = {};
        _modelConfigFilePath = cfgFile;
        return {};
    }
}

/**
 * Save model_config.json atomically.
 */
async function saveModelConfig(config) {
    const agDir = process.env.AG_TOOLS_DIR || path.join(HOME_DIR, '.antigravity_tools');
    const cfgFile = getModelConfigFile();
    if (!fs.existsSync(agDir)) {
        fs.mkdirSync(agDir, { recursive: true });
    }
    const result = await atomicWriteJson(cfgFile, config);
    if (result) {
        _modelConfigCache = config;
        try { _modelConfigMtimeMs = fs.statSync(cfgFile).mtimeMs; } catch (_) {}
        _modelConfigFilePath = cfgFile;
    }
    return result;
}

/**
 * Determine the quota tier of a model, with user config taking priority over defaults.
 *
 * Priority order:
 *   1. tier_overrides in model_config.json (exact key match first, then substring)
 *   2. custom_models in model_config.json (exact key match first, then substring)
 *   3. Built-in MODEL_TIERS_DEFAULT (substring match)
 *
 * @param {string} modelId -- e.g. "claude-sonnet", "my-delegator-model", "flash"
 * @returns {"heavy"|"fast"|"unknown"}
 */
function getModelTier(modelId) {
    if (!modelId) return 'unknown';
    const low = String(modelId).toLowerCase();
    const cfg = loadModelConfig();

    // 1. Check user tier_overrides (exact match first, then substring)
    const overrides = (cfg.tier_overrides && typeof cfg.tier_overrides === 'object')
        ? cfg.tier_overrides : {};
    if (overrides[low]) {
        const ov = String(overrides[low]).toLowerCase();
        if (ov === 'heavy' || ov === 'fast') return ov;
    }
    for (const [pattern, tier] of Object.entries(overrides)) {
        if (low.includes(pattern.toLowerCase())) {
            const ov = String(tier).toLowerCase();
            if (ov === 'heavy' || ov === 'fast') return ov;
        }
    }

    // 2. Check user custom_models
    const customModels = (cfg.custom_models && typeof cfg.custom_models === 'object')
        ? cfg.custom_models : {};
    if (customModels[low] && customModels[low].tier) {
        const t = String(customModels[low].tier).toLowerCase();
        if (t === 'heavy' || t === 'fast') return t;
    }
    for (const [pattern, def] of Object.entries(customModels)) {
        if (low.includes(pattern.toLowerCase()) && def && def.tier) {
            const t = String(def.tier).toLowerCase();
            if (t === 'heavy' || t === 'fast') return t;
        }
    }

    // 3. Built-in defaults (substring match)
    for (const pattern of MODEL_TIERS_DEFAULT.tier2_fast) {
        if (low.includes(pattern.toLowerCase())) return 'fast';
    }
    for (const pattern of MODEL_TIERS_DEFAULT.tier1_heavy) {
        if (low.includes(pattern.toLowerCase())) return 'heavy';
    }

    return 'unknown';
}

/**
 * Returns the preferred orchestrator model order for this setup.
 * User config orchestrator_order prepended before defaults.
 */
function getOrchestratorOrder() {
    const cfg = loadModelConfig();
    const userOrder = Array.isArray(cfg.orchestrator_order) ? cfg.orchestrator_order : [];
    // Merge: user models first (deduped), then defaults not already in list
    const seen = new Set(userOrder.map((m) => String(m).toLowerCase()));
    const merged = [...userOrder];
    for (const m of DEFAULT_ORCHESTRATOR_ORDER) {
        if (!seen.has(m.toLowerCase())) {
            merged.push(m);
            seen.add(m.toLowerCase());
        }
    }
    return merged;
}

/**
 * Returns the preferred worker model (user setting wins over default).
 */
function getWorkerModel() {
    const cfg = loadModelConfig();
    return (cfg.worker_model && typeof cfg.worker_model === 'string')
        ? cfg.worker_model
        : DEFAULT_WORKER_MODEL;
}

/**
 * Returns accounts sorted by available quota for a given model tier.
 * Heavy models → sorted by claude_percentage DESC.
 * Fast models  → sorted by gemini_percentage DESC.
 * Excludes the currently-active account (by id).
 *
 * @param {string} modelId
 * @param {string} [currentAccountId]
 * @returns {Array<object>}
 */
function getBestAccountsForModel(modelId, currentAccountId = '') {
    const tier = getModelTier(modelId);
    const state = loadState({ appTarget: 'ide' });
    if (!state.ok || !Array.isArray(state.accounts)) return [];

    const others = state.accounts.filter(
        (a) => a.id && a.id !== currentAccountId
    );

    if (tier === 'fast') {
        return others.slice().sort((a, b) =>
            (Number(b.gemini_percentage) || 0) - (Number(a.gemini_percentage) || 0)
        );
    }
    return others.slice().sort((a, b) =>
        (Number(b.claude_percentage) || 0) - (Number(a.claude_percentage) || 0)
    );
}

/**
 * Determine quota pool ('gemini' vs 'claude') for a given model id and read remaining %.
 * Gemini Pro / Argon / Carbon / Flash consume the Gemini quota pool; Claude / GPT-OSS consume Claude pool.
 */
function getModelPoolName(modelId) {
    const low = String(modelId || '').toLowerCase();
    if (getModelTier(low) === 'fast' || low.includes('gemini') || low.includes('argon') || low.includes('carbon')) {
        return 'gemini';
    }
    return 'claude';
}

function getModelPoolQuota(account, modelId) {
    if (!account || typeof account !== 'object') return 0;
    const pool = getModelPoolName(modelId);
    const raw = pool === 'gemini' ? account.gemini_percentage : account.claude_percentage;
    const num = Number(raw);
    return Number.isFinite(num) ? Math.max(0, Math.min(100, num)) : 0;
}

/**
 * 3-step survival cascade when smart quota runs out (never leave the user without a swarm):
 *   1) 'account_hop': switch to another account that still has quota for the same smart model
 *   2) 'tier1_model': switch to another Tier-1 smart model pool (e.g. Gemini 2.5 Pro / Argon) with remaining quota
 *   3) 'degraded_swarm': fallback to Flash + strict 1-2 file atomic steps & mandatory tests when ALL Tier-1 hit 0%
 */
function resolveQuotaExhaustionPlan({ currentModel = 'claude', currentAccountId = '', minQuota = 1 } = {}) {
    const state = loadState({ appTarget: 'ide' });
    const accounts = (state.ok && Array.isArray(state.accounts)) ? state.accounts.filter((a) => a && a.id) : [];
    const currentPool = getModelPoolName(currentModel);
    const threshold = Math.max(1, Number(minQuota) || 1);

    // 1. Other account with remaining quota in the current model pool
    const sameModelCandidates = getBestAccountsForModel(currentModel, currentAccountId)
        .filter((a) => getModelPoolQuota(a, currentModel) >= threshold);
    if (sameModelCandidates.length > 0) {
        const best = sameModelCandidates[0];
        const remainingPct = getModelPoolQuota(best, currentModel);
        return {
            strategy: 'account_hop',
            degraded: false,
            model: currentModel,
            account: best,
            switchAccount: best.id !== currentAccountId,
            remainingPct,
            reason: `Найден аккаунт ${best.name || best.email || best.id} (${remainingPct}% квоты) для ${currentModel}`
        };
    }

    // 2. Another smart Tier-1 model pool on current account first, then on any other account
    const currentAcc = accounts.find((a) => a.id === currentAccountId) || state.active || null;
    const orderedAccounts = currentAcc
        ? [currentAcc, ...accounts.filter((a) => a.id !== currentAcc.id)]
        : accounts;
    const tier1Candidates = getOrchestratorOrder().filter((m) => {
        if (getModelTier(m) === 'fast') return false;
        return getModelPoolName(m) !== currentPool;
    });
    for (const candidateModel of tier1Candidates) {
        let bestAcc = null;
        let bestPct = 0;
        for (const acc of orderedAccounts) {
            const pct = getModelPoolQuota(acc, candidateModel);
            if (pct >= threshold && pct > bestPct) {
                bestPct = pct;
                bestAcc = acc;
            }
            if (currentAcc && acc.id === currentAcc.id && pct >= threshold) {
                // Prefer staying on current account when another Tier-1 pool has quota there
                bestAcc = acc;
                bestPct = pct;
                break;
            }
        }
        if (bestAcc) {
            return {
                strategy: 'tier1_model',
                degraded: false,
                model: candidateModel,
                account: bestAcc,
                switchAccount: Boolean(bestAcc.id && bestAcc.id !== currentAccountId),
                remainingPct: bestPct,
                reason: `Основной пул исчерпан — доступна другая умная модель Tier-1 (${candidateModel}, ${bestPct}%)`
            };
        }
    }

    // 3. Degraded fallback: ALL Tier-1 smart models exhausted everywhere -> Flash with strict guardrails
    const workerModel = getWorkerModel() || 'gemini-2.5-flash';
    const flashCandidates = orderedAccounts.slice().sort(
        (a, b) => getModelPoolQuota(b, workerModel) - getModelPoolQuota(a, workerModel)
    );
    const flashAcc = flashCandidates.find((a) => getModelPoolQuota(a, workerModel) > 0) || currentAcc || null;
    const flashPct = flashAcc ? getModelPoolQuota(flashAcc, workerModel) : 0;

    return {
        strategy: 'degraded_swarm',
        degraded: true,
        model: workerModel,
        account: flashAcc,
        switchAccount: Boolean(flashAcc && flashAcc.id && currentAccountId && flashAcc.id !== currentAccountId && getModelPoolQuota(currentAcc, workerModel) <= 0),
        remainingPct: flashPct,
        guardrails: [
            'Мелкие брифы: 1-2 файла на одного воркера (строго атомарные правки)',
            'Без масштабных архитектурных перестроек без подтверждения',
            'Обязательный запуск python -m swarm.cli verify --affected после каждого шага и полный прогон тестов в merge'
        ],
        reason: 'Все умные модели (Tier-1) исчерпаны на всех аккаунтах — активирован резервный режим роя на Flash со строгим планом'
    };
}

// Keep MODEL_TIERS exported for backwards compat (tests reference it)
const MODEL_TIERS = MODEL_TIERS_DEFAULT;


const ACCOUNTS_DIR = path.join(AG_TOOLS_DIR, 'accounts');
const ACCOUNTS_INDEX_FILE = path.join(AG_TOOLS_DIR, 'accounts.json');
const OAUTH_CLIENTS_FILE = path.join(AG_TOOLS_DIR, 'oauth_clients.json');
const POLL_STATE_FILE = path.join(AG_TOOLS_DIR, '.quota_poll_state.json');
const AVATARS_DIR = path.join(AG_TOOLS_DIR, 'avatars');
const GEMINI_DIR = process.env.AG_GEMINI_DIR || path.join(HOME_DIR, '.gemini');

const DAILY_COMPANION_HOST = 'https://daily-cloudcode-pa.googleapis.com';
const PROD_COMPANION_HOST = 'https://cloudcode-pa.googleapis.com';
const IDE_CLIENT_VERSION = '2.6.0';
const LOAD_CODE_ASSIST_BODY = JSON.stringify({
    metadata: {
        ideType: 'ANTIGRAVITY',
        ideName: 'antigravity',
        ideVersion: IDE_CLIENT_VERSION
    }
});

function getIdeUserAgent() {
    const goOs = process.platform === 'win32' ? 'windows' : (process.platform === 'darwin' ? 'darwin' : 'linux');
    const archMap = { x64: 'amd64', arm64: 'arm64', ia32: '386' };
    const goArch = archMap[process.arch] || process.arch || 'amd64';
    return `antigravity/${IDE_CLIENT_VERSION} ${goOs}/${goArch}`;
}

const COUNTRY_NAMES = {
    'DE': 'Германия', 'NL': 'Нидерланды', 'US': 'США', 'FI': 'Финляндия',
    'FR': 'Франция', 'GB': 'Великобритания', 'UK': 'Великобритания',
    'SE': 'Швеция', 'PL': 'Польша', 'CH': 'Швейцария', 'AT': 'Австрия',
    'ES': 'Испания', 'IT': 'Италия', 'TR': 'Турция', 'KZ': 'Казахстан',
    'UA': 'Украина', 'RU': 'Россия', 'JP': 'Япония', 'SG': 'Сингапур',
    'CA': 'Канада', 'AE': 'ОАЭ', 'IL': 'Израиль', 'CZ': 'Чехия',
    'RO': 'Румыния', 'BG': 'Болгария', 'MD': 'Молдова', 'GE': 'Грузия',
    'AM': 'Армения', 'AZ': 'Азербайджан', 'UZ': 'Узбекистан', 'LV': 'Латвия',
    'LT': 'Литва', 'EE': 'Эстония', 'NO': 'Норвегия', 'DK': 'Дания',
    'HK': 'Гонконг', 'TW': 'Тайвань', 'KR': 'Южная Корея', 'IN': 'Индия'
};

function createKeepAliveAgent() {
    return new https.Agent({
        keepAlive: true,
        keepAliveMsecs: 30000,
        maxSockets: 16,
        maxFreeSockets: 6,
        rejectUnauthorized: false
    });
}

// Persistent HTTPS agent for low-latency Cloud Companion & OAuth requests
let _keepAliveAgent = createKeepAliveAgent();

function resetConnectionPool() {
    try {
        _keepAliveAgent.destroy();
    } catch (_) {}
    _keepAliveAgent = createKeepAliveAgent();
}

let _proxyCache = { ts: 0, value: null };
let _proxyInFlight = null;
let _netCache = { ts: 0, value: null };
let _netInFlight = null;
let _netSeq = 0;

// Stat cache keyed by file path -> { mtimeMs, size, parsed }
const _fileCache = new Map();
const _avatarCache = new Map();
const _lastCheckedMap = new Map();
let _agyHubCache = { ts: 0, value: false };
let _indexWriteQueue = Promise.resolve();
let _pollStateWriteQueue = Promise.resolve();
let _oauthClientsCache = null;
let _oauthRediscovered = false;

function getSharedPollState() {
    const raw = readJsonCached(POLL_STATE_FILE);
    return (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw : {};
}

function recordSharedPollTimestamps(entries) {
    if (!entries || typeof entries !== 'object') return Promise.resolve();
    const keys = Object.keys(entries);
    if (keys.length === 0) return Promise.resolve();
    _pollStateWriteQueue = _pollStateWriteQueue.then(async () => {
        try {
            const cur = Object.assign({}, getSharedPollState());
            let changed = false;
            const nowSec = Math.floor(Date.now() / 1000);
            // Prune entries older than 7 days to keep file tiny
            for (const k of Object.keys(cur)) {
                if ((nowSec - Number(cur[k] || 0)) > 604800) {
                    delete cur[k];
                    changed = true;
                }
            }
            for (const k of keys) {
                const v = Number(entries[k] || 0);
                if (v > Number(cur[k] || 0)) {
                    cur[k] = v;
                    changed = true;
                }
            }
            if (changed) {
                await atomicWriteJson(POLL_STATE_FILE, cur);
            }
        } catch (_) {}
    });
    return _pollStateWriteQueue;
}

function getCountryName(code) {
    if (!code) return 'Онлайн';
    const upper = String(code).toUpperCase();
    return COUNTRY_NAMES[upper] || upper;
}

function readJsonCached(filePath) {
    try {
        const st = fs.statSync(filePath);
        const cached = _fileCache.get(filePath);
        if (cached && cached.mtimeMs === st.mtimeMs && cached.size === st.size) {
            return cached.parsed;
        }
        const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
        _fileCache.set(filePath, { mtimeMs: st.mtimeMs, size: st.size, parsed });
        return parsed;
    } catch (_) {
        _fileCache.delete(filePath);
        return null;
    }
}

function sleepMs(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function atomicWriteJson(filePath, data) {
    const dir = path.dirname(filePath);
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
    const rand = Math.random().toString(36).slice(2, 8);
    const tmp = `${filePath}.${process.pid}.${Date.now()}.${rand}.tmp`;
    const serialized = JSON.stringify(data, null, 2);
    fs.writeFileSync(tmp, serialized, 'utf8');

    for (let attempt = 0; attempt < 4; attempt++) {
        try {
            fs.renameSync(tmp, filePath);
            try {
                const st = fs.statSync(filePath);
                _fileCache.set(filePath, { mtimeMs: st.mtimeMs, size: st.size, parsed: data });
            } catch (_) {
                _fileCache.delete(filePath);
            }
            return true;
        } catch (_) {
            if (attempt === 3) {
                try { if (fs.existsSync(tmp)) fs.unlinkSync(tmp); } catch (_) {}
                return false;
            }
            await sleepMs(15 * (attempt + 1));
        }
    }
    return false;
}

function execFileAsync(bin, args, timeoutMs = 2000) {
    return new Promise((resolve) => {
        execFile(bin, args, { encoding: 'utf8', timeout: timeoutMs, windowsHide: true }, (err, stdout) => {
            if (err) resolve(null);
            else resolve(stdout || '');
        });
    });
}

const LOCAL_VPN_PROXY_PORTS = [10808, 10809, 2080, 7890, 2081, 7891, 8889, 12334];
let _autoSyncedProxyUrl = null;

function probeLocalTcpPort(port, timeoutMs = 150, host = '127.0.0.1') {
    return new Promise((resolve) => {
        const net = require('net');
        let settled = false;
        const sock = new net.Socket();
        const done = (ok) => {
            if (settled) return;
            settled = true;
            try { sock.destroy(); } catch (_) {}
            resolve(ok);
        };
        sock.setTimeout(timeoutMs);
        sock.once('connect', () => done(true));
        sock.once('timeout', () => done(false));
        sock.once('error', () => done(false));
        try {
            sock.connect(port, host || '127.0.0.1');
        } catch (_) {
            done(false);
        }
    });
}

function readSavedProxyConfigEntry() {
    try {
        const cfgPath = path.join(process.env.AG_TOOLS_DIR || AG_TOOLS_DIR, 'proxy_config.json');
        if (!fs.existsSync(cfgPath)) return null;
        const data = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
        const raw = String((data && (data.proxy_url || data.url)) || '').trim();
        if (!raw) return null;
        return {
            url: raw.includes('://') ? raw : ('http://' + raw),
            source: String((data && data.source) || '').trim(),
            cfgPath
        };
    } catch (_) {
        return null;
    }
}

function readSavedProxyConfig() {
    const entry = readSavedProxyConfigEntry();
    return entry ? entry.url : null;
}

function writeSavedProxyConfig(proxyUrl, source) {
    try {
        const dir = process.env.AG_TOOLS_DIR || AG_TOOLS_DIR;
        if (!fs.existsSync(dir)) return;
        const cfgPath = path.join(dir, 'proxy_config.json');
        fs.writeFileSync(cfgPath, JSON.stringify({
            proxy_url: proxyUrl,
            source: source || 'auto',
            updated_at: Math.floor(Date.now() / 1000)
        }, null, 2), 'utf8');
    } catch (_) {}
}

function clearSavedLocalProxyIfStale() {
    try {
        const entry = readSavedProxyConfigEntry();
        if (entry && entry.source.startsWith('Local VPN') && fs.existsSync(entry.cfgPath)) {
            fs.unlinkSync(entry.cfgPath);
        }
    } catch (_) {}
}

async function getActiveProxy(forceRefresh = false) {
    const now = Date.now();
    if (!forceRefresh && (now - _proxyCache.ts) < 30000) {
        return _proxyCache.value;
    }
    if (!forceRefresh && _proxyInFlight) {
        return _proxyInFlight;
    }

    const work = (async () => {
        let result = null;
        if (process.platform === 'win32') {
            const regKey = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings';
            const regOut = await execFileAsync('reg', ['query', regKey], 1800);
            if (regOut) {
                const enMatch = regOut.match(/^\s*ProxyEnable\s+REG_DWORD\s+(0x[0-9a-fA-F]+|\d+)/im);
                const enabled = Boolean(
                    enMatch && parseInt(enMatch[1], enMatch[1].toLowerCase().startsWith('0x') ? 16 : 10) !== 0
                );
                if (enabled) {
                    const m = regOut.match(/^\s*ProxyServer\s+REG_(?:EXPAND_)?SZ\s+([^\r\n]+)/im);
                    if (m && m[1]) {
                        let raw = m[1].trim();
                        if (raw.includes('=')) {
                            const httpMatch = raw.match(/(?:^|;)http=([^;]+)/i) || raw.match(/(?:^|;)https=([^;]+)/i);
                            raw = httpMatch ? httpMatch[1].trim() : '';
                        }
                        if (raw) {
                            if (!raw.startsWith('http://') && !raw.startsWith('https://') && !raw.startsWith('socks5://')) {
                                raw = 'http://' + raw;
                            }
                            result = { url: raw, source: 'Windows Registry' };
                        }
                    }
                }
            }
        }

        if (!result) {
            const envProxy = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy || process.env.ALL_PROXY || process.env.all_proxy;
            if (envProxy && envProxy !== _autoSyncedProxyUrl) {
                let raw = String(envProxy).trim();
                if (raw && !raw.includes('://')) raw = 'http://' + raw;
                if (raw) {
                    result = { url: raw, source: 'Environment' };
                }
            }
        }

        const allowLocalProbe = process.env.AG_DISABLE_LOCAL_PROXY_PROBE !== '1' && (
            !process.env.AG_TOOLS_DIR || process.env.AG_PROBE_LOCAL_PROXY === '1'
        );

        if (!result) {
            const savedEntry = readSavedProxyConfigEntry();
            if (savedEntry) {
                let okSaved = true;
                if (allowLocalProbe) {
                    try {
                        const parsed = new url.URL(savedEntry.url);
                        const portNum = parseInt(parsed.port, 10);
                        if (parsed.hostname && portNum) {
                            okSaved = await probeLocalTcpPort(portNum, 150, parsed.hostname);
                        }
                    } catch (_) {}
                }
                if (okSaved) {
                    result = { url: savedEntry.url, source: 'Saved Proxy Config' };
                } else {
                    clearSavedLocalProxyIfStale();
                }
            }
        }

        if (!result && allowLocalProbe) {
            const probeResults = await Promise.all(
                LOCAL_VPN_PROXY_PORTS.map(async (port) => (await probeLocalTcpPort(port, 140)) ? port : 0)
            );
            const activePort = probeResults.find((p) => p > 0);
            if (activePort) {
                const urlStr = `http://127.0.0.1:${activePort}`;
                result = { url: urlStr, source: `Local VPN (${activePort})` };
                writeSavedProxyConfig(urlStr, result.source);
            }
        }

        _proxyCache = { ts: Date.now(), value: result };
        return result;
    })().finally(() => {
        _proxyInFlight = null;
    });

    if (!forceRefresh) _proxyInFlight = work;
    return work;
}

async function syncProxyEnvironment(forceRefresh = false) {
    const proxy = await getActiveProxy(forceRefresh);
    if (proxy && proxy.url) {
        _autoSyncedProxyUrl = proxy.url;
        process.env.HTTP_PROXY = proxy.url;
        process.env.http_proxy = proxy.url;
        process.env.HTTPS_PROXY = proxy.url;
        process.env.https_proxy = proxy.url;
        process.env.ALL_PROXY = proxy.url;
        process.env.all_proxy = proxy.url;
    } else if (_autoSyncedProxyUrl) {
        if (process.env.HTTP_PROXY === _autoSyncedProxyUrl) delete process.env.HTTP_PROXY;
        if (process.env.http_proxy === _autoSyncedProxyUrl) delete process.env.http_proxy;
        if (process.env.HTTPS_PROXY === _autoSyncedProxyUrl) delete process.env.HTTPS_PROXY;
        if (process.env.https_proxy === _autoSyncedProxyUrl) delete process.env.https_proxy;
        if (process.env.ALL_PROXY === _autoSyncedProxyUrl) delete process.env.ALL_PROXY;
        if (process.env.all_proxy === _autoSyncedProxyUrl) delete process.env.all_proxy;
        _autoSyncedProxyUrl = null;
    }
    process.env.NO_PROXY = 'localhost,127.0.0.1,::1';
    process.env.no_proxy = 'localhost,127.0.0.1,::1';
    return proxy;
}

async function flushDnsCache() {
    if (process.platform === 'win32') {
        await execFileAsync('ipconfig', ['/flushdns'], 1800);
    }
}

function httpsRequest(targetUrl, { method = 'GET', headers = {}, body = null, timeout = 3500, proxyUrl = null } = {}) {
    return new Promise((resolve) => {
        const parsed = url.parse(targetUrl);
        const hostname = parsed.hostname;
        const port = parsed.port ? parseInt(parsed.port, 10) : 443;
        const pathAndQuery = parsed.path || '/';
        const payload = body !== null && body !== undefined
            ? (Buffer.isBuffer(body) ? body : Buffer.from(String(body), 'utf8'))
            : null;

        const reqHeaders = Object.assign({}, headers);
        if (payload && !reqHeaders['Content-Length'] && !reqHeaders['content-length']) {
            reqHeaders['Content-Length'] = String(payload.length);
        }

        const finishDirect = () => {
            let settled = false;
            const done = (res) => {
                if (!settled) {
                    settled = true;
                    resolve(res);
                }
            };
            const req = https.request({
                hostname,
                port,
                path: pathAndQuery,
                method,
                headers: reqHeaders,
                agent: _keepAliveAgent,
                timeout,
                rejectUnauthorized: false
            }, (res) => {
                let buf = '';
                res.setEncoding('utf8');
                res.on('data', (c) => { buf += c; });
                res.on('end', () => done({ ok: res.statusCode >= 200 && res.statusCode < 300, statusCode: res.statusCode, body: buf }));
            });
            req.on('timeout', () => { req.destroy(); done({ ok: false, statusCode: 0, body: '', error: 'timeout' }); });
            req.on('error', (err) => done({ ok: false, statusCode: 0, body: '', error: err.message }));
            if (payload) req.write(payload);
            req.end();
        };

        if (proxyUrl && proxyUrl.startsWith('http://')) {
            let settled = false;
            const done = (res) => {
                if (!settled) {
                    settled = true;
                    resolve(res);
                }
            };
            const parsedProxy = url.parse(proxyUrl);
            const connectReq = http.request({
                host: parsedProxy.hostname,
                port: parseInt(parsedProxy.port, 10) || 80,
                method: 'CONNECT',
                path: `${hostname}:${port}`,
                timeout
            });
            connectReq.on('connect', (res, socket) => {
                if (res.statusCode !== 200) {
                    try { socket.destroy(); } catch (_) {}
                    done({ ok: false, statusCode: res.statusCode, body: '', error: 'proxy_connect_failed' });
                    return;
                }
                const req = https.request({
                    hostname,
                    port,
                    path: pathAndQuery,
                    method,
                    headers: reqHeaders,
                    socket,
                    agent: false,
                    timeout,
                    rejectUnauthorized: false
                }, (tlsRes) => {
                    let buf = '';
                    tlsRes.setEncoding('utf8');
                    tlsRes.on('data', (c) => { buf += c; });
                    tlsRes.on('end', () => {
                        try { socket.destroy(); } catch (_) {}
                        done({ ok: tlsRes.statusCode >= 200 && tlsRes.statusCode < 300, statusCode: tlsRes.statusCode, body: buf });
                    });
                });
                req.on('timeout', () => { req.destroy(); try { socket.destroy(); } catch (_) {} done({ ok: false, statusCode: 0, body: '', error: 'timeout' }); });
                req.on('error', (err) => { try { socket.destroy(); } catch (_) {} done({ ok: false, statusCode: 0, body: '', error: err.message }); });
                if (payload) req.write(payload);
                req.end();
            });
            connectReq.on('timeout', () => { connectReq.destroy(); done({ ok: false, statusCode: 0, body: '', error: 'proxy_timeout' }); });
            connectReq.on('error', (err) => done({ ok: false, statusCode: 0, body: '', error: err.message }));
            connectReq.end();
            return;
        }

        finishDirect();
    });
}

function getLastKnownNetwork() {
    return _netCache.value;
}

async function detectNetwork({ forceProxyRefresh = false, maxAgeMs = 0, proxyUrl = undefined } = {}) {
    const now = Date.now();
    if (!forceProxyRefresh && maxAgeMs > 0 && _netCache.value && (now - _netCache.ts) < maxAgeMs) {
        return _netCache.value;
    }
    if (!forceProxyRefresh && _netInFlight) {
        return _netInFlight;
    }

    const reqSeq = ++_netSeq;
    const work = (async () => {
        if (forceProxyRefresh) {
            resetConnectionPool();
            await flushDnsCache().catch(() => {});
        }
        const proxy = proxyUrl === undefined
            ? await syncProxyEnvironment(forceProxyRefresh)
            : (proxyUrl ? { url: proxyUrl, source: 'Explicit' } : null);

        const start = Date.now();
        const res = await httpsRequest('https://cloudflare.com/cdn-cgi/trace', {
            method: 'GET',
            timeout: 3500,
            proxyUrl: proxy ? proxy.url : null
        });
        const ms = Date.now() - start;

        let out;
        if (!res.ok) {
            out = {
                ok: false,
                ms: 0,
                country: 'Офлайн',
                countryCode: '',
                status: proxy ? 'Прокси офлайн / таймаут' : 'Офлайн / таймаут',
                proxyUrl: proxy ? proxy.url : null
            };
        } else {
            const locMatch = res.body.match(/loc=([A-Z]{2})/);
            const countryCode = locMatch ? locMatch[1] : '';
            const country = getCountryName(countryCode);
            out = {
                ok: true,
                ms,
                country,
                countryCode,
                status: `${ms}ms`,
                proxyUrl: proxy ? proxy.url : null
            };
        }
        if (reqSeq === _netSeq || !_netCache.value) {
            _netCache = { ts: Date.now(), value: out };
            return out;
        }
        return _netCache.value;
    })().finally(() => {
        if (_netInFlight === work) {
            _netInFlight = null;
        }
    });

    if (!forceProxyRefresh) _netInFlight = work;
    return work;
}

function cleanNameFromAccount(detail, fallbackItem) {
    let name = (detail && detail.name) || (fallbackItem && fallbackItem.name) || '';
    const email = (detail && detail.email) || (fallbackItem && fallbackItem.email) || '';
    const idt = detail && detail.token && detail.token.id_token;
    if (idt && (!name || name.includes('?') || name.includes('\ufffd'))) {
        try {
            const parts = String(idt).split('.');
            if (parts.length >= 2) {
                const payload = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
                if (payload.name) name = payload.name;
            }
        } catch (_) {}
    }
    if (!name || name.includes('?') || name.includes('\ufffd')) {
        name = email ? email.split('@')[0] : 'Account';
    }
    return name;
}

function getCachedAvatarPath(email) {
    if (!email) return '';
    const now = Date.now();
    const cached = _avatarCache.get(email);
    if (cached && (now - cached.ts) < 60000) {
        return cached.path;
    }
    let found = '';
    try {
        const pngFile = path.join(AVATARS_DIR, `${email}.png`);
        if (fs.existsSync(pngFile) && fs.statSync(pngFile).size > 0) {
            found = pngFile;
        } else {
            const jpgFile = path.join(AVATARS_DIR, `${email}.jpg`);
            if (fs.existsSync(jpgFile) && fs.statSync(jpgFile).size > 0) {
                found = jpgFile;
            }
        }
    } catch (_) {}
    _avatarCache.set(email, { ts: now, path: found });
    return found;
}

const _avatarDataCache = new Map();

function getCachedAvatarDataUrl(email) {
    const filePath = getCachedAvatarPath(email);
    if (!filePath) return '';
    let mtimeMs = 0;
    try {
        mtimeMs = fs.statSync(filePath).mtimeMs || 0;
    } catch (_) {
        return '';
    }
    const cached = _avatarDataCache.get(filePath);
    if (cached && cached.mtimeMs === mtimeMs) {
        return cached.dataUrl;
    }
    let dataUrl = '';
    try {
        const raw = fs.readFileSync(filePath);
        if (raw && raw.length > 0 && raw.length <= 256 * 1024) {
            const ext = path.extname(filePath).toLowerCase();
            const mime = ext === '.jpg' || ext === '.jpeg' ? 'image/jpeg' : 'image/png';
            dataUrl = `data:${mime};base64,${raw.toString('base64')}`;
        }
    } catch (_) {}
    _avatarDataCache.set(filePath, { mtimeMs, dataUrl });
    return dataUrl;
}

function parseQuotaGroups(groups) {
    const out = {
        claudePct: null,
        claudeReset: null,
        claudeLimitingBucket: null,
        claudeWeeklyPct: null,
        claudeWeeklyReset: null,
        claude5hPct: null,
        claude5hReset: null,
        geminiPct: null,
        geminiReset: null,
        geminiLimitingBucket: null,
        geminiWeeklyPct: null,
        geminiWeeklyReset: null,
        gemini5hPct: null,
        gemini5hReset: null,
        argonPct: null,
        argonReset: null,
        argonLimitingBucket: null,
        argonWeeklyPct: null,
        argonWeeklyReset: null,
        argon5hPct: null,
        argon5hReset: null,
        geminiGroups: []
    };
    if (!Array.isArray(groups)) return out;

    for (const g of groups) {
        if (!g || typeof g !== 'object') continue;
        const dname = String(g.displayName || g.display_name || '');
        const dnameLow = dname.toLowerCase();
        const buckets = Array.isArray(g.buckets) ? g.buckets : [];

        // Classify Gemini strictly before Claude because both groups share weekly/5h windows
        const isGemini = dnameLow.includes('gemini') || dnameLow.includes('argon') || dnameLow.includes('hydrogen') || buckets.some((b) => {
            const bid = String((b && (b.bucketId || b.bucket_id)) || '').toLowerCase();
            return bid.includes('gemini') || bid.includes('argon') || bid.includes('hydrogen');
        });
        const isClaude = !isGemini && (
            dnameLow.includes('claude') ||
            dnameLow.includes('3p') ||
            dnameLow.includes('gpt') ||
            buckets.some((b) => {
                const bidLow = String((b && (b.bucketId || b.bucket_id)) || '').toLowerCase();
                const winLow = String((b && b.window) || '').toLowerCase();
                return bidLow.includes('3p') || winLow === 'weekly' || winLow === '5h';
            })
        );

        if (!isGemini && !isClaude) continue;

        const allCands = [];
        const enabledCands = [];
        let weeklyPct = null;
        let weeklyReset = null;
        let h5Pct = null;
        let h5Reset = null;
        let h5Bid = '';

        for (const b of buckets) {
            if (!b || typeof b !== 'object') continue;
            const rf = b.remainingFraction !== undefined ? b.remainingFraction : b.remaining_fraction;
            if (rf === undefined || rf === null) continue;
            const rfNum = Number(rf);
            if (!Number.isFinite(rfNum)) continue;
            const pctVal = Math.max(0, Math.min(100, Math.round(rfNum * 100)));
            const resetVal = b.resetTime || b.reset_time || null;
            const bid = String(b.bucketId || b.bucket_id || b.window || '');
            const bidLow = bid.toLowerCase();
            const winLow = String(b.window || '').toLowerCase();

            if (winLow === 'weekly' || bidLow.endsWith('-weekly') || bidLow === 'weekly') {
                if (!b.disabled || weeklyPct === null) {
                    weeklyPct = pctVal;
                    weeklyReset = resetVal;
                }
            } else if (winLow === '5h' || bidLow.endsWith('-5h') || bidLow === '5h') {
                if (!b.disabled) {
                    h5Pct = pctVal;
                    h5Reset = resetVal;
                    h5Bid = bid;
                }
            }

            const entry = { rf: rfNum, pct: pctVal, reset: resetVal, bid };
            allCands.push(entry);
            if (!b.disabled) enabledCands.push(entry);
        }

        const cands = enabledCands.length > 0 ? enabledCands : allCands;
        if (cands.length === 0) continue;
        cands.sort((a, b) => a.rf - b.rf);
        const limiting = cands[0];
        const use5h = (h5Pct !== null) && (weeklyPct === null || weeklyPct > 0);
        const primaryPct = use5h ? h5Pct : limiting.pct;
        const primaryReset = use5h ? h5Reset : limiting.reset;
        const primaryBid = use5h ? (h5Bid || limiting.bid) : limiting.bid;

        if (isGemini) {
            const isArgon = dnameLow.includes('argon') || buckets.some((b) =>
                String((b && (b.bucketId || b.bucket_id)) || '').toLowerCase().includes('argon')
            );
            const isMainGem = !isArgon && (
                dnameLow.includes('gemini models') ||
                dnameLow.includes('hydrogen') ||
                buckets.some((b) => String((b && (b.bucketId || b.bucket_id)) || '').toLowerCase().startsWith('gemini-'))
            );
            out.geminiGroups.push({
                id: isArgon ? 'gemini_argon' : (isMainGem ? 'gemini_models' : `gemini_${out.geminiGroups.length}`),
                display_name: dname || (isArgon ? 'Gemini 4 Argon' : 'Gemini Models'),
                short_label: isArgon ? 'Argon' : 'Gemini',
                description: String(g.description || ''),
                pct: primaryPct,
                reset_time: primaryReset,
                limiting_bucket: primaryBid || null,
                weekly_pct: weeklyPct,
                weekly_reset: weeklyReset,
                h5_pct: h5Pct,
                h5_reset: h5Reset,
                is_argon: isArgon,
                is_main: isMainGem
            });
            if (isArgon) {
                out.argonPct = primaryPct;
                out.argonReset = primaryReset;
                out.argonLimitingBucket = primaryBid || null;
                out.argonWeeklyPct = weeklyPct;
                out.argonWeeklyReset = weeklyReset;
                out.argon5hPct = h5Pct;
                out.argon5hReset = h5Reset;
            } else if (out.geminiPct === null || isMainGem) {
                out.geminiPct = primaryPct;
                out.geminiReset = primaryReset;
                out.geminiLimitingBucket = primaryBid || null;
                out.geminiWeeklyPct = weeklyPct;
                out.geminiWeeklyReset = weeklyReset;
                out.gemini5hPct = h5Pct;
                out.gemini5hReset = h5Reset;
            }
        } else if (isClaude) {
            out.claudePct = primaryPct;
            out.claudeReset = primaryReset;
            out.claudeLimitingBucket = primaryBid || null;
            out.claudeWeeklyPct = weeklyPct;
            out.claudeWeeklyReset = weeklyReset;
            out.claude5hPct = h5Pct;
            out.claude5hReset = h5Reset;
        }
    }

    if (out.geminiPct === null && out.argonPct !== null) {
        out.geminiPct = out.argonPct;
        out.geminiReset = out.argonReset;
        out.geminiLimitingBucket = out.argonLimitingBucket;
        out.geminiWeeklyPct = out.argonWeeklyPct;
        out.geminiWeeklyReset = out.argonWeeklyReset;
        out.gemini5hPct = out.argon5hPct;
        out.gemini5hReset = out.argon5hReset;
    }
    return out;
}

function computeQuotaSummary(detail, indexItem) {
    detail = detail || {};
    const q = detail.quota || {};
    const groups = Array.isArray(q.quota_groups) ? q.quota_groups : [];
    const models = Array.isArray(q.models) ? q.models : [];

    const parsed = parseQuotaGroups(groups);
    let claudePct = parsed.claudePct;
    let claudeReset = parsed.claudeReset;
    let claudeLimitingBucket = parsed.claudeLimitingBucket || q.claude_limiting_bucket || null;
    let geminiPct = parsed.geminiPct;
    let geminiReset = parsed.geminiReset;
    let geminiLimitingBucket = parsed.geminiLimitingBucket || q.gemini_limiting_bucket || null;
    let argonPct = parsed.argonPct;
    let argonReset = parsed.argonReset;
    let argonLimitingBucket = parsed.argonLimitingBucket || q.argon_limiting_bucket || null;

    const gemModelCands = [];
    const argonModelCands = [];
    const claudeModelCands = [];
    for (const m of models) {
        const mname = (m.name || '').toLowerCase();
        const dname = (m.display_name || '').toLowerCase();
        if (m.percentage === undefined || m.percentage === null) continue;
        if (mname.includes('2.5') || dname.includes('2.5') ||
            mname.includes('flash-lite') || mname.includes('flash_lite') ||
            mname.includes('-low') || mname.includes('-medium')) {
            continue;
        }
        if (mname.includes('argon') || dname.includes('argon')) {
            argonModelCands.push({ pct: Number(m.percentage), reset: m.reset_time || null });
        } else if (mname.includes('gemini')) {
            gemModelCands.push({ pct: Number(m.percentage), reset: m.reset_time || null });
        } else if (mname.includes('claude')) {
            claudeModelCands.push({ pct: Number(m.percentage), reset: m.reset_time || null });
        }
    }

    if (geminiPct === null && gemModelCands.length > 0) {
        gemModelCands.sort((a, b) => a.pct - b.pct);
        geminiPct = gemModelCands[0].pct;
        geminiReset = gemModelCands[0].reset;
    }

    if (argonPct === null) {
        if (q.argon_percentage !== undefined && q.argon_percentage !== null) argonPct = Number(q.argon_percentage);
        else if (detail.argon_percentage !== undefined && detail.argon_percentage !== null) argonPct = Number(detail.argon_percentage);
        else if (argonModelCands.length > 0) {
            argonModelCands.sort((a, b) => a.pct - b.pct);
            argonPct = argonModelCands[0].pct;
            argonReset = argonModelCands[0].reset;
        } else if (indexItem && indexItem.argon_percentage !== undefined && indexItem.argon_percentage !== null) {
            argonPct = Number(indexItem.argon_percentage);
        }
    }
    if (!argonReset) {
        argonReset = q.argon_reset_time || detail.argon_reset_time || (indexItem && indexItem.argon_reset_time) || null;
    }

    const geminiGroups = Array.isArray(parsed.geminiGroups) ? [...parsed.geminiGroups] : [];
    const argonWeeklyPct = parsed.argonWeeklyPct !== null ? parsed.argonWeeklyPct : (q.argon_weekly_percentage ?? detail.argon_weekly_percentage ?? null);
    const argonWeeklyReset = parsed.argonWeeklyReset || q.argon_weekly_reset_time || detail.argon_weekly_reset_time || null;
    const argon5hPct = parsed.argon5hPct !== null ? parsed.argon5hPct : (q.argon_5h_percentage ?? detail.argon_5h_percentage ?? null);
    const argon5hReset = parsed.argon5hReset || q.argon_5h_reset_time || detail.argon_5h_reset_time || null;

    if (argonPct !== null && !geminiGroups.some((g) => g && g.is_argon)) {
        geminiGroups.push({
            id: 'gemini_argon',
            display_name: 'Gemini 4 Argon',
            short_label: 'Argon',
            description: '',
            pct: argonPct,
            reset_time: argonReset,
            limiting_bucket: argonLimitingBucket,
            weekly_pct: argonWeeklyPct,
            weekly_reset: argonWeeklyReset,
            h5_pct: argon5hPct,
            h5_reset: argon5hReset,
            is_argon: true,
            is_main: false
        });
    }

    if (claudePct === null) {
        if (q.claude_percentage !== undefined && q.claude_percentage !== null) claudePct = Number(q.claude_percentage);
        else if (detail.claude_percentage !== undefined && detail.claude_percentage !== null) claudePct = Number(detail.claude_percentage);
        else if (claudeModelCands.length > 0) {
            claudeModelCands.sort((a, b) => a.pct - b.pct);
            claudePct = claudeModelCands[0].pct;
            claudeReset = claudeModelCands[0].reset;
        } else if (indexItem && indexItem.claude_percentage !== undefined && indexItem.claude_percentage !== null) {
            claudePct = Number(indexItem.claude_percentage);
        } else {
            claudePct = 100;
        }
    }

    if (!claudeReset) {
        claudeReset = q.claude_reset_time || detail.claude_reset_time || null;
    }

    if (geminiPct === null) {
        if (q.gemini_percentage !== undefined && q.gemini_percentage !== null) geminiPct = Number(q.gemini_percentage);
        else if (detail.gemini_percentage !== undefined && detail.gemini_percentage !== null) geminiPct = Number(detail.gemini_percentage);
        else if (indexItem && indexItem.gemini_percentage !== undefined && indexItem.gemini_percentage !== null) {
            geminiPct = Number(indexItem.gemini_percentage);
        } else {
            geminiPct = 100;
        }
    }

    if (!geminiReset) {
        geminiReset = q.gemini_reset_time || detail.gemini_reset_time || null;
    }

    const tier = q.subscription_tier || detail.tier || (indexItem && indexItem.tier) || 'PRO';

    return {
        tier,
        claude_percentage: claudePct,
        gemini_percentage: geminiPct,
        claude_reset_time: claudeReset,
        gemini_reset_time: geminiReset,
        claude_limiting_bucket: claudeLimitingBucket,
        claude_weekly_percentage: parsed.claudeWeeklyPct,
        claude_weekly_reset_time: parsed.claudeWeeklyReset,
        claude_5h_percentage: parsed.claude5hPct,
        claude_5h_reset_time: parsed.claude5hReset,
        gemini_limiting_bucket: geminiLimitingBucket,
        gemini_weekly_percentage: parsed.geminiWeeklyPct,
        gemini_weekly_reset_time: parsed.geminiWeeklyReset,
        gemini_5h_percentage: parsed.gemini5hPct,
        gemini_5h_reset_time: parsed.gemini5hReset,
        argon_percentage: argonPct,
        argon_reset_time: argonReset,
        argon_limiting_bucket: argonLimitingBucket,
        argon_weekly_percentage: argonWeeklyPct,
        argon_weekly_reset_time: argonWeeklyReset,
        argon_5h_percentage: argon5hPct,
        argon_5h_reset_time: argon5hReset,
        gemini_groups: geminiGroups,
        last_updated: Number(q.last_updated || 0)
    };
}

function readLiveAuthFromFiles(gemFiles) {
    for (const gf of gemFiles) {
        const gdata = readJsonCached(gf);
        if (!gdata) continue;
        const tok = (gdata.token && typeof gdata.token === 'object') ? gdata.token : gdata;
        const rt = tok && tok.refresh_token ? String(tok.refresh_token).trim() : '';
        let em = String(gdata.id_token_email || gdata.email || gdata.active || '').trim().toLowerCase();
        if (!em && tok && tok.id_token) {
            try {
                const parts = String(tok.id_token).split('.');
                if (parts.length >= 2) {
                    const p = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
                    if (p.email) em = String(p.email).trim().toLowerCase();
                }
            } catch (_) {}
        }
        if (rt || em) {
            return { refreshToken: rt, email: em };
        }
    }
    return null;
}

const LIVE_AUTH_MAX_AGE_SEC = 120;
let _liveSyncInFlight = null;
let _lastLiveSyncAt = 0;

function readLiveAuthSection(section) {
    const liveFile = path.join(AG_TOOLS_DIR, 'live_auth.json');
    const liveData = readJsonCached(liveFile);
    if (!liveData || !liveData[section]) {
        return { auth: null, isFresh: false };
    }
    const entry = liveData[section];
    if (!entry.email && !entry.id) {
        return { auth: null, isFresh: false };
    }
    const updatedAt = Number(liveData.updated_at || 0);
    const ageSec = updatedAt > 0 ? (Math.floor(Date.now() / 1000) - updatedAt) : Number.POSITIVE_INFINITY;
    return {
        auth: {
            id: entry.id || '',
            email: String(entry.email || '').trim().toLowerCase(),
            name: entry.name || ''
        },
        isFresh: ageSec >= -60 && ageSec <= LIVE_AUTH_MAX_AGE_SEC
    };
}

let _cachedPythonBin = null;

/**
 * Return ordered Python executable candidates: bundled Antigravity Python runtime
 * paths (if present on disk) alongside system python / py / python3 fallbacks.
 */
function getPythonCandidates() {
    if (_cachedPythonBin) {
        return [_cachedPythonBin];
    }
    const isWin = process.platform === 'win32';
    const pyExe = isWin ? 'python.exe' : 'python3';
    const rawPaths = [];

    if (process.env.AG_PYTHON_PATH) {
        rawPaths.push(process.env.AG_PYTHON_PATH);
    }

    const resourcesPath = process.resourcesPath || '';
    const execDir = process.execPath ? path.dirname(process.execPath) : '';
    const geminiDir = process.env.AG_GEMINI_DIR || path.join(HOME_DIR, '.gemini');
    const agDir = process.env.AG_TOOLS_DIR || AG_TOOLS_DIR;

    const candidateRoots = [
        resourcesPath,
        execDir ? path.join(execDir, 'resources') : '',
        path.join(geminiDir, 'antigravity'),
        agDir
    ];
    if (isWin) {
        const localApp = process.env.LOCALAPPDATA || path.join(HOME_DIR, 'AppData', 'Local');
        candidateRoots.push(
            path.join(localApp, 'antigravity'),
            path.join(localApp, 'Programs', 'antigravity', 'resources'),
            path.join(localApp, 'Programs', 'Antigravity IDE', 'resources')
        );
    } else if (process.platform === 'darwin') {
        candidateRoots.push('/Applications/Antigravity.app/Contents/Resources');
    } else {
        candidateRoots.push('/opt/antigravity/resources', '/usr/share/antigravity/resources');
    }

    for (const root of candidateRoots) {
        if (!root) continue;
        if (isWin) {
            rawPaths.push(
                path.join(root, 'bin', 'python.exe'),
                path.join(root, 'python', 'python.exe'),
                path.join(root, 'python', 'Scripts', 'python.exe'),
                path.join(root, 'app', 'python', 'python.exe')
            );
        } else {
            rawPaths.push(
                path.join(root, 'bin', 'python3'),
                path.join(root, 'bin', 'python'),
                path.join(root, 'python', 'bin', 'python3'),
                path.join(root, 'python', 'bin', 'python')
            );
        }
    }

    const out = [];
    const seen = new Set();
    for (const p of rawPaths) {
        if (!p || seen.has(p)) continue;
        seen.add(p);
        try {
            if (fs.existsSync(p)) out.push(p);
        } catch (_) {}
    }

    const fallbacks = isWin ? ['python', 'py', 'python3'] : ['python3', 'python'];
    for (const cmd of fallbacks) {
        if (!seen.has(cmd)) {
            seen.add(cmd);
            out.push(cmd);
        }
    }
    return out;
}

function resolvePythonCommand() {
    const candidates = getPythonCandidates();
    const bin = candidates[0] || (process.platform === 'win32' ? 'python' : 'python3');
    return {
        bin,
        prefixArgs: bin === 'py' ? ['-3'] : []
    };
}

function resolveAccountSwitcherPath() {
    const candidates = [
        path.join(__dirname, 'account_switcher.py'),
        path.join(AG_TOOLS_DIR, 'account_switcher.py')
    ];
    return candidates.find((p) => fs.existsSync(p)) || '';
}

function syncLiveAuth({ appTarget = 'ide', force = false, minAgeSec = 15 } = {}) {
    if (process.env.AG_DISABLE_WINCRED_SYNC === '1') {
        return Promise.resolve({ ok: false, skipped: true });
    }
    if (_liveSyncInFlight) return _liveSyncInFlight;
    const now = Date.now();
    const cooldownMs = Math.max(0, Number(minAgeSec) || 0) * 1000;
    if (!force && _lastLiveSyncAt > 0 && (now - _lastLiveSyncAt) < cooldownMs) {
        return Promise.resolve({ ok: true, cached: true });
    }
    const switcherPy = resolveAccountSwitcherPath();
    if (!switcherPy) {
        return Promise.resolve({ ok: false, error: 'account_switcher.py not found' });
    }

    const candidates = getPythonCandidates();
    const scriptArgs = [switcherPy, 'sync_live', '--app', String(appTarget || 'ide')];

    _liveSyncInFlight = new Promise((resolve) => {
        const tryIdx = (idx) => {
            if (idx >= candidates.length) {
                _lastLiveSyncAt = Date.now();
                resolve({ ok: false, error: 'Python executable not found' });
                return;
            }
            const bin = candidates[idx];
            const finalArgs = bin === 'py' ? ['-3', ...scriptArgs] : scriptArgs;
            execFile(bin, finalArgs, { timeout: 15000, windowsHide: true }, (err, stdout) => {
                if (err && err.code === 'ENOENT') {
                    if (_cachedPythonBin === bin) _cachedPythonBin = null;
                    tryIdx(idx + 1);
                    return;
                }
                _cachedPythonBin = bin;
                _lastLiveSyncAt = Date.now();
                _fileCache.delete(path.join(AG_TOOLS_DIR, 'live_auth.json'));
                _fileCache.delete(path.join(AG_TOOLS_DIR, 'accounts.json'));
                if (err) {
                    resolve({ ok: false, error: String(err.message || err) });
                    return;
                }
                try {
                    const parsed = JSON.parse(String(stdout || '').trim() || '{}');
                    resolve({ ok: true, ...parsed });
                } catch (_) {
                    resolve({ ok: true });
                }
            });
        };
        tryIdx(0);
    }).finally(() => {
        _liveSyncInFlight = null;
    });
    return _liveSyncInFlight;
}

function detectLiveIdeAuth() {
    const live = readLiveAuthSection('ide');
    if (live.isFresh && live.auth) {
        return live.auth;
    }
    const fromFiles = readLiveAuthFromFiles([
        path.join(GEMINI_DIR, 'antigravity-cli', 'antigravity-oauth-token'),
        path.join(GEMINI_DIR, 'jetski-standalone-oauth-token'),
        path.join(GEMINI_DIR, 'oauth_creds.json'),
        path.join(GEMINI_DIR, 'google_accounts.json')
    ]);
    if (fromFiles) return fromFiles;
    return live.auth;
}

function detectLiveStandaloneAuth() {
    // 1. Trust fresh live_auth.json written by account_switcher WinCred sync
    const live = readLiveAuthSection('standalone');
    if (live.isFresh && live.auth) {
        return live.auth;
    }

    // 2. Check app_storage.json in Roaming/Antigravity
    const appStoragePath = path.join(HOME_DIR, 'AppData', 'Roaming', 'Antigravity', 'app_storage.json');
    const appStorage = readJsonCached(appStoragePath);
    if (appStorage) {
        const lastUser = appStorage['jetski.onboarding.lastLoginUsername'] || appStorage['agy.onboarding.lastLoginUsername'];
        if (lastUser) {
            return { refreshToken: '', email: String(lastUser).trim().toLowerCase() };
        }
    }

    // 3. Fallback to token file, then stale live_auth snapshot
    const fromFile = readLiveAuthFromFiles([
        path.join(GEMINI_DIR, 'jetski-desktopapp-oauth-token')
    ]);
    if (fromFile) return fromFile;
    return live.auth;
}

function ideUsesAgyHub() {
    const now = Date.now();
    if (_agyHubCache.ts && (now - _agyHubCache.ts) < 60000) {
        return _agyHubCache.value;
    }
    let detected = false;
    try {
        if (fs.existsSync(path.join(GEMINI_DIR, 'bin', 'agy.exe'))) {
            detected = true;
        } else {
            for (const root of [path.join(HOME_DIR, '.vscode', 'extensions'), path.join(HOME_DIR, '.antigravity-ide', 'extensions')]) {
                if (fs.existsSync(root) && fs.readdirSync(root).some((d) => d.startsWith('google.google-antigravity'))) {
                    detected = true;
                    break;
                }
            }
        }
    } catch (_) {}
    _agyHubCache = { ts: now, value: detected };
    return detected;
}

function loadState({ appTarget = 'ide' } = {}) {
    const accountsData = readJsonCached(ACCOUNTS_INDEX_FILE);
    if (!accountsData || !Array.isArray(accountsData.accounts)) {
        return { ok: true, activeAccount: null, accounts: [] };
    }

    const rawList = accountsData.accounts;
    const detailsById = new Map();
    const sharedPollState = getSharedPollState();
    const enriched = [];

    for (const item of rawList) {
        const detailFile = path.join(ACCOUNTS_DIR, `${item.id}.json`);
        const detail = readJsonCached(detailFile);
        if (detail) detailsById.set(item.id, detail);

        const email = (detail && detail.email) || item.email || '';
        const name = cleanNameFromAccount(detail, item);
        const summary = computeQuotaSummary(detail, item);
        const avatarPath = getCachedAvatarPath(email);
        const avatarDataUrl = getCachedAvatarDataUrl(email);
        const memChecked = _lastCheckedMap.get(item.id) || 0;
        const sharedChecked = Number(sharedPollState[item.id] || 0);

        enriched.push({
            id: item.id,
            email,
            name,
            avatar_path: avatarPath,
            avatar_data_url: avatarDataUrl,
            avatar_url: (detail && detail.avatar_url) || '',
            tier: summary.tier,
            claude_percentage: summary.claude_percentage,
            gemini_percentage: summary.gemini_percentage,
            claude_reset_time: summary.claude_reset_time,
            gemini_reset_time: summary.gemini_reset_time,
            claude_limiting_bucket: summary.claude_limiting_bucket,
            claude_weekly_percentage: summary.claude_weekly_percentage,
            claude_weekly_reset_time: summary.claude_weekly_reset_time,
            claude_5h_percentage: summary.claude_5h_percentage,
            claude_5h_reset_time: summary.claude_5h_reset_time,
            gemini_limiting_bucket: summary.gemini_limiting_bucket,
            gemini_weekly_percentage: summary.gemini_weekly_percentage,
            gemini_weekly_reset_time: summary.gemini_weekly_reset_time,
            gemini_5h_percentage: summary.gemini_5h_percentage,
            gemini_5h_reset_time: summary.gemini_5h_reset_time,
            argon_percentage: summary.argon_percentage,
            argon_reset_time: summary.argon_reset_time,
            argon_limiting_bucket: summary.argon_limiting_bucket,
            argon_weekly_percentage: summary.argon_weekly_percentage,
            argon_weekly_reset_time: summary.argon_weekly_reset_time,
            argon_5h_percentage: summary.argon_5h_percentage,
            argon_5h_reset_time: summary.argon_5h_reset_time,
            gemini_groups: summary.gemini_groups,
            last_updated: Math.max(summary.last_updated, memChecked, sharedChecked),
            last_used: item.last_used || 0,
            standalone_last_used: item.standalone_last_used || 0,
            quota: (detail && detail.quota) || null,
            _rt: (detail && detail.token && detail.token.refresh_token) || ''
        });
    }

    let matched = null;
    const liveGem = appTarget === 'standalone'
        ? detectLiveStandaloneAuth()
        : (ideUsesAgyHub() ? detectLiveIdeAuth() : null);
    if (liveGem) {
        if (liveGem.id) {
            matched = enriched.find((a) => a.id === liveGem.id) || null;
        }
        if (!matched && liveGem.refreshToken) {
            matched = enriched.find((a) => a._rt && a._rt === liveGem.refreshToken) || null;
        }
        if (!matched && liveGem.email) {
            matched = enriched.find((a) => a.email && a.email.toLowerCase() === liveGem.email) || null;
        }
    }

    if (!matched) {
        const key = appTarget === 'standalone'
            ? accountsData.current_standalone_account_id
            : (accountsData.current_ide_account_id || accountsData.current_account_id);
        if (key) {
            matched = enriched.find((a) => a.id === key) || null;
        }
    }

    if (!matched && enriched.length > 0) {
        if (appTarget === 'standalone') {
            const usedStandalone = enriched.filter((a) => Number(a.standalone_last_used || 0) > 0);
            if (usedStandalone.length > 0) {
                usedStandalone.sort((a, b) => (b.standalone_last_used || 0) - (a.standalone_last_used || 0));
                matched = usedStandalone[0];
            }
        } else {
            matched = enriched.slice().sort((a, b) => (b.last_used || 0) - (a.last_used || 0))[0];
        }
    }

    // Keep accounts.json index keys in hard sync with matched live account
    if (matched && matched.id) {
        try {
            if (appTarget === 'standalone' && accountsData.current_standalone_account_id !== matched.id) {
                accountsData.current_standalone_account_id = matched.id;
                fs.writeFileSync(ACCOUNTS_INDEX_FILE, JSON.stringify(accountsData, null, 2), 'utf8');
            } else if (appTarget === 'ide' && accountsData.current_ide_account_id !== matched.id) {
                accountsData.current_ide_account_id = matched.id;
                fs.writeFileSync(ACCOUNTS_INDEX_FILE, JSON.stringify(accountsData, null, 2), 'utf8');
            }
        } catch (_) {}
    }

    let activeAccount = null;
    const publicAccounts = enriched.map(({ _rt, standalone_last_used: _slu, ...rest }) => {
        const isActive = Boolean(matched && rest.id === matched.id);
        const obj = { ...rest, is_active: isActive };
        if (isActive) activeAccount = obj;
        return obj;
    });

    return {
        ok: true,
        activeAccount,
        accounts: publicAccounts
    };
}

function scanFileForOAuthStrings(filePath, secretPrefix, cidSet, secSet) {
    let fd = null;
    try {
        const st = fs.statSync(filePath);
        if (!st.isFile() || st.size === 0 || st.size > 400 * 1024 * 1024) return;
        const secretFullRe = new RegExp(`^${secretPrefix}[A-Za-z0-9_-]{28}$`);
        const cidTailRe = /([0-9]{10,}-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com)$/;

        if (st.size <= 32 * 1024 * 1024) {
            const text = fs.readFileSync(filePath).toString('latin1');
            const cidAllRe = /[0-9]{10,}-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com/g;
            const secAllRe = new RegExp(`${secretPrefix}[A-Za-z0-9_-]{28}`, 'g');
            for (const m of text.matchAll(cidAllRe)) cidSet.add(m[0]);
            for (const m of text.matchAll(secAllRe)) secSet.add(m[0]);
            return;
        }

        const CHUNK = 8 * 1024 * 1024;
        const OVERLAP = 256;
        const buf = Buffer.allocUnsafe(CHUNK + OVERLAP);
        const prefBuf = Buffer.from(secretPrefix, 'ascii');
        const cidSuffBuf = Buffer.from('.apps.googleusercontent.com', 'ascii');
        fd = fs.openSync(filePath, 'r');
        let pos = 0;
        while (pos < st.size) {
            const toRead = Math.min(buf.length, st.size - pos);
            const n = fs.readSync(fd, buf, 0, toRead, pos);
            if (n <= 0) break;
            const slice = buf.subarray(0, n);

            let idx = 0;
            while ((idx = slice.indexOf(prefBuf, idx)) !== -1) {
                const seg = slice.subarray(idx, Math.min(idx + 35, n)).toString('latin1');
                if (secretFullRe.test(seg)) secSet.add(seg);
                idx += prefBuf.length;
            }

            idx = 0;
            while ((idx = slice.indexOf(cidSuffBuf, idx)) !== -1) {
                const start = Math.max(0, idx - 64);
                const seg = slice.subarray(start, idx + cidSuffBuf.length).toString('latin1');
                const m = seg.match(cidTailRe);
                if (m) cidSet.add(m[1]);
                idx += cidSuffBuf.length;
            }

            pos += (n === buf.length) ? CHUNK : n;
        }
    } catch (_) {
    } finally {
        if (fd !== null) {
            try { fs.closeSync(fd); } catch (_) {}
        }
    }
}

function discoverOAuthClientsFromBinaries() {
    const localApp = process.env.LOCALAPPDATA || path.join(HOME_DIR, 'AppData', 'Local');
    const progFiles = process.env.ProgramFiles || 'C:\\Program Files';
    const candidates = [
        path.join(localApp, 'Programs', 'Antigravity IDE', 'resources', 'app', 'out', 'main.js'),
        path.join(progFiles, 'Antigravity IDE', 'resources', 'app', 'out', 'main.js'),
        '/Applications/Antigravity IDE.app/Contents/Resources/app/out/main.js',
        '/usr/share/antigravity-ide/resources/app/out/main.js',
        path.join(GEMINI_DIR, 'bin', 'agy.exe'),
        path.join(GEMINI_DIR, 'bin', 'antigravity.exe'),
        path.join(GEMINI_DIR, 'bin', 'agy'),
        path.join(GEMINI_DIR, 'bin', 'antigravity'),
        path.join(localApp, 'Programs', 'antigravity', 'resources', 'bin', 'language_server.exe'),
        '/Applications/Antigravity.app/Contents/Resources/bin/language_server',
        '/opt/antigravity/resources/bin/language_server'
    ];

    const secretPrefix = ['GOC', 'SPX-'].join('');
    const cidSet = new Set();
    const secSet = new Set();
    let sourcesScanned = 0;

    for (const binPath of candidates) {
        if (!fs.existsSync(binPath)) continue;
        const prevCount = cidSet.size + secSet.size;
        scanFileForOAuthStrings(binPath, secretPrefix, cidSet, secSet);
        if ((cidSet.size + secSet.size) > prevCount) {
            sourcesScanned++;
        }
        // Scanning main.js + primary binary covers all client IDs without excess I/O
        if (sourcesScanned >= 2 && cidSet.size > 0 && secSet.size > 0) {
            break;
        }
    }

    const cids = Array.from(cidSet);
    const secs = Array.from(secSet);
    if (cids.length === 0 || secs.length === 0) return [];

    const pairs = [];
    for (let i = 0; i < Math.min(cids.length, secs.length); i++) {
        pairs.push({ client_id: cids[i], client_secret: secs[i], verified: false });
    }
    for (const cid of cids.slice(0, 4)) {
        for (const sec of secs.slice(0, 4)) {
            if (!pairs.some((p) => p.client_id === cid && p.client_secret === sec)) {
                pairs.push({ client_id: cid, client_secret: sec, verified: false });
            }
        }
    }
    return pairs.slice(0, 12);
}

function getOAuthClients() {
    if (Array.isArray(_oauthClientsCache) && _oauthClientsCache.length > 0) {
        return _oauthClientsCache;
    }
    try {
        if (fs.existsSync(OAUTH_CLIENTS_FILE)) {
            const parsed = JSON.parse(fs.readFileSync(OAUTH_CLIENTS_FILE, 'utf8'));
            if (Array.isArray(parsed) && parsed.length > 0) {
                _oauthClientsCache = parsed;
                return parsed;
            }
        }
    } catch (_) {}

    const discovered = discoverOAuthClientsFromBinaries();
    if (discovered.length > 0) {
        _oauthClientsCache = discovered;
        atomicWriteJson(OAUTH_CLIENTS_FILE, discovered).catch(() => {});
    }
    return discovered;
}

async function persistVerifiedOAuthClient(cid, csec) {
    if (!cid || !csec) return;
    const current = getOAuthClients();
    const otherVerified = current.filter((p) => !(p.client_id === cid && p.client_secret === csec) && p.verified);
    const otherUnverified = current.filter((p) => !(p.client_id === cid && p.client_secret === csec) && !p.verified);
    const ordered = [{ client_id: cid, client_secret: csec, verified: true }, ...otherVerified, ...otherUnverified].slice(0, 12);
    _oauthClientsCache = ordered;
    await atomicWriteJson(OAUTH_CLIENTS_FILE, ordered);
}

async function refreshGoogleToken(refreshToken, proxyUrl = null) {
    if (!refreshToken) return null;
    let clients = getOAuthClients();
    for (let pass = 0; pass < 2; pass++) {
        for (const pair of clients) {
            const cid = pair.client_id;
            const csec = pair.client_secret;
            if (!cid || !csec) continue;
            try {
                const form = new URLSearchParams({
                    client_id: cid,
                    client_secret: csec,
                    refresh_token: refreshToken,
                    grant_type: 'refresh_token'
                }).toString();

                const res = await httpsRequest('https://oauth2.googleapis.com/token', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'User-Agent': getIdeUserAgent()
                    },
                    body: form,
                    timeout: 8000,
                    proxyUrl
                });
                if (res.ok && res.body) {
                    const parsed = JSON.parse(res.body);
                    if (parsed && parsed.access_token) {
                        if (!pair.verified || !fs.existsSync(OAUTH_CLIENTS_FILE) ||
                            (clients[0] && (clients[0].client_id !== cid || clients[0].client_secret !== csec))) {
                            persistVerifiedOAuthClient(cid, csec).catch(() => {});
                        }
                        return {
                            access_token: parsed.access_token,
                            expires_in: Number(parsed.expires_in || 3599),
                            id_token: parsed.id_token || ''
                        };
                    }
                }
            } catch (_) {}
        }
        if (pass === 0 && !_oauthRediscovered) {
            _oauthRediscovered = true;
            const fresh = discoverOAuthClientsFromBinaries();
            const merged = [...clients];
            for (const p of fresh) {
                if (!merged.some((x) => x.client_id === p.client_id && x.client_secret === p.client_secret)) {
                    merged.push(p);
                }
            }
            if (merged.length > clients.length) {
                _oauthClientsCache = merged.slice(0, 12);
                clients = _oauthClientsCache;
                continue;
            }
        }
        break;
    }
    return null;
}

const AICODE_REMOTE_QUOTA_URL = 'https://aicode.googleapis.com/v1alpha/remote:getUserQuotaSummary';

function getQuotaGroupKey(g, fallbackIdx) {
    const buckets = Array.isArray(g && g.buckets) ? g.buckets : [];
    const dnameLow = String((g && (g.displayName || g.display_name)) || '').toLowerCase();
    const hasArgon = dnameLow.includes('argon') || buckets.some((b) =>
        String((b && (b.bucketId || b.bucket_id)) || '').toLowerCase().includes('argon')
    );
    if (hasArgon) return 'argon';
    if (dnameLow.includes('gemini') || dnameLow.includes('hydrogen')) return 'gemini_models';
    if (dnameLow.includes('claude') || dnameLow.includes('gpt') || dnameLow.includes('3p')) return 'claude';
    return dnameLow || `group_${fallbackIdx}`;
}

function mergeQuotaGroups(primaryGroups, fallbackGroups) {
    const merged = [];
    const seen = new Set();
    for (const src of [primaryGroups, fallbackGroups]) {
        if (!Array.isArray(src)) continue;
        for (let i = 0; i < src.length; i++) {
            const g = src[i];
            if (!g || typeof g !== 'object') continue;
            const key = getQuotaGroupKey(g, merged.length);
            if (seen.has(key)) continue;
            seen.add(key);
            merged.push(g);
        }
    }
    return merged;
}

async function fetchRemoteQuotaSummary(headers, proxyUrl = null, timeout = 3500) {
    const getHeaders = {
        'Authorization': headers && headers.Authorization ? headers.Authorization : '',
        'User-Agent': (headers && headers['User-Agent']) || getIdeUserAgent()
    };
    const res = await httpsRequest(AICODE_REMOTE_QUOTA_URL, {
        method: 'GET',
        headers: getHeaders,
        timeout,
        proxyUrl
    });
    if (res.statusCode >= 200 && res.statusCode < 300 && res.body) {
        try {
            const parsed = JSON.parse(res.body);
            const respObj = (parsed && parsed.summary && parsed.summary.response)
                || (parsed && parsed.response)
                || parsed;
            const groups = respObj && Array.isArray(respObj.groups) ? respObj.groups : [];
            return { groups, data: respObj || {}, code: res.statusCode };
        } catch (_) {}
    }
    return { groups: [], data: {}, code: res.statusCode };
}

async function postCompanionJson(endpoint, headers, proxyUrl = null, timeout = 3500, { host = DAILY_COMPANION_HOST, body = '{}' } = {}) {
    const cleanHost = String(host || DAILY_COMPANION_HOST).replace(/\/+$/, '');
    const res = await httpsRequest(`${cleanHost}/v1internal:${endpoint}`, {
        method: 'POST',
        headers,
        body: body !== undefined && body !== null ? body : '{}',
        timeout,
        proxyUrl
    });
    if (res.body) {
        try {
            return { data: JSON.parse(res.body), code: res.statusCode };
        } catch (_) {}
    }
    return { data: {}, code: res.statusCode };
}

function updateAccountsIndexBatch(updatesById) {
    if (!updatesById || updatesById.size === 0) return Promise.resolve();
    _indexWriteQueue = _indexWriteQueue.then(async () => {
        try {
            if (!fs.existsSync(ACCOUNTS_INDEX_FILE)) return;
            const idx = JSON.parse(fs.readFileSync(ACCOUNTS_INDEX_FILE, 'utf8'));
            if (!idx || !Array.isArray(idx.accounts)) return;
            let changed = false;
            for (const item of idx.accounts) {
                const upd = updatesById.get(item.id);
                if (!upd) continue;
                if (item.claude_percentage !== upd.claude_percentage) {
                    item.claude_percentage = upd.claude_percentage;
                    changed = true;
                }
                if (item.gemini_percentage !== upd.gemini_percentage) {
                    item.gemini_percentage = upd.gemini_percentage;
                    changed = true;
                }
                if (upd.argon_percentage !== undefined && upd.argon_percentage !== null) {
                    if (item.argon_percentage !== upd.argon_percentage) {
                        item.argon_percentage = upd.argon_percentage;
                        changed = true;
                    }
                } else if (item.argon_percentage !== undefined) {
                    delete item.argon_percentage;
                    changed = true;
                }
                if (upd.argon_reset_time) {
                    if (item.argon_reset_time !== upd.argon_reset_time) {
                        item.argon_reset_time = upd.argon_reset_time;
                        changed = true;
                    }
                } else if (item.argon_reset_time !== undefined) {
                    delete item.argon_reset_time;
                    changed = true;
                }
                if (upd.tier && item.tier !== upd.tier) {
                    item.tier = upd.tier;
                    changed = true;
                }
            }
            if (changed) {
                await atomicWriteJson(ACCOUNTS_INDEX_FILE, idx);
            }
        } catch (_) {}
    });
    return _indexWriteQueue;
}

async function fetchLiveQuota(
    detailFileOrId,
    { updateIndex = true, includeTier = null, proxyUrl = undefined, minAgeSec = undefined, persistPollState = true, force = undefined } = {}
) {
    try {
        let destFile = '';
        let passedAccQuota = null;
        if (typeof detailFileOrId === 'string') {
            destFile = fs.existsSync(detailFileOrId)
                ? detailFileOrId
                : path.join(ACCOUNTS_DIR, `${detailFileOrId}.json`);
        } else if (detailFileOrId && typeof detailFileOrId === 'object' && detailFileOrId.id) {
            passedAccQuota = detailFileOrId.quota && typeof detailFileOrId.quota === 'object' ? detailFileOrId.quota : null;
            destFile = path.join(ACCOUNTS_DIR, `${detailFileOrId.id}.json`);
        } else {
            return null;
        }
        if (!fs.existsSync(destFile)) return null;

        const nowSecFloat = Date.now() / 1000;
        const nowSec = Math.floor(nowSecFloat);
        const effectiveMinAge = force === true
            ? 0
            : (minAgeSec !== undefined ? Number(minAgeSec) : (force === false ? 12 : 0));

        if (effectiveMinAge > 0) {
            const cachedDetail = readJsonCached(destFile);
            if (cachedDetail) {
                const cachedQ = cachedDetail.quota || {};
                const hasGcpTosFlag = typeof cachedQ.uses_gcp_tos === 'boolean';
                const hasValidModels = (Array.isArray(cachedQ.models) && cachedQ.models.length > 0) ||
                    (Array.isArray(cachedQ.quota_groups) && cachedQ.quota_groups.length > 0) ||
                    (passedAccQuota && Array.isArray(passedAccQuota.models) && passedAccQuota.models.length > 0);
                const accId = cachedDetail.id || path.basename(destFile, '.json');
                const sharedState = getSharedPollState();
                const lastChecked = Math.max(
                    Number(cachedQ.quota_updated_at || 0),
                    Number(cachedQ.last_updated || 0),
                    Number((passedAccQuota && passedAccQuota.quota_updated_at) || 0),
                    _lastCheckedMap.get(accId) || 0,
                    Number(sharedState[accId] || 0)
                );
                if (hasGcpTosFlag && hasValidModels && lastChecked > 0 && (nowSecFloat - lastChecked) < effectiveMinAge) {
                    const cachedSum = computeQuotaSummary(cachedDetail, null);
                    return {
                        id: cachedDetail.id,
                        email: cachedDetail.email,
                        name: cleanNameFromAccount(cachedDetail),
                        tier: cachedSum.tier,
                        claude_percentage: cachedSum.claude_percentage,
                        gemini_percentage: cachedSum.gemini_percentage,
                        claude_reset_time: cachedSum.claude_reset_time,
                        gemini_reset_time: cachedSum.gemini_reset_time,
                        claude_limiting_bucket: cachedSum.claude_limiting_bucket,
                        claude_weekly_percentage: cachedSum.claude_weekly_percentage,
                        claude_weekly_reset_time: cachedSum.claude_weekly_reset_time,
                        claude_5h_percentage: cachedSum.claude_5h_percentage,
                        claude_5h_reset_time: cachedSum.claude_5h_reset_time,
                        gemini_limiting_bucket: cachedSum.gemini_limiting_bucket,
                        gemini_weekly_percentage: cachedSum.gemini_weekly_percentage,
                        gemini_weekly_reset_time: cachedSum.gemini_weekly_reset_time,
                        gemini_5h_percentage: cachedSum.gemini_5h_percentage,
                        gemini_5h_reset_time: cachedSum.gemini_5h_reset_time,
                        argon_percentage: cachedSum.argon_percentage,
                        argon_reset_time: cachedSum.argon_reset_time,
                        argon_limiting_bucket: cachedSum.argon_limiting_bucket,
                        argon_weekly_percentage: cachedSum.argon_weekly_percentage,
                        argon_weekly_reset_time: cachedSum.argon_weekly_reset_time,
                        argon_5h_percentage: cachedSum.argon_5h_percentage,
                        argon_5h_reset_time: cachedSum.argon_5h_reset_time,
                        gemini_groups: cachedSum.gemini_groups,
                        quota: cachedDetail.quota || null,
                        last_updated: lastChecked,
                        quota_updated_at: lastChecked,
                        skippedByCooldown: true
                    };
                }
            }
        }

        const detail = JSON.parse(fs.readFileSync(destFile, 'utf8'));
        const tok = detail.token || (detail.token = {});
        let accTok = tok.access_token || '';
        const refTok = tok.refresh_token || '';
        const expTs = Number(tok.expiry_timestamp || 0);

        const resolvedProxyUrl = proxyUrl === undefined
            ? ((await getActiveProxy(false))?.url || null)
            : proxyUrl;

        let tokenUpdated = false;
        if ((!accTok || !expTs || nowSec > (expTs - 120)) && refTok) {
            const refRes = await refreshGoogleToken(refTok, resolvedProxyUrl);
            if (refRes) {
                accTok = refRes.access_token;
                tok.access_token = accTok;
                tok.expires_in = refRes.expires_in;
                tok.expiry_timestamp = Math.floor(Date.now() / 1000) + refRes.expires_in;
                if (refRes.id_token) tok.id_token = refRes.id_token;
                tokenUpdated = true;
            }
        }

        if (!accTok) return null;

        const cachedQ = detail.quota || {};
        const hasGcpTosFlag = typeof cachedQ.uses_gcp_tos === 'boolean';
        const cachedTier = cachedQ.subscription_tier || detail.tier || '';
        const lastLu = Number(cachedQ.last_updated || 0);
        const fetchTier = includeTier === null ? (!hasGcpTosFlag || !cachedTier || (nowSec - lastLu) > 86400) : Boolean(includeTier);
        const preferredHost = cachedQ.uses_gcp_tos ? PROD_COMPANION_HOST : DAILY_COMPANION_HOST;
        const hasRemoteFlag = typeof cachedQ.remote_quota_supported === 'boolean';
        const remoteCheckedAt = Number(cachedQ.remote_quota_checked_at || 0);
        // Never blackhole aicode remote quota for 6h on transient errors: retry every 5 minutes
        const shouldQueryRemote = hasRemoteFlag
            ? (Boolean(cachedQ.remote_quota_supported) || ((nowSec - remoteCheckedAt) > 300) || includeTier === true)
            : true;
        const skipCompanionOnPrimary = hasRemoteFlag && Boolean(cachedQ.remote_quota_supported) && hasGcpTosFlag && !fetchTier;

        for (let attempt = 0; attempt < 2; attempt++) {
            const headers = {
                'Authorization': `Bearer ${accTok}`,
                'Content-Type': 'application/json',
                'User-Agent': getIdeUserAgent()
            };

            let [qsRes, caRes, remoteRes] = await Promise.all([
                skipCompanionOnPrimary
                    ? Promise.resolve({ data: {}, code: 200 })
                    : postCompanionJson('retrieveUserQuotaSummary', headers, resolvedProxyUrl, 3500, { host: preferredHost, body: '{}' }),
                fetchTier
                    ? postCompanionJson('loadCodeAssist', headers, resolvedProxyUrl, 3500, { host: preferredHost, body: LOAD_CODE_ASSIST_BODY })
                    : Promise.resolve({ data: {}, code: 200 }),
                shouldQueryRemote
                    ? fetchRemoteQuotaSummary(headers, resolvedProxyUrl, 3500)
                    : Promise.resolve({ groups: [], data: {}, code: 0 })
            ]);

            if (attempt === 0 && refTok && ([qsRes.code, caRes.code].some((c) => c === 401 || c === 403) || remoteRes.code === 401)) {
                const refRes = await refreshGoogleToken(refTok, resolvedProxyUrl);
                if (refRes) {
                    accTok = refRes.access_token;
                    tok.access_token = accTok;
                    tok.expires_in = refRes.expires_in;
                    tok.expiry_timestamp = Math.floor(Date.now() / 1000) + refRes.expires_in;
                    if (refRes.id_token) tok.id_token = refRes.id_token;
                    tokenUpdated = true;
                    continue;
                }
            }

            if (fetchTier && (!caRes.data || Object.keys(caRes.data).length === 0)) {
                const altCaHost = preferredHost === DAILY_COMPANION_HOST ? PROD_COMPANION_HOST : DAILY_COMPANION_HOST;
                const altCa = await postCompanionJson('loadCodeAssist', headers, resolvedProxyUrl, 3500, { host: altCaHost, body: LOAD_CODE_ASSIST_BODY });
                if (attempt === 0 && refTok && (altCa.code === 401 || altCa.code === 403)) {
                    const refRes = await refreshGoogleToken(refTok, resolvedProxyUrl);
                    if (refRes) {
                        accTok = refRes.access_token;
                        tok.access_token = accTok;
                        tok.expires_in = refRes.expires_in;
                        tok.expiry_timestamp = Math.floor(Date.now() / 1000) + refRes.expires_in;
                        if (refRes.id_token) tok.id_token = refRes.id_token;
                        tokenUpdated = true;
                        continue;
                    }
                }
                if (altCa && altCa.data && Object.keys(altCa.data).length > 0) {
                    caRes = altCa;
                }
            }

            const caData = (caRes && caRes.data) || {};
            const caHasData = Boolean(caData && Object.keys(caData).length > 0);
            const usesGcpTos = caHasData
                ? Boolean(caData.paidTier && caData.paidTier.usesGcpTos === true)
                : (hasGcpTosFlag ? Boolean(cachedQ.uses_gcp_tos) : false);
            const targetHost = usesGcpTos ? PROD_COMPANION_HOST : DAILY_COMPANION_HOST;

            let qsData = (qsRes && qsRes.data) || {};
            let groups = Array.isArray(qsData.groups) ? qsData.groups : [];
            const remoteGroups = (remoteRes && Array.isArray(remoteRes.groups)) ? remoteRes.groups : [];
            const needCompanionFallback = skipCompanionOnPrimary && remoteGroups.length === 0;
            if (targetHost !== preferredHost || needCompanionFallback || (groups.length === 0 && remoteGroups.length === 0 && !hasGcpTosFlag)) {
                const retryHost = targetHost !== preferredHost
                    ? targetHost
                    : (preferredHost === DAILY_COMPANION_HOST ? PROD_COMPANION_HOST : DAILY_COMPANION_HOST);
                const retryQs = await postCompanionJson('retrieveUserQuotaSummary', headers, resolvedProxyUrl, 3500, { host: retryHost, body: '{}' });
                if (attempt === 0 && refTok && (retryQs.code === 401 || retryQs.code === 403)) {
                    const refRes = await refreshGoogleToken(refTok, resolvedProxyUrl);
                    if (refRes) {
                        accTok = refRes.access_token;
                        tok.access_token = accTok;
                        tok.expires_in = refRes.expires_in;
                        tok.expiry_timestamp = Math.floor(Date.now() / 1000) + refRes.expires_in;
                        if (refRes.id_token) tok.id_token = refRes.id_token;
                        tokenUpdated = true;
                        continue;
                    }
                }
                if (retryQs && retryQs.data && Array.isArray(retryQs.data.groups) && retryQs.data.groups.length > 0) {
                    qsData = retryQs.data;
                    groups = qsData.groups;
                }
            }

            let tierStr = cachedTier || 'PRO';
            if (caHasData) {
                const paidTier = caData.paidTier || {};
                const curTier = caData.currentTier || {};
                const paidId = String(paidTier.id || '').toLowerCase();
                const curId = String(curTier.id || '').toLowerCase();
                if (paidId.includes('ultra') || curId.includes('ultra')) tierStr = 'ULTRA';
                else if (paidId.includes('pro') || curId.includes('pro')) tierStr = 'PRO';
                else if (paidTier.name) tierStr = paidTier.name;
                else if (curId.includes('free')) tierStr = 'FREE';
                else if (curId.includes('standard')) tierStr = 'PRO';
            }

            const cachedArgonGroups = (remoteGroups.length === 0 && Array.isArray(cachedQ.quota_groups))
                ? cachedQ.quota_groups.filter((g) => {
                    if (!g || typeof g !== 'object') return false;
                    if (g.is_argon) return true;
                    const dn = String(g.displayName || '').toLowerCase();
                    if (dn.includes('argon')) return true;
                    return Array.isArray(g.buckets) && g.buckets.some((b) => String((b && b.bucketId) || '').toLowerCase().includes('argon'));
                })
                : [];
            groups = mergeQuotaGroups(
                remoteGroups.length > 0 ? remoteGroups : cachedArgonGroups,
                groups
            );
            const parsedGroups = parseQuotaGroups(groups);
            let claudePct = parsedGroups.claudePct;
            let claudeResetTime = parsedGroups.claudeReset;
            let claudeLimitingBucket = parsedGroups.claudeLimitingBucket;
            let geminiPct = parsedGroups.geminiPct;
            let geminiResetTime = parsedGroups.geminiReset;
            let geminiLimitingBucket = parsedGroups.geminiLimitingBucket;
            let argonPct = parsedGroups.argonPct;
            let argonResetTime = parsedGroups.argonReset;
            let argonLimitingBucket = parsedGroups.argonLimitingBucket;
            if (argonPct === null && remoteGroups.length === 0 && typeof cachedQ.argon_percentage === 'number') {
                argonPct = cachedQ.argon_percentage;
                argonResetTime = cachedQ.argon_reset_time || null;
                argonLimitingBucket = cachedQ.argon_limiting_bucket || null;
            }

            let modelsData = {};
            if (geminiPct === null || claudePct === null) {
                const modelsRes = await postCompanionJson('fetchAvailableModels', headers, resolvedProxyUrl, 3500, { host: targetHost, body: '{}' });
                if (attempt === 0 && refTok && (modelsRes.code === 401 || modelsRes.code === 403)) {
                    const refRes = await refreshGoogleToken(refTok, resolvedProxyUrl);
                    if (refRes) {
                        accTok = refRes.access_token;
                        tok.access_token = accTok;
                        tok.expires_in = refRes.expires_in;
                        tok.expiry_timestamp = Math.floor(Date.now() / 1000) + refRes.expires_in;
                        if (refRes.id_token) tok.id_token = refRes.id_token;
                        tokenUpdated = true;
                        continue;
                    }
                }
                modelsData = (modelsRes && modelsRes.data) || {};
                const gemModels = modelsData.models;
                if (gemModels && typeof gemModels === 'object' && !Array.isArray(gemModels)) {
                    if (argonPct === null) {
                        const argonCands = [];
                        for (const [mkey, mval] of Object.entries(gemModels)) {
                            const ml = mkey.toLowerCase();
                            if (ml.includes('argon') && mval && typeof mval === 'object') {
                                const qi = mval.quotaInfo || {};
                                if (typeof qi.remainingFraction === 'number') {
                                    argonCands.push({ rf: qi.remainingFraction, reset: qi.resetTime || null });
                                }
                            }
                        }
                        if (argonCands.length > 0) {
                            argonCands.sort((a, b) => a.rf - b.rf);
                            argonPct = Math.round(argonCands[0].rf * 100);
                            argonResetTime = argonCands[0].reset;
                        }
                    }
                    if (geminiPct === null) {
                        const gemCands = [];
                        for (const [mkey, mval] of Object.entries(gemModels)) {
                            const ml = mkey.toLowerCase();
                            if (ml.includes('gemini') && !ml.includes('argon') && mval && typeof mval === 'object' &&
                                !ml.includes('2.5') && !ml.includes('flash-lite') && !ml.includes('flash_lite') &&
                                !ml.includes('image') && !ml.includes('-low') && !ml.includes('-medium')) {
                                const qi = mval.quotaInfo || {};
                                if (typeof qi.remainingFraction === 'number') {
                                    const prio = (ml.includes('3.8') || ml.includes('3-flash')) ? 0 : 1;
                                    gemCands.push({ rf: qi.remainingFraction, prio, reset: qi.resetTime || null });
                                }
                            }
                        }
                        if (gemCands.length > 0) {
                            gemCands.sort((a, b) => (a.rf - b.rf) || (a.prio - b.prio));
                            geminiPct = Math.round(gemCands[0].rf * 100);
                            geminiResetTime = gemCands[0].reset;
                        }
                    }

                    if (claudePct === null) {
                        const clCands = [];
                        for (const [mkey, mval] of Object.entries(gemModels)) {
                            if (mkey.toLowerCase().includes('claude') && mval && typeof mval === 'object') {
                                const qi = mval.quotaInfo || {};
                                if (typeof qi.remainingFraction === 'number') {
                                    clCands.push({ rf: qi.remainingFraction, reset: qi.resetTime || null });
                                }
                            }
                        }
                        if (clCands.length > 0) {
                            clCands.sort((a, b) => a.rf - b.rf);
                            claudePct = Math.round(clCands[0].rf * 100);
                            claudeResetTime = clCands[0].reset;
                        }
                    }
                }
            }

            if (groups.length === 0 && (!modelsData || Object.keys(modelsData).length === 0)) {
                return null;
            }

            if (geminiPct === null) {
                geminiPct = cachedQ.gemini_percentage !== undefined ? Number(cachedQ.gemini_percentage) : 100;
                geminiResetTime = geminiResetTime || cachedQ.gemini_reset_time || null;
            }
            if (claudePct === null) {
                claudePct = cachedQ.claude_percentage !== undefined ? Number(cachedQ.claude_percentage) : 100;
                claudeResetTime = claudeResetTime || cachedQ.claude_reset_time || null;
            }

            const modelsList = [
                {
                    name: 'gemini-3.8-flash',
                    display_name: 'Gemini 3.8 Flash',
                    percentage: geminiPct,
                    reset_time: geminiResetTime
                },
                {
                    name: 'claude-sonnet-4-6',
                    display_name: 'Claude Sonnet 4.6 (Thinking)',
                    percentage: claudePct,
                    reset_time: claudeResetTime
                },
                {
                    name: 'claude-opus-4-6-thinking',
                    display_name: 'Claude Opus 4.6 (Thinking)',
                    percentage: claudePct,
                    reset_time: claudeResetTime
                }
            ];
            if (argonPct !== null) {
                modelsList.push({
                    name: 'gemini-4-argon',
                    display_name: 'Gemini 4 Argon',
                    percentage: argonPct,
                    reset_time: argonResetTime
                });
            }

            const checkedSec = Math.floor(Date.now() / 1000);
            if (detail.id) {
                _lastCheckedMap.set(detail.id, checkedSec);
            }
            const remoteQuotaSupported = shouldQueryRemote
                ? (remoteGroups.length > 0
                    ? true
                    : ((remoteRes && remoteRes.code >= 200) ? false : Boolean(cachedQ.remote_quota_supported ?? true)))
                : Boolean(cachedQ.remote_quota_supported);

            const quotaChanged = (
                !hasGcpTosFlag ||
                !hasRemoteFlag ||
                Boolean(cachedQ.remote_quota_supported) !== remoteQuotaSupported ||
                Boolean(cachedQ.uses_gcp_tos) !== usesGcpTos ||
                cachedQ.claude_percentage !== claudePct ||
                cachedQ.gemini_percentage !== geminiPct ||
                (cachedQ.argon_percentage ?? null) !== (argonPct ?? null) ||
                (cachedQ.subscription_tier || '') !== tierStr ||
                (cachedQ.claude_reset_time || null) !== (claudeResetTime || null) ||
                (cachedQ.gemini_reset_time || null) !== (geminiResetTime || null) ||
                (cachedQ.argon_reset_time || null) !== (argonResetTime || null) ||
                (cachedQ.claude_limiting_bucket || null) !== (claudeLimitingBucket || null) ||
                (cachedQ.gemini_limiting_bucket || null) !== (geminiLimitingBucket || null) ||
                (cachedQ.argon_limiting_bucket || null) !== (argonLimitingBucket || null) ||
                !Array.isArray(cachedQ.models) ||
                cachedQ.models.length !== modelsList.length ||
                (groups.length > 0 && JSON.stringify(cachedQ.quota_groups || []) !== JSON.stringify(groups))
            );

            if (!detail.quota) detail.quota = {};
            detail.quota.subscription_tier = tierStr;
            detail.quota.uses_gcp_tos = usesGcpTos;
            detail.quota.remote_quota_supported = remoteQuotaSupported;
            if (shouldQueryRemote || !detail.quota.remote_quota_checked_at) {
                detail.quota.remote_quota_checked_at = checkedSec;
            }
            detail.quota.models = modelsList;
            detail.quota.claude_percentage = claudePct;
            detail.quota.gemini_percentage = geminiPct;
            if (argonPct !== null) {
                detail.quota.argon_percentage = argonPct;
                detail.quota.argon_reset_time = argonResetTime;
            } else {
                delete detail.quota.argon_percentage;
                delete detail.quota.argon_reset_time;
            }
            if (groups.length > 0) detail.quota.quota_groups = groups;
            if (claudeLimitingBucket) detail.quota.claude_limiting_bucket = claudeLimitingBucket;
            else delete detail.quota.claude_limiting_bucket;
            if (geminiLimitingBucket) detail.quota.gemini_limiting_bucket = geminiLimitingBucket;
            else delete detail.quota.gemini_limiting_bucket;
            if (argonLimitingBucket) detail.quota.argon_limiting_bucket = argonLimitingBucket;
            else delete detail.quota.argon_limiting_bucket;
            detail.quota.claude_reset_time = claudeResetTime;
            detail.quota.gemini_reset_time = geminiResetTime;
            detail.quota.last_updated = checkedSec;
            detail.quota.quota_updated_at = checkedSec;

            detail.claude_percentage = claudePct;
            detail.gemini_percentage = geminiPct;
            if (argonPct !== null) {
                detail.argon_percentage = argonPct;
                detail.argon_reset_time = argonResetTime;
            } else {
                delete detail.argon_percentage;
                delete detail.argon_reset_time;
            }
            detail.tier = tierStr;

            if (tokenUpdated || quotaChanged || !cachedQ.quota_updated_at || (checkedSec - lastLu) >= 300) {
                await atomicWriteJson(destFile, detail);
            }

            if (persistPollState && detail.id) {
                await recordSharedPollTimestamps({ [detail.id]: checkedSec });
            }

            const summary = {
                id: detail.id,
                email: detail.email,
                name: cleanNameFromAccount(detail),
                tier: tierStr,
                claude_percentage: claudePct,
                gemini_percentage: geminiPct,
                claude_reset_time: claudeResetTime,
                gemini_reset_time: geminiResetTime,
                claude_limiting_bucket: claudeLimitingBucket,
                claude_weekly_percentage: parsedGroups.claudeWeeklyPct,
                claude_weekly_reset_time: parsedGroups.claudeWeeklyReset,
                claude_5h_percentage: parsedGroups.claude5hPct,
                claude_5h_reset_time: parsedGroups.claude5hReset,
                gemini_limiting_bucket: geminiLimitingBucket,
                gemini_weekly_percentage: parsedGroups.geminiWeeklyPct,
                gemini_weekly_reset_time: parsedGroups.geminiWeeklyReset,
                gemini_5h_percentage: parsedGroups.gemini5hPct,
                gemini_5h_reset_time: parsedGroups.gemini5hReset,
                argon_percentage: argonPct,
                argon_reset_time: argonResetTime,
                argon_limiting_bucket: argonLimitingBucket,
                argon_weekly_percentage: parsedGroups.argonWeeklyPct,
                argon_weekly_reset_time: parsedGroups.argonWeeklyReset,
                argon_5h_percentage: parsedGroups.argon5hPct,
                argon_5h_reset_time: parsedGroups.argon5hReset,
                gemini_groups: parsedGroups.geminiGroups,
                quota: detail.quota,
                last_updated: checkedSec,
                quota_updated_at: checkedSec
            };

            if (updateIndex && detail.id && (quotaChanged || (checkedSec - lastLu) >= 300)) {
                const map = new Map();
                map.set(detail.id, summary);
                await updateAccountsIndexBatch(map);
            }

            return summary;
        }
    } catch (_) {}
    return null;
}

async function refreshActive({ appTarget = 'ide', forceTier = false, force = false, minAgeSec = undefined, syncWinCred = true } = {}) {
    if (syncWinCred !== false) {
        await syncLiveAuth({ appTarget, force, minAgeSec: force ? 0 : 15 }).catch(() => null);
    }
    const st = loadState({ appTarget });
    const act = st.activeAccount;
    if (!act || !act.id) return act || { id: null, name: 'Default', email: '' };
    const effectiveMinAge = minAgeSec !== undefined ? Number(minAgeSec) : (force ? 0 : 12);
    const proxy = await getActiveProxy(false);
    const updated = await fetchLiveQuota(act.id, {
        updateIndex: true,
        includeTier: forceTier ? true : null,
        proxyUrl: proxy ? proxy.url : null,
        minAgeSec: effectiveMinAge,
        force
    });
    if (updated) {
        return {
            ...act,
            ...updated
        };
    }
    return act;
}

async function refreshAll({ appTarget = 'ide', concurrency = 8, force = false, minAgeSec = undefined, syncWinCred = true } = {}) {
    if (syncWinCred !== false) {
        await syncLiveAuth({ appTarget, force, minAgeSec: force ? 0 : 15 }).catch(() => null);
    }
    if (!fs.existsSync(ACCOUNTS_DIR)) return [];
    const files = fs.readdirSync(ACCOUNTS_DIR)
        .filter((f) => f.endsWith('.json'))
        .map((f) => path.join(ACCOUNTS_DIR, f));
    if (files.length === 0) return [];

    const effectiveMinAge = minAgeSec !== undefined ? Number(minAgeSec) : (force ? 0 : 180);
    const proxy = await getActiveProxy(false);
    const proxyUrl = proxy ? proxy.url : null;
    const results = [];
    const polledTimestamps = {};
    let cursor = 0;
    const workers = Array.from({ length: Math.min(concurrency, files.length) }, async () => {
        while (cursor < files.length) {
            const idx = cursor++;
            const res = await fetchLiveQuota(files[idx], {
                updateIndex: false,
                proxyUrl,
                minAgeSec: effectiveMinAge,
                persistPollState: false
            });
            if (res) {
                results.push(res);
                if (res.id && !res.skippedByCooldown && res.last_updated) {
                    polledTimestamps[res.id] = res.last_updated;
                }
            }
        }
    });
    await Promise.all(workers);

    if (Object.keys(polledTimestamps).length > 0) {
        await recordSharedPollTimestamps(polledTimestamps);
    }

    const updatesMap = new Map();
    for (const r of results) {
        if (r && r.id && !r.skippedByCooldown) updatesMap.set(r.id, r);
    }
    if (updatesMap.size > 0) {
        await updateAccountsIndexBatch(updatesMap);
    }
    return results;
}

module.exports = {
    DAILY_COMPANION_HOST,
    PROD_COMPANION_HOST,
    LOAD_CODE_ASSIST_BODY,
    COUNTRY_NAMES,
    getIdeUserAgent,
    getCountryName,
    getActiveProxy,
    syncProxyEnvironment,
    flushDnsCache,
    resetConnectionPool,
    getLastKnownNetwork,
    detectNetwork,
    parseQuotaGroups,
    mergeQuotaGroups,
    AICODE_REMOTE_QUOTA_URL,
    computeQuotaSummary,
    detectLiveIdeAuth,
    detectLiveStandaloneAuth,
    detectLiveGeminiAuth: detectLiveIdeAuth,
    syncLiveAuth,
    getPythonCandidates,
    resolvePythonCommand,
    loadState,
    getOAuthClients,
    discoverOAuthClientsFromBinaries,
    fetchLiveQuota,
    refreshActive,
    refreshAll,
    // 429 Failover Engine exports
    MODEL_TIERS,
    getModelTier,
    getModelPoolName,
    getModelPoolQuota,
    getBestAccountsForModel,
    resolveQuotaExhaustionPlan,
    // Model config system (user-configurable, Delegator-compatible)
    loadModelConfig,
    saveModelConfig,
    getOrchestratorOrder,
    getWorkerModel,
    loadAssistantSettings,
    saveAssistantSettings
};


