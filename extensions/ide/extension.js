/**
 * ag-toolkit -- Antigravity Assistant (Account Switcher & Quota Monitor)
 * Extension for Antigravity IDE (VS Code fork).
 *
 * Capabilities:
 *  1. Real active account detection & seamless switching between accounts with Google OAuth refresh.
 *  2. Live ping & country status in Status Bar.
 *  3. Account avatars displayed in switcher menu and tooltip.
 *  4. Detailed quota monitor for Claude, Gemini (High mode) and subscription tier.
 */

const vscode = require('vscode');
const { exec, execFile, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');
const http = require('http');
const url = require('url');

// ---------------------------------------------------------------------------
// Swarm IPC state
// ---------------------------------------------------------------------------
let _swarmProcess = null;         // ChildProcess | null
let _swarmTaskId = null;          // current task_id
let _swarmStatusBar = null;       // StatusBarItem for swarm progress (priority 151)
let _swarmWorkerCallbacks = {};   // worker_name → {resolve, reject}
let _swarmOutputChannel = null;   // OutputChannel for Swarm events
let _swarmPollTimer = null;       // Checkpoint polling timer
let _swarmBridgeRef = null;       // Cached shared swarm_bridge.js module
let _swarmTickTimer = null;       // Adaptive live ticker for status bar (1s active / 15s idle fallback)
let _swarmTickRunning = false;
let _swarmLastActive = 0;         // Last observed active_agents count
let _lastSwarmPillText = null;    // Dirty-check cache for status bar text (anti-flicker)
let _lastSwarmTooltipMarkdown = null; // Dirty-check cache for hover tooltip (anti-flicker)
let _swarmRecentCompletionState = null; // { project_name, task_id, done_workers, total_workers, duration_seconds, failed, timestamp }
let _swarmActiveTaskIds = new Map(); // task_id -> { project_name, started_at_ms, done_workers, total_workers }
let _swarmKnownCompletedTaskIds = new Set();
let _swarmPreviousSnapshot = null;

let statusBarItem;
let checkTimer = null;
let activeQuotaTimer = null;
let quotaTimer = null;
let lastKnownAccountData = null;
let lastConnectionInfo = { ok: false, ms: 0, countryCode: '', status: 'Проверка...' };
let lastProxyInfo = null;
let lastNetCheckAt = 0;
let netCheckPromise = null;
let netCheckSeq = 0;
let lastRenderedBarSig = '';
let cachedSvgIcons = null;
let isRefreshingQuotas = false;
let allRefreshPromise = null;
let activeRefreshPromise = null;
let activeRefreshIsForced = false;
let lastActiveRefreshAt = 0;
let lastAllRefreshAt = 0;
let lastEnsuredEmail = '';
let onboardingInFlightEmail = '';
let convoWatchers = [];
let convoDebounceTimer = null;
let pendingConvoRefresh = false;
let accountsWatcher = null;
let accountsDebounceTimer = null;
let agToolsWatcher = null;
let swarmDirWatcher = null;
let swarmWatchDebounceTimer = null;
let _swarmCheckpointWakeFn = null;
const agyRestartTimestamps = [];
let lastAgyRestartAt = 0;
let lastNetWasOk = null;
let lastAppliedProxyUrl = undefined;
let lastAppliedCountry = '';
let lastAppliedRefreshAt = 0;
const NET_CACHE_TTL_MS = 45000;

// ---------------------------------------------------------------------------
// 429 Failover Engine state
// ---------------------------------------------------------------------------
let _failoverToastLastShownAt = 0;    // throttle: max 1 toast per 60 s
let _failoverLogWatchers = [];         // fs.watch handles on LS log dirs
let _failoverLogDebounceTimer = null;
let _failoverLogPollTimer = null;      // periodic fallback poll (Windows fs.watch buffers)
let _startupUpdateTimer = null;
let _startupChecksumTimer = null;
let _startupLogWatchTimer = null;
let _startupActiveQuotaTimer = null;
let _startupAllQuotasTimer = null;
let _failoverLogLastOffset = new Map(); // path -> bytesRead (avoid re-scanning)

// Paths
const homeDir = os.homedir();
const agToolsDir = process.env.AG_TOOLS_DIR || path.join(homeDir, '.antigravity_tools');
const geminiDir = process.env.AG_GEMINI_DIR || path.join(homeDir, '.gemini');
const standaloneProfilesDir = path.join(homeDir, '.antigravity-profiles');
const appDataDir = process.env.APPDATA || path.join(homeDir, 'AppData', 'Roaming');
const ideGlobalStorageDir = path.join(appDataDir, 'Antigravity IDE', 'User', 'globalStorage');

function _resolveSharedCoreCandidates(filename) {
    const toolsRoot = process.env.AG_TOOLS_DIR || agToolsDir;
    const shared = path.join(toolsRoot, filename);
    const local = path.join(__dirname, filename);
    const devCore = path.resolve(__dirname, '..', 'core', filename);
    const candidates = [];
    if (fs.existsSync(local) && fs.existsSync(shared)) {
        try {
            const localMs = fs.statSync(local).mtimeMs;
            const sharedMs = fs.statSync(shared).mtimeMs;
            candidates.push(sharedMs > localMs ? shared : local, sharedMs > localMs ? local : shared);
        } catch (_) {
            candidates.push(local, shared);
        }
    } else {
        if (fs.existsSync(local)) candidates.push(local);
        if (fs.existsSync(shared)) candidates.push(shared);
    }
    if (fs.existsSync(devCore) && !candidates.includes(devCore)) {
        candidates.push(devCore);
    }
    return candidates;
}

let _quotaEngineRef = null;
function getQuotaEngine() {
    if (_quotaEngineRef) return _quotaEngineRef;
    for (const p of _resolveSharedCoreCandidates('quota_engine.js')) {
        try {
            _quotaEngineRef = require(p);
            return _quotaEngineRef;
        } catch (_) {}
    }
    return null;
}

function getSwarmBridge() {
    if (_swarmBridgeRef) return _swarmBridgeRef;
    for (const p of _resolveSharedCoreCandidates('swarm_bridge.js')) {
        try {
            _swarmBridgeRef = require(p);
            return _swarmBridgeRef;
        } catch (_) {}
    }
    return null;
}

let _errorGuardianRef = null;
function getErrorGuardian() {
    if (_errorGuardianRef) return _errorGuardianRef;
    for (const p of _resolveSharedCoreCandidates('error_guardian.js')) {
        try {
            _errorGuardianRef = require(p);
            return _errorGuardianRef;
        } catch (_) {}
    }
    return null;
}

function getSwitcherScriptPath() {
    const shared = path.join(agToolsDir, 'account_switcher.py');
    const local = path.join(__dirname, 'account_switcher.py');
    if (fs.existsSync(shared) && fs.existsSync(local)) {
        try {
            return fs.statSync(shared).mtimeMs > fs.statSync(local).mtimeMs ? shared : local;
        } catch (e) {}
    }
    return fs.existsSync(local) ? local : shared;
}

function isWindowFocused() {
    return !vscode.window.state || vscode.window.state.focused !== false;
}

/**
 * Extension activation
 */
function activate(context) {
    if (!process.env.NO_PROXY) {
        process.env.NO_PROXY = 'localhost,127.0.0.1,::1';
    }

    // Ensure profile directories exist
    if (!fs.existsSync(standaloneProfilesDir)) {
        try { fs.mkdirSync(standaloneProfilesDir, { recursive: true }); } catch (e) {}
    }

    // Create Status Bar item (Right side)
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 150);
    statusBarItem.command = 'agToolkit.openMenu';
    statusBarItem.name = 'Antigravity Assistant';
    context.subscriptions.push(statusBarItem);

    // Swarm Status Bar item strictly to the LEFT of the account pill (priority 151 > 150)
    const swarmAlign = (vscode.StatusBarAlignment && vscode.StatusBarAlignment.Right) ? vscode.StatusBarAlignment.Right : 2;
    _swarmStatusBar = vscode.window.createStatusBarItem(swarmAlign, 151);
    _swarmStatusBar.command = 'agToolkit.resumeSwarm';
    _swarmStatusBar.name = 'Antigravity Swarm';
    _swarmStatusBar.text = '⚡ Агенты: 0';
    _swarmStatusBar.tooltip = '⚡ Swarm Engine — Нажмите для управления роем';
    context.subscriptions.push(_swarmStatusBar);
    try { _swarmStatusBar.show(); } catch (_) {}
    startSwarmLiveTicker();

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('agToolkit.openMenu', showMainMenu),
        vscode.commands.registerCommand('agToolkit.switchAccount', showAccountSwitchMenu),
        vscode.commands.registerCommand('agToolkit.showQuota', showQuotaDetails),
        vscode.commands.registerCommand('agToolkit.refreshQuotas', refreshQuotasOnline),
        vscode.commands.registerCommand('agToolkit.refreshNetwork', refreshNetworkConnection),
        vscode.commands.registerCommand('agToolkit.openLogs', openAntigravityLogs),
        vscode.commands.registerCommand('agToolkit.checkUpdate', checkCloudUpdates),
        vscode.commands.registerCommand('agToolkit.set429Policy', show429PolicyMenu),
        vscode.commands.registerCommand('agToolkit.manageModels', showModelConfigMenu),
        vscode.commands.registerCommand('agToolkit.runSwarm', runSwarmCommand),
        vscode.commands.registerCommand('agToolkit.resumeSwarm', resumeSwarmCommand),
        vscode.commands.registerCommand('agToolkit.stopSwarm', stopSwarmCommand)
    );

    // Initial async sync and UI render (non-blocking)
    syncProxyEnvironment(false).catch(() => {});
    updateStatusBar();
    tryRestorePreviousConversation();

    const intervalSec = vscode.workspace.getConfiguration('agToolkit').get('checkIntervalSeconds') || 15;
    checkTimer = setInterval(() => {
        if (isWindowFocused()) {
            updateStatusBar();
        }
    }, Math.max(10, intervalSec) * 1000);
    if (typeof checkTimer.unref === 'function') checkTimer.unref();

    // Automatic non-intrusive cloud update check on startup
    checkForUpdatesOnStartup();

    // Background quota refreshes using pure-Node Keep-Alive engine with cross-process cooldown
    _startupActiveQuotaTimer = setTimeout(() => {
        refreshActiveQuotaBackground({ minAgeSec: 12, ignoreFocus: true });
    }, 600);
    if (typeof _startupActiveQuotaTimer.unref === 'function') _startupActiveQuotaTimer.unref();
    _startupAllQuotasTimer = setTimeout(() => {
        refreshQuotasBackground({ minAgeSec: 90, ignoreFocus: true });
    }, 2500);
    if (typeof _startupAllQuotasTimer.unref === 'function') _startupAllQuotasTimer.unref();

    activeQuotaTimer = setInterval(() => {
        if (isWindowFocused()) {
            refreshActiveQuotaBackground({ minAgeSec: 12 });
        }
    }, 15 * 1000);
    if (typeof activeQuotaTimer.unref === 'function') activeQuotaTimer.unref();

    quotaTimer = setInterval(() => {
        if (isWindowFocused()) {
            refreshQuotasBackground({ minAgeSec: 180 });
        }
    }, 180 * 1000);
    if (typeof quotaTimer.unref === 'function') quotaTimer.unref();

    setupFileWatchers();

    // Instant refresh when IDE window regains focus (or flush pending conversation turn update)
    context.subscriptions.push(
        vscode.window.onDidChangeWindowState((e) => {
            if (e && e.focused) {
                updateStatusBar({ skipNetwork: true });
                if (pendingConvoRefresh) {
                    pendingConvoRefresh = false;
                    refreshActiveQuotaBackground({ force: false, minAgeSec: 5 });
                } else {
                    refreshActiveQuotaBackground({ minAgeSec: 12 });
                }
            }
        })
    );

    // Defer checksum verification off startup critical path (non-blocking async I/O)
    _startupChecksumTimer = setTimeout(() => {
        repairProductChecksums().catch(() => {});
    }, 3500);
    if (typeof _startupChecksumTimer.unref === 'function') _startupChecksumTimer.unref();

    // 429 Failover Engine & Scoped IDE Error Guardian
    _startupLogWatchTimer = setTimeout(() => {
        setup429LogWatchers();
        setupIdeErrorGuardian();
    }, 4000);
    if (typeof _startupLogWatchTimer.unref === 'function') _startupLogWatchTimer.unref();

    context.subscriptions.push({
        dispose: () => {
            deactivate();
        }
    });
}

function scheduleConvoQuotaRefresh(delayMs = 2500) {
    if (!isWindowFocused()) {
        pendingConvoRefresh = true;
        return;
    }
    if (convoDebounceTimer) clearTimeout(convoDebounceTimer);
    convoDebounceTimer = setTimeout(() => {
        convoDebounceTimer = null;
        pendingConvoRefresh = false;
        refreshActiveQuotaBackground({ force: false, minAgeSec: 5 });
    }, delayMs);
}

function setupFileWatchers() {
    disposeFileWatchers();
    const convoDirs = [
        path.join(geminiDir, 'antigravity', 'conversations'),
        path.join(geminiDir, 'antigravity-ide', 'conversations')
    ];
    for (const dir of convoDirs) {
        try {
            if (!fs.existsSync(dir)) continue;
            const w = fs.watch(dir, { persistent: false }, (_eventType, filename) => {
                if (filename && String(filename).endsWith('.tmp')) return;
                scheduleConvoQuotaRefresh(2500);
            });
            w.on('error', () => {});
            convoWatchers.push(w);
        } catch (_) {}
    }

    const accountsDir = path.join(agToolsDir, 'accounts');
    try {
        if (fs.existsSync(accountsDir)) {
            accountsWatcher = fs.watch(accountsDir, { persistent: false }, (_eventType, filename) => {
                if (filename && String(filename).endsWith('.tmp')) return;
                if (accountsDebounceTimer) clearTimeout(accountsDebounceTimer);
                accountsDebounceTimer = setTimeout(() => {
                    accountsDebounceTimer = null;
                    updateStatusBar({ skipNetwork: true });
                }, 350);
            });
            accountsWatcher.on('error', () => {});
        }
    } catch (_) {}

    const triggerSwarmWakeup = () => {
        if (swarmWatchDebounceTimer) clearTimeout(swarmWatchDebounceTimer);
        swarmWatchDebounceTimer = setTimeout(() => {
            swarmWatchDebounceTimer = null;
            updateSwarmStatusBarLive();
            if (typeof _swarmCheckpointWakeFn === 'function') {
                try { _swarmCheckpointWakeFn(); } catch (_) {}
            }
            if (_swarmTickRunning) {
                _scheduleSwarmLiveTick();
            }
        }, 200);
    };

    const attachSwarmDirWatcher = () => {
        if (swarmDirWatcher) return;
        const swarmDir = path.join(agToolsDir, 'swarm');
        try {
            if (!fs.existsSync(swarmDir)) return;
            const onChange = (_ev, filename) => {
                if (filename && String(filename).endsWith('.tmp')) return;
                triggerSwarmWakeup();
            };
            try {
                swarmDirWatcher = fs.watch(swarmDir, { recursive: true, persistent: false }, onChange);
            } catch (_) {
                swarmDirWatcher = fs.watch(swarmDir, { persistent: false }, onChange);
            }
            swarmDirWatcher.on('error', () => {});
        } catch (_) {}
    };

    attachSwarmDirWatcher();

    try {
        if (fs.existsSync(agToolsDir)) {
            agToolsWatcher = fs.watch(agToolsDir, { persistent: false }, (_ev, filename) => {
                const fname = filename ? String(filename) : '';
                if (!fname || fname.endsWith('.tmp')) return;
                if (fname === 'accounts.json' || fname === 'live_auth.json' || fname === 'quota_poll_state.json') {
                    if (accountsDebounceTimer) clearTimeout(accountsDebounceTimer);
                    accountsDebounceTimer = setTimeout(() => {
                        accountsDebounceTimer = null;
                        updateStatusBar({ skipNetwork: true });
                    }, 350);
                } else if (fname === 'swarm') {
                    attachSwarmDirWatcher();
                    triggerSwarmWakeup();
                }
            });
            agToolsWatcher.on('error', () => {});
        }
    } catch (_) {}
}

function disposeFileWatchers() {
    if (convoDebounceTimer) {
        clearTimeout(convoDebounceTimer);
        convoDebounceTimer = null;
    }
    if (accountsDebounceTimer) {
        clearTimeout(accountsDebounceTimer);
        accountsDebounceTimer = null;
    }
    if (swarmWatchDebounceTimer) {
        clearTimeout(swarmWatchDebounceTimer);
        swarmWatchDebounceTimer = null;
    }
    for (const w of convoWatchers) {
        try { w.close(); } catch (_) {}
    }
    convoWatchers = [];
    if (accountsWatcher) {
        try { accountsWatcher.close(); } catch (_) {}
        accountsWatcher = null;
    }
    if (agToolsWatcher) {
        try { agToolsWatcher.close(); } catch (_) {}
        agToolsWatcher = null;
    }
    if (swarmDirWatcher) {
        try { swarmDirWatcher.close(); } catch (_) {}
        swarmDirWatcher = null;
    }
}

/**
 * Automatically synchronize product.json checksums to eliminate
 * the 'Your Antigravity IDE installation appears to be corrupt' notification banner.
 */
async function repairProductChecksums() {
    try {
        const crypto = require('crypto');
        const fsp = fs.promises;
        const localApp = process.env.LOCALAPPDATA || path.join(homeDir, 'AppData', 'Local');
        const candidateRoots = [
            path.join(localApp, 'Programs', 'Antigravity IDE'),
            'C:\\Program Files\\Antigravity IDE'
        ];
        for (const root of candidateRoots) {
            const appDir = path.join(root, 'resources', 'app');
            const pJsonPath = path.join(appDir, 'product.json');
            let pJson = null;
            try {
                pJson = JSON.parse(await fsp.readFile(pJsonPath, 'utf8'));
            } catch (_) {
                continue;
            }
            if (!pJson || !pJson.checksums || typeof pJson.checksums !== 'object') continue;

            let modified = false;
            for (const rel of Object.keys(pJson.checksums)) {
                let buf = null;
                try {
                    buf = await fsp.readFile(path.join(appDir, 'out', rel));
                } catch (_) {
                    try {
                        buf = await fsp.readFile(path.join(appDir, rel));
                    } catch (_) {}
                }
                if (buf) {
                    const hash = crypto.createHash('sha256').update(buf).digest('base64').replace(/=+$/, '');
                    if (hash !== pJson.checksums[rel]) {
                        pJson.checksums[rel] = hash;
                        modified = true;
                    }
                }
            }
            if (modified) {
                await fsp.writeFile(pJsonPath, JSON.stringify(pJson, null, '\t'), 'utf8');
            }
        }
    } catch (e) {}
}

/**
 * Detect the currently active conversation ID from the latest modified brain transcript
 */
function getActiveConversationId() {
    try {
        // Strictly look ONLY in IDE brain (antigravity-ide/brain). Never look in antigravity/brain (Antigravity 2.0).
        const ideBrainDir = path.join(geminiDir, 'antigravity-ide', 'brain');
        if (!fs.existsSync(ideBrainDir)) return null;

        const currentWorkspace = (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0)
            ? vscode.workspace.workspaceFolders[0].uri.fsPath.toLowerCase()
            : null;

        let latestId = null;
        let latestTime = 0;
        let latestMatchedWorkspaceId = null;
        let latestMatchedTime = 0;

        const entries = fs.readdirSync(ideBrainDir, { withFileTypes: true });
        for (const ent of entries) {
            if (!ent.isDirectory()) continue;
            const logPath = path.join(ideBrainDir, ent.name, '.system_generated', 'logs', 'transcript.jsonl');
            if (fs.existsSync(logPath)) {
                const stat = fs.statSync(logPath);
                if (stat.mtimeMs > latestTime) {
                    latestTime = stat.mtimeMs;
                    latestId = ent.name;
                }
                if (currentWorkspace && stat.mtimeMs > latestMatchedTime) {
                    try {
                        const fd = fs.openSync(logPath, 'r');
                        const buf = Buffer.alloc(4096);
                        const bytesRead = fs.readSync(fd, buf, 0, 4096, 0);
                        fs.closeSync(fd);
                        const head = buf.toString('utf8', 0, bytesRead).toLowerCase();
                        if (head.includes(currentWorkspace)) {
                            latestMatchedTime = stat.mtimeMs;
                            latestMatchedWorkspaceId = ent.name;
                        }
                    } catch (_) {}
                }
            }
        }
        return latestMatchedWorkspaceId || latestId;
    } catch (e) {
        return null;
    }
}

/**
 * Check if a previous conversation was active prior to account switch, and restore it into the agent panel.
 * If restore_conversation.json contains a resume_message (set during 429 account hop),
 * that message is injected into the chat after the conversation is opened so the agent
 * automatically picks up where it left off.
 */
function tryRestorePreviousConversation() {
    try {
        const restoreFile = path.join(agToolsDir, 'restore_conversation.json');
        if (!fs.existsSync(restoreFile)) return;

        let data = null;
        try {
            const raw = fs.readFileSync(restoreFile, 'utf8');
            data = JSON.parse(raw);
        } catch (e) {}

        // Consume file immediately so it only executes once
        try { fs.unlinkSync(restoreFile); } catch (e) {}

        if (data && data.cascadeId && (Date.now() - (data.timestamp || 0) < 600000)) {
            const targetId = data.cascadeId;
            const resumeMsg = (data.resume_message || '').trim();
            const resumeReason = data.resume_reason || '';
            console.log(`[ag-toolkit] Restoring previous conversation: ${targetId}${resumeMsg ? ' (with resume message)' : ''}`);

            // Primary attempt after 1.5s
            setTimeout(async () => {
                try {
                    await vscode.commands.executeCommand('antigravity.openConversation', targetId);
                    console.log(`[ag-toolkit] Restored active conversation: ${targetId}`);
                } catch (e) {
                    console.log('[ag-toolkit] Primary restore attempt deferred:', e.message);
                }
            }, 1500);

            // Safeguard attempt after 3.5s
            setTimeout(async () => {
                try {
                    await vscode.commands.executeCommand('antigravity.openConversation', targetId);
                } catch (e) {}
            }, 3500);

            // Final retry after 6s
            setTimeout(async () => {
                try {
                    await vscode.commands.executeCommand('antigravity.openConversation', targetId);
                } catch (e) {}
            }, 6000);

            // Resume injection: after conversation is open, send the resume message so the agent
            // knows to continue the interrupted task. Delayed 6.5s to ensure the conversation
            // panel is fully loaded and the new account is authenticated.
            if (resumeMsg) {
                const isAutoResume = Boolean(data.auto_send_resume || resumeReason === '429_account_hop');
                const modelNote = data.model ? ` (${data.model})` : '';
                setTimeout(async () => {
                    try {
                        if (isAutoResume) {
                            let sent = await dispatchPromptToChatPanel(resumeMsg);
                            if (!sent) {
                                await new Promise((r) => setTimeout(r, 2500));
                                sent = await dispatchPromptToChatPanel(resumeMsg);
                            }
                            if (sent) {
                                console.log('[ag-toolkit] Auto-resume message injected into chat.');
                                vscode.window.showInformationMessage(
                                    `⏩ Автосмена аккаунта выполнена${modelNote}: отправлено "${resumeMsg}".`
                                );
                            } else {
                                try {
                                    await vscode.env.clipboard.writeText(resumeMsg);
                                } catch (_) {}
                                vscode.window.showInformationMessage(
                                    `⏩ Аккаунт переключён${modelNote}. Вставьте в чат "${resumeMsg}" (уже скопировано).`
                                );
                            }
                            return;
                        }

                        // Manual switch toast confirmation
                        const reason = resumeReason === '429_account_switch'
                            ? 'аккаунт сменён'
                            : 'восстановление после перезапуска';
                        const choice = await vscode.window.showInformationMessage(
                            `⏩ Продолжить прерванную задачу${modelNote}? (${reason})`,
                            'Да, продолжить',
                            'Нет'
                        );
                        if (choice === 'Да, продолжить') {
                            const sent = await dispatchPromptToChatPanel(resumeMsg);
                            if (sent) {
                                console.log('[ag-toolkit] Resume message injected into chat.');
                            } else {
                                try {
                                    await vscode.env.clipboard.writeText(resumeMsg);
                                } catch (_) {}
                                vscode.window.showInformationMessage(
                                    `Введи в чат: "${resumeMsg}" (уже скопировано в буфер обмена)`
                                );
                            }
                        }
                    } catch (e) {
                        console.log('[ag-toolkit] Resume injection error:', e.message);
                    }
                }, isAutoResume ? 6500 : 7000);
            }
        }
    } catch (e) {
        console.error('[ag-toolkit] Error restoring conversation:', e);
    }
}

/**
 * Read system proxy asynchronously from Windows Registry via quota_engine or reg.exe
 */
function execFileAsync(bin, args, timeoutMs = 2000) {
    return new Promise((resolve) => {
        execFile(bin, args, { encoding: 'utf8', timeout: timeoutMs, windowsHide: true }, (err, stdout) => {
            resolve(err ? null : (stdout || ''));
        });
    });
}

async function getActiveProxyAsync(forceRefresh = false) {
    const eng = getQuotaEngine();
    if (eng && typeof eng.getActiveProxy === 'function') {
        const proxy = await eng.getActiveProxy(forceRefresh);
        lastProxyInfo = proxy || null;
        return proxy;
    }

    let result = null;
    if (process.platform === 'win32') {
        const regKey = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings';
        const regOut = await execFileAsync('reg', ['query', regKey], 1800);
        if (regOut) {
            const enableMatch = regOut.match(/ProxyEnable\s+REG_DWORD\s+(0x[0-9a-f]+|\d+)/i);
            const enabled = enableMatch ? (parseInt(enableMatch[1], enableMatch[1].startsWith('0x') ? 16 : 10) === 1) : false;
            const m = enabled ? regOut.match(/ProxyServer\s+REG_SZ\s+([^\r\n]+)/i) : null;
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
                    result = { url: raw, source: 'Windows Registry (Auto)' };
                }
            }
        }
    }
    lastProxyInfo = result;
    return result;
}

/**
 * Apply proxy into current process environment without blocking Extension Host
 */
async function syncProxyEnvironment(forceRefresh = false) {
    const eng = getQuotaEngine();
    let proxy = null;
    if (eng && typeof eng.syncProxyEnvironment === 'function') {
        proxy = await eng.syncProxyEnvironment(forceRefresh);
    } else {
        proxy = await getActiveProxyAsync(forceRefresh);
        if (proxy && proxy.url) {
            process.env.HTTP_PROXY = proxy.url;
            process.env.http_proxy = proxy.url;
            process.env.HTTPS_PROXY = proxy.url;
            process.env.https_proxy = proxy.url;
            process.env.ALL_PROXY = proxy.url;
            process.env.all_proxy = proxy.url;
        } else {
            delete process.env.HTTP_PROXY;
            delete process.env.http_proxy;
            delete process.env.HTTPS_PROXY;
            delete process.env.https_proxy;
            delete process.env.ALL_PROXY;
            delete process.env.all_proxy;
        }
        process.env.NO_PROXY = 'localhost,127.0.0.1,::1';
        process.env.no_proxy = 'localhost,127.0.0.1,::1';
    }
    lastProxyInfo = proxy || null;
    return proxy;
}

