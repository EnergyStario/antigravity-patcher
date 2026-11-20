"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.registerIpcHandlers = registerIpcHandlers;
const electron_1 = require("electron");
const electron_updater_1 = require("electron-updater");
const types_1 = require("./types");
const updater_1 = require("./updater");
const main_1 = __importDefault(require("electron-log/main"));
const fs = __importStar(require("fs/promises"));
const customScheme_1 = require("./customScheme");
const tray_1 = require("./tray");
const webviewBrowser_1 = require("./webviewBrowser");
const constants_1 = require("./ideInstall/constants");
const url_1 = require("url");
const wsl_1 = require("./wsl");
const menu_1 = require("./menu");
const scriptJail_1 = require("./scriptJail");
const externalUrl_1 = require("./externalUrl");
const utils_1 = require("./utils");
const wcvBrowserRegistry_1 = require("./wcvBrowserRegistry");
const wcvIpcHandlers_1 = require("./wcvIpcHandlers");
const guestDialogController_1 = require("./guestDialogController");
function buildContextMenuTemplate(items, onSelect) {
    return items.map((item) => {
        if (item.type === 'separator') {
            return { type: 'separator' };
        }
        if (item.type === 'submenu' || (item.submenu && item.submenu.length > 0)) {
            return {
                id: item.id,
                label: item.label ?? '',
                type: 'submenu',
                enabled: !item.disabled,
                submenu: buildContextMenuTemplate(item.submenu ?? [], onSelect),
            };
        }
        return {
            id: item.id,
            label: item.label ?? '',
            type: item.type ?? 'normal',
            checked: item.checked,
            enabled: !item.disabled,
            accelerator: item.accelerator,
            click: () => {
                onSelect(item.id);
            },
        };
    });
}
/**
 * Registers all IPC handlers for the main process.
 */
