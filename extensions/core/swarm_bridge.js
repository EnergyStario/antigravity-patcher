/**
 * swarm_bridge.js -- Unified pure-Node.js Swarm Engine Bridge
 * Shared by Antigravity IDE (VS Code Extension Host) and Antigravity 2.0 (Electron Main).
 * Single source of truth for Python `swarm.cli` orchestration, `state.json` checkpoints,
 * dispatch prompt formatting, and git project discovery.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFile } = require('child_process');

let _cachedPythonCmd = null;

/**
 * Resolve root directory for ~/.antigravity_tools (respects dynamic AG_TOOLS_DIR overrides).
 * @returns {string}
 */
function getSwarmRootDir() {
    return process.env.AG_TOOLS_DIR || path.join(os.homedir(), '.antigravity_tools');
}

/**
 * Resolve ~/.gemini/antigravity directory (respects dynamic AG_GEMINI_HOME overrides for tests).
 * @returns {string}
 */
function getGeminiAntigravityDir() {
    return process.env.AG_GEMINI_HOME || path.join(os.homedir(), '.gemini', 'antigravity');
}

/**
 * Resolve checkpoint storage directory (~/.antigravity_tools/swarm).
 * @returns {string}
 */
function getSwarmStateDir() {
    return path.join(getSwarmRootDir(), 'swarm');
}

function getRecentProjectsFile() {
    return path.join(getSwarmRootDir(), 'swarm_projects.json');
}

function getCleanPythonEnv(enginePath) {
    const cleanEnv = Object.assign({}, process.env, {
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1'
    });
    for (const key of Object.keys(cleanEnv)) {
        const up = key.toUpperCase();
        if (
            up.startsWith('ELECTRON_') ||
            up.startsWith('VSCODE_') ||
            up.startsWith('ATOM_SHELL_') ||
            up === 'NODE_OPTIONS' ||
            up === 'NODE_ENV'
        ) {
            delete cleanEnv[key];
        }
    }
    cleanEnv.AG_TOOLS_DIR = getSwarmRootDir();
    if (enginePath) {
        cleanEnv.PYTHONPATH = cleanEnv.PYTHONPATH
            ? `${enginePath}${path.delimiter}${cleanEnv.PYTHONPATH}`
            : enginePath;
    }
    return cleanEnv;
}

function _pythonCandidates() {
    if (_cachedPythonCmd) {
        return [_cachedPythonCmd];
    }
    if (process.platform === 'win32') {
        return [['python', []], ['py', ['-3']], ['python3', []]];
    }
    return [['python3', []], ['python', []]];
}

function _execFilePromise(bin, args, opts) {
    return new Promise((resolve) => {
        execFile(bin, args, opts, (err, stdout, stderr) => {
            resolve({
                err,
                stdout: stdout || '',
                stderr: stderr || '',
                code: err ? (typeof err.code === 'number' ? err.code : 1) : 0
            });
        });
    });
}

function _resetPythonCacheForTests() {
    _cachedPythonCmd = null;
}

/**
 * Locate directory containing `swarm/ipc_server.py` (for PYTHONPATH).
 * @param {string|string[]} [workspacePaths]
 * @param {string[]} [extraDirs]
 * @returns {string|null}
 */
function resolveSwarmEnginePath(workspacePaths = [], extraDirs = []) {
    const candidates = [];
    const wsList = Array.isArray(workspacePaths)
        ? workspacePaths
        : (workspacePaths ? [workspacePaths] : []);
    for (const wp of wsList) {
        if (wp) candidates.push(path.join(wp, 'source', 'patcher'));
    }

    const extraList = Array.isArray(extraDirs) ? extraDirs : (extraDirs ? [extraDirs] : []);
    for (const dir of extraList) {
        if (!dir) continue;
        candidates.push(path.join(dir, 'swarm_engine'));
        candidates.push(path.join(dir, 'source', 'patcher'));
        candidates.push(dir);
    }

    candidates.push(path.join(getSwarmRootDir(), 'swarm_engine'));
    candidates.push(path.resolve(__dirname, 'swarm_engine'));
    candidates.push(path.resolve(__dirname, '..', '..', 'source', 'patcher'));

    for (const cand of candidates) {
        try {
            if (cand && fs.existsSync(path.join(cand, 'swarm', 'ipc_server.py'))) {
                return cand;
            }
        } catch (_) {}
    }
    return null;
}

function countWorkerResults(workerResults) {
    if (Array.isArray(workerResults)) return workerResults.length;
    if (workerResults && typeof workerResults === 'object') return Object.keys(workerResults).length;
    return 0;
}

const SWARM_STAGE_LABELS = {
    decompose: 'Планирование',
    plan: 'Планирование',
    validate: 'Проверка плана',
    execute: 'Агенты работают',
    implement: 'Агенты работают',
    verify: 'Проверка тестами',
    test: 'Проверка тестами',
    merge: 'Слияние в проект',
    report: 'Готово',
    done: 'Готово',
    error: 'Ошибка',
    failed: 'Ошибка'
};

function getStageLabel(nodeName, isComplete, errors) {
    if (isComplete) return 'Готово';
    if (Array.isArray(errors) && errors.length > 0 && String(nodeName || '').toUpperCase() === 'DONE') {
        return 'Ошибка';
    }
    const key = String(nodeName || '').trim().toLowerCase();
    return SWARM_STAGE_LABELS[key] || (nodeName ? String(nodeName) : 'В очереди');
}

function _parseTimestampMs(raw) {
    if (!raw) return 0;
    if (typeof raw === 'number') return Number.isFinite(raw) && raw > 0 ? raw : 0;
    let s = String(raw).trim();
    if (!s || s.startsWith('0001-')) return 0;
    // Normalize SQLite datetime like "2026-11-11 13:46:50.0217797+00:00"
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(s)) {
        s = s.replace(' ', 'T');
    }
    s = s.replace(/\.(\d{3})\d+/, '.$1');
    const ms = Date.parse(s);
    if (!Number.isFinite(ms) || ms < 946684800000) return 0; // < 2000-01-01
    return ms;
}

function normalizeCheckpointSummary(raw, fallbackTaskId = '', mtimeMs = 0) {
    if (!raw || typeof raw !== 'object') return null;
    const briefs = Array.isArray(raw.briefs) ? raw.briefs : [];
    const totalPhases = Number.isFinite(raw.total_phases)
        ? raw.total_phases
        : (Array.isArray(raw.phases) ? raw.phases.length : 0);
    const resultsMap = (raw.worker_results && typeof raw.worker_results === 'object' && !Array.isArray(raw.worker_results))
        ? raw.worker_results : {};

    const taskCreatedMs = _parseTimestampMs(raw.created_at) || mtimeMs || 0;
    const taskUpdatedMs = _parseTimestampMs(raw._checkpoint_at) || mtimeMs || taskCreatedMs || 0;

    let runningWorkers = 0;
    let failedWorkers = 0;
    let pendingWorkers = 0;
    let doneByBrief = 0;
    const workerModelSet = new Set();
    const workers = briefs.map((b, idx) => {
        const name = (b && b.worker_name) || `worker-${idx + 1}`;
        const model = (b && b.model) || 'flash';
        if (model) workerModelSet.add(model);
        const wRes = resultsMap[name] || null;
        let status = (b && b.status) || 'pending';
        if (status === 'pending' && wRes) {
            status = wRes.success === false ? 'failed' : 'done';
        }
        if (status === 'done') doneByBrief++;
        else if (status === 'running') runningWorkers++;
        else if (status === 'failed') failedWorkers++;
        else pendingWorkers++;

        const wStartedMs = _parseTimestampMs(b && b.started_at) || (status === 'running' ? taskCreatedMs : 0);
        const wFinishedMs = _parseTimestampMs(b && b.finished_at);
        const wDur = Number((wRes && wRes.duration_seconds) || 0)
            || (wStartedMs > 0 && wFinishedMs > wStartedMs ? Math.round((wFinishedMs - wStartedMs) / 1000) : 0);

        return {
            name,
            status,
            type: (b && b.worker_type) || 'code',
            role: (b && b.worker_type) || 'code',
            model,
            branch: (b && b.branch_name) || '',
            worktree_path: (b && b.worktree_path) || '',
            task_description: (b && b.task_description) || '',
            commit_sha: (b && b.commit_sha) || (wRes && wRes.commit_sha) || '',
            attempt_count: Number((b && b.attempt_count) || 0),
            started_at: (b && b.started_at) || '',
            finished_at: (b && b.finished_at) || '',
            started_at_ms: wStartedMs,
            finished_at_ms: wFinishedMs,
            duration_seconds: wDur,
            files: Array.isArray(b && b.owned_files) ? b.owned_files.slice(0, 8) : []
        };
    });

    const projectPath = raw.project_path || '';
    const errors = Array.isArray(raw.errors) ? raw.errors : [];
    const currentNode = raw.current_node || 'UNKNOWN';
    const isComplete = Boolean(raw.is_complete);
    const doneWorkers = Math.max(doneByBrief, countWorkerResults(raw.worker_results));

    return {
        task_id: raw.task_id || fallbackTaskId || '',
        project_path: projectPath,
        project_name: projectPath ? path.basename(projectPath) : '',
        user_request: raw.user_request || '',
        current_node: currentNode,
        stage_label: getStageLabel(currentNode, isComplete, errors),
        current_phase: Number.isFinite(raw.current_phase) ? raw.current_phase : 0,
        total_phases: totalPhases,
        is_complete: isComplete,
        done_workers: doneWorkers,
        running_workers: runningWorkers,
        failed_workers: failedWorkers,
        pending_workers: pendingWorkers,
        total_workers: briefs.length,
        orchestrator_model: (raw.orchestrator_model || '').trim() || 'Модель чата (умная)',
        worker_models: Array.from(workerModelSet),
        workers,
        errors,
        final_branch: raw.final_branch || '',
        created_at: raw.created_at || '',
        created_at_ms: taskCreatedMs,
        updated_at_ms: taskUpdatedMs,
        mtimeMs
    };
}

/**
 * Read a single Swarm checkpoint summary from state.json.
 * @param {string} taskId
 * @returns {object|null}
 */
function loadCheckpointSummary(taskId) {
    if (!taskId || /[\\/]/.test(String(taskId))) return null;
    const safeId = path.basename(String(taskId));
    const statePath = path.join(getSwarmStateDir(), safeId, 'state.json');
    try {
        if (!fs.existsSync(statePath)) return null;
        const stat = fs.statSync(statePath);
        const raw = JSON.parse(fs.readFileSync(statePath, 'utf8'));
        return normalizeCheckpointSummary(raw, safeId, stat.mtimeMs);
    } catch (_) {
        return null;
    }
}

/**
 * List persisted Swarm checkpoints from ~/.antigravity_tools/swarm/*\/state.json sorted newest-first.
 * @param {number} [limit]
 * @returns {Array<object>}
 */