const countryNames = {
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

function getCountryInfo(code) {
    if (!code) return { name: 'Онлайн' };
    const upper = code.toUpperCase();
    const eng = getQuotaEngine();
    const name = (eng && typeof eng.getCountryName === 'function')
        ? (eng.getCountryName(upper) || 'Онлайн')
        : (countryNames[upper] || 'Онлайн');
    return { name };
}

/**
 * Fallback direct/proxy probe when quota_engine.js is unavailable
 */
function checkConnectionAndCountry(proxyUrl) {
    return new Promise((resolve) => {
        const start = Date.now();
        const options = {
            timeout: 3500,
            rejectUnauthorized: false
        };

        const parseResult = (data) => {
            const ms = Date.now() - start;
            const locMatch = data.match(/loc=([A-Z]{2})/);
            const countryCode = locMatch ? locMatch[1] : '';
            resolve({
                ok: true,
                ms,
                countryCode,
                status: `${ms} ms`,
                proxyUrl: proxyUrl || null
            });
        };

        if (proxyUrl && proxyUrl.startsWith('http://')) {
            const parsedProxy = url.parse(proxyUrl);
            const proxyReq = http.request({
                host: parsedProxy.hostname,
                port: parsedProxy.port || 80,
                method: 'CONNECT',
                path: 'cloudflare.com:443',
                timeout: 3500
            });

            proxyReq.on('connect', (res, socket) => {
                if (res.statusCode !== 200) {
                    try { socket.destroy(); } catch (_) {}
                    resolve({ ok: false, ms: 0, countryCode: '', status: 'Ошибка прокси', proxyUrl });
                    return;
                }
                const tlsSocket = https.get({
                    host: 'cloudflare.com',
                    path: '/cdn-cgi/trace',
                    socket: socket,
                    agent: false,
                    timeout: 3500,
                    rejectUnauthorized: false
                }, (r) => {
                    let body = '';
                    r.on('data', chunk => body += chunk);
                    r.on('end', () => {
                        try { socket.destroy(); } catch (_) {}
                        parseResult(body);
                    });
                });

                tlsSocket.on('error', () => {
                    try { socket.destroy(); } catch (_) {}
                    resolve({ ok: false, ms: 0, countryCode: '', status: 'Ошибка прокси', proxyUrl });
                });
            });

            proxyReq.on('timeout', () => {
                proxyReq.destroy();
                resolve({ ok: false, ms: 0, countryCode: '', status: 'Таймаут прокси', proxyUrl });
            });

            proxyReq.on('error', () => {
                resolve({ ok: false, ms: 0, countryCode: '', status: 'Прокси офлайн', proxyUrl });
            });

            proxyReq.end();
            return;
        }

        const req = https.get('https://cloudflare.com/cdn-cgi/trace', options, (res) => {
            let body = '';
            res.on('data', chunk => body += chunk);
            res.on('end', () => parseResult(body));
        });

        req.on('timeout', () => {
            req.destroy();
            resolve({ ok: false, ms: 0, countryCode: '', status: 'Таймаут', proxyUrl: null });
        });

        req.on('error', () => {
            resolve({ ok: false, ms: 0, countryCode: '', status: 'Офлайн', proxyUrl: null });
        });
    });
}

async function probeNetwork(forceRefresh = false) {
    if (!forceRefresh && netCheckPromise) {
        return netCheckPromise;
    }

    const reqSeq = ++netCheckSeq;
    const work = (async () => {
        const eng = getQuotaEngine();
        let conn;
        if (eng && typeof eng.detectNetwork === 'function') {
            conn = await eng.detectNetwork({
                forceProxyRefresh: forceRefresh,
                maxAgeMs: forceRefresh ? 0 : NET_CACHE_TTL_MS
            });
            if (reqSeq === netCheckSeq || !lastNetCheckAt) {
                lastProxyInfo = conn && conn.proxyUrl ? { url: conn.proxyUrl, source: 'Windows Registry' } : null;
            }
        } else {
            const proxy = await syncProxyEnvironment(forceRefresh);
            conn = await checkConnectionAndCountry(proxy ? proxy.url : null);
        }
        if (reqSeq === netCheckSeq || !lastNetCheckAt) {
            lastConnectionInfo = conn;
            lastNetCheckAt = Date.now();
            return conn;
        }
        return lastConnectionInfo;
    })().finally(() => {
        if (netCheckPromise === work) {
            netCheckPromise = null;
        }
    });

    if (!forceRefresh) {
        netCheckPromise = work;
    }
    return work;
}

function getCleanPythonEnv() {
    const cleanEnv = Object.assign({}, process.env, {
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1'
    });
    // Critical: remove Electron and VS Code runtime flags so that launched IDE apps do not execute as headless Node CLI
    delete cleanEnv.ELECTRON_RUN_AS_NODE;
    delete cleanEnv.ELECTRON_NO_ATTACH_CONSOLE;
    delete cleanEnv.ELECTRON_NO_ASAR;
    delete cleanEnv.VSCODE_PID;
    delete cleanEnv.VSCODE_IPC_HOOK;
    delete cleanEnv.VSCODE_NLS_CONFIG;
    delete cleanEnv.VSCODE_CWD;
    delete cleanEnv.ATOM_SHELL_INTERNAL_RUN_AS_NODE;
    delete cleanEnv.NODE_OPTIONS;
    delete cleanEnv.NODE_ENV;
    return cleanEnv;
}

/**
 * Execute Python helper script asynchronously (used for OS-level account switching & process relaunch)
 */
function runAccountSwitcherAsync(args, timeoutMs = 30000) {
    return new Promise((resolve) => {
        const script = getSwitcherScriptPath();
        if (!fs.existsSync(script)) return resolve(null);
        const onDone = (err, stdout) => {
            if (err) {
                console.error('Account switcher async error:', err);
                return resolve(null);
            }
            try {
                if (stdout && stdout.trim().length > 0) {
                    resolve(JSON.parse(stdout.trim()));
                } else {
                    resolve(null);
                }
            } catch (e) {
                console.error('Account switcher async parse error:', e);
                resolve(null);
            }
        };
        const opts = {
            encoding: 'utf8',
            timeout: timeoutMs || 30000,
            windowsHide: true,
            env: getCleanPythonEnv()
        };
        if (Array.isArray(args)) {
            execFile('python', [script, ...args], opts, onDone);
        } else {
            exec(`python "${script}" ${args}`, opts, onDone);
        }
    });
}

function ideUsesAgyHub() {
    try {
        if (fs.existsSync(path.join(geminiDir, 'bin', 'agy.exe'))) return true;
        for (const root of [path.join(homeDir, '.vscode', 'extensions'), path.join(homeDir, '.antigravity-ide', 'extensions')]) {
            if (fs.existsSync(root)) {
                const entries = fs.readdirSync(root);
                if (entries.some(e => e.startsWith('google.google-antigravity'))) return true;
            }
        }
    } catch (e) {}
    return false;
}

function detectLiveGeminiAuth() {
    const tokenFiles = [
        path.join(geminiDir, 'antigravity-cli', 'antigravity-oauth-token'),
        path.join(geminiDir, 'jetski-standalone-oauth-token'),
        path.join(geminiDir, 'oauth_creds.json'),
        path.join(geminiDir, 'google_accounts.json')
    ];
    for (const tf of tokenFiles) {
        try {
            if (!fs.existsSync(tf)) continue;
            const gdata = JSON.parse(fs.readFileSync(tf, 'utf8'));
            const tok = (gdata && typeof gdata.token === 'object') ? gdata.token : gdata;
            const rt = (tok && tok.refresh_token) ? String(tok.refresh_token).trim() : '';
            const em = String(gdata.id_token_email || gdata.email || gdata.active || '').trim().toLowerCase();
            if (rt || em) {
                return { refreshToken: rt, email: em };
            }
        } catch (e) {}
    }
    return null;
}

function computeAccountQuotaSummary(detail, item) {
    const eng = getQuotaEngine();
    if (eng && typeof eng.computeQuotaSummary === 'function') {
        const s = eng.computeQuotaSummary(detail, item);
        return {
            tier: s.tier || 'PRO',
            claudePct: s.claude_percentage,
            claudeReset: s.claude_reset_time,
            claudeWeeklyPct: s.claude_weekly_percentage,
            claudeWeeklyReset: s.claude_weekly_reset_time,
            claude5hPct: s.claude_5h_percentage,
            claude5hReset: s.claude_5h_reset_time,
            geminiPct: s.gemini_percentage,
            geminiReset: s.gemini_reset_time,
            geminiLimitingBucket: s.gemini_limiting_bucket,
            geminiWeeklyPct: s.gemini_weekly_percentage,
            geminiWeeklyReset: s.gemini_weekly_reset_time,
            gemini5hPct: s.gemini_5h_percentage,
            gemini5hReset: s.gemini_5h_reset_time,
            lastUpdated: s.last_updated || 0
        };
    }

    const quota = (detail && detail.quota) || null;
    const tier = (quota && quota.subscription_tier) || (detail && detail.tier) || (item && item.tier) || 'PRO';

    let claudePct = null;
    let claudeReset = null;
    let claudeWeeklyPct = null;
    let claudeWeeklyReset = null;
    let claude5hPct = null;
    let claude5hReset = null;
    let geminiPct = null;
    let geminiReset = null;
    let geminiLimitingBucket = (quota && quota.gemini_limiting_bucket) || null;
    let geminiWeeklyPct = null;
    let geminiWeeklyReset = null;
    let gemini5hPct = null;
    let gemini5hReset = null;
    let argonPct = null;
    let argonReset = null;
    let argonLimitingBucket = (quota && quota.argon_limiting_bucket) || null;
    let argonWeeklyPct = null;
    let argonWeeklyReset = null;
    let argon5hPct = null;
    let argon5hReset = null;
    const geminiGroups = [];

    if (quota && Array.isArray(quota.quota_groups)) {
        for (const g of quota.quota_groups) {
            if (!g || typeof g !== 'object') continue;
            const dname = String(g.displayName || g.display_name || '');
            const dnameLow = dname.toLowerCase();
            const buckets = Array.isArray(g.buckets) ? g.buckets : [];
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
            if (cands.length > 0) {
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
                    geminiGroups.push({
                        id: isArgon ? 'gemini_argon' : (isMainGem ? 'gemini_models' : `gemini_${geminiGroups.length}`),
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
                        is_main: isMainGem,
                    });
                    if (isArgon) {
                        argonPct = primaryPct;
                        argonReset = primaryReset;
                        argonLimitingBucket = primaryBid || null;
                        argonWeeklyPct = weeklyPct;
                        argonWeeklyReset = weeklyReset;
                        argon5hPct = h5Pct;
                        argon5hReset = h5Reset;
                    } else if (geminiPct === null || isMainGem) {
                        geminiPct = primaryPct;
                        geminiReset = primaryReset;
                        geminiLimitingBucket = primaryBid || null;
                        geminiWeeklyPct = weeklyPct;
                        geminiWeeklyReset = weeklyReset;
                        gemini5hPct = h5Pct;
                        gemini5hReset = h5Reset;
                    }
                } else {
                    claudePct = primaryPct;
                    claudeReset = primaryReset;
                    claudeWeeklyPct = weeklyPct;
                    claudeWeeklyReset = weeklyReset;
                    claude5hPct = h5Pct;
                    claude5hReset = h5Reset;
                }
            }
        }
    }

    if (quota && Array.isArray(quota.models)) {
        const gemModelCands = [];
        const argonModelCands = [];
        const claudeModelCands = [];
        for (const m of quota.models) {
            const mname = (m.name || '').toLowerCase();
            const dname = (m.display_name || '').toLowerCase();
            if (m.percentage === undefined || m.percentage === null) continue;
            if (mname.includes('2.5') || dname.includes('2.5') || mname.includes('flash-lite') || mname.includes('flash_lite') || mname.includes('-low') || mname.includes('-medium')) {
                continue;
            }
            if (mname.includes('argon') || dname.includes('argon')) {
                argonModelCands.push({ pct: Number(m.percentage), reset: m.reset_time || null });
            } else if (mname.includes('gemini') || mname.includes('hydrogen') || dname.includes('hydrogen')) {
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
        if (argonPct === null && argonModelCands.length > 0) {
            argonModelCands.sort((a, b) => a.pct - b.pct);
            argonPct = argonModelCands[0].pct;
            argonReset = argonModelCands[0].reset;
        }
        if (claudePct === null && claudeModelCands.length > 0) {
            claudeModelCands.sort((a, b) => a.pct - b.pct);
            claudePct = claudeModelCands[0].pct;
            claudeReset = claudeModelCands[0].reset;
        }
    }

    if (claudePct === null) {
        if (quota && quota.claude_percentage !== undefined && quota.claude_percentage !== null) claudePct = quota.claude_percentage;
        else if (detail && detail.claude_percentage !== undefined && detail.claude_percentage !== null) claudePct = detail.claude_percentage;
        else if (item && item.claude_percentage !== undefined && item.claude_percentage !== null) claudePct = item.claude_percentage;
        else claudePct = 100;
    }
    if (!claudeReset && quota && quota.claude_reset_time) {
        claudeReset = quota.claude_reset_time;
    }

    if (geminiPct === null && argonPct !== null) {
        geminiPct = argonPct;
        geminiReset = argonReset;
        geminiLimitingBucket = argonLimitingBucket;
        geminiWeeklyPct = argonWeeklyPct;
        geminiWeeklyReset = argonWeeklyReset;
        gemini5hPct = argon5hPct;
        gemini5hReset = argon5hReset;
    }

    if (geminiPct === null) {
        if (quota && quota.gemini_percentage !== undefined && quota.gemini_percentage !== null) geminiPct = quota.gemini_percentage;
        else if (detail && detail.gemini_percentage !== undefined && detail.gemini_percentage !== null) geminiPct = detail.gemini_percentage;
        else if (item && item.gemini_percentage !== undefined && item.gemini_percentage !== null) geminiPct = item.gemini_percentage;
        else geminiPct = 100;
    }
    if (!geminiReset && quota && quota.gemini_reset_time) {
        geminiReset = quota.gemini_reset_time;
    }
    if (argonPct === null) {
        if (quota && quota.argon_percentage !== undefined && quota.argon_percentage !== null) argonPct = quota.argon_percentage;
        else if (detail && detail.argon_percentage !== undefined && detail.argon_percentage !== null) argonPct = detail.argon_percentage;
        else if (item && item.argon_percentage !== undefined && item.argon_percentage !== null) argonPct = item.argon_percentage;
    }
    if (!argonReset && quota && quota.argon_reset_time) {
        argonReset = quota.argon_reset_time;
    }

    return {
        tier,
        claudePct,
        claudeReset,
        claudeWeeklyPct,
        claudeWeeklyReset,
        claude5hPct,
        claude5hReset,
        geminiPct,
        geminiReset,
        geminiLimitingBucket,
        geminiWeeklyPct,
        geminiWeeklyReset,
        gemini5hPct,
        gemini5hReset,
        argonPct,
        argonReset,
        argonLimitingBucket,
        argonWeeklyPct,
        argonWeeklyReset,
        argon5hPct,
        argon5hReset,
        geminiGroups,
        lastUpdated: (quota && quota.last_updated) || 0
    };
}

/**
 * Load all accounts directly in pure Node.js (mtime-cached via quota_engine, zero Python calls)
 */
function loadAllAccountsSync() {
    const eng = getQuotaEngine();
    if (eng && typeof eng.loadState === 'function') {
        try {
            const st = eng.loadState({ appTarget: 'ide' });
            if (st && Array.isArray(st.accounts)) return st.accounts;
        } catch (_) {}
    }

    const agToolsAccountsFile = path.join(agToolsDir, 'accounts.json');
    if (!fs.existsSync(agToolsAccountsFile)) return [];
    try {
        const raw = JSON.parse(fs.readFileSync(agToolsAccountsFile, 'utf8'));
        const fallbackActiveId = raw.current_ide_account_id || raw.current_account_id;
        const liveGeminiAuth = ideUsesAgyHub() ? detectLiveGeminiAuth() : null;
        const list = raw.accounts || [];
        const accountsDir = path.join(agToolsDir, 'accounts');

        const parsed = list.map(item => {
            let detail = null;
            const detailFile = path.join(accountsDir, `${item.id}.json`);
            if (fs.existsSync(detailFile)) {
                try { detail = JSON.parse(fs.readFileSync(detailFile, 'utf8')); } catch (e) {}
            }
            const email = (detail && detail.email) || item.email || '';
            let name = (detail && detail.name) || item.name || '';
            if (!name || name.includes('?') || name.includes('\ufffd')) {
                if (detail && detail.token && detail.token.id_token) {
                    try {
                        const parts = detail.token.id_token.split('.');
                        if (parts.length >= 2) {
                            const payload = JSON.parse(Buffer.from(parts[1], 'base64').toString('utf8'));
                            if (payload.name) name = payload.name;
                        }
                    } catch (e) {}
                }
                if (!name || name.includes('?') || name.includes('\ufffd')) {
                    name = email ? email.split('@')[0] : 'Account';
                }
            }

            const quota = (detail && detail.quota) || null;
            const summary = computeAccountQuotaSummary(detail, item);

            let avatarPath = '';
            if (email) {
                const png = path.join(agToolsDir, 'avatars', `${email}.png`);
                const jpg = path.join(agToolsDir, 'avatars', `${email}.jpg`);
                if (fs.existsSync(png)) avatarPath = png;
                else if (fs.existsSync(jpg)) avatarPath = jpg;
            }
            const rt = (detail && detail.token && detail.token.refresh_token) ? String(detail.token.refresh_token).trim() : '';
            return {
                id: item.id,
                email: email,
                name: name,
                _rt: rt,
                is_active: false,
                tier: summary.tier,
                claude_percentage: summary.claudePct,
                gemini_percentage: summary.geminiPct,
                claude_reset_time: summary.claudeReset,
                gemini_reset_time: summary.geminiReset,
                claude_weekly_percentage: summary.claudeWeeklyPct,
                claude_weekly_reset_time: summary.claudeWeeklyReset,
                claude_5h_percentage: summary.claude5hPct,
                claude_5h_reset_time: summary.claude5hReset,
                gemini_limiting_bucket: summary.geminiLimitingBucket,
                gemini_weekly_percentage: summary.geminiWeeklyPct,
                gemini_weekly_reset_time: summary.geminiWeeklyReset,
                gemini_5h_percentage: summary.gemini5hPct,
                gemini_5h_reset_time: summary.gemini5hReset,
                argon_percentage: summary.argonPct,
                argon_reset_time: summary.argonReset,
                argon_limiting_bucket: summary.argonLimitingBucket,
                argon_weekly_percentage: summary.argonWeeklyPct,
                argon_weekly_reset_time: summary.argonWeeklyReset,
                argon_5h_percentage: summary.argon5hPct,
                argon_5h_reset_time: summary.argon5hReset,
                gemini_groups: summary.geminiGroups,
                last_updated: summary.lastUpdated,
                avatar_path: avatarPath,
                avatar_url: (detail && detail.avatar_url) || '',
                quota: quota,
                last_used: item.last_used || 0
            };
        });

        let matchedActive = null;
        if (liveGeminiAuth) {
            if (liveGeminiAuth.refreshToken) {
                matchedActive = parsed.find(a => a._rt && a._rt === liveGeminiAuth.refreshToken) || null;
            }
            if (!matchedActive && liveGeminiAuth.email) {
                matchedActive = parsed.find(a => a.email && a.email.toLowerCase() === liveGeminiAuth.email) || null;
            }
        }
        if (!matchedActive && fallbackActiveId) {
            matchedActive = parsed.find(a => a.id === fallbackActiveId) || null;
        }
        if (matchedActive) {
            matchedActive.is_active = true;
        }

        return parsed.map(({ _rt, ...rest }) => rest);
    } catch (e) {
        return [];
    }
}

/**
 * Get accurate account information for active IDE session, avatar and quotas.
 * Pure Node.js implementation: loads from mtime cache without spawning Python processes.
 */
function getAccountData() {
    let allAccounts = [];
    let matched = null;

    const eng = getQuotaEngine();
    if (eng && typeof eng.loadState === 'function') {
        try {
            const st = eng.loadState({ appTarget: 'ide' });
            if (st) {
                allAccounts = st.accounts || [];
                matched = st.activeAccount || null;
            }
        } catch (_) {}
    }

    if (!allAccounts.length && !matched) {
        allAccounts = loadAllAccountsSync();
        matched = allAccounts.find(a => a.is_active);
    }

    if (!matched && allAccounts.length > 0) {
        matched = allAccounts.slice().sort((a, b) => (b.last_used || 0) - (a.last_used || 0))[0];
    }

    let activeId = matched ? matched.id : null;
    let activeName = matched ? matched.name : 'Default';
    let activeEmail = matched ? matched.email : '';
    let quota = matched ? matched.quota : null;
    let tier = (matched && matched.tier) || (quota && quota.subscription_tier) || 'PRO';
    let avatarPath = matched ? matched.avatar_path : '';
    let avatarUrl = matched ? matched.avatar_url : '';
    let claudePct = (matched && matched.claude_percentage !== undefined && matched.claude_percentage !== null) ? matched.claude_percentage : 100;
    let geminiPct = (matched && matched.gemini_percentage !== undefined && matched.gemini_percentage !== null) ? matched.gemini_percentage : 100;
    let claudeReset = matched ? matched.claude_reset_time : null;
    let geminiReset = matched ? matched.gemini_reset_time : null;
    let claudeLimitingBucket = matched ? (matched.claude_limiting_bucket || null) : null;
    let claudeWeeklyPct = (matched && matched.claude_weekly_percentage !== undefined) ? matched.claude_weekly_percentage : null;
    let claudeWeeklyReset = matched ? (matched.claude_weekly_reset_time || null) : null;
    let claude5hPct = (matched && matched.claude_5h_percentage !== undefined) ? matched.claude_5h_percentage : null;
    let claude5hReset = matched ? (matched.claude_5h_reset_time || null) : null;
    let geminiLimitingBucket = matched ? (matched.gemini_limiting_bucket || null) : null;
    let geminiWeeklyPct = (matched && matched.gemini_weekly_percentage !== undefined) ? matched.gemini_weekly_percentage : null;
    let geminiWeeklyReset = matched ? (matched.gemini_weekly_reset_time || null) : null;
    let gemini5hPct = (matched && matched.gemini_5h_percentage !== undefined) ? matched.gemini_5h_percentage : null;
    let gemini5hReset = matched ? (matched.gemini_5h_reset_time || null) : null;
    let argonPct = (matched && matched.argon_percentage !== undefined && matched.argon_percentage !== null) ? matched.argon_percentage : null;
    let argonReset = matched ? (matched.argon_reset_time || null) : null;
    let argonLimitingBucket = matched ? (matched.argon_limiting_bucket || null) : null;
    let argonWeeklyPct = (matched && matched.argon_weekly_percentage !== undefined) ? matched.argon_weekly_percentage : null;
    let argonWeeklyReset = matched ? (matched.argon_weekly_reset_time || null) : null;
    let argon5hPct = (matched && matched.argon_5h_percentage !== undefined) ? matched.argon_5h_percentage : null;
    let argon5hReset = matched ? (matched.argon_5h_reset_time || null) : null;
    let geminiGroups = (matched && Array.isArray(matched.gemini_groups)) ? matched.gemini_groups : [];
    let lastUpdated = matched ? matched.last_updated : 0;

    if (activeName && activeName !== 'Default' && activeEmail) {
        lastKnownAccountData = {
            id: activeId,
            name: activeName,
            email: activeEmail,
            avatar_path: avatarPath,
            avatar_url: avatarUrl,
            quota: quota,
            tier: tier,
            claude_percentage: claudePct,
            gemini_percentage: geminiPct,
            claude_reset_time: claudeReset,
            gemini_reset_time: geminiReset,
            claude_limiting_bucket: claudeLimitingBucket,
            claude_weekly_percentage: claudeWeeklyPct,
            claude_weekly_reset_time: claudeWeeklyReset,
            claude_5h_percentage: claude5hPct,
            claude_5h_reset_time: claude5hReset,
            gemini_limiting_bucket: geminiLimitingBucket,
            gemini_weekly_percentage: geminiWeeklyPct,
            gemini_weekly_reset_time: geminiWeeklyReset,
            gemini_5h_percentage: gemini5hPct,
            gemini_5h_reset_time: gemini5hReset,
            argon_percentage: argonPct,
            argon_reset_time: argonReset,
            argon_limiting_bucket: argonLimitingBucket,
            argon_weekly_percentage: argonWeeklyPct,
            argon_weekly_reset_time: argonWeeklyReset,
            argon_5h_percentage: argon5hPct,
            argon_5h_reset_time: argon5hReset,
            gemini_groups: geminiGroups,
            last_updated: lastUpdated
        };
    } else if (lastKnownAccountData) {
        activeId = activeId || lastKnownAccountData.id;
        activeName = lastKnownAccountData.name;
        activeEmail = activeEmail || lastKnownAccountData.email;
        avatarPath = avatarPath || lastKnownAccountData.avatar_path;
        avatarUrl = avatarUrl || lastKnownAccountData.avatar_url;
        quota = quota || lastKnownAccountData.quota;
        tier = tier || lastKnownAccountData.tier || 'PRO';
        claudePct = lastKnownAccountData.claude_percentage;
        geminiPct = lastKnownAccountData.gemini_percentage;
        claudeReset = lastKnownAccountData.claude_reset_time;
        geminiReset = lastKnownAccountData.gemini_reset_time;
        claudeLimitingBucket = lastKnownAccountData.claude_limiting_bucket || null;
        claudeWeeklyPct = lastKnownAccountData.claude_weekly_percentage ?? null;
        claudeWeeklyReset = lastKnownAccountData.claude_weekly_reset_time || null;
        claude5hPct = lastKnownAccountData.claude_5h_percentage ?? null;
        claude5hReset = lastKnownAccountData.claude_5h_reset_time || null;
        geminiLimitingBucket = lastKnownAccountData.gemini_limiting_bucket || null;
        geminiWeeklyPct = lastKnownAccountData.gemini_weekly_percentage ?? null;
        geminiWeeklyReset = lastKnownAccountData.gemini_weekly_reset_time || null;
        gemini5hPct = lastKnownAccountData.gemini_5h_percentage ?? null;
        gemini5hReset = lastKnownAccountData.gemini_5h_reset_time || null;
        argonPct = lastKnownAccountData.argon_percentage ?? null;
        argonReset = lastKnownAccountData.argon_reset_time || null;
        argonLimitingBucket = lastKnownAccountData.argon_limiting_bucket || null;
        argonWeeklyPct = lastKnownAccountData.argon_weekly_percentage ?? null;
        argonWeeklyReset = lastKnownAccountData.argon_weekly_reset_time || null;
        argon5hPct = lastKnownAccountData.argon_5h_percentage ?? null;
        argon5hReset = lastKnownAccountData.argon_5h_reset_time || null;
        geminiGroups = Array.isArray(lastKnownAccountData.gemini_groups) ? lastKnownAccountData.gemini_groups : [];
        lastUpdated = lastKnownAccountData.last_updated || 0;
    }

    return {
        type: 'active_ide',
        active: {
            id: activeId,
            name: activeName,
            email: activeEmail,
            avatar_path: avatarPath,
            avatar_url: avatarUrl,
            claude_percentage: claudePct,
            gemini_percentage: geminiPct,
            claude_reset_time: claudeReset,
            gemini_reset_time: geminiReset,
            claude_limiting_bucket: claudeLimitingBucket,
            claude_weekly_percentage: claudeWeeklyPct,
            claude_weekly_reset_time: claudeWeeklyReset,
            claude_5h_percentage: claude5hPct,
            claude_5h_reset_time: claude5hReset,
            gemini_limiting_bucket: geminiLimitingBucket,
            gemini_weekly_percentage: geminiWeeklyPct,
            gemini_weekly_reset_time: geminiWeeklyReset,
            gemini_5h_percentage: gemini5hPct,
            gemini_5h_reset_time: gemini5hReset,
            argon_percentage: argonPct,
            argon_reset_time: argonReset,
            argon_limiting_bucket: argonLimitingBucket,
            argon_weekly_percentage: argonWeeklyPct,
            argon_weekly_reset_time: argonWeeklyReset,
            argon_5h_percentage: argon5hPct,
            argon_5h_reset_time: argon5hReset,
            gemini_groups: geminiGroups,
            last_updated: lastUpdated
        },
        quota: quota,
        tier: tier,
        last_updated: lastUpdated,
        accounts: allAccounts
    };
}

/**
 * Convert ISO reset timestamp into clear, friendly Russian relative time
 */
function formatResetTime(isoStr, percentage, opts = {}) {
    const withIcon = !(opts && opts.withIcon === false);
    if (percentage === 100) return '(доступно 100%)';
    if (!isoStr) return '';
    try {
        const target = new Date(isoStr);
        const diffMs = target.getTime() - Date.now();
        if (diffMs <= 0) return percentage < 100 ? '(ожидает обновления)' : '(доступно 100%)';

        const totalMins = Math.floor(diffMs / (1000 * 60));
        const hours = Math.floor(totalMins / 60);
        const mins = totalMins % 60;
        const days = Math.floor(hours / 24);
        const remHours = hours % 24;

        let rel = '';
        if (days > 0) {
            rel = `${days} дн. ${remHours} ч.`;
        } else {
            rel = `${hours} ч. ${mins} мин.`;
        }

        const daysRu = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
        const monthsRu = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
        const d = daysRu[target.getDay()];
        const dayNum = target.getDate();
        const month = monthsRu[target.getMonth()];
        const time = target.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });

        return `${withIcon ? '⏳ ' : ''}сброс через ${rel} (${d}, ${dayNum} ${month} в ${time})`;
    } catch (e) {
        return `(сброс: ${isoStr})`;
    }
}