function registerIpcHandlers(storageManager) {
    if (process.platform === 'win32' && typeof electron_1.app?.setAppUserModelId === 'function') {
        try {
            electron_1.app.setAppUserModelId('electron.app.Antigravity');
        }
        catch { }
    }
    const getTargetHubWindow = () => {
        return (0, utils_1.getFocusedHubWindow)() ?? (0, utils_1.getHubWindows)()[0];
    };
    // Dialog
    // In WSL mode, start the folder dialog in the distro's home directory.
    async function dialogDefaultPath() {
        const distro = (0, wsl_1.getActiveWslDistro)();
        if (!distro) {
            return undefined;
        }
        try {
            return (0, wsl_1.distroToWindowsPath)(await (0, wsl_1.getDistroHome)(distro), distro);
        }
        catch {
            return undefined;
        }
    }
    // In WSL mode, translate a picked Windows path to the distro's view.
    // Returns undefined when the path is unusable (e.g. another distro).
    function mapPickedPath(picked) {
        const distro = (0, wsl_1.getActiveWslDistro)();
        if (!distro) {
            return picked;
        }
        const t = (0, wsl_1.windowsToDistroPath)(picked, distro);
        if (t.error) {
            electron_1.dialog.showErrorBox('Cannot open folder', t.error);
            return undefined;
        }
        if (t.warning) {
            void electron_1.dialog.showMessageBox({
                type: 'warning',
                message: 'Folder is on the Windows filesystem',
                detail: t.warning,
            });
        }
        return t.path;
    }
    electron_1.ipcMain.handle('dialog:open-workspace', async () => {
        const result = await electron_1.dialog.showOpenDialog({
            properties: ['openDirectory', 'createDirectory'],
            title: 'Open workspace',
            defaultPath: await dialogDefaultPath(),
        });
        if (result.canceled || result.filePaths.length === 0) {
            return undefined;
        }
        return mapPickedPath(result.filePaths[0]);
    });
    // Like 'dialog:open-workspace' but allows selecting multiple folders,
    // returning an array of paths (empty if cancelled).
    electron_1.ipcMain.handle('dialog:open-workspaces', async () => {
        const result = await electron_1.dialog.showOpenDialog({
            properties: ['openDirectory', 'createDirectory', 'multiSelections'],
            title: 'Open workspaces',
            defaultPath: await dialogDefaultPath(),
        });
        if (result.canceled || result.filePaths.length === 0) {
            return [];
        }
        return result.filePaths
            .map(mapPickedPath)
            .filter((p) => p !== undefined);
    });
    // Auto-updater
    electron_1.ipcMain.handle('updater:apply', async () => {
        (0, updater_1.broadcastState)({ type: types_1.UpdateState.Ready });
    });
    electron_1.ipcMain.handle('updater:quit-and-install', () => {
        if (!electron_1.app.isPackaged) {
            console.log('[AutoUpdater] Skipping quitAndInstall (requires a packaged app).');
            return;
        }
        electron_updater_1.autoUpdater.quitAndInstall();
    });
    electron_1.ipcMain.handle('updater:get-state', () => {
        return (0, updater_1.getLastState)();
    });
    // Notifications
    electron_1.ipcMain.handle('notification:send', (_, options) => {
        const notification = new electron_1.Notification({
            title: options.title,
            body: options.body,
            silent: options.silent ?? false,
        });
        notification.on('click', () => {
            const win = (0, utils_1.getHubWindows)()[0];
            if (win) {
                if (win.isMinimized()) {
                    win.restore();
                }
                win.show();
                win.focus();
                if (options.payload) {
                    win.webContents.send('notification:clicked', options.payload);
                }
            }
        });
        notification.show();
    });
    // Note: copied from our desktop AGY implementation:
    // vs/platform/nativeNotification/electron-main/electronNotificationService.ts
    electron_1.ipcMain.handle('notification:open-system-preferences', async () => {
        if (process.platform === 'darwin') {
            void electron_1.shell.openExternal('x-apple.systempreferences:com.apple.preference.notifications');
        }
        else if (process.platform === 'win32') {
            void electron_1.shell.openExternal('ms-settings:notifications');
        }
        else if (process.platform === 'linux') {
            const { exec } = await Promise.resolve().then(() => __importStar(require('child_process')));
            const commands = [
                'gnome-control-center notifications',
                'systemsettings kcm_notifications',
                'xfce4-notifyd-config',
                'gnome-control-center',
                'systemsettings',
            ];
            for (const command of commands) {
                try {
                    exec(command);
                    return; // If one command executes without immediate error, assume success for now
                }
                catch {
                    // Try next
                }
            }
        }
    });
    // Storage
    electron_1.ipcMain.handle('storage:get-items', async () => {
        return storageManager.getItems();
    });
    electron_1.ipcMain.handle('storage:update-items', async (_event, changes) => {
        await storageManager.updateItems(changes);
    });
    // Logs
    electron_1.ipcMain.handle('logs:electron', async () => {
        try {
            const logPath = main_1.default.transports.file.getFile().path;
            const contents = await fs.readFile(logPath, 'utf-8');
            return contents;
        }
        catch (err) {
            return `Failed to read logs: ${String(err)}`;
        }
    });
    // Sidecar extension custom scheme
    electron_1.ipcMain.handle('extensions:send-authorities', async (_event, authorities) => {
        customScheme_1.extensionAuthorities.clear();
        for (const [key, value] of Object.entries(authorities)) {
            customScheme_1.extensionAuthorities.set(key, value);
        }
    });
    // Agent
    electron_1.ipcMain.handle('agent:update-active-count', async (_event, count) => {
        (0, tray_1.updateTrayAgentCount)(count);
    });
    // Window
    electron_1.ipcMain.handle('window:set-title-bar-overlay', async (_event, options) => {
        const win = getTargetHubWindow();
        if (win && process.platform === 'win32') {
            win.setTitleBarOverlay({
                color: options.color,
                symbolColor: options.symbolColor,
                height: 30,
            });
        }
    });
    electron_1.ipcMain.handle('window:minimize', async () => {
        const win = getTargetHubWindow();
        if (win) {
            win.minimize();
        }
    });
    electron_1.ipcMain.handle('window:maximize', async () => {
        const win = getTargetHubWindow();
        if (win) {
            win.maximize();
        }
    });
    electron_1.ipcMain.handle('window:unmaximize', async () => {
        const win = getTargetHubWindow();
        if (win) {
            win.unmaximize();
        }
    });
    electron_1.ipcMain.handle('window:is-maximized', async () => {
        const win = getTargetHubWindow();
        return win ? win.isMaximized() : false;
    });
    electron_1.ipcMain.handle('window:close', async () => {
        const win = getTargetHubWindow();
        if (win) {
            win.close();
        }
    });
    electron_1.ipcMain.handle('window:toggle-devtools', async () => {
        const win = getTargetHubWindow();
        if (win) {
            win.webContents.toggleDevTools();
        }
    });
    let activeContextMenu = null;
    electron_1.ipcMain.handle('window:show-context-menu', async (_event, items) => {
        const win = getTargetHubWindow();
        if (!win || !Array.isArray(items) || items.length === 0) {
            return null;
        }
        activeContextMenu?.closePopup?.(win);
        return new Promise((resolve) => {
            let settled = false;
            const template = buildContextMenuTemplate(items, (id) => {
                if (!settled) {
                    settled = true;
                    activeContextMenu = null;
                    resolve(id);
                }
            });
            const menu = electron_1.Menu.buildFromTemplate(template);
            activeContextMenu = menu;
            menu.popup({
                window: win,
                callback: () => {
                    setImmediate(() => {
                        if (activeContextMenu === menu) {
                            activeContextMenu = null;
                        }
                        if (!settled) {
                            settled = true;
                            resolve(null);
                        }
                    });
                },
            });
        });
    });
    electron_1.ipcMain.handle('window:close-context-menu', () => {
        const win = getTargetHubWindow();
        activeContextMenu?.closePopup?.(win);
        activeContextMenu = null;
    });
    // Zoom — main-process source of truth so the level is reliably persisted
    // across restarts on all platforms (works around a Chromium quirk on Windows
    // where renderer-only webFrame.setZoomLevel changes aren't cached).
    const ZOOM_LEVEL_KEY = 'zoomLevel';
    const ZOOM_STEP = 0.5;
    electron_1.ipcMain.handle('window:zoom-in', async () => {
        const win = getTargetHubWindow();
        if (win) {
            const newLevel = win.webContents.getZoomLevel() + ZOOM_STEP;
            win.webContents.setZoomLevel(newLevel);
            await storageManager.updateItems({
                [ZOOM_LEVEL_KEY]: String(newLevel),
            });
        }
    });
    electron_1.ipcMain.handle('window:zoom-out', async () => {
        const win = getTargetHubWindow();
        if (win) {
            const newLevel = win.webContents.getZoomLevel() - ZOOM_STEP;
            win.webContents.setZoomLevel(newLevel);
            await storageManager.updateItems({
                [ZOOM_LEVEL_KEY]: String(newLevel),
            });
        }
    });
    electron_1.ipcMain.handle('window:reset-zoom', async () => {
        const win = getTargetHubWindow();
        if (win) {
            win.webContents.setZoomLevel(0);
            await storageManager.updateItems({ [ZOOM_LEVEL_KEY]: String(0) });
        }
    });
    // Auto-updater manual check
    electron_1.ipcMain.handle('updater:check-for-updates', () => {
        (0, updater_1.checkForUpdates)(true);
    });
    // Safe external shell launch
    electron_1.ipcMain.handle('shell:open-external', async (_event, url) => {
        await (0, externalUrl_1.openExternalSafely)(url, 'confirmed');
    });
    electron_1.ipcMain.handle('shell:reveal-in-file-picker', (_event, path) => {
        const distro = (0, wsl_1.getActiveWslDistro)();
        if (distro) {
            const posixPath = (0, url_1.fileURLToPath)(path, { windows: false });
            electron_1.shell.showItemInFolder((0, wsl_1.distroToWindowsPath)(posixPath, distro));
            return;
        }
        electron_1.shell.showItemInFolder((0, url_1.fileURLToPath)(path));
    });
    electron_1.ipcMain.handle('webview:set-download-dir', (event, webContentsId, downloadDir) => {
        if (typeof webContentsId !== 'number' ||
            !Number.isInteger(webContentsId) ||
            webContentsId <= 0) {
            throw new Error(`Invalid webContentsId: ${String(webContentsId)}`);
        }
        if (downloadDir !== null && typeof downloadDir !== 'string') {
            throw new Error('downloadDir must be a string or null');
        }
        if (event?.senderFrame?.parent) {
            throw new Error('Unauthorized webview:set-download-dir sender: subframe cannot configure downloads');
        }
        const sender = event?.sender;
        if (!sender || sender.isDestroyed?.()) {
            throw new Error(`WebContents ${webContentsId} is not owned by the calling renderer`);
        }
        const wc = electron_1.webContents.fromId(webContentsId);
        if (!(0, webviewBrowser_1.isOwnedBrowserGuestWebContents)(wc, sender.id)) {
            throw new Error(`WebContents ${webContentsId} is not owned by the calling renderer`);
        }
        return (0, webviewBrowser_1.setGuestDownloadDirectory)(wc, downloadDir);
    });
    // IDE installation check
    electron_1.ipcMain.handle('ide:is-installed', async () => {
        try {
            // Check standard installation path (works even if the app has never been launched).
            await fs.stat((0, constants_1.getIdeInstallPath)());
            return true;
        }
        catch {
            return false;
        }
    });
    // Per-guest renderer working-set memory in bytes for memory-budgeted LRU eviction.
    electron_1.ipcMain.handle('webview:get-memory-usage', async (event, webContentsIds) => {
        const sender = event?.sender;
        if (!sender ||
            sender.isDestroyed() ||
            event?.senderFrame?.parent ||
            (event?.senderFrame &&
                sender.mainFrame &&
                event.senderFrame !== sender.mainFrame) ||
            !Array.isArray(webContentsIds) ||
            webContentsIds.length === 0) {
            return {};
        }
        const MAX_QUERIED_GUESTS = 64;
        const seenIds = new Set();
        const ownedGuests = [];
        for (const rawId of webContentsIds) {
            if (seenIds.size >= MAX_QUERIED_GUESTS) {
                break;
            }
            if (typeof rawId !== 'number' ||
                !Number.isInteger(rawId) ||
                rawId <= 0 ||
                seenIds.has(rawId)) {
                continue;
            }
            seenIds.add(rawId);
            const wc = electron_1.webContents.fromId(rawId);
            const isWcv = wcvBrowserRegistry_1.wcvBrowserRegistry.isRegisteredWcv(rawId);
            if (!wc || wc.isDestroyed() || (wc.getType() !== 'webview' && !isWcv)) {
                continue;
            }
            const isOwned = isWcv
                ? wcvBrowserRegistry_1.wcvBrowserRegistry.isOwnedBySender(rawId, sender.id)
                : Boolean(sender.id !== undefined && wc.hostWebContents?.id === sender.id);
            if (!isOwned) {
                continue;
            }
            let pid;
            try {
                pid = wc.getOSProcessId?.();
            }
            catch {
                pid = undefined;
            }
            ownedGuests.push({ webContentsId: rawId, pid });
        }
        if (ownedGuests.length === 0) {
            return {};
        }
        const bytesByWcId = wcvBrowserRegistry_1.wcvBrowserRegistry.computeGuestMemoryBytes(ownedGuests);
        const out = {};
        for (const [id, bytes] of bytesByWcId.entries()) {
            out[id] = bytes;
        }
        return out;
    });
    // WSL environment switching (Windows only). Lets the web UI (e.g. the
    // Settings page) discover WSL distros and relaunch the app into/out of one.
    electron_1.ipcMain.handle('wsl:get-state', async () => {
        if (!(0, wsl_1.isWslAvailable)()) {
            return { available: false, activeDistro: null, distros: [] };
        }
        try {
            const distros = await (0, wsl_1.listWslDistros)();
            return {
                available: true,
                activeDistro: (0, wsl_1.getActiveWslDistro)() ?? null,
                distros: distros.map((d) => ({ name: d.name, isDefault: d.isDefault })),
            };
        }
        catch (err) {
            main_1.default.error('wsl:get-state failed:', err);
            return { available: false, activeDistro: null, distros: [] };
        }
    });
    // Relaunches into the given distro, or locally when `distro` is empty.
    electron_1.ipcMain.handle('wsl:connect', (_event, distro) => {
        (0, menu_1.relaunchWithWslDistro)(distro || '');
    });
    // Execute JavaScript inside an Electron <webview> guest's isolated world.
    // Only the renderer that hosts the <webview> may target it, and worldId is
    // bounded to [MIN, MAX] so it can never hit the main (0) or preload worlds
    // and stays well below Chromium's fatal ISOLATED_WORLD_ID_MAX CHECK.
    const MIN_ISOLATED_WORLD_ID = 1000;
    const MAX_ISOLATED_WORLD_ID = 100000;
    electron_1.ipcMain.handle('webview:execute-js-isolated-world', async (event, webContentsId, worldId, code) => {
        if (typeof webContentsId !== 'number' ||
            !Number.isInteger(webContentsId) ||
            webContentsId <= 0) {
            throw new Error(`Invalid webContentsId: ${String(webContentsId)}`);
        }
        if (typeof worldId !== 'number' ||
            !Number.isInteger(worldId) ||
            worldId < MIN_ISOLATED_WORLD_ID ||
            worldId > MAX_ISOLATED_WORLD_ID) {
            throw new Error(`Invalid isolated worldId: ${String(worldId)} (must be in [${MIN_ISOLATED_WORLD_ID}, ${MAX_ISOLATED_WORLD_ID}])`);
        }
        if (typeof code !== 'string') {
            throw new Error('code must be a string');
        }
        const wc = electron_1.webContents.fromId(webContentsId);
        const isWcv = wcvBrowserRegistry_1.wcvBrowserRegistry.isRegisteredWcv(webContentsId);
        if (!wc || wc.isDestroyed() || (wc.getType() !== 'webview' && !isWcv)) {
            throw new Error(`Invalid or non-webview webContents id: ${webContentsId}`);
        }
        const sender = event?.sender;
        const isOwned = isWcv
            ? wcvBrowserRegistry_1.wcvBrowserRegistry.isOwnedBySender(webContentsId, sender?.id)
            : wc.hostWebContents?.id === sender?.id;
        if (!sender || sender.isDestroyed() || !isOwned) {
            throw new Error(`WebContents ${webContentsId} is not owned by the calling renderer`);
        }
        if (!event?.senderFrame ||
            event.senderFrame !== sender.mainFrame ||
            event.senderFrame.parent) {
            throw new Error('webview:execute-js-isolated-world must be called from the main frame');
        }
        return wc.executeJavaScriptInIsolatedWorld(worldId, [{ code }], false);
    });
    const resolveOwnedGuestWebContents = (event, webContentsId) => {
        if (typeof webContentsId !== 'number' ||
            !Number.isInteger(webContentsId) ||
            webContentsId <= 0) {
            throw new Error(`Invalid webContentsId: ${String(webContentsId)}`);
        }
        const wc = electron_1.webContents.fromId(webContentsId);
        const isWcv = wcvBrowserRegistry_1.wcvBrowserRegistry.isRegisteredWcv(webContentsId);
        if (!wc || wc.isDestroyed() || (wc.getType() !== 'webview' && !isWcv)) {
            throw new Error(`Invalid or non-webview webContents id: ${webContentsId}`);
        }
        const sender = event?.sender;
        const isOwned = isWcv
            ? wcvBrowserRegistry_1.wcvBrowserRegistry.isOwnedBySender(webContentsId, sender?.id)
            : wc.hostWebContents?.id === sender?.id;
        if (!sender || sender.isDestroyed() || !isOwned) {
            throw new Error(`WebContents ${webContentsId} is not owned by the calling renderer`);
        }
        return { wc, sender };
    };
    electron_1.ipcMain.handle('webview:set-dialog-policy', async (event, webContentsId, policy) => {
        const { wc, sender } = resolveOwnedGuestWebContents(event, webContentsId);
        const safePolicy = {
            enabled: Boolean(policy?.enabled),
            ...(policy?.action === 'accept' ||
                policy?.action === 'dismiss' ||
                policy?.action === 'manual'
                ? { action: policy.action }
                : {}),
            ...(typeof policy?.promptText === 'string'
                ? { promptText: policy.promptText }
                : {}),
            ...(typeof policy?.tabKey === 'string' ? { tabKey: policy.tabKey } : {}),
        };
        return guestDialogController_1.guestDialogController.setPolicy(wc, sender, safePolicy);
    });
    electron_1.ipcMain.handle('webview:handle-javascript-dialog', async (event, webContentsId, decision) => {
        const { wc } = resolveOwnedGuestWebContents(event, webContentsId);
        return guestDialogController_1.guestDialogController.handleDialog(wc, {
            ...(typeof decision?.dialogId === 'string'
                ? { dialogId: decision.dialogId }
                : {}),
            accept: Boolean(decision?.accept),
            ...(typeof decision?.promptText === 'string'
                ? { promptText: decision.promptText }
                : {}),
        });
    });
    // Execute an untrusted agent script inside a single-use, zero-egress
    // Electron script jail and transfer the paired MessagePort to the caller.
    electron_1.ipcMain.handle('script-jail:run', async (event, req) => {
        return (0, scriptJail_1.runScriptInJail)(event?.sender, req, event?.senderFrame);
    });
    electron_1.ipcMain.handle('script-jail:abort', (event, jailId) => {
        return (0, scriptJail_1.abortScriptInJail)(event?.sender, jailId, event?.senderFrame);
    });

    // === AG Toolkit Desktop IPC Handlers ===
    const _agFs = require('fs');
    const _agPath = require('path');
    const _agOs = require('os');
    const { execFile: _agExecFile } = require('child_process');

    const _agToolsDir = process.env.AG_TOOLS_DIR || _agPath.join(_agOs.homedir(), '.antigravity_tools');
    const _agGeminiDir = process.env.AG_GEMINI_DIR || _agPath.join(_agOs.homedir(), '.gemini');
    const _agSwitcherPy = _agPath.join(_agToolsDir, 'account_switcher.py');
    const _agQuotaEnginePath = _agPath.join(_agToolsDir, 'quota_engine.js');
    const _agSwarmBridgePath = _agPath.join(_agToolsDir, 'swarm_bridge.js');
    const _agErrorGuardianPath = _agPath.join(_agToolsDir, 'error_guardian.js');
    const _agModuleCache = new Map();

    function _loadCoreModule(primaryPath, relName) {
        const entry = _agModuleCache.get(relName) || { ref: null, mtime: 0 };
        const candidates = [primaryPath, _agPath.resolve(__dirname, '..', 'core', relName)];
        for (const p of candidates) {
            try {
                if (!_agFs.existsSync(p)) continue;
                const st = _agFs.statSync(p);
                if (entry.ref && st.mtimeMs === entry.mtime) {
                    return entry.ref;
                }
                delete require.cache[p];
                entry.ref = require(p);
                entry.mtime = st.mtimeMs;
                _agModuleCache.set(relName, entry);
                return entry.ref;
            } catch (_) {}
        }
        return entry.ref;
    }

    function _getQuotaEngine() {
        return _loadCoreModule(_agQuotaEnginePath, 'quota_engine.js');
    }

    function _getSwarmBridge() {
        return _loadCoreModule(_agSwarmBridgePath, 'swarm_bridge.js');
    }

    function _getErrorGuardian() {
        return _loadCoreModule(_agErrorGuardianPath, 'error_guardian.js');
    }

    let _agPyBin = null;
    function _agCleanEnv() {
        const cleanEnv = Object.assign({}, process.env, {
            PYTHONIOENCODING: 'utf-8',
            PYTHONUTF8: '1'
        });
        for (const k of Object.keys(cleanEnv)) {
            const uk = k.toUpperCase();
            if (uk.startsWith('ELECTRON_') || uk.startsWith('VSCODE_') || uk.startsWith('ATOM_SHELL_') ||
                uk === 'NODE_OPTIONS' || uk === 'NODE_ENV') {
                delete cleanEnv[k];
            }
        }
        return cleanEnv;
    }

    function _agGetPythonCandidates() {
        if (_agPyBin) {
            return [_agPyBin];
        }
        try {
            const eng = _getQuotaEngine();
            if (eng && typeof eng.getPythonCandidates === 'function') {
                const list = eng.getPythonCandidates();
                if (Array.isArray(list) && list.length > 0) return list;
            }
        } catch (_) {}

        const isWin = process.platform === 'win32';
        const rawPaths = [];
        if (process.env.AG_PYTHON_PATH) {
            rawPaths.push(process.env.AG_PYTHON_PATH);
        }
        const resourcesPath = process.resourcesPath || '';
        const execDir = process.execPath ? _agPath.dirname(process.execPath) : '';
        const roots = [
            resourcesPath,
            execDir ? _agPath.join(execDir, 'resources') : '',
            _agPath.join(_agGeminiDir, 'antigravity'),
            _agToolsDir
        ];
        if (isWin) {
            const localApp = process.env.LOCALAPPDATA || _agPath.join(_agOs.homedir(), 'AppData', 'Local');
            roots.push(
                _agPath.join(localApp, 'antigravity'),
                _agPath.join(localApp, 'Programs', 'antigravity', 'resources'),
                _agPath.join(localApp, 'Programs', 'Antigravity IDE', 'resources')
            );
        } else if (process.platform === 'darwin') {
            roots.push('/Applications/Antigravity.app/Contents/Resources');
        } else {
            roots.push('/opt/antigravity/resources', '/usr/share/antigravity/resources');
        }

        for (const root of roots) {
            if (!root) continue;
            if (isWin) {
                rawPaths.push(
                    _agPath.join(root, 'bin', 'python.exe'),
                    _agPath.join(root, 'python', 'python.exe'),
                    _agPath.join(root, 'python', 'Scripts', 'python.exe'),
                    _agPath.join(root, 'app', 'python', 'python.exe')
                );
            } else {
                rawPaths.push(
                    _agPath.join(root, 'bin', 'python3'),
                    _agPath.join(root, 'bin', 'python'),
                    _agPath.join(root, 'python', 'bin', 'python3'),
                    _agPath.join(root, 'python', 'bin', 'python')
                );
            }
        }

        const out = [];
        const seen = new Set();
        for (const p of rawPaths) {
            if (!p || seen.has(p)) continue;
            seen.add(p);
            try {
                if (_agFs.existsSync(p)) out.push(p);
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

    function _agRunPython(args, timeoutMs, cb) {
        const candidates = _agGetPythonCandidates();
        const env = _agCleanEnv();
        const tryIdx = (idx) => {
            if (idx >= candidates.length) {
                cb(new Error('Python executable not found'), '', '');
                return;
            }
            const bin = candidates[idx];
            const finalArgs = bin === 'py' ? ['-3', ...args] : args;
            _agExecFile(bin, finalArgs, { timeout: timeoutMs, env, windowsHide: true }, (err, stdout, stderr) => {
                if (err && err.code === 'ENOENT') {
                    if (_agPyBin === bin) _agPyBin = null;
                    tryIdx(idx + 1);
                    return;
                }
                _agPyBin = bin;
                cb(err, stdout, stderr);
            });
        };
        tryIdx(0);
    }

    const _countryMap = {
        'DE': 'Германия', 'NL': 'Нидерланды', 'US': 'США', 'FI': 'Финляндия',
        'FR': 'Франция', 'GB': 'Великобритания', 'UK': 'Великобритания',
        'SE': 'Швеция', 'PL': 'Польша', 'CH': 'Швейцария', 'AT': 'Австрия',
        'ES': 'Испания', 'IT': 'Италия', 'TR': 'Турция', 'KZ': 'Казахстан',
        'UA': 'Украина', 'RU': 'Россия', 'JP': 'Япония', 'SG': 'Сингапур',
        'CA': 'Канада', 'AE': 'ОАЭ', 'IL': 'Израиль', 'CZ': 'Чехия'
    };

    let _cachedNet = { country: 'Проверка...', countryCode: '', ms: 0, ok: true, status: 'Проверка...', proxyUrl: null };
    let _netProbeInFlight = null;

    function _detectNetwork(forceProxyRefresh = false) {
        if (!forceProxyRefresh && _netProbeInFlight) {
            return _netProbeInFlight;
        }
        const work = (async () => {
            const eng = _getQuotaEngine();
            if (eng && typeof eng.detectNetwork === 'function') {
                const res = await eng.detectNetwork({ forceProxyRefresh });
                _cachedNet = res;
                return res;
            }
            return new Promise((resolve) => {
                const https = require('https');
                const start = Date.now();
                const req = https.get('https://cloudflare.com/cdn-cgi/trace', { timeout: 3500, rejectUnauthorized: false }, (res) => {
                    let d = '';
                    res.on('data', chunk => d += chunk);
                    res.on('end', () => {
                        const ms = Date.now() - start;
                        const code = (d.match(/loc=([A-Z]{2})/) || [])[1] || '';
                        const name = _countryMap[code] || code || 'Онлайн';
                        const net = { country: name, countryCode: code, ms, ok: true, status: `${ms}ms`, proxyUrl: null };
                        _cachedNet = net;
                        resolve(net);
                    });
                });
                req.on('error', () => {
                    const net = { country: 'Офлайн', countryCode: '', ms: 0, ok: false, status: 'Офлайн / таймаут', proxyUrl: null };
                    _cachedNet = net;
                    resolve(net);
                });
                req.on('timeout', () => {
                    req.destroy();
                    const net = { country: 'Офлайн', countryCode: '', ms: 0, ok: false, status: 'Таймаут', proxyUrl: null };
                    _cachedNet = net;
                    resolve(net);
                });
            });
        })().finally(() => {
            _netProbeInFlight = null;
        });
        if (!forceProxyRefresh) _netProbeInFlight = work;
        return work;
    }

    function _agGetTargetWindows() {
        try {
            const hubWins = (typeof utils_1.getHubWindows === 'function')
                ? (0, utils_1.getHubWindows)()
                : null;
            const allWins = electron_1.BrowserWindow.getAllWindows();
            const merged = [];
            const seen = new Set();
            for (const list of [hubWins, allWins]) {
                if (!Array.isArray(list)) continue;
                for (const w of list) {
                    if (!w || w.isDestroyed?.() || seen.has(w)) continue;
                    seen.add(w);
                    merged.push(w);
                }
            }
            return merged;
        } catch (_) {
            try { return electron_1.BrowserWindow.getAllWindows(); } catch (_) { return []; }
        }
    }

    function _agHasVisibleWindow() {
        try {
            return _agGetTargetWindows().some((w) => {
                try { return !w.isDestroyed() && w.isVisible() && !w.isMinimized(); } catch (_) { return false; }
            });
        } catch (_) {
            return false;
        }
    }

    _detectNetwork(false).catch(() => {});
    const _agNetTimer = setInterval(() => {
        if (_agHasVisibleWindow()) {
            _detectNetwork(false).catch(() => {});
        }
    }, 300000);
    if (typeof _agNetTimer.unref === 'function') _agNetTimer.unref();

    function _loadStandaloneStateDirect() {
        try {
            const eng = _getQuotaEngine();
            if (eng && typeof eng.loadState === 'function') {
                const st = eng.loadState({ appTarget: 'standalone' });
                return {
                    ok: true,
                    activeAccount: st.activeAccount,
                    accounts: st.accounts,
                    country: _cachedNet.country,
                    countryCode: _cachedNet.countryCode,
                    ping: _cachedNet.ms,
                    networkOk: _cachedNet.ok,
                    networkStatus: _cachedNet.status || '',
                    proxyUrl: _cachedNet.proxyUrl || null
                };
            }

            const accIndexFile = _agPath.join(_agToolsDir, 'accounts.json');
            let accountsData = { accounts: [], current_standalone_account_id: null };
            if (_agFs.existsSync(accIndexFile)) {
                accountsData = JSON.parse(_agFs.readFileSync(accIndexFile, 'utf8'));
            }

            const detailsById = new Map();
            for (const acc of (accountsData.accounts || [])) {
                const detFile = _agPath.join(_agToolsDir, 'accounts', `${acc.id}.json`);
                if (_agFs.existsSync(detFile)) {
                    try {
                        detailsById.set(acc.id, JSON.parse(_agFs.readFileSync(detFile, 'utf8')));
                    } catch (_) {}
                }
            }

            const activeId = accountsData.current_standalone_account_id;
            const active = activeId ? (detailsById.get(activeId) || null) : null;
            return {
                ok: true,
                activeAccount: active,
                accounts: accountsData.accounts || [],
                country: _cachedNet.country,
                countryCode: _cachedNet.countryCode,
                ping: _cachedNet.ms,
                networkOk: _cachedNet.ok,
                networkStatus: _cachedNet.status || '',
                proxyUrl: _cachedNet.proxyUrl || null
            };
        } catch (e) {
            return { ok: false, error: String(e) };
        }
    }

    async function _syncStandaloneOnboarding(activeAccount) {
        try {
            if (!storageManager || typeof storageManager.updateItems !== 'function') return;
            if (!activeAccount || !activeAccount.email) return;
            const gcpStr = (activeAccount.quota && activeAccount.quota.uses_gcp_tos) ? 'true' : 'false';
            const desired = {
                'agy.onboarding.lastLoginUsername': activeAccount.email,
                'agy.onboarding.lastLoginIsGcpTos': gcpStr,
                'agy.onboarding.welcomeModalSeen': 'true',
                'jetski.onboarding.lastLoginUsername': activeAccount.email,
                'jetski.onboarding.lastLoginIsGcpTos': gcpStr,
                'jetski.onboarding.welcomeModalSeen': 'true',
                'jetski.onboarding.cloudResourceDisclosureSeen': 'true'
            };
            const staleKeys = [
                'agy.onboarding.lastLoginStrategy',
                'jetski.onboarding.lastLoginStrategy'
            ];
            let current = {};
            if (typeof storageManager.getItems === 'function') {
                current = (await Promise.resolve(storageManager.getItems())) || {};
            }
            const changes = {};
            let dirty = false;
            for (const [k, v] of Object.entries(desired)) {
                if (current[k] !== v) {
                    changes[k] = v;
                    dirty = true;
                }
            }
            for (const sk of staleKeys) {
                if (Object.prototype.hasOwnProperty.call(current, sk) && current[sk] !== null && current[sk] !== undefined) {
                    changes[sk] = null;
                    dirty = true;
                }
            }
            if (dirty) {
                await Promise.resolve(storageManager.updateItems(changes));
            }
        } catch (_) {}
    }

    try {
        const initState = _loadStandaloneStateDirect();
        if (initState && initState.activeAccount) {
            void _syncStandaloneOnboarding(initState.activeAccount);
        }
    } catch (_) {}

    let _agLastGetStateSyncAt = 0;
    electron_1.ipcMain.handle('ag:get-state', async () => {
        const st = _loadStandaloneStateDirect();
        if (st && st.activeAccount) {
            await _syncStandaloneOnboarding(st.activeAccount);
        }
        const now = Date.now();
        if ((now - _agLastGetStateSyncAt) > 20000) {
            _agLastGetStateSyncAt = now;
            const eng = _getQuotaEngine();
            if (eng && typeof eng.syncLiveAuth === 'function') {
                const prevId = st && st.activeAccount ? st.activeAccount.id : '';
                const prevCount = st && Array.isArray(st.accounts) ? st.accounts.length : 0;
                void eng.syncLiveAuth({ appTarget: 'standalone', minAgeSec: 20 }).then((res) => {
                    if (!res || !res.ok || res.cached) return;
                    const nextSt = _loadStandaloneStateDirect();
                    const nextId = nextSt && nextSt.activeAccount ? nextSt.activeAccount.id : '';
                    const nextCount = nextSt && Array.isArray(nextSt.accounts) ? nextSt.accounts.length : 0;
                    if (nextId !== prevId || nextCount !== prevCount) {
                        if (nextSt && nextSt.activeAccount) {
                            void _syncStandaloneOnboarding(nextSt.activeAccount);
                        }
                        _agPushStateUpdated();
                    }
                }).catch(() => {});
            }
        }
        return st;
    });

    let _agQuotaRefreshInFlight = null;
    let _agLastLsRestartAt = 0;
    const _AG_LS_RESTART_COOLDOWN_MS = 65000;

    function _agPushChannel(channel, data) {
        try {
            for (const w of _agGetTargetWindows()) {
                try {
                    if (!w || w.isDestroyed?.()) continue;
                    const wc = (typeof utils_1.getHubWebContents === 'function' ? (0, utils_1.getHubWebContents)(w) : null)
                        || w.webContents;
                    if (wc && !wc.isDestroyed?.()) {
                        wc.send(channel, data);
                    }
                } catch (_) {}
            }
        } catch (_) {}
    }

    function _agPushStateUpdated() {
        _agPushChannel('ag:state-updated');
    }

    async function _agWaitForLsPort(lsMod, prevPort, prevPid, timeoutMs = 8000) {
        if (!lsMod || typeof lsMod.getLsPort !== 'function') {
            await new Promise((r) => setTimeout(r, Math.min(timeoutMs, 2600)));
            return 0;
        }
        const start = Date.now();
        let sawDown = !prevPort && !prevPid;
        while ((Date.now() - start) < timeoutMs) {
            let port = 0;
            let proc = null;
            try { port = Number(lsMod.getLsPort() || 0); } catch (_) { port = 0; }
            try { proc = typeof lsMod.getLsProcess === 'function' ? lsMod.getLsProcess() : null; } catch (_) { proc = null; }
            const alive = Boolean(proc && !proc.killed && proc.exitCode === null);
            const pid = Number(proc?.pid || 0);
            if (!alive || port === 0 || (prevPid && pid && pid !== prevPid)) {
                sawDown = true;
            }
            if (alive && port > 0 && (port !== prevPort || (prevPid && pid && pid !== prevPid) || sawDown)) {
                return port;
            }
            await new Promise((r) => setTimeout(r, 150));
        }
        try {
            const port = Number(lsMod.getLsPort() || 0);
            const proc = typeof lsMod.getLsProcess === 'function' ? lsMod.getLsProcess() : null;
            if (proc && !proc.killed && proc.exitCode === null && port > 0 && sawDown) {
                return port;
            }
        } catch (_) {}
        return 0;
    }

    function _agNavigateWindowsToUrl(targetUrl) {
        if (!targetUrl) return false;
        let navigated = false;
        for (const w of _agGetTargetWindows()) {
            try {
                if (!w || w.isDestroyed?.()) continue;
                const wc = (typeof utils_1.getHubWebContents === 'function' ? (0, utils_1.getHubWebContents)(w) : null)
                    || w.webContents;
                if (!wc || wc.isDestroyed?.()) continue;
                const curUrl = String(wc.getURL?.() || '');
                if (curUrl === targetUrl) continue;
                if (typeof w.loadURL === 'function') {
                    void Promise.resolve(w.loadURL(targetUrl)).catch(() => {});
                    navigated = true;
                } else if (typeof wc.loadURL === 'function') {
                    void Promise.resolve(wc.loadURL(targetUrl)).catch(() => {});
                    navigated = true;
                }
            } catch (_) {}
        }
        return navigated;
    }

    electron_1.ipcMain.handle('ag:switch-account', async (_event, targetId) => {
        return new Promise((resolve) => {
            if (!_agFs.existsSync(_agSwitcherPy)) {
                resolve({ ok: false, error: 'account_switcher.py not found' });
                return;
            }
            let lsMod = null;
            try { lsMod = require('./languageServer'); } catch (_) { lsMod = null; }
            if (lsMod) {
                try { lsMod.setIntentionalTermination?.(false); } catch (_) {}
                try { lsMod.resetRestartCount?.(); } catch (_) {}
            }
            const prevProc = typeof lsMod?.getLsProcess === 'function' ? lsMod.getLsProcess() : null;
            const prevPid = Number(prevProc?.pid || 0);
            const prevPort = typeof lsMod?.getLsPort === 'function' ? Number(lsMod.getLsPort() || 0) : 0;

            _agRunPython([_agSwitcherPy, 'switch', String(targetId), '--app', 'standalone'], 15000, async (err, stdout, stderr) => {
                let res = { ok: false, error: 'Не удалось переключить аккаунт' };
                if (stdout) {
                    try {
                        const parsed = JSON.parse(String(stdout).trim());
                        if (parsed && typeof parsed === 'object') {
                            res = parsed;
                        }
                    } catch (_) {}
                }
                if (err && res.ok !== true) {
                    const errMsg = String((stderr && String(stderr).trim()) || err.message || err);
                    if (!res.error || res.error === 'Не удалось переключить аккаунт') {
                        res.error = errMsg;
                    }
                    res.ok = false;
                } else if (!err && res.ok === undefined) {
                    res.ok = true;
                }

                if (!res.ok) {
                    resolve(res);
                    return;
                }

                if (res.restarting === true) {
                    resolve(res);
                    return;
                }

                try {
                    const nextState = _loadStandaloneStateDirect();
                    if (nextState && nextState.activeAccount) {
                        await _syncStandaloneOnboarding(nextState.activeAccount);
                    }
                } catch (_) {}

                resolve(res);

                void (async () => {
                    try {
                        const livePort = await _agWaitForLsPort(lsMod, prevPort, prevPid, 8000);
                        if (livePort > 0) {
                            await new Promise((r) => setTimeout(r, 350));
                            _agNavigateWindowsToUrl(`https://127.0.0.1:${livePort}/`);
                        }
                        const eng = _getQuotaEngine();
                        if (eng && typeof eng.refreshActive === 'function') {
                            await eng.refreshActive({ appTarget: 'standalone', force: true, minAgeSec: 0 }).catch(() => null);
                        }
                        _detectNetwork(false).catch(() => {});
                    } catch (_) {}
                    _agPushStateUpdated();
                })();
            });
        });
    });

    electron_1.ipcMain.handle('ag:fix-400', async () => {
        return new Promise((resolve) => {
            const _nrptPy = _agPath.join(_agToolsDir, 'nrpt_dns.py');
            const script = _agFs.existsSync(_nrptPy) ? _nrptPy : _agSwitcherPy;
            _agRunPython([script, '--fix-400'], 10000, (err, stdout) => {
                resolve({ ok: !err, message: stdout || 'DNS updated' });
            });
        });
    });

    async function _agRefreshElectronSessions(proxyUrl) {
        const sessions = new Set();
        try {
            if (electron_1.session && electron_1.session.defaultSession) {
                sessions.add(electron_1.session.defaultSession);
            }
            for (const w of _agGetTargetWindows()) {
                try {
                    if (!w || w.isDestroyed?.()) continue;
                    if (w.webContents && w.webContents.session) sessions.add(w.webContents.session);
                    const hwc = typeof utils_1.getHubWebContents === 'function' ? (0, utils_1.getHubWebContents)(w) : null;
                    if (hwc && !hwc.isDestroyed?.() && hwc.session) sessions.add(hwc.session);
                } catch (_) {}
            }
        } catch (_) {}

        const proxyCfg = proxyUrl
            ? { proxyRules: proxyUrl, proxyBypassRules: '<local>' }
            : { mode: 'system' };

        for (const sess of sessions) {
            try {
                if (typeof sess.setProxy === 'function') {
                    await Promise.resolve(sess.setProxy(proxyCfg)).catch(() => {});
                }
                if (typeof sess.forceReloadProxyConfig === 'function') {
                    await Promise.resolve(sess.forceReloadProxyConfig()).catch(() => {});
                }
                if (typeof sess.closeAllConnections === 'function') {
                    await Promise.resolve(sess.closeAllConnections()).catch(() => {});
                }
                if (typeof sess.clearHostResolverCache === 'function') {
                    await Promise.resolve(sess.clearHostResolverCache()).catch(() => {});
                }
            } catch (_) {}
        }
    }

    async function _agRestartLanguageServerIfNeeded(net, lsProvider = null) {
        if (!net || !net.ok) {
            return { restarted: false, reason: 'offline', note: 'Сеть недоступна — перезапуск агента (LS) отложен' };
        }
        const now = Date.now();
        if (_agLastLsRestartAt > 0 && (now - _agLastLsRestartAt) < _AG_LS_RESTART_COOLDOWN_MS) {
            const waitSec = Math.ceil((_AG_LS_RESTART_COOLDOWN_MS - (now - _agLastLsRestartAt)) / 1000);
            return { restarted: false, reason: 'cooldown', note: `Сокеты обновлены (перезапуск LS через ${waitSec}с)` };
        }
        let lsMod = lsProvider;
        if (!lsMod) {
            try { lsMod = require('./languageServer'); } catch (_) { lsMod = null; }
        }
        if (!lsMod || typeof lsMod.getLsProcess !== 'function') {
            return { restarted: false, reason: 'unavailable', note: 'Сокеты сессии и DNS обновлены' };
        }
        const proc = lsMod.getLsProcess();
        if (!proc || proc.killed || proc.exitCode !== null) {
            return { restarted: false, reason: 'not_running', note: 'Сокеты сессии и DNS обновлены' };
        }
        try {
            if (typeof lsMod.setIntentionalTermination === 'function') {
                lsMod.setIntentionalTermination(false);
            }
            if (typeof lsMod.resetRestartCount === 'function') {
                lsMod.resetRestartCount();
            }
            const prevPort = typeof lsMod.getLsPort === 'function' ? Number(lsMod.getLsPort() || 0) : 0;
            const prevPid = Number(proc.pid || 0);
            proc.kill('SIGTERM');
            _agLastLsRestartAt = now;
            const livePort = await _agWaitForLsPort(lsMod, prevPort, prevPid, 4500);
            if (livePort > 0) {
                await new Promise((resolve) => setTimeout(resolve, 250));
                _agNavigateWindowsToUrl(`https://127.0.0.1:${livePort}/`);
            }
            return { restarted: true, reason: 'restarted', note: 'Агент (LS) и сокеты перезапущены' };
        } catch (e) {
            return { restarted: false, reason: 'kill_failed', note: String((e && e.message) || e) };
        }
    }

    let _agConvoWatcher = null;
    let _agConvoDebounce = null;
    try {
        const _convoDir = _agPath.join(_agGeminiDir, 'antigravity', 'conversations');
        if (_agFs.existsSync(_convoDir)) {
            _agConvoWatcher = _agFs.watch(_convoDir, { persistent: false }, (_ev, filename) => {
                if (filename && String(filename).endsWith('.tmp')) return;
                if (_agConvoDebounce) clearTimeout(_agConvoDebounce);
                _agConvoDebounce = setTimeout(async () => {
                    _agConvoDebounce = null;
                    if (!_agHasVisibleWindow()) return;
                    const eng = _getQuotaEngine();
                    if (eng && typeof eng.refreshActive === 'function') {
                        await eng.refreshActive({ appTarget: 'standalone', minAgeSec: 5 }).catch(() => null);
                        _agPushStateUpdated();
                    }
                }, 2500);
            });
            _agConvoWatcher.on('error', () => {});
            electron_1.app?.once?.('before-quit', () => {
                if (_agConvoDebounce) { clearTimeout(_agConvoDebounce); _agConvoDebounce = null; }
                try { _agConvoWatcher?.close(); } catch (_) {}
                _agConvoWatcher = null;
            });
        }
    } catch (_) {}

    let _agAccountsWatcher = null;
    let _agAccountsDebounce = null;
    try {
        const _accWatchDir = _agPath.join(_agToolsDir, 'accounts');
        if (_agFs.existsSync(_accWatchDir)) {
            _agAccountsWatcher = _agFs.watch(_accWatchDir, { persistent: false }, (_ev, filename) => {
                if (filename && String(filename).endsWith('.tmp')) return;
                if (_agAccountsDebounce) clearTimeout(_agAccountsDebounce);
                _agAccountsDebounce = setTimeout(() => {
                    _agAccountsDebounce = null;
                    if (!_agHasVisibleWindow()) return;
                    _agPushStateUpdated();
                }, 350);
            });
            _agAccountsWatcher.on('error', () => {});
            electron_1.app?.once?.('before-quit', () => {
                if (_agAccountsDebounce) { clearTimeout(_agAccountsDebounce); _agAccountsDebounce = null; }
                try { _agAccountsWatcher?.close(); } catch (_) {}
                _agAccountsWatcher = null;
            });
        }
    } catch (_) {}

    let _agSwarmWatcher = null;
    let _agSwarmDebounce = null;
    try {
        const _swarmWatchDir = _agPath.join(_agToolsDir, 'swarm');
        if (_agFs.existsSync(_swarmWatchDir)) {
            const onSwarmFsChange = (_ev, filename) => {
                if (filename && String(filename).endsWith('.tmp')) return;
                if (_agSwarmDebounce) clearTimeout(_agSwarmDebounce);
                _agSwarmDebounce = setTimeout(() => {
                    _agSwarmDebounce = null;
                    if (!_agHasVisibleWindow()) return;
                    _agPushStateUpdated();
                }, 250);
            };
            try {
                _agSwarmWatcher = _agFs.watch(_swarmWatchDir, { recursive: true, persistent: false }, onSwarmFsChange);
            } catch (_) {
                _agSwarmWatcher = _agFs.watch(_swarmWatchDir, { persistent: false }, onSwarmFsChange);
            }
            _agSwarmWatcher.on('error', () => {});
            electron_1.app?.once?.('before-quit', () => {
                if (_agSwarmDebounce) { clearTimeout(_agSwarmDebounce); _agSwarmDebounce = null; }
                try { _agSwarmWatcher?.close(); } catch (_) {}
                _agSwarmWatcher = null;
            });
        }
    } catch (_) {}

    electron_1.ipcMain.handle('ag:refresh', async (_event, mode) => {
        const eng = _getQuotaEngine();

        const isObj = mode && typeof mode === 'object';
        const effectiveMode = isObj ? (mode.mode || 'active') : mode;
        const explicitForce = isObj ? Boolean(mode.force) : false;
        const explicitMinAge = (isObj && mode.minAgeSec !== undefined) ? Number(mode.minAgeSec) : undefined;

        if (effectiveMode === 'sync_accounts') {
            if (!_agFs.existsSync(_agSwitcherPy)) {
                return { ok: false, error: 'account_switcher.py not found' };
            }
            return new Promise((resolve) => {
                _agRunPython([_agSwitcherPy, 'sync_live', '--app', 'standalone'], 15000, async (err, stdout, stderr) => {
                    let parsed = {};
                    if (stdout) {
                        try { parsed = JSON.parse(String(stdout).trim()); } catch (_) {}
                    }
                    if (eng && typeof eng.refreshAll === 'function') {
                        await eng.refreshAll({ appTarget: 'standalone', concurrency: 8, minAgeSec: 15, syncWinCred: false }).catch(() => null);
                    }
                    _agPushStateUpdated();
                    resolve({
                        ok: !err && parsed.ok !== false,
                        ...(parsed && typeof parsed === 'object' ? parsed : {}),
                        ...(err && !parsed.error ? { error: String((stderr && String(stderr).trim()) || err.message || err) } : {})
                    });
                });
            });
        }

        if (effectiveMode === 'add_account') {
            if (!_agFs.existsSync(_agSwitcherPy)) {
                return { ok: false, error: 'account_switcher.py not found' };
            }
            return new Promise((resolve) => {
                _agRunPython([_agSwitcherPy, 'add_account', '--app', 'standalone'], 190000, (err, stdout, stderr) => {
                    let parsed = { ok: false, error: 'Не удалось добавить аккаунт' };
                    if (stdout) {
                        try {
                            const obj = JSON.parse(String(stdout).trim());
                            if (obj && typeof obj === 'object') parsed = obj;
                        } catch (_) {}
                    }
                    if (err && parsed.ok !== true) {
                        parsed.ok = false;
                        if (!parsed.error || parsed.error === 'Не удалось добавить аккаунт') {
                            parsed.error = String((stderr && String(stderr).trim()) || err.message || err);
                        }
                    }
                    if (parsed.ok) {
                        _agPushStateUpdated();
                    }
                    resolve(parsed);
                });
            });
        }

        if (effectiveMode === 'network') {
            if (eng && typeof eng.flushDnsCache === 'function') {
                await eng.flushDnsCache().catch(() => {});
            }
            const net = await _detectNetwork(true);
            await _agRefreshElectronSessions(net ? net.proxyUrl : null);
            const lsRes = await _agRestartLanguageServerIfNeeded(net);
            const activeAccount = (net && net.ok && eng && typeof eng.refreshActive === 'function')
                ? await eng.refreshActive({ appTarget: 'standalone', force: true, minAgeSec: 0 }).catch(() => null)
                : null;
            _agPushStateUpdated();
            return {
                ok: true,
                network: net,
                activeAccount,
                backendRestarted: lsRes.restarted,
                backendNote: lsRes.note || ''
            };
        }

        if (_agQuotaRefreshInFlight) {
            if (explicitForce && effectiveMode === 'active') {
                await _agQuotaRefreshInFlight.catch(() => null);
            } else {
                return _agQuotaRefreshInFlight;
            }
        }

        if (eng && typeof eng.refreshActive === 'function' && typeof eng.refreshAll === 'function') {
            _agQuotaRefreshInFlight = (async () => {
                try {
                    if (effectiveMode === 'active') {
                        const opts = { appTarget: 'standalone' };
                        if (explicitForce) {
                            opts.force = true;
                            opts.minAgeSec = 0;
                        } else {
                            opts.minAgeSec = explicitMinAge !== undefined ? explicitMinAge : 10;
                        }
                        const activeAccount = await eng.refreshActive(opts);
                        _agPushStateUpdated();
                        return { ok: true, activeAccount };
                    }
                    const allOpts = { appTarget: 'standalone', concurrency: 8 };
                    if (explicitForce) {
                        allOpts.force = true;
                        allOpts.minAgeSec = 0;
                    } else if (explicitMinAge !== undefined) {
                        allOpts.minAgeSec = explicitMinAge;
                    }
                    await eng.refreshAll(allOpts);
                    _agPushStateUpdated();
                    return { ok: true };
                } catch (e) {
                    return { ok: false, error: String(e) };
                } finally {
                    _agQuotaRefreshInFlight = null;
                }
            })();
            return _agQuotaRefreshInFlight;
        }

        if (!_agFs.existsSync(_agSwitcherPy)) {
            return { ok: false };
        }
        const args = effectiveMode === 'active'
            ? [_agSwitcherPy, 'refresh_active', '--app', 'standalone']
            : [_agSwitcherPy, 'refresh_quotas', '--app', 'standalone'];

        _agQuotaRefreshInFlight = new Promise((resolve) => {
            _agRunPython(args, 15000, (err) => {
                _agQuotaRefreshInFlight = null;
                resolve({ ok: !err });
            });
        });
        return _agQuotaRefreshInFlight;
    });

    electron_1.ipcMain.handle('ag:open-log', async () => {
        try {
            const userDataPath = electron_1.app.getPath('userData');
            const lsLog = _agPath.join(userDataPath, 'logs', 'language_server.log');
            const mainLog = _agPath.join(userDataPath, 'logs', 'main.log');
            let targetLog = lsLog;
            if (!_agFs.existsSync(lsLog) || _agFs.statSync(lsLog).size === 0) {
                if (_agFs.existsSync(mainLog)) targetLog = mainLog;
            }
            if (_agFs.existsSync(targetLog)) {
                electron_1.shell.openPath(targetLog);
                return { ok: true, path: targetLog };
            }
            const switchLog = _agPath.join(_agToolsDir, 'switch.log');
            if (_agFs.existsSync(switchLog)) {
                electron_1.shell.openPath(switchLog);
                return { ok: true, path: switchLog };
            }
            electron_1.shell.openPath(_agToolsDir);
            return { ok: true };
        } catch (e) {
            return { ok: false, error: String(e) };
        }
    });

    electron_1.ipcMain.handle('ag:swarm', async (_event, op, payload = {}) => {
        try {
            const data = payload && typeof payload === 'object' ? payload : {};

            if (op === 'get_settings') {
                const eng = _getQuotaEngine();
                const settings = eng && typeof eng.loadAssistantSettings === 'function'
                    ? eng.loadAssistantSettings()
                    : {};
                const modelConfig = eng && typeof eng.loadModelConfig === 'function'
                    ? eng.loadModelConfig()
                    : {};
                let quotaPlan = null;
                try {
                    if (eng && typeof eng.resolveQuotaExhaustionPlan === 'function') {
                        const st = typeof eng.loadState === 'function' ? eng.loadState({ appTarget: 'ide' }) : null;
                        const activeId = (st && st.active && st.active.id) || '';
                        const prefOrch = (modelConfig && Array.isArray(modelConfig.orchestrator_order) && modelConfig.orchestrator_order[0]) || 'claude';
                        quotaPlan = eng.resolveQuotaExhaustionPlan({ currentModel: prefOrch, currentAccountId: activeId });
                    }
                } catch (_) {}
                return {
                    ok: true,
                    failoverPolicy: settings.failoverPolicy || 'interactive',
                    modelConfig,
                    quotaPlan
                };
            }

            if (op === 'set_failover_policy') {
                const eng = _getQuotaEngine();
                const allowed = new Set(['interactive', 'account_hop', 'model_fallback']);
                const policy = allowed.has(data.policy) ? data.policy : 'interactive';
                const saved = eng && typeof eng.saveAssistantSettings === 'function'
                    ? await eng.saveAssistantSettings({ failoverPolicy: policy })
                    : { failoverPolicy: policy };
                return { ok: true, failoverPolicy: saved.failoverPolicy || policy };
            }

            if (op === 'save_model_config') {
                const eng = _getQuotaEngine();
                if (!eng || typeof eng.saveModelConfig !== 'function') {
                    return { ok: false, error: 'quota_engine не поддерживает сохранение model_config.json' };
                }
                const nextCfg = data.config && typeof data.config === 'object' ? data.config : {};
                await eng.saveModelConfig(nextCfg);
                return { ok: true, modelConfig: eng.loadModelConfig() };
            }

            if (op === 'reset_model_config') {
                const eng = _getQuotaEngine();
                if (!eng || typeof eng.getModelConfigFile !== 'function') {
                    const cfgPath = _agPath.join(_agToolsDir, 'model_config.json');
                    try { if (_agFs.existsSync(cfgPath)) _agFs.unlinkSync(cfgPath); } catch (_) {}
                    return { ok: true, modelConfig: {} };
                }
                await eng.saveModelConfig({});
                return { ok: true, modelConfig: {} };
            }

            const bridge = _getSwarmBridge();
            if (!bridge) {
                return { ok: false, error: 'Swarm Bridge не найден (~/.antigravity_tools/swarm_bridge.js)' };
            }

            if (op === 'copy_prompt') {
                if (electron_1.clipboard && typeof electron_1.clipboard.writeText === 'function' && data.text) {
                    electron_1.clipboard.writeText(String(data.text));
                }
                return { ok: true };
            }

            if (op === 'pick_project') {
                if (!electron_1.dialog || typeof electron_1.dialog.showOpenDialog !== 'function') {
                    return { ok: false, error: 'Electron dialog недоступен' };
                }
                let win;
                try {
                    win = typeof getTargetHubWindow === 'function' ? getTargetHubWindow() : undefined;
                } catch (_) {}
                const dlgOpts = {
                    properties: ['openDirectory'],
                    title: 'Выберите папку git-проекта для Команды агентов'
                };
                if (data.defaultPath && _agFs.existsSync(data.defaultPath)) {
                    dlgOpts.defaultPath = data.defaultPath;
                }
                const picked = await electron_1.dialog.showOpenDialog(win || undefined, dlgOpts);
                if (!picked || picked.canceled || !Array.isArray(picked.filePaths) || !picked.filePaths[0]) {
                    return { ok: true, canceled: true };
                }
                const projectPath = picked.filePaths[0];
                bridge.rememberProject(projectPath);
                return {
                    ok: true,
                    canceled: false,
                    projectPath,
                    isGit: bridge.isGitRepoDir(projectPath),
                    projects: bridge.listKnownProjects()
                };
            }

            const res = await bridge.handleSwarmAction(op, data, { extraDirs: [_agToolsDir] });
            if ((op === 'prepare' || op === 'resume') && res && res.ok && res.dispatchPrompt) {
                try {
                    if (electron_1.clipboard && typeof electron_1.clipboard.writeText === 'function') {
                        electron_1.clipboard.writeText(res.dispatchPrompt);
                    }
                } catch (_) {}
            }
            return res;
        } catch (e) {
            return { ok: false, error: String((e && e.message) || e) };
        }
    });

    let _agGuardianMonitor = null;
    let _agLastGuardianAlert = null;
    let _agGuardianHealInFlight = null;

    function _agEnsureGuardianMonitor() {
        if (_agGuardianMonitor) return _agGuardianMonitor;
        const guard = _getErrorGuardian();
        if (!guard || typeof guard.createLogErrorMonitor !== 'function') return null;
        let userDataLogs = '';
        try {
            userDataLogs = _agPath.join(electron_1.app.getPath('userData'), 'logs');
        } catch (_) {
            const appData = process.env.APPDATA || _agPath.join(_agOs.homedir(), 'AppData', 'Roaming');
            userDataLogs = _agPath.join(appData, 'Antigravity', 'logs');
        }
        _agGuardianMonitor = guard.createLogErrorMonitor({
            logDirs: [userDataLogs],
            cooldownMs: 120000,
            onError: (alert) => {
                _agLastGuardianAlert = alert;
                _agPushChannel('ag:guardian-alert', {
                    alert,
                    config: guard.loadAutoHealConfig(),
                });
            },
        });
        return _agGuardianMonitor;
    }

    const _agGuardianTimer = setInterval(() => {
        if (!_agHasVisibleWindow()) return;
        try {
            const mon = _agEnsureGuardianMonitor();
            if (mon) mon.scanNow();
        } catch (_) {}
    }, 20000);
    if (typeof _agGuardianTimer.unref === 'function') _agGuardianTimer.unref();

    electron_1.ipcMain.handle('ag:guardian', async (_event, op, payload = {}) => {
        try {
            const guard = _getErrorGuardian();
            const data = payload && typeof payload === 'object' ? payload : {};

            if (op === 'get_config') {
                const cfg = guard ? guard.loadAutoHealConfig() : { enabled: true, auto_relaunch_broken_app: true };
                return { ok: true, config: cfg, alert: _agLastGuardianAlert };
            }

            if (op === 'set_config') {
                if (!guard) return { ok: false, error: 'error_guardian.js не найден' };
                const saved = guard.saveAutoHealConfig({ enabled: Boolean(data.enabled) });
                return { ok: true, config: saved };
            }

            if (op === 'dismiss') {
                _agLastGuardianAlert = null;
                return { ok: true };
            }

            if (op === 'scan') {
                const mon = _agEnsureGuardianMonitor();
                if (mon) mon.scanNow();
                const cfg = guard ? guard.loadAutoHealConfig() : { enabled: true };
                return { ok: true, config: cfg, alert: _agLastGuardianAlert };
            }

            if (op === 'heal') {
                if (_agGuardianHealInFlight) return _agGuardianHealInFlight;
                _agGuardianHealInFlight = (async () => {
                    try {
                        const inPlaceOnly = data.inPlaceOnly !== false;
                        let healRes = { ok: true, actions: [] };
                        if (guard && typeof guard.runScopedHeal === 'function') {
                            healRes = await guard.runScopedHeal({
                                target: 'manager',
                                force: true,
                                allowRestart: !inPlaceOnly,
                                timeoutMs: 45000,
                            });
                        }
                        const eng = _getQuotaEngine();
                        if (eng && typeof eng.flushDnsCache === 'function') {
                            await eng.flushDnsCache().catch(() => {});
                        }
                        const net = await _detectNetwork(true).catch(() => null);
                        await _agRefreshElectronSessions(net ? net.proxyUrl : null).catch(() => {});
                        const lsRes = await _agRestartLanguageServerIfNeeded(net).catch(() => ({ restarted: false }));
                        _agLastGuardianAlert = null;
                        _agPushStateUpdated();
                        return {
                            ok: Boolean(healRes && healRes.ok !== false),
                            guardian: healRes,
                            network: net,
                            backendRestarted: Boolean(lsRes && lsRes.restarted),
                            backendNote: (lsRes && lsRes.note) || '',
                        };
                    } finally {
                        _agGuardianHealInFlight = null;
                    }
                })();
                return _agGuardianHealInFlight;
            }

            return { ok: false, error: `Unknown guardian op: ${op}` };
        } catch (e) {
            return { ok: false, error: String((e && e.message) || e) };
        }
    });
    // === End AG Toolkit Desktop IPC Handlers ===

    (0, wcvIpcHandlers_1.registerWcvIpcHandlers)();
}