function listSwarmCheckpoints(limit = 25) {
    const swarmDir = getSwarmStateDir();
    if (!fs.existsSync(swarmDir)) return [];

    const results = [];
    let entries = [];
    try {
        entries = fs.readdirSync(swarmDir, { withFileTypes: true });
    } catch (_) {
        return [];
    }

    for (const ent of entries) {
        if (!ent.isDirectory()) continue;
        const summary = loadCheckpointSummary(ent.name);
        if (summary) results.push(summary);
    }

    results.sort((a, b) => b.mtimeMs - a.mtimeMs);
    return results.slice(0, limit);
}

/**
 * Delete a checkpoint directory inside ~/.antigravity_tools/swarm (rejects path traversal).
 * @param {string} taskId
 * @returns {boolean}
 */
function deleteSwarmCheckpoint(taskId) {
    if (!taskId || /[\\/]/.test(String(taskId))) return false;
    const safeId = path.basename(String(taskId));
    const targetDir = path.join(getSwarmStateDir(), safeId);
    try {
        if (!fs.existsSync(targetDir)) return false;
        fs.rmSync(targetDir, { recursive: true, force: true });
        return !fs.existsSync(targetDir);
    } catch (_) {
        return false;
    }
}

/**
 * Execute `python -m swarm.cli <args>` and parse structured JSON stdout.
 * @param {string[]} argsArray
 * @param {{ enginePath?: string, workspacePaths?: string|string[], extraDirs?: string[], timeoutMs?: number, cwd?: string }} [options]
 */
async function runSwarmCliAsync(argsArray, options = {}) {
    const enginePath = options.enginePath || resolveSwarmEnginePath(options.workspacePaths, options.extraDirs);
    if (!enginePath) {
        return { ok: false, data: null, error: 'Swarm Engine directory not found' };
    }
    const env = getCleanPythonEnv(enginePath);
    const timeoutMs = options.timeoutMs || 30000;

    for (const [bin, prefixArgs] of _pythonCandidates()) {
        const fullArgs = [...prefixArgs, '-m', 'swarm.cli', ...argsArray];
        const execOpts = {
            encoding: 'utf8',
            timeout: timeoutMs,
            windowsHide: true,
            env
        };
        if (options.cwd && fs.existsSync(options.cwd)) {
            execOpts.cwd = options.cwd;
        }
        const res = await _execFilePromise(bin, fullArgs, execOpts);
        if (res.err && res.err.code === 'ENOENT') {
            _cachedPythonCmd = null;
            continue;
        }
        _cachedPythonCmd = [bin, prefixArgs];
        let parsed = null;
        try {
            if (res.stdout.trim()) parsed = JSON.parse(res.stdout.trim());
        } catch (_) {}
        return {
            ok: res.code === 0,
            data: parsed,
            stdout: res.stdout,
            stderr: res.stderr,
            error: res.code === 0 ? null : ((parsed && parsed.error) || res.stderr.trim() || `Exit code ${res.code}`)
        };
    }
    return { ok: false, data: null, error: 'Python 3 executable not found in PATH' };
}

function _loadQuotaEngineSafely(extraDirs = []) {
    const candidates = [
        path.join(getSwarmRootDir(), 'quota_engine.js'),
        path.join(__dirname, 'quota_engine.js'),
        ...extraDirs.map((d) => path.join(d, 'quota_engine.js'))
    ];
    for (const cand of candidates) {
        try {
            if (cand && fs.existsSync(cand)) {
                return require(cand);
            }
        } catch (_) {}
    }
    return null;
}

/**
 * Load model_config.json safely via sibling quota_engine if present.
 * @returns {object|null}
 */
function loadModelConfigSafely(extraDirs = []) {
    const eng = _loadQuotaEngineSafely(extraDirs);
    try {
        if (eng && typeof eng.loadModelConfig === 'function') {
            return eng.loadModelConfig();
        }
    } catch (_) {}
    return null;
}

/**
 * Inspect active quota survival plan and assistant failover policy safely via quota_engine.
 */
function loadQuotaPlanSafely(extraDirs = [], currentModel = 'claude', { appTarget = 'ide' } = {}) {
    const eng = _loadQuotaEngineSafely(extraDirs);
    if (!eng) return { settings: {}, plan: null };
    const target = appTarget === 'standalone' ? 'standalone' : 'ide';
    let settings = {};
    let plan = null;
    try {
        if (typeof eng.loadAssistantSettings === 'function') {
            settings = eng.loadAssistantSettings() || {};
        }
    } catch (_) {}
    try {
        let currentAccId = '';
        if (typeof eng.loadState === 'function') {
            const st = eng.loadState({ appTarget: target });
            currentAccId = (st && ((st.activeAccount && st.activeAccount.id) || (st.active && st.active.id))) || '';
        }
        if (typeof eng.resolveQuotaExhaustionPlan === 'function') {
            plan = eng.resolveQuotaExhaustionPlan({
                currentModel: currentModel || 'claude',
                currentAccountId: currentAccId,
                appTarget: target,
            });
        }
    } catch (_) {}
    return { settings, plan };
}

/**
 * Build structured instructions for Antigravity Chat / native `/swarm` backed by Python Swarm Engine CLI.
 */
function buildSwarmDispatchPrompt({
    taskId,
    projectPath,
    prompt,
    enginePath,
    modelConfig,
    mode = 'run',
    stateSummary = null,
    useAffectedTests = true,
    quotaPlan = null,
    assistantSettings = null
}) {
    const isResume = mode === 'resume';
    const tierNote = modelConfig && modelConfig.autoSwitchPolicy
        ? `Model policy: ${modelConfig.autoSwitchPolicy}`
        : null;
    const affectedFlag = useAffectedTests === false ? '' : ' --affected';
    const policy = (assistantSettings && assistantSettings.failoverPolicy) || 'interactive';
    const isDegraded = (quotaPlan && quotaPlan.strategy === 'degraded_swarm') || policy === 'model_fallback';
    const projName = projectPath ? path.basename(projectPath) : 'проект';
    const cleanPrompt = String(prompt || '').trim() || '(см. state.json)';

    const lines = [
        `🎯 **Задача Swarm в проекте [${projName}]** (\`${taskId}\`):`,
        cleanPrompt,
        '',
        `> **Контекст ${isResume ? 'возобновления' : 'выполнения'} (skill \`ag-swarm\`):**`,
        `- **Папка проекта:** \`${projectPath}\``,
        `- **Task ID:** \`${taskId}\``,
        `- **Команда движка:** \`$env:PYTHONPATH='${enginePath}'; python -m swarm.cli\``,
    ];
    if (tierNote) lines.push(`- **Модель:** ${tierNote}`);
    if (stateSummary) {
        lines.push(`- **Чекпоинт:** этап=\`${stateSummary.current_node}\`, фаза=${stateSummary.current_phase || 1}/${stateSummary.total_phases || '?'}, воркеры=${stateSummary.done_workers}/${stateSummary.total_workers}`);
    }
    if (isDegraded) {
        const degradedModel = (quotaPlan && quotaPlan.model) || (modelConfig && modelConfig.worker_model) || 'gemini-2.5-flash';
        lines.push(
            `- 🛡 **ДЕЖУРНЫЙ РЕЗЕРВНЫЙ РЕЖИМ РОЯ (${degradedModel}):** работай мелкими шагами (1–2 файла на воркера) с обязательным \`verify${affectedFlag}\`.`
        );
    }

    lines.push(
        '',
        isResume
            ? `Продолжи выполнение задачи (для возобновления): \`python -m swarm.cli status --task-id ${taskId}\` и при необходимости \`resume\`.`
            : `Выполни задачу по регламенту \`ag-swarm\`: декомпозируй на JSON worker briefs -> \`setup --briefs -\` -> запусти изолированных воркеров в worktrees -> \`verify${affectedFlag}\` -> \`merge\`.`
    );
    return lines.join('\n');
}

function isGitRepoDir(dirPath) {
    if (!dirPath || typeof dirPath !== 'string') return false;
    try {
        return fs.existsSync(dirPath) && fs.existsSync(path.join(dirPath, '.git'));
    } catch (_) {
        return false;
    }
}