function getDnsConfig() {
    try {
        const p = path.join(agToolsDir, 'dns_config.json');
        if (fs.existsSync(p)) {
            return JSON.parse(fs.readFileSync(p, 'utf8'));
        }
    } catch (e) {}
    return { enabled: false };
}

function ensureOnboardingCompleted(email, usesGcpTos = false) {
    const gcpStr = usesGcpTos ? 'true' : 'false';
    const ensureKey = `${email}|${gcpStr}`;
    if (!email || ensureKey === lastEnsuredEmail || ensureKey === onboardingInFlightEmail) return;
    try {
        const effectiveAppDataDir = process.env.APPDATA || appDataDir;
        const appName = (vscode.env && vscode.env.appName) ? String(vscode.env.appName) : '';
        const appDirName = /visual studio code/i.test(appName) ? 'Code' : 'Antigravity IDE';
        const dbPath = path.join(effectiveAppDataDir, appDirName, 'User', 'globalStorage', 'state.vscdb');
        if (!fs.existsSync(dbPath)) return;

        let dbBirthMs = 0;
        try {
            const dbStat = fs.statSync(dbPath);
            dbBirthMs = Math.round(dbStat.birthtimeMs || 0);
        } catch (_) {}

        const markerPath = path.join(agToolsDir, '.onboarding_synced');
        try {
            if (fs.existsSync(markerPath)) {
                const cached = JSON.parse(fs.readFileSync(markerPath, 'utf8'));
                if (
                    cached &&
                    cached.version === 3 &&
                    cached.email === email &&
                    cached.usesGcpTos === gcpStr &&
                    cached.dbPath === dbPath &&
                    cached.dbBirthMs === dbBirthMs &&
                    (Date.now() - (cached.ts || 0)) < 86400000
                ) {
                    lastEnsuredEmail = ensureKey;
                    return;
                }
            }
        } catch (_) {}

        onboardingInFlightEmail = ensureKey;
        const pyCode = [
            'import sys, json, sqlite3',
            'db, em, gcp = sys.argv[1], sys.argv[2], sys.argv[3]',
            'con = sqlite3.connect(db, timeout=2)',
            'cur = con.cursor()',
            'rows = [',
            "    ('agy.onboarding.lastLoginUsername', em),",
            "    ('agy.onboarding.lastLoginIsGcpTos', gcp),",
            "    ('agy.onboarding.welcomeModalSeen', 'true'),",
            "    ('jetski.onboarding.lastLoginUsername', em),",
            "    ('jetski.onboarding.lastLoginIsGcpTos', gcp),",
            "    ('jetski.onboarding.welcomeModalSeen', 'true'),",
            "    ('jetski.onboarding.cloudResourceDisclosureSeen', 'true'),",
            "    ('antigravityOnboarding', 'true'),",
            "    ('antigravity.hasShownOnboarding', 'true'),",
            ']',
            "cur.executemany('INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)', rows)",
            "cur.execute(\"DELETE FROM ItemTable WHERE key IN ('agy.onboarding.lastLoginStrategy','jetski.onboarding.lastLoginStrategy')\")",
            "cur.execute(\"SELECT value FROM ItemTable WHERE key = 'google.google-antigravity'\")",
            'row = cur.fetchone()',
            'ext_data = {}',
            'if row and row[0]:',
            '    try:',
            '        parsed = json.loads(row[0])',
            '        if isinstance(parsed, dict):',
            '            ext_data = parsed',
            '    except Exception:',
            '        ext_data = {}',
            "ext_data['antigravity.accountTelemetryEnabled'] = True",
            "ext_data['antigravity.legacyTelemetrySettingNoticeShown'] = True",
            "ag_store = ext_data.get('antigravity-storage')",
            'if not isinstance(ag_store, dict):',
            '    ag_store = {}',
            "ag_store['agy.onboarding.lastLoginUsername'] = em",
            "ag_store['agy.onboarding.lastLoginIsGcpTos'] = gcp",
            "ag_store['agy.onboarding.welcomeModalSeen'] = 'true'",
            "ag_store['jetski.onboarding.lastLoginUsername'] = em",
            "ag_store['jetski.onboarding.lastLoginIsGcpTos'] = gcp",
            "ag_store['jetski.onboarding.welcomeModalSeen'] = 'true'",
            "ag_store['jetski.onboarding.cloudResourceDisclosureSeen'] = 'true'",
            "for stale_k in ('agy.onboarding.lastLoginStrategy', 'jetski.onboarding.lastLoginStrategy'):",
            '    ag_store.pop(stale_k, None)',
            "ext_data['antigravity-storage'] = ag_store",
            "cur.execute('INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)', ('google.google-antigravity', json.dumps(ext_data, ensure_ascii=False, separators=(',', ':'))))",
            'con.commit()',
            'con.close()'
        ].join('\n');
        execFile('python', ['-c', pyCode, dbPath, email, gcpStr], { timeout: 3000, windowsHide: true, env: getCleanPythonEnv() }, (err) => {
            if (onboardingInFlightEmail === ensureKey) onboardingInFlightEmail = '';
            if (!err) {
                lastEnsuredEmail = ensureKey;
                try {
                    if (!fs.existsSync(agToolsDir)) fs.mkdirSync(agToolsDir, { recursive: true });
                    fs.writeFileSync(markerPath, JSON.stringify({ version: 3, email, usesGcpTos: gcpStr, dbPath, dbBirthMs, ts: Date.now() }), 'utf8');
                } catch (_) {}
            }
        });
    } catch (_) {
        if (onboardingInFlightEmail === ensureKey) onboardingInFlightEmail = '';
    }
}

function getCachedSvgIcons() {
    if (cachedSvgIcons) return cachedSvgIcons;
    const claudeSvgPath = path.join(__dirname, 'icons', 'claude.svg');
    const geminiSvgPath = path.join(__dirname, 'icons', 'gemini.svg');
    cachedSvgIcons = {
        claude: fs.existsSync(claudeSvgPath) ? `<img src="${vscode.Uri.file(claudeSvgPath).toString()}" width="14" height="14" style="vertical-align: middle;" /> ` : '✻ ',
        gemini: fs.existsSync(geminiSvgPath) ? `<img src="${vscode.Uri.file(geminiSvgPath).toString()}" width="14" height="14" style="vertical-align: middle;" /> ` : '✦ '
    };
    return cachedSvgIcons;
}

function formatQuotaWindowDetails(weeklyPct, weeklyReset, h5Pct, h5Reset) {
    const lines = [];
    if (h5Pct !== null && h5Pct !== undefined) {
        const r = (h5Pct < 100 && h5Reset) ? ` — ${formatResetTime(h5Reset, h5Pct)}` : '';
        lines.push(`  \n&nbsp;&nbsp;&nbsp;&nbsp;↳ 5ч лимит: **${h5Pct}%**${r}`);
    }
    if (weeklyPct !== null && weeklyPct !== undefined) {
        const r = (weeklyPct < 100 && weeklyReset) ? ` — ${formatResetTime(weeklyReset, weeklyPct)}` : '';
        lines.push(`  \n&nbsp;&nbsp;&nbsp;&nbsp;↳ Недельный: **${weeklyPct}%**${r}`);
    }
    return lines.join('');
}

/**
 * Update Status Bar text, icon and clean tooltip with avatar (network probe cached 45s + DOM dirty-checked)
 */
async function updateStatusBar(options = {}) {
    if (!statusBarItem) return;

    const forceNetwork = Boolean(options.forceNetwork);
    const skipNetwork = Boolean(options.skipNetwork);
    const forceRender = Boolean(options.forceRender);

    const accountInfo = getAccountData();
    const act = accountInfo.active || null;

    let accountId = '';
    let accountName = 'Default';
    let accountEmail = '';
    let tier = '';
    let claudePct = 100;
    let geminiPct = 100;
    let argonPct = null;
    let avatarPath = '';

    if (act) {
        accountId = act.id || '';
        accountName = act.name || (act.email ? act.email.split('@')[0] : 'Account');
        accountEmail = act.email || '';
        avatarPath = act.avatar_path || '';
        if (act.claude_percentage !== undefined && act.claude_percentage !== null) claudePct = act.claude_percentage;
        if (act.gemini_percentage !== undefined && act.gemini_percentage !== null) geminiPct = act.gemini_percentage;
        if (act.argon_percentage !== undefined && act.argon_percentage !== null) argonPct = act.argon_percentage;
        if (accountEmail) {
            const usesGcpTos = Boolean(accountInfo.quota && accountInfo.quota.uses_gcp_tos);
            ensureOnboardingCompleted(accountEmail, usesGcpTos);
        }
    }
    if (argonPct === null && accountInfo.quota && accountInfo.quota.argon_percentage !== undefined && accountInfo.quota.argon_percentage !== null) {
        argonPct = accountInfo.quota.argon_percentage;
    }

    tier = accountInfo.tier || (accountInfo.quota && accountInfo.quota.subscription_tier) || 'PRO';

    const claudeQuota = `${claudePct}%`;
    const geminiQuota = `${geminiPct}%`;
    const initialQuotaText = argonPct !== null
        ? ` [✻ ${claudeQuota} | ✦ G ${geminiQuota} · A ${argonPct}%]`
        : ` [✻ ${claudeQuota} | ✦ ${geminiQuota}]`;

    // Render cached values immediately without flicker on first paint
    if (!statusBarItem.text) {
        statusBarItem.text = `$(account) ${accountName}${initialQuotaText} | Проверка...`;
        statusBarItem.show();
    }

    const needNet = !skipNetwork && (forceNetwork || !lastNetCheckAt || (Date.now() - lastNetCheckAt) >= NET_CACHE_TTL_MS);
    if (needNet) {
        await probeNetwork(forceNetwork);
    }

    const conn = lastConnectionInfo;
    const country = getCountryInfo(conn.countryCode);
    const proxyUrl = (conn && conn.proxyUrl) || (lastProxyInfo && lastProxyInfo.url) || '';

    const countryLabel = conn.ok ? (country.name || 'Онлайн') : 'Офлайн';
    const pingLabel = conn.ok ? `${conn.ms}ms` : conn.status;

    const claudeResetIso = (act && act.claude_reset_time) || null;
    const geminiResetIso = (act && act.gemini_reset_time) || null;
    const claudeWeeklyPct = (act && act.claude_weekly_percentage !== undefined) ? act.claude_weekly_percentage : null;
    const claudeWeeklyReset = (act && act.claude_weekly_reset_time) || null;
    const claude5hPct = (act && act.claude_5h_percentage !== undefined) ? act.claude_5h_percentage : null;
    const claude5hReset = (act && act.claude_5h_reset_time) || null;
    const geminiWeeklyPct = (act && act.gemini_weekly_percentage !== undefined) ? act.gemini_weekly_percentage : null;
    const geminiWeeklyReset = (act && act.gemini_weekly_reset_time) || null;
    const gemini5hPct = (act && act.gemini_5h_percentage !== undefined) ? act.gemini_5h_percentage : null;
    const gemini5hReset = (act && act.gemini_5h_reset_time) || null;
    const argonResetIso = (act && act.argon_reset_time) || (accountInfo.quota && accountInfo.quota.argon_reset_time) || null;
    const argonWeeklyPct = (act && act.argon_weekly_percentage !== undefined) ? act.argon_weekly_percentage : null;
    const argonWeeklyReset = (act && act.argon_weekly_reset_time) || null;
    const argon5hPct = (act && act.argon_5h_percentage !== undefined) ? act.argon_5h_percentage : null;
    const argon5hReset = (act && act.argon_5h_reset_time) || null;

    const renderSig = [
        accountId, accountName, accountEmail, tier, claudePct, geminiPct, argonPct ?? '',
        claudeResetIso || '', geminiResetIso || '', argonResetIso || '',
        claudeWeeklyPct ?? '', claude5hPct ?? '', claudeWeeklyReset || '', claude5hReset || '',
        geminiWeeklyPct ?? '', gemini5hPct ?? '', geminiWeeklyReset || '', gemini5hReset || '',
        argonWeeklyPct ?? '', argon5hPct ?? '', argonWeeklyReset || '', argon5hReset || '',
        avatarPath,
        conn.ok ? 1 : 0, conn.countryCode || '', conn.status || '', proxyUrl
    ].join('|');

    if (!forceRender && renderSig === lastRenderedBarSig && statusBarItem.text) {
        return;
    }
    lastRenderedBarSig = renderSig;

    statusBarItem.text = `$(account) ${accountName}${initialQuotaText} | ${countryLabel} (${pingLabel})`;

    // Clean, minimalist tooltip with avatar and neural network logos
    const md = new vscode.MarkdownString('', true);
    md.isTrusted = true;
    md.supportHtml = true;
    md.appendMarkdown(`### **Antigravity Assistant**\n\n`);

    let avatarMarkdown = '';
    if (avatarPath) {
        try {
            const fileUri = vscode.Uri.file(avatarPath).toString();
            avatarMarkdown = `<img src="${fileUri}" width="18" height="18" style="border-radius: 50%;" /> `;
        } catch (_) {}
    }

    const icons = getCachedSvgIcons();
    const claudeReset = claudeResetIso ? formatResetTime(claudeResetIso, claudePct) : (claudePct === 100 ? '(доступно 100%)' : '');
    const geminiReset = geminiResetIso ? formatResetTime(geminiResetIso, geminiPct) : (geminiPct === 100 ? '(доступно 100%)' : '');
    const argonReset = argonPct !== null ? (argonResetIso ? formatResetTime(argonResetIso, argonPct) : (argonPct === 100 ? '(доступно 100%)' : '')) : '';
    const claudeBreakdown = formatQuotaWindowDetails(claudeWeeklyPct, claudeWeeklyReset, claude5hPct, claude5hReset);
    const geminiBreakdown = formatQuotaWindowDetails(geminiWeeklyPct, geminiWeeklyReset, gemini5hPct, gemini5hReset);
    const argonBreakdown = argonPct !== null ? formatQuotaWindowDetails(argonWeeklyPct, argonWeeklyReset, argon5hPct, argon5hReset) : '';
    const isUltraTier = String(tier || '').toUpperCase().includes('ULTRA');
    const geminiLabel = (isUltraTier || argonPct !== null)
        ? 'Gemini (Hydrogen / 3.8 Flash)'
        : 'Gemini';

    md.appendMarkdown(`- **Аккаунт:** ${avatarMarkdown}**${accountName}**${accountEmail ? ` (${accountEmail})` : ''}\n`);
    md.appendMarkdown(`- **Тариф:** **${tier}**\n`);
    md.appendMarkdown(`- **Лимиты нейросетей:**\n`);
    md.appendMarkdown(`  - ${icons.claude}**Claude / GPT:** **${claudeQuota}**${claudeBreakdown ? claudeBreakdown : (claudeReset ? ` — *${claudeReset}*` : '')}\n`);
    md.appendMarkdown(`  - ${icons.gemini}**${geminiLabel}:** **${geminiQuota}**${geminiBreakdown ? geminiBreakdown : (geminiReset ? ` — *${geminiReset}*` : '')}\n`);
    if (argonPct !== null) {
        md.appendMarkdown(`  - ${icons.gemini}**Gemini 4 Argon:** **${argonPct}%**${argonBreakdown ? argonBreakdown : (argonReset ? ` — *${argonReset}*` : '')}\n`);
    } else if (isUltraTier) {
        md.appendMarkdown(`  - ${icons.gemini}**Gemini 4 Carbon:** **${geminiQuota}** *(пул Ultra)*\n`);
        md.appendMarkdown(`  - ${icons.gemini}**Gemini 4 Argon:** **${geminiQuota}** *(пул Ultra)*\n`);
    } else {
        md.appendMarkdown(`  - ${icons.gemini}**Gemini 4 Argon:** **${geminiQuota}** *(общий пул Gemini)*\n`);
    }
    if (conn.ok) {
        if (country.name) {
            md.appendMarkdown(`- **Страна:** ${country.name}\n`);
        }
        md.appendMarkdown(`- **Пинг:** ${conn.status}\n`);
    } else {
        md.appendMarkdown(`- **Сеть:** ${conn.status}\n`);
    }
    if (proxyUrl) {
        md.appendMarkdown(`- **Прокси:** ${proxyUrl}\n`);
    }
    md.appendMarkdown(`\n---\n*Нажмите для выбора действия*`);

    statusBarItem.tooltip = md;
    statusBarItem.show();
}

/**
 * Show QuickPick Main Menu -- Opens immediately (0ms lag) using cached status and prefetches active quota
 */
async function showMainMenu() {
    refreshActiveQuotaBackground({ minAgeSec: 15 });
    const accountInfo = getAccountData();
    let currentAcc = 'Default';
    const currentEmail = accountInfo.active && accountInfo.active.email ? accountInfo.active.email : '';
    let switchIcon = new vscode.ThemeIcon('account');

    if (accountInfo.active) {
        currentAcc = accountInfo.active.name || accountInfo.active.email;
        if (accountInfo.active.avatar_path) {
            switchIcon = vscode.Uri.file(accountInfo.active.avatar_path);
        }
    }

    const policyLabels = {
        interactive: 'Спрашивать каждый раз',
        account_hop: 'Авто: смена аккаунта + «continue»',
        model_fallback: 'Авто: быстрый Flash'
    };
    const eng = getQuotaEngine();
    const sharedSettings = (eng && typeof eng.loadAssistantSettings === 'function')
        ? (eng.loadAssistantSettings() || {}) : {};
    const cfgPolicy = vscode.workspace.getConfiguration('agToolkit').get('failoverPolicy');
    const currentPolicy = cfgPolicy || sharedSettings.failoverPolicy || 'interactive';

    const checkpoints = listSwarmCheckpointsSync(5);
    const activeCp = checkpoints.find((c) => c && !c.is_complete) || checkpoints[0] || null;
    let swarmDesc = 'Запуск нескольких агентов по разным файлам, прогресс и /swarm в чате';
    if (activeCp) {
        const pName = activeCp.project_name || (activeCp.project_path ? path.basename(activeCp.project_path) : 'проект');
        const stage = activeCp.stage_label || activeCp.current_node || 'В очереди';
        const wModels = Array.isArray(activeCp.worker_models) && activeCp.worker_models.length
            ? activeCp.worker_models.join(', ') : 'flash';
        swarmDesc = activeCp.is_complete
            ? `Последняя: ${pName} · ${stage} (${activeCp.done_workers}/${activeCp.total_workers} агентов)`
            : `Активна: ${pName} · ${stage} · Агенты: ${activeCp.done_workers}/${activeCp.total_workers} (${wModels})`;
    }
    const modelCfg = typeof loadModelConfigSync === 'function' ? loadModelConfigSync() : {};
    const customModelCount = modelCfg && modelCfg.custom_models && typeof modelCfg.custom_models === 'object'
        ? Object.keys(modelCfg.custom_models).length : 0;

    const allAccountsCount = loadAllAccountsSync().length;
    const items = [
        {
            label: 'Переключить или добавить аккаунт',
            description: currentEmail
                ? `Текущий: ${currentAcc} (${currentEmail}) • Всего: ${allAccountsCount}`
                : `Текущий: ${currentAcc} • Всего: ${allAccountsCount}`,
            iconPath: switchIcon,
            action: 'switch_account'
        },
        {
            label: '$(pulse) Квоты и лимиты аккаунта',
            description: 'Остаток токенов Claude, Gemini и точное время сброса',
            action: 'quota'
        },
        {
            label: `$(zap) Режим при исчерпании квоты: ${policyLabels[currentPolicy] || currentPolicy}`,
            description: 'Другой аккаунт → другая умная модель → резервный Flash, чтобы рой не встал',
            action: 'set_429_policy'
        },
        {
            label: '$(organization) Команда агентов (задачи и запуск)',
            description: swarmDesc,
            action: 'swarm_hub'
        },
        ...((_swarmProcess || _swarmPollTimer) ? [{
            label: '$(debug-stop) Остановить фоновую команду агентов',
            description: _swarmTaskId ? `Активная задача: ${_swarmTaskId}` : 'Остановить мониторинг / IPC',
            action: 'stop_swarm'
        }] : []),
        {
            label: '$(settings-gear) Доп. настройки моделей (сторонние API)',
            description: `Для стандартных Claude/Gemini/Flash менять не нужно (своих: ${customModelCount})`,
            action: 'manage_models'
        },
        {
            label: '$(sync) Обновить VPN и сеть',
            description: 'Перепроверить пинг, страну и соединение',
            action: 'refresh_network'
        },
        {
            label: '$(output) Открыть лог работы',
            description: 'Просмотр лога запросов моделей и языкового сервера',
            action: 'open_logs'
        }
    ];

    const country = getCountryInfo(lastConnectionInfo.countryCode);
    const locText = lastConnectionInfo.ok ? (country.name ? `${country.name} (${lastConnectionInfo.ms}ms)` : `${lastConnectionInfo.ms}ms`) : 'Офлайн';

    const selected = await vscode.window.showQuickPick(items, {
        placeHolder: `Antigravity Assistant | Аккаунт: ${currentAcc} | ${locText}`
    });

    if (!selected) return;

    switch (selected.action) {
        case 'switch_account':
            await showAccountSwitchMenu();
            break;
        case 'refresh_network':
            await refreshNetworkConnection();
            break;
        case 'quota':
            await showQuotaDetails();
            break;
        case 'open_logs':
            await openAntigravityLogs();
            break;
        case 'set_429_policy':
            await show429PolicyMenu();
            break;
        case 'manage_models':
            await showModelConfigMenu();
            break;
        case 'swarm_hub':
            await showSwarmHubMenu();
            break;
        case 'run_swarm':
            await runSwarmCommand();
            break;
        case 'resume_swarm':
            await resumeSwarmCommand();
            break;
        case 'stop_swarm':
            await stopSwarmCommand();
            break;
    }
}

/**
 * Force refresh quotas for all accounts online using shared pure-Node Keep-Alive engine
 */
async function refreshQuotasOnline() {
    await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Запрос актуальных лимитов у Google Cloud API...',
        cancellable: false
    }, async (progress) => {
        progress.report({ message: 'Опрос квот для всех сохраненных профилей...' });
        try {
            const eng = getQuotaEngine();
            if (eng && typeof eng.refreshAll === 'function') {
                await eng.refreshAll({ appTarget: 'ide', concurrency: 8, force: true, minAgeSec: 0 });
            } else {
                await runAccountSwitcherAsync('refresh_quotas --app ide');
            }
            await updateStatusBar({ skipNetwork: Boolean(lastNetCheckAt), forceRender: true });
            vscode.window.showInformationMessage('Актуальные лимиты всех аккаунтов успешно загружены!');
        } catch (e) {
            vscode.window.showErrorMessage(`Ошибка обновления квот: ${e.message}`);
        }
    });
}

async function findAgyHubPids(hubPort) {
    const safePort = Number(hubPort);
    if (!Number.isInteger(safePort) || safePort <= 0 || safePort > 65535) {
        return [];
    }
    const selfPids = new Set([process.pid, process.ppid].filter((n) => Number.isInteger(n) && n > 0));
    const portRe = new RegExp(`--hub-port(?:=|\\s+)${safePort}(?:\\s|$)`);

    if (process.platform === 'win32') {
        // Fast pre-check via tasklist (<20ms) avoids cold powershell.exe startup when no agy/antigravity process is running
        const [agyOut, antigravityOut] = await Promise.all([
            execFileAsync('tasklist', ['/FI', 'IMAGENAME eq agy.exe', '/NH'], 1500),
            execFileAsync('tasklist', ['/FI', 'IMAGENAME eq antigravity.exe', '/NH'], 1500)
        ]);
        const combinedTaskOut = `${agyOut || ''}\n${antigravityOut || ''}`.toLowerCase();
        if (!combinedTaskOut.includes('agy.exe') && !combinedTaskOut.includes('antigravity.exe')) {
            return [];
        }

        // Try wmic first if available (fast ~30ms without PowerShell)
        const wmicOut = await execFileAsync(
            'wmic',
            ['process', 'where', "name='agy.exe' or name='antigravity.exe'", 'get', 'ProcessId,CommandLine', '/FORMAT:CSV'],
            2000
        );
        if (wmicOut && wmicOut.includes(',')) {
            const wmicPids = [];
            for (const line of String(wmicOut).split(/\r?\n/)) {
                const trimmed = line.trim();
                if (!trimmed || trimmed.startsWith('Node,')) continue;
                const lastComma = trimmed.lastIndexOf(',');
                if (lastComma === -1) continue;
                const pid = parseInt(trimmed.slice(lastComma + 1).trim(), 10);
                const cmdline = trimmed.slice(0, lastComma);
                if (Number.isInteger(pid) && pid > 0 && !selfPids.has(pid) && portRe.test(cmdline)) {
                    wmicPids.push(pid);
                }
            }
            return wmicPids;
        }

        const psScript = `Get-CimInstance Win32_Process -Filter "Name='agy.exe' OR Name='antigravity.exe'" | Where-Object { $_.CommandLine -match '--hub-port(?:=|\\s+)${safePort}(?:\\s|$)' } | Select-Object -ExpandProperty ProcessId`;
        const out = await execFileAsync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', psScript], 3000);
        if (!out) return [];
        const pids = [];
        for (const line of String(out).split(/\r?\n/)) {
            const pid = parseInt(line.trim(), 10);
            if (Number.isInteger(pid) && pid > 0 && !selfPids.has(pid)) {
                pids.push(pid);
            }
        }
        return pids;
    }

    const out = await execFileAsync('ps', ['-eo', 'pid=,args='], 2500);
    if (!out) return [];
    const pids = [];
    for (const line of String(out).split(/\r?\n/)) {
        const m = line.trim().match(/^(\d+)\s+(.*)$/);
        if (!m) continue;
        const pid = parseInt(m[1], 10);
        const cmdline = m[2] || '';
        if (Number.isInteger(pid) && pid > 0 && !selfPids.has(pid) && portRe.test(cmdline)) {
            pids.push(pid);
        }
    }
    return pids;
}

async function restartAgyHubIfNeeded({ conn, proxyUrl = '', hubPort = null, findPids = findAgyHubPids, killPid = (pid) => process.kill(pid, 'SIGTERM') } = {}) {
    const now = Date.now();
    for (let i = agyRestartTimestamps.length - 1; i >= 0; i--) {
        if ((now - agyRestartTimestamps[i]) >= 600000) {
            agyRestartTimestamps.splice(i, 1);
        }
    }

    const safePort = Number(hubPort);
    if (!conn || !conn.ok) {
        lastNetWasOk = Boolean(conn && conn.ok);
        return { restarted: false, reason: 'offline' };
    }
    if (!Number.isInteger(safePort) || safePort <= 0 || safePort > 65535) {
        lastNetWasOk = Boolean(conn.ok);
        lastAppliedProxyUrl = proxyUrl || '';
        lastAppliedCountry = conn.countryCode || '';
        lastAppliedRefreshAt = now;
        return { restarted: false, reason: 'no_port' };
    }
    if (lastAgyRestartAt > 0 && (now - lastAgyRestartAt) < 35000) {
        return {
            restarted: false,
            reason: 'cooldown',
            waitSec: Math.ceil((35000 - (now - lastAgyRestartAt)) / 1000)
        };
    }
    if (agyRestartTimestamps.length >= 2) {
        return { restarted: false, reason: 'budget' };
    }

    const pids = await findPids(safePort);
    if (!Array.isArray(pids) || pids.length === 0) {
        return { restarted: false, reason: 'not_found' };
    }

    const killed = [];
    for (const pid of pids) {
        try {
            killPid(pid);
            killed.push(pid);
        } catch (_) {}
    }

    if (killed.length === 0) {
        return { restarted: false, reason: 'kill_failed' };
    }

    lastAgyRestartAt = now;
    agyRestartTimestamps.push(now);
    lastNetWasOk = Boolean(conn.ok);
    lastAppliedProxyUrl = proxyUrl || '';
    lastAppliedCountry = conn.countryCode || '';
    lastAppliedRefreshAt = now;
    return { restarted: true, pids: killed, port: safePort };
}

/**
 * Force refresh VPN / proxy network connection: flush DNS, reset socket pool, sync proxy, respawn agy backend, reconnect chat
 */
async function refreshNetworkConnection() {
    await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Обновление сетевого соединения и прокси / VPN...',
        cancellable: false
    }, async (progress) => {
        progress.report({ message: 'Сброс DNS-кэша, пула соединений и синхронизация прокси...' });
        const eng = getQuotaEngine();
        if (eng && typeof eng.flushDnsCache === 'function') {
            await eng.flushDnsCache().catch(() => {});
        } else if (process.platform === 'win32') {
            await execFileAsync('ipconfig', ['/flushdns'], 1800);
        }

        const conn = await probeNetwork(true);
        const proxyUrl = (conn && conn.proxyUrl) || (lastProxyInfo && lastProxyInfo.url) || '';

        const agyExt = (vscode.extensions && typeof vscode.extensions.getExtension === 'function')
            ? vscode.extensions.getExtension('google.google-antigravity')
            : null;
        const hubPort = agyExt && agyExt.exports ? agyExt.exports.port : null;

        const restartRes = await restartAgyHubIfNeeded({ conn, proxyUrl, hubPort });
        if (restartRes.restarted) {
            progress.report({ message: 'Перезапуск бэкенда агента (agy) с новым VPN/прокси...' });
            await new Promise((resolve) => setTimeout(resolve, 2200));
        }

        if (vscode.commands && typeof vscode.commands.executeCommand === 'function') {
            await vscode.commands.executeCommand('antigravity.reconnect').catch(() => {});
        }

        if (conn && conn.ok && eng && typeof eng.refreshActive === 'function') {
            await eng.refreshActive({ appTarget: 'ide', force: true, minAgeSec: 0 }).catch(() => null);
        }

        progress.report({ message: 'Обновление строки состояния...' });
        await updateStatusBar({ skipNetwork: true, forceRender: true });

        const country = getCountryInfo(conn.countryCode);
        const proxyDesc = proxyUrl ? `Прокси: ${proxyUrl}` : 'Прямое подключение (без системного прокси)';
        const locDesc = conn.ok ? `${country.name} (${conn.ms} ms)` : `Соединение недоступно (${conn.status || 'офлайн'})`;
        let backendNote = '';
        if (restartRes.restarted) {
            backendNote = ' — агент (agy) перезапущен и подключён с новой сетью';
        } else if (restartRes.reason === 'cooldown') {
            backendNote = ` — выполнено переподключение чата (перезапуск agy через ${restartRes.waitSec}с)`;
        } else if (restartRes.reason === 'budget') {
            backendNote = ' — выполнено переподключение чата (лимит автоперезапуска agy на 10 мин)';
        }

        vscode.window.showInformationMessage(`🌐 Сеть обновлена: ${locDesc} — ${proxyDesc}${backendNote}`);
    });
}

/**
 * Open the latest Antigravity Language Server / IDE log directly in the editor
 */
async function openAntigravityLogs() {
    try {
        const appData = process.env.APPDATA || (process.platform === 'darwin' ? path.join(os.homedir(), 'Library', 'Application Support') : path.join(os.homedir(), '.config'));
        const baseLogs = path.join(appData, 'Antigravity IDE', 'logs');
        if (!fs.existsSync(baseLogs)) {
            vscode.window.showWarningMessage('Папка с логами Antigravity IDE не найдена (%APPDATA%\\Antigravity IDE\\logs).');
            return;
        }

        const sessions = fs.readdirSync(baseLogs)
            .map(s => path.join(baseLogs, s))
            .filter(p => {
                try { return fs.statSync(p).isDirectory(); } catch (e) { return false; }
            })
            .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);

        let targetLog = null;
        for (const session of sessions) {
            const extHostDir = path.join(session, 'window1', 'exthost');
            if (fs.existsSync(extHostDir)) {
                try {
                    const subdirs = fs.readdirSync(extHostDir)
                        .filter(d => d.startsWith('output_logging_'))
                        .map(d => path.join(extHostDir, d))
                        .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
                    for (const outDir of subdirs) {
                        const lsLog = path.join(outDir, '1-Antigravity LS.log');
                        const agLog = path.join(outDir, '2-Antigravity.log');
                        if (fs.existsSync(lsLog) && fs.statSync(lsLog).size > 0) { targetLog = lsLog; break; }
                        if (fs.existsSync(agLog) && fs.statSync(agLog).size > 0) { targetLog = agLog; break; }
                    }
                } catch (e) {}
            }
            if (targetLog) break;
            const mainLog = path.join(session, 'main.log');
            if (fs.existsSync(mainLog)) { targetLog = mainLog; break; }
        }

        if (targetLog) {
            const doc = await vscode.workspace.openTextDocument(targetLog);
            await vscode.window.showTextDocument(doc, { preview: false });
            vscode.window.showInformationMessage(`📄 Открыт актуальный лог: ${path.basename(targetLog)}`);
        } else {
            vscode.window.showInformationMessage('Логи Antigravity LS ещё не созданы в текущей сессии.');
        }
    } catch (e) {
        vscode.window.showErrorMessage(`Не удалось открыть лог: ${e.message}`);
    }
}

/**
 * Download a remote file to a local destination with redirect support
 */
function downloadFile(url, destPath) {
    return new Promise((resolve, reject) => {
        const https = require('https');
        const http = require('http');
        const parsed = new URL(url);
        const getter = parsed.protocol === 'https:' ? https : http;

        getter.get(url, { headers: { 'User-Agent': 'Antigravity-Assistant/1.2.2' } }, (res) => {
            if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
                downloadFile(res.headers.location, destPath).then(resolve).catch(reject);
                return;
            }
            if (res.statusCode !== 200) {
                reject(new Error(`HTTP status ${res.statusCode}`));
                return;
            }
            const fileStream = fs.createWriteStream(destPath);
            res.pipe(fileStream);
            fileStream.on('finish', () => {
                fileStream.close();
                resolve(destPath);
            });
            fileStream.on('error', reject);
        }).on('error', reject);
    });
}

/**
 * Compare two semver strings (returns true if remote > local)
 */
function isNewerVersion(remote, local) {
    const rParts = (remote || '').replace(/^v/i, '').split('.').map(n => parseInt(n, 10) || 0);
    const lParts = (local || '').replace(/^v/i, '').split('.').map(n => parseInt(n, 10) || 0);
    for (let i = 0; i < Math.max(rParts.length, lParts.length); i++) {
        const r = rParts[i] || 0;
        const l = lParts[i] || 0;
        if (r > l) return true;
        if (r < l) return false;
    }
    return false;
}

/**
 * Silent non-intrusive check for updates on startup
 */
function checkForUpdatesOnStartup() {
    if (_startupUpdateTimer) { clearTimeout(_startupUpdateTimer); _startupUpdateTimer = null; }
    _startupUpdateTimer = setTimeout(async () => {
        try {
            const currentVer = '1.2.2';
            const https = require('https');
            const data = await new Promise((resolve) => {
                const req = https.get('https://api.github.com/repos/EnergyStario/antigravity-patcher/releases/latest', {
                    headers: { 'User-Agent': 'Antigravity-Assistant/1.2.2' }
                }, (res) => {
                    let buf = '';
                    res.on('data', c => buf += c);
                    res.on('end', () => {
                        try { resolve(JSON.parse(buf)); } catch (e) { resolve(null); }
                    });
                });
                req.on('error', () => resolve(null));
                req.setTimeout(4000, () => { req.destroy(); resolve(null); });
            });

            if (!data || !data.tag_name) return;
            const latestTag = (data.tag_name || '').replace(/^v/i, '');

            if (latestTag && latestTag !== currentVer && isNewerVersion(latestTag, currentVer)) {
                const choice = await vscode.window.showInformationMessage(
                    `🎉 Доступно обновление Antigravity Assistant (${data.tag_name})! Обновить плагин сейчас?`,
                    'Обновить сейчас', 'Напомнить позже'
                );
                if (choice === 'Обновить сейчас') {
                    const vsixAsset = (data.assets || []).find(a => (a.name || '').endsWith('.vsix'));
                    if (vsixAsset && vsixAsset.browser_download_url) {
                        await vscode.window.withProgress({
                            location: vscode.ProgressLocation.Notification,
                            title: `Загрузка и установка ${vsixAsset.name}...`,
                            cancellable: false
                        }, async (progress) => {
                            const tempVsix = path.join(os.tmpdir(), vsixAsset.name);
                            progress.report({ message: 'Загрузка файла VSIX с GitHub...' });
                            await downloadFile(vsixAsset.browser_download_url, tempVsix);
                            progress.report({ message: 'Установка в Antigravity IDE...' });
                            await vscode.commands.executeCommand('workbench.extensions.installExtension', vscode.Uri.file(tempVsix));
                            const reload = await vscode.window.showInformationMessage(
                                `✨ Antigravity Assistant успешно обновлён до ${data.tag_name}!`,
                                'Перезагрузить окно', 'Позже'
                            );
                            if (reload === 'Перезагрузить окно') {
                                vscode.commands.executeCommand('workbench.action.reloadWindow');
                            }
                        });
                    }
                }
            }
        } catch (e) {}
    }, 3000);
    if (typeof _startupUpdateTimer.unref === 'function') _startupUpdateTimer.unref();
}

/**
 * Fast active-account refresh via pure-Node Keep-Alive engine without spawning Python
 */
async function refreshActiveQuotaBackground(optsOrForce = false) {
    const opts = (typeof optsOrForce === 'object' && optsOrForce !== null)
        ? optsOrForce
        : { force: Boolean(optsOrForce) };
    const force = Boolean(opts.force);
    const ignoreFocus = Boolean(opts.ignoreFocus);
    const minAgeSec = opts.minAgeSec !== undefined ? Number(opts.minAgeSec) : (force ? 0 : 12);

    if (!force && !ignoreFocus && !isWindowFocused()) {
        return activeRefreshPromise || null;
    }
    const now = Date.now();
    if (!force && (now - lastActiveRefreshAt) < Math.max(5000, minAgeSec * 1000)) {
        return activeRefreshPromise;
    }
    if (activeRefreshPromise) {
        if (force && !activeRefreshIsForced) {
            await activeRefreshPromise.catch(() => null);
        } else {
            return activeRefreshPromise;
        }
    }

    lastActiveRefreshAt = now;
    activeRefreshIsForced = force;
    activeRefreshPromise = (async () => {
        try {
            const eng = getQuotaEngine();
            if (eng && typeof eng.refreshActive === 'function') {
                await eng.refreshActive({ appTarget: 'ide', force, minAgeSec });
            } else {
                await runAccountSwitcherAsync('refresh_active --app ide');
            }
            await updateStatusBar({ skipNetwork: Boolean(lastNetCheckAt) });
        } catch (e) {
        } finally {
            activeRefreshPromise = null;
            activeRefreshIsForced = false;
        }
    })();
    return activeRefreshPromise;
}

/**
 * Background auto-refresh of all accounts using shared concurrent engine
 */
async function refreshQuotasBackground(optsOrForce = false) {
    const opts = (typeof optsOrForce === 'object' && optsOrForce !== null)
        ? optsOrForce
        : { force: Boolean(optsOrForce) };
    const force = Boolean(opts.force);
    const ignoreFocus = Boolean(opts.ignoreFocus);
    const minAgeSec = opts.minAgeSec !== undefined ? Number(opts.minAgeSec) : (force ? 0 : 60);

    if (!force && !ignoreFocus && !isWindowFocused()) {
        return allRefreshPromise || null;
    }
    const now = Date.now();
    if (!force && (now - lastAllRefreshAt) < Math.max(15000, minAgeSec * 1000)) {
        return allRefreshPromise || null;
    }
    if (allRefreshPromise || isRefreshingQuotas) {
        return allRefreshPromise || null;
    }
    isRefreshingQuotas = true;
    lastAllRefreshAt = now;
    allRefreshPromise = (async () => {
        try {
            const eng = getQuotaEngine();
            if (eng && typeof eng.refreshAll === 'function') {
                await eng.refreshAll({ appTarget: 'ide', concurrency: 8, force, minAgeSec });
            } else {
                await runAccountSwitcherAsync('refresh_quotas --app ide');
            }
            await updateStatusBar({ skipNetwork: Boolean(lastNetCheckAt) });
        } catch (e) {
        } finally {
            isRefreshingQuotas = false;
            allRefreshPromise = null;
        }
    })();
    return allRefreshPromise;
}

/**
 * Check for updates from GitHub releases and seamlessly install VSIX in 1-click
 */
async function checkCloudUpdates() {
    await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Проверка обновлений Antigravity Assistant...',
        cancellable: false
    }, async () => {
        try {
            const https = require('https');
            const data = await new Promise((resolve) => {
                const req = https.get('https://api.github.com/repos/EnergyStario/antigravity-patcher/releases/latest', {
                    headers: { 'User-Agent': 'Antigravity-Assistant/1.2.2' }
                }, (res) => {
                    let buf = '';
                    res.on('data', c => buf += c);
                    res.on('end', () => {
                        try { resolve(JSON.parse(buf)); } catch (e) { resolve(null); }
                    });
                });
                req.on('error', () => resolve(null));
                req.setTimeout(8000, () => { req.destroy(); resolve(null); });
            });

            if (!data || !data.tag_name) {
                vscode.window.showWarningMessage('Не удалось связаться с GitHub releases (проверьте интернет-соединение).');
                return;
            }

            const currentVer = '1.2.2';
            const latestTag = (data.tag_name || '').replace(/^v/i, '');
            if (!latestTag || !isNewerVersion(latestTag, currentVer)) {
                vscode.window.showInformationMessage(`У вас установлена актуальная версия (${data.tag_name || 'v1.2.2'})!`);
                return;
            }

            const choice = await vscode.window.showInformationMessage(
                `🎉 Доступна новая версия Antigravity Assistant: ${data.tag_name}!\nОбновить плагин автоматически?`,
                'Обновить сейчас', 'Открыть релиз', 'Отмена'
            );
            if (choice === 'Обновить сейчас') {
                const vsixAsset = (data.assets || []).find(a => (a.name || '').endsWith('.vsix'));
                if (vsixAsset && vsixAsset.browser_download_url) {
                    await vscode.window.withProgress({
                        location: vscode.ProgressLocation.Notification,
                        title: `Загрузка и установка ${vsixAsset.name}...`,
                        cancellable: false
                    }, async (progress) => {
                        const tempVsix = path.join(os.tmpdir(), vsixAsset.name);
                        progress.report({ message: 'Загрузка файла VSIX с GitHub...' });
                        await downloadFile(vsixAsset.browser_download_url, tempVsix);
                        progress.report({ message: 'Установка в Antigravity IDE...' });
                        await vscode.commands.executeCommand('workbench.extensions.installExtension', vscode.Uri.file(tempVsix));
                        const reload = await vscode.window.showInformationMessage(
                            `✨ Antigravity Assistant успешно обновлён до ${data.tag_name}!`,
                            'Перезагрузить окно', 'Позже'
                        );
                        if (reload === 'Перезагрузить окно') {
                            vscode.commands.executeCommand('workbench.action.reloadWindow');
                        }
                    });
                } else {
                    vscode.window.showWarningMessage('Файл VSIX не найден в релизе. Открываю страницу релиза...');
                    vscode.env.openExternal(vscode.Uri.parse(data.html_url || 'https://github.com/EnergyStario/antigravity-patcher/releases'));
                }
            } else if (choice === 'Открыть релиз') {
                const targetUrl = data.html_url || 'https://github.com/EnergyStario/antigravity-patcher/releases';
                vscode.env.openExternal(vscode.Uri.parse(targetUrl));
            }
        } catch (e) {
            vscode.window.showErrorMessage(`Ошибка проверки обновлений: ${e.message}`);
        }
    });
}

/**
 * Account switch menu -- Clean list with Google avatars and immediate seamless switch (0ms lag)
 */
async function showAccountSwitchMenu() {
    const appName = (vscode.env && vscode.env.appName) ? String(vscode.env.appName) : '';
    const appTarget = /visual studio code/i.test(appName) ? 'code' : 'ide';
    const eng = getQuotaEngine();
    if (eng && typeof eng.syncLiveAuth === 'function') {
        await eng.syncLiveAuth({ appTarget, minAgeSec: 10 }).catch(() => null);
    }
    refreshQuotasBackground({ minAgeSec: 15 });
    const accounts = loadAllAccountsSync();

    const items = [
        {
            label: '$(add) Добавить Google-аккаунт',
            description: `Войти через браузер и добавить в список (сейчас: ${accounts.length})`,
            detail: 'Автоматически загрузит профиль, токен, аватар и квоты Claude/Gemini',
            action: 'add_account'
        },
        {
            label: `Сохранённые аккаунты (всего: ${accounts.length})`,
            kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
        },
        ...accounts.map(acc => {
            const isCurrent = !!acc.is_active;
            const activeBadge = isCurrent ? ' [АКТИВЕН]' : '';
            const tier = acc.tier ? ` • ${acc.tier}` : '';
            const claude = acc.claude_percentage !== undefined && acc.claude_percentage !== null ? `✻ Claude: ${acc.claude_percentage}%` : '';
            const argonStr = acc.argon_percentage !== undefined && acc.argon_percentage !== null ? ` · A ${acc.argon_percentage}%` : ' · A —';
            const gemini = acc.gemini_percentage !== undefined && acc.gemini_percentage !== null ? `✦ G ${acc.gemini_percentage}%${argonStr}` : '';
            const quotaSummary = [claude, gemini].filter(Boolean).join(' | ');

            let iconPath = undefined;
            if (acc.avatar_path && fs.existsSync(acc.avatar_path)) {
                iconPath = vscode.Uri.file(acc.avatar_path);
            } else {
                iconPath = new vscode.ThemeIcon(isCurrent ? 'check' : 'account');
            }

            return {
                label: `${isCurrent ? '👉 ' : ''}${acc.name}${activeBadge}`,
                description: `${acc.email}${tier}`,
                detail: quotaSummary ? `📊 ${quotaSummary}` : '',
                iconPath: iconPath,
                account: acc
            };
        })
    ];

    const chosen = await vscode.window.showQuickPick(items, {
        placeHolder: `Выберите или добавьте Google-аккаунт (всего: ${accounts.length}):`
    });

    if (!chosen) return;

    if (chosen.action === 'add_account') {
        await vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: 'Вход в Google-аккаунт (открыт браузер)...',
            cancellable: false
        }, async () => {
            const res = await runAccountSwitcherAsync(`add_account --app ${appTarget}`, 190000);
            if (res && res.ok) {
                const label = (res.account && (res.account.email || res.account.name)) || 'аккаунт';
                updateStatusBar();
                vscode.window.showInformationMessage(`Добавлен аккаунт ${label} (всего: ${res.total_accounts || accounts.length + 1}).`);
                await showAccountSwitchMenu();
            } else {
                vscode.window.showErrorMessage(`Не удалось добавить аккаунт: ${(res && res.error) || 'таймаут или ошибка авторизации'}`);
            }
        });
        return;
    }

    const acc = chosen.account;
    if (acc.is_active) {
        vscode.window.showInformationMessage(`Аккаунт "${acc.name}" уже активен!`);
        return;
    }

    _failoverToastLastShownAt = Date.now() + 120000; // Suppress stale 429 notifications after manual switch
    try { await vscode.workspace.saveAll(false); } catch (e) {}

    const currentWorkspace = (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0)
        ? vscode.workspace.workspaceFolders[0].uri.fsPath
        : '';

    // Snapshot active conversation ID to seamlessly restore after restart.
    // Also write a resume_message so the agent knows to continue after account switch.
    const activeConvoId = getActiveConversationId();
    if (activeConvoId) {
        try {
            if (!fs.existsSync(agToolsDir)) fs.mkdirSync(agToolsDir, { recursive: true });
            const restoreFile = path.join(agToolsDir, 'restore_conversation.json');
            fs.writeFileSync(restoreFile, JSON.stringify({
                cascadeId: activeConvoId,
                timestamp: Date.now(),
                // After account switch the agent won't auto-resume — this message
                // will be injected into the chat so it knows to continue.
                resume_message: 'continue',
                resume_reason: '429_account_switch'
            }), 'utf8');
        } catch (e) {}
    }

    await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: `Переключение на аккаунт: ${acc.name}...`,
        cancellable: false
    }, async (progress) => {
        progress.report({ message: 'Обновление токенов Google и перезапуск Antigravity IDE...' });

        const targetArg = acc.id || acc.email;
        const wsArg = currentWorkspace ? ` "${currentWorkspace}"` : '';
        const appName = (vscode.env && vscode.env.appName) ? String(vscode.env.appName) : '';
        const appTarget = /visual studio code/i.test(appName) ? 'code' : 'ide';
        const res = await runAccountSwitcherAsync(`switch "${targetArg}" --app ${appTarget} --restart${wsArg}`);

        if (res && res.ok) {
            progress.report({ message: 'Credentials updated! IDE is restarting...' });
            setTimeout(() => updateStatusBar(), 4000);
            setTimeout(() => updateStatusBar(), 8000);
        } else {
            const err = (res && res.error) || 'Failed to switch account.';
            vscode.window.showErrorMessage(`Switch error: ${err}`);
        }
    });
}

function wrapResetParen(rt) {
    if (!rt) return '';
    return /^\(.*\)$/.test(rt) ? rt : `(${rt})`;
}

/**
 * Detailed quota view with clear model filter and friendly reset times
 */
async function showQuotaDetails() {
    try {
        await Promise.race([
            refreshActiveQuotaBackground({ force: true, minAgeSec: 0, ignoreFocus: true }),
            new Promise(resolve => setTimeout(resolve, 2000))
        ]);
    } catch (e) {}
    const data = getAccountData();

    if (data && data.quota) {
        const q = data.quota;
        const tier = data.tier || q.subscription_tier || 'PRO';
        const act = data.active || {};
        const effectiveClaudePct = (act.claude_percentage !== undefined && act.claude_percentage !== null)
            ? act.claude_percentage
            : (q.claude_percentage !== undefined ? q.claude_percentage : 100);
        const effectiveClaudeReset = act.claude_reset_time || q.claude_reset_time || null;
        const effectiveGeminiPct = (act.gemini_percentage !== undefined && act.gemini_percentage !== null)
            ? act.gemini_percentage
            : (q.gemini_percentage !== undefined ? q.gemini_percentage : 100);
        const effectiveGeminiReset = act.gemini_reset_time || q.gemini_reset_time || null;
        const effectiveArgonPct = (act.argon_percentage !== undefined && act.argon_percentage !== null)
            ? act.argon_percentage
            : (q.argon_percentage !== undefined ? q.argon_percentage : null);
        const effectiveArgonReset = act.argon_reset_time || q.argon_reset_time || null;

        const windowSections = [];
        const geminiWinLines = [];
        if (act.gemini_5h_percentage !== undefined && act.gemini_5h_percentage !== null) {
            const rt = wrapResetParen(formatResetTime(act.gemini_5h_reset_time, act.gemini_5h_percentage, { withIcon: false }));
            geminiWinLines.push(`\u00A0\u00A0\u00A0• 5ч лимит: ${act.gemini_5h_percentage}%${rt ? ' ' + rt : ''}`);
        }
        if (act.gemini_weekly_percentage !== undefined && act.gemini_weekly_percentage !== null) {
            const rt = wrapResetParen(formatResetTime(act.gemini_weekly_reset_time, act.gemini_weekly_percentage, { withIcon: false }));
            geminiWinLines.push(`\u00A0\u00A0\u00A0• Недельный лимит: ${act.gemini_weekly_percentage}%${rt ? ' ' + rt : ''}`);
        }
        if (geminiWinLines.length > 0) {
            windowSections.push(`✦ Gemini (Hydrogen / Flash / Pro):\n${geminiWinLines.join('\n')}`);
        }
        const argonWinLines = [];
        if (act.argon_5h_percentage !== undefined && act.argon_5h_percentage !== null) {
            const rt = wrapResetParen(formatResetTime(act.argon_5h_reset_time, act.argon_5h_percentage, { withIcon: false }));
            argonWinLines.push(`\u00A0\u00A0\u00A0• 5ч лимит: ${act.argon_5h_percentage}%${rt ? ' ' + rt : ''}`);
        }
        if (act.argon_weekly_percentage !== undefined && act.argon_weekly_percentage !== null) {
            const rt = wrapResetParen(formatResetTime(act.argon_weekly_reset_time, act.argon_weekly_percentage, { withIcon: false }));
            argonWinLines.push(`\u00A0\u00A0\u00A0• Недельный лимит: ${act.argon_weekly_percentage}%${rt ? ' ' + rt : ''}`);
        }
        if (argonWinLines.length > 0) {
            windowSections.push(`✦ Gemini 4 Argon (окна лимитов):\n${argonWinLines.join('\n')}`);
        }
        const claudeWinLines = [];
        if (act.claude_5h_percentage !== undefined && act.claude_5h_percentage !== null) {
            const rt = wrapResetParen(formatResetTime(act.claude_5h_reset_time, act.claude_5h_percentage, { withIcon: false }));
            claudeWinLines.push(`\u00A0\u00A0\u00A0• 5ч лимит: ${act.claude_5h_percentage}%${rt ? ' ' + rt : ''}`);
        }
        if (act.claude_weekly_percentage !== undefined && act.claude_weekly_percentage !== null) {
            const rt = wrapResetParen(formatResetTime(act.claude_weekly_reset_time, act.claude_weekly_percentage, { withIcon: false }));
            claudeWinLines.push(`\u00A0\u00A0\u00A0• Недельный лимит: ${act.claude_weekly_percentage}%${rt ? ' ' + rt : ''}`);
        }
        if (claudeWinLines.length > 0) {
            windowSections.push(`✻ Claude (окна лимитов):\n${claudeWinLines.join('\n')}`);
        }

        // Filter models:
        // Excluded: GPT-OSS, Gemini 3.5, Gemini 2.5 Pro, Gemini 3.1 Flash Lite, Low, Medium
        const allModels = q.models || [];
        const filteredModels = allModels.filter(m => {
            const rawName = (m.name || '').toLowerCase();
            const dispName = (m.display_name || '').toLowerCase();

            if (rawName.includes('gpt-oss') || dispName.includes('gpt-oss')) return false;
            if (rawName.includes('gemini-3.5') || dispName.includes('gemini 3.5')) return false;
            if (rawName.includes('2.5') || dispName.includes('2.5')) return false;
            if (rawName.includes('flash-lite') || rawName.includes('flash lite') ||
                dispName.includes('flash lite') || dispName.includes('flash-lite')) return false;
            if (rawName.includes('-low') || dispName.includes('(low)')) return false;
            if (rawName.includes('-medium') || dispName.includes('(medium)')) return false;

            return true;
        });

        function getModelPriority(m) {
            const name = ((m.display_name || '') + ' ' + (m.name || '')).toLowerCase();
            if (name.includes('gemini') && name.includes('3.8')) return 10;
            if (name.includes('gemini')) return 18;
            if (name.includes('claude') && name.includes('sonnet')) return 20;
            if (name.includes('claude') && name.includes('opus')) return 30;
            if (name.includes('claude')) return 40;
            return 100;
        }

        filteredModels.sort((a, b) => getModelPriority(a) - getModelPriority(b));

        if (filteredModels.length === 0) {
            filteredModels.push(
                { display_name: 'Gemini 3.8 Flash', name: 'gemini-3.8-flash', percentage: effectiveGeminiPct, reset_time: effectiveGeminiReset },
                { display_name: 'Claude Sonnet 4.6 (Thinking)', name: 'claude-sonnet-4-6', percentage: effectiveClaudePct, reset_time: effectiveClaudeReset },
                { display_name: 'Claude Opus 4.6 (Thinking)', name: 'claude-opus-4-6-thinking', percentage: effectiveClaudePct, reset_time: effectiveClaudeReset }
            );
        }
        if (effectiveArgonPct !== null && !filteredModels.some(m => ((m.name || '') + ' ' + (m.display_name || '')).toLowerCase().includes('argon'))) {
            filteredModels.push({
                display_name: 'Gemini 4 Argon',
                name: 'gemini-4-argon',
                percentage: effectiveArgonPct,
                reset_time: effectiveArgonReset,
            });
        }

        const modelLines = filteredModels.map(m => {
            const raw = ((m.name || '') + ' ' + (m.display_name || '')).toLowerCase();
            const isClaude = raw.includes('claude');
            const isArgon = !isClaude && raw.includes('argon') && effectiveArgonPct !== null;
            const isGemini = !isClaude && !isArgon && raw.includes('gemini');
            const iconPrefix = isClaude ? '✻ ' : '✦ ';
            const name = m.display_name || m.name;
            const rawPct = m.percentage !== undefined ? m.percentage : 100;
            const pctNum = isClaude ? effectiveClaudePct : (isArgon ? effectiveArgonPct : (isGemini ? effectiveGeminiPct : rawPct));
            const resetIso = isClaude ? (effectiveClaudeReset || m.reset_time) : (isArgon ? (effectiveArgonReset || m.reset_time) : (isGemini ? (effectiveGeminiReset || m.reset_time) : (m.reset_time || effectiveGeminiReset)));
            const resetInfo = formatResetTime(resetIso, pctNum, { withIcon: isClaude });
            return `${iconPrefix}${name}: ${pctNum}%${resetInfo ? ' ' + resetInfo : ''}`;
        }).join('\n');

        const windowsBlock = windowSections.length > 0
            ? `\n\n─────────────────────────\n⏱ Детализация окон (5ч / Неделя):\n${windowSections.join('\n')}`
            : '';
        const message =
`💎 Тариф аккаунта: ${tier}

📊 Лимиты нейросетей:
${modelLines || '• Нет активных моделей'}${windowsBlock}`;

        vscode.window.showInformationMessage(message, { modal: true });
    } else {
        vscode.window.showInformationMessage(
            'Информация о квотах пока загружается или аккаунт не синхронизирован с .antigravity_tools.',
            { modal: true }
        );
    }
}