function _decodeFileUriToPath(uriStr) {
    if (!uriStr || typeof uriStr !== 'string') return null;
    if (!uriStr.startsWith('file://')) return uriStr;
    try {
        const u = new URL(uriStr);
        let p = decodeURIComponent(u.pathname || '');
        if (/^\/[a-zA-Z]:/.test(p)) {
            p = p.slice(1);
            if (process.platform === 'win32') {
                p = p.replace(/\//g, '\\');
            }
        }
        return p;
    } catch (_) {
        return null;
    }
}

function _isSwarmWorktreePath(p) {
    if (!p) return true;
    return p.toLowerCase().includes('-swarm-');
}

function _isTempDirPath(p) {
    if (!p) return true;
    const norm = p.toLowerCase();
    return norm.includes('\\appdata\\local\\temp\\') || norm.startsWith('/tmp/');
}

function rememberProject(projectPath) {
    if (!projectPath || typeof projectPath !== 'string') return false;
    const trimmed = path.resolve(projectPath.trim());
    if (!trimmed || _isSwarmWorktreePath(trimmed) || !fs.existsSync(trimmed)) return false;
    const filePath = getRecentProjectsFile();
    try {
        let existing = [];
        if (fs.existsSync(filePath)) {
            const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
            if (Array.isArray(parsed)) existing = parsed;
        }
        const keyLower = trimmed.toLowerCase();
        const deduped = [trimmed, ...existing.filter((p) => typeof p === 'string' && path.resolve(p.trim()).toLowerCase() !== keyLower)];
        fs.mkdirSync(path.dirname(filePath), { recursive: true });
        fs.writeFileSync(filePath, JSON.stringify(deduped.slice(0, 15), null, 2), 'utf8');
        return true;
    } catch (_) {
        return false;
    }
}

/**
 * Discover existing git repositories recently used in Swarm, MRU list, or Antigravity IDE storage.
 * @param {string[]} [extraPaths]
 * @returns {Array<{ path: string, name: string, isGit: boolean }>}
 */
function listKnownProjects(extraPaths = []) {
    const seen = new Set();
    const collected = [];
    const geminiDir = getGeminiAntigravityDir();
    const projectsDir = path.join(os.homedir(), '.gemini', 'config', 'projects');

    const projectIdToName = new Map();
    const pathToCustomName = new Map();
    const projectConfigs = [];

    // Scan ~/.gemini/config/projects/*.json (sorted by mtime desc)
    try {
        if (fs.existsSync(projectsDir)) {
            const files = fs.readdirSync(projectsDir);
            for (const f of files) {
                if (!f.endsWith('.json')) continue;
                const fullPath = path.join(projectsDir, f);
                try {
                    const st = fs.statSync(fullPath);
                    projectConfigs.push({ file: f, path: fullPath, mtimeMs: st.mtimeMs });
                } catch (_) {}
            }
            projectConfigs.sort((a, b) => b.mtimeMs - a.mtimeMs);

            for (const item of projectConfigs) {
                try {
                    const data = JSON.parse(fs.readFileSync(item.path, 'utf8'));
                    if (!data) continue;
                    const cName = String(data.name || '').trim();
                    if (data.id && cName) {
                        projectIdToName.set(String(data.id).trim(), cName);
                    }
                    const resList = (data.projectResources && Array.isArray(data.projectResources.resources))
                        ? data.projectResources.resources
                        : (Array.isArray(data.resources) ? data.resources : []);
                    for (const res of resList) {
                        const uri = res && (res.folderUri || (res.gitFolder && res.gitFolder.folderUri));
                        if (uri) {
                            const decoded = _decodeFileUriToPath(uri);
                            if (decoded) {
                                const resolved = path.resolve(decoded.trim());
                                const normKey = process.platform === 'win32' ? resolved.toLowerCase() : resolved;
                                if (cName) pathToCustomName.set(normKey, cName);
                            }
                        }
                    }
                } catch (_) {}
            }
        }
    } catch (_) {}

    const addCandidate = (rawPath, requireGit = false, allowTemp = false, customName = '') => {
        if (!rawPath || typeof rawPath !== 'string') return;
        const resolved = path.resolve(rawPath.trim());
        const key = process.platform === 'win32' ? resolved.toLowerCase() : resolved;
        if (_isSwarmWorktreePath(resolved)) return;
        if (!allowTemp && _isTempDirPath(resolved)) return;

        const effectiveName = customName || pathToCustomName.get(key) || '';

        if (seen.has(key)) {
            if (effectiveName) {
                const existing = collected.find((c) => (process.platform === 'win32' ? c.path.toLowerCase() : c.path) === key);
                if (existing && (!existing.name || existing.name === path.basename(existing.path))) {
                    existing.name = effectiveName;
                }
            }
            return;
        }

        try {
            if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) return;
            const isGit = isGitRepoDir(resolved);
            if (requireGit && !isGit) return;
            seen.add(key);
            collected.push({
                path: resolved,
                name: effectiveName || path.basename(resolved) || resolved,
                isGit
            });
        } catch (_) {}
    };

    // 1. Caller extra paths (e.g. active workspace)
    for (const p of (Array.isArray(extraPaths) ? extraPaths : [])) {
        addCandidate(p, false, true);
    }

    // 2. Explicitly remembered projects file
    try {
        const recentFile = getRecentProjectsFile();
        if (fs.existsSync(recentFile)) {
            const items = JSON.parse(fs.readFileSync(recentFile, 'utf8'));
            if (Array.isArray(items)) {
                for (const p of items) addCandidate(p, false, true);
            }
        }
    } catch (_) {}

    // 3. Projects from config (mtime desc) — 'Контент мейкер', 'Flow_vpn', 'ai_farm_tg', 'threads', etc.
    for (const item of projectConfigs) {
        try {
            const data = JSON.parse(fs.readFileSync(item.path, 'utf8'));
            if (!data) continue;
            const cName = String(data.name || '').trim();
            const resList = (data.projectResources && Array.isArray(data.projectResources.resources))
                ? data.projectResources.resources
                : (Array.isArray(data.resources) ? data.resources : []);
            for (const res of resList) {
                const uri = res && (res.folderUri || (res.gitFolder && res.gitFolder.folderUri));
                if (uri) {
                    const decoded = _decodeFileUriToPath(uri);
                    if (decoded) addCandidate(decoded, false, false, cName);
                }
            }
        } catch (_) {}
    }

    // 4. Swarm Engine checkpoints
    for (const cp of listSwarmCheckpoints(50)) {
        addCandidate(cp.project_path, false, false, cp.project_name || '');
    }

    // 5. Conversation summaries unique workspace paths with project_id matching
    try {
        const convs = _queryConversationSummariesSafe(geminiDir, 200);
        for (const row of convs) {
            const wsList = Array.isArray(row.workspace_paths) && row.workspace_paths.length > 0
                ? row.workspace_paths
                : (row.project_path ? [row.project_path] : []);
            for (const ws of wsList) {
                if (!ws) continue;
                const normKey = process.platform === 'win32' ? path.resolve(ws).toLowerCase() : path.resolve(ws);
                const cName = (row.project_id && projectIdToName.get(row.project_id)) || pathToCustomName.get(normKey) || '';
                addCandidate(ws, false, false, cName);
            }
        }
    } catch (_) {}

    // 6. IDE user global storage (opened windows, backup folders)
    try {
        const appData = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
        const storagePath = path.join(appData, 'Antigravity IDE', 'User', 'globalStorage', 'storage.json');
        if (fs.existsSync(storagePath)) {
            const st = JSON.parse(fs.readFileSync(storagePath, 'utf8'));
            const lastWin = st && st.windowsState && st.windowsState.lastActiveWindow;
            if (lastWin && lastWin.folder) addCandidate(_decodeFileUriToPath(lastWin.folder), false);
            const opened = (st && st.windowsState && Array.isArray(st.windowsState.openedWindows))
                ? st.windowsState.openedWindows
                : [];
            for (const w of opened) {
                if (w && w.folder) addCandidate(_decodeFileUriToPath(w.folder), false);
            }
            const backupFolders = st && st.backupWorkspaces && Array.isArray(st.backupWorkspaces.folders)
                ? st.backupWorkspaces.folders
                : [];
            for (const f of backupFolders) {
                if (f && f.folderUri) addCandidate(_decodeFileUriToPath(f.folderUri), false);
            }
        }
    } catch (_) {}

    return collected.slice(0, 25);
}

/**
 * Initialize a new Swarm task (`swarm.cli prepare`) and return dispatch prompt + checkpoint summary.
 */
async function prepareSwarmTask({
    projectPath,
    request,
    enginePath = null,
    workspacePaths = [],
    extraDirs = [],
    useAffectedTests = true,
    modelConfig = null,
    appTarget = 'ide',
} = {}) {
    const cleanProject = String(projectPath || '').trim();
    const cleanRequest = String(request || '').trim();
    if (!cleanProject) return { ok: false, error: 'Не указана папка git-проекта.' };
    if (!cleanRequest) return { ok: false, error: 'Опишите задачу для Swarm.' };
    if (!fs.existsSync(cleanProject)) return { ok: false, error: `Папка не найдена: ${cleanProject}` };

    const resolvedEngine = enginePath || resolveSwarmEnginePath(
        [cleanProject, ...(Array.isArray(workspacePaths) ? workspacePaths : [workspacePaths])],
        extraDirs
    );
    if (!resolvedEngine) {
        return { ok: false, error: 'Swarm Engine не найден (ни в проекте, ни в ~/.antigravity_tools/swarm_engine).' };
    }

    const cfg = modelConfig || loadModelConfigSafely(extraDirs);
    const prefOrch = (cfg && Array.isArray(cfg.orchestrator_order) && cfg.orchestrator_order[0]) || '';
    const quotaInfo = loadQuotaPlanSafely(extraDirs, prefOrch || 'claude', { appTarget });
    const cliArgs = ['prepare', '--project', cleanProject, '--request', cleanRequest];
    if (useAffectedTests !== false) cliArgs.push('--use-affected-tests');
    const effectiveOrch = prefOrch || (quotaInfo.plan && quotaInfo.plan.strategy === 'degraded_swarm' ? quotaInfo.plan.model : '');
    if (effectiveOrch) cliArgs.push('--orchestrator-model', String(effectiveOrch));

    const prep = await runSwarmCliAsync(cliArgs, { enginePath: resolvedEngine });
    if (!prep.ok || !prep.data || !prep.data.task_id) {
        return { ok: false, error: prep.error || 'Не удалось создать Swarm checkpoint.' };
    }

    rememberProject(cleanProject);
    const taskId = prep.data.task_id;
    const dispatchPrompt = buildSwarmDispatchPrompt({
        taskId,
        projectPath: cleanProject,
        prompt: cleanRequest,
        enginePath: resolvedEngine,
        modelConfig: cfg,
        mode: 'run',
        useAffectedTests: useAffectedTests !== false,
        quotaPlan: quotaInfo.plan,
        assistantSettings: quotaInfo.settings
    });
    return {
        ok: true,
        taskId,
        projectPath: cleanProject,
        enginePath: resolvedEngine,
        dispatchPrompt,
        quotaPlan: quotaInfo.plan,
        checkpoint: loadCheckpointSummary(taskId)
    };
}

/**
 * Resume an existing Swarm task (`swarm.cli resume`) and return dispatch prompt + checkpoint summary.
 */
async function resumeSwarmTask({
    taskId,
    projectPath = '',
    enginePath = null,
    workspacePaths = [],
    extraDirs = [],
    modelConfig = null,
    appTarget = 'ide',
} = {}) {
    if (!taskId) return { ok: false, error: 'Не указан task_id для возобновления.' };
    const cp = loadCheckpointSummary(taskId);
    const targetProject = String(projectPath || (cp && cp.project_path) || '').trim();
    const resolvedEngine = enginePath || resolveSwarmEnginePath(
        [targetProject, ...(Array.isArray(workspacePaths) ? workspacePaths : [workspacePaths])],
        extraDirs
    );
    if (!resolvedEngine) {
        return { ok: false, error: 'Swarm Engine не найден для возобновления задачи.' };
    }

    const res = await runSwarmCliAsync(['resume', '--task-id', taskId], { enginePath: resolvedEngine });
    if (!res.ok) {
        return { ok: false, error: res.error || 'Ошибка resume Swarm.' };
    }

    const cfg = modelConfig || loadModelConfigSafely(extraDirs);
    const refreshedCp = loadCheckpointSummary(taskId) || cp;
    const quotaInfo = loadQuotaPlanSafely(extraDirs, (refreshedCp && refreshedCp.orchestrator_model) || 'claude', { appTarget });
    const dispatchPrompt = buildSwarmDispatchPrompt({
        taskId,
        projectPath: targetProject,
        prompt: (cp && cp.user_request) || '',
        enginePath: resolvedEngine,
        modelConfig: cfg,
        mode: 'resume',
        stateSummary: res.data || refreshedCp,
        quotaPlan: quotaInfo.plan,
        assistantSettings: quotaInfo.settings
    });
    return {
        ok: true,
        taskId,
        projectPath: targetProject,
        enginePath: resolvedEngine,
        dispatchPrompt,
        quotaPlan: quotaInfo.plan,
        checkpoint: refreshedCp,
        resumeData: res.data || null
    };
}

/**
 * Cleanup task worktrees (`swarm.cli cleanup`) and optionally remove checkpoint folder.
 */