// =============================================================================
// 429 FAILOVER ENGINE
// Monitors Language Server logs for RESOURCE_EXHAUSTED / 429 signals and
// shows a non-intrusive toast with actionable options based on the user policy.
// =============================================================================

/**
 * 429-trigger patterns matched against LS log lines (case-insensitive).
 * Covers all known representations from the Google Cloud Companion API.
 */
const FAILOVER_429_PATTERNS = [
    /RESOURCE_EXHAUSTED/i,
    /status[\s:=]+429/i,
    /code[\s:=]+429/i,
    /429 Too Many Requests/i,
    /quota.*exceeded/i,
    /rateLimitExceeded/i
];

/** Directories where language_server writes its logs on Windows/macOS/Linux. */
function get429WatchDirs() {
    const appData = process.env.APPDATA || path.join(homeDir, 'AppData', 'Roaming');
    const localApp = process.env.LOCALAPPDATA || path.join(homeDir, 'AppData', 'Local');
    return [
        // Antigravity IDE language_server logs
        path.join(appData, 'Antigravity IDE', 'logs'),
        path.join(localApp, 'Antigravity IDE', 'logs'),
        // CLI / agy logs
        path.join(geminiDir, 'antigravity', 'log'),
        path.join(geminiDir, 'antigravity-ide', 'log'),
        path.join(homeDir, '.config', 'antigravity', 'logs')
    ].filter(fs.existsSync);
}

/**
 * Read new tail of a log file since last known offset.
 * Returns the new content (string) and updated offset.
 */
function readLogTail(filePath) {
    try {
        const stat = fs.statSync(filePath);
        const lastOffset = _failoverLogLastOffset.get(filePath) || 0;
        if (stat.size <= lastOffset) {
            // File unchanged or truncated/rotated -- reset offset
            _failoverLogLastOffset.set(filePath, stat.size);
            return '';
        }
        const fd = fs.openSync(filePath, 'r');
        const toRead = Math.min(stat.size - lastOffset, 64 * 1024); // max 64 KB tail
        const buf = Buffer.allocUnsafe(toRead);
        fs.readSync(fd, buf, 0, toRead, lastOffset);
        fs.closeSync(fd);
        _failoverLogLastOffset.set(filePath, lastOffset + toRead);
        return buf.toString('utf8', 0, toRead);
    } catch (_) {
        return '';
    }
}

/**
 * Check if any line in logText matches a 429 pattern.
 * Also applies the same false-positive filter as diagnostics.py:
 * skip lines that look like prompt echoes ("received prompt:", etc.)
 */
function logTextHas429(logText) {
    for (const line of logText.split(/\r?\n/)) {
        // False-positive filter: skip lines that contain request/prompt echoes
        if (/received prompt:|"messages":|"content":\s*\[/i.test(line)) continue;
        for (const pat of FAILOVER_429_PATTERNS) {
            if (pat.test(line)) return true;
        }
    }
    return false;
}

/**
 * Collect recent log/txt files under dirPath (supports nested IDE session subdirs up to depth 3).
 */
function collect429CandidateFiles(dirPath, maxFiles = 6, maxAgeMs = 30 * 60 * 1000) {
    const out = [];
    const now = Date.now();
    const walk = (curDir, depth) => {
        if (depth > 5) return;
        let entries;
        try {
            entries = fs.readdirSync(curDir, { withFileTypes: true });
        } catch (_) {
            return;
        }
        const subDirs = [];
        for (const ent of entries) {
            const full = path.join(curDir, ent.name);
            try {
                if (ent.isFile() && /\.(log|txt)$/i.test(ent.name)) {
                    const st = fs.statSync(full);
                    if ((now - st.mtimeMs) <= maxAgeMs) {
                        out.push({ path: full, mtimeMs: st.mtimeMs, size: st.size });
                    }
                } else if (ent.isDirectory() && depth < 5) {
                    const st = fs.statSync(full);
                    if ((now - st.mtimeMs) <= maxAgeMs) {
                        subDirs.push({ path: full, mtimeMs: st.mtimeMs });
                    }
                }
            } catch (_) {}
        }
        subDirs.sort((a, b) => b.mtimeMs - a.mtimeMs);
        for (const sd of subDirs.slice(0, 3)) {
            walk(sd.path, depth + 1);
        }
    };
    walk(dirPath, 0);
    out.sort((a, b) => b.mtimeMs - a.mtimeMs);
    return out.slice(0, maxFiles);
}

/**
 * Seed initial offsets so historical 429 entries from closed sessions never trigger on startup.
 */
function seed429LogOffsets() {
    const dirs = get429WatchDirs();
    for (const dir of dirs) {
        const files = collect429CandidateFiles(dir, 8, 60 * 60 * 1000);
        for (const f of files) {
            if (!_failoverLogLastOffset.has(f.path)) {
                _failoverLogLastOffset.set(f.path, f.size);
            }
        }
    }
}

/**
 * Scan all recent log files in a directory (including nested session folders) for 429 signals.
 * Only reads the tail since last-known offset.
 */
function checkDirFor429(dirPath) {
    try {
        const files = collect429CandidateFiles(dirPath, 6, 30 * 60 * 1000);
        for (const item of files) {
            if (!_failoverLogLastOffset.has(item.path)) {
                // First time seeing this file after startup: scan tail up to 16KB if active recently, else seed at EOF
                const ageMs = Date.now() - item.mtimeMs;
                const seed = ageMs <= 30000 ? Math.max(0, item.size - 16384) : item.size;
                _failoverLogLastOffset.set(item.path, seed);
            }
            const tail = readLogTail(item.path);
            if (tail && logTextHas429(tail)) return true;
        }
    } catch (_) {}
    return false;
}

/**
 * Called when a log file change is detected. Checks all watched dirs
 * and triggers the failover toast if 429 is found.
 */
function onLogFileChanged() {
    const dirs = get429WatchDirs();
    for (const dir of dirs) {
        if (checkDirFor429(dir)) {
            handle429Detected();
            return;
        }
    }
}

/**
 * Set up fs.watch watchers + 10s periodic poll on LS log directories.
 * Uses debouncing to avoid hammering disk on every write.
 */
function setup429LogWatchers() {
    dispose429LogWatchers();
    seed429LogOffsets();
    const dirs = get429WatchDirs();
    for (const dir of dirs) {
        try {
            const w = fs.watch(dir, { persistent: false }, (_eventType, filename) => {
                if (!filename) return;
                // Debounce: 800 ms delay
                if (_failoverLogDebounceTimer) clearTimeout(_failoverLogDebounceTimer);
                _failoverLogDebounceTimer = setTimeout(() => {
                    _failoverLogDebounceTimer = null;
                    onLogFileChanged();
                }, 800);
            });
            w.on('error', () => {});
            _failoverLogWatchers.push(w);
        } catch (_) {}
    }
    _failoverLogPollTimer = setInterval(() => {
        if (isWindowFocused()) {
            onLogFileChanged();
        }
    }, 10000);
    if (typeof _failoverLogPollTimer.unref === 'function') _failoverLogPollTimer.unref();
}

/** Remove all log watchers. */
function dispose429LogWatchers() {
    if (_failoverLogDebounceTimer) {
        clearTimeout(_failoverLogDebounceTimer);
        _failoverLogDebounceTimer = null;
    }
    if (_failoverLogPollTimer) {
        clearInterval(_failoverLogPollTimer);
        _failoverLogPollTimer = null;
    }
    for (const w of _failoverLogWatchers) {
        try { w.close(); } catch (_) {}
    }
    _failoverLogWatchers = [];
}

let _ideGuardianMonitor = null;
let _ideGuardianTimer = null;
let _ideGuardianInFlight = false;
const _ideGuardianCooldowns = new Map();

function getIdeErrorLogDirs() {
    const appData = process.env.APPDATA || path.join(homeDir, 'AppData', 'Roaming');
    const localApp = process.env.LOCALAPPDATA || path.join(homeDir, 'AppData', 'Local');
    return [
        path.join(appData, 'Antigravity IDE', 'logs'),
        path.join(localApp, 'Antigravity IDE', 'logs'),
        path.join(geminiDir, 'antigravity-ide', 'log'),
    ].filter(fs.existsSync);
}

async function runIdeScopedHeal(alert, isAuto = false) {
    if (_ideGuardianInFlight) return null;
    const guard = getErrorGuardian();
    if (!guard || typeof guard.runScopedHeal !== 'function') return null;

    _ideGuardianInFlight = true;
    try {
        const res = await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: isAuto
                    ? `🛡 Авто-починка IDE (${alert.title})...`
                    : `🔧 Исправляю IDE (${alert.title})...`,
                cancellable: false,
            },
            () => guard.runScopedHeal({ target: 'ide', force: true, allowRestart: true, timeoutMs: 45000 })
        );
        if (res && res.ok) {
            const actText = Array.isArray(res.actions) && res.actions.length
                ? ` (${res.actions.join(', ')})`
                : '';
            vscode.window.showInformationMessage(`✓ Antigravity IDE восстановлен${actText}`);
        } else {
            const msg = (res && (res.diagnosis || res.error)) || 'Не удалось применить исправление.';
            const retry = await vscode.window.showWarningMessage(
                `⚠ ${alert.title}: ${msg}`,
                '🔧 Починить'
            );
            if (retry === '🔧 Починить') {
                setTimeout(() => { void runIdeScopedHeal(alert, false); }, 50);
            }
        }
        return res;
    } finally {
        _ideGuardianInFlight = false;
    }
}

async function handleIdeGuardianAlert(alert) {
    if (!alert || !alert.key) return;
    if (!vscode || !vscode.window || typeof vscode.window.showWarningMessage !== 'function') return;
    const now = Date.now();
    const lastTs = _ideGuardianCooldowns.get(alert.key) || 0;
    if ((now - lastTs) < 120000) return;
    _ideGuardianCooldowns.set(alert.key, now);

    const guard = getErrorGuardian();
    const cfg = guard && typeof guard.loadAutoHealConfig === 'function'
        ? guard.loadAutoHealConfig()
        : { enabled: true };
    const autoOn = Boolean(cfg && cfg.enabled !== false);

    if (autoOn) {
        const btnFixNow = '🔧 Починить сейчас';
        const btnDisableAuto = '⏸ Выкл. авто-режим';
        vscode.window.showWarningMessage(
            `🛡 [Авто-режим] Ошибка в Antigravity IDE — ${alert.title}. Исправляю автоматически...`,
            btnFixNow,
            btnDisableAuto
        ).then((choice) => {
            if (choice === btnFixNow) {
                void runIdeScopedHeal(alert, false);
            } else if (choice === btnDisableAuto && guard && typeof guard.saveAutoHealConfig === 'function') {
                guard.saveAutoHealConfig({ enabled: false });
                vscode.window.showInformationMessage('Авто-режим исправления выключен (остаются мини-окна с кнопкой «Починить»).');
            }
        });
        await runIdeScopedHeal(alert, true);
        return;
    }

    const btnFix = '🔧 Починить';
    const btnAutoOn = '⚡ Включить Авто-режим';
    const choice = await vscode.window.showErrorMessage(
        `⚠ ${alert.title}: ${alert.description}`,
        btnFix,
        btnAutoOn,
        'Скрыть'
    );
    if (choice === btnFix) {
        await runIdeScopedHeal(alert, false);
    } else if (choice === btnAutoOn && guard && typeof guard.saveAutoHealConfig === 'function') {
        guard.saveAutoHealConfig({ enabled: true });
        await runIdeScopedHeal(alert, true);
    }
}

function setupIdeErrorGuardian() {
    disposeIdeErrorGuardian();
    const guard = getErrorGuardian();
    if (!guard || typeof guard.createLogErrorMonitor !== 'function') return;
    const dirs = getIdeErrorLogDirs();
    if (!dirs.length) return;
    _ideGuardianMonitor = guard.createLogErrorMonitor({
        logDirs: dirs,
        cooldownMs: 120000,
        onError: (alert) => {
            void handleIdeGuardianAlert(alert);
        },
    });
    _ideGuardianMonitor.scanNow();
    _ideGuardianTimer = setInterval(() => {
        if (!isWindowFocused() || !_ideGuardianMonitor) return;
        _ideGuardianMonitor.scanNow();
    }, 15000);
    if (typeof _ideGuardianTimer.unref === 'function') _ideGuardianTimer.unref();
}

function disposeIdeErrorGuardian() {
    if (_ideGuardianTimer) {
        clearInterval(_ideGuardianTimer);
        _ideGuardianTimer = null;
    }
    _ideGuardianMonitor = null;
}

/**
 * Core handler: called when a 429 signal is detected.
 * Reads the configured policy and either auto-acts or shows a toast.
 */
async function handle429Detected() {
    if (!vscode || !vscode.window || typeof vscode.window.showWarningMessage !== 'function') return;
    const now = Date.now();
    // Throttle: max 1 toast per 60 seconds
    if ((now - _failoverToastLastShownAt) < 60000) return;
    _failoverToastLastShownAt = now;

    const eng = getQuotaEngine();
    const sharedSettings = (eng && typeof eng.loadAssistantSettings === 'function')
        ? (eng.loadAssistantSettings() || {}) : {};
    const cfg = vscode.workspace.getConfiguration('agToolkit');
    const policy = cfg.get('failoverPolicy') || sharedSettings.failoverPolicy || 'interactive';

    // Determine current model from workspace config (best-effort)
    const currentModel = cfg.get('currentModel') || 'claude';

    if (policy === 'account_hop') {
        await doAutoAccountHop(currentModel);
        return;
    }
    if (policy === 'model_fallback') {
        await doAutoModelFallback();
        return;
    }

    // Interactive: show smart cascade prompt / 3-button toast
    await show429Toast(currentModel);
}

function _getQuotaExhaustionPlan(currentModel) {
    try {
        const eng = getQuotaEngine();
        if (!eng || typeof eng.resolveQuotaExhaustionPlan !== 'function') return null;
        const accountData = getAccountData();
        const currentId = (accountData.active && accountData.active.id) || '';
        return eng.resolveQuotaExhaustionPlan({ currentModel, currentAccountId: currentId });
    } catch (_) {
        return null;
    }
}

/**
 * Show failover toast tailored to remaining smart quotas:
 *   - When smart Tier-1 quota exists on other account/pool -> offer Account / Tier-1 / Flash
 *   - When ALL smart quotas hit 0% everywhere -> warn explicitly & offer Degraded Swarm Mode (Flash + strict guardrails)
 */
async function show429Toast(currentModel) {
    const modelLabel = currentModel ? `(${currentModel})` : '';
    const plan = _getQuotaExhaustionPlan(currentModel);

    if (plan && plan.strategy === 'degraded_swarm') {
        const btnDegraded = '🛡 Резервный режим роя (Flash + строгий план)';
        const btnManual = '🔀 Переключить аккаунт вручную';
        const btnSettings = '⚙ Настроить';
        const choice = await vscode.window.showWarningMessage(
            '⚠️ Умные модели (Tier-1) исчерпаны (0%) на всех аккаунтах! Включить резервный режим (Flash по 1–2 файла + обязательные тесты), чтобы рой не встал?',
            btnDegraded, btnManual, btnSettings
        );
        if (!choice) return;
        if (choice === btnDegraded) {
            await doAutoModelFallback(plan.model || 'gemini-2.5-flash', true);
        } else if (choice === btnManual) {
            await showAccountSwitchMenu();
        } else if (choice === btnSettings) {
            await show429PolicyMenu();
        }
        return;
    }

    const btn1 = `🔀 Сменить аккаунт ${modelLabel}`;
    const btn2 = '🧠 Другой Tier-1 (Gemini Pro)';
    const btn3 = '⚡ Flash (безлимит)';
    const btnSettings = '⚙ Настроить';

    const hint = (plan && plan.reason) ? ` ${plan.reason}.` : '';
    const choice = await vscode.window.showWarningMessage(
        `⚠️ Лимит квоты исчерпан (429)!${hint} Выберите действие:`,
        btn1, btn2, btn3, btnSettings
    );

    if (!choice) return;
    if (choice === btn1) {
        await doAutoAccountHop(currentModel);
    } else if (choice === btn2) {
        await doSwitchToAnotherTier1(currentModel);
    } else if (choice === btn3) {
        await doAutoModelFallback();
    } else if (choice === btnSettings) {
        await show429PolicyMenu();
    }
}

function _saveAutoResumeConversation(currentModel) {
    const activeConvoId = getActiveConversationId();
    if (!activeConvoId) return;
    try {
        if (!fs.existsSync(agToolsDir)) fs.mkdirSync(agToolsDir, { recursive: true });
        const restoreFile = path.join(agToolsDir, 'restore_conversation.json');
        fs.writeFileSync(restoreFile, JSON.stringify({
            cascadeId: activeConvoId,
            timestamp: Date.now(),
            resume_message: 'continue',
            resume_reason: '429_account_hop',
            auto_send_resume: true,
            model: currentModel || 'claude'
        }), 'utf8');
    } catch (_) {}
}

function _buildSwitchRestartArgs(targetArg) {
    const currentWorkspace = (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0)
        ? vscode.workspace.workspaceFolders[0].uri.fsPath
        : '';
    const appName = (vscode.env && vscode.env.appName) ? String(vscode.env.appName) : '';
    const appTarget = /visual studio code/i.test(appName) ? 'code' : 'ide';
    const args = ['switch', '--target', String(targetArg), '--app', appTarget, '--restart'];
    if (currentWorkspace) {
        args.push('--workspace', currentWorkspace);
    }
    return args;
}

/**
 * 3-step survival failover:
 *   1) Hop to another account with remaining smart quota (>0%)
 *   2) Switch to another Tier-1 smart model pool (e.g. Gemini 2.5 Pro / Argon)
 *   3) Activate degraded Swarm mode on Flash (1-2 files + strict tests) so Swarm never stalls
 */
async function doAutoAccountHop(currentModel) {
    try {
        const eng = getQuotaEngine();
        if (!eng) {
            vscode.window.showWarningMessage(
                '429 Failover: quota_engine не загружен. Откройте меню → Переключить аккаунт вручную.'
            );
            return;
        }
        const plan = _getQuotaExhaustionPlan(currentModel);
        if (plan && plan.strategy === 'account_hop' && plan.account) {
            const targetArg = plan.account.id || plan.account.email;
            _saveAutoResumeConversation(currentModel);
            await runAccountSwitcherAsync(_buildSwitchRestartArgs(targetArg));
            vscode.window.showInformationMessage(
                `✅ 429 Failover: переключено на аккаунт ${plan.account.name || plan.account.email} (${Math.round(plan.remainingPct)}% квоты).`
            );
            return;
        }
        if (plan && plan.strategy === 'tier1_model') {
            if (plan.switchAccount && plan.account) {
                const targetArg = plan.account.id || plan.account.email;
                _saveAutoResumeConversation(plan.model || currentModel);
                await runAccountSwitcherAsync(_buildSwitchRestartArgs(targetArg));
            }
            await vscode.workspace.getConfiguration('agToolkit').update(
                'currentModel', plan.model, vscode.ConfigurationTarget.Global
            );
            const accNote = plan.switchAccount && plan.account ? ` на аккаунте ${plan.account.name || plan.account.email}` : '';
            vscode.window.showInformationMessage(
                `🧠 Пул ${currentModel} исчерпан — переключаю на другую умную модель Tier-1: ${plan.model}${accNote} (${Math.round(plan.remainingPct)}%). Выберите её в чате.`
            );
            return;
        }
        if (plan && plan.strategy === 'degraded_swarm') {
            if (plan.switchAccount && plan.account) {
                const targetArg = plan.account.id || plan.account.email;
                _saveAutoResumeConversation(plan.model || 'gemini-2.5-flash');
                await runAccountSwitcherAsync(_buildSwitchRestartArgs(targetArg));
            }
            await doAutoModelFallback(plan.model || 'gemini-2.5-flash', true);
            return;
        }

        // Legacy fallback if resolveQuotaExhaustionPlan is unavailable
        const accountData = getAccountData();
        const currentId = (accountData.active && accountData.active.id) || '';
        const candidates = typeof eng.getBestAccountsForModel === 'function'
            ? eng.getBestAccountsForModel(currentModel, currentId)
            : [];
        if (!candidates || candidates.length === 0) {
            await doAutoModelFallback('gemini-2.5-flash', true);
            return;
        }
        const best = candidates[0];
        const targetArg = best.id || best.email;
        _saveAutoResumeConversation(currentModel);
        await runAccountSwitcherAsync(_buildSwitchRestartArgs(targetArg));
        vscode.window.showInformationMessage(
            `✅ 429 Failover: переключено на аккаунт ${best.name || best.email} (${Math.round(best.claude_percentage || best.gemini_percentage || 0)}% квоты).`
        );
    } catch (e) {
        vscode.window.showErrorMessage(`429 Failover (account_hop) ошибка: ${e.message}`);
    }
}

/**
 * Suggest switching to another Tier-1 smart model (ordered by getOrchestratorOrder).
 */
async function doSwitchToAnotherTier1(currentModel) {
    const eng = getQuotaEngine();
    const order = (eng && typeof eng.getOrchestratorOrder === 'function')
        ? eng.getOrchestratorOrder()
        : ['claude-sonnet-4', 'claude-opus-4', 'gpt-oss-120b', 'gemini-2.5-pro'];
    const labels = {
        'claude-sonnet-4': 'Claude Sonnet 4 (Thinking)',
        'claude-opus-4': 'Claude Opus 4 (Thinking)',
        'gpt-oss-120b': 'GPT-OSS 120B (Smart Tier-1)',
        'gemini-2.5-pro': 'Gemini 2.5 Pro (отдельный пул Gemini)'
    };
    const lowCur = String(currentModel || '').toLowerCase();
    const tier1Models = order
        .filter((m) => (!eng || typeof eng.getModelTier !== 'function' || eng.getModelTier(m) !== 'fast'))
        .filter((m) => !lowCur.includes(String(m).toLowerCase()))
        .map((m) => ({
            label: `$(sparkle) ${labels[m] || m}`,
            description: m.includes('gemini')
                ? 'Умная Tier-1 (Gemini-пул — работает даже если Claude = 0%)'
                : 'Умная Tier-1',
            model: m
        }));

    if (tier1Models.length === 0) {
        vscode.window.showWarningMessage('Нет других Tier-1 моделей для переключения.');
        return;
    }

    const choice = await vscode.window.showQuickPick(tier1Models, {
        placeHolder: 'Выберите другую мощную модель (Tier-1)'
    });
    if (!choice) return;

    await vscode.workspace.getConfiguration('agToolkit').update(
        'currentModel', choice.model, vscode.ConfigurationTarget.Global
    );
    vscode.window.showInformationMessage(
        `✅ Модель переключена на ${choice.label}. Смените модель в чате и продолжайте работу.`
    );
}

/**
 * Auto: switch to Flash (unlimited quota / degraded Swarm survival mode).
 */
async function doAutoModelFallback(targetModel = 'gemini-2.5-flash', degraded = false) {
    const modelName = targetModel || 'gemini-2.5-flash';
    await vscode.workspace.getConfiguration('agToolkit').update(
        'currentModel', modelName, vscode.ConfigurationTarget.Global
    );
    if (degraded) {
        vscode.window.showWarningMessage(
            `🛡 Все умные (Tier-1) модели исчерпаны — активирован резервный режим роя на ${modelName}: малые шаги (1–2 файла) + обязательные тесты, чтобы не простаивать. Переключите модель в чате.`
        );
    } else {
        vscode.window.showInformationMessage(
            `⚡ Резерв квоты: переключено на ${modelName}. Смените модель в панели чата и продолжайте!`
        );
    }
}

/**
 * Policy settings menu: let user choose how to react to future quota exhaustion events.
 */
async function show429PolicyMenu() {
    const eng = getQuotaEngine();
    const sharedSettings = (eng && typeof eng.loadAssistantSettings === 'function')
        ? (eng.loadAssistantSettings() || {}) : {};
    const currentPolicy = vscode.workspace.getConfiguration('agToolkit').get('failoverPolicy')
        || sharedSettings.failoverPolicy
        || 'interactive';
    const policies = [
        {
            label: currentPolicy === 'interactive' ? '$(check) Спрашивать каждый раз (рекомендуется)' : 'Спрашивать каждый раз (рекомендуется)',
            description: 'Умные модели защищены — при 429 спросим: другой аккаунт, другая Tier-1 или резерв Flash',
            policy: 'interactive'
        },
        {
            label: currentPolicy === 'account_hop' ? '$(check) Авто: смена аккаунта + то же окно + «continue»' : 'Авто: смена аккаунта + то же окно + «continue»',
            description: 'При 429 на Claude: сменить аккаунт, открыть то же окно/модель и отправить continue (при 0% везде → Flash)',
            policy: 'account_hop'
        },
        {
            label: currentPolicy === 'model_fallback' ? '$(check) Авто: сразу перейти на быстрый Flash' : 'Авто: сразу перейти на быстрый Flash',
            description: 'Максимальная скорость, без остановки роя (простое планирование + обязательные тесты)',
            policy: 'model_fallback'
        }
    ];

    const choice = await vscode.window.showQuickPick(policies, {
        placeHolder: 'Режим при исчерпании квоты (умные модели защищены по умолчанию)'
    });
    if (!choice) return;

    await vscode.workspace.getConfiguration('agToolkit').update(
        'failoverPolicy', choice.policy, vscode.ConfigurationTarget.Global
    );
    if (eng && typeof eng.saveAssistantSettings === 'function') {
        await eng.saveAssistantSettings({ failoverPolicy: choice.policy }).catch(() => {});
    }
    vscode.window.showInformationMessage(
        `✅ Режим при исчерпании квоты сохранён: "${choice.label.replace(/^\$\([^)]+\)\s*/, '')}"`
    );
}

// =============================================================================
// MODEL CONFIG MANAGER
// Allows users to add custom models (via Delegator or any source),
// override their tier (heavy/fast), set role preference, and configure
// the orchestrator priority order. All settings persist to
// ~/.antigravity_tools/model_config.json — user config always wins over defaults.
// =============================================================================

/**
 * Load current model_config.json via quota_engine helper.
 * Returns {} if file doesn't exist or engine not loaded.
 */
function loadModelConfigSync() {
    const eng = getQuotaEngine();
    if (eng && typeof eng.loadModelConfig === 'function') {
        return eng.loadModelConfig() || {};
    }
    // Fallback: direct read
    try {
        const cfgFile = path.join(agToolsDir, 'model_config.json');
        return JSON.parse(fs.readFileSync(cfgFile, 'utf8')) || {};
    } catch (_) {
        return {};
    }
}

/**
 * Save model_config.json via quota_engine helper.
 */
async function saveModelConfigAsync(config) {
    const eng = getQuotaEngine();
    if (eng && typeof eng.saveModelConfig === 'function') {
        return eng.saveModelConfig(config);
    }
    // Fallback: direct atomic write
    const cfgFile = path.join(agToolsDir, 'model_config.json');
    const tmpFile = cfgFile + '.' + Date.now() + '.tmp';
    fs.writeFileSync(tmpFile, JSON.stringify(config, null, 2), 'utf8');
    fs.renameSync(tmpFile, cfgFile);
    return true;
}

/**
 * Main model configuration menu.
 * Shows current custom models, default overrides, and orchestrator order.
 */
async function showModelConfigMenu() {
    const cfg = loadModelConfigSync();
    const customModels = cfg.custom_models || {};
    const tierOverrides = cfg.tier_overrides || {};
    const orchestratorOrder = cfg.orchestrator_order || [];
    const workerModel = cfg.worker_model || 'flash';

    const customCount = Object.keys(customModels).length;
    const overrideCount = Object.keys(tierOverrides).length;

    const items = [
        {
            label: `$(add) Добавить пользовательскую модель`,
            description: `Для дополнительных моделей (сейчас: ${customCount})`,
            action: 'add_custom'
        },
        {
            label: `$(edit) Назначить «умная / быстрая» встроенной модели`,
            description: `Изменить роль модели при нехватке квоты (сейчас: ${overrideCount} переопределений)`,
            action: 'override_tier'
        },
        {
            label: `$(list-ordered) Какие модели могут планировать задачи (Архитектор)`,
            description: orchestratorOrder.length
                ? `Приоритет: ${orchestratorOrder.slice(0, 3).join(' → ')}${orchestratorOrder.length > 3 ? ' …' : ''}`
                : 'Только умные (Claude → Opus → GPT-OSS…) — глупые модели не планируют',
            action: 'orchestrator_order'
        },
        {
            label: `$(zap) Модель для исполнителей (воркеров): ${workerModel}`,
            description: 'По умолчанию быстрая Flash — можно выбрать другую модель',
            action: 'worker_model'
        },
        {
            label: `$(trash) Сбросить настройки моделей на стандартные`,
            description: 'Вернуть встроенные правила Antigravity (удалить ручные правки)',
            action: 'reset'
        }
    ];

    if (customCount > 0) {
        items.splice(2, 0, {
            label: `$(list-unordered) Просмотр / удаление сторонних моделей`,
            description: Object.keys(customModels).join(', '),
            action: 'list_custom'
        });
    }

    const selected = await vscode.window.showQuickPick(items, {
        placeHolder: 'Доп. настройки моделей (для стандартных Claude/Gemini/Flash менять не нужно)'
    });
    if (!selected) return;

    switch (selected.action) {
        case 'add_custom':
            await addCustomModelWizard(cfg);
            break;
        case 'override_tier':
            await overrideTierWizard(cfg);
            break;
        case 'orchestrator_order':
            await editOrchestratorOrderWizard(cfg);
            break;
        case 'worker_model':
            await editWorkerModelWizard(cfg);
            break;
        case 'list_custom':
            await listCustomModelsMenu(cfg);
            break;
        case 'reset':
            await resetModelConfig();
            break;
    }
}

/**
 * Wizard to add a new custom model.
 */
async function addCustomModelWizard(cfg) {
    // Step 1: Model ID
    const modelId = await vscode.window.showInputBox({
        prompt: 'Введи ID или имя модели',
        placeHolder: 'например: custom-llm, deepseek-r2, qwen-72b',
        validateInput: (v) => v && v.trim() ? null : 'ID не может быть пустым'
    });
    if (!modelId) return;

    // Step 2: Display name
    const displayName = await vscode.window.showInputBox({
        prompt: 'Отображаемое имя (для меню)',
        placeHolder: `например: Custom LLM`,
        value: modelId
    });

    // Step 3: Tier
    const tierChoice = await vscode.window.showQuickPick([
        {
            label: '$(symbol-event) Heavy (умная, ограниченная квота)',
            description: 'Как Claude, GPT-4 — будет использоваться как Orchestrator',
            tier: 'heavy'
        },
        {
            label: '$(zap) Fast (быстрая, большая квота)',
            description: 'Как Flash — будет использоваться как Worker/Verifier',
            tier: 'fast'
        }
    ], { placeHolder: 'Выбери тир квоты для этой модели' });
    if (!tierChoice) return;

    // Step 4: Role preference
    const roleChoice = await vscode.window.showQuickPick([
        { label: '$(account) Orchestrator — архитектор задачи',  role: 'orchestrator' },
        { label: '$(tools) Worker/Verifier — исполнитель',        role: 'worker' },
        { label: '$(symbol-misc) Любая роль',                     role: 'any' }
    ], { placeHolder: 'Предпочтительная роль этой модели в swarm' });
    if (!roleChoice) return;

    // Save
    const updated = { ...cfg };
    if (!updated.custom_models) updated.custom_models = {};
    const key = modelId.trim().toLowerCase();
    updated.custom_models[key] = {
        tier: tierChoice.tier,
        display_name: (displayName || modelId).trim(),
        role_preference: roleChoice.role
    };

    // If heavy + orchestrator → offer to prepend to orchestrator_order
    if (tierChoice.tier === 'heavy' && roleChoice.role !== 'worker') {
        const addToOrder = await vscode.window.showInformationMessage(
            `Добавить "${displayName || modelId}" в начало очереди Orchestrator?`,
            'Да, первым', 'Нет'
        );
        if (addToOrder === 'Да, первым') {
            if (!updated.orchestrator_order) updated.orchestrator_order = [];
            updated.orchestrator_order = [key, ...updated.orchestrator_order.filter((m) => m !== key)];
        }
    }

    await saveModelConfigAsync(updated);
    vscode.window.showInformationMessage(
        `✅ Модель "${displayName || modelId}" добавлена как ${tierChoice.tier} (${roleChoice.role}).`
    );
}

/**
 * Wizard to override the tier of a built-in model.
 */
async function overrideTierWizard(cfg) {
    const modelId = await vscode.window.showInputBox({
        prompt: 'Введи ID встроенной модели для переопределения тира',
        placeHolder: 'например: gemini-pro, claude-sonnet, gpt-oss-120b'
    });
    if (!modelId) return;

    const tierChoice = await vscode.window.showQuickPick([
        { label: '$(symbol-event) Heavy', tier: 'heavy' },
        { label: '$(zap) Fast',           tier: 'fast' },
        { label: '$(trash) Удалить переопределение (вернуть дефолт)', tier: '__remove__' }
    ], { placeHolder: `Новый тир для "${modelId}"` });
    if (!tierChoice) return;

    const updated = { ...cfg };
    if (!updated.tier_overrides) updated.tier_overrides = {};
    const key = modelId.trim().toLowerCase();

    if (tierChoice.tier === '__remove__') {
        delete updated.tier_overrides[key];
        vscode.window.showInformationMessage(`Переопределение тира для "${modelId}" удалено.`);
    } else {
        updated.tier_overrides[key] = tierChoice.tier;
        vscode.window.showInformationMessage(
            `✅ Тир "${modelId}" установлен: ${tierChoice.tier}`
        );
    }

    await saveModelConfigAsync(updated);
}

/**
 * Wizard to view and delete custom models.
 */
async function listCustomModelsMenu(cfg) {
    const customModels = cfg.custom_models || {};
    const entries = Object.entries(customModels).map(([id, def]) => ({
        label: `$(symbol-misc) ${def.display_name || id}`,
        description: `tier: ${def.tier || '?'} | роль: ${def.role_preference || 'any'} | id: ${id}`,
        id
    }));

    if (entries.length === 0) {
        vscode.window.showInformationMessage('Нет пользовательских моделей.');
        return;
    }

    const selected = await vscode.window.showQuickPick(entries, {
        placeHolder: 'Выбери модель для удаления (или Escape для отмены)'
    });
    if (!selected) return;

    const confirm = await vscode.window.showWarningMessage(
        `Удалить модель "${selected.label.replace(/^\$\([^)]+\)\s*/, '')}"?`,
        'Удалить', 'Отмена'
    );
    if (confirm !== 'Удалить') return;

    const updated = { ...cfg };
    delete updated.custom_models[selected.id];
    // Also remove from orchestrator_order if present
    if (Array.isArray(updated.orchestrator_order)) {
        updated.orchestrator_order = updated.orchestrator_order.filter((m) => m !== selected.id);
    }
    await saveModelConfigAsync(updated);
    vscode.window.showInformationMessage(`Модель "${selected.id}" удалена.`);
}

/**
 * Wizard to edit the orchestrator model priority order.
 */
async function editOrchestratorOrderWizard(cfg) {
    const eng = getQuotaEngine();
    const currentOrder = Array.isArray(cfg.orchestrator_order) && cfg.orchestrator_order.length > 0
        ? cfg.orchestrator_order
        : (eng && typeof eng.getOrchestratorOrder === 'function' ? eng.getOrchestratorOrder() : []);

    const preview = currentOrder.length
        ? currentOrder.join(' → ')
        : '(дефолт: Claude Sonnet → Opus → GPT-OSS…)';

    const choice = await vscode.window.showQuickPick([
        {
            label: '$(list-ordered) Задать новый порядок',
            description: `Текущий: ${preview.slice(0, 80)}`,
            action: 'edit'
        },
        {
            label: '$(trash) Сбросить на дефолтный порядок',
            description: 'Claude Sonnet 4.6 → Opus 4.6 → GPT-OSS 120B → …',
            action: 'reset'
        }
    ], { placeHolder: 'Порядок Orchestrator-моделей (первая = приоритетная)' });
    if (!choice) return;

    if (choice.action === 'reset') {
        const updated = { ...cfg };
        delete updated.orchestrator_order;
        await saveModelConfigAsync(updated);
        vscode.window.showInformationMessage('Порядок Orchestrator сброшен на дефолт.');
        return;
    }

    const newOrderStr = await vscode.window.showInputBox({
        prompt: 'Введи модели через запятую (первая = приоритетная)',
        value: currentOrder.join(', '),
        placeHolder: 'claude-sonnet-4, claude-opus-4, gpt-oss-120b'
    });
    if (!newOrderStr) return;

    const newOrder = newOrderStr.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
    const updated = { ...cfg, orchestrator_order: newOrder };
    await saveModelConfigAsync(updated);
    vscode.window.showInformationMessage(
        `✅ Порядок Orchestrator обновлён: ${newOrder.join(' → ')}`
    );
}

/**
 * Wizard to change the preferred worker/verifier model.
 */
async function editWorkerModelWizard(cfg) {
    const current = cfg.worker_model || 'flash';
    const newModel = await vscode.window.showInputBox({
        prompt: 'Предпочтительная модель для Worker и Verifier',
        value: current,
        placeHolder: 'flash | gemini-3.8-flash'
    });
    if (!newModel || !newModel.trim()) return;

    const updated = { ...cfg, worker_model: newModel.trim().toLowerCase() };
    await saveModelConfigAsync(updated);
    vscode.window.showInformationMessage(`✅ Worker/Verifier модель: "${newModel.trim()}"`);
}

/**
 * Reset model_config.json (delete file → revert to built-in defaults).
 */
async function resetModelConfig() {
    const confirm = await vscode.window.showWarningMessage(
        'Сбросить все пользовательские настройки моделей на дефолт?',
        'Сбросить', 'Отмена'
    );
    if (confirm !== 'Сбросить') return;

    try {
        const cfgFile = path.join(agToolsDir, 'model_config.json');
        if (fs.existsSync(cfgFile)) {
            fs.unlinkSync(cfgFile);
        }
        // Clear quota_engine cache
        const eng = getQuotaEngine();
        if (eng && typeof eng.loadModelConfig === 'function') {
            eng.loadModelConfig(); // re-read (will get empty now)
        }
        vscode.window.showInformationMessage('✅ Настройки моделей сброшены на встроенные дефолты.');
    } catch (e) {
        vscode.window.showErrorMessage(`Ошибка сброса: ${e.message}`);
    }
}

// ---------------------------------------------------------------------------
// Multi-Agent Swarm Engine Integration (Shared swarm_bridge.js + Antigravity Chat)
// ---------------------------------------------------------------------------

function _getWorkspacePaths() {
    const folders = (vscode.workspace && vscode.workspace.workspaceFolders) || [];
    return folders.map((f) => f && f.uri && f.uri.fsPath).filter(Boolean);
}

/** Return the checkpoint storage directory (~/.antigravity_tools/swarm). */
function getSwarmStateDir() {
    const bridge = getSwarmBridge();
    return bridge ? bridge.getSwarmStateDir() : path.join(process.env.AG_TOOLS_DIR || agToolsDir, 'swarm');
}

/**
 * Resolve directory that contains `swarm/ipc_server.py` (for PYTHONPATH).
 * Delegates to unified `swarm_bridge.js`.
 * @param {string} [workspacePath]
 * @returns {string|null}
 */
function resolveSwarmEnginePath(workspacePath) {
    const bridge = getSwarmBridge();
    if (!bridge) return null;
    const ws = [];
    if (workspacePath) ws.push(workspacePath);
    ws.push(..._getWorkspacePaths());
    return bridge.resolveSwarmEnginePath(ws, [__dirname]);
}

function getSwarmOutputChannel() {
    if (!_swarmOutputChannel && vscode.window && typeof vscode.window.createOutputChannel === 'function') {
        _swarmOutputChannel = vscode.window.createOutputChannel('Antigravity Swarm');
    }
    return _swarmOutputChannel;
}

function logSwarm(line) {
    const ch = getSwarmOutputChannel();
    if (ch) {
        ch.appendLine(`[${new Date().toLocaleTimeString()}] ${line}`);
    }
}

function updateSwarmStatusBar(text, tooltip) {
    if (!_swarmStatusBar || _swarmStatusBar === statusBarItem) return;
    if (!text) {
        updateSwarmStatusBarLive();
        return;
    }
    _swarmStatusBar.text = text;
    if (tooltip) {
        if (vscode.MarkdownString && typeof tooltip === 'object' && tooltip instanceof vscode.MarkdownString) {
            _swarmStatusBar.tooltip = tooltip;
        } else if (vscode.MarkdownString) {
            const md = new vscode.MarkdownString(String(tooltip));
            md.isTrusted = true;
            _swarmStatusBar.tooltip = md;
        } else {
            _swarmStatusBar.tooltip = tooltip;
        }
    }
    try { _swarmStatusBar.show(); } catch (_) {}
}

function _scheduleSwarmLiveTick() {
    if (_swarmTickTimer) {
        clearTimeout(_swarmTickTimer);
        _swarmTickTimer = null;
    }
    if (!_swarmTickRunning) return;
    const isActive = _swarmLastActive > 0 || Boolean(_swarmTaskId) || Boolean(_swarmProcess);
    const delayMs = isActive ? 1000 : 15000;
    _swarmTickTimer = setTimeout(() => {
        _swarmTickTimer = null;
        if (!_swarmTickRunning) return;
        updateSwarmStatusBarLive();
        _scheduleSwarmLiveTick();
    }, delayMs);
    if (typeof _swarmTickTimer.unref === 'function') _swarmTickTimer.unref();
}

function startSwarmLiveTicker() {
    stopSwarmLiveTicker();
    _swarmTickRunning = true;
    updateSwarmStatusBarLive();
    _scheduleSwarmLiveTick();
}

function stopSwarmLiveTicker() {
    _swarmTickRunning = false;
    if (_swarmTickTimer) {
        clearTimeout(_swarmTickTimer);
        _swarmTickTimer = null;
    }
}

function _pluralRu(n, one, few, many) {
    const abs = Math.abs(Number(n) || 0);
    const mod10 = abs % 10;
    const mod100 = abs % 100;
    if (mod100 >= 11 && mod100 <= 19) return `${n} ${many}`;
    if (mod10 === 1) return `${n} ${one}`;
    if (mod10 >= 2 && mod10 <= 4) return `${n} ${few}`;
    return `${n} ${many}`;
}