async function cleanupSwarmTask({
    taskId,
    projectPath = '',
    enginePath = null,
    workspacePaths = [],
    extraDirs = [],
    deleteState = false
} = {}) {
    if (!taskId) return { ok: false, error: 'Не указан task_id.' };
    const cp = loadCheckpointSummary(taskId);
    const targetProject = String(projectPath || (cp && cp.project_path) || '').trim();
    const resolvedEngine = enginePath || resolveSwarmEnginePath(
        [targetProject, ...(Array.isArray(workspacePaths) ? workspacePaths : [workspacePaths])],
        extraDirs
    );

    let cliError = null;
    if (resolvedEngine && cp) {
        const res = await runSwarmCliAsync(['cleanup', '--task-id', taskId], { enginePath: resolvedEngine });
        if (!res.ok) cliError = res.error;
    }
    const deletedState = deleteState ? deleteSwarmCheckpoint(taskId) : false;
    return {
        ok: deletedState || !cliError,
        taskId,
        deletedState,
        warning: cliError || null,
        checkpoints: listSwarmCheckpoints(25)
    };
}

function _normalizeProjectKey(projectPath) {
    if (!projectPath || typeof projectPath !== 'string') return '__unassigned__';
    let resolved = projectPath.trim();
    if (/^[a-zA-Z]:[\\/]/.test(resolved)) {
        return resolved.replace(/\//g, '\\').toLowerCase();
    }
    try {
        resolved = path.resolve(resolved);
    } catch (_) {}
    return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
}

function _prettifyProjectPath(p) {
    if (!p || typeof p !== 'string') return '';
    if (/^[a-z]:[\\/]/.test(p)) {
        return p.charAt(0).toUpperCase() + p.slice(1);
    }
    return p;
}

let _cachedSqliteModule;
function _getSqliteSyncClass() {
    if (_cachedSqliteModule !== undefined) return _cachedSqliteModule;
    try {
        const mod = require('node:sqlite');
        _cachedSqliteModule = mod && typeof mod.DatabaseSync === 'function' ? mod.DatabaseSync : null;
    } catch (_) {
        _cachedSqliteModule = null;
    }
    return _cachedSqliteModule;
}

function _readFirstTranscriptTimestampMs(geminiDir, convId) {
    if (!geminiDir || !convId || /[\\/]/.test(String(convId))) return 0;
    const logPath = path.join(geminiDir, 'brain', path.basename(String(convId)), '.system_generated', 'logs', 'transcript.jsonl');
    let fd;
    try {
        if (!fs.existsSync(logPath)) return 0;
        fd = fs.openSync(logPath, 'r');
        const buf = Buffer.alloc(640);
        const bytesRead = fs.readSync(fd, buf, 0, buf.length, 0);
        if (bytesRead <= 0) return 0;
        const firstLine = buf.toString('utf8', 0, bytesRead).split(/\r?\n/)[0];
        if (!firstLine) return 0;
        const obj = JSON.parse(firstLine);
        return _parseTimestampMs(obj && obj.created_at);
    } catch (_) {
        return 0;
    } finally {
        if (fd !== undefined) {
            try { fs.closeSync(fd); } catch (_) {}
        }
    }
}

function _isTranscriptActivelyStreaming(geminiDir, convId, nowMs = Date.now()) {
    if (!geminiDir || !convId || /[\\/]/.test(String(convId))) return false;
    const logPath = path.join(geminiDir, 'brain', path.basename(String(convId)), '.system_generated', 'logs', 'transcript.jsonl');
    let fd;
    try {
        if (!fs.existsSync(logPath)) return false;
        const st = fs.statSync(logPath);
        if (nowMs - st.mtimeMs > 25000 || st.size <= 0) return false;
        const readLen = Math.min(2048, st.size);
        const startPos = Math.max(0, st.size - readLen);
        fd = fs.openSync(logPath, 'r');
        const buf = Buffer.alloc(readLen);
        const bytesRead = fs.readSync(fd, buf, 0, readLen, startPos);
        if (bytesRead <= 0) return false;
        const lines = buf.toString('utf8', 0, bytesRead).split(/\r?\n/);
        for (let i = lines.length - 1; i >= 0; i--) {
            const s = lines[i].trim();
            if (!s) continue;
            try {
                const obj = JSON.parse(s);
                if (obj && obj.type === 'PLANNER_RESPONSE' && obj.status === 'DONE') {
                    return false;
                }
                return true;
            } catch (_) {
                return true;
            }
        }
        return false;
    } catch (_) {
        return false;
    } finally {
        if (fd !== undefined) {
            try { fs.closeSync(fd); } catch (_) {}
        }
    }
}

const _dbSchemaProjectIdCache = new Map();

function _queryConversationSummariesSafe(geminiDir, limit = 160) {
    const DatabaseSync = _getSqliteSyncClass();
    if (!DatabaseSync) return [];
    const targetDir = geminiDir || getGeminiAntigravityDir();
    const dbPath = path.join(targetDir, 'conversation_summaries.db');
    if (!fs.existsSync(dbPath)) return [];
    let db;
    try {
        db = new DatabaseSync(dbPath, { readOnly: true });
        try { db.exec('PRAGMA busy_timeout = 1000;'); } catch (_) {}
        let hasProjectId = _dbSchemaProjectIdCache.get(dbPath);
        if (hasProjectId === undefined) {
            hasProjectId = false;
            try {
                const cols = db.prepare('PRAGMA table_info(conversation_summaries)').all();
                hasProjectId = cols.some((c) => c.name === 'project_id');
            } catch (_) {}
            _dbSchemaProjectIdCache.set(dbPath, hasProjectId);
        }

        const nowMs = Date.now();
        const projCol = hasProjectId ? ', project_id' : '';
        const rows = db.prepare(
            `SELECT conversation_id, title, preview, step_count, last_modified_time,
                    workspace_uris, status, agent_name, parent_conversation_id,
                    nesting_depth, not_fully_idle, killed, last_user_input_time${projCol}
             FROM conversation_summaries
             ORDER BY last_modified_time DESC
             LIMIT ?`
        ).all(Number(limit) || 160);

        return rows.map((r) => {
            let workspaces = [];
            try {
                const parsed = JSON.parse(r.workspace_uris || '[]');
                if (Array.isArray(parsed)) {
                    workspaces = parsed.map((u) => _prettifyProjectPath(_decodeFileUriToPath(u) || '')).filter(Boolean);
                }
            } catch (_) {}
            const primaryWs = workspaces[0] || '';
            const isKilled = Boolean(r.killed);
            const modMs = _parseTimestampMs(r.last_modified_time);
            const inputMs = _parseTimestampMs(r.last_user_input_time);
            const cid = r.conversation_id || '';
            const isRunning = !isKilled && (
                r.status === 'CASCADE_RUN_STATUS_RUNNING' ||
                (Number(r.not_fully_idle) === 1 && (nowMs - modMs) <= 20 * 60 * 1000) ||
                _isTranscriptActivelyStreaming(targetDir, cid, nowMs)
            );
            return {
                conversation_id: cid,
                parent_conversation_id: r.parent_conversation_id || '',
                agent_name: r.agent_name || '',
                nesting_depth: Number(r.nesting_depth || 0),
                title: r.title || '',
                preview: r.preview || '',
                step_count: Number(r.step_count || 0),
                project_path: primaryWs,
                workspace_paths: workspaces,
                project_id: (hasProjectId && r.project_id) || '',
                project_name: primaryWs ? path.basename(primaryWs) : '',
                status: r.status || '',
                is_running: isRunning,
                is_killed: isKilled,
                last_modified_ms: modMs,
                last_user_input_ms: inputMs
            };
        });
    } catch (_) {
        return [];
    } finally {
        if (db) {
            try { db.close(); } catch (_) {}
        }
    }
}

function _cleanBoardBriefText(rawText) {
    if (!rawText || typeof rawText !== 'string') return '';
    return rawText
        .replace(/^@[A-Za-z0-9_.-]+(\s+@[A-Za-z0-9_.-]+)*\s*/, '')
        .replace(/`/g, '')
        .replace(/\s+/g, ' ')
        .trim();
}

function _parseBoardJsonl(boardPath) {
    if (!fs.existsSync(boardPath)) return [];
    const out = [];
    try {
        const lines = fs.readFileSync(boardPath, 'utf8').split(/\r?\n/);
        for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            try {
                const obj = JSON.parse(trimmed);
                out.push({
                    seq: obj.seq != null ? Number(obj.seq) : out.length + 1,
                    agent: obj.agent || '',
                    text: String(obj.text || '')
                });
            } catch (_) {}
        }
    } catch (_) {}
    return out;
}

function readSwarmBoard(swarmId, geminiDirOverride = null, dbById = null) {
    if (!swarmId || /[\\/]/.test(String(swarmId))) return null;
    const geminiDir = geminiDirOverride || getGeminiAntigravityDir();
    const safeId = path.basename(String(swarmId));
    const swarmDir = path.join(geminiDir, 'brain', safeId, '.system_generated', 'swarm');
    if (!fs.existsSync(swarmDir)) return null;

    let swarmMeta = {};
    let membersMeta = {};
    try {
        const sf = path.join(swarmDir, 'swarm.json');
        if (fs.existsSync(sf)) swarmMeta = JSON.parse(fs.readFileSync(sf, 'utf8'));
    } catch (_) {}
    try {
        const mf = path.join(swarmDir, 'members.json');
        if (fs.existsSync(mf)) membersMeta = JSON.parse(fs.readFileSync(mf, 'utf8'));
    } catch (_) {}

    const messages = _parseBoardJsonl(path.join(swarmDir, 'board.jsonl'));
    const dbMap = dbById || (() => {
        const map = new Map();
        for (const row of _queryConversationSummariesSafe(geminiDir, 200)) {
            if (row.conversation_id) map.set(row.conversation_id, row);
        }
        return map;
    })();

    const parentDb = dbMap.get(safeId) || null;
    const boardDone = messages.length > 0 && messages.some((m) =>
        m.agent === 'coordinator' && /^\[done\]/i.test(String(m.text || '').trim())
    );
    const parentIdle = Boolean(parentDb && !parentDb.is_running);

    const briefByMember = new Map();
    for (const msg of messages) {
        if (msg.agent !== 'coordinator' || !msg.text) continue;
        const mentions = msg.text.match(/@([A-Za-z0-9_.-]+)/g) || [];
        for (const token of mentions) {
            const target = token.slice(1);
            if (!briefByMember.has(target) && target !== 'coordinator') {
                briefByMember.set(target, _cleanBoardBriefText(msg.text));
            }
        }
    }

    const rawMembers = (membersMeta && membersMeta.members && typeof membersMeta.members === 'object')
        ? membersMeta.members : {};
    const nonCoordinatorWorkers = Object.entries(rawMembers).filter(([k, v]) => k !== 'coordinator' && v && v.role !== 'coordinator');
    const allWorkersDone = nonCoordinatorWorkers.length > 0 && nonCoordinatorWorkers.every(([_, w]) => w.killed || String(w.phase || '').toUpperCase() === 'KILLED');

    const memberNames = Object.keys(rawMembers);
    const members = memberNames.map((name) => {
        const m = rawMembers[name] || {};
        const convId = m.conversation_id || '';
        const mDb = convId ? dbMap.get(convId) : null;
        let status = 'pending';
        if (m.killed || String(m.phase || '').toUpperCase() === 'KILLED') {
            status = 'done';
        } else if (name === 'coordinator' || m.role === 'coordinator') {
            // Coordinator is never left running if board is done, all workers are done, or parent conversation is idle
            if (boardDone || allWorkersDone || parentIdle || (parentDb && !parentDb.is_running)) {
                status = 'done';
            } else {
                status = (parentDb && parentDb.is_running) ? 'running' : 'waiting';
            }
        } else if (mDb) {
            if (mDb.is_running) status = 'running';
            else if (boardDone || parentIdle) status = 'done';
            else status = 'waiting';
        } else if (boardDone || parentIdle) {
            status = 'done';
        } else if (String(m.phase || '').toUpperCase() === 'RUNNING') {
            status = 'running';
        } else {
            status = 'waiting';
        }

        const firstTs = convId ? _readFirstTranscriptTimestampMs(geminiDir, convId) : 0;
        const startedAtMs = firstTs || _parseTimestampMs(swarmMeta.created_at);
        const finishedAtMs = (status === 'done' && mDb && mDb.last_modified_ms) ? mDb.last_modified_ms : 0;
        const durationSec = (startedAtMs > 0 && finishedAtMs > startedAtMs)
            ? Math.round((finishedAtMs - startedAtMs) / 1000)
            : 0;

        return {
            name,
            conversation_id: convId,
            role: m.role || 'worker',
            model: m.applied_model || m.desired_model || m.model || '',
            phase: m.phase || '',
            wake_mode: m.wake_mode || '',
            status,
            task_description: briefByMember.get(name) || '',
            started_at_ms: startedAtMs,
            finished_at_ms: finishedAtMs,
            duration_seconds: durationSec
        };
    });

    const createdMs = _parseTimestampMs(swarmMeta.created_at) || (parentDb && parentDb.last_user_input_ms) || 0;
    let boardMtimeMs = 0;
    try {
        const bp = path.join(swarmDir, 'board.jsonl');
        if (fs.existsSync(bp)) boardMtimeMs = fs.statSync(bp).mtimeMs;
    } catch (_) {}

    const anyMemberRunning = members.some((m) => m.status === 'running');
    const allMembersDone = members.length > 0 && members.every((m) => m.status === 'done' || m.status === 'killed' || m.status === 'failed');
    const isActive = !allMembersDone && !boardDone && Boolean((parentDb && parentDb.is_running) || anyMemberRunning);
    const updatedMs = Math.max(
        createdMs,
        boardMtimeMs,
        (parentDb && parentDb.last_modified_ms) || 0,
        ...members.map((m) => m.finished_at_ms || m.started_at_ms || 0)
    );

    return {
        swarm_id: swarmMeta.swarm_id || safeId,
        title: (membersMeta && membersMeta.title) || (parentDb && parentDb.title) || safeId,
        project_path: (parentDb && parentDb.project_path) || '',
        project_name: (parentDb && parentDb.project_name) || '',
        created_at_ms: createdMs,
        updated_at_ms: updatedMs,
        is_active: isActive,
        is_complete: !isActive || allMembersDone || boardDone || parentIdle,
        members,
        messages,
        board_count: messages.length,
        board_text: messages.map((m) => m.text).join('\n')
    };
}

function _collectNativeSwarms(geminiDir, dbById) {
    const brainDir = path.join(geminiDir || getGeminiAntigravityDir(), 'brain');
    if (!fs.existsSync(brainDir)) return [];
    const results = [];
    let entries = [];
    try {
        entries = fs.readdirSync(brainDir, { withFileTypes: true });
    } catch (_) {
        return [];
    }
    for (const ent of entries) {
        if (!ent.isDirectory()) continue;
        const swDir = path.join(brainDir, ent.name, '.system_generated', 'swarm');
        if (fs.existsSync(swDir)) {
            const parsed = readSwarmBoard(ent.name, geminiDir, dbById);
            if (parsed) results.push(parsed);
        }
    }
    results.sort((a, b) => (b.updated_at_ms || 0) - (a.updated_at_ms || 0));
    return results;
}

/**
 * Pure aggregator building unified Mission Control snapshot grouped by project.
 * @param {{ checkpoints?: Array, nativeSwarms?: Array, conversations?: Array, nowMs?: number }} [input]
 * @returns {{ summary: object, projects: Array<object>, updated_at_ms: number }}
 */
function buildAgentMonitorSnapshot(input = {}) {
    const checkpoints = Array.isArray(input.checkpoints) ? input.checkpoints : [];
    const nativeSwarms = Array.isArray(input.nativeSwarms) ? input.nativeSwarms : [];
    const conversations = Array.isArray(input.conversations) ? input.conversations : [];
    const nowMs = Number(input.nowMs || Date.now());

    const consumedSwarmIds = new Set();
    const consumedConvIds = new Set();

    const projectBuckets = new Map();
    const ensureProject = (projPath, projName) => {
        const cleanPath = _prettifyProjectPath(projPath || '');
        const key = _normalizeProjectKey(cleanPath);
        if (!projectBuckets.has(key)) {
            projectBuckets.set(key, {
                project_key: key,
                project_path: cleanPath,
                project_name: projName || (cleanPath ? path.basename(cleanPath) : 'Вне проекта'),
                active_agents: 0,
                done_agents: 0,
                failed_agents: 0,
                pending_agents: 0,
                total_agents: 0,
                active_sessions: 0,
                latest_activity_ms: 0,
                active_started_at_ms: 0,
                sessions: []
            });
        }
        return projectBuckets.get(key);
    };

    const addSessionToProject = (projPath, projName, session) => {
        const bucket = ensureProject(projPath, projName);
        bucket.sessions.push(session);
        const workers = Array.isArray(session.workers) ? session.workers : [];
        let sActive = 0;
        let sDone = 0;
        let sFailed = 0;
        let sPending = 0;
        let earliestActiveStart = 0;
        for (const w of workers) {
            const st = String(w.status || '').toLowerCase();
            if (st === 'running' || st === 'working') {
                sActive++;
                if (w.started_at_ms > 0 && (!earliestActiveStart || w.started_at_ms < earliestActiveStart)) {
                    earliestActiveStart = w.started_at_ms;
                }
            } else if (st === 'done' || st === 'killed' || st === 'completed') {
                sDone++;
            } else if (st === 'failed' || st === 'error') {
                sFailed++;
            } else {
                sPending++;
            }
        }
        // If session is actively executing (e.g. phase DECOMPOSE/VERIFY) even with 0 per-worker running marks
        if (session.is_active && !session.is_complete && sActive === 0 && (workers.length === 0 || workers.some((w) => w.status !== 'done' && w.status !== 'killed'))) {
            sActive = 1;
            if (session.started_at_ms > 0) earliestActiveStart = session.started_at_ms;
        }
        bucket.active_agents += sActive;
        bucket.done_agents += sDone;
        bucket.failed_agents += sFailed;
        bucket.pending_agents += sPending;
        bucket.total_agents += Math.max(workers.length, (session.is_active && !session.is_complete) ? 1 : 0);
        if (session.is_active && !session.is_complete && sActive > 0) {
            bucket.active_sessions += 1;
            const candStart = earliestActiveStart || session.started_at_ms || 0;
            if (candStart > 0 && (!bucket.active_started_at_ms || candStart < bucket.active_started_at_ms)) {
                bucket.active_started_at_ms = candStart;
            }
        }
        const actMs = Number(session.updated_at_ms || session.started_at_ms || 0);
        if (actMs > bucket.latest_activity_ms) bucket.latest_activity_ms = actMs;
    };

    // 1. Swarm Engine checkpoints (linked with native Swarm boards when matching task_id/shortId)
    for (const cp of checkpoints) {
        const cpKey = _normalizeProjectKey(cp.project_path);
        const shortId = String(cp.task_id || '').slice(0, 8);
        let matchedNative = null;
        for (const ns of nativeSwarms) {
            if (consumedSwarmIds.has(ns.swarm_id)) continue;
            const sameProj = !ns.project_path || _normalizeProjectKey(ns.project_path) === cpKey;
            const boardTxt = ns.board_text || '';
            if ((cp.task_id && boardTxt.includes(cp.task_id)) || (shortId && shortId.length >= 6 && boardTxt.includes(shortId))) {
                matchedNative = ns;
                break;
            }
            if (sameProj && Array.isArray(cp.workers) && cp.workers.length > 0 && Array.isArray(ns.members)) {
                const nsNames = new Set(ns.members.map((m) => m.name));
                const overlap = cp.workers.filter((w) => nsNames.has(w.name)).length;
                if (overlap >= Math.min(2, cp.workers.length)) {
                    matchedNative = ns;
                    break;
                }
            }
        }

        const mergedWorkers = (Array.isArray(cp.workers) ? cp.workers : []).map((w) => Object.assign({}, w));
        if (matchedNative) {
            consumedSwarmIds.add(matchedNative.swarm_id);
            if (matchedNative.is_active) {
                consumedConvIds.add(matchedNative.swarm_id);
            }
            const byName = new Map(mergedWorkers.map((w) => [w.name, w]));
            for (const nm of matchedNative.members) {
                if (nm.conversation_id && (matchedNative.is_active || nm.status === 'running')) {
                    consumedConvIds.add(nm.conversation_id);
                }
                const existing = byName.get(nm.name);
                if (existing) {
                    if (!existing.task_description && nm.task_description) existing.task_description = nm.task_description;
                    if (!existing.started_at_ms && nm.started_at_ms) existing.started_at_ms = nm.started_at_ms;
                    if (!existing.duration_seconds && nm.duration_seconds) existing.duration_seconds = nm.duration_seconds;
                    if (existing.status === 'pending' && nm.status === 'running') existing.status = 'running';
                    if (nm.model && existing.model === 'flash') existing.model = nm.model;
                } else {
                    mergedWorkers.push({
                        name: nm.name,
                        role: nm.role || 'worker',
                        status: nm.status,
                        model: nm.model || '',
                        task_description: nm.task_description || '',
                        started_at_ms: nm.started_at_ms || 0,
                        finished_at_ms: nm.finished_at_ms || 0,
                        duration_seconds: nm.duration_seconds || 0,
                        files: []
                    });
                }
            }
        }

        const isActive = Boolean(!cp.is_complete && (
            Number(cp.running_workers || 0) > 0 ||
            mergedWorkers.some((w) => w.status === 'running') ||
            (matchedNative && matchedNative.is_active)
        ));
        const startedMs = cp.created_at_ms || (matchedNative && matchedNative.created_at_ms) || cp.mtimeMs || 0;
        const updatedMs = Math.max(cp.updated_at_ms || 0, (matchedNative && matchedNative.updated_at_ms) || 0, cp.mtimeMs || 0);
        const totalDur = (cp.is_complete && startedMs > 0 && updatedMs > startedMs)
            ? Math.round((updatedMs - startedMs) / 1000)
            : 0;

        addSessionToProject(cp.project_path, cp.project_name, {
            id: cp.task_id,
            source: 'swarm_engine',
            title: cp.user_request || cp.task_id,
            user_request: cp.user_request || '',
            stage_label: cp.stage_label || getStageLabel(cp.current_node, cp.is_complete, cp.errors),
            orchestrator_model: cp.orchestrator_model || '',
            is_active: isActive,
            is_complete: Boolean(cp.is_complete),
            errors: cp.errors || [],
            workers: mergedWorkers,
            native_swarm_id: matchedNative ? matchedNative.swarm_id : '',
            board_count: matchedNative ? matchedNative.board_count : 0,
            started_at_ms: startedMs,
            updated_at_ms: updatedMs,
            duration_seconds: totalDur
        });
    }

    // 2. Remaining native Antigravity Swarms (not already linked to a Swarm Engine checkpoint)
    for (const ns of nativeSwarms) {
        if (consumedSwarmIds.has(ns.swarm_id)) continue;
        consumedSwarmIds.add(ns.swarm_id);
        if (ns.is_active) {
            consumedConvIds.add(ns.swarm_id);
        }
        for (const m of (ns.members || [])) {
            if (m.conversation_id && (ns.is_active || m.status === 'running')) {
                consumedConvIds.add(m.conversation_id);
            }
        }
        const workers = (ns.members || []).map((m) => ({
            name: m.name,
            role: m.role || 'worker',
            status: m.status,
            model: m.model || '',
            task_description: m.task_description || '',
            started_at_ms: m.started_at_ms || 0,
            finished_at_ms: m.finished_at_ms || 0,
            duration_seconds: m.duration_seconds || 0,
            files: []
        }));
        const totalDur = (ns.is_complete && ns.created_at_ms > 0 && ns.updated_at_ms > ns.created_at_ms)
            ? Math.round((ns.updated_at_ms - ns.created_at_ms) / 1000)
            : 0;
        addSessionToProject(ns.project_path, ns.project_name, {
            id: ns.swarm_id,
            source: 'native_swarm',
            title: ns.title || ns.swarm_id,
            stage_label: ns.is_active ? 'Рой работает' : 'Готово',
            orchestrator_model: '',
            is_active: Boolean(ns.is_active),
            is_complete: Boolean(ns.is_complete),
            errors: [],
            workers,
            native_swarm_id: ns.swarm_id,
            board_count: ns.board_count || 0,
            started_at_ms: ns.created_at_ms || 0,
            updated_at_ms: ns.updated_at_ms || 0,
            duration_seconds: totalDur
        });
    }

    // 3. Active standalone Antigravity chats & unlinked subagent trees from conversation_summaries.db
    const childrenByParent = new Map();
    for (const c of conversations) {
        if (c.parent_conversation_id) {
            if (!childrenByParent.has(c.parent_conversation_id)) {
                childrenByParent.set(c.parent_conversation_id, []);
            }
            childrenByParent.get(c.parent_conversation_id).push(c);
        }
    }

    for (const c of conversations) {
        if (!c.conversation_id || consumedConvIds.has(c.conversation_id)) continue;
        if (c.parent_conversation_id) continue; // handled under parent

        const rawChildren = (childrenByParent.get(c.conversation_id) || []).filter(
            (ch) => !consumedConvIds.has(ch.conversation_id)
        );
        const hasActiveChild = rawChildren.some((ch) => ch.is_running);
        const sessionIsActive = Boolean(c.is_running || hasActiveChild);

        // Include only currently active sessions or conversations that actually spawned subagents
        if (!sessionIsActive && rawChildren.length === 0) continue;

        consumedConvIds.add(c.conversation_id);
        let workers = rawChildren.length > 0
            ? rawChildren.map((ch) => {
                consumedConvIds.add(ch.conversation_id);
                const startedMs = ch.last_user_input_ms || ch.last_modified_ms || 0;
                return {
                    name: ch.agent_name || ch.conversation_id.slice(0, 8),
                    role: 'subagent',
                    status: ch.is_running ? 'running' : 'done',
                    model: '',
                    task_description: ch.title || ch.preview || '',
                    started_at_ms: startedMs,
                    finished_at_ms: ch.is_running ? 0 : ch.last_modified_ms,
                    duration_seconds: 0,
                    files: []
                };
            })
            : [{
                name: c.agent_name || 'Главный агент',
                role: 'agent',
                status: 'running',
                model: '',
                task_description: c.title || c.preview || '',
                started_at_ms: c.last_user_input_ms || c.last_modified_ms || 0,
                finished_at_ms: 0,
                duration_seconds: 0,
                files: []
            }];
        if (rawChildren.length > 0 && c.is_running && !hasActiveChild) {
            workers = [{
                name: c.agent_name || 'Главный агент',
                role: 'agent',
                status: 'running',
                model: '',
                task_description: c.title || c.preview || '',
                started_at_ms: c.last_user_input_ms || c.last_modified_ms || 0,
                finished_at_ms: 0,
                duration_seconds: 0,
                files: []
            }, ...workers];
        }

        const startedMs = c.last_user_input_ms || c.last_modified_ms || 0;
        addSessionToProject(c.project_path, c.project_name, {
            id: c.conversation_id,
            source: rawChildren.length > 0 ? 'subagent_tree' : 'chat_agent',
            title: c.title || c.conversation_id,
            stage_label: sessionIsActive ? 'В работе' : 'Готово',
            orchestrator_model: '',
            is_active: sessionIsActive,
            is_complete: !sessionIsActive,
            errors: [],
            workers,
            native_swarm_id: '',
            board_count: 0,
            started_at_ms: startedMs,
            updated_at_ms: c.last_modified_ms || startedMs,
            duration_seconds: 0
        });
    }

    const projects = Array.from(projectBuckets.values());
    for (const proj of projects) {
        proj.sessions.sort((a, b) => {
            if (Boolean(a.is_active) !== Boolean(b.is_active)) return a.is_active ? -1 : 1;
            return (b.updated_at_ms || 0) - (a.updated_at_ms || 0);
        });
    }
    projects.sort((a, b) => {
        const aAct = a.active_agents > 0 ? 1 : 0;
        const bAct = b.active_agents > 0 ? 1 : 0;
        if (aAct !== bAct) return bAct - aAct;
        if (a.active_agents !== b.active_agents) return b.active_agents - a.active_agents;
        return (b.latest_activity_ms || 0) - (a.latest_activity_ms || 0);
    });

    const summary = {
        active_agents: projects.reduce((acc, p) => acc + p.active_agents, 0),
        active_projects: projects.filter((p) => p.active_agents > 0).length,
        active_sessions: projects.reduce((acc, p) => acc + p.active_sessions, 0),
        done_agents: projects.reduce((acc, p) => acc + p.done_agents, 0),
        failed_agents: projects.reduce((acc, p) => acc + p.failed_agents, 0),
        pending_agents: projects.reduce((acc, p) => acc + p.pending_agents, 0),
        total_projects: projects.length
    };

    // Build compact recent_projects (up to 3 projects × up to 3 completed tasks each)
    // from both swarm sessions and finished top-level conversations without altering summary/done_agents.
    const recentProjectMap = new Map();
    const addRecentTask = (projPath, projName, task) => {
        const cleanPath = _prettifyProjectPath(projPath || '');
        const key = _normalizeProjectKey(cleanPath || projName || 'default');
        const label = projName || (cleanPath ? path.basename(cleanPath) : '') || 'Проект';
        if (!recentProjectMap.has(key)) {
            recentProjectMap.set(key, {
                project_name: label,
                project_path: cleanPath,
                latest_ms: 0,
                seenTitles: new Set(),
                tasks: []
            });
        }
        const entry = recentProjectMap.get(key);
        const rawTitle = String(task.title || task.user_request || task.name || '').replace(/\s+/g, ' ').trim();
        const normTitle = rawTitle.toLowerCase();
        if (!rawTitle || entry.seenTitles.has(normTitle)) return;
        entry.seenTitles.add(normTitle);
        const ts = Number(task.updated_at_ms || task.started_at_ms || 0);
        if (ts > entry.latest_ms) entry.latest_ms = ts;
        entry.tasks.push({
            id: task.id || '',
            title: rawTitle,
            duration_seconds: Number(task.duration_seconds) || 0,
            isError: Boolean(task.isError || task.failed || (Array.isArray(task.errors) && task.errors.length > 0)),
            updated_at_ms: ts
        });
    };

    for (const p of projects) {
        const pName = p.project_name || (p.project_path ? path.basename(p.project_path) : '') || 'Проект';
        for (const s of (p.sessions || [])) {
            if (s.is_complete || !s.is_active) {
                addRecentTask(p.project_path, pName, s);
            }
        }
    }
    for (const c of conversations) {
        if (!c || c.parent_conversation_id || c.is_running || c.is_killed) continue;
        if (!c.project_path || _isSwarmWorktreePath(c.project_path) || _isTempDirPath(c.project_path)) continue;
        const cleanTitle = String(c.title || '').replace(/\s+/g, ' ').trim();
        if (!cleanTitle || cleanTitle === c.agent_name) continue;
        const startMs = c.last_user_input_ms || 0;
        const endMs = c.last_modified_ms || 0;
        const durSec = (startMs > 0 && endMs > startMs && (endMs - startMs) < 86400000)
            ? Math.round((endMs - startMs) / 1000)
            : 0;
        addRecentTask(c.project_path, c.project_name || path.basename(c.project_path), {
            id: c.conversation_id,
            title: cleanTitle,
            duration_seconds: durSec,
            isError: false,
            updated_at_ms: endMs || startMs
        });
    }

    const recent_projects = Array.from(recentProjectMap.values())
        .filter((rp) => rp.tasks.length > 0)
        .sort((a, b) => b.latest_ms - a.latest_ms)
        .slice(0, 3)
        .map((rp) => ({
            project_name: rp.project_name,
            project_path: rp.project_path,
            latest_ms: rp.latest_ms,
            tasks: rp.tasks.sort((a, b) => b.updated_at_ms - a.updated_at_ms).slice(0, 3)
        }));

    return { summary, projects, recent_projects, updated_at_ms: nowMs };
}

function collectAgentMonitorSnapshot(options = {}) {
    const geminiDir = options.geminiDir || getGeminiAntigravityDir();
    const limit = Number(options.limit) || 40;
    const checkpoints = listSwarmCheckpoints(limit);
    const conversations = _queryConversationSummariesSafe(geminiDir, 200);
    const dbById = new Map();
    for (const row of conversations) {
        if (row.conversation_id) dbById.set(row.conversation_id, row);
    }
    const nativeSwarms = _collectNativeSwarms(geminiDir, dbById);
    return {
        checkpoints,
        snapshot: buildAgentMonitorSnapshot({
            checkpoints,
            nativeSwarms,
            conversations,
            nowMs: Date.now()
        })
    };
}

/**
 * Format duration in seconds into human-readable compact Russian string (e.g. '45с', '1м 12с', '2ч 05м').
 * Preserves preformatted strings as-is.
 * @param {number|string} secOrMs
 * @returns {string}
 */
function formatDuration(secOrMs) {
    if (secOrMs == null) return '';
    if (typeof secOrMs === 'string') return secOrMs.trim();
    let sec = Number(secOrMs) || 0;
    if (sec > 100000) sec = Math.round(sec / 1000); // ms -> sec
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

/**
 * Format elapsed time in seconds or ms into timer format (e.g. '00:05', '01:23', '1:02:05').
 * @param {number|string} secOrMs
 * @returns {string}
 */
function formatElapsed(secOrMs) {
    if (secOrMs == null) return '00:00';
    if (typeof secOrMs === 'string') return secOrMs.trim();
    let sec = Number(secOrMs) || 0;
    if (sec > 100000) sec = Math.round(sec / 1000); // ms -> sec
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

/**
 * Russian grammatical pluralization helper.
 * @param {number} n
 * @param {string} one
 * @param {string} few
 * @param {string} many
 * @returns {string} e.g. '2 проекта'
 */
function _pluralRu(n, one, few, many) {
    const abs = Math.abs(Number(n) || 0);
    const mod10 = abs % 10;
    const mod100 = abs % 100;
    if (mod100 >= 11 && mod100 <= 19) return `${n} ${many}`;
    if (mod10 === 1) return `${n} ${one}`;
    if (mod10 >= 2 && mod10 <= 4) return `${n} ${few}`;
    return `${n} ${many}`;
}

/**
 * Format status bar / sidebar pill text according to active projects and completion states.
 * - when idle (0 active): '⚡ Агенты: 0'
 * - when recent completion active: '✓ Готово: <Proj> (<done>/<total> · <dur>)' (or '⚠ Ошибка: <Proj>' if failed)
 * - 1 active project: '⚡ <N> аг. · <Proj> · <elapsed_mm:ss>' (smart ellipsis if > 26 chars)
 * - 2+ active projects: '⚡ <N> аг. · <P> проекта · <S> задачи' (or '2 пр. · 3 зад.')
 * @param {object|null} snapshot
 * @param {object|string|null} [recentCompletionState]
 * @param {{ maxLength?: number, compact?: boolean }} [options]
 * @returns {string}
 */
function formatAgentPillText(snapshot, recentCompletionState, options = {}) {
    const summary = (snapshot && snapshot.summary) || {};
    const activeAgents = Number(summary.active_agents != null ? summary.active_agents : (snapshot && snapshot.activeAgents)) || 0;

    if (snapshot && activeAgents > 0) {
        const projects = Array.isArray(snapshot.projects) ? snapshot.projects : [];
        const activeProjects = projects.filter((p) => p && (p.active_agents > 0 || p.active_sessions > 0));

        if (activeProjects.length <= 1) {
            const p = activeProjects[0] || projects[0] || {};
            const N = p.active_agents || activeAgents;
            const proj = p.project_name || (p.project_path ? path.basename(p.project_path) : '') || 'Проект';

            const now = (snapshot && snapshot.nowMs) || (snapshot && snapshot.updated_at_ms) || Date.now();
            const startMs = p.active_started_at_ms || p.latest_activity_ms || 0;
            const elapsedSec = startMs > 0 ? Math.max(0, Math.floor((now - startMs) / 1000)) : 0;
            const elapsedStr = formatElapsed(elapsedSec);

            const prefix = `⚡ ${N} аг. · `;
            const suffix = ` · ${elapsedStr}`;
            const full = `${prefix}${proj}${suffix}`;

            const maxLen = Number(options.maxLength) || 26;
            if (full.length <= maxLen) {
                return full;
            }

            const availForProj = maxLen - prefix.length - suffix.length;
            if (availForProj >= 2) {
                const shortProj = proj.slice(0, availForProj - 1) + '…';
                return `${prefix}${shortProj}${suffix}`;
            }
            return full.slice(0, maxLen - 1) + '…';
        }

        // 2+ active projects
        const P = activeProjects.length;
        const S = summary.active_sessions || activeProjects.reduce((acc, p) => acc + (p.active_sessions || 1), 0) || P;
        const N = summary.active_agents || activeAgents;

        if (options.compact) {
            return `⚡ ${N} аг. · ${P} пр. · ${S} зад.`;
        }

        const pText = _pluralRu(P, 'проект', 'проекта', 'проектов');
        const sText = _pluralRu(S, 'задача', 'задачи', 'задач');
        const fullText = `⚡ ${N} аг. · ${pText} · ${sText}`;

        if (options.maxLength && fullText.length > options.maxLength) {
            return `⚡ ${N} аг. · ${P} пр. · ${S} зад.`;
        }
        return fullText;
    }

    // Check if there is an active session being planned (e.g. newly created quick task)
    const projects = Array.isArray(snapshot && snapshot.projects) ? snapshot.projects : [];
    const planningSession = projects.flatMap((p) => (p && Array.isArray(p.sessions)) ? p.sessions.map((s) => ({ ...s, projectName: p.project_name || (p.project_path ? path.basename(p.project_path) : 'проект') })) : [])
        .find((s) => s && !s.is_complete && (s.stage_label === 'Планирование' || s.current_node === 'DECOMPOSE') && (Date.now() - (s.started_at_ms || 0)) < 15 * 60 * 1000);

    if (planningSession) {
        const pName = planningSession.projectName || 'проект';
        return `⚡ Планирование: ${pName}`;
    }

    const comp = Array.isArray(recentCompletionState)
        ? recentCompletionState[0]
        : recentCompletionState;

    if (comp) {
        const isFailed = Boolean(
            comp.isError ||
            comp.failed ||
            comp.status === 'failed' ||
            comp.status === 'error'
        );
        const proj = typeof comp === 'string'
            ? comp
            : (comp.projectName || comp.project_name || comp.project || 'Проект');

        if (isFailed) {
            return `⚠ Ошибка: ${proj}`;
        }

        const done = comp.done != null ? comp.done : comp.done_workers;
        const total = comp.total != null ? comp.total : comp.total_workers;
        const durRaw = comp.duration != null
            ? comp.duration
            : (comp.duration_seconds != null ? comp.duration_seconds : comp.dur);
        const dur = durRaw != null ? formatDuration(durRaw) : '';

        if (done != null && total != null && dur) {
            return `✓ Готово: ${proj} (${done}/${total} · ${dur})`;
        }
        if (done != null && total != null) {
            return `✓ Готово: ${proj} (${done}/${total})`;
        }
        return `✓ Готово: ${proj}`;
    }

    return '⚡ Агенты: 0';
}

/**
 * Format rich tooltip sections (structured lines & plain/markdown text) for hover status.
 * @param {object|null} snapshot
 * @param {Array<object|string>} [recentHistory]
 * @returns {String} String instance with attached .lines, .sections, .plainText, .markdown, .header
 */
function formatAgentTooltipSections(snapshot, recentHistory = []) {
    const summary = (snapshot && snapshot.summary) || {};
    const activeAgents = Number(summary.active_agents != null ? summary.active_agents : (snapshot && snapshot.activeAgents)) || 0;
    const projects = Array.isArray(snapshot && snapshot.projects) ? snapshot.projects : [];
    let activeProjects = projects.filter((p) => p && (p.active_agents > 0 || p.active_sessions > 0));
    if (activeAgents > 0 && activeProjects.length === 0 && projects.length > 0) {
        activeProjects = projects;
    }
    const now = (snapshot && snapshot.nowMs) || (snapshot && snapshot.updated_at_ms) || Date.now();

    const lines = [];
    const mdLines = [];
    const sections = [];

    let header = '';
    if (activeAgents > 0) {
        const P = Math.max(activeProjects.length, 1);
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
            const active = Number(p.active_agents) || 0;
            const done = Number(p.done_agents) || 0;
            const total = Number(p.total_agents) || Math.max(active + done, 1);
            const isFinished = active === 0 && done > 0;

            const startMs = p.active_started_at_ms || p.latest_activity_ms || 0;
            const elapsedSec = isFinished
                ? Math.max(0, Math.floor(((p.latest_activity_ms || startMs) - startMs) / 1000))
                : (startMs > 0 ? Math.max(0, Math.floor((now - startMs) / 1000)) : 0);
            const elapsed = formatElapsed(elapsedSec);

            const countStatus = active > 0
                ? `${active}/${total} работают${done > 0 ? ` (${done} готово)` : ''}`
                : `✓ ${done}/${total} готово`;

            const projLine = `📁 ${projName} — ${countStatus} · ⏱ ${elapsed}`;
            lines.push(projLine);
            mdLines.push(`📁 **${projName}** — ${countStatus} · ⏱ ${elapsed}  `);

            const sectionObj = {
                project: projName,
                project_path: p.project_path || '',
                active,
                done,
                total,
                elapsed,
                workers: []
            };

            const sessions = Array.isArray(p.sessions) ? p.sessions : [];
            const runningWorkers = [];
            for (const sess of sessions) {
                const workers = Array.isArray(sess.workers) ? sess.workers : [];
                for (const w of workers) {
                    const status = String(w.status || '').toLowerCase();
                    if (status === 'running' || status === 'working') {
                        runningWorkers.push(w);
                    }
                }
            }

            // Active task titles under project
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
            }

            if (sessions.length === 0 && active > 0) {
                const fallbackLine = `  • Выполнение задачи · ${elapsed}`;
                lines.push(fallbackLine);
                mdLines.push(`  • *Выполнение задачи* · \`${elapsed}\`  `);
                sectionObj.workers.push({ name: 'Выполнение задачи', model: '', status: 'running', line: fallbackLine });
            }

            // Show currently running subtasks prioritizing task description/files over technical role names
            const maxVisible = 4;
            const shownWorkers = runningWorkers.slice(0, maxVisible);
            for (const w of shownWorkers) {
                const wName = w.name || 'агент';
                const wModelRaw = w.model ? String(w.model) : '';
                const wStart = w.started_at_ms || startMs || 0;
                const wElapsedSec = wStart > 0 ? Math.max(0, Math.floor((now - wStart) / 1000)) : elapsedSec;
                const wElapsed = formatElapsed(wElapsedSec);
                const rawDesc = String(w.task_description || w.task_desc || '').replace(/\s+/g, ' ').trim();
                const filesStr = Array.isArray(w.files) && w.files.length > 0 ? w.files.slice(0, 2).map((f) => path.basename(String(f))).join(', ') : '';
                const taskSummary = rawDesc
                    ? (rawDesc.length > 110 ? `${rawDesc.slice(0, 110).trimEnd()}…` : rawDesc)
                    : filesStr;

                let wLine = '';
                let wMdLine = '';
                if (taskSummary) {
                    const meta = [wName !== taskSummary ? wName : '', wModelRaw].filter(Boolean).join(', ');
                    const metaSuffix = meta ? ` (${meta})` : '';
                    wLine = `  ↳ ⏳ ${taskSummary}${metaSuffix} · ${wElapsed}`;
                    wMdLine = `  ↳ ⏳ ${taskSummary}${metaSuffix} · \`${wElapsed}\`  `;
                } else {
                    const modelTag = wModelRaw ? ` (${wModelRaw})` : '';
                    wLine = `  ↳ ⏳ ${wName}${modelTag} · ${wElapsed}`;
                    wMdLine = `  ↳ ⏳ **${wName}**${modelTag} · \`${wElapsed}\`  `;
                }
                lines.push(wLine);
                mdLines.push(wMdLine);
                sectionObj.workers.push({ name: wName, model: wModelRaw, status: 'running', line: wLine });
            }
            if (runningWorkers.length > maxVisible) {
                const remaining = runningWorkers.length - maxVisible;
                const moreLine = `  ↳ … ещё ${remaining} задач в работе`;
                lines.push(moreLine);
                mdLines.push(`  ↳ *… ещё ${remaining} задач в работе*  `);
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
                if (items.length > 0) {
                    grouped.push({ proj: rp.project_name || 'Проект', items });
                }
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
            const taskEntries = [];
            for (const item of grp.items.slice(0, 3)) {
                const rawTitle = String(item.title || item.user_request || item.name || '').replace(/\s+/g, ' ').trim();
                const title = rawTitle.length > 68 ? `${rawTitle.slice(0, 68).trimEnd()}…` : rawTitle;
                const durSec = Number(item.duration_seconds) || 0;
                const dur = durSec > 0 ? formatDuration(durSec) : (item.duration ? formatDuration(item.duration) : '');
                const isErr = Boolean(item.isError || item.failed || (item.errors && item.errors.length > 0));
                const statusSuffix = isErr ? `(ошибка${dur ? `, ${dur}` : ''})` : `(готово${dur ? `, ${dur}` : ''})`;
                const taskText = title || 'Задача';
                const taskLine = `  • ${taskText} ${statusSuffix}`;
                lines.push(taskLine);
                mdLines.push(`  • ${taskText} *${statusSuffix}*  `);
                taskEntries.push({
                    project: grp.proj,
                    title: taskText,
                    duration: dur,
                    status: isErr ? 'failed' : 'done',
                    line: taskLine
                });
            }
            sections.push({
                project: grp.proj,
                tasks: taskEntries,
                line: `📁 ${grp.proj}`
            });
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
    result.mdLines = mdLines;
    result.plainText = plainText;
    result.markdown = markdown;
    result.value = markdown;
    result.sections = sections;
    return result;
}

function _extractSessionsMap(snap) {
    const map = new Map();
    if (!snap) return map;
    const projects = Array.isArray(snap.projects) ? snap.projects : [];
    for (const p of projects) {
        const pName = p.project_name || (p.project_path ? path.basename(p.project_path) : '');
        const pPath = p.project_path || '';
        const sessions = Array.isArray(p.sessions) ? p.sessions : [];
        for (const s of sessions) {
            if (s && s.id) {
                map.set(s.id, {
                    session: s,
                    projectName: pName,
                    projectPath: pPath,
                    project: p
                });
            }
        }
    }
    return map;
}

/**
 * Detect newly completed or failed sessions between two snapshots.
 * @param {object|null} previousSnap
 * @param {object|null} currentSnap
 * @param {object} [options]
 * @returns {Array<object>|null} List of newly finished sessions (also accessible as first item) or null
 */
function detectRecentCompletion(previousSnap, currentSnap, options = {}) {
    if (!previousSnap || !currentSnap) return null;
    const prevMap = _extractSessionsMap(previousSnap);
    const currMap = _extractSessionsMap(currentSnap);

    const newlyFinished = [];

    for (const [id, currEntry] of currMap.entries()) {
        const prevEntry = prevMap.get(id);
        if (!prevEntry) continue;

        const wasActive = Boolean(prevEntry.session.is_active);
        const wasComplete = Boolean(prevEntry.session.is_complete);
        const isNowActive = Boolean(currEntry.session.is_active);
        const isNowComplete = Boolean(currEntry.session.is_complete);

        const transitioned = (wasActive && !isNowActive) || (!wasComplete && isNowComplete);
        if (!transitioned) continue;

        const s = currEntry.session;
        const workers = Array.isArray(s.workers) ? s.workers : [];
        const doneWorkers = workers.filter((w) =>
            w.status === 'done' || w.status === 'completed' || w.status === 'killed'
        ).length;
        const failedWorkers = workers.filter((w) =>
            w.status === 'failed' || w.status === 'error'
        ).length;
        const totalWorkers = Math.max(workers.length, 1);

        const hasErrors = Boolean(
            (Array.isArray(s.errors) && s.errors.length > 0) ||
            s.stage_label === 'Ошибка' ||
            (failedWorkers > 0 && doneWorkers === 0)
        );

        const durSec = Number(s.duration_seconds || 0);
        const durStr = durSec > 0 ? formatDuration(durSec) : '';

        const item = {
            id: s.id,
            sessionId: s.id,
            taskId: s.id,
            project: currEntry.projectName,
            projectName: currEntry.projectName,
            project_name: currEntry.projectName,
            projectPath: currEntry.projectPath,
            project_path: currEntry.projectPath,
            title: s.title || s.user_request || '',
            status: hasErrors ? 'failed' : 'done',
            isError: hasErrors,
            failed: hasErrors,
            done: doneWorkers,
            total: totalWorkers,
            duration: durStr,
            duration_seconds: durSec,
            completedAt: Date.now(),
            session: s
        };

        newlyFinished.push(item);
    }

    if (newlyFinished.length === 0) return null;

    Object.assign(newlyFinished, newlyFinished[0]);
    return newlyFinished;
}

/**
 * Unified IPC/action dispatcher shared across desktop Electron main & host adapters.
 */
async function handleSwarmAction(op, payload = {}, hostOpts = {}) {
    const extraDirs = Array.isArray(hostOpts.extraDirs) ? hostOpts.extraDirs : [];
    const workspacePaths = Array.isArray(hostOpts.workspacePaths) ? hostOpts.workspacePaths : [];
    const data = payload && typeof payload === 'object' ? payload : {};

    switch (op) {
        case 'list': {
            const collected = collectAgentMonitorSnapshot({ limit: Number(data.limit) || 40 });
            const tooltipObj = formatAgentTooltipSections(collected.snapshot);
            const pillText = formatAgentPillText(collected.snapshot, null);
            return {
                ok: true,
                checkpoints: collected.checkpoints,
                monitor: collected.snapshot,
                pillText,
                tooltipLines: tooltipObj.lines,
                tooltipSections: tooltipObj.sections,
                tooltipMarkdown: tooltipObj.markdown || String(tooltipObj),
                enginePath: resolveSwarmEnginePath(workspacePaths, extraDirs),
                projects: listKnownProjects(data.extraProjects || [])
            };
        }
        case 'monitor': {
            const collected = collectAgentMonitorSnapshot({ limit: Number(data.limit) || 40 });
            const uiState = (data.uiState && typeof data.uiState === 'object') ? Object.assign({}, data.uiState) : {};
            let boardData = null;
            if (uiState.selectedBoardId) {
                boardData = readSwarmBoard(uiState.selectedBoardId);
            }
            const tooltipObj = formatAgentTooltipSections(collected.snapshot);
            const pillText = formatAgentPillText(collected.snapshot, null);
            return {
                ok: true,
                monitor: collected.snapshot,
                checkpoints: collected.checkpoints,
                pillText,
                tooltipLines: tooltipObj.lines,
                tooltipSections: tooltipObj.sections,
                tooltipMarkdown: tooltipObj.markdown || String(tooltipObj),
                projects: listKnownProjects(data.extraProjects || []),
                enginePath: resolveSwarmEnginePath(workspacePaths, extraDirs),
                boardData
            };
        }
        case 'board': {
            const boardData = readSwarmBoard(data.swarmId || '');
            if (!boardData) {
                return { ok: false, error: 'Доска Swarm не найдена' };
            }
            return {
                ok: true,
                board: boardData
            };
        }
        case 'status': {
            const cp = data.taskId
                ? loadCheckpointSummary(data.taskId)
                : (listSwarmCheckpoints(1)[0] || null);
            return { ok: true, checkpoint: cp };
        }
        case 'prepare':
            return prepareSwarmTask({
                projectPath: data.projectPath,
                request: data.request,
                workspacePaths,
                extraDirs,
                useAffectedTests: data.useAffectedTests !== false,
                appTarget: hostOpts.appTarget || data.appTarget || 'ide',
            });
        case 'resume':
            return resumeSwarmTask({
                taskId: data.taskId,
                projectPath: data.projectPath,
                workspacePaths,
                extraDirs,
                appTarget: hostOpts.appTarget || data.appTarget || 'ide',
            });
        case 'cleanup':
            return cleanupSwarmTask({
                taskId: data.taskId,
                projectPath: data.projectPath,
                workspacePaths,
                extraDirs,
                deleteState: Boolean(data.deleteState)
            });
        case 'remember_project':
            return { ok: rememberProject(data.projectPath), projects: listKnownProjects() };
        case 'status_pill': {
            const snap = data.snapshot || collectAgentMonitorSnapshot({
                limit: Number(data.limit) || 25,
                geminiDir: data.geminiDir || hostOpts.geminiDir
            }).snapshot;
            const prevSnap = data.previousSnapshot || data.previousSnap || null;
            let recentCompletion = data.recentCompletion || null;
            if (!recentCompletion && prevSnap) {
                recentCompletion = detectRecentCompletion(prevSnap, snap);
            }
            const pillText = formatAgentPillText(snap, recentCompletion, data.options || {});
            const tooltipObj = formatAgentTooltipSections(snap, data.recentHistory || []);
            return {
                ok: true,
                pillText,
                tooltipSections: tooltipObj.sections || tooltipObj.lines,
                tooltipMarkdown: tooltipObj.markdown || String(tooltipObj),
                tooltipLines: tooltipObj.lines,
                recentCompletion,
                snapshot: snap
            };
        }
        default:
            return { ok: false, error: `Unknown swarm operation: ${String(op)}` };
    }
}

module.exports = {
    getSwarmRootDir,
    getGeminiAntigravityDir,
    getSwarmStateDir,
    getRecentProjectsFile,
    getCleanPythonEnv,
    resolveSwarmEnginePath,
    getStageLabel,
    countWorkerResults,
    normalizeCheckpointSummary,
    loadCheckpointSummary,
    listSwarmCheckpoints,
    deleteSwarmCheckpoint,
    readSwarmBoard,
    buildAgentMonitorSnapshot,
    collectAgentMonitorSnapshot,
    runSwarmCliAsync,
    loadModelConfigSafely,
    loadQuotaPlanSafely,
    buildSwarmDispatchPrompt,
    isGitRepoDir,
    rememberProject,
    listKnownProjects,
    prepareSwarmTask,
    resumeSwarmTask,
    cleanupSwarmTask,
    handleSwarmAction,
    formatDuration,
    formatElapsed,
    formatAgentPillText,
    formatAgentTooltipSections,
    detectRecentCompletion,
    _parseTimestampMs,
    _execFilePromise,
    _pythonCandidates,
    _resetPythonCacheForTests
};