function _formatElapsedTimer(secOrMs) {
    let sec = Number(secOrMs) || 0;
    if (sec > 100000) sec = Math.round(sec / 1000);
    sec = Math.max(0, Math.round(sec));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    if (m >= 60) {
        const h = Math.floor(m / 60);
        const remM = m % 60;
        return `${h}:${String(remM).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function _formatHumanDuration(secOrMs) {
    if (secOrMs == null) return '';
    if (typeof secOrMs === 'string') return secOrMs.trim();
    let sec = Number(secOrMs) || 0;
    if (sec > 100000) sec = Math.round(sec / 1000);
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec}с`;
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    if (m < 60) {
        return s > 0 ? `${m}м ${s}с` : `${m}м`;
    }
    const h = Math.floor(m / 60);
    const remM = m % 60;
    return remM > 0 ? `${h}ч ${String(remM).padStart(2, '0')}м` : `${h}ч`;
}

function _buildFallbackMonitorSnapshot(checkpoints = []) {
    let activeAgents = 0;
    let doneAgents = 0;
    const projectBuckets = new Map();

    for (const cp of (checkpoints || [])) {
        const pPath = cp.project_path || '';
        const pName = cp.project_name || (pPath ? path.basename(pPath) : 'Проект');
        const key = pPath.toLowerCase() || pName.toLowerCase();

        if (!projectBuckets.has(key)) {
            projectBuckets.set(key, {
                project_key: key,
                project_path: pPath,
                project_name: pName,
                active_agents: 0,
                done_agents: 0,
                total_agents: 0,
                active_sessions: 0,
                active_started_at_ms: 0,
                sessions: []
            });
        }
        const bucket = projectBuckets.get(key);
        bucket.sessions.push(cp);

        const isDone = cp.is_complete || cp.current_node === 'DONE';
        const wCount = cp.total_workers || (Array.isArray(cp.workers) ? cp.workers.length : 1);
        const dCount = cp.done_workers || (isDone ? wCount : 0);
        const aCount = isDone ? 0 : Math.max(0, wCount - dCount);

        bucket.total_agents += wCount;
        bucket.done_agents += dCount;
        bucket.active_agents += aCount;
        if (!isDone) {
            bucket.active_sessions += 1;
            if (!bucket.active_started_at_ms || (cp.created_at_ms && cp.created_at_ms < bucket.active_started_at_ms)) {
                bucket.active_started_at_ms = cp.created_at_ms || Date.now();
            }
        }

        activeAgents += aCount;
        doneAgents += dCount;
    }

    const projects = Array.from(projectBuckets.values());
    const activeProjects = projects.filter((p) => p.active_agents > 0 || p.active_sessions > 0).length;

    return {
        summary: {
            active_agents: activeAgents,
            active_projects: activeProjects,
            active_sessions: projects.reduce((acc, p) => acc + p.active_sessions, 0),
            done_agents: doneAgents,
            total_projects: projects.length
        },
        projects,
        updated_at_ms: Date.now()
    };
}

function _formatAgentPillTextFallback(snapshot, recentCompletionState) {
    const comp = Array.isArray(recentCompletionState)
        ? recentCompletionState[0]
        : recentCompletionState;

    if (comp) {
        const isFailed = Boolean(comp.isError || comp.failed || comp.status === 'failed' || comp.status === 'error');
        const proj = typeof comp === 'string'
            ? comp
            : (comp.projectName || comp.project_name || comp.project || 'Проект');

        if (isFailed) {
            return `⚠ Ошибка: ${proj}`;
        }

        const done = comp.done != null ? comp.done : comp.done_workers;
        const total = comp.total != null ? comp.total : comp.total_workers;
        const durRaw = comp.duration != null ? comp.duration : (comp.duration_seconds != null ? comp.duration_seconds : comp.dur);
        const dur = durRaw != null ? _formatHumanDuration(durRaw) : '';

        if (done != null && total != null && dur) {
            return `✓ Готово: ${proj} (${done}/${total} · ${dur})`;
        }
        if (done != null && total != null) {
            return `✓ Готово: ${proj} (${done}/${total})`;
        }
        return `✓ Готово: ${proj}`;
    }

    const summary = (snapshot && snapshot.summary) || {};
    const activeAgents = Number(summary.active_agents) || 0;
    if (!snapshot || activeAgents <= 0) {
        return '⚡ Агенты: 0';
    }

    const projects = Array.isArray(snapshot.projects) ? snapshot.projects : [];
    const activeProjects = projects.filter((p) => p && p.active_agents > 0);

    if (activeProjects.length <= 1) {
        const p = activeProjects[0] || projects[0] || {};
        const pName = p.project_name || (p.project_path ? path.basename(p.project_path) : '') || 'Проект';
        const startMs = p.active_started_at_ms || p.latest_activity_ms || 0;
        const elapsedSec = startMs > 0 ? Math.max(0, Math.floor((Date.now() - startMs) / 1000)) : 0;
        const timeStr = _formatElapsedTimer(elapsedSec);
        const nAg = p.active_agents || activeAgents;

        let line = `⚡ ${nAg} аг. · ${pName} · ${timeStr}`;
        if (line.length > 26) {
            const prefix = `⚡ ${nAg} аг. · `;
            const suffix = ` · ${timeStr}`;
            const maxNameLen = Math.max(3, 26 - (prefix.length + suffix.length + 1));
            const truncatedName = pName.length > maxNameLen ? pName.slice(0, maxNameLen) + '…' : pName;
            line = `${prefix}${truncatedName}${suffix}`;
        }
        return line;
    }

    const P = activeProjects.length;
    const S = summary.active_sessions || activeProjects.reduce((acc, p) => acc + (p.active_sessions || 1), 0) || P;
    const N = activeAgents;
    return `⚡ ${N} аг. · ${_pluralRu(P, 'проект', 'проекта', 'проектов')} · ${_pluralRu(S, 'задача', 'задачи', 'задач')}`;
}

function _formatAgentTooltipSectionsFallback(snapshot, recentHistory = []) {
    const summary = (snapshot && snapshot.summary) || {};
    const activeAgents = Number(summary.active_agents) || 0;
    const projects = Array.isArray(snapshot && snapshot.projects) ? snapshot.projects : [];
    const activeProjects = projects.filter((p) => p && p.active_agents > 0);
    const now = (snapshot && snapshot.nowMs) || (snapshot && snapshot.updated_at_ms) || Date.now();

    const lines = [];
    const mdLines = [];
    const sections = [];

    let header = '';
    if (activeAgents > 0) {
        const P = activeProjects.length;
        const S = summary.active_sessions || activeProjects.reduce((acc, p) => acc + (p.active_sessions || 1), 0) || P;
        const N = summary.active_agents || activeAgents;
        header = `⚡ ${_pluralRu(N, 'агент', 'агента', 'агентов')} · ${_pluralRu(P, 'проект', 'проекта', 'проектов')} · ${_pluralRu(S, 'задача', 'задачи', 'задач')}`;
    } else {
        header = '⚡ Все агенты свободны';
    }

    lines.push(header);
    mdLines.push(`**${header}**\n`);

    if (activeAgents > 0) {
        for (const p of activeProjects) {
            const projName = p.project_name || (p.project_path ? path.basename(p.project_path) : '') || 'Проект';
            const done = p.done_agents || 0;
            const total = p.total_agents || Math.max(p.active_agents + done, 1);
            const startMs = p.active_started_at_ms || p.latest_activity_ms || 0;
            const elapsedSec = startMs > 0 ? Math.max(0, Math.floor((now - startMs) / 1000)) : 0;
            const elapsed = _formatElapsedTimer(elapsedSec);

            const projLine = `📁 ${projName} — ${done}/${total} работают · ⏱ ${elapsed}`;
            lines.push(projLine);
            mdLines.push(`📁 **${projName}** — ${done}/${total} работают · ⏱ ${elapsed}  `);

            const sectionObj = {
                project: projName,
                project_path: p.project_path || '',
                done,
                total,
                elapsed,
                workers: []
            };

            const sessions = Array.isArray(p.sessions) ? p.sessions : [];
            for (const sess of sessions) {
                if (sess.is_active) {
                    const rawTitle = String(sess.title || sess.user_request || '').replace(/\s+/g, ' ').trim();
                    if (rawTitle && rawTitle !== projName) {
                        const titleShort = rawTitle.length > 140 ? `${rawTitle.slice(0, 140).trimEnd()}…` : rawTitle;
                        const sLine = `  • ${titleShort} · ${elapsed}`;
                        lines.push(sLine);
                        mdLines.push(`  • **${titleShort}** · \`${elapsed}\`  `);
                        sectionObj.workers.push({ name: titleShort, model: sess.orchestrator_model || '', status: 'running', line: sLine });
                    }
                }
                const workers = Array.isArray(sess.workers) ? sess.workers : [];
                for (const w of workers) {
                    const status = String(w.status || '').toLowerCase();
                    const wName = w.name || 'агент';
                    const wModelRaw = w.model ? String(w.model) : '';
                    const rawDesc = String(w.task_description || w.task_desc || '').replace(/\s+/g, ' ').trim();
                    const filesStr = Array.isArray(w.files) && w.files.length > 0 ? w.files.slice(0, 2).map((f) => path.basename(String(f))).join(', ') : '';
                    const taskSummary = rawDesc
                        ? (rawDesc.length > 110 ? `${rawDesc.slice(0, 110).trimEnd()}…` : rawDesc)
                        : filesStr;

                    if (status === 'running' || status === 'working') {
                        const wStart = w.started_at_ms || startMs || 0;
                        const wElapsedSec = wStart > 0 ? Math.max(0, Math.floor((now - wStart) / 1000)) : elapsedSec;
                        const wElapsed = _formatElapsedTimer(wElapsedSec);
                        let wLine = '';
                        let wMdLine = '';
                        if (taskSummary) {
                            const meta = [wName !== taskSummary ? wName : '', wModelRaw].filter(Boolean).join(', ');
                            const metaSuffix = meta ? ` (${meta})` : '';
                            wLine = `  ↳ ⏳ ${taskSummary}${metaSuffix} · ${wElapsed}`;
                            wMdLine = `  ↳ ⏳ ${taskSummary}${metaSuffix} · \`${wElapsed}\`  `;
                        } else {
                            const wModel = wModelRaw ? ` (${wModelRaw})` : '';
                            wLine = `  ↳ ⏳ ${wName}${wModel} · ${wElapsed}`;
                            wMdLine = `  ↳ ⏳ **${wName}**${wModel} · \`${wElapsed}\`  `;
                        }
                        lines.push(wLine);
                        mdLines.push(wMdLine);
                        sectionObj.workers.push({ name: wName, model: wModelRaw, status: 'running', line: wLine });
                    } else if (status === 'done' || status === 'completed' || status === 'killed') {
                        if (!taskSummary) continue;
                        const durSec = w.duration_seconds || 0;
                        const durText = durSec > 0 ? `, ${_formatHumanDuration(durSec)}` : '';
                        const wLine = `  ↳ ✅ ${taskSummary} (готово${durText})`;
                        lines.push(wLine);
                        mdLines.push(`  ↳ ✅ ${taskSummary} *(готово${durText})*  `);
                        sectionObj.workers.push({ name: wName, model: wModelRaw, status: 'done', line: wLine });
                    } else if (status === 'failed' || status === 'error') {
                        const errLabel = taskSummary || wName;
                        const wLine = `  ↳ ❌ ${errLabel} (ошибка)`;
                        lines.push(wLine);
                        mdLines.push(`  ↳ ❌ ${errLabel} *(ошибка)*  `);
                        sectionObj.workers.push({ name: wName, model: wModelRaw, status: 'failed', line: wLine });
                    } else if (taskSummary) {
                        const wLine = `  ↳ ⏳ ${taskSummary}`;
                        lines.push(wLine);
                        mdLines.push(`  ↳ ⏳ ${taskSummary}  `);
                        sectionObj.workers.push({ name: wName, model: wModelRaw, status: 'pending', line: wLine });
                    }
                }
            }
            sections.push(sectionObj);
        }
    } else {
        // Idle: Compact view — up to 3 recent projects × up to 3 finished tasks each (no subagent walls)
        const grouped = [];
        if (Array.isArray(recentHistory) && recentHistory.length > 0) {
            const groupMap = new Map();
            for (const item of recentHistory) {
                if (typeof item === 'string') {
                    const line = `  • ${item}`;
                    lines.push(line);
                    mdLines.push(`  • ${item}  `);
                    sections.push({ line });
                    continue;
                }
                const proj = item.project_name || item.projectName || item.project || 'Проект';
                if (!groupMap.has(proj)) {
                    if (grouped.length >= 3) continue;
                    const entry = { proj, items: [] };
                    groupMap.set(proj, entry);
                    grouped.push(entry);
                }
                const grp = groupMap.get(proj);
                if (grp.items.length < 3) grp.items.push(item);
            }
        } else if (Array.isArray(snapshot && snapshot.recent_projects) && snapshot.recent_projects.length > 0) {
            for (const rp of snapshot.recent_projects.slice(0, 3)) {
                const items = Array.isArray(rp.tasks) ? rp.tasks.slice(0, 3) : [];
                if (items.length > 0) grouped.push({ proj: rp.project_name || 'Проект', items });
            }
        } else {
            const completed = [];
            for (const p of projects) {
                const pName = p.project_name || (p.project_path ? path.basename(p.project_path) : '') || 'Проект';
                for (const s of (p.sessions || [])) {
                    if (s.is_complete || !s.is_active) {
                        completed.push({ ...s, projectName: pName });
                    }
                }
            }
            completed.sort((a, b) => (b.updated_at_ms || 0) - (a.updated_at_ms || 0));
            const groupMap = new Map();
            for (const item of completed) {
                const proj = item.project_name || item.projectName || item.project || 'Проект';
                if (!groupMap.has(proj)) {
                    if (grouped.length >= 3) continue;
                    const entry = { proj, items: [] };
                    groupMap.set(proj, entry);
                    grouped.push(entry);
                }
                const grp = groupMap.get(proj);
                if (grp.items.length < 3) grp.items.push(item);
            }
        }

        for (const grp of grouped) {
            lines.push(`📁 ${grp.proj}`);
            mdLines.push(`📁 **${grp.proj}**  `);
            for (const item of grp.items.slice(0, 3)) {
                const rawTitle = String(item.title || item.user_request || item.name || '').replace(/\s+/g, ' ').trim();
                const title = rawTitle.length > 68 ? `${rawTitle.slice(0, 68).trimEnd()}…` : rawTitle;
                const durSec = item.duration_seconds || 0;
                const dur = durSec > 0 ? _formatHumanDuration(durSec) : (item.duration ? _formatHumanDuration(item.duration) : '');
                const isErr = Boolean(item.isError || item.failed || (item.errors && item.errors.length > 0));
                const suffix = isErr ? `(ошибка${dur ? `, ${dur}` : ''})` : `(готово${dur ? `, ${dur}` : ''})`;
                const taskText = title || 'Задача';
                const taskLine = `  • ${taskText} ${suffix}`;
                lines.push(taskLine);
                mdLines.push(`  • ${taskText} *${suffix}*  `);
                sections.push({
                    project: grp.proj,
                    title: taskText,
                    status: isErr ? 'failed' : 'done',
                    line: taskLine
                });
            }
        }
    }

    const bottomHint = 'Клик — меню задач и быстрый запуск';
    lines.push(bottomHint);
    mdLines.push(`\n---\n*${bottomHint}*`);

    const markdown = mdLines.join('\n');
    const plainText = lines.join('\n');

    const result = new String(markdown);
    result.header = header;
    result.lines = lines;
    result.plainText = plainText;
    result.markdown = markdown;
    result.sections = sections;
    return result;
}

function _detectAndHandleCompletions(currentSnapshot, checkpoints) {
    const bridge = getSwarmBridge();
    if (bridge && typeof bridge.detectRecentCompletion === 'function' && _swarmPreviousSnapshot) {
        try {
            const newlyCompleted = bridge.detectRecentCompletion(_swarmPreviousSnapshot, currentSnapshot);
            if (Array.isArray(newlyCompleted) && newlyCompleted.length > 0) {
                for (const item of newlyCompleted) {
                    if (item && item.taskId && !_swarmKnownCompletedTaskIds.has(item.taskId)) {
                        _swarmKnownCompletedTaskIds.add(item.taskId);
                        _swarmActiveTaskIds.delete(item.taskId);
                        triggerSwarmCompletion(item.projectName || item.project || path.basename(item.projectPath || 'проект'), item);
                    }
                }
            }
        } catch (_) {}
    }

    for (const cp of (checkpoints || [])) {
        if (!cp || !cp.task_id) continue;
        const isDone = cp.is_complete || cp.current_node === 'DONE' || cp.current_node === 'ERROR';
        if (!isDone) {
            _swarmActiveTaskIds.set(cp.task_id, {
                project_name: cp.project_name || (cp.project_path ? path.basename(cp.project_path) : 'проект'),
                task_id: cp.task_id,
                started_at_ms: cp.created_at_ms || cp.updated_at_ms || Date.now(),
                done_workers: cp.done_workers,
                total_workers: cp.total_workers,
                failed: Boolean(cp.errors && cp.errors.length)
            });
        } else {
            if (_swarmActiveTaskIds.has(cp.task_id) && !_swarmKnownCompletedTaskIds.has(cp.task_id)) {
                _swarmKnownCompletedTaskIds.add(cp.task_id);
                const prev = _swarmActiveTaskIds.get(cp.task_id);
                _swarmActiveTaskIds.delete(cp.task_id);
                triggerSwarmCompletion(cp.project_name || (prev && prev.project_name) || path.basename(cp.project_path || 'проект'), cp);
            }
        }
    }

    _swarmPreviousSnapshot = currentSnapshot;
}

function triggerSwarmCompletion(projectName, sessionData = {}) {
    const pName = projectName || 'проект';
    const durSec = sessionData.duration_seconds || (sessionData.started_at_ms ? Math.max(1, Math.round((Date.now() - sessionData.started_at_ms) / 1000)) : 0);
    const mm = String(Math.floor(durSec / 60)).padStart(2, '0');
    const ss = String(durSec % 60).padStart(2, '0');
    const durStr = durSec > 0 ? `${mm}:${ss}` : '';

    _swarmRecentCompletionState = {
        project_name: pName,
        task_id: sessionData.task_id || sessionData.id,
        done_workers: sessionData.done_workers || sessionData.total_workers || 1,
        total_workers: sessionData.total_workers || sessionData.done_workers || 1,
        duration_seconds: durSec,
        duration_str: durStr,
        failed: Boolean(sessionData.failed || sessionData.isError || sessionData.current_node === 'ERROR'),
        completed_at_ms: Date.now()
    };

    if (_swarmStatusBar && _swarmStatusBar !== statusBarItem) {
        _swarmStatusBar.text = `✓ Готово: ${pName}`;
        if (vscode.ThemeColor) {
            _swarmStatusBar.color = new vscode.ThemeColor('testing.iconPassed');
        } else {
            _swarmStatusBar.color = '#73c991';
        }
        try { _swarmStatusBar.show(); } catch (_) {}
    }

    if (vscode.window && typeof vscode.window.showInformationMessage === 'function') {
        vscode.window.showInformationMessage(`Команда завершена: ${pName}`, 'Открыть итоги / Новая задача')
            .then((choice) => {
                if (choice === 'Открыть итоги / Новая задача') {
                    resumeSwarmCommand();
                }
            });
    }

    setTimeout(() => {
        if (_swarmRecentCompletionState && _swarmRecentCompletionState.task_id === (sessionData.task_id || sessionData.id)) {
            _swarmRecentCompletionState = null;
            if (_swarmStatusBar && _swarmStatusBar !== statusBarItem) {
                _swarmStatusBar.color = undefined;
            }
            updateSwarmStatusBarLive();
        }
    }, 15000);
}

function updateSwarmStatusBarLive() {
    if (!_swarmStatusBar || _swarmStatusBar === statusBarItem) return;

    const bridge = getSwarmBridge();
    const gDir = process.env.AG_GEMINI_HOME || (process.env.AG_GEMINI_DIR ? path.join(process.env.AG_GEMINI_DIR, 'antigravity') : null);
    let collected = null;
    if (bridge && typeof bridge.collectAgentMonitorSnapshot === 'function') {
        try {
            collected = bridge.collectAgentMonitorSnapshot({ limit: 40, geminiDir: gDir });
        } catch (_) {}
    }
    if (!collected) {
        const cps = listSwarmCheckpointsSync(40);
        const snap = (bridge && typeof bridge.buildAgentMonitorSnapshot === 'function')
            ? bridge.buildAgentMonitorSnapshot({ checkpoints: cps, nowMs: Date.now() })
            : _buildFallbackMonitorSnapshot(cps);
        collected = { checkpoints: cps, snapshot: snap };
    }

    const snapshot = collected.snapshot || _buildFallbackMonitorSnapshot(collected.checkpoints);
    const checkpoints = Array.isArray(collected.checkpoints) ? collected.checkpoints : [];
    _swarmLastActive = Number(snapshot && snapshot.summary && snapshot.summary.active_agents) || 0;

    _detectAndHandleCompletions(snapshot, checkpoints);

    const pillText = (bridge && typeof bridge.formatAgentPillText === 'function')
        ? bridge.formatAgentPillText(snapshot, _swarmRecentCompletionState)
        : _formatAgentPillTextFallback(snapshot, _swarmRecentCompletionState);

    let tooltipData = null;
    if (bridge && typeof bridge.formatAgentTooltipSections === 'function') {
        try {
            tooltipData = bridge.formatAgentTooltipSections(snapshot);
        } catch (_) {}
    }
    if (!tooltipData) {
        const recentHistory = checkpoints.filter((c) => c && (c.is_complete || c.current_node === 'DONE'));
        tooltipData = _formatAgentTooltipSectionsFallback(snapshot, recentHistory);
    }

    let tooltipMarkdown = '';
    if (typeof tooltipData === 'string') {
        tooltipMarkdown = tooltipData;
    } else if (tooltipData && typeof tooltipData.markdown === 'string') {
        tooltipMarkdown = tooltipData.markdown;
    } else if (tooltipData && Array.isArray(tooltipData.lines)) {
        tooltipMarkdown = tooltipData.lines.join('\n\n');
    } else if (Array.isArray(tooltipData)) {
        tooltipMarkdown = tooltipData.join('\n\n');
    }

    if (_swarmStatusBar.text !== pillText) {
        _swarmStatusBar.text = pillText;
    }

    if (_lastSwarmTooltipMarkdown !== tooltipMarkdown) {
        _lastSwarmTooltipMarkdown = tooltipMarkdown;
        if (vscode.MarkdownString) {
            const md = new vscode.MarkdownString(tooltipMarkdown);
            md.isTrusted = true;
            md.supportThemeIcons = true;
            _swarmStatusBar.tooltip = md;
        } else {
            _swarmStatusBar.tooltip = tooltipMarkdown;
        }
    }

    if (_swarmRecentCompletionState && _swarmRecentCompletionState.project_name) {
        if (vscode.ThemeColor) {
            _swarmStatusBar.color = new vscode.ThemeColor('testing.iconPassed');
        } else {
            _swarmStatusBar.color = '#73c991';
        }
    } else {
        _swarmStatusBar.color = undefined;
    }

    try { _swarmStatusBar.show(); } catch (_) {}
}

/**
 * Execute `python -m swarm.cli <args>` via shared `swarm_bridge.js`.
 * @param {string[]} argsArray
 * @param {{ enginePath?: string, timeoutMs?: number }} [options]
 */
async function runSwarmCliAsync(argsArray, options = {}) {
    const bridge = getSwarmBridge();
    if (!bridge) {
        return { ok: false, data: null, error: 'Swarm Bridge not found' };
    }
    return bridge.runSwarmCliAsync(argsArray, {
        ...options,
        enginePath: options.enginePath || resolveSwarmEnginePath(),
        workspacePaths: _getWorkspacePaths(),
        extraDirs: [__dirname]
    });
}

/**
 * List persisted Swarm checkpoints from ~/.antigravity_tools/swarm/*\/state.json sorted newest-first.
 * @param {number} [limit]
 * @returns {Array<object>}
 */
function listSwarmCheckpointsSync(limit = 20) {
    const bridge = getSwarmBridge();
    return bridge ? bridge.listSwarmCheckpoints(limit) : [];
}

function stopCheckpointWatcher() {
    _swarmCheckpointWakeFn = null;
    if (_swarmPollTimer) {
        clearTimeout(_swarmPollTimer);
        _swarmPollTimer = null;
    }
}

/**
 * Stop active Swarm IPC child process and/or checkpoint monitor.
 * @param {{ keepOutput?: boolean }} [opts]
 */
function stopSwarm(opts = {}) {
    stopCheckpointWatcher();
    if (_swarmProcess) {
        const proc = _swarmProcess;
        _swarmProcess = null;
        try {
            if (proc.stdin && !proc.stdin.destroyed) {
                proc.stdin.write(JSON.stringify({ cmd: 'shutdown' }) + '\n');
            }
        } catch (_) {}
        try { proc.kill(); } catch (_) {}
    }
    for (const key of Object.keys(_swarmWorkerCallbacks)) {
        try {
            _swarmWorkerCallbacks[key].reject(new Error('Swarm stopped'));
        } catch (_) {}
    }
    _swarmWorkerCallbacks = {};
    _swarmTaskId = null;
    updateSwarmStatusBarLive();
    if (!opts.keepOutput && _swarmOutputChannel) {
        try { _swarmOutputChannel.dispose(); } catch (_) {}
        _swarmOutputChannel = null;
    }
}

function startCheckpointWatcher(taskId) {
    stopCheckpointWatcher();
    _swarmTaskId = taskId;
    let active = true;

    const poll = () => {
        try {
            const bridge = getSwarmBridge();
            const summary = bridge ? bridge.loadCheckpointSummary(taskId) : null;
            if (!summary) return;
            if (summary.is_complete) {
                active = false;
                stopCheckpointWatcher();
                triggerSwarmCompletion(summary.project_name || path.basename(summary.project_path || 'проект'), summary);
                logSwarm(`Swarm [${taskId}] завершён (node=${summary.current_node}).`);
                return;
            }
            updateSwarmStatusBarLive();
        } catch (_) {}
    };

    const scheduleNext = () => {
        if (!active || _swarmTaskId !== taskId) return;
        if (_swarmPollTimer) {
            clearTimeout(_swarmPollTimer);
            _swarmPollTimer = null;
        }
        const delayMs = _swarmLastActive > 0 ? 2000 : 15000;
        _swarmPollTimer = setTimeout(() => {
            _swarmPollTimer = null;
            poll();
            scheduleNext();
        }, delayMs);
        if (typeof _swarmPollTimer.unref === 'function') _swarmPollTimer.unref();
    };

    _swarmCheckpointWakeFn = () => {
        poll();
        scheduleNext();
    };

    poll();
    scheduleNext();
}

/**
 * Build structured instructions for Antigravity Chat / native `/swarm` via shared `swarm_bridge.js`.
 */
function buildSwarmDispatchPrompt(opts) {
    const bridge = getSwarmBridge();
    return bridge ? bridge.buildSwarmDispatchPrompt(opts) : '';
}

/**
 * Attempt to send a prompt directly into Antigravity's Agent/Chat panel.
 * Tries the verified Antigravity IDE command (`antigravity.sendPromptToAgentPanel`),
 * then `antigravity.sendMessage` on alternative hosts, returning false if unavailable.
 * @param {string} text
 * @returns {Promise<boolean>}
 */
async function dispatchPromptToChatPanel(text) {
    if (!vscode.commands || typeof vscode.commands.executeCommand !== 'function') {
        return false;
    }
    const candidates = ['antigravity.sendPromptToAgentPanel', 'antigravity.sendMessage'];
    let registered = null;
    try {
        if (typeof vscode.commands.getCommands === 'function') {
            registered = new Set(await vscode.commands.getCommands(true));
        }
    } catch (_) {
        registered = null;
    }
    for (const cmd of candidates) {
        if (registered && !registered.has(cmd)) {
            continue;
        }
        try {
            await vscode.commands.executeCommand(cmd, text);
            return true;
        } catch (_) {}
    }
    return false;
}

async function injectPromptIntoAntigravityChat(text, label) {
    logSwarm(`--- ${label} ---\n${text}\n`);
    const sent = await dispatchPromptToChatPanel(text);

    if (sent) {
        if (vscode.window && vscode.window.showInformationMessage) {
            vscode.window.showInformationMessage(`${label}: инструкция отправлена в чат Antigravity.`);
        }
        return true;
    }

    try {
        if (vscode.env && vscode.env.clipboard) {
            await vscode.env.clipboard.writeText(text);
        }
    } catch (_) {}
    const ch = getSwarmOutputChannel();
    if (ch) ch.show(true);
    if (vscode.window && vscode.window.showInformationMessage) {
        vscode.window.showInformationMessage(
            `${label}: промпт скопирован в буфер обмена и выведен в Output -> "Antigravity Swarm". Вставьте в чат Antigravity.`
        );
    }
    return false;
}

async function _resolveProjectFolder() {
    const folders = (vscode.workspace && vscode.workspace.workspaceFolders) || [];
    if (folders.length === 1) {
        return folders[0].uri.fsPath;
    }
    if (folders.length > 1) {
        const picked = await vscode.window.showQuickPick(
            folders.map((f) => ({ label: f.name, description: f.uri.fsPath, fsPath: f.uri.fsPath })),
            { placeHolder: 'Выберите папку проекта для Swarm' }
        );
        return picked ? picked.fsPath : null;
    }
    const chosen = await vscode.window.showOpenDialog({
        canSelectFolders: true,
        canSelectFiles: false,
        canSelectMany: false,
        openLabel: 'Выбрать папку git-репозитория'
    });
    return chosen && chosen[0] ? chosen[0].fsPath : null;
}

function _extractJsonArray(rawText) {
    if (!rawText) return null;
    const text = String(rawText).trim();
    const fenceMatch = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
    const body = fenceMatch ? fenceMatch[1].trim() : text;
    try {
        const parsed = JSON.parse(body);
        if (Array.isArray(parsed)) return parsed;
    } catch (_) {}
    const start = body.indexOf('[');
    const end = body.lastIndexOf(']');
    if (start !== -1 && end > start) {
        try {
            const parsed = JSON.parse(body.slice(start, end + 1));
            if (Array.isArray(parsed)) return parsed;
        } catch (_) {}
    }
    return null;
}

async function _collectLmResponseText(promptText) {
    if (!vscode.lm || typeof vscode.lm.selectChatModels !== 'function') {
        return { ok: false, error: 'vscode.lm API недоступен в этом хосте' };
    }
    const models = await vscode.lm.selectChatModels({});
    if (!models || models.length === 0) {
        return { ok: false, error: 'Нет доступных моделей vscode.lm' };
    }
    const model = models[0];
    const msg = vscode.LanguageModelChatMessage && vscode.LanguageModelChatMessage.User
        ? vscode.LanguageModelChatMessage.User(promptText)
        : { role: 'user', content: promptText };
    const cts = vscode.CancellationTokenSource ? new vscode.CancellationTokenSource() : null;
    try {
        const resp = await model.sendRequest([msg], {}, cts ? cts.token : undefined);
        let text = '';
        for await (const part of resp.text) {
            text += part;
        }
        return { ok: true, text, modelId: model.id || model.name || 'lm' };
    } catch (e) {
        return { ok: false, error: e.message || String(e) };
    } finally {
        if (cts) cts.dispose();
    }
}

async function _inspectWorktreeGitState(worktreePath) {
    const bridge = getSwarmBridge();
    if (!bridge) return { commit_sha: null, files_changed: [] };
    const shaRes = await bridge._execFilePromise('git', ['-C', worktreePath, 'rev-parse', 'HEAD'], { encoding: 'utf8', windowsHide: true });
    const diffRes = await bridge._execFilePromise('git', ['-C', worktreePath, 'diff', '--name-only', 'HEAD~1..HEAD'], { encoding: 'utf8', windowsHide: true });
    const files = diffRes.code === 0
        ? diffRes.stdout.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
        : [];
    return {
        commit_sha: shaRes.code === 0 ? shaRes.stdout.trim() : null,
        files_changed: files
    };
}

function _sendIpcJson(obj) {
    if (_swarmProcess && _swarmProcess.stdin && !_swarmProcess.stdin.destroyed) {
        _swarmProcess.stdin.write(JSON.stringify(obj) + '\n');
        return true;
    }
    return false;
}

async function _handleSwarmIpcEvent(evt, initPayload) {
    if (!evt || !evt.event) return;

    switch (evt.event) {
        case 'ready':
            _sendIpcJson(initPayload);
            break;
        case 'started':
            _swarmTaskId = evt.task_id || _swarmTaskId;
            logSwarm(`IPC started task_id=${_swarmTaskId}`);
            updateSwarmStatusBar(`$(organization) Swarm ${_swarmTaskId}`, 'Нажмите чтобы остановить Swarm');
            break;
        case 'node_started': {
            const totalPh = evt.total_phases ? `/${evt.total_phases}` : '';
            const phText = Number.isFinite(evt.phase) && evt.phase > 0 ? ` фаза ${evt.phase}${totalPh}` : '';
            logSwarm(`Node: ${evt.node}${phText}`);
            updateSwarmStatusBar(`$(sync~spin) Swarm ${evt.node}${phText}`, `Task ${_swarmTaskId || ''}`);
            break;
        }
        case 'orchestrator_prompt': {
            logSwarm('Запрос декомпозиции у vscode.lm...');
            const lmRes = await _collectLmResponseText(evt.prompt);
            if (!lmRes.ok) {
                logSwarm(`Ошибка vscode.lm: ${lmRes.error}`);
                vscode.window.showErrorMessage(`Swarm Orchestrator (vscode.lm): ${lmRes.error}`);
                stopSwarm();
                return;
            }
            const briefs = _extractJsonArray(lmRes.text);
            if (!briefs) {
                logSwarm(`Не удалось распарсить JSON briefs из ответа модели:\n${lmRes.text}`);
                vscode.window.showErrorMessage('Swarm Orchestrator: модель не вернула валидный JSON массив воркеров.');
                stopSwarm();
                return;
            }
            logSwarm(`Декомпозиция готова (${lmRes.modelId}): ${briefs.length} воркеров.`);
            _sendIpcJson({ cmd: 'orchestrator_result', briefs });
            break;
        }
        case 'worker_dispatched': {
            const wName = evt.worker_name;
            const wt = evt.worktree_path;
            const taskDesc = evt.prompt || evt.task_description || '';
            const ownedList = Array.isArray(evt.owned_files) && evt.owned_files.length
                ? `\nФайлы: ${evt.owned_files.join(', ')}`
                : '';
            logSwarm(`Воркер [${wName}] -> ${wt}`);
            await injectPromptIntoAntigravityChat(
                `Выполни подзадачу Swarm [${_swarmTaskId}] для воркера \`${wName}\` строго в директории \`${wt}\`${ownedList}\nИ зафиксируй результат git commit:\n\n${taskDesc}`,
                `Swarm Worker ${wName}`
            );
            const action = await vscode.window.showInformationMessage(
                `Воркер "${wName}" отправлен в worktree. Нажмите когда агент сделает git commit.`,
                'Зафиксировать commit',
                'Отметить ошибку'
            );
            if (!_swarmProcess) return;
            if (action === 'Зафиксировать commit') {
                const gitState = await _inspectWorktreeGitState(wt);
                _sendIpcJson({
                    cmd: 'worker_result',
                    worker_name: wName,
                    success: Boolean(gitState.commit_sha),
                    commit_sha: gitState.commit_sha || '',
                    files_changed: gitState.files_changed,
                    error_message: gitState.commit_sha ? '' : 'Commit not found in worktree'
                });
            } else {
                _sendIpcJson({
                    cmd: 'worker_result',
                    worker_name: wName,
                    success: false,
                    error_message: 'Aborted or failed in IDE worker prompt'
                });
            }
            break;
        }
        case 'worker_done':
            logSwarm(`Worker [${evt.worker_name}] success=${evt.success} commit=${evt.commit_sha || '-'}`);
            break;
        case 'phase_verified':
            logSwarm(`Phase verified: passed=${evt.passed} (${evt.summary || ''})`);
            break;
        case 'done': {
            const finalBranch = evt.final_branch || (evt.state && evt.state.final_branch) || '-';
            logSwarm(`Swarm завершён: task_id=${evt.task_id}, integration=${finalBranch}`);
            stopSwarm();
            vscode.window.showInformationMessage(`Swarm [${evt.task_id}] завершён!`);
            break;
        }
        case 'error':
            logSwarm(`Ошибка Swarm: ${evt.message}`);
            vscode.window.showErrorMessage(`Swarm ошибка: ${evt.message}`);
            if (evt.fatal) stopSwarm();
            break;
        default:
            logSwarm(`Event: ${JSON.stringify(evt)}`);
            break;
    }
}

function startSwarmIpcSession(enginePath, initPayload) {
    stopSwarm();
    const ch = getSwarmOutputChannel();
    if (ch) ch.show(true);

    const bridge = getSwarmBridge();
    if (!bridge) {
        logSwarm('Swarm Bridge not found.');
        return;
    }
    const env = bridge.getCleanPythonEnv(enginePath);
    const [bin, prefixArgs] = bridge._pythonCandidates()[0];
    const proc = spawn(bin, [...prefixArgs, '-u', '-m', 'swarm.ipc_server'], {
        env,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe']
    });
    _swarmProcess = proc;

    let buf = '';
    proc.stdout.on('data', (chunk) => {
        buf += chunk.toString('utf8');
        const lines = buf.split(/\r?\n/);
        buf = lines.pop() || '';
        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            try {
                const evt = JSON.parse(trimmed);
                Promise.resolve(_handleSwarmIpcEvent(evt, initPayload)).catch((e) => {
                    logSwarm(`IPC event handler error: ${e.message}`);
                });
            } catch (_) {
                logSwarm(`[stdout] ${trimmed}`);
            }
        }
    });
    proc.stderr.on('data', (chunk) => {
        const msg = chunk.toString('utf8').trim();
        if (msg) logSwarm(`[stderr] ${msg}`);
    });
    proc.on('close', (code) => {
        if (_swarmProcess === proc) {
            _swarmProcess = null;
            updateSwarmStatusBar(null);
            logSwarm(`IPC process exited (code=${code}).`);
        }
    });
}

/** Level 1 QuickPick: "+ Быстрая задача", active projects with session cards, then history */
async function showSwarmHubMenu() {
    const checkpoints = listSwarmCheckpointsSync(50);
    const bridge = getSwarmBridge();
    const knownProjects = (bridge && typeof bridge.listKnownProjects === 'function') ? bridge.listKnownProjects() : [];
    const items = [];

    // Contextual detection of current open project:
    let currentProjectFolder = null;
    if (vscode.window && vscode.window.activeTextEditor && vscode.window.activeTextEditor.document) {
        const docUri = vscode.window.activeTextEditor.document.uri;
        if (vscode.workspace && typeof vscode.workspace.getWorkspaceFolder === 'function') {
            currentProjectFolder = vscode.workspace.getWorkspaceFolder(docUri);
        }
    }
    if (!currentProjectFolder && vscode.workspace && Array.isArray(vscode.workspace.workspaceFolders) && vscode.workspace.workspaceFolders.length > 0) {
        currentProjectFolder = vscode.workspace.workspaceFolders[0];
    }
    const currentProjectPath = currentProjectFolder ? (currentProjectFolder.uri ? currentProjectFolder.uri.fsPath : currentProjectFolder.fsPath) : null;
    const currentProjectName = currentProjectFolder ? (currentProjectFolder.name || path.basename(currentProjectPath)) : null;

    items.push({
        label: 'Новая задача для агентов (в 1 клик)',
        kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
    });

    if (currentProjectPath && currentProjectName) {
        items.push({
            label: '$(zap) В текущем проекте: ' + currentProjectName,
            description: '(активный) ' + currentProjectPath,
            action: 'quick_launch_project',
            projectPath: currentProjectPath
        });
    }

    const seenPaths = new Set();
    if (currentProjectPath) seenPaths.add(path.normalize(currentProjectPath).toLowerCase());

    const recentList = knownProjects.filter((p) => p && p.path && !seenPaths.has(path.normalize(p.path).toLowerCase())).slice(0, 5);
    for (const p of recentList) {
        seenPaths.add(path.normalize(p.path).toLowerCase());
        items.push({
            label: '$(folder) ' + (p.name || path.basename(p.path)),
            description: p.path,
            action: 'quick_launch_project',
            projectPath: p.path
        });
    }

    items.push({
        label: '$(folder-opened) Выбрать другую папку на диске...',
        description: 'Открыть проводник для выбора проекта',
        action: 'quick_launch_custom'
    });

    const activeCps = checkpoints.filter((c) => !c.is_complete && c.current_node !== 'DONE' && c.current_node !== 'ERROR');
    const historyCps = checkpoints.filter((c) => c.is_complete || c.current_node === 'DONE' || c.current_node === 'ERROR');

    if (activeCps.length > 0) {
        items.push({
            label: 'Активные команды агентов',
            kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
        });
        for (const cp of activeCps) {
            const pName = cp.project_name || (cp.project_path ? path.basename(cp.project_path) : 'проект');
            const stage = cp.stage_label || cp.current_node || 'В работе';
            const wModels = Array.isArray(cp.worker_models) && cp.worker_models.length ? cp.worker_models.join(', ') : 'flash';
            const req = cp.user_request ? (cp.user_request.length > 56 ? cp.user_request.slice(0, 53) + '...' : cp.user_request) : cp.task_id;
            items.push({
                label: `$(sync~spin) ${pName} — ${req}`,
                description: `${stage} · ${cp.done_workers || 0}/${cp.total_workers || 0} агентов`,
                detail: `Модели: ${wModels} · ${cp.project_path || ''}`,
                action: 'open_session',
                checkpoint: cp
            });
        }
    }

    if (historyCps.length > 0) {
        items.push({
            label: 'История завершённых задач',
            kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
        });
        for (const cp of historyCps) {
            const pName = cp.project_name || (cp.project_path ? path.basename(cp.project_path) : 'проект');
            const isOk = cp.is_complete || cp.current_node === 'DONE';
            const icon = isOk ? '$(check)' : '$(error)';
            const stage = cp.stage_label || (isOk ? 'Готово' : 'Ошибка');
            const wModels = Array.isArray(cp.worker_models) && cp.worker_models.length ? cp.worker_models.join(', ') : 'flash';
            const req = cp.user_request ? (cp.user_request.length > 56 ? cp.user_request.slice(0, 53) + '...' : cp.user_request) : cp.task_id;
            items.push({
                label: `${icon} ${pName} — ${req}`,
                description: `${stage} · ${cp.done_workers || 0}/${cp.total_workers || 0} агентов`,
                detail: `Модели: ${wModels} · ${cp.project_path || ''}`,
                action: 'open_session',
                checkpoint: cp
            });
        }
    }

    if (activeCps.length === 0 && historyCps.length === 0) {
        items.push({
            label: '$(info) Пока нет запущенных задач',
            description: 'Нажмите "Новая задача" выше или введите в чат: /swarm <задача>',
            action: 'noop'
        });
    }

    const picked = await vscode.window.showQuickPick(items, {
        placeHolder: 'Команда агентов — активные проекты и история',
        matchOnDescription: true,
        matchOnDetail: true
    });
    if (!picked) return;

    if (picked.action === 'quick_launch_project' && picked.projectPath) {
        await quickLaunchSwarmTask(picked.projectPath);
    } else if (picked.action === 'quick_launch_custom') {
        let chosenPath = null;
        if (vscode.window && typeof vscode.window.showOpenDialog === 'function') {
            const chosen = await vscode.window.showOpenDialog({
                canSelectFolders: true,
                canSelectFiles: false,
                canSelectMany: false,
                openLabel: 'Выбрать папку проекта'
            });
            if (chosen && chosen[0]) {
                chosenPath = chosen[0].fsPath;
            }
        }
        if (chosenPath) {
            await quickLaunchSwarmTask(chosenPath);
        } else if (!vscode.window || typeof vscode.window.showOpenDialog !== 'function') {
            await quickLaunchSwarmTask();
        }
    } else if (picked.action === 'quick_launch') {
        await quickLaunchSwarmTask();
    } else if (picked.action === 'open_session' && picked.checkpoint) {
        await showSwarmSessionDetails(picked.checkpoint);
    }
}

/** Quick launch flow: pick project -> input prompt -> sends prompt to Antigravity chat */
async function quickLaunchSwarmTask(preselectedProjectPath = null) {
    let selectedProjectPath = preselectedProjectPath;
    const bridge = getSwarmBridge();

    if (!selectedProjectPath) {
        const wsFolders = (vscode.workspace && vscode.workspace.workspaceFolders) || [];
        const knownProjects = (bridge && typeof bridge.listKnownProjects === 'function') ? bridge.listKnownProjects() : [];

    const projectItems = [];
    const seenPaths = new Set();

    if (wsFolders.length > 0) {
        projectItems.push({
            label: 'Текущий workspace',
            kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
        });
        for (const f of wsFolders) {
            const fsPath = f.uri ? f.uri.fsPath : f.fsPath;
            if (!fsPath) continue;
            seenPaths.add(path.normalize(fsPath).toLowerCase());
            projectItems.push({
                label: `$(folder) ${f.name || path.basename(fsPath)}`,
                description: '(Текущий workspace) ' + fsPath,
                projectPath: fsPath
            });
        }
    }

    const recentProjects = knownProjects.filter((p) => p && p.path && !seenPaths.has(path.normalize(p.path).toLowerCase()));
    if (recentProjects.length > 0) {
        projectItems.push({
            label: 'Недавние git-проекты',
            kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
        });
        for (const p of recentProjects) {
            seenPaths.add(path.normalize(p.path).toLowerCase());
            projectItems.push({
                label: `$(repo) ${p.name || path.basename(p.path)}`,
                description: p.path,
                projectPath: p.path
            });
        }
    }

    projectItems.push({
        label: 'Другое',
        kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
    });
    projectItems.push({
        label: '$(folder-opened) Выбрать другую папку...',
        description: 'Открыть проводник для выбора git-репозитория',
        isBrowse: true
    });

    const pickedProject = await vscode.window.showQuickPick(projectItems, {
        placeHolder: 'Выберите проект для запуска команды агентов',
        matchOnDescription: true
    });
    if (!pickedProject) return;

    selectedProjectPath = pickedProject.projectPath;
    if (pickedProject.isBrowse) {
        if (vscode.window && typeof vscode.window.showOpenDialog === 'function') {
            const chosen = await vscode.window.showOpenDialog({
                canSelectFolders: true,
                canSelectFiles: false,
                canSelectMany: false,
                openLabel: 'Выбрать папку проекта'
            });
            if (!chosen || !chosen[0]) return;
            selectedProjectPath = chosen[0].fsPath;
        }
    }
    }
    if (!selectedProjectPath) return;

    if (bridge && typeof bridge.rememberProject === 'function') {
        try { bridge.rememberProject(selectedProjectPath); } catch (_) {}
    }

    const normPath = selectedProjectPath ? path.normalize(selectedProjectPath).replace(/[/\\]+$/, '') : '';
    const pName = path.basename(normPath) || selectedProjectPath || 'проект';

    const prompt = await vscode.window.showInputBox({
        prompt: 'Задача для команды агентов в ' + pName,
        placeHolder: 'Например: отрефактори роутер, добавь тесты и обнови документацию',
        ignoreFocusOut: true
    });
    if (!prompt || !prompt.trim()) return;

    const enginePath = resolveSwarmEnginePath(selectedProjectPath);
    let dispatchPrompt = '';
    let taskId = null;

    if (bridge && typeof bridge.prepareSwarmTask === 'function') {
        try {
            const prep = await bridge.prepareSwarmTask({
                projectPath: selectedProjectPath,
                request: prompt.trim(),
                enginePath,
                workspacePaths: _getWorkspacePaths(),
                extraDirs: [__dirname],
                useAffectedTests: true,
                modelConfig: typeof loadModelConfigSync === 'function' ? loadModelConfigSync() : null
            });
            if (prep && prep.ok && prep.taskId) {
                dispatchPrompt = prep.dispatchPrompt;
                taskId = prep.taskId;
            }
        } catch (_) {}
    }

    if (!dispatchPrompt) {
        taskId = taskId || `task-${Date.now().toString(36)}`;
        dispatchPrompt = buildSwarmDispatchPrompt({
            taskId,
            projectPath: selectedProjectPath,
            prompt: prompt.trim(),
            enginePath: enginePath || '',
            mode: 'run'
        }) || `/swarm ${prompt.trim()}`;
    }

    if (taskId) {
        startCheckpointWatcher(taskId);
    }

    await injectPromptIntoAntigravityChat(dispatchPrompt, `Команда агентов (${pName})`);
}

/** Level 2: session detail (workers, models, tasks, files; buttons '💬 Переписка агентов', '▶ Продолжить в чате', '🗑 Удалить') */
async function showSwarmSessionDetails(cp) {
    if (!cp) return;
    const bridge = getSwarmBridge();
    const pName = cp.project_name || (cp.project_path ? path.basename(cp.project_path) : 'проект');
    const stage = cp.stage_label || cp.current_node || 'В очереди';
    const orchModel = cp.orchestrator_model || 'Модель чата';

    let msgCount = 0;
    if (bridge && typeof bridge.readSwarmBoard === 'function') {
        try {
            const b = bridge.readSwarmBoard(cp.task_id);
            if (b && Array.isArray(b.messages)) msgCount = b.messages.length;
        } catch (_) {}
    }

    const items = [];

    items.push({
        label: '$(arrow-left) Назад к списку проектов',
        action: 'back'
    });
    items.push({
        label: `💬 Переписка агентов (${msgCount} сообщений)`,
        description: 'Открыть хронологическую переписку агентов из board.jsonl',
        action: 'open_board'
    });

    if (!cp.is_complete && cp.current_node !== 'DONE') {
        items.push({
            label: '▶ Продолжить в чате',
            description: `Этап: ${stage} · Готово ${cp.done_workers || 0}/${cp.total_workers || 0}`,
            action: 'resume'
        });
    }

    items.push({
        label: '🗑 Удалить',
        description: `Удалить задачу ${cp.task_id} и временные worktree`,
        action: 'delete'
    });

    const workers = Array.isArray(cp.workers) ? cp.workers : [];
    if (workers.length > 0) {
        items.push({
            label: `Агенты команды (${workers.length} · План: ${orchModel})`,
            kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
        });

        for (const w of workers) {
            const st = String(w.status || '').toLowerCase();
            const isDone = st === 'done' || st === 'completed';
            const isRunning = st === 'running' || st === 'working';
            const isFailed = st === 'failed' || st === 'error';

            const stIcon = isDone ? '✅' : (isRunning ? '⏳' : (isFailed ? '❌' : '⚪'));
            const stText = isDone ? 'Готово' : (isRunning ? 'В работе' : (isFailed ? 'Ошибка' : 'В очереди'));

            const files = Array.isArray(w.files) ? w.files
                : (Array.isArray(w.owned_files) ? w.owned_files
                : (Array.isArray(w.files_changed) ? w.files_changed : []));
            const filesStr = files.length > 0 ? files.join(', ') : '(без файлов)';
            const desc = w.task_desc || w.prompt || '';

            items.push({
                label: `${stIcon} ${w.name || 'агент'} (${w.model || 'flash'}) — ${stText}`,
                description: desc,
                detail: `Файлы: ${filesStr}${w.branch ? ` · Ветка: ${w.branch}` : ''}`,
                action: 'open_worker_file',
                worker: w,
                files
            });
        }
    }

    const picked = await vscode.window.showQuickPick(items, {
        placeHolder: `${pName} · ${stage} · Агенты ${cp.done_workers || 0}/${cp.total_workers || 0}`,
        matchOnDescription: true,
        matchOnDetail: true
    });
    if (!picked) return;

    if (picked.action === 'back') {
        return showSwarmHubMenu();
    }

    if (picked.action === 'open_board') {
        return showSwarmBoardMessages(cp);
    }

    if (picked.action === 'resume') {
        const modelConfig = typeof loadModelConfigSync === 'function' ? loadModelConfigSync() : null;
        const res = bridge
            ? await bridge.resumeSwarmTask({
                taskId: cp.task_id,
                projectPath: cp.project_path,
                workspacePaths: _getWorkspacePaths(),
                extraDirs: [__dirname],
                modelConfig
            })
            : { ok: false, error: 'Swarm Bridge не найден' };
        if (!res || !res.ok) {
            if (vscode.window && vscode.window.showErrorMessage) {
                vscode.window.showErrorMessage(`Ошибка продолжения задачи: ${(res && res.error) || 'неизвестно'}`);
            }
            return;
        }
        startCheckpointWatcher(cp.task_id);
        await injectPromptIntoAntigravityChat(res.dispatchPrompt, `Команда [${cp.task_id}]`);
        return;
    }

    if (picked.action === 'delete') {
        const res = bridge
            ? await bridge.cleanupSwarmTask({
                taskId: cp.task_id,
                projectPath: cp.project_path,
                workspacePaths: _getWorkspacePaths(),
                extraDirs: [__dirname],
                deleteState: true
            })
            : { ok: false, error: 'Swarm Bridge не найден' };
        if (res && res.ok) {
            if (vscode.window && vscode.window.showInformationMessage) {
                vscode.window.showInformationMessage(`Задача ${pName} (${cp.task_id}) удалена.`);
            }
        } else {
            if (vscode.window && vscode.window.showErrorMessage) {
                vscode.window.showErrorMessage(`Ошибка удаления: ${(res && res.error) || 'неизвестно'}`);
            }
        }
        return showSwarmHubMenu();
    }

    if (picked.action === 'open_worker_file') {
        const files = picked.files || [];
        if (files.length === 1) {
            return _openFileInEditor(cp.project_path, files[0]);
        } else if (files.length > 1) {
            const filePick = await vscode.window.showQuickPick(
                files.map((f) => ({ label: `$(file) ${f}`, filePath: f })),
                { placeHolder: `Выберите файл воркера ${picked.worker ? picked.worker.name : ''}` }
            );
            if (filePick) {
                return _openFileInEditor(cp.project_path, filePick.filePath);
            }
        } else {
            if (cp.project_path) {
                return _openFileInEditor(cp.project_path, '');
            }
        }
        return null;
    }
}

const _inspectAndManageCheckpoint = showSwarmSessionDetails;

async function _openFileInEditor(projectPath, filePath) {
    if (!vscode.workspace || typeof vscode.workspace.openTextDocument !== 'function') return;
    const target = filePath
        ? (path.isAbsolute(filePath) ? filePath : path.join(projectPath || '', filePath))
        : projectPath;
    if (!target) return;
    try {
        const doc = await vscode.workspace.openTextDocument(target);
        if (vscode.window && typeof vscode.window.showTextDocument === 'function') {
            await vscode.window.showTextDocument(doc);
        }
        return doc;
    } catch (err) {
        if (vscode.window && vscode.window.showErrorMessage) {
            vscode.window.showErrorMessage(`Не удалось открыть файл: ${target} (${err.message || err})`);
        }
        return null;
    }
}

/** Level 3: show board.jsonl thread `#1 @coordinator -> @worker` */
async function showSwarmBoardMessages(cp) {
    if (!cp) return;
    const bridge = getSwarmBridge();
    const pName = cp.project_name || (cp.project_path ? path.basename(cp.project_path) : 'проект');
    let board = null;
    if (bridge && typeof bridge.readSwarmBoard === 'function') {
        try {
            board = bridge.readSwarmBoard(cp.task_id);
        } catch (_) {}
    }
    const messages = (board && Array.isArray(board.messages)) ? board.messages : [];

    const items = [];
    items.push({
        label: '$(arrow-left) Назад к карточке сессии',
        action: 'back'
    });

    if (messages.length > 0) {
        items.push({
            label: `Переписка агентов (${messages.length} сообщений)`,
            kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1
        });

        for (const m of messages) {
            const seq = m.seq != null ? m.seq : '?';
            const sender = m.agent || 'agent';
            const text = String(m.text || '');
            const mentions = text.match(/@([A-Za-z0-9_.-]+)/g) || [];
            const targets = mentions.map((s) => s.replace(/^@/, '')).filter((t) => t !== sender);

            let header = '';
            if (targets.length > 0) {
                header = `#${seq} @${sender} -> @${targets.join(', @')}`;
            } else {
                header = `#${seq} @${sender}`;
            }

            const snippet = text.replace(/[\r\n]+/g, ' ').trim();
            const desc = snippet.length > 70 ? snippet.slice(0, 67) + '...' : snippet;

            items.push({
                label: header,
                description: `${m.time || ''} · ${desc}`,
                detail: text,
                action: 'view_message',
                message: m
            });
        }
    } else {
        items.push({
            label: '$(info) В этой задаче нет сообщений доски board.jsonl',
            description: 'Сообщения появляются при параллельной работе swarm-агентов',
            action: 'noop'
        });
    }

    const picked = await vscode.window.showQuickPick(items, {
        placeHolder: `Доска board.jsonl: ${pName} [${cp.task_id}]`,
        matchOnDescription: true,
        matchOnDetail: true
    });
    if (!picked) return;

    if (picked.action === 'back') {
        return showSwarmSessionDetails(cp);
    }

    if (picked.action === 'view_message' && picked.message) {
        if (vscode.window && typeof vscode.window.showInformationMessage === 'function') {
            const choice = await vscode.window.showInformationMessage(
                `[${picked.label}]\n${picked.message.text || ''}`,
                'Копировать текст',
                'Назад к переписке'
            );
            if (choice === 'Копировать текст' && vscode.env && vscode.env.clipboard) {
                await vscode.env.clipboard.writeText(picked.message.text || '');
            }
        }
        return showSwarmBoardMessages(cp);
    }
}

/** Command: `agToolkit.runSwarm` */
async function runSwarmCommand() {
    const projectPath = await _resolveProjectFolder();
    if (!projectPath) return;

    const enginePath = resolveSwarmEnginePath(projectPath);
    if (!enginePath) {
        vscode.window.showErrorMessage(
            'Модуль команды агентов не найден (ни в workspace source/patcher, ни в расширении). Переустановите расширение.'
        );
        return;
    }

    const prompt = await vscode.window.showInputBox({
        prompt: 'Что должна сделать команда агентов? (лучше задачи по разным файлам)',
        placeHolder: 'Например: почини баг в X, добавь функцию в Y и обнови README',
        ignoreFocusOut: true
    });
    if (!prompt || !prompt.trim()) return;

    const testPick = await vscode.window.showQuickPick(
        [
            {
                label: '$(zap) Быстрая проверка: только тесты изменённых файлов (рекомендуется)',
                description: 'В 5–10 раз быстрее на каждом шаге; перед финальным слиянием прогонит все тесты',
                useAffectedTests: true
            },
            {
                label: '$(checklist) Полная проверка после каждого шага',
                description: 'Запускать все тесты проекта целиком для каждого агента',
                useAffectedTests: false
            }
        ],
        { placeHolder: 'Как проверять изменения агентов?' }
    );
    if (!testPick) return;
    const useAffectedTests = testPick.useAffectedTests;

    const lmModels = (vscode.lm && typeof vscode.lm.selectChatModels === 'function')
        ? await vscode.lm.selectChatModels({}).catch(() => [])
        : [];

    let selectedMode = 'chat';
    if (Array.isArray(lmModels) && lmModels.length > 0) {
        const modePick = await vscode.window.showQuickPick(
            [
                {
                    label: '$(rocket) Чат Antigravity (рекомендуется)',
                    description: 'Отправит план в чат и запустит параллельных агентов (/swarm)',
                    mode: 'chat'
                },
                {
                    label: '$(beaker) Фоновый процесс (VS Code LM)',
                    description: 'Автоматический запуск в фоне без окна чата',
                    mode: 'ipc'
                }
            ],
            { placeHolder: 'Где запустить команду агентов?' }
        );
        if (!modePick) return;
        selectedMode = modePick.mode;
    }

    const modelConfig = typeof loadModelConfigSync === 'function' ? loadModelConfigSync() : null;

    if (selectedMode === 'ipc') {
        startSwarmIpcSession(enginePath, {
            cmd: 'run',
            project_path: projectPath,
            request: prompt.trim(),
            max_workers: 5,
            use_affected_tests: useAffectedTests
        });
        return;
    }

    const bridge = getSwarmBridge();
    const prep = bridge
        ? await bridge.prepareSwarmTask({
            projectPath,
            request: prompt.trim(),
            enginePath,
            workspacePaths: _getWorkspacePaths(),
            extraDirs: [__dirname],
            useAffectedTests,
            modelConfig
        })
        : { ok: false, error: 'Swarm Bridge не найден' };
    if (!prep.ok || !prep.taskId) {
        vscode.window.showErrorMessage(`Ошибка запуска команды агентов: ${prep.error || 'неизвестная ошибка'}`);
        return;
    }

    startCheckpointWatcher(prep.taskId);
    await injectPromptIntoAntigravityChat(prep.dispatchPrompt, `Команда [${prep.taskId}]`);
}

/** Command: `agToolkit.resumeSwarm` */
async function resumeSwarmCommand() {
    return showSwarmHubMenu();
}

/** Command: `agToolkit.stopSwarm` */
async function stopSwarmCommand() {
    const hadActive = Boolean(_swarmProcess || _swarmPollTimer || _swarmTaskId);
    stopSwarm({ keepOutput: true });
    if (vscode.window && vscode.window.showInformationMessage) {
        vscode.window.showInformationMessage(
            hadActive
                ? 'Swarm остановлен. Прогресс сохранён в checkpoint (доступен через Resume Swarm).'
                : 'Активных процессов Swarm сейчас нет.'
        );
    }
}

function _clearAllExtensionTimers() {
    if (_startupUpdateTimer) { clearTimeout(_startupUpdateTimer); _startupUpdateTimer = null; }
    if (_startupChecksumTimer) { clearTimeout(_startupChecksumTimer); _startupChecksumTimer = null; }
    if (_startupLogWatchTimer) { clearTimeout(_startupLogWatchTimer); _startupLogWatchTimer = null; }
    if (_startupActiveQuotaTimer) { clearTimeout(_startupActiveQuotaTimer); _startupActiveQuotaTimer = null; }
    if (_startupAllQuotasTimer) { clearTimeout(_startupAllQuotasTimer); _startupAllQuotasTimer = null; }
    if (checkTimer) { clearInterval(checkTimer); checkTimer = null; }
    if (activeQuotaTimer) { clearInterval(activeQuotaTimer); activeQuotaTimer = null; }
    if (quotaTimer) { clearInterval(quotaTimer); quotaTimer = null; }
}

function deactivate() {
    _clearAllExtensionTimers();
    stopSwarmLiveTicker();
    disposeFileWatchers();
    dispose429LogWatchers();
    disposeIdeErrorGuardian();
    stopSwarm({ keepOutput: false });
}

function _resetStateForTests() {
    _clearAllExtensionTimers();
    lastKnownAccountData = null;
    lastConnectionInfo = { ok: false, ms: 0, countryCode: '', status: 'Проверка...' };
    lastProxyInfo = null;
    lastNetCheckAt = 0;
    lastRenderedBarSig = '';
    activeRefreshPromise = null;
    activeRefreshIsForced = false;
    allRefreshPromise = null;
    isRefreshingQuotas = false;
    lastActiveRefreshAt = 0;
    lastAllRefreshAt = 0;
    lastEnsuredEmail = '';
    onboardingInFlightEmail = '';
    pendingConvoRefresh = false;
    agyRestartTimestamps.length = 0;
    lastAgyRestartAt = 0;
    lastNetWasOk = null;
    lastAppliedProxyUrl = undefined;
    lastAppliedCountry = '';
    lastAppliedRefreshAt = 0;
    _quotaEngineRef = null;
    // 429 Failover Engine state reset
    _failoverToastLastShownAt = 0;
    _failoverLogLastOffset.clear();
    dispose429LogWatchers();
    // Swarm Engine state reset
    stopSwarmLiveTicker();
    disposeFileWatchers();
    _swarmLastActive = 0;
    if (_swarmStatusBar) {
        try { _swarmStatusBar.dispose(); } catch (_) {}
        _swarmStatusBar = null;
    }
    _swarmRecentCompletionState = null;
    _swarmPreviousSnapshot = null;
    _swarmActiveTaskIds.clear();
    _swarmKnownCompletedTaskIds.clear();
    stopSwarm({ keepOutput: false });
    if (_swarmBridgeRef && typeof _swarmBridgeRef._resetPythonCacheForTests === 'function') {
        _swarmBridgeRef._resetPythonCacheForTests();
    }
    _swarmBridgeRef = null;
}

module.exports = {
    activate,
    deactivate,
    computeAccountQuotaSummary,
    getAccountData,
    updateStatusBar,
    refreshNetworkConnection,
    refreshActiveQuotaBackground,
    restartAgyHubIfNeeded,
    findAgyHubPids,
    scheduleConvoQuotaRefresh,
    setupFileWatchers,
    disposeFileWatchers,
    showQuotaDetails,
    ensureOnboardingCompleted,
    // 429 Failover Engine & Error Guardian
    setup429LogWatchers,
    dispose429LogWatchers,
    collect429CandidateFiles,
    checkDirFor429,
    tryRestorePreviousConversation,
    doAutoAccountHop,
    setupIdeErrorGuardian,
    disposeIdeErrorGuardian,
    handleIdeGuardianAlert,
    runIdeScopedHeal,
    handle429Detected,
    show429Toast,
    show429PolicyMenu,
    // Model config manager
    showModelConfigMenu,
    loadModelConfigSync,
    // Multi-Agent Swarm Engine
    resolveSwarmEnginePath,
    buildSwarmDispatchPrompt,
    dispatchPromptToChatPanel,
    injectPromptIntoAntigravityChat,
    listSwarmCheckpointsSync,
    runSwarmCliAsync,
    updateSwarmStatusBarLive,
    getSwarmStatusBarItem: () => _swarmStatusBar,
    formatAgentPillText: (s, r, opts) => {
        const b = getSwarmBridge();
        return (b && typeof b.formatAgentPillText === 'function')
            ? b.formatAgentPillText(s, r, opts)
            : _formatAgentPillTextFallback(s, r);
    },
    formatAgentTooltipSections: (s, r) => {
        const b = getSwarmBridge();
        return (b && typeof b.formatAgentTooltipSections === 'function')
            ? b.formatAgentTooltipSections(s, r)
            : _formatAgentTooltipSectionsFallback(s, r);
    },
    detectRecentCompletion: (p, c, opts) => {
        const b = getSwarmBridge();
        return (b && typeof b.detectRecentCompletion === 'function')
            ? b.detectRecentCompletion(p, c, opts)
            : null;
    },
    triggerSwarmCompletion,
    quickLaunchSwarmTask,
    showSwarmSessionDetails,
    showSwarmBoardMessages,
    showSwarmHubMenu,
    getActiveConversationId,
    runSwarmCommand,
    resumeSwarmCommand,
    stopSwarmCommand,
    stopSwarm,
    _resetStateForTests
};
