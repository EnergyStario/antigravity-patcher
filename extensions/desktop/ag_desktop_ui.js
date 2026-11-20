/**
 * ag_desktop_ui.js -- Antigravity Assistant Native Sidebar Widget & Account Switcher
 * Exact 1:1 visual and functional parity with Antigravity IDE:
 * - Transparent background (no box), 10px margin from Settings gear icon
 * - Inline color SVG country flags (Germany, Finland, Netherlands, USA, etc.)
 * - Orange Claude asterisk (*), light-blue Gemini star (✦), numbers in normal text color
 * - 0ms instantaneous Quota Details modal (matching IDE modal, without Gemini 2.5 Pro)
 * - Clean left-aligned tooltip
 * - QuickPick menu with 4 native actions (no fix-400 clutter)
 */

(function () {
    if (typeof window === 'undefined' || typeof document === 'undefined') {
        return;
    }
    if (window.__ag_desktop_ui_loaded) {
        return;
    }
    window.__ag_desktop_ui_loaded = true;

    // SVG Icons
    const USER_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0; opacity:0.95;"><circle cx="12" cy="7" r="4"></circle><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"></path></svg>';
    const CLAUDE_SVG = '<svg viewBox="0 0 24 24" width="13" height="13" style="vertical-align: -1px; margin-right: 2px; flex-shrink: 0;"><path fill="#e26938" d="M12 2a1.5 1.5 0 0 1 1.5 1.5v4.64l3.28-3.28a1.5 1.5 0 0 1 2.12 2.12L15.62 10.26h4.88a1.5 1.5 0 0 1 0 3h-4.88l3.28 3.28a1.5 1.5 0 0 1-2.12 2.12L13.5 15.38V20.5a1.5 1.5 0 0 1-3 0v-5.12l-3.28 3.28a1.5 1.5 0 0 1-2.12-2.12l3.28-3.28H3.5a1.5 1.5 0 0 1 0-3h4.88L5.1 7.22a1.5 1.5 0 0 1 2.12-2.12L10.5 8.38V3.5A1.5 1.5 0 0 1 12 2z"/></svg>';
    const GEMINI_SVG = '<svg viewBox="0 0 24 24" width="13" height="13" style="vertical-align: -1px; margin-right: 2px; flex-shrink: 0;"><path fill="#60a5fa" d="M12 0C12 6.627 6.627 12 0 12c6.627 0 12 5.627 12 12 0-6.627 5.627-12 12-12-6.627 0-12-5.627-12-12z"/></svg>';
    const PULSE_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#60a5fa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>';
    const SYNC_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>';
    const LOG_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fbbf24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>';
    const SWARM_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><circle cx="12" cy="5" r="2.5"></circle><circle cx="5" cy="18" r="2.5"></circle><circle cx="19" cy="18" r="2.5"></circle><line x1="10.7" y1="7.2" x2="6.3" y2="15.8"></line><line x1="13.3" y1="7.2" x2="17.7" y2="15.8"></line><line x1="7.5" y1="18" x2="16.5" y2="18"></line></svg>';
    const RESUME_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><polyline points="1 4 1 10 7 10"></polyline><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"></path><polygon points="10 8 16 12 10 16 10 8" fill="#38bdf8" stroke="none"></polygon></svg>';

    // Color SVG flags for Windows rendering
    function getCountryFlagSvg(code) {
        const c = (code || '').toUpperCase();
        switch (c) {
            case 'DE': // Germany: Black, Red, Gold
                return '<svg width="16" height="11" viewBox="0 0 5 3" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="5" height="1" y="0" fill="#000000"/><rect width="5" height="1" y="1" fill="#DD0000"/><rect width="5" height="1" y="2" fill="#FFCE00"/></svg>';
            case 'FI': // Finland: Blue cross on white
                return '<svg width="16" height="11" viewBox="0 0 18 11" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="18" height="11" fill="#FFFFFF"/><rect width="18" height="3" y="4" fill="#003580"/><rect width="3" height="11" x="5" fill="#003580"/></svg>';
            case 'NL': // Netherlands: Red, White, Blue
                return '<svg width="16" height="11" viewBox="0 0 9 6" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="9" height="2" y="0" fill="#AE1C28"/><rect width="9" height="2" y="2" fill="#FFFFFF"/><rect width="9" height="2" y="4" fill="#21468B"/></svg>';
            case 'FR': // France: Blue, White, Red
                return '<svg width="16" height="11" viewBox="0 0 3 2" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="1" height="2" x="0" fill="#002654"/><rect width="1" height="2" x="1" fill="#FFFFFF"/><rect width="1" height="2" x="2" fill="#CE1126"/></svg>';
            case 'SE': // Sweden: Yellow cross on blue
                return '<svg width="16" height="11" viewBox="0 0 16 10" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="16" height="10" fill="#006AA7"/><rect width="16" height="2" y="4" fill="#FECC00"/><rect width="2" height="10" x="5" fill="#FECC00"/></svg>';
            case 'PL': // Poland: White, Red
                return '<svg width="16" height="11" viewBox="0 0 16 10" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="16" height="5" y="0" fill="#FFFFFF"/><rect width="16" height="5" y="5" fill="#DC143C"/></svg>';
            case 'AT': // Austria: Red, White, Red
                return '<svg width="16" height="11" viewBox="0 0 3 2" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="3" height="0.67" y="0" fill="#ED2939"/><rect width="3" height="0.67" y="0.67" fill="#FFFFFF"/><rect width="3" height="0.67" y="1.33" fill="#ED2939"/></svg>';
            case 'CH': // Switzerland: Red with white cross
                return '<svg width="16" height="11" viewBox="0 0 1 1" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="1" height="1" fill="#D52B1E"/><rect width="0.2" height="0.6" x="0.4" y="0.2" fill="#FFFFFF"/><rect width="0.6" height="0.2" x="0.2" y="0.4" fill="#FFFFFF"/></svg>';
            case 'US': // USA
                return '<svg width="16" height="11" viewBox="0 0 19 10" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="19" height="10" fill="#B22234"/><rect width="19" height="1.5" y="1.5" fill="#FFFFFF"/><rect width="19" height="1.5" y="4.5" fill="#FFFFFF"/><rect width="19" height="1.5" y="7.5" fill="#FFFFFF"/><rect width="8" height="5.5" fill="#3C3B6E"/></svg>';
            case 'GB':
            case 'UK': // UK Union Jack
                return '<svg width="16" height="11" viewBox="0 0 60 30" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><clipPath id="ag_uk_s"><path d="M0,0 v30 h60 v-30 z"/></clipPath><clipPath id="ag_uk_t"><path d="M30,15 h30 v15 z v15 h-30 z h-30 v-15 z v-15 h30 z"/></clipPath><g clip-path="url(#ag_uk_s)"><path d="M0,0 v30 h60 v-30 z" fill="#012169"/><path d="M0,0 L60,30 M60,0 L0,30" stroke="#fff" stroke-width="6"/><path d="M0,0 L60,30 M60,0 L0,30" clip-path="url(#ag_uk_t)" stroke="#C8102E" stroke-width="4"/><path d="M30,0 v30 M0,15 h60" stroke="#fff" stroke-width="10"/><path d="M30,0 v30 M0,15 h60" stroke="#C8102E" stroke-width="6"/></g></svg>';
            case 'UA': // Ukraine: Blue, Yellow
                return '<svg width="16" height="11" viewBox="0 0 3 2" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="3" height="1" y="0" fill="#005BBB"/><rect width="3" height="1" y="1" fill="#FFD500"/></svg>';
            case 'RU': // Russia: White, Blue, Red
                return '<svg width="16" height="11" viewBox="0 0 3 2" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="3" height="0.67" y="0" fill="#FFFFFF"/><rect width="3" height="0.67" y="0.67" fill="#0039A6"/><rect width="3" height="0.67" y="1.33" fill="#D52B1E"/></svg>';
            case 'KZ': // Kazakhstan: Cyan
                return '<svg width="16" height="11" viewBox="0 0 2 1" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="2" height="1" fill="#00AFCA"/><circle cx="1" cy="0.5" r="0.22" fill="#FEC50C"/></svg>';
            case 'TR': // Turkey: Red with crescent
                return '<svg width="16" height="11" viewBox="0 0 3 2" style="border-radius:2px; vertical-align:middle; display:inline-block; margin-right:3px; flex-shrink:0;"><rect width="3" height="2" fill="#E30A17"/><circle cx="1.1" cy="1" r="0.45" fill="#FFFFFF"/><circle cx="1.2" cy="1" r="0.36" fill="#E30A17"/></svg>';
            default:
                return c ? `<span style="font-size:9.5px; font-weight:600; padding:1px 3px; background:#282828; border-radius:2px; color:#aaa; margin-right:3px;">${c}</span>` : '🌐 ';
        }
    }

    let activeAccount = null;
    let accountsList = [];
    let connCountry = 'Проверка...';
    let connCountryCode = '';
    let connPing = 0;
    let connOk = true;
    let connStatusText = 'Проверка...';
    let connProxyUrl = null;
    let isRefreshingNetwork = false;
    let cachedSettingsBtn = null;
    let mountedPillEl = null;
    const rejectedDockUntil = typeof WeakMap !== 'undefined' ? new WeakMap() : null;
    let remountTimer = null;
    let toastTimer = null;
    let isMenuOpen = false;
    let currentMenuLevel = 'main';
    let currentFilterText = '';
    let swarmCheckpoints = [];
    let swarmProjects = [];
    let swarmEngineAvailable = true;
    let watchedSwarmTask = null;
    let swarmPollTimer = null;
    let swarmSelectedProjectPath = '';
    let swarmPendingAction = false;
    let swarmModalRefreshTimer = null;
    let failoverPolicy = 'interactive';
    let modelConfig = {};
    let swarmQuotaPlan = null;
    let lastLiveRefreshAt = 0;
    let mountedSwarmPillEl = null;
    let recentCompletionTask = null;
    let recentCompletionTimer = null;
    let currentSelectedSwarmTask = null;
    let swarmQuickProject = '';
    let swarmBoardMessages = [];
    let _bridgePillText = null;
    let swarmSnapshot = null;
    let swarmTooltipLines = null;
    let swarmLivePollTimer = null;

    const FAILOVER_POLICY_LABELS = {
        interactive: 'Спрашивать каждый раз',
        account_hop: 'Авто: держать умную (каскад + резерв Flash)',
        model_fallback: 'Авто: быстрый Flash'
    };

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

    function formatSwarmStage(cp) {
        if (!cp) return 'В очереди';
        if (cp.stage_label) return cp.stage_label;
        if (cp.is_complete) return 'Готово';
        const key = String(cp.current_node || '').trim().toLowerCase();
        return SWARM_STAGE_LABELS[key] || (cp.current_node ? String(cp.current_node) : 'В очереди');
    }
    let inFlightLiveRefresh = null;
    let inFlightLiveForce = false;
    let pendingForceActiveRefresh = false;

    try {
        const savedNet = localStorage.getItem('ag_last_net');
        if (savedNet) {
            const parsedNet = JSON.parse(savedNet);
            if (parsedNet && typeof parsedNet === 'object') {
                if (parsedNet.country) connCountry = parsedNet.country;
                if (parsedNet.countryCode) connCountryCode = parsedNet.countryCode;
                if (typeof parsedNet.ping === 'number') connPing = parsedNet.ping;
                if (typeof parsedNet.ok === 'boolean') connOk = parsedNet.ok;
            }
        }
    } catch (_) {}

    function escapeHtml(val) {
        return String(val ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function saveCachedNet() {
        try {
            localStorage.setItem('ag_last_net', JSON.stringify({
                country: connCountry,
                countryCode: connCountryCode,
                ping: connPing,
                ok: connOk
            }));
        } catch (_) {}
    }

    function getAvatarBg(name) {
        const colors = ['#ea580c', '#0284c7', '#16a34a', '#9333ea', '#dc2626', '#d97706', '#0d9488'];
        let hash = 0;
        for (let i = 0; i < (name || '').length; i++) {
            hash = name.charCodeAt(i) + ((hash << 5) - hash);
        }
        return colors[Math.abs(hash) % colors.length];
    }

    function getAvatarSrc(acc) {
        if (!acc || typeof acc !== 'object') return '';
        if (acc.avatar_data_url && /^data:image\//i.test(String(acc.avatar_data_url))) {
            return String(acc.avatar_data_url);
        }
        if (acc.avatar_url && /^https?:\/\//i.test(String(acc.avatar_url))) {
            return String(acc.avatar_url);
        }
        return '';
    }

    function renderAvatarHtml(acc, sizePx = 16, fallbackSvg = '') {
        const label = (acc && (acc.name || acc.email)) || 'A';
        const initial = escapeHtml(label.charAt(0).toUpperCase());
        const bg = getAvatarBg(label);
        const src = getAvatarSrc(acc);
        const dimStyle = `width:${sizePx}px;height:${sizePx}px;font-size:${Math.max(9, Math.round(sizePx * 0.58))}px;`;
        if (src) {
            return `<img class="ag-avatar-img" src="${escapeHtml(src)}" alt="" style="width:${sizePx}px;height:${sizePx}px;" onerror="this.classList.add('ag-avatar-hidden');if(this.nextElementSibling)this.nextElementSibling.classList.remove('ag-avatar-hidden');" /><span class="ag-avatar-letter ag-avatar-hidden" style="${dimStyle}background:${bg};">${initial}</span>`;
        }
        if (fallbackSvg) return fallbackSvg;
        return `<span class="ag-avatar-letter" style="${dimStyle}background:${bg};">${initial}</span>`;
    }

    function injectStyles() {
        if (document.getElementById('ag-desktop-custom-styles')) return;
        const style = document.createElement('style');
        style.id = 'ag-desktop-custom-styles';
        style.textContent = `
            /* Hide legacy full-width bottom bar */
            #ag-desktop-bar { display: none !important; }

            /* Swarm Status Pill: Mounted strictly ABOVE #ag-status-pill in bottom footer */
            #ag-swarm-pill {
                display: inline-flex !important;
                align-items: center !important;
                gap: 5px !important;
                height: 24px !important;
                padding: 2px 6px !important;
                margin-left: 10px !important;
                margin-bottom: 2px !important;
                flex-shrink: 0 !important;
                visibility: visible !important;
                opacity: 1 !important;
                background: transparent !important;
                border: none !important;
                border-radius: 4px !important;
                color: #cccccc !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
                font-size: 12px !important;
                cursor: pointer !important;
                user-select: none !important;
                white-space: nowrap !important;
                overflow: hidden !important;
                text-overflow: ellipsis !important;
                max-width: 240px !important;
                transition: background 0.15s ease, color 0.15s ease !important;
                box-sizing: border-box !important;
            }
            #ag-swarm-pill:hover {
                background: rgba(255, 255, 255, 0.08) !important;
                color: #ffffff !important;
            }
            #ag-swarm-pill.ag-swarm-done {
                color: #4ade80 !important;
            }

            /* Status Pill: Transparent background, clean spacing from Settings gear icon */
            #ag-status-pill {
                display: inline-flex !important;
                align-items: center !important;
                gap: 4px !important;
                height: 24px !important;
                padding: 2px 4px !important;
                margin-left: 6px !important;
                flex-shrink: 0 !important;
                visibility: visible !important;
                opacity: 1 !important;
                background: transparent !important;
                border: none !important;
                border-radius: 4px !important;
                color: #cccccc !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
                font-size: 11.5px !important;
                cursor: pointer !important;
                user-select: none !important;
                white-space: nowrap !important;
                transition: background 0.15s ease !important;
                box-sizing: border-box !important;
            }
            #ag-status-pill:hover {
                background: rgba(255, 255, 255, 0.08) !important;
                color: #ffffff !important;
            }
            #ag-status-pill.ag-offline .ag-pill-loc {
                color: #f87171 !important;
            }
            .ag-pill-quotas {
                font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace !important;
                font-size: 11px !important;
                color: #888888 !important;
                display: inline-flex !important;
                align-items: center !important;
                gap: 3px !important;
            }
            .ag-sym-claude {
                color: #e26938 !important;
                display: inline-flex !important;
                align-items: center !important;
                line-height: 1 !important;
            }
            .ag-sym-gemini {
                color: #60a5fa !important;
                display: inline-flex !important;
                align-items: center !important;
                line-height: 1 !important;
            }
            .ag-pct-num {
                color: #d4d4d4 !important;
            }
            .ag-pill-loc {
                color: #888888 !important;
                font-size: 11px !important;
                display: inline-flex !important;
                align-items: center !important;
                gap: 2px !important;
                margin-left: 3px !important;
            }

            /* Tooltip Card (1:1 with Antigravity IDE) */
            #ag-ide-tooltip,
            #ag-swarm-tooltip {
                position: fixed !important;
                background: #1e1e1e !important;
                border: 1px solid #333333 !important;
                border-radius: 6px !important;
                padding: 10px 14px !important;
                box-shadow: 0 8px 24px rgba(0,0,0,0.75) !important;
                z-index: 2147483647 !important;
                display: none;
                flex-direction: column !important;
                gap: 4px !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
                font-size: 11.5px !important;
                color: #d4d4d4 !important;
                line-height: 1.5 !important;
                pointer-events: none !important;
                min-width: 320px !important;
                max-width: 540px !important;
            }
            .ag-tt-title {
                font-size: 12.5px !important;
                font-weight: 700 !important;
                color: #ffffff !important;
                margin-bottom: 4px !important;
            }
            .ag-tt-line {
                display: flex !important;
                align-items: center !important;
                gap: 6px !important;
                white-space: nowrap !important;
            }
            .ag-tt-subline {
                display: flex !important;
                align-items: center !important;
                gap: 6px !important;
                padding-left: 14px !important;
                white-space: nowrap !important;
            }
            #ag-swarm-tooltip .ag-tt-line,
            #ag-swarm-tooltip .ag-tt-subline {
                display: block !important;
                white-space: normal !important;
                word-break: break-word !important;
            }
            #ag-swarm-tooltip .ag-tt-subline {
                padding-left: 12px !important;
            }
            .ag-avatar-letter {
                display: inline-flex !important;
                align-items: center !important;
                justify-content: center !important;
                width: 16px !important;
                height: 16px !important;
                border-radius: 50% !important;
                color: #ffffff !important;
                font-size: 10px !important;
                font-weight: bold !important;
                flex-shrink: 0 !important;
            }
            .ag-avatar-img {
                display: inline-block !important;
                border-radius: 50% !important;
                object-fit: cover !important;
                flex-shrink: 0 !important;
                border: 1px solid rgba(255, 255, 255, 0.14) !important;
            }
            .ag-avatar-hidden {
                display: none !important;
            }
            .ag-acc-count-badge {
                display: inline-flex !important;
                align-items: center !important;
                padding: 2px 7px !important;
                border-radius: 10px !important;
                background: rgba(96, 165, 250, 0.14) !important;
                border: 1px solid rgba(96, 165, 250, 0.3) !important;
                color: #93c5fd !important;
                font-size: 11px !important;
                font-weight: 600 !important;
                flex-shrink: 0 !important;
            }
            .ag-tt-tier {
                font-size: 9.5px !important;
                font-weight: 600 !important;
                padding: 1px 4px !important;
                border-radius: 3px !important;
                background: #252525 !important;
                border: 1px solid #383838 !important;
                color: #a0a0a0 !important;
            }
            .ag-tt-muted {
                color: #888888 !important;
                font-size: 10.5px !important;
            }
            .ag-tt-footer {
                margin-top: 6px !important;
                padding-top: 6px !important;
                border-top: 1px solid #282828 !important;
                font-style: italic !important;
                color: #777777 !important;
                font-size: 10.5px !important;
            }

            /* QuickPick Modal (1:1 with Antigravity IDE) */
            #ag-qp-backdrop {
                position: fixed !important;
                top: 0 !important;
                left: 0 !important;
                right: 0 !important;
                bottom: 0 !important;
                background: rgba(0,0,0,0.4) !important;
                z-index: 2147483646 !important;
                display: none;
            }
            #ag-qp-container {
                position: fixed !important;
                top: 36px !important;
                left: 50% !important;
                transform: translateX(-50%) !important;
                width: 580px !important;
                max-width: 94vw !important;
                background: #1e1e1e !important;
                border: 1px solid #007acc !important;
                border-radius: 6px !important;
                box-shadow: 0 10px 36px rgba(0,0,0,0.8) !important;
                z-index: 2147483647 !important;
                display: none;
                flex-direction: column !important;
                overflow: hidden !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
            }
            #ag-qp-input-wrapper {
                padding: 6px !important;
                background: #181818 !important;
                border-bottom: 1px solid #282828 !important;
            }
            #ag-qp-input {
                width: 100% !important;
                background: #141414 !important;
                border: 1px solid #2d2d2d !important;
                border-radius: 3px !important;
                color: #ffffff !important;
                padding: 6px 10px !important;
                font-size: 12px !important;
                outline: none !important;
                box-sizing: border-box !important;
            }
            #ag-qp-input:focus {
                border-color: #007acc !important;
            }
            #ag-qp-items-list {
                max-height: 340px !important;
                overflow-y: auto !important;
                padding: 4px !important;
            }
            .ag-qp-row {
                display: flex !important;
                align-items: center !important;
                justify-content: space-between !important;
                padding: 7px 10px !important;
                border-radius: 4px !important;
                cursor: pointer !important;
                font-size: 11.5px !important;
                color: #cccccc !important;
                transition: background 0.1s ease !important;
                margin-bottom: 1px !important;
            }
            .ag-qp-row:hover, .ag-qp-row.selected {
                background: #04395e !important;
                color: #ffffff !important;
            }
            .ag-qp-row-left {
                display: flex !important;
                align-items: center !important;
                gap: 8px !important;
                overflow: hidden !important;
            }
            .ag-qp-row-title {
                font-weight: 500 !important;
                color: #ffffff !important;
            }
            .ag-qp-row-desc {
                font-size: 10.5px !important;
                color: #888888 !important;
                margin-left: 6px !important;
            }
            .ag-qp-row:hover .ag-qp-row-desc, .ag-qp-row.selected .ag-qp-row-desc {
                color: #cccccc !important;
            }
            .ag-active-badge {
                font-size: 9.5px !important;
                color: #4ec9b0 !important;
                background: #162d27 !important;
                border: 1px solid #235848 !important;
                border-radius: 3px !important;
                padding: 1px 5px !important;
                margin-left: 6px !important;
            }

            /* Rich Modal Dialog on "Проверить статус квот" (1:1 with Image 4) */
            #ag-quota-modal-backdrop {
                position: fixed !important;
                top: 0 !important;
                left: 0 !important;
                right: 0 !important;
                bottom: 0 !important;
                background: rgba(0,0,0,0.6) !important;
                z-index: 2147483648 !important;
                display: none;
                align-items: center !important;
                justify-content: center !important;
            }
            #ag-quota-modal-box {
                position: relative !important;
                width: 480px !important;
                max-width: 90vw !important;
                background: #1e1e1e !important;
                border: 1px solid #333333 !important;
                border-radius: 8px !important;
                box-shadow: 0 16px 48px rgba(0,0,0,0.85) !important;
                padding: 24px 28px !important;
                color: #cccccc !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
                font-size: 12px !important;
                line-height: 1.6 !important;
                display: flex !important;
                gap: 18px !important;
                box-sizing: border-box !important;
            }
            .ag-dialog-icon {
                flex-shrink: 0 !important;
                width: 38px !important;
                height: 38px !important;
                border-radius: 50% !important;
                border: 2px solid #007acc !important;
                display: flex !important;
                align-items: center !important;
                justify-content: center !important;
                color: #007acc !important;
                font-family: serif !important;
                font-size: 22px !important;
                font-style: italic !important;
                font-weight: bold !important;
            }
            .ag-dialog-content {
                flex-grow: 1 !important;
                display: flex !important;
                flex-direction: column !important;
                gap: 8px !important;
            }
            .ag-dialog-close {
                position: absolute !important;
                top: 14px !important;
                right: 16px !important;
                color: #888888 !important;
                font-size: 16px !important;
                cursor: pointer !important;
                line-height: 1 !important;
            }
            .ag-dialog-close:hover {
                color: #ffffff !important;
            }
            .ag-dialog-ok-btn {
                align-self: flex-end !important;
                margin-top: 10px !important;
                background: #007acc !important;
                color: #ffffff !important;
                border: 1px solid #0098ff !important;
                border-radius: 4px !important;
                padding: 5px 22px !important;
                font-size: 12px !important;
                font-weight: 500 !important;
                cursor: pointer !important;
                outline: none !important;
                transition: background 0.12s ease !important;
            }
            .ag-dialog-ok-btn:hover {
                background: #0062a3 !important;
            }
            #ag-desktop-toast {
                position: fixed !important;
                right: 16px !important;
                bottom: 38px !important;
                z-index: 2147483649 !important;
                background: #1e1e1e !important;
                border: 1px solid #007acc !important;
                border-radius: 6px !important;
                padding: 10px 14px !important;
                color: #e5e7eb !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
                font-size: 12px !important;
                line-height: 1.45 !important;
                box-shadow: 0 8px 24px rgba(0,0,0,0.75) !important;
                display: none;
                max-width: 360px !important;
                pointer-events: none !important;
            }
            #ag-desktop-toast.ag-toast-error {
                border-color: #dc2626 !important;
            }
            #ag-autofix-popup {
                position: fixed !important;
                right: 16px !important;
                bottom: 42px !important;
                z-index: 2147483650 !important;
                width: 344px !important;
                max-width: calc(100vw - 32px) !important;
                background: rgba(24, 24, 27, 0.96) !important;
                backdrop-filter: blur(14px) !important;
                border: 1px solid rgba(255, 255, 255, 0.14) !important;
                border-radius: 10px !important;
                padding: 12px 14px !important;
                color: #f4f4f5 !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
                font-size: 12px !important;
                line-height: 1.45 !important;
                box-shadow: 0 14px 36px rgba(0, 0, 0, 0.72) !important;
                display: none;
            }
            #ag-autofix-popup.ag-autofix-err {
                border-color: rgba(248, 113, 113, 0.45) !important;
            }
            #ag-autofix-popup.ag-autofix-ok {
                border-color: rgba(52, 211, 153, 0.48) !important;
            }
            .ag-af-head {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 8px;
                margin-bottom: 6px;
            }
            .ag-af-title-wrap {
                display: flex;
                align-items: center;
                gap: 6px;
                font-weight: 600;
                font-size: 12px;
                color: #ffffff;
                min-width: 0;
            }
            .ag-af-controls {
                display: flex;
                align-items: center;
                gap: 6px;
                flex-shrink: 0;
            }
            .ag-af-auto-pill {
                background: rgba(52, 211, 153, 0.16);
                color: #6ee7b7;
                border: 1px solid rgba(52, 211, 153, 0.35);
                border-radius: 999px;
                padding: 2px 8px;
                font-size: 10.5px;
                font-weight: 600;
                cursor: pointer;
                outline: none;
            }
            .ag-af-auto-pill.ag-af-off {
                background: rgba(255, 255, 255, 0.06);
                color: #a1a1aa;
                border-color: rgba(255, 255, 255, 0.14);
            }
            .ag-af-close {
                background: transparent;
                border: none;
                color: #a1a1aa;
                font-size: 15px;
                line-height: 1;
                cursor: pointer;
                padding: 2px 4px;
            }
            .ag-af-close:hover {
                color: #ffffff;
            }
            .ag-af-err-title {
                font-weight: 600;
                color: #fca5a5;
                margin-bottom: 3px;
            }
            #ag-autofix-popup.ag-autofix-ok .ag-af-err-title {
                color: #6ee7b7;
            }
            .ag-af-desc {
                color: #d4d4d8;
                font-size: 11.5px;
                margin-bottom: 8px;
            }
            .ag-af-status {
                color: #93c5fd;
                font-size: 11px;
                margin-bottom: 8px;
            }
            .ag-af-actions {
                display: flex;
                align-items: center;
                justify-content: flex-end;
                gap: 8px;
            }
            .ag-af-btn-fix {
                background: #ffffff;
                color: #09090b;
                border: none;
                border-radius: 6px;
                padding: 5px 12px;
                font-size: 11.5px;
                font-weight: 600;
                cursor: pointer;
                transition: opacity 0.12s ease;
            }
            .ag-af-btn-fix:hover {
                opacity: 0.9;
            }
            .ag-af-btn-fix:disabled {
                opacity: 0.5;
                cursor: default;
            }
            .ag-pill-swarm {
                color: #c4b5fd;
                font-size: 11px;
                font-weight: 500;
                margin-left: 2px;
            }
            .ag-swarm-status-badge {
                font-size: 10px;
                padding: 1px 6px;
                border-radius: 3px;
                font-weight: 600;
                flex-shrink: 0;
                border: 1px solid transparent;
            }
            .ag-swarm-status-running {
                background: rgba(56, 189, 248, 0.14);
                color: #38bdf8;
                border-color: rgba(56, 189, 248, 0.35);
            }
            .ag-swarm-status-done {
                background: rgba(52, 211, 153, 0.14);
                color: #34d399;
                border-color: rgba(52, 211, 153, 0.35);
            }
            .ag-swarm-status-err {
                background: rgba(248, 113, 113, 0.16);
                color: #f87171;
                border-color: rgba(248, 113, 113, 0.38);
            }
            .ag-qp-del-btn {
                background: transparent;
                border: 1px solid #3c3c3c;
                color: #9ca3af;
                border-radius: 3px;
                padding: 2px 6px;
                font-size: 11px;
                cursor: pointer;
                line-height: 1.2;
                margin-left: 6px;
                flex-shrink: 0;
            }
            .ag-qp-del-btn:hover {
                background: rgba(220, 38, 38, 0.2);
                border-color: #ef4444;
                color: #fca5a5;
            }
            #ag-swarm-modal-backdrop {
                position: fixed;
                inset: 0;
                background: rgba(0, 0, 0, 0.62);
                z-index: 2147483647;
                display: none;
                align-items: center;
                justify-content: center;
            }
            #ag-swarm-modal-box {
                background: #252526;
                color: #d4d4d4;
                border: 1px solid #454545;
                border-radius: 6px;
                padding: 18px 20px;
                width: 620px;
                max-width: calc(100vw - 32px);
                max-height: 86vh;
                overflow-y: auto;
                box-shadow: 0 12px 32px rgba(0, 0, 0, 0.68);
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                font-size: 13px;
                position: relative;
                display: flex;
                flex-direction: column;
                gap: 10px;
            }
            .ag-swarm-modal-title {
                font-size: 14px;
                font-weight: 600;
                color: #f3f4f6;
                display: flex;
                align-items: center;
                gap: 8px;
                padding-right: 22px;
            }
            .ag-swarm-field-label {
                display: block;
                font-size: 11.5px;
                color: #9ca3af;
                margin-bottom: 4px;
                font-weight: 500;
            }
            .ag-swarm-row {
                display: flex;
                gap: 6px;
                align-items: center;
            }
            .ag-swarm-input, .ag-swarm-select, .ag-swarm-textarea {
                width: 100%;
                box-sizing: border-box;
                background: #1e1e1e;
                color: #e5e7eb;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 6px 9px;
                font-size: 12.5px;
                font-family: inherit;
                outline: none;
            }
            .ag-swarm-input:focus, .ag-swarm-select:focus, .ag-swarm-textarea:focus {
                border-color: #7c3aed;
            }
            .ag-swarm-textarea {
                resize: vertical;
                min-height: 78px;
                line-height: 1.4;
            }
            .ag-swarm-checkbox-row {
                display: flex;
                align-items: center;
                gap: 7px;
                font-size: 12px;
                color: #cbd5e1;
                cursor: pointer;
                user-select: none;
            }
            .ag-swarm-modal-error {
                color: #f87171;
                font-size: 12px;
                min-height: 16px;
                display: none;
            }
            .ag-swarm-btn-row {
                display: flex;
                justify-content: flex-end;
                gap: 8px;
                margin-top: 4px;
            }
            .ag-swarm-btn {
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px 14px;
                font-size: 12.5px;
                font-weight: 500;
                cursor: pointer;
                color: #e5e7eb;
                background: #2d2d2d;
            }
            .ag-swarm-btn:hover {
                background: #383838;
            }
            .ag-swarm-btn-primary {
                background: #6d28d9;
                border-color: #7c3aed;
                color: #ffffff;
            }
            .ag-swarm-btn-primary:hover {
                background: #7c3aed;
            }
            .ag-swarm-btn:disabled {
                opacity: 0.55;
                cursor: default;
            }
            .ag-swarm-section-head {
                display: flex;
                align-items: center;
                justify-content: space-between;
                font-size: 12px;
                font-weight: 600;
                color: #e5e7eb;
                margin-top: 2px;
            }
            .ag-swarm-tasks-wrap {
                display: flex;
                flex-direction: column;
                gap: 6px;
                max-height: 220px;
                overflow-y: auto;
                padding-right: 2px;
            }
            .ag-swarm-task-card {
                background: #1e1e1e;
                border: 1px solid #3c3c3c;
                border-radius: 5px;
                padding: 8px 10px;
                display: flex;
                flex-direction: column;
                gap: 4px;
            }
            .ag-swarm-task-top {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 8px;
            }
            .ag-swarm-task-meta {
                font-size: 11.5px;
                color: #9ca3af;
                line-height: 1.35;
            }
            .ag-swarm-workers-list {
                display: flex;
                flex-direction: column;
                gap: 2px;
                margin-top: 2px;
                padding-left: 6px;
                border-left: 2px solid #3c3c3c;
            }
            .ag-swarm-worker-row {
                font-size: 11px;
                color: #cbd5e1;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .ag-swarm-card-actions {
                display: flex;
                gap: 6px;
                justify-content: flex-end;
                margin-top: 2px;
            }
            .ag-swarm-btn-sm {
                padding: 2px 9px;
                font-size: 11px;
            }
            .ag-swarm-hint {
                font-size: 11px;
                color: #9ca3af;
                line-height: 1.35;
            }
            .ag-swarm-chat-tip {
                background: rgba(124, 58, 237, 0.08);
                border: 1px solid rgba(124, 58, 237, 0.28);
                border-radius: 4px;
                padding: 6px 9px;
                font-size: 11.5px;
                color: #d1d5db;
                line-height: 1.4;
            }
            .ag-swarm-divider {
                border-top: 1px solid #383838;
                margin: 4px 0;
            }
        `;
        const styleTarget = document.head || document.documentElement;
        if (styleTarget) {
            styleTarget.appendChild(style);
        }
    }

    function formatResetText(resetTimeStr, pct) {
        if (pct === 100) return '(доступно 100%)';
        if (!resetTimeStr) return '';
        try {
            const target = new Date(resetTimeStr);
            const diff = target.getTime() - Date.now();
            if (diff <= 0) return (pct !== undefined && pct < 100) ? '(ожидает обновления)' : '(доступно 100%)';
            const totalHours = Math.floor(diff / (1000 * 60 * 60));
            const days = Math.floor(totalHours / 24);
            const remHours = totalHours % 24;

            const weekdays = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
            const months = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
            const wday = weekdays[target.getDay()];
            const mday = target.getDate();
            const mon = months[target.getMonth()];
            const hh = String(target.getHours()).padStart(2, '0');
            const mm = String(target.getMinutes()).padStart(2, '0');
            const dateSuffix = `(${wday}, ${mday} ${mon} в ${hh}:${mm})`;

            if (days > 0) {
                return `сброс через ${days} дн. ${remHours} ч. ${dateSuffix}`;
            }
            const mins = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
            return `сброс через ${totalHours} ч. ${mins} мин. ${dateSuffix}`;
        } catch (_) {
            return '';
        }
    }

    const IGNORED_UI_MODEL_RE = /^(?:refresh|try again|unavailable|retry|reload)$/i;
    const IGNORED_UI_MODEL_WORD_RE = /\b(?:refresh|try again|unavailable)\b/i;

    function isIgnoredUiModelLabel(rawText) {
        const clean = String(rawText || '').replace(/\s+/g, ' ').trim();
        if (!clean) return true;
        return IGNORED_UI_MODEL_RE.test(clean) || IGNORED_UI_MODEL_WORD_RE.test(clean);
    }

    function parseActiveUiModelText(rawText) {
        const clean = String(rawText || '').replace(/\s+/g, ' ').trim();
        if (!clean || clean.length > 80 || isIgnoredUiModelLabel(clean)) return null;
        const low = clean.toLowerCase();
        if (low.includes('argon')) {
            return { raw: clean, displayName: clean, family: 'argon', shortLabel: 'Argon' };
        }
        if (low.includes('carbon')) {
            return { raw: clean, displayName: clean, family: 'gemini_models', shortLabel: 'Carbon' };
        }
        if (low.includes('hydrogen')) {
            return { raw: clean, displayName: clean, family: 'gemini_models', shortLabel: 'Hydrogen' };
        }
        if (low.includes('gemini')) {
            let shortLabel = 'Gemini';
            if (low.includes('flash')) shortLabel = 'Flash';
            else if (/\bpro\b/.test(low)) shortLabel = 'Pro';
            return { raw: clean, displayName: clean, family: 'gemini_models', shortLabel };
        }
        if (low.includes('claude')) {
            return { raw: clean, displayName: clean, family: 'claude', shortLabel: 'Claude' };
        }
        if (low.includes('gpt-oss') || /\bgpt\b/.test(low)) {
            return { raw: clean, displayName: clean, family: 'gpt', shortLabel: 'GPT' };
        }
        return null;
    }

    let _cachedUiModelAt = 0;
    let _cachedUiModel = null;
    let _overrideUiModel = undefined;
    let _uiModelListenerAttached = false;

    function detectActiveUiModel(forceScan = false) {
        if (_overrideUiModel !== undefined) {
            return _overrideUiModel ? parseActiveUiModelText(_overrideUiModel) : null;
        }
        const now = Date.now();
        if (!forceScan && _cachedUiModelAt > 0 && (now - _cachedUiModelAt) < 600) {
            return _cachedUiModel;
        }
        _cachedUiModelAt = now;
        try {
            if (typeof document === 'undefined' || !document.querySelectorAll) {
                _cachedUiModel = null;
                return null;
            }
            if (!_uiModelListenerAttached && typeof document.addEventListener === 'function') {
                _uiModelListenerAttached = true;
                const scheduleRescan = (ev) => {
                    if (ev && ev.target && isInsideOwnOverlay(ev.target)) return;
                    setTimeout(() => {
                        const prevRaw = _cachedUiModel ? _cachedUiModel.raw : '';
                        const next = detectActiveUiModel(true);
                        const nextRaw = next ? next.raw : '';
                        if (nextRaw !== prevRaw) {
                            renderPillContent();
                            const tt = document.getElementById('ag-ide-tooltip');
                            if (tt && tt.style.display !== 'none') renderTooltipContent(tt);
                        }
                    }, 160);
                };
                document.addEventListener('click', scheduleRescan, true);
            }

            const useGeom = hasLayoutGeometry();
            const vh = useGeom ? (window.innerHeight || 0) : 0;
            const els = document.querySelectorAll('button, [role="button"], [aria-haspopup], [class*="model"]');
            let best = null;
            let bestScore = -1;
            for (const el of els) {
                if (!el || isInsideOwnOverlay(el)) continue;
                const pid = el.id || '';
                if (pid.startsWith('ag-')) continue;
                const txt = String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                if (!txt || txt.length < 4 || txt.length > 68 || isIgnoredUiModelLabel(txt)) continue;
                const parsed = parseActiveUiModelText(txt);
                if (!parsed) continue;
                if (hasOwnHiddenStyle(el)) continue;
                let score = 1;
                if (/\((high|xhigh|medium|low|thinking|max)\)/i.test(txt)) score += 4;
                if (/argon|carbon|hydrogen|3\.8|3\.1|sonnet|opus|gpt-oss/i.test(txt)) score += 3;
                if (el.getAttribute && (el.getAttribute('aria-haspopup') || el.getAttribute('aria-expanded') !== null)) score += 2;
                if (useGeom && typeof el.getBoundingClientRect === 'function') {
                    const r = el.getBoundingClientRect();
                    if (!r || r.width < 4 || r.height < 4) continue;
                    if (r.top > vh * 0.45) score += 2;
                }
                if (score > bestScore) {
                    bestScore = score;
                    best = parsed;
                }
            }
            _cachedUiModel = best;
            return best;
        } catch (_) {
            _cachedUiModel = null;
            return null;
        }
    }

    function compactModelTag(shortLabel) {
        const s = String(shortLabel || '').trim().toLowerCase();
        if (!s) return '';
        if (s.startsWith('argon')) return 'A';
        if (s.startsWith('hydrogen')) return 'H';
        if (s.startsWith('carbon')) return 'C';
        if (s.startsWith('gemini')) return 'G';
        return s.charAt(0).toUpperCase();
    }

    function resolvePillGeminiView(info, activeModel) {
        const argonPct = info ? info.argonPct : null;
        const geminiPct = info ? info.geminiPct : 100;
        const hasArgon = argonPct !== null && argonPct !== undefined;
        if (activeModel && activeModel.family === 'argon') {
            if (hasArgon) {
                return { pct: argonPct, label: 'A', isArgon: true, hasSeparateArgon: true };
            }
            return { pct: null, label: 'A', isArgon: true, hasSeparateArgon: false };
        }
        if (activeModel && activeModel.family === 'gemini_models') {
            const tag = compactModelTag(activeModel.shortLabel);
            const lbl = (hasArgon || (tag && tag !== 'G')) ? (tag || 'G') : '';
            return { pct: geminiPct, label: lbl, isArgon: false, hasSeparateArgon: hasArgon };
        }
        return { pct: geminiPct, label: '', isArgon: false, hasSeparateArgon: hasArgon };
    }

    function getLimitingQuotaInfo(acc) {
        const q = (acc && acc.quota) || null;
        let claudePct = null;
        let claudeReset = null;
        let claudeWeeklyPct = (acc && acc.claude_weekly_percentage !== undefined) ? acc.claude_weekly_percentage : null;
        let claudeWeeklyReset = (acc && acc.claude_weekly_reset_time) || null;
        let claude5hPct = (acc && acc.claude_5h_percentage !== undefined) ? acc.claude_5h_percentage : null;
        let claude5hReset = (acc && acc.claude_5h_reset_time) || null;
        let geminiPct = null;
        let geminiReset = null;
        let geminiWeeklyPct = (acc && acc.gemini_weekly_percentage !== undefined) ? acc.gemini_weekly_percentage : null;
        let geminiWeeklyReset = (acc && acc.gemini_weekly_reset_time) || null;
        let gemini5hPct = (acc && acc.gemini_5h_percentage !== undefined) ? acc.gemini_5h_percentage : null;
        let gemini5hReset = (acc && acc.gemini_5h_reset_time) || null;
        let argonPct = (acc && acc.argon_percentage !== undefined && acc.argon_percentage !== null) ? Number(acc.argon_percentage) : null;
        let argonReset = (acc && acc.argon_reset_time) || null;
        let argonWeeklyPct = (acc && acc.argon_weekly_percentage !== undefined) ? acc.argon_weekly_percentage : null;
        let argonWeeklyReset = (acc && acc.argon_weekly_reset_time) || null;
        let argon5hPct = (acc && acc.argon_5h_percentage !== undefined) ? acc.argon_5h_percentage : null;
        let argon5hReset = (acc && acc.argon_5h_reset_time) || null;
        const geminiGroups = [];

        if (q && Array.isArray(q.quota_groups)) {
            for (const g of q.quota_groups) {
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

                for (const b of buckets) {
                    if (!b || typeof b !== 'object') continue;
                    const rf = b.remainingFraction !== undefined ? b.remainingFraction : b.remaining_fraction;
                    if (rf === undefined || rf === null) continue;
                    const rfNum = Number(rf);
                    if (!Number.isFinite(rfNum)) continue;
                    const pctVal = Math.max(0, Math.min(100, Math.round(rfNum * 100)));
                    const resetVal = b.resetTime || b.reset_time || null;
                    const bid = String(b.bucketId || b.bucket_id || b.window || '').toLowerCase();
                    const winLow = String(b.window || '').toLowerCase();

                    if (winLow === 'weekly' || bid.endsWith('-weekly') || bid === 'weekly') {
                        if (!b.disabled || weeklyPct === null) {
                            weeklyPct = pctVal;
                            weeklyReset = resetVal;
                        }
                    } else if (winLow === '5h' || bid.endsWith('-5h') || bid === '5h') {
                        if (!b.disabled) {
                            h5Pct = pctVal;
                            h5Reset = resetVal;
                        }
                    }

                    const entry = { rf: rfNum, pct: pctVal, reset: resetVal };
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
                        displayName: dname || (isArgon ? 'Gemini 4 Argon' : 'Gemini Models'),
                        pct: primaryPct,
                        reset: primaryReset,
                        weeklyPct,
                        weeklyReset,
                        h5Pct,
                        h5Reset,
                        isArgon,
                        isMain: isMainGem
                    });
                    if (isArgon) {
                        argonPct = primaryPct;
                        argonReset = primaryReset;
                        argonWeeklyPct = weeklyPct;
                        argonWeeklyReset = weeklyReset;
                        argon5hPct = h5Pct;
                        argon5hReset = h5Reset;
                    } else if (geminiPct === null || isMainGem) {
                        geminiPct = primaryPct;
                        geminiReset = primaryReset;
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

        if (q && Array.isArray(q.models)) {
            const gemCands = [];
            const argonCands = [];
            const claudeCands = [];
            for (const m of q.models) {
                const mname = (m.name || '').toLowerCase();
                const dname = (m.display_name || '').toLowerCase();
                if (m.percentage === undefined || m.percentage === null) continue;
                if (mname.includes('2.5') || dname.includes('2.5') ||
                    mname.includes('flash-lite') || mname.includes('flash_lite') ||
                    mname.includes('-low') || mname.includes('-medium')) {
                    continue;
                }
                if (mname.includes('argon') || dname.includes('argon')) {
                    argonCands.push({ pct: Number(m.percentage), reset: m.reset_time || null });
                } else if (mname.includes('gemini')) {
                    gemCands.push({ pct: Number(m.percentage), reset: m.reset_time || null });
                } else if (mname.includes('claude')) {
                    claudeCands.push({ pct: Number(m.percentage), reset: m.reset_time || null });
                }
            }
            if (geminiPct === null && gemCands.length > 0) {
                gemCands.sort((a, b) => a.pct - b.pct);
                geminiPct = gemCands[0].pct;
                geminiReset = gemCands[0].reset;
            }
            if (argonPct === null && argonCands.length > 0) {
                argonCands.sort((a, b) => a.pct - b.pct);
                argonPct = argonCands[0].pct;
                argonReset = argonCands[0].reset;
            }
            if (claudePct === null && claudeCands.length > 0) {
                claudeCands.sort((a, b) => a.pct - b.pct);
                claudePct = claudeCands[0].pct;
                claudeReset = claudeCands[0].reset;
            }
        }

        if (claudePct === null) {
            if (q && q.claude_percentage !== undefined && q.claude_percentage !== null) claudePct = q.claude_percentage;
            else if (acc && acc.claude_percentage !== undefined && acc.claude_percentage !== null) claudePct = acc.claude_percentage;
        }
        if (!claudeReset) {
            claudeReset = (acc && acc.claude_reset_time) || (q && q.claude_reset_time) || null;
        }
        if (geminiPct === null) {
            if (q && q.gemini_percentage !== undefined && q.gemini_percentage !== null) geminiPct = q.gemini_percentage;
            else if (acc && acc.gemini_percentage !== undefined && acc.gemini_percentage !== null) geminiPct = acc.gemini_percentage;
        }
        if (!geminiReset) {
            geminiReset = (acc && acc.gemini_reset_time) || (q && q.gemini_reset_time) || null;
        }
        if (argonPct === null) {
            if (q && q.argon_percentage !== undefined && q.argon_percentage !== null) argonPct = Number(q.argon_percentage);
            else if (acc && acc.argon_percentage !== undefined && acc.argon_percentage !== null) argonPct = Number(acc.argon_percentage);
        }
        if (!argonReset) {
            argonReset = (acc && acc.argon_reset_time) || (q && q.argon_reset_time) || null;
        }
        if (geminiPct === null && argonPct !== null) {
            geminiPct = argonPct;
            geminiReset = argonReset;
            geminiWeeklyPct = argonWeeklyPct;
            geminiWeeklyReset = argonWeeklyReset;
            gemini5hPct = argon5hPct;
            gemini5hReset = argon5hReset;
        }
        if (argonPct !== null && !geminiGroups.some((g) => g.isArgon)) {
            geminiGroups.push({
                displayName: 'Gemini 4 Argon',
                pct: argonPct,
                reset: argonReset,
                weeklyPct: argonWeeklyPct,
                weeklyReset: argonWeeklyReset,
                h5Pct: argon5hPct,
                h5Reset: argon5hReset,
                isArgon: true,
                isMain: false
            });
        }

        return {
            claudePct: claudePct !== null ? claudePct : null,
            claudeReset,
            claudeWeeklyPct,
            claudeWeeklyReset,
            claude5hPct,
            claude5hReset,
            geminiPct: geminiPct !== null ? geminiPct : 100,
            geminiReset,
            geminiWeeklyPct,
            geminiWeeklyReset,
            gemini5hPct,
            gemini5hReset,
            argonPct,
            argonReset,
            argonWeeklyPct,
            argonWeeklyReset,
            argon5hPct,
            argon5hReset,
            geminiGroups
        };
    }

    function hasLayoutGeometry() {
        try {
            if (typeof window === 'undefined' || !(window.innerWidth > 0 && window.innerHeight > 0)) {
                return false;
            }
            const ref = document.body || document.documentElement;
            if (!ref || typeof ref.getBoundingClientRect !== 'function') {
                return false;
            }
            const r = ref.getBoundingClientRect();
            return Boolean(r && r.width > 0 && r.height > 0);
        } catch (_) {
            return false;
        }
    }

    function isInsideOwnOverlay(el) {
        let cur = (el && el.nodeType === 1) ? el : (el ? el.parentElement : null);
        while (cur && cur.nodeType === 1) {
            if (cur.id && cur.id.startsWith('ag-') && cur.id !== 'ag-desktop-bar') {
                return true;
            }
            cur = cur.parentElement;
        }
        return false;
    }

    function hasOwnHiddenStyle(el) {
        if (!el || el.hidden === true) return true;
        if (el.style) {
            if (el.style.display === 'none' || el.style.visibility === 'hidden') return true;
            if (el.style.opacity !== '' && parseFloat(el.style.opacity) === 0) return true;
        }
        if (typeof window.getComputedStyle === 'function') {
            const cs = window.getComputedStyle(el);
            if (cs && (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0)) {
                return true;
            }
        }
        return false;
    }

    function isElementVisible(el) {
        if (!el || !el.isConnected) return false;
        const useGeometry = hasLayoutGeometry();
        let cur = el;
        while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
            if (cur.hidden === true) return false;
            if (cur.style) {
                if (cur.style.display === 'none' || cur.style.visibility === 'hidden') return false;
                if (cur.style.opacity !== '' && parseFloat(cur.style.opacity) === 0) return false;
            }
            if (typeof window.getComputedStyle === 'function') {
                const cs = window.getComputedStyle(cur);
                if (cs) {
                    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) {
                        return false;
                    }
                    if (useGeometry && cur !== el && cur !== document.body &&
                        (cs.overflow === 'hidden' || cs.overflowX === 'hidden' || cs.overflowY === 'hidden' || cs.overflow === 'clip')) {
                        const cr = typeof cur.getBoundingClientRect === 'function' ? cur.getBoundingClientRect() : null;
                        if (!cr || cr.width < 4 || cr.height < 4) {
                            return false;
                        }
                    }
                }
            }
            cur = cur.parentElement;
        }

        if (useGeometry) {
            if (typeof el.getClientRects === 'function' && el.getClientRects().length === 0) {
                return false;
            }
            if (typeof el.getBoundingClientRect === 'function') {
                const rect = el.getBoundingClientRect();
                if (!rect || rect.width < 4 || rect.height < 4) {
                    return false;
                }
                if (rect.right <= 0 || rect.left >= window.innerWidth || rect.bottom <= 0 || rect.top >= window.innerHeight) {
                    return false;
                }
            }
        }
        return true;
    }

    function isPillVisibleInViewport(pill) {
        if (!pill || !pill.isConnected) return false;
        const useGeometry = hasLayoutGeometry();
        let visLeft = 0;
        let visRight = useGeometry ? window.innerWidth : 0;
        let visTop = 0;
        let visBottom = useGeometry ? window.innerHeight : 0;

        // Single ancestor pass combining display/visibility checks + overflow viewport clipping
        let cur = pill;
        while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
            if (cur.hidden === true) return false;
            if (cur.style) {
                if (cur.style.display === 'none' || cur.style.visibility === 'hidden') return false;
                if (cur.style.opacity !== '' && parseFloat(cur.style.opacity) === 0) return false;
            }
            if (typeof window.getComputedStyle === 'function') {
                const cs = window.getComputedStyle(cur);
                if (cs) {
                    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) {
                        return false;
                    }
                    if (useGeometry && cur !== pill && cur !== document.body &&
                        (cs.overflow === 'hidden' || cs.overflowX === 'hidden' || cs.overflowY === 'hidden' || cs.overflow === 'clip')) {
                        const ar = typeof cur.getBoundingClientRect === 'function' ? cur.getBoundingClientRect() : null;
                        if (!ar || ar.width < 4 || ar.height < 4) {
                            return false;
                        }
                        visLeft = Math.max(visLeft, ar.left);
                        visRight = Math.min(visRight, ar.right);
                        visTop = Math.max(visTop, ar.top);
                        visBottom = Math.min(visBottom, ar.bottom);
                    }
                }
            }
            cur = cur.parentElement;
        }

        if (!useGeometry || typeof pill.getBoundingClientRect !== 'function') {
            return true;
        }
        if (typeof pill.getClientRects === 'function' && pill.getClientRects().length === 0) {
            return false;
        }
        const pr = pill.getBoundingClientRect();
        if (!pr || pr.width < 20 || pr.height < 12) return false;
        if (pr.right <= 0 || pr.left >= window.innerWidth || pr.bottom <= 0 || pr.top >= window.innerHeight) {
            return false;
        }

        visLeft = Math.max(visLeft, pr.left);
        visRight = Math.min(visRight, pr.right);
        visTop = Math.max(visTop, pr.top);
        visBottom = Math.min(visBottom, pr.bottom);

        const visW = visRight - visLeft;
        const visH = visBottom - visTop;
        return visW >= 24 && visH >= 12 && visW >= pr.width * 0.7;
    }

    function promoteInteractiveTarget(el) {
        if (!el || el.nodeType !== 1) return null;
        if (typeof el.matches === 'function' && el.matches('button, a, [role="button"]')) {
            return el;
        }
        if (typeof el.closest === 'function') {
            const interactive = el.closest('button, a, [role="button"]');
            if (interactive) return interactive;
        }
        if (el.tagName === 'DIV') return el;
        return el.parentElement || el;
    }

    function isRejectedTarget(el) {
        if (!rejectedDockUntil || !el) return false;
        const until = rejectedDockUntil.get(el);
        return Boolean(until && until > Date.now());
    }

    function findSettingsButton(excludedSet = null) {
        if (cachedSettingsBtn && (!excludedSet || !excludedSet.has(cachedSettingsBtn)) &&
            !isRejectedTarget(cachedSettingsBtn) &&
            cachedSettingsBtn.parentElement && isElementVisible(cachedSettingsBtn)) {
            return cachedSettingsBtn;
        }
        cachedSettingsBtn = null;

        const root = document.body || document.documentElement;
        if (!root) return null;

        const candidates = new Map();
        const addCandidate = (rawEl, baseScore) => {
            const target = promoteInteractiveTarget(rawEl);
            if (!target || !target.parentElement || isInsideOwnOverlay(target)) return;
            if ((excludedSet && excludedSet.has(target)) || isRejectedTarget(target)) return;
            if (!isElementVisible(target)) return;
            const prev = candidates.get(target) || -9999;
            if (baseScore > prev) candidates.set(target, baseScore);
        };

        // 1. Prefer exact text label ('Settings' / 'Настройки') on visible controls
        if (typeof document.createTreeWalker === 'function' && typeof NodeFilter !== 'undefined') {
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
            let node;
            while ((node = walker.nextNode())) {
                const txt = node.nodeValue ? node.nodeValue.trim() : '';
                if (txt === 'Settings' || txt === 'Настройки') {
                    const p = node.parentElement;
                    if (p && p.tagName !== 'SCRIPT' && p.tagName !== 'STYLE') {
                        addCandidate(p, 200);
                    }
                }
            }
        } else {
            const allInteractive = document.querySelectorAll('button, a, [role="button"], div');
            for (let i = 0; i < allInteractive.length; i++) {
                const el = allInteractive[i];
                const txt = el.textContent ? el.textContent.trim() : '';
                if (txt === 'Settings' || txt === 'Настройки') {
                    addCandidate(el, 200);
                }
            }
        }

        // 2. Attribute matches (promoted so we never dock inside an <svg> or inner span)
        const byAttr = document.querySelectorAll('[aria-label*="Settings"], [title*="Settings"], [aria-label*="Настройки"], [title*="Настройки"]');
        for (let i = 0; i < byAttr.length; i++) {
            const el = byAttr[i];
            const aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
            const title = (el.getAttribute && el.getAttribute('title')) || '';
            const exact = aria.trim() === 'Settings' || title.trim() === 'Settings' ||
                aria.trim() === 'Настройки' || title.trim() === 'Настройки';
            addCandidate(el, exact ? 140 : 100);
        }

        if (candidates.size === 0) return null;

        const useGeometry = hasLayoutGeometry();
        let best = null;
        let bestScore = 0;
        candidates.forEach((baseScore, cand) => {
            let score = baseScore;
            if (useGeometry && typeof cand.getBoundingClientRect === 'function') {
                const r = cand.getBoundingClientRect();
                if (r.top >= window.innerHeight * 0.5) {
                    score += 80;
                } else if (r.bottom < window.innerHeight * 0.35) {
                    score -= 40;
                }
                if (r.left <= window.innerWidth * 0.45) {
                    score += 60;
                }
                const parent = cand.parentElement;
                if (parent && typeof parent.getBoundingClientRect === 'function') {
                    const pr = parent.getBoundingClientRect();
                    if (pr.width >= 200) score += 40;
                    const pcs = typeof window.getComputedStyle === 'function' ? window.getComputedStyle(parent) : null;
                    if (pcs && (pcs.overflow === 'hidden' || pcs.overflowX === 'hidden' || pcs.overflow === 'clip') && pr.width < 120) {
                        score -= 150;
                    }
                }
            }
            if (score > bestScore) {
                bestScore = score;
                best = cand;
            }
        });

        if (best) {
            cachedSettingsBtn = best;
        }
        return best;
    }

    function mountPill() {
        let pill = mountedPillEl && mountedPillEl.isConnected
            ? mountedPillEl
            : document.getElementById('ag-status-pill');
        if (!pill) {
            pill = document.createElement('div');
            pill.id = 'ag-status-pill';
            pill.onclick = (e) => {
                e.stopPropagation();
                openQuickPick();
            };
            pill.onmouseenter = showTooltip;
            pill.onmouseleave = hideTooltip;
        }
        mountedPillEl = pill;

        let swarmPill = mountedSwarmPillEl && mountedSwarmPillEl.isConnected
            ? mountedSwarmPillEl
            : document.getElementById('ag-swarm-pill');
        if (!swarmPill) {
            swarmPill = document.createElement('div');
            swarmPill.id = 'ag-swarm-pill';
            swarmPill.onclick = (e) => {
                e.stopPropagation();
                openSwarmCheckpointsMenu();
            };
            swarmPill.onmouseenter = showSwarmTooltip;
            swarmPill.onmouseleave = hideSwarmTooltip;
        }
        mountedSwarmPillEl = swarmPill;

        let dockedInline = false;
        const excluded = new Set();
        for (let attempt = 0; attempt < 3; attempt++) {
            const settingsBtn = findSettingsButton(excluded);
            if (!settingsBtn || !settingsBtn.parentElement) break;

            const parent = settingsBtn.parentElement;
            const prevCssText = parent.style ? parent.style.cssText : '';
            const prevStyled = parent.dataset ? parent.dataset.agStyled : undefined;
            if (parent.style &&
                (parent.style.getPropertyValue('display') !== 'flex' ||
                 parent.style.getPropertyValue('flex-direction') !== 'row' ||
                 parent.style.getPropertyValue('align-items') !== 'center' ||
                 parent.style.getPropertyValue('gap') !== '10px')) {
                if (parent.dataset) parent.dataset.agStyled = '1';
                parent.style.setProperty('display', 'flex', 'important');
                parent.style.setProperty('flex-direction', 'row', 'important');
                parent.style.setProperty('align-items', 'center', 'important');
                parent.style.setProperty('gap', '10px', 'important');
                parent.style.setProperty('flex-wrap', 'nowrap', 'important');
            }

            if (pill.parentElement !== parent || pill.previousElementSibling !== settingsBtn) {
                if (typeof settingsBtn.after === 'function') {
                    settingsBtn.after(pill);
                } else {
                    parent.insertBefore(pill, settingsBtn.nextSibling || null);
                }
            }
            pill.style.position = 'relative';
            pill.style.left = 'auto';
            pill.style.bottom = 'auto';
            pill.style.zIndex = '';
            pill.style.marginLeft = '';

            // Mount #ag-swarm-pill strictly ABOVE #ag-status-pill in the fixed bottom footer of the left sidebar
            // (below Conversations >, above the Settings/Account row so it stays fixed and does not scroll with projects)
            const footer = parent.parentElement;
            if (footer) {
                if (swarmPill.parentElement !== footer || swarmPill.nextElementSibling !== parent) {
                    footer.insertBefore(swarmPill, parent);
                }
                swarmPill.style.position = 'relative';
                swarmPill.style.left = 'auto';
                swarmPill.style.bottom = 'auto';
                swarmPill.style.zIndex = '';
                swarmPill.style.marginLeft = '10px';
                swarmPill.style.marginBottom = '2px';
            } else {
                if (swarmPill.parentElement !== parent || swarmPill.nextElementSibling !== settingsBtn) {
                    parent.insertBefore(swarmPill, settingsBtn);
                }
                swarmPill.style.position = 'relative';
                swarmPill.style.left = 'auto';
                swarmPill.style.bottom = 'auto';
                swarmPill.style.zIndex = '';
            }

            renderPillContent();
            renderSwarmPillContent();

            if (isPillVisibleInViewport(pill)) {
                dockedInline = true;
                break;
            }
            if (parent.style) parent.style.cssText = prevCssText;
            if (parent.dataset) {
                if (prevStyled === undefined) delete parent.dataset.agStyled;
                else parent.dataset.agStyled = prevStyled;
            }
            excluded.add(settingsBtn);
            if (rejectedDockUntil) rejectedDockUntil.set(settingsBtn, Date.now() + 30000);
            cachedSettingsBtn = null;
        }

        if (!dockedInline) {
            if (!document.body) return;
            cachedSettingsBtn = null;
            if (pill.parentElement !== document.body) {
                document.body.appendChild(pill);
            }
            pill.style.position = 'fixed';
            pill.style.bottom = '8px';
            pill.style.left = '112px';
            pill.style.zIndex = '2147483647';
            pill.style.marginLeft = '0px';

            if (swarmPill.parentElement !== document.body || swarmPill.nextElementSibling !== pill) {
                document.body.insertBefore(swarmPill, pill);
            }
            swarmPill.style.position = 'fixed';
            swarmPill.style.bottom = '34px';
            swarmPill.style.left = '112px';
            swarmPill.style.zIndex = '2147483647';
            swarmPill.style.marginLeft = '0px';

            renderPillContent();
            renderSwarmPillContent();
        }
        startSwarmLivePoll();
    }

    function mountDesktopBar() {
        return mountPill();
    }

    let _swarmPollActive = false;
    let _swarmPollIsIdle = false;

    function _isSwarmPollBusy() {
        const hasActive = Boolean(
            (swarmSnapshot && swarmSnapshot.summary && Number(swarmSnapshot.summary.active_agents) > 0) ||
            (Array.isArray(swarmCheckpoints) && swarmCheckpoints.some((c) => c && !c.is_complete)) ||
            Boolean(watchedSwarmTask && !watchedSwarmTask.is_complete)
        );
        const qp = typeof document !== 'undefined' ? document.getElementById('ag-qp-backdrop') : null;
        const tt = typeof document !== 'undefined' ? document.getElementById('ag-swarm-tooltip') : null;
        const sm = typeof document !== 'undefined' ? document.getElementById('ag-swarm-modal-backdrop') : null;
        const uiOpen = Boolean(
            (qp && qp.style.display !== 'none') ||
            (tt && tt.style.display !== 'none') ||
            (sm && sm.style.display !== 'none')
        );
        return hasActive || uiOpen;
    }

    function _scheduleNextSwarmPoll() {
        if (!_swarmPollActive) return;
        if (swarmLivePollTimer) {
            clearTimeout(swarmLivePollTimer);
            swarmLivePollTimer = null;
        }
        const busy = _isSwarmPollBusy();
        _swarmPollIsIdle = !busy;
        const delay = busy ? 2500 : 20000;
        swarmLivePollTimer = setTimeout(async () => {
            swarmLivePollTimer = null;
            if (!_swarmPollActive) return;
            if (typeof document === 'undefined' || !document.hidden) {
                await loadSwarmState().catch(() => {});
            }
            _scheduleNextSwarmPoll();
        }, delay);
    }

    function startSwarmLivePoll() {
        if (_swarmPollActive) {
            if (_swarmPollIsIdle && _isSwarmPollBusy()) {
                _scheduleNextSwarmPoll();
            }
            return;
        }
        _swarmPollActive = true;
        _scheduleNextSwarmPoll();
    }

    function stopSwarmLivePoll() {
        _swarmPollActive = false;
        _swarmPollIsIdle = false;
        if (swarmLivePollTimer) {
            clearTimeout(swarmLivePollTimer);
            swarmLivePollTimer = null;
        }
    }

    function getCachedAccount() {
        try {
            const raw = localStorage.getItem('ag_last_active_account');
            if (raw) return JSON.parse(raw);
        } catch (_) {}
        return null;
    }

    function saveCachedAccount(acc) {
        try {
            if (acc) localStorage.setItem('ag_last_active_account', JSON.stringify(acc));
        } catch (_) {}
    }

    // Render text inside status pill with dirty-check so idle refreshes cause zero DOM churn
    function renderPillContent() {
        const pill = mountedPillEl && mountedPillEl.isConnected
            ? mountedPillEl
            : document.getElementById('ag-status-pill');
        if (!pill) return;

        const effective = activeAccount || getCachedAccount();
        const rawName = effective ? (effective.name || (effective.email ? effective.email.split('@')[0] : 'Аккаунт')) : 'Загрузка...';
        const info = getLimitingQuotaInfo(effective);
        const activeModel = detectActiveUiModel();
        const gemView = resolvePillGeminiView(info, activeModel);
        const claudePct = info.claudePct;
        const geminiPct = (gemView.pct !== null && gemView.pct !== undefined) ? gemView.pct : info.geminiPct;
        const claudeText = claudePct !== null ? `${claudePct}%` : '—';
        const dualSuffix = (info.argonPct !== null && !gemView.isArgon)
            ? ` · A ${info.argonPct}%`
            : ((gemView.isArgon && info.argonPct !== null && info.geminiPct !== null) ? ` · G ${info.geminiPct}%` : '');
        const gemPctText = (geminiPct !== null && geminiPct !== undefined) ? `${geminiPct}%` : '—';
        const geminiText = gemView.label
            ? `${gemView.label} ${gemPctText}${dualSuffix}`
            : `${gemPctText}${dualSuffix}`;
        const swarmSig = watchedSwarmTask
            ? `${watchedSwarmTask.task_id || ''}:${watchedSwarmTask.current_node || ''}:${watchedSwarmTask.done_workers || 0}/${watchedSwarmTask.total_workers || 0}:${watchedSwarmTask.is_complete ? '1' : '0'}`
            : '';
        const avatarKey = effective ? (effective.avatar_data_url ? 'img' : (effective.avatar_url || '')) : '';
        const sig = `${rawName}|${avatarKey}|${claudeText}|${geminiText}|${info.argonPct ?? ''}|${isRefreshingNetwork ? 'R' : (connOk ? '1' : '0')}|${connCountryCode}|${connPing}|${swarmSig}`;
        if (pill.dataset.sig === sig && pill.childElementCount > 0) return;
        pill.dataset.sig = sig;

        const claudeStyle = claudePct === 0 ? 'color: #ff5555 !important; font-weight: 600;' : '';
        const geminiStyle = geminiPct <= 15 ? 'color: #f59e0b !important; font-weight: 600;' : '';
        let locHtml;
        if (isRefreshingNetwork) {
            pill.classList.remove('ag-offline');
            locHtml = `<span class="ag-pill-loc" style="color:#60a5fa !important;">↻ Сеть...</span>`;
        } else if (!connOk) {
            pill.classList.add('ag-offline');
            locHtml = `<span class="ag-pill-loc">🌐 Офлайн</span>`;
        } else if (connPing > 0 || connCountryCode) {
            pill.classList.remove('ag-offline');
            locHtml = `<span class="ag-pill-loc">${getCountryFlagSvg(connCountryCode)} ${connPing}ms</span>`;
        } else {
            pill.classList.remove('ag-offline');
            locHtml = `<span class="ag-pill-loc">🌐 ...</span>`;
        }

        let swarmHtml = '';
        if (watchedSwarmTask) {
            const done = Number(watchedSwarmTask.done_workers || 0);
            const total = Number(watchedSwarmTask.total_workers || 0);
            const stageLabel = watchedSwarmTask.is_complete
                ? 'Готово ✓'
                : formatSwarmStage(watchedSwarmTask);
            const countText = total > 0 ? ` ${done}/${total}` : '';
            swarmHtml = `<span class="ag-pill-swarm" title="Команда агентов: ${escapeHtml(watchedSwarmTask.task_id || '')}" style="display:inline-block; max-width:96px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align:middle;">• Команда: ${escapeHtml(stageLabel)}${escapeHtml(countText)}</span>`;
        }

        const pillAvatarHtml = renderAvatarHtml(effective, 15, USER_SVG);
        pill.innerHTML = `
            ${pillAvatarHtml}
            <span style="font-weight: 500;">${escapeHtml(rawName)}</span>
            <span class="ag-pill-quotas">[ <span class="ag-sym-claude">${CLAUDE_SVG}</span> <span class="ag-pct-num" style="${claudeStyle}">${claudeText}</span> | <span class="ag-sym-gemini">${GEMINI_SVG}</span> <span class="ag-pct-num" style="${geminiStyle}">${escapeHtml(geminiText)}</span> ]</span>
            ${locHtml}
            ${swarmHtml}
        `;
    }

    function copyTextToClipboard(text) {
        try {
            if (typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
                navigator.clipboard.writeText(text);
                return;
            }
        } catch (_) {}
    }

    function formatDuration(secOrMs) {
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

    function formatElapsed(secOrMs) {
        if (secOrMs == null) return '00:00';
        if (typeof secOrMs === 'string') return secOrMs.trim();
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

    function formatSwarmElapsed(cp) {
        if (!cp) return '00:00';
        if (cp.elapsed) return String(cp.elapsed);
        const start = Number(cp.start_time || cp.created_at || cp.started_at || 0);
        if (!start) return '00:00';
        const startMs = start > 1e11 ? start : start * 1000;
        const diffSec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
        return formatElapsed(diffSec);
    }

    function pluralRu(n, one, two, five) {
        const num = Math.abs(Number(n) || 0);
        const mod10 = num % 10;
        const mod100 = num % 100;
        if (mod100 >= 11 && mod100 <= 19) return `${num} ${five}`;
        if (mod10 === 1) return `${num} ${one}`;
        if (mod10 >= 2 && mod10 <= 4) return `${num} ${two}`;
        return `${num} ${five}`;
    }

    function formatAgentPillText(snapshot, recentCompletionState = null, options = {}) {
        let activeAgents = 0;
        let projects = [];
        const isSnapshotObj = Boolean(snapshot && typeof snapshot === 'object' && !Array.isArray(snapshot));
        if (Array.isArray(snapshot)) {
            const activeCps = snapshot.filter((c) => c && !c.is_complete && c.current_node !== 'error' && c.current_node !== 'failed');
            const projMap = new Map();
            for (const cp of activeCps) {
                const pName = cp.project_name || getPathBasename(cp.project_path || '') || 'Проект';
                let entry = projMap.get(pName);
                if (!entry) {
                    entry = { project_name: pName, project_path: cp.project_path || '', active_agents: 0, active_sessions: 0, latest_activity_ms: cp.start_time || cp.created_at || 0 };
                    projMap.set(pName, entry);
                }
                let wCount = 0;
                if (cp.running_workers != null && Number(cp.running_workers) > 0) {
                    wCount = Number(cp.running_workers);
                } else if (Array.isArray(cp.workers) && cp.workers.length > 0) {
                    const running = cp.workers.filter((w) => w && (w.status === 'running' || w.status === 'working')).length;
                    wCount = running;
                } else {
                    wCount = 0;
                }
                entry.active_agents += wCount;
                entry.active_sessions += 1;
                activeAgents += wCount;
            }
            projects = Array.from(projMap.values());
        } else if (isSnapshotObj) {
            const summary = snapshot.summary || {};
            activeAgents = Number(summary.active_agents) || 0;
            projects = Array.isArray(snapshot.projects) ? snapshot.projects : [];
        }

        const comp = Array.isArray(recentCompletionState)
            ? recentCompletionState[0]
            : recentCompletionState;

        const planningSession = projects.flatMap((p) => (p && Array.isArray(p.sessions)) ? p.sessions.map((s) => ({ ...s, projectName: p.project_name || getPathBasename(p.project_path || 'проект') })) : [])
            .find((s) => s && !s.is_complete && (s.stage_label === 'Планирование' || s.current_node === 'DECOMPOSE') && (Date.now() - (s.started_at_ms || 0)) < 15 * 60 * 1000);

        // If monitor snapshot reports active agents, live ticker ALWAYS takes priority over past completion!
        if (isSnapshotObj && activeAgents > 0) {
            // Proceed to render active ticker below
        } else if (activeAgents === 0 && Array.isArray(snapshot) && snapshot.some(c => c && !c.is_complete && c.current_node !== 'error' && c.current_node !== 'failed')) {
            const activeCp = snapshot.find(c => c && !c.is_complete && c.current_node !== 'error' && c.current_node !== 'failed');
            const pName = activeCp.project_name || getPathBasename(activeCp.project_path || '') || 'Проект';
            return `⚡ Планирование: ${pName}`;
        } else if (planningSession) {
            const pName = planningSession.projectName || 'проект';
            return `⚡ Планирование: ${pName}`;
        } else if (comp) {
            const isFailed = Boolean(
                comp.isError || comp.failed || comp.status === 'failed' || comp.status === 'error'
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

            if (options && options.shortOnly) {
                return `✓ Готово: ${proj}`;
            }
            if (done != null && total != null && dur) {
                return `✓ Готово: ${proj} (${done}/${total} · ${dur})`;
            }
            if (done != null && total != null) {
                return `✓ Готово: ${proj} (${done}/${total})`;
            }
            return `✓ Готово: ${proj}`;
        }

        if (activeAgents <= 0) {
            return '⚡ Агенты: 0';
        }

        const activeProjects = projects.filter((p) => p && p.active_agents > 0);
        if (activeProjects.length <= 1) {
            const p = activeProjects[0] || projects[0] || {};
            const N = p.active_agents || activeAgents;
            const proj = p.project_name || getPathBasename(p.project_path || '') || 'Проект';
            const startMs = Number(p.active_started_at_ms || p.latest_activity_ms || 0);
            const elapsedSec = startMs > 0 ? Math.max(0, Math.floor((Date.now() - (startMs > 1e11 ? startMs : startMs * 1000)) / 1000)) : 0;
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
        const S = activeProjects.reduce((acc, p) => acc + (p.active_sessions || 1), 0) || P;
        const N = activeAgents;

        if (options.compact) {
            return `⚡ ${N} аг. · ${P} пр. · ${S} зад.`;
        }

        const pText = (P >= 2 && P <= 4) ? 'проекта' : 'проектов';
        const sText = (S >= 2 && S <= 4) ? 'задачи' : 'задач';
        const fullText = `⚡ ${N} аг. · ${P} ${pText} · ${S} ${sText}`;

        if (options.maxLength && fullText.length > options.maxLength) {
            return `⚡ ${N} аг. · ${P} пр. · ${S} зад.`;
        }
        return fullText;
    }

    function renderSwarmPillContent() {
        let pill = mountedSwarmPillEl && mountedSwarmPillEl.isConnected
            ? mountedSwarmPillEl
            : document.getElementById('ag-swarm-pill');
        if (!pill) {
            const statusPill = mountedPillEl && mountedPillEl.isConnected
                ? mountedPillEl
                : document.getElementById('ag-status-pill');
            if (statusPill && statusPill.parentElement && typeof statusPill.parentElement.insertBefore === 'function') {
                pill = document.createElement('div');
                pill.id = 'ag-swarm-pill';
                pill.onclick = (e) => {
                    e.stopPropagation();
                    openSwarmCheckpointsMenu();
                };
                pill.onmouseenter = showSwarmTooltip;
                pill.onmouseleave = hideSwarmTooltip;
                statusPill.parentElement.insertBefore(pill, statusPill);
                mountedSwarmPillEl = pill;
            } else {
                return;
            }
        }

        const list = Array.isArray(swarmCheckpoints) ? swarmCheckpoints : [];
        const text = formatAgentPillText(swarmSnapshot || list, recentCompletionTask);
        const isDone = text.startsWith('✓ Готово');
        pill.classList.toggle('ag-swarm-done', isDone);
        if (pill.textContent !== text) {
            pill.textContent = text;
            pill.innerText = text;
            pill.title = text;
        }
    }

    function showSwarmTooltip() {
        if (isMenuOpen) return;
        let tt = document.getElementById('ag-swarm-tooltip');
        if (!tt) {
            tt = document.createElement('div');
            tt.id = 'ag-swarm-tooltip';
            document.body.appendChild(tt);
        }
        renderSwarmTooltipContent(tt);

        const pill = document.getElementById('ag-swarm-pill');
        if (pill) {
            const rect = pill.getBoundingClientRect();
            tt.style.left = Math.max(10, rect.left) + 'px';
            tt.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
            tt.style.display = 'flex';
        }
    }

    function hideSwarmTooltip() {
        const tt = document.getElementById('ag-swarm-tooltip');
        if (tt) tt.style.display = 'none';
    }

    function renderSwarmTooltipContent(tt) {
        if (!tt) return;

        if (Array.isArray(swarmTooltipLines) && swarmTooltipLines.length > 0) {
            let html = '';
            const lastIdx = swarmTooltipLines.length - 1;
            for (let idx = 0; idx < swarmTooltipLines.length; idx++) {
                const rawLine = String(swarmTooltipLines[idx] || '');
                if (!rawLine) continue;
                if (idx === 0) {
                    html += `<div class="ag-tt-title">${escapeHtml(rawLine)}</div>`;
                } else if (idx === lastIdx && rawLine.startsWith('Клик —')) {
                    html += `<div class="ag-tt-footer"><span class="ag-tt-muted">${escapeHtml(rawLine)}</span></div>`;
                } else if (rawLine.startsWith('Проекты и задачи') || rawLine.startsWith('Недавние чаты')) {
                    html += `<div class="ag-tt-line" style="font-size:11px;margin-bottom:2px;"><span class="ag-tt-muted">${escapeHtml(rawLine)}</span></div>`;
                } else if (rawLine.startsWith('📁 ')) {
                    const body = rawLine.slice(2).trim();
                    const sepIdx = body.indexOf(' — ');
                    if (sepIdx !== -1) {
                        const projPart = body.slice(0, sepIdx);
                        const statusPart = body.slice(sepIdx + 3);
                        html += `<div class="ag-tt-line" style="margin-top:3px;">📁 <b>${escapeHtml(projPart)}</b> — <span class="ag-tt-muted">${escapeHtml(statusPart)}</span></div>`;
                    } else {
                        html += `<div class="ag-tt-line" style="margin-top:3px;">📁 <b>${escapeHtml(body)}</b></div>`;
                    }
                } else if (/^\s*↳/.test(rawLine)) {
                    html += `<div class="ag-tt-subline" style="padding-left:14px;"><span>${escapeHtml(rawLine.trim())}</span></div>`;
                } else if (/^\s*•\s/.test(rawLine)) {
                    const body = rawLine.trim().slice(2);
                    const sepIdx = body.indexOf(' — ');
                    if (sepIdx !== -1) {
                        const projPart = body.slice(0, sepIdx);
                        const taskPart = body.slice(sepIdx + 3);
                        html += `<div class="ag-tt-line" style="padding-left:8px;"><span style="color:#4ade80;">•</span> <b>${escapeHtml(projPart)}</b> — <span class="ag-tt-muted">${escapeHtml(taskPart)}</span></div>`;
                    } else {
                        html += `<div class="ag-tt-line" style="padding-left:8px;"><span style="color:#4ade80;">•</span> <span>${escapeHtml(body)}</span></div>`;
                    }
                } else {
                    html += `<div class="ag-tt-line"><span>${escapeHtml(rawLine)}</span></div>`;
                }
            }
            tt.innerHTML = html;
            return;
        }

        if (swarmSnapshot && typeof swarmSnapshot === 'object' && !Array.isArray(swarmSnapshot)) {
            const summary = swarmSnapshot.summary || {};
            const activeAgents = Number(summary.active_agents) || 0;
            const snapProjects = Array.isArray(swarmSnapshot.projects) ? swarmSnapshot.projects : [];
            const activeSnapProjects = snapProjects.filter((p) => p && (p.active_agents > 0 || (p.sessions && p.sessions.some((s) => s && s.is_active))));
            const now = Number(swarmSnapshot.nowMs || swarmSnapshot.updated_at_ms || Date.now());

            let html = '';
            if (activeAgents > 0 && activeSnapProjects.length > 0) {
                const P = activeSnapProjects.length;
                const S = summary.active_sessions || activeSnapProjects.reduce((acc, p) => acc + (p.active_sessions || (p.sessions ? p.sessions.length : 1)), 0) || P;
                const N = activeAgents;
                html += `<div class="ag-tt-title">⚡ ${pluralRu(N, 'агент', 'агента', 'агентов')} · ${pluralRu(P, 'проект', 'проекта', 'проектов')} · ${pluralRu(S, 'задача', 'задачи', 'задач')}</div>`;
                for (const p of activeSnapProjects) {
                    const pName = p.project_name || getPathBasename(p.project_path || '') || 'Проект';
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
                    html += `<div class="ag-tt-line" style="margin-top:3px;">📁 <b>${escapeHtml(pName)}</b> — ${escapeHtml(countStatus)} · ⏱ ${escapeHtml(elapsed)}</div>`;

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

                    for (const sess of sessions) {
                        if (sess.is_active) {
                            const rawTitle = String(sess.title || sess.user_request || '').replace(/\s+/g, ' ').trim();
                            if (rawTitle && rawTitle !== pName) {
                                const titleShort = rawTitle.length > 140 ? `${rawTitle.slice(0, 140).trimEnd()}…` : rawTitle;
                                html += `<div class="ag-tt-line" style="padding-left:8px;"><span style="color:#f59e0b;">•</span> <span>${escapeHtml(titleShort)} · ${escapeHtml(elapsed)}</span></div>`;
                            }
                        }
                    }

                    if (sessions.length === 0 && active > 0) {
                        html += `<div class="ag-tt-line" style="padding-left:8px;"><span style="color:#f59e0b;">•</span> <span>Выполнение задачи · ${escapeHtml(elapsed)}</span></div>`;
                    }

                    const maxVisible = 4;
                    const shownWorkers = runningWorkers.slice(0, maxVisible);
                    for (const w of shownWorkers) {
                        const wName = w.name || 'агент';
                        const wModelRaw = w.model ? String(w.model) : '';
                        const wStart = w.started_at_ms || startMs || 0;
                        const wElapsedSec = wStart > 0 ? Math.max(0, Math.floor((now - wStart) / 1000)) : elapsedSec;
                        const wElapsed = formatElapsed(wElapsedSec);
                        const rawDesc = String(w.task_description || w.task_desc || '').replace(/\s+/g, ' ').trim();
                        const filesStr = Array.isArray(w.files) && w.files.length > 0 ? w.files.slice(0, 2).map((f) => getPathBasename(String(f))).join(', ') : '';
                        const taskSummary = rawDesc
                            ? (rawDesc.length > 110 ? `${rawDesc.slice(0, 110).trimEnd()}…` : rawDesc)
                            : filesStr;
                        if (taskSummary) {
                            const meta = [wName !== taskSummary ? wName : '', wModelRaw].filter(Boolean).join(', ');
                            const metaSuffix = meta ? ` (${meta})` : '';
                            html += `<div class="ag-tt-subline" style="padding-left:14px;"><span style="color:#f59e0b;">↳ ⏳</span> <span>${escapeHtml(taskSummary)}${escapeHtml(metaSuffix)} · ${escapeHtml(wElapsed)}</span></div>`;
                        } else {
                            const wModel = wModelRaw ? ` (${wModelRaw})` : '';
                            html += `<div class="ag-tt-subline" style="padding-left:14px;"><span style="color:#f59e0b;">↳ ⏳</span> <span>${escapeHtml(wName)}${escapeHtml(wModel)} · ${escapeHtml(wElapsed)}</span></div>`;
                        }
                    }
                    if (runningWorkers.length > maxVisible) {
                        const remaining = runningWorkers.length - maxVisible;
                        html += `<div class="ag-tt-subline" style="padding-left:14px;"><span style="color:#9ca3af;">↳ … ещё ${remaining} задач в работе</span></div>`;
                    }
                }
            } else {
                html += `<div class="ag-tt-title">⚡ Все агенты свободны</div>`;
                const grouped = [];
                if (Array.isArray(swarmSnapshot.recent_projects) && swarmSnapshot.recent_projects.length > 0) {
                    for (const rp of swarmSnapshot.recent_projects.slice(0, 3)) {
                        const items = Array.isArray(rp.tasks) ? rp.tasks.slice(0, 3) : [];
                        if (items.length > 0) grouped.push({ pName: rp.project_name || 'Проект', items });
                    }
                } else {
                    const completedSessions = [];
                    for (const p of snapProjects) {
                        const pName = p.project_name || getPathBasename(p.project_path || '') || 'Проект';
                        for (const s of (p.sessions || [])) {
                            if (s.is_complete || !s.is_active) {
                                completedSessions.push({ ...s, projectName: pName });
                            }
                        }
                    }
                    completedSessions.sort((a, b) => (b.updated_at_ms || 0) - (a.updated_at_ms || 0));
                    const groupMap = new Map();
                    for (const s of completedSessions) {
                        const pName = s.projectName || getPathBasename(s.project_path || '') || 'Проект';
                        if (!groupMap.has(pName)) {
                            if (grouped.length >= 3) continue;
                            const entry = { pName, items: [] };
                            groupMap.set(pName, entry);
                            grouped.push(entry);
                        }
                        const grp = groupMap.get(pName);
                        if (grp.items.length < 3) grp.items.push(s);
                    }
                }
                if (grouped.length > 0) {
                    for (const grp of grouped) {
                        html += `<div class="ag-tt-line" style="margin-top:3px;">📁 <b>${escapeHtml(grp.pName)}</b></div>`;
                        for (const s of grp.items.slice(0, 3)) {
                            const rawTitle = String(s.title || s.user_request || s.name || '').replace(/\s+/g, ' ').trim();
                            const reqShort = rawTitle.length > 68 ? `${rawTitle.slice(0, 68).trimEnd()}…` : rawTitle;
                            const durSec = Number(s.duration_seconds) || 0;
                            const dur = durSec > 0 ? formatDuration(durSec) : (s.duration ? formatDuration(s.duration) : '');
                            const isErr = Boolean(s.isError || s.failed || (s.errors && s.errors.length > 0));
                            const suffix = isErr ? `(ошибка${dur ? `, ${dur}` : ''})` : `(готово${dur ? `, ${dur}` : ''})`;
                            const dotColor = isErr ? '#f87171' : '#4ade80';
                            const taskText = reqShort || 'Задача';
                            html += `<div class="ag-tt-line" style="padding-left:8px;"><span style="color:${dotColor};">•</span> <span>${escapeHtml(taskText)}</span> <span class="ag-tt-muted">${escapeHtml(suffix)}</span></div>`;
                        }
                    }
                } else {
                    html += `<div class="ag-tt-line"><span class="ag-tt-muted">Нет недавних задач</span></div>`;
                }
            }
            html += `<div class="ag-tt-footer"><span class="ag-tt-muted">Клик — меню задач и быстрый запуск</span></div>`;
            tt.innerHTML = html;
            return;
        }

        const list = Array.isArray(swarmCheckpoints) ? swarmCheckpoints : [];
        const active = list.filter((c) => c && !c.is_complete && c.current_node !== 'error' && c.current_node !== 'failed');

        let html = '';
        if (active.length > 0) {
            let totalW = 0;
            const projSet = new Set();
            for (const cp of active) {
                const p = cp.project_name || getPathBasename(cp.project_path || '') || 'проект';
                projSet.add(p);
                const rw = Number(cp.running_workers || 0);
                const workers = Array.isArray(cp.workers) ? cp.workers : [];
                const runningFromWorkers = workers.filter(w => w && (w.status === 'running' || w.status === 'working')).length;
                totalW += Math.max(rw, runningFromWorkers);
            }
            if (totalW > 0) {
                html += `<div class="ag-tt-title">⚡ ${totalW} агентов · ${projSet.size} проектов · ${active.length} задач</div>`;
            } else {
                html += `<div class="ag-tt-title">⚡ ${active.length} задач в планировании · ${projSet.size} проектов</div>`;
            }
            for (const cp of active) {
                const p = cp.project_name || getPathBasename(cp.project_path || '') || 'проект';
                const done = Number(cp.done_workers || 0);
                const total = Number(cp.total_workers || (Array.isArray(cp.workers) ? cp.workers.length : 0));
                const elapsed = formatSwarmElapsed(cp);
                const workers = Array.isArray(cp.workers) ? cp.workers : [];
                const runningW = Math.max(Number(cp.running_workers || 0), workers.filter(w => w && (w.status === 'running' || w.status === 'working')).length);
                let countStatus = '';
                if (total === 0 || workers.length === 0) {
                    countStatus = '📋 Планирование';
                } else if (runningW > 0) {
                    countStatus = `${runningW}/${total} работают${done > 0 ? ` (${done} готово)` : ''}`;
                } else if (done > 0) {
                    countStatus = `✓ ${done}/${total} готово`;
                } else {
                    countStatus = 'В очереди';
                }
                html += `<div class="ag-tt-line"><b>• ${escapeHtml(p)}</b> — ${escapeHtml(countStatus)} · ⏱ ${escapeHtml(elapsed)}</div>`;
                for (const w of workers) {
                    const wModel = w.model || 'flash';
                    const wElapsed = w.elapsed || elapsed;
                    const wFiles = Array.isArray(w.files) && w.files.length ? ` — ${w.files.join(', ')}` : (w.task ? ` — ${w.task}` : '');
                    if (w.status === 'done') {
                        html += `<div class="ag-tt-subline"><span style="color:#4ade80;">↳ ✅</span> <span class="ag-tt-muted">${escapeHtml(w.name)} (${escapeHtml(wModel)}) (готово${w.duration ? ', ' + escapeHtml(w.duration) : ''})</span></div>`;
                    } else {
                        html += `<div class="ag-tt-subline"><span style="color:#f59e0b;">↳ ⏳</span> <span>${escapeHtml(w.name)} (${escapeHtml(wModel)}) · ${escapeHtml(wElapsed)}</span><span class="ag-tt-muted">${escapeHtml(wFiles)}</span></div>`;
                    }
                }
            }
        } else {
            html += `<div class="ag-tt-title">⚡ Все агенты свободны</div>`;
            const history = list.filter((c) => c && (c.is_complete || c.current_node === 'error')).slice(0, 3);
            if (history.length > 0) {
                for (const cp of history) {
                    const p = cp.project_name || getPathBasename(cp.project_path || '') || 'проект';
                    const req = String(cp.user_request || '').replace(/\s+/g, ' ').trim();
                    const reqShort = req.length > 40 ? `${req.slice(0, 40)}…` : req;
                    html += `<div class="ag-tt-line"><span style="color:#4ade80;">•</span> <b>${escapeHtml(p)}</b> — <span class="ag-tt-muted">${escapeHtml(reqShort || 'готово')}</span></div>`;
                }
            } else {
                html += `<div class="ag-tt-line"><span class="ag-tt-muted">Нет недавних задач</span></div>`;
            }
        }
        html += `<div class="ag-tt-footer"><span class="ag-tt-muted">Клик — меню задач и быстрый запуск</span></div>`;
        tt.innerHTML = html;
    }

    function onSwarmTaskCompleted(cp) {
        if (!cp) return;
        const projName = cp.project_name || getPathBasename(cp.project_path || '') || 'проект';
        recentCompletionTask = cp;
        renderSwarmPillContent();
        showToast(`✓ Готово: <b>${escapeHtml(projName)}</b>`, false, 5000);
        if (recentCompletionTimer) clearTimeout(recentCompletionTimer);
        recentCompletionTimer = setTimeout(() => {
            recentCompletionTask = null;
            recentCompletionTimer = null;
            renderSwarmPillContent();
        }, 15000);
    }

    function showTooltip() {
        triggerLiveRefresh('active', false);
        if (isMenuOpen) return;
        let tt = document.getElementById('ag-ide-tooltip');
        if (!tt) {
            tt = document.createElement('div');
            tt.id = 'ag-ide-tooltip';
            document.body.appendChild(tt);
        }
        renderTooltipContent(tt);

        const pill = document.getElementById('ag-status-pill');
        if (pill) {
            const rect = pill.getBoundingClientRect();
            tt.style.left = Math.max(10, rect.left) + 'px';
            tt.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
            tt.style.display = 'flex';
        }
    }

    function formatWindowSublinesHtml(h5Pct, h5Reset, weeklyPct, weeklyReset) {
        let html = '';
        if (h5Pct !== null && h5Pct !== undefined) {
            const r = (h5Pct < 100 && h5Reset) ? ` — ⌛ ${escapeHtml(formatResetText(h5Reset, h5Pct))}` : '';
            html += `<div class="ag-tt-subline" style="padding-left:28px;"><span style="color:#555555;">↳</span> <span class="ag-tt-muted">5ч лимит: <strong style="color:#d4d4d4;">${h5Pct}%</strong>${r}</span></div>`;
        }
        if (weeklyPct !== null && weeklyPct !== undefined) {
            const r = (weeklyPct < 100 && weeklyReset) ? ` — ⌛ ${escapeHtml(formatResetText(weeklyReset, weeklyPct))}` : '';
            html += `<div class="ag-tt-subline" style="padding-left:28px;"><span style="color:#555555;">↳</span> <span class="ag-tt-muted">Недельный: <strong style="color:#d4d4d4;">${weeklyPct}%</strong>${r}</span></div>`;
        }
        return html;
    }

    function renderTooltipContent(tt) {
        if (!tt) return;
        const effective = activeAccount || getCachedAccount();
        const name = effective ? (effective.name || 'Аккаунт') : 'Аккаунт Google';
        const email = effective ? (effective.email || '') : '';
        const initial = escapeHtml((name || email || 'A').trim().charAt(0).toUpperCase());
        const avatarBg = getAvatarBg(name || email);
        const tier = (effective && effective.tier) ? effective.tier : 'PRO';
        const info = getLimitingQuotaInfo(effective);
        const activeModel = detectActiveUiModel(true);
        const claudePct = info.claudePct !== null ? info.claudePct : 0;
        const geminiPct = info.geminiPct;
        const argonPct = info.argonPct;

        const claudeResetText = formatResetText(info.claudeReset, claudePct) || (claudePct === 100 ? '(доступно 100%)' : '');
        const geminiResetText = formatResetText(info.geminiReset, geminiPct) || (geminiPct === 100 ? '(доступно 100%)' : '');
        const argonResetText = argonPct !== null ? (formatResetText(info.argonReset, argonPct) || (argonPct === 100 ? '(доступно 100%)' : '')) : '';
        const claudeNumStyle = claudePct === 0 ? 'color:#ff5555; font-weight:600;' : '';
        const geminiNumStyle = geminiPct <= 15 ? 'color:#f59e0b; font-weight:600;' : '';
        const argonNumStyle = (argonPct !== null && argonPct <= 15) ? 'color:#f59e0b; font-weight:600;' : '';

        const claudeWindowsHtml = formatWindowSublinesHtml(info.claude5hPct, info.claude5hReset, info.claudeWeeklyPct, info.claudeWeeklyReset);
        const geminiWindowsHtml = formatWindowSublinesHtml(info.gemini5hPct, info.gemini5hReset, info.geminiWeeklyPct, info.geminiWeeklyReset);
        const argonWindowsHtml = argonPct !== null
            ? formatWindowSublinesHtml(info.argon5hPct, info.argon5hReset, info.argonWeeklyPct, info.argonWeeklyReset)
            : '';

        const isCarbonActive = Boolean(activeModel && activeModel.shortLabel === 'Carbon');
        const activeGeminiBadge = (activeModel && activeModel.family === 'gemini_models' && !isCarbonActive)
            ? '<span style="color:#60a5fa; font-size:10px; margin-left:4px;">активна</span>'
            : '';
        const activeCarbonBadge = isCarbonActive
            ? '<span style="color:#60a5fa; font-size:10px; margin-left:4px;">активна</span>'
            : '';
        const activeArgonBadge = (activeModel && activeModel.family === 'argon')
            ? '<span style="color:#60a5fa; font-size:10px; margin-left:4px;">активна</span>'
            : '';
        const activeClaudeBadge = (activeModel && (activeModel.family === 'claude' || activeModel.family === 'gpt'))
            ? '<span style="color:#60a5fa; font-size:10px; margin-left:4px;">активна</span>'
            : '';

        const activeModelRowHtml = activeModel
            ? `
            <div class="ag-tt-line">
                <span>•</span>
                <span style="color:#999999;">Модель:</span>
                <strong style="color:#ffffff;">${escapeHtml(activeModel.displayName)}</strong>
                ${activeModel.family === 'argon'
                    ? (argonPct !== null ? '<span class="ag-tt-muted">(отд. лимит Argon)</span>' : '<span class="ag-tt-muted">(пул Ultra)</span>')
                    : (isCarbonActive
                        ? '<span class="ag-tt-muted">(пул Ultra)</span>'
                        : (activeModel.family === 'gemini_models'
                            ? '<span class="ag-tt-muted">(лимит Gemini)</span>'
                            : ''))}
            </div>`
            : '';

        const isUltraTier = String(tier || '').toUpperCase().includes('ULTRA');
        const geminiGroupLabel = (isUltraTier || argonPct !== null || (activeModel && activeModel.family === 'gemini_models'))
            ? 'Gemini (Hydrogen / 3.8 Flash):'
            : 'Gemini:';

        const argonRowHtml = argonPct !== null
            ? `
            <div class="ag-tt-subline">
                <span style="color:#666666;">◦</span>
                ${GEMINI_SVG}
                <strong style="color:#ffffff;">Gemini 4 Argon:</strong>
                <span style="${argonNumStyle}">${argonPct}%</span>
                ${activeArgonBadge}
                ${(!argonWindowsHtml && argonResetText) ? `<span class="ag-tt-muted">— ⌛ ${escapeHtml(argonResetText)}</span>` : ''}
            </div>
            ${argonWindowsHtml}`
            : (isUltraTier
                ? `
            <div class="ag-tt-subline">
                <span style="color:#666666;">◦</span>
                ${GEMINI_SVG}
                <strong style="color:#ffffff;">Gemini 4 Carbon:</strong>
                <span style="${geminiNumStyle}">${geminiPct}%</span>
                ${activeCarbonBadge}
                <span class="ag-tt-muted">(пул Ultra)</span>
            </div>
            <div class="ag-tt-subline">
                <span style="color:#666666;">◦</span>
                ${GEMINI_SVG}
                <strong style="color:#ffffff;">Gemini 4 Argon:</strong>
                <span style="${geminiNumStyle}">${geminiPct}%</span>
                ${activeArgonBadge}
                <span class="ag-tt-muted">(пул Ultra)</span>
            </div>`
                : `
            <div class="ag-tt-subline">
                <span style="color:#666666;">◦</span>
                ${GEMINI_SVG}
                <strong style="color:#ffffff;">Gemini 4 Argon:</strong>
                <span style="${geminiNumStyle}">${geminiPct}%</span>
                <span class="ag-tt-muted">(общий пул Gemini)</span>
                ${activeArgonBadge}
            </div>`);

        const netRowsHtml = connOk
            ? `
            <div class="ag-tt-line">
                <span>•</span>
                <span style="color:#999999;">Страна:</span>
                <span style="color:#ffffff;">${escapeHtml(connCountry || 'Проверка...')}</span>
            </div>
            <div class="ag-tt-line">
                <span>•</span>
                <span style="color:#999999;">Пинг:</span>
                <span style="color:#ffffff;">${connPing} ms</span>
            </div>`
            : `
            <div class="ag-tt-line">
                <span>•</span>
                <span style="color:#999999;">Сеть:</span>
                <span style="color:#f87171;">${escapeHtml(connStatusText || 'Офлайн')}</span>
            </div>`;
        const proxyRowHtml = connProxyUrl
            ? `
            <div class="ag-tt-line">
                <span>•</span>
                <span style="color:#999999;">Прокси:</span>
                <span style="color:#a0a0a0;">${escapeHtml(connProxyUrl)}</span>
            </div>`
            : '';

        tt.innerHTML = `
            <div class="ag-tt-title">Antigravity Assistant</div>
            <div class="ag-tt-line">
                <span>•</span>
                <span style="color:#999999;">Аккаунт:</span>
                ${renderAvatarHtml(activeAccount, 16)}
                <strong style="color:#ffffff;">${escapeHtml(name)}</strong>
                <span class="ag-tt-muted">(${escapeHtml(email)})</span>
            </div>
            <div class="ag-tt-line">
                <span>•</span>
                <span style="color:#999999;">Тариф:</span>
                <span class="ag-tt-tier">${escapeHtml(tier)}</span>
            </div>
            ${activeModelRowHtml}
            <div class="ag-tt-line">
                <span>•</span>
                <span style="color:#999999;">Лимиты нейросетей:</span>
            </div>
            <div class="ag-tt-subline">
                <span style="color:#666666;">◦</span>
                ${CLAUDE_SVG}
                <strong style="color:#ffffff;">Claude / GPT:</strong>
                <span style="${claudeNumStyle}">${claudePct}%</span>
                ${activeClaudeBadge}
                ${(!claudeWindowsHtml && claudeResetText) ? `<span class="ag-tt-muted">— ⌛ ${claudeResetText}</span>` : ''}
            </div>
            ${claudeWindowsHtml}
            <div class="ag-tt-subline">
                <span style="color:#666666;">◦</span>
                ${GEMINI_SVG}
                <strong style="color:#ffffff;">${geminiGroupLabel}</strong>
                <span style="${geminiNumStyle}">${geminiPct}%</span>
                ${activeGeminiBadge}
                ${(!geminiWindowsHtml && geminiResetText) ? `<span class="ag-tt-muted">— ${geminiResetText}</span>` : ''}
            </div>
            ${geminiWindowsHtml}
            ${argonRowHtml}
            ${netRowsHtml}
            ${proxyRowHtml}
            <div class="ag-tt-footer">Нажмите для выбора действия</div>
        `;
    }

    function hideTooltip() {
        const tt = document.getElementById('ag-ide-tooltip');
        if (tt) tt.style.display = 'none';
    }

    // Modal dialog for Quota Details (0ms Instant opening, 1:1 with Antigravity IDE modal)
    function openQuotaModal() {
        let backdrop = document.getElementById('ag-quota-modal-backdrop');
        if (!backdrop) {
            backdrop = document.createElement('div');
            backdrop.id = 'ag-quota-modal-backdrop';
            backdrop.onclick = (e) => {
                if (e.target === backdrop) closeQuotaModal();
            };
            document.body.appendChild(backdrop);
        }

        renderQuotaModalContent();
        backdrop.style.display = 'flex';

        triggerLiveRefresh('active', true).then(() => {
            renderQuotaModalContent();
        });
    }

    function wrapParenText(rt) {
        if (!rt) return '';
        return /^\(.*\)$/.test(rt) ? rt : `(${rt})`;
    }

    function formatModalWindowHtml(label, h5Pct, h5Reset, weeklyPct, weeklyReset) {
        const lines = [];
        if (h5Pct !== null && h5Pct !== undefined) {
            const rt = wrapParenText(formatResetText(h5Reset, h5Pct));
            lines.push(`<div style="padding-left:18px; color:#9ca3af; font-size:11px;">• 5ч лимит: <strong style="color:#e5e7eb;">${h5Pct}%</strong>${rt ? ` ${escapeHtml(rt)}` : ''}</div>`);
        }
        if (weeklyPct !== null && weeklyPct !== undefined) {
            const rt = wrapParenText(formatResetText(weeklyReset, weeklyPct));
            lines.push(`<div style="padding-left:18px; color:#9ca3af; font-size:11px;">• Недельный лимит: <strong style="color:#e5e7eb;">${weeklyPct}%</strong>${rt ? ` ${escapeHtml(rt)}` : ''}</div>`);
        }
        if (!lines.length) return '';
        return `<div style="margin-top:4px;"><div style="color:#d1d5db; font-weight:500;">${label}</div>${lines.join('')}</div>`;
    }

    function renderQuotaModalContent() {
        const backdrop = document.getElementById('ag-quota-modal-backdrop');
        if (!backdrop) return;

        const effective = activeAccount || getCachedAccount();
        const tier = (effective && effective.tier) ? effective.tier : 'PRO';
        const info = getLimitingQuotaInfo(effective);
        const claudePct = info.claudePct !== null ? info.claudePct : 0;
        const geminiPct = info.geminiPct;

        const claudeResetText = formatResetText(info.claudeReset, claudePct) || (claudePct === 100 ? '(доступно 100%)' : '');
        const geminiResetText = formatResetText(info.geminiReset, geminiPct) || (geminiPct === 100 ? '(доступно 100%)' : '');
        const claudePctHtml = claudePct === 0 ? `<span style="color:#ff5555; font-weight:600;">${claudePct}%</span>` : `${claudePct}%`;
        const claudeResetHtml = claudeResetText
            ? (/^\(.*\)$/.test(claudeResetText) ? ` ${claudeResetText}` : ` ⌛ ${claudeResetText}`)
            : '';

        const geminiModalWin = formatModalWindowHtml(`${GEMINI_SVG} <strong>Gemini (Hydrogen / Flash / Pro):</strong>`, info.gemini5hPct, info.gemini5hReset, info.geminiWeeklyPct, info.geminiWeeklyReset);
        const argonModalWin = info.argonPct !== null
            ? formatModalWindowHtml(`${GEMINI_SVG} <strong>Gemini 4 Argon:</strong>`, info.argon5hPct, info.argon5hReset, info.argonWeeklyPct, info.argonWeeklyReset)
            : '';
        const claudeModalWin = formatModalWindowHtml(`${CLAUDE_SVG} <strong>Claude / GPT:</strong>`, info.claude5hPct, info.claude5hReset, info.claudeWeeklyPct, info.claudeWeeklyReset);
        const argonResetModal = info.argonPct !== null
            ? (formatResetText(info.argonReset, info.argonPct) || (info.argonPct === 100 ? '(доступно 100%)' : ''))
            : '';
        const argonLineHtml = info.argonPct !== null
            ? `<div>${GEMINI_SVG} Gemini 4 Argon: ${info.argonPct}%${argonResetModal ? ` ${escapeHtml(argonResetModal)}` : ''}</div>`
            : '';

        backdrop.innerHTML = `
            <div id="ag-quota-modal-box">
                <div class="ag-dialog-close" id="ag-dialog-close-btn">✕</div>
                <div class="ag-dialog-icon">i</div>
                <div class="ag-dialog-content">
                    <div>💎 <strong>Тариф аккаунта:</strong> ${escapeHtml(tier)}</div>
                    <div style="margin-top: 6px;">📊 <strong>Лимиты нейросетей:</strong></div>
                    <div style="display:flex; flex-direction:column; gap:3px; margin-top:2px;">
                        <div>${GEMINI_SVG} Gemini (Hydrogen / Flash / Pro): ${geminiPct}%${geminiResetText ? ` ${geminiResetText}` : ''}</div>
                        ${argonLineHtml}
                        <div>${CLAUDE_SVG} Claude Sonnet 4.6 (Thinking): ${claudePctHtml}${claudeResetHtml}</div>
                        <div>${CLAUDE_SVG} Claude Opus 4.6 (Thinking): ${claudePctHtml}${claudeResetHtml}</div>
                    </div>
                    ${(geminiModalWin || argonModalWin || claudeModalWin) ? `<div style="margin-top: 8px; padding-top: 6px; border-top: 1px solid #2d2d2d;">⏱ <strong>Детализация окон (5ч / Неделя):</strong>${geminiModalWin}${argonModalWin}${claudeModalWin}</div>` : ''}
                    <button class="ag-dialog-ok-btn" id="ag-dialog-ok-btn">OK</button>
                </div>
            </div>
        `;

        const closeBtn = document.getElementById('ag-dialog-close-btn');
        if (closeBtn) closeBtn.onclick = closeQuotaModal;
        const okBtn = document.getElementById('ag-dialog-ok-btn');
        if (okBtn) okBtn.onclick = closeQuotaModal;
    }

    function closeQuotaModal() {
        const backdrop = document.getElementById('ag-quota-modal-backdrop');
        if (backdrop) backdrop.style.display = 'none';
    }

    function openQuickPick() {
        hideTooltip();
        let backdrop = document.getElementById('ag-qp-backdrop');
        if (!backdrop) {
            backdrop = document.createElement('div');
            backdrop.id = 'ag-qp-backdrop';
            backdrop.onclick = closeQuickPick;
            document.body.appendChild(backdrop);
        }

        let container = document.getElementById('ag-qp-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'ag-qp-container';
            document.body.appendChild(container);
        }

        isMenuOpen = true;
        currentMenuLevel = 'main';
        currentFilterText = '';
        backdrop.style.display = 'block';
        container.style.display = 'flex';

        fetchData();
        renderQuickPick();
        triggerLiveRefresh('all', false);
        loadSwarmState().then(() => {
            if (isMenuOpen && currentMenuLevel === 'main') renderQuickPick();
        });
        loadAssistantSettingsState().then(() => {
            if (isMenuOpen && currentMenuLevel === 'main') renderQuickPick();
        });
    }

    function closeQuickPick() {
        const backdrop = document.getElementById('ag-qp-backdrop');
        const container = document.getElementById('ag-qp-container');
        if (backdrop) backdrop.style.display = 'none';
        if (container) container.style.display = 'none';
        isMenuOpen = false;
        currentMenuLevel = 'main';
    }

    function getAccountsRenderSig() {
        const activeId = activeAccount ? (activeAccount.id || activeAccount.email || '') : '';
        return activeId + '|' + accountsList.map((a) =>
            `${a.id}:${a.name || ''}:${a.email || ''}:${a.claude_percentage ?? ''}:${a.gemini_percentage ?? ''}:${a.argon_percentage ?? ''}`
        ).join(';');
    }

    function showToast(htmlText, isError = false, durationMs = 4500) {
        let toast = document.getElementById('ag-desktop-toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'ag-desktop-toast';
            document.body.appendChild(toast);
        }
        if (toastTimer) {
            clearTimeout(toastTimer);
            toastTimer = null;
        }
        toast.classList.toggle('ag-toast-error', Boolean(isError));
        toast.innerHTML = htmlText;
        toast.style.display = 'block';
        if (durationMs > 0) {
            toastTimer = setTimeout(() => {
                toastTimer = null;
                toast.style.display = 'none';
            }, durationMs);
        }
    }

    function renderQuickPick(filterText = '') {
        const container = document.getElementById('ag-qp-container');
        if (!container) return;

        const name = activeAccount ? (activeAccount.name || activeAccount.email) : 'Default';
        const initial = escapeHtml((name || 'A').charAt(0).toUpperCase());
        const avatarBg = getAvatarBg(name);
        let locText;
        if (isRefreshingNetwork) {
            locText = 'Обновление сети...';
        } else if (!connOk) {
            locText = 'Офлайн';
        } else if (connCountry && connCountry !== 'Проверка...') {
            locText = `${connCountry} (${connPing}ms)`;
        } else {
            locText = `${connPing}ms`;
        }

        if (currentMenuLevel === 'main') {
            // Native quick-pick items (1:1 synced with Antigravity IDE)
            const placeholder = escapeHtml(`Antigravity Assistant | Аккаунт: ${name} | ${locText}`);
            const policyText = FAILOVER_POLICY_LABELS[failoverPolicy] || FAILOVER_POLICY_LABELS.interactive;
            const activeCp = Array.isArray(swarmCheckpoints)
                ? (swarmCheckpoints.find((c) => c && !c.is_complete) || swarmCheckpoints[0] || null)
                : null;
            let swarmDesc = 'Запуск нескольких агентов по разным файлам, прогресс и /swarm в чате';
            if (activeCp) {
                const pName = activeCp.project_name || getPathBasename(activeCp.project_path || '') || 'проект';
                const stage = formatSwarmStage(activeCp);
                const wModels = Array.isArray(activeCp.worker_models) && activeCp.worker_models.length
                    ? activeCp.worker_models.join(', ')
                    : 'flash';
                const totalW = Number(activeCp.total_workers || 0);
                const doneW = Number(activeCp.done_workers || 0);
                swarmDesc = activeCp.is_complete
                    ? `Последняя: ${pName} · ${stage} (${doneW}/${totalW} агентов)`
                    : `Активна: ${pName} · ${stage} · Агенты: ${doneW}/${totalW} (${wModels})`;
            }
            const customModelCount = modelConfig && modelConfig.custom_models && typeof modelConfig.custom_models === 'object'
                ? Object.keys(modelConfig.custom_models).length
                : 0;
            const GEAR_SVG = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#9ca3af" stroke-width="1.35" style="flex-shrink:0"><circle cx="8" cy="8" r="2.5"/><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1"/></svg>`;
            const BOLT_SVG = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#f59e0b" stroke-width="1.45" stroke-linejoin="round" style="flex-shrink:0"><polygon points="9,1.5 3.5,9 8,9 7,14.5 12.5,7 8,7"/></svg>`;

            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" value="${placeholder}" readonly spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">
                    <div class="ag-qp-row selected" data-action="switch_account">
                        <div class="ag-qp-row-left">
                            ${renderAvatarHtml(activeAccount, 18)}
                            <span class="ag-qp-row-title">Переключить или добавить аккаунт</span>
                            <span class="ag-qp-row-desc">Текущий: ${escapeHtml(name)}</span>
                        </div>
                        <span class="ag-acc-count-badge">Аккаунтов: ${accountsList.length}</span>
                    </div>
                    <div class="ag-qp-row" data-action="quota">
                        <div class="ag-qp-row-left">
                            ${PULSE_SVG}
                            <span class="ag-qp-row-title">Квоты и лимиты аккаунта</span>
                            <span class="ag-qp-row-desc">Остаток Claude, Gemini и точное время сброса</span>
                        </div>
                    </div>
                    <div class="ag-qp-row" data-action="quota_policy">
                        <div class="ag-qp-row-left">
                            ${BOLT_SVG}
                            <span class="ag-qp-row-title">Режим при исчерпании квоты: ${escapeHtml(policyText)}</span>
                            <span class="ag-qp-row-desc">Другой аккаунт → другая умная модель → резервный Flash, чтобы рой не встал</span>
                        </div>
                    </div>
                    <div class="ag-qp-row" data-action="swarm_hub">
                        <div class="ag-qp-row-left">
                            ${SWARM_SVG}
                            <span class="ag-qp-row-title">Команда агентов (задачи и запуск)</span>
                            <span class="ag-qp-row-desc">${escapeHtml(swarmDesc)}</span>
                        </div>
                    </div>
                    <div class="ag-qp-row" data-action="model_config">
                        <div class="ag-qp-row-left">
                            ${GEAR_SVG}
                            <span class="ag-qp-row-title">Доп. настройки моделей (сторонние API)</span>
                            <span class="ag-qp-row-desc">Для стандартных Claude/Gemini/Flash менять не нужно (своих: ${customModelCount})</span>
                        </div>
                    </div>
                    <div class="ag-qp-row" data-action="refresh_network">
                        <div class="ag-qp-row-left">
                            ${SYNC_SVG}
                            <span class="ag-qp-row-title">Обновить VPN и сеть</span>
                            <span class="ag-qp-row-desc">Перепроверить пинг, страну и соединение</span>
                        </div>
                    </div>
                    <div class="ag-qp-row" data-action="open_log">
                        <div class="ag-qp-row-left">
                            ${LOG_SVG}
                            <span class="ag-qp-row-title">Открыть лог работы</span>
                            <span class="ag-qp-row-desc">Просмотр лога запросов моделей и языкового сервера</span>
                        </div>
                    </div>
                </div>
            `;

            const rows = container.querySelectorAll('.ag-qp-row');
            rows.forEach(r => {
                r.onclick = () => {
                    const action = r.getAttribute('data-action');
                    handleMainAction(action);
                };
            });
        } else if (currentMenuLevel === 'quota_policy') {
            const policies = [
                {
                    id: 'interactive',
                    title: 'Спрашивать каждый раз (рекомендуется)',
                    desc: 'Умные модели защищены — при 429 спросим: другой аккаунт, другая Tier-1 или резерв Flash'
                },
                {
                    id: 'account_hop',
                    title: 'Авто: держать умную (каскад + резерв Flash при 0%)',
                    desc: 'Другой аккаунт → другая Tier-1 (Gemini Pro) → дежурный Flash по 1–2 файла, если 0% везде'
                },
                {
                    id: 'model_fallback',
                    title: 'Авто: сразу перейти на быстрый Flash',
                    desc: 'Максимальная скорость, без остановки роя (простое планирование + обязательные тесты)'
                }
            ];
            let rowsHtml = `
                <div class="ag-qp-row" data-action="back_to_main">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#007acc; font-size:14px;">←</span>
                        <span class="ag-qp-row-title">Назад в главное меню</span>
                    </div>
                </div>
            `;
            for (const p of policies) {
                const activeMark = failoverPolicy === p.id ? '✓ ' : '';
                rowsHtml += `
                    <div class="ag-qp-row ${failoverPolicy === p.id ? 'selected' : ''}" data-policy-id="${p.id}">
                        <div class="ag-qp-row-left">
                            <span class="ag-qp-row-title">${escapeHtml(activeMark + p.title)}</span>
                            <span class="ag-qp-row-desc">${escapeHtml(p.desc)}</span>
                        </div>
                    </div>
                `;
            }
            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" value="Режим при исчерпании квоты (умные модели защищены по умолчанию)" readonly spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">${rowsHtml}</div>
            `;
            const backRow = container.querySelector('[data-action="back_to_main"]');
            if (backRow) backRow.onclick = () => { currentMenuLevel = 'main'; renderQuickPick(); };
            container.querySelectorAll('[data-policy-id]').forEach((row) => {
                row.onclick = async () => {
                    const nextPolicy = row.getAttribute('data-policy-id');
                    const swarmApi = getSwarmApi();
                    if (swarmApi) {
                        await swarmApi('set_failover_policy', { policy: nextPolicy });
                    }
                    failoverPolicy = nextPolicy;
                    showToast(`Сохранено: <b>${escapeHtml(FAILOVER_POLICY_LABELS[nextPolicy] || nextPolicy)}</b>`, false, 3500);
                    currentMenuLevel = 'main';
                    renderQuickPick();
                };
            });
        } else if (currentMenuLevel === 'model_config') {
            const customModels = (modelConfig && modelConfig.custom_models && typeof modelConfig.custom_models === 'object')
                ? modelConfig.custom_models : {};
            const workerModel = (modelConfig && modelConfig.worker_model) || 'flash';
            const orchOrder = (modelConfig && Array.isArray(modelConfig.orchestrator_order) && modelConfig.orchestrator_order.length)
                ? modelConfig.orchestrator_order.join(' → ')
                : 'Claude Sonnet/Opus → GPT-OSS → Gemini Pro (по умолчанию)';
            let rowsHtml = `
                <div class="ag-qp-row" data-action="back_to_main">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#007acc; font-size:14px;">←</span>
                        <span class="ag-qp-row-title">Назад в главное меню</span>
                    </div>
                </div>
                <div class="ag-qp-row" data-model-op="info_only">
                    <div class="ag-qp-row-left">
                        <span class="ag-qp-row-title">Архитектор (план задач): только умные модели</span>
                        <span class="ag-qp-row-desc">${escapeHtml(orchOrder)}</span>
                    </div>
                </div>
                <div class="ag-qp-row" data-model-op="cycle_worker_model">
                    <div class="ag-qp-row-left">
                        <span class="ag-qp-row-title">Модель для исполнителей (воркеров): ${escapeHtml(workerModel)}</span>
                        <span class="ag-qp-row-desc">Нажмите, чтобы переключить: flash (быстрая) ↔ claude-sonnet (умная) ↔ auto</span>
                    </div>
                </div>
                <div class="ag-qp-row" data-model-op="custom_info">
                    <div class="ag-qp-row-left">
                        <span class="ag-qp-row-title">Пользовательские модели: ${Object.keys(customModels).length} шт.</span>
                        <span class="ag-qp-row-desc">Для стандартных моделей Antigravity настройка не требуется</span>
                    </div>
                </div>
                <div class="ag-qp-row" data-model-op="reset_models">
                    <div class="ag-qp-row-left">
                        <span class="ag-qp-row-title" style="color:#f87171;">Сбросить настройки моделей на стандартные</span>
                        <span class="ag-qp-row-desc">Вернёт встроенные правила (умные планируют, быстрая Flash проверяет)</span>
                    </div>
                </div>
            `;
            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" value="Доп. настройки моделей (для стандартных Claude/Gemini/Flash менять не нужно)" readonly spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">${rowsHtml}</div>
            `;
            const backRow = container.querySelector('[data-action="back_to_main"]');
            if (backRow) backRow.onclick = () => { currentMenuLevel = 'main'; renderQuickPick(); };
            container.querySelectorAll('[data-model-op]').forEach((row) => {
                row.onclick = async () => {
                    const op = row.getAttribute('data-model-op');
                    const swarmApi = getSwarmApi();
                    if (!swarmApi) return;
                    if (op === 'cycle_worker_model') {
                        const order = ['flash', 'claude-sonnet', 'auto'];
                        const idx = order.indexOf(String(workerModel).toLowerCase());
                        const nextWorker = order[(idx + 1) % order.length];
                        const nextCfg = Object.assign({}, modelConfig || {}, { worker_model: nextWorker });
                        const res = await swarmApi('save_model_config', { config: nextCfg });
                        if (res && res.ok) {
                            modelConfig = res.modelConfig || nextCfg;
                            showToast(`Модель воркеров изменена на <b>${escapeHtml(nextWorker)}</b>`, false, 3500);
                            renderQuickPick();
                        }
                    } else if (op === 'reset_models') {
                        const res = await swarmApi('reset_model_config');
                        if (res && res.ok) {
                            modelConfig = res.modelConfig || {};
                            showToast('Настройки моделей сброшены на стандартные.', false, 3500);
                            renderQuickPick();
                        }
                    } else if (op === 'custom_info') {
                        showToast('Сторонние модели задаются через Antigravity IDE или файл ~/.antigravity_tools/model_config.json', false, 4500);
                    }
                };
            });
        } else if (currentMenuLevel === 'accounts') {
            currentFilterText = filterText || '';
            const q = currentFilterText.toLowerCase().trim();
            const filtered = accountsList.filter(a => {
                if (!q) return true;
                return (a.name || '').toLowerCase().includes(q) || (a.email || '').toLowerCase().includes(q);
            });

            const activeId = activeAccount ? activeAccount.id : '';
            const activeEmail = activeAccount ? (activeAccount.email || '').toLowerCase() : '';

            const countBadgeText = q
                ? `${filtered.length} / ${accountsList.length}`
                : `Аккаунтов: ${accountsList.length}`;
            let rowsHtml = `
                <div class="ag-qp-row" data-action="back_to_main">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#007acc; font-size:14px;">←</span>
                        <span class="ag-qp-row-title">Назад в главное меню</span>
                    </div>
                    <span class="ag-acc-count-badge">${escapeHtml(countBadgeText)}</span>
                </div>
                <div class="ag-qp-row" data-action="add_google_account">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#4ade80; font-size:15px;">+</span>
                        <span class="ag-qp-row-title" style="color:#4ade80;">Добавить Google-аккаунт</span>
                        <span class="ag-qp-row-desc">Войти через браузер и добавить в список</span>
                    </div>
                </div>
            `;

            for (const acc of filtered) {
                const isActive = (acc.id === activeId) || (acc.email && acc.email.toLowerCase() === activeEmail);
                const cPct = (acc.claude_percentage !== undefined && acc.claude_percentage !== null) ? acc.claude_percentage : 0;
                const gPct = (acc.gemini_percentage !== undefined && acc.gemini_percentage !== null) ? acc.gemini_percentage : 100;
                const aPct = (acc.argon_percentage !== undefined && acc.argon_percentage !== null) ? acc.argon_percentage : null;
                const claudeNumStyle = cPct === 0 ? 'color: #ff5555; font-weight: bold;' : '';
                const argonPart = aPct !== null
                    ? `<span class="ag-pct-num" style="color:#93c5fd;">A${aPct}%</span>`
                    : `<span class="ag-pct-num" style="color:#666666;">A—</span>`;
                const gemTooltip = aPct !== null
                    ? `Gemini (Hydrogen / Flash): ${gPct}% | Gemini 4 Argon: ${aPct}%`
                    : `Gemini (Hydrogen / Flash): ${gPct}% | Gemini 4 Argon: нет отд. квоты`;

                rowsHtml += `
                    <div class="ag-qp-row ${isActive ? 'selected' : ''}" data-acc-id="${escapeHtml(acc.id)}">
                        <div class="ag-qp-row-left">
                            ${renderAvatarHtml(acc, 18)}
                            <span class="ag-qp-row-title">${escapeHtml(acc.name || acc.email)}</span>
                            <span class="ag-qp-row-desc">${escapeHtml(acc.email || '')}</span>
                            ${isActive ? '<span class="ag-active-badge">активен</span>' : ''}
                        </div>
                        <div class="ag-pill-quotas" title="${escapeHtml(gemTooltip)}">
                            [ <span class="ag-sym-claude">${CLAUDE_SVG}</span> <span class="ag-pct-num" style="${claudeNumStyle}">${cPct}%</span> | <span class="ag-sym-gemini">${GEMINI_SVG}</span> <span class="ag-pct-num">G${gPct}%</span>·${argonPart} ]
                        </div>
                    </div>
                `;
            }

            container.dataset.accountsSig = getAccountsRenderSig();
            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" placeholder="Выберите Google-аккаунт (всего: ${accountsList.length})..." value="${escapeHtml(filterText)}" autocomplete="off" spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">${rowsHtml}</div>
            `;

            const input = document.getElementById('ag-qp-input');
            if (input) {
                input.focus();
                input.oninput = (e) => {
                    renderQuickPick(e.target.value);
                };
            }

            const backRow = container.querySelector('[data-action="back_to_main"]');
            if (backRow) {
                backRow.onclick = () => {
                    currentMenuLevel = 'main';
                    renderQuickPick();
                };
            }

            const addAccRow = container.querySelector('[data-action="add_google_account"]');
            if (addAccRow) {
                addAccRow.onclick = () => {
                    void triggerAddAccount();
                };
            }

            const accRows = container.querySelectorAll('[data-acc-id]');
            accRows.forEach(r => {
                r.onclick = () => {
                    const accId = r.getAttribute('data-acc-id');
                    switchAccount(accId);
                };
            });
        } else if (currentMenuLevel === 'swarm_checkpoints' || currentMenuLevel === 'swarm_hub') {
            currentFilterText = filterText || '';
            const q = currentFilterText.toLowerCase().trim();
            const list = Array.isArray(swarmCheckpoints) ? swarmCheckpoints : [];
            const filtered = list.filter((cp) => {
                if (!q) return true;
                const hay = [
                    cp.task_id,
                    cp.user_request,
                    cp.project_path,
                    cp.current_node,
                    cp.final_branch,
                ].filter(Boolean).join(' ').toLowerCase();
                return hay.includes(q);
            });

            const activeList = filtered.filter((c) => c && !c.is_complete && c.current_node !== 'error' && c.current_node !== 'failed');
            const historyList = filtered.filter((c) => c && (c.is_complete || c.current_node === 'error' || c.current_node === 'failed'));

            let currentProj = null;
            if (Array.isArray(swarmProjects)) {
                currentProj = swarmProjects.find((p) => p && (p.isCurrent || p.current));
            }
            if (!currentProj && swarmSelectedProjectPath) {
                currentProj = (Array.isArray(swarmProjects) ? swarmProjects : []).find((p) => p && p.path === swarmSelectedProjectPath)
                    || { path: swarmSelectedProjectPath, name: getPathBasename(swarmSelectedProjectPath) };
            }
            const currentProjectPath = currentProj ? (currentProj.path || '') : '';
            const currProjName = currentProj ? (currentProj.name || getPathBasename(currentProjectPath) || 'проект') : '';

            let launchSectionHtml = '';
            if (currentProjectPath) {
                launchSectionHtml += `
                    <div class="ag-qp-row" data-action="quick_task_recent" data-project-path="${escapeHtml(currentProjectPath)}">
                        <div class="ag-qp-row-left">
                            <span>⚡</span>
                            <span class="ag-qp-row-title">В текущем проекте: ${escapeHtml(currProjName)}</span>
                            <span class="ag-qp-row-desc">${escapeHtml(currentProjectPath)}</span>
                        </div>
                    </div>
                `;
            }

            const recentProjects = (Array.isArray(swarmProjects) ? swarmProjects : [])
                .filter((p) => p && p.path && (!currentProjectPath || p.path !== currentProjectPath))
                .slice(0, 5);

            for (const p of recentProjects) {
                if (!p || !p.path) continue;
                const pName = p.name || getPathBasename(p.path);
                launchSectionHtml += `
                    <div class="ag-qp-row" data-action="quick_task_recent" data-project-path="${escapeHtml(p.path)}">
                        <div class="ag-qp-row-left">
                            <span>📁</span>
                            <span class="ag-qp-row-title">${escapeHtml(pName)}</span>
                            <span class="ag-qp-row-desc">${escapeHtml(p.path)}</span>
                        </div>
                    </div>
                `;
            }

            launchSectionHtml += `
                <div class="ag-qp-row" data-action="pick_custom_project">
                    <div class="ag-qp-row-left">
                        <span>📂</span>
                        <span class="ag-qp-row-title">Выбрать другую папку на диске...</span>
                        <span class="ag-qp-row-desc">Открыть проводник для выбора проекта</span>
                    </div>
                </div>
            `;

            let rowsHtml = `
                <div class="ag-qp-row" data-action="back_to_main">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#007acc; font-size:14px;">←</span>
                        <span class="ag-qp-row-title">Назад в главное меню</span>
                    </div>
                </div>
                ${launchSectionHtml}
                <div style="display:none;" data-action="quick_task"></div>
                <div style="display:none;" data-action="open_new_swarm"></div>
            `;

            const renderTaskRow = (cp) => {
                const taskId = String(cp.task_id || '');
                const done = Number(cp.done_workers || 0);
                const total = Number(cp.total_workers || (Array.isArray(cp.workers) ? cp.workers.length : 0));
                const running = Number(cp.running_workers || (Array.isArray(cp.workers) ? cp.workers.filter((w) => w.status === 'running' || w.status === 'working').length : 0));
                const errCount = Array.isArray(cp.errors) ? cp.errors.length : 0;
                const projName = cp.project_name || getPathBasename(cp.project_path || '') || 'project';
                const stageLabel = formatSwarmStage(cp);
                const orchModel = cp.orchestrator_model || 'Модель чата (умная)';
                const wModels = Array.isArray(cp.worker_models) && cp.worker_models.length
                    ? cp.worker_models.join(', ')
                    : 'flash';
                const reqPreview = String(cp.user_request || '').replace(/\s+/g, ' ').trim();
                const reqShort = reqPreview.length > 56 ? `${reqPreview.slice(0, 56)}…` : reqPreview;

                const startMs = cp.created_at_ms || cp.timestamp_ms || 0;
                const lastMs = cp.updated_at_ms || cp.latest_activity_ms || startMs;
                const elapsedSec = cp.is_complete
                    ? Math.max(0, Math.floor((lastMs - startMs) / 1000))
                    : (startMs > 0 ? Math.max(0, Math.floor((Date.now() - startMs) / 1000)) : 0);
                const elapsedStr = elapsedSec > 0 ? ` · ⏱ ${formatElapsed(elapsedSec)}` : '';

                let badgeClass = 'ag-swarm-status-running';
                let badgeLabel = '';
                let statusDesc = '';

                if (cp.is_complete) {
                    badgeClass = 'ag-swarm-status-done';
                    badgeLabel = total > 0 ? `Готово ✓ (${done}/${total})` : 'Готово ✓';
                    statusDesc = total > 0 ? `Завершено: ${done}/${total}` : 'Завершено';
                } else if (errCount > 0) {
                    badgeClass = 'ag-swarm-status-err';
                    badgeLabel = `Ошибка (${errCount})`;
                    statusDesc = `Ошибок: ${errCount}`;
                } else if (running > 0) {
                    badgeClass = 'ag-swarm-status-running';
                    badgeLabel = `⚡ ${running}/${total} в работе`;
                    statusDesc = `В работе: ${running}/${total} (${wModels})`;
                } else if (total === 0 || stageLabel === 'Планирование' || cp.current_node === 'DECOMPOSE') {
                    badgeClass = 'ag-swarm-status-running';
                    badgeLabel = `⏳ Планирование`;
                    statusDesc = `Формирование команды`;
                } else {
                    badgeClass = 'ag-swarm-status-running';
                    badgeLabel = total > 0 ? `${stageLabel} (${done}/${total})` : stageLabel;
                    statusDesc = `Агенты: ${done}/${total} (${wModels})`;
                }

                return `
                    <div class="ag-qp-row" data-swarm-task="${escapeHtml(taskId)}" data-swarm-project="${escapeHtml(cp.project_path || '')}">
                        <div class="ag-qp-row-left">
                            ${RESUME_SVG}
                            <span class="ag-qp-row-title">${escapeHtml(projName)}${reqShort ? ` — ${escapeHtml(reqShort)}` : ` (${escapeHtml(taskId)})`}</span>
                            <span class="ag-qp-row-desc">${escapeHtml(stageLabel)}${elapsedStr} · ${escapeHtml(statusDesc)} · ${escapeHtml(orchModel)}</span>
                        </div>
                        <span class="ag-swarm-status-badge ${badgeClass}">${escapeHtml(badgeLabel)}</span>
                        <button type="button" class="ag-qp-del-btn" data-cleanup-task="${escapeHtml(taskId)}" data-cleanup-project="${escapeHtml(cp.project_path || '')}" title="Удалить временные папки и задачу">✕</button>
                    </div>
                `;
            };

            if (activeList.length > 0) {
                rowsHtml += `<div style="padding:6px 14px 2px; font-size:11px; color:#888; text-transform:uppercase; font-weight:600;">Активные задачи</div>`;
                for (const cp of activeList) {
                    rowsHtml += renderTaskRow(cp);
                }
            }

            if (historyList.length > 0) {
                rowsHtml += `<div style="padding:8px 14px 2px; font-size:11px; color:#888; text-transform:uppercase; font-weight:600;">История</div>`;
                for (const cp of historyList) {
                    rowsHtml += renderTaskRow(cp);
                }
            }

            if (filtered.length === 0) {
                rowsHtml += `
                    <div class="ag-qp-row" data-action="empty_swarm_hint">
                        <div class="ag-qp-row-left">
                            <span class="ag-qp-row-title" style="color:#9ca3af;">Задачи Swarm не найдены</span>
                            <span class="ag-qp-row-desc">${list.length > 0 ? 'Сбросьте строку фильтра' : 'Запустите новую задачу выше или прямо в чате'}</span>
                        </div>
                    </div>
                `;
            }

            container.dataset.accountsSig = '';
            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" placeholder="Фильтр чекпоинтов Swarm (task_id, проект, задача)..." value="${escapeHtml(filterText)}" autocomplete="off" spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">${rowsHtml}</div>
            `;

            const filterInput = document.getElementById('ag-qp-input');
            if (filterInput) {
                filterInput.focus();
                if (typeof filterInput.setSelectionRange === 'function') {
                    const len = filterInput.value.length;
                    filterInput.setSelectionRange(len, len);
                }
                filterInput.oninput = (e) => {
                    renderQuickPick(e.target.value);
                };
            }

            const backRow = container.querySelector('[data-action="back_to_main"]');
            if (backRow) {
                backRow.onclick = () => {
                    currentMenuLevel = 'main';
                    renderQuickPick();
                };
            }

            const quickTaskRow = container.querySelector('[data-action="quick_task"]');
            if (quickTaskRow) {
                quickTaskRow.onclick = () => {
                    swarmQuickProject = currentProjectPath || (recentProjects[0] && recentProjects[0].path) || '';
                    currentMenuLevel = 'swarm_quick_input';
                    renderQuickPick();
                };
            }

            container.querySelectorAll('[data-action="quick_task_recent"]').forEach((el) => {
                el.onclick = () => {
                    const pPath = el.getAttribute('data-project-path');
                    swarmQuickProject = pPath || '';
                    currentMenuLevel = 'swarm_quick_input';
                    renderQuickPick();
                };
            });

            const pickProjRow = container.querySelector('[data-action="pick_custom_project"]');
            if (pickProjRow) {
                pickProjRow.onclick = async () => {
                    const swarmApi = getSwarmApi();
                    if (!swarmApi) return;
                    const defaultPath = swarmQuickProject || swarmSelectedProjectPath || '';
                    const picked = await swarmApi('pick_project', { defaultPath });
                    if (picked && picked.ok && !picked.canceled && picked.projectPath) {
                        swarmSelectedProjectPath = picked.projectPath;
                        if (Array.isArray(picked.projects)) swarmProjects = picked.projects;
                        swarmQuickProject = picked.projectPath;
                        currentMenuLevel = 'swarm_quick_input';
                        renderQuickPick();
                    }
                };
            }

            const newSwarmRow = container.querySelector('[data-action="open_new_swarm"]');
            if (newSwarmRow) {
                newSwarmRow.onclick = () => {
                    closeQuickPick();
                    openSwarmRunModal();
                };
            }

            const delBtns = container.querySelectorAll('[data-cleanup-task]');
            delBtns.forEach((btn) => {
                btn.onclick = async (e) => {
                    if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
                    const tId = btn.getAttribute('data-cleanup-task');
                    const pPath = btn.getAttribute('data-cleanup-project') || '';
                    await cleanupDesktopSwarmTask(tId, pPath);
                    renderQuickPick(currentFilterText);
                };
            });

            const cpRows = container.querySelectorAll('[data-swarm-task]');
            cpRows.forEach((row) => {
                row.onclick = (e) => {
                    if (e && e.target && typeof e.target.getAttribute === 'function' && e.target.getAttribute('data-cleanup-task')) return;
                    const tId = row.getAttribute('data-swarm-task');
                    const match = (Array.isArray(swarmCheckpoints) ? swarmCheckpoints : []).find((c) => c && c.task_id === tId);
                    if (match) {
                        currentSelectedSwarmTask = match;
                        currentMenuLevel = 'swarm_session_detail';
                        renderQuickPick();
                    }
                };
            });
        } else if (currentMenuLevel === 'swarm_session_detail') {
            const cp = currentSelectedSwarmTask;
            if (!cp) {
                currentMenuLevel = 'swarm_checkpoints';
                renderQuickPick();
                return;
            }

            const taskId = String(cp.task_id || '');
            const projName = cp.project_name || getPathBasename(cp.project_path || '') || 'проект';
            const reqText = String(cp.user_request || '').trim();
            const stageLabel = formatSwarmStage(cp);
            const done = Number(cp.done_workers || 0);
            const total = Number(cp.total_workers || (Array.isArray(cp.workers) ? cp.workers.length : 0));
            const orchModel = cp.orchestrator_model || 'умная модель';
            const boardCount = cp.board_count != null ? cp.board_count : (Array.isArray(cp.messages) ? cp.messages.length : (Array.isArray(cp.board) ? cp.board.length : 0));

            const statusCounts = total > 0 ? ` (${done}/${total})` : (stageLabel === 'Планирование' || cp.current_node === 'DECOMPOSE' ? ' · Формирование плана' : '');
            let rowsHtml = `
                <div class="ag-qp-row" data-action="back_to_swarm_list">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#007acc; font-size:14px;">←</span>
                        <span class="ag-qp-row-title">Назад к списку задач</span>
                    </div>
                </div>
                <div style="padding:10px 14px; border-bottom:1px solid #2d2d2d; font-size:12px; color:#d4d4d4;">
                    <div style="font-weight:600; color:#fff; font-size:13px; margin-bottom:4px;">📁 ${escapeHtml(projName)} — ${escapeHtml(reqText || 'Задача Swarm')}</div>
                    <div style="color:#888;">Статус: <b style="color:#e5e7eb;">${escapeHtml(stageLabel)}</b>${statusCounts} · Модель: <b>${escapeHtml(orchModel)}</b></div>
                </div>
                <div style="padding:6px 14px 2px; font-size:11px; color:#888; text-transform:uppercase; font-weight:600;">Агенты и файлы</div>
            `;

            const workers = Array.isArray(cp.workers) ? cp.workers : [];
            const workerStatusIcon = (s) => (s === 'done' ? '✅' : (s === 'running' ? '⏳' : (s === 'failed' ? '❌' : '⚪')));
            if (workers.length > 0) {
                for (const w of workers) {
                    const statusIcon = workerStatusIcon(w.status);
                    const desc = w.task || (w.status === 'done' ? 'выполнено' : 'в работе');
                    rowsHtml += `
                        <div class="ag-qp-row" style="cursor:default;">
                            <div class="ag-qp-row-left">
                                <span>${statusIcon}</span>
                                <span class="ag-qp-row-title"><b>${escapeHtml(w.name)}</b> (${escapeHtml(w.model || 'flash')})</span>
                                <span class="ag-qp-row-desc">${escapeHtml(desc)}</span>
                            </div>
                        </div>
                    `;
                    const files = Array.isArray(w.files) ? w.files : [];
                    for (const f of files) {
                        rowsHtml += `
                            <div class="ag-qp-row ag-qp-file-row" data-copy-path="${escapeHtml(f)}" style="padding-left:34px; cursor:pointer;" title="Клик — скопировать путь">
                                <div class="ag-qp-row-left">
                                    <span>📄</span>
                                    <span class="ag-qp-row-title" style="color:#60a5fa; text-decoration:underline;">${escapeHtml(f)}</span>
                                    <span class="ag-qp-row-desc" style="margin-left:auto; font-size:10px;">(копировать путь)</span>
                                </div>
                            </div>
                        `;
                    }
                }
            } else {
                rowsHtml += `<div class="ag-qp-row" style="cursor:default;"><div class="ag-qp-row-left"><span class="ag-tt-muted">Агенты ещё не назначены</span></div></div>`;
            }

            rowsHtml += `
                <div class="ag-qp-row" data-action="open_board_messages">
                    <div class="ag-qp-row-left">
                        <span>💬</span>
                        <span class="ag-qp-row-title">💬 Переписка агентов (${boardCount} сообщений)</span>
                        <span class="ag-qp-row-desc">Открыть журнал board.jsonl</span>
                    </div>
                </div>
                <div class="ag-qp-row" data-action="resume_in_chat">
                    <div class="ag-qp-row-left">
                        <span style="color:#38bdf8;">▶</span>
                        <span class="ag-qp-row-title" style="color:#38bdf8;">▶ Продолжить в чате</span>
                    </div>
                </div>
                <div class="ag-qp-row" data-action="cleanup_in_detail">
                    <div class="ag-qp-row-left">
                        <span style="color:#ef4444;">🗑</span>
                        <span class="ag-qp-row-title" style="color:#ef4444;">🗑 Удалить</span>
                    </div>
                </div>
            `;

            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" value="Сессия Swarm: ${escapeHtml(projName)} (${escapeHtml(taskId)})" readonly spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">${rowsHtml}</div>
            `;

            const backRow = container.querySelector('[data-action="back_to_swarm_list"]');
            if (backRow) backRow.onclick = () => { currentMenuLevel = 'swarm_checkpoints'; renderQuickPick(); };

            container.querySelectorAll('[data-copy-path]').forEach((row) => {
                row.onclick = () => {
                    const filePath = row.getAttribute('data-copy-path');
                    copyTextToClipboard(filePath);
                    showToast(`Путь скопирован: <b>${escapeHtml(filePath)}</b>`, false, 3000);
                };
            });

            const openBoardRow = container.querySelector('[data-action="open_board_messages"]');
            if (openBoardRow) {
                openBoardRow.onclick = async () => {
                    swarmBoardMessages = Array.isArray(cp.messages) ? cp.messages : (Array.isArray(cp.board) ? cp.board : []);
                    const swarmApi = getSwarmApi();
                    if (swarmApi && swarmBoardMessages.length === 0) {
                        try {
                            const res = await swarmApi('board', { swarmId: cp.swarm_id || cp.task_id, taskId: cp.task_id, projectPath: cp.project_path });
                            if (res && res.board && Array.isArray(res.board.messages)) {
                                swarmBoardMessages = res.board.messages;
                            }
                        } catch (_) {}
                    }
                    currentMenuLevel = 'swarm_board_messages';
                    renderQuickPick();
                };
            }

            const resumeRow = container.querySelector('[data-action="resume_in_chat"]');
            if (resumeRow) {
                resumeRow.onclick = async () => {
                    closeQuickPick();
                    await resumeDesktopSwarmTask(cp.task_id, cp.project_path);
                };
            }

            const cleanupRow = container.querySelector('[data-action="cleanup_in_detail"]');
            if (cleanupRow) {
                cleanupRow.onclick = async () => {
                    await cleanupDesktopSwarmTask(cp.task_id, cp.project_path);
                    currentSelectedSwarmTask = null;
                    currentMenuLevel = 'swarm_checkpoints';
                    renderQuickPick();
                };
            }
        } else if (currentMenuLevel === 'swarm_board_messages') {
            const cp = currentSelectedSwarmTask;
            const projName = cp ? (cp.project_name || getPathBasename(cp.project_path || '') || 'проект') : 'задаче';
            const msgs = Array.isArray(swarmBoardMessages) ? swarmBoardMessages : [];

            let rowsHtml = `
                <div class="ag-qp-row" data-action="back_to_session_detail">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#007acc; font-size:14px;">←</span>
                        <span class="ag-qp-row-title">Назад к сессии ${escapeHtml(projName)}</span>
                    </div>
                </div>
                <div style="padding:8px 14px; border-bottom:1px solid #2d2d2d; font-size:12px; color:#d4d4d4;">
                    <span style="font-weight:600; color:#fff;">💬 Сообщения swarm board.jsonl (${msgs.length})</span>
                </div>
            `;

            if (msgs.length === 0) {
                rowsHtml += `<div class="ag-qp-row" style="cursor:default;"><div class="ag-qp-row-left"><span class="ag-tt-muted">Сообщений на доске пока нет.</span></div></div>`;
            } else {
                for (const m of msgs) {
                    const seq = m.seq != null ? `#${m.seq}` : '';
                    const agent = m.agent ? `@${m.agent}` : '@agent';
                    const time = m.time ? escapeHtml(m.time) : '';
                    const text = escapeHtml(String(m.text || ''));
                    rowsHtml += `
                        <div class="ag-qp-row" style="cursor:default; display:flex; flex-direction:column; align-items:flex-start; gap:4px; padding:8px 14px;">
                            <div style="display:flex; gap:8px; align-items:center; width:100%;">
                                <span style="font-size:10px; color:#888;">${escapeHtml(seq)}</span>
                                <span style="font-weight:600; color:#a78bfa;">${escapeHtml(agent)}</span>
                                ${time ? `<span style="font-size:10px; color:#666; margin-left:auto;">${time}</span>` : ''}
                            </div>
                            <div style="font-size:11.5px; color:#e5e7eb; white-space:pre-wrap; word-break:break-word;">${text}</div>
                        </div>
                    `;
                }
            }

            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" value="Переписка агентов: ${escapeHtml(projName)}" readonly spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">${rowsHtml}</div>
            `;

            const backRow = container.querySelector('[data-action="back_to_session_detail"]');
            if (backRow) {
                backRow.onclick = () => {
                    currentMenuLevel = 'swarm_session_detail';
                    renderQuickPick();
                };
            }
        } else if (currentMenuLevel === 'swarm_quick_select_project') {
            const list = Array.isArray(swarmProjects) ? swarmProjects : [];
            let rowsHtml = `
                <div class="ag-qp-row" data-action="back_to_swarm_list">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#007acc; font-size:14px;">←</span>
                        <span class="ag-qp-row-title">Назад</span>
                    </div>
                </div>
            `;

            const seenPaths = new Set();
            for (const p of list) {
                if (!p || !p.path || seenPaths.has(p.path)) continue;
                seenPaths.add(p.path);
                rowsHtml += `
                    <div class="ag-qp-row" data-select-project="${escapeHtml(p.path)}">
                        <div class="ag-qp-row-left">
                            <span>📁</span>
                            <span class="ag-qp-row-title">${escapeHtml(p.name || getPathBasename(p.path))}</span>
                            <span class="ag-qp-row-desc">${escapeHtml(p.path)}</span>
                        </div>
                    </div>
                `;
            }
            for (const cp of (Array.isArray(swarmCheckpoints) ? swarmCheckpoints : [])) {
                if (cp && cp.project_path && !seenPaths.has(cp.project_path)) {
                    seenPaths.add(cp.project_path);
                    rowsHtml += `
                        <div class="ag-qp-row" data-select-project="${escapeHtml(cp.project_path)}">
                            <div class="ag-qp-row-left">
                                <span>📁</span>
                                <span class="ag-qp-row-title">${escapeHtml(cp.project_name || getPathBasename(cp.project_path))}</span>
                                <span class="ag-qp-row-desc">${escapeHtml(cp.project_path)}</span>
                            </div>
                        </div>
                    `;
                }
            }

            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" placeholder="Выберите проект для быстрой задачи..." readonly spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">${rowsHtml}</div>
            `;

            const backRow = container.querySelector('[data-action="back_to_swarm_list"]');
            if (backRow) backRow.onclick = () => { currentMenuLevel = 'swarm_checkpoints'; renderQuickPick(); };

            container.querySelectorAll('[data-select-project]').forEach((row) => {
                row.onclick = () => {
                    swarmQuickProject = row.getAttribute('data-select-project');
                    currentMenuLevel = 'swarm_quick_input';
                    renderQuickPick();
                };
            });
        } else if (currentMenuLevel === 'swarm_quick_input') {
            const projName = getPathBasename(swarmQuickProject) || swarmQuickProject;
            let rowsHtml = `
                <div class="ag-qp-row" data-action="back_to_quick_project">
                    <div class="ag-qp-row-left">
                        <span style="font-weight:bold; color:#007acc; font-size:14px;">←</span>
                        <span class="ag-qp-row-title">Назад к выбору проекта</span>
                    </div>
                </div>
                <div class="ag-qp-row" style="cursor:default;">
                    <div class="ag-qp-row-left">
                        <span class="ag-tt-muted">Проект: <b>${escapeHtml(projName)}</b>. Введите текст задачи и нажмите <b>Enter</b> для отправки в чат.</span>
                    </div>
                </div>
            `;

            container.innerHTML = `
                <div id="ag-qp-input-wrapper">
                    <input type="text" id="ag-qp-input" placeholder="Опишите задачу (Enter — отправить в чат)" value="" autocomplete="off" spellcheck="false" />
                </div>
                <div id="ag-qp-items-list">${rowsHtml}</div>
            `;

            const backRow = container.querySelector('[data-action="back_to_quick_project"]');
            if (backRow) backRow.onclick = () => { currentMenuLevel = 'swarm_checkpoints'; renderQuickPick(); };

            const taskInput = document.getElementById('ag-qp-input');
            if (taskInput) {
                taskInput.focus();
                taskInput.onkeydown = async (e) => {
                    if (e.key === 'Enter') {
                        const text = taskInput.value.trim();
                        if (!text) return;
                        closeQuickPick();
                        const swarmApi = getSwarmApi();
                        if (swarmApi) {
                            try {
                                const res = await swarmApi('prepare', {
                                    projectPath: swarmQuickProject,
                                    request: text,
                                    useAffectedTests: true
                                });
                                if (res && res.ok) {
                                    dispatchPreparedSwarmResult('Быстрая задача', res);
                                } else {
                                    showToast(`Ошибка: ${(res && res.error) || 'не удалось подготовить задачу'}`, true, 4500);
                                }
                            } catch (err) {
                                showToast(`Ошибка: ${(err && err.message) || String(err)}`, true, 4500);
                            }
                        }
                    }
                };
            }
        }

        const input = document.getElementById('ag-qp-input');
        if (input && currentMenuLevel === 'main') {
            input.focus();
        }
    }

    async function refreshNetwork() {
        if (isRefreshingNetwork || !window.agAPI || !window.agAPI.refresh) return;
        isRefreshingNetwork = true;
        renderPillContent();
        showToast('🔄 Обновление VPN, прокси и сетевого соединения...', false, 8000);

        try {
            const res = await window.agAPI.refresh('network');
            const hasNetworkPayload = Boolean(res && res.network && typeof res.network === 'object');
            await fetchData();
            if (hasNetworkPayload) {
                const net = res.network;
                connOk = Boolean(net.ok);
                connPing = Number(net.ms || 0);
                connCountryCode = net.countryCode || '';
                connCountry = net.country || (connOk ? 'Онлайн' : 'Офлайн');
                connStatusText = net.status || (connOk ? `${connPing}ms` : 'Офлайн');
                connProxyUrl = net.proxyUrl || null;
                saveCachedNet();
            }
            if (res && res.activeAccount) {
                activeAccount = res.activeAccount;
                saveCachedAccount(activeAccount);
            }
            renderPillContent();

            if (res && res.ok === false) {
                showToast(`⚠ Ошибка обновления сети: ${escapeHtml(res.error || 'не удалось выполнить проверку')}`, true, 5500);
                return;
            }
            if (!hasNetworkPayload) {
                showToast(
                    '⚠ <strong>Квоты обновлены</strong>, но мост <code>app.asar</code> в Antigravity 2.0 устарел.<br><span style="color:#fcd34d; font-size:11px;">Закройте Antigravity 2.0 и запустите обновлённый Antigravity Patcher один раз.</span>',
                    true,
                    6500
                );
                return;
            }

            const proxyDesc = connProxyUrl
                ? `Прокси: ${escapeHtml(connProxyUrl)}`
                : 'Прямое подключение (без системного прокси)';
            const backendDesc = res.backendRestarted
                ? '<br><span style="color:#6ee7b7; font-size:11px;">Сервер агента (LS) и сокеты сессии перезапущены с новой сетью</span>'
                : (res.backendNote ? `<br><span style="color:#9ca3af; font-size:11px;">${escapeHtml(res.backendNote)}</span>` : '');
            if (connOk) {
                const flagSvg = getCountryFlagSvg(connCountryCode);
                showToast(
                    `🌐 <strong>Сеть обновлена!</strong> ${flagSvg} ${escapeHtml(connCountry)} (${connPing} ms)<br><span style="color:#9ca3af; font-size:11px;">${proxyDesc}</span>${backendDesc}`,
                    false,
                    5000
                );
            } else {
                showToast(
                    `⚠ <strong>Сеть недоступна:</strong> ${escapeHtml(connStatusText || 'Офлайн / таймаут')}<br><span style="color:#fca5a5; font-size:11px;">${proxyDesc}</span>${backendDesc}`,
                    true,
                    5500
                );
            }
        } catch (e) {
            connOk = false;
            connStatusText = 'Ошибка проверки сети';
            showToast(`⚠ Ошибка обновления сети: ${escapeHtml((e && e.message) || String(e))}`, true, 5000);
        } finally {
            isRefreshingNetwork = false;
            renderPillContent();
            const tt = document.getElementById('ag-ide-tooltip');
            if (tt && tt.style.display === 'flex') {
                renderTooltipContent(tt);
            }
        }
    }

    function getSwarmApi() {
        return (window.agAPI && typeof window.agAPI.swarm === 'function') ? window.agAPI.swarm : null;
    }

    function getPathBasename(rawPath) {
        if (!rawPath) return '';
        const normalized = String(rawPath).replace(/[\\/]+$/, '');
        const idx = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'));
        return idx >= 0 ? normalized.slice(idx + 1) : normalized;
    }

    function isInsideOwnOverlayNode(el) {
        let cur = el;
        while (cur) {
            if (cur.id && cur.id.startsWith('ag-') && cur.id !== 'ag-desktop-bar') {
                return true;
            }
            cur = cur.parentElement;
        }
        return false;
    }

    function injectPromptIntoDesktopChat(promptText, autoSend = true) {
        const text = String(promptText || '').trim();
        if (!text || typeof document.querySelectorAll !== 'function') {
            return { inserted: false, sent: false };
        }

        let candidates = [];
        try {
            candidates = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"], input[type="text"]'));
        } catch (_) {
            candidates = [];
        }

        let bestEl = null;
        let bestScore = -1;
        for (const el of candidates) {
            if (!el || isInsideOwnOverlayNode(el) || el.disabled || el.readOnly) continue;
            if (typeof window.getComputedStyle === 'function') {
                try {
                    const cs = window.getComputedStyle(el);
                    if (cs && (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0')) continue;
                } catch (_) {}
            }
            let rect = { width: 200, height: 40, top: 400 };
            if (typeof el.getBoundingClientRect === 'function') {
                const r = el.getBoundingClientRect();
                if (!r || r.width < 80 || r.height < 16) continue;
                rect = r;
            }

            let score = 0;
            const hint = [
                el.placeholder,
                typeof el.getAttribute === 'function' ? el.getAttribute('aria-label') : '',
                typeof el.getAttribute === 'function' ? el.getAttribute('data-placeholder') : '',
            ].filter(Boolean).join(' ').toLowerCase();

            if (/ask anything|message|chat|prompt|напиш|спрос/.test(hint)) score += 50;
            if (rect.top >= (window.innerHeight || 720) * 0.45) score += 20;
            if (el.tagName === 'TEXTAREA' || el.isContentEditable || (typeof el.getAttribute === 'function' && el.getAttribute('data-lexical-editor') === 'true')) {
                score += 10;
            }
            if (score > bestScore) {
                bestScore = score;
                bestEl = el;
            }
        }

        if (!bestEl) {
            return { inserted: false, sent: false };
        }

        if (typeof bestEl.focus === 'function') {
            try { bestEl.focus(); } catch (_) {}
        }

        let inserted = false;
        const tag = String(bestEl.tagName || '').toUpperCase();
        if (tag === 'TEXTAREA' || tag === 'INPUT') {
            const proto = tag === 'TEXTAREA' && typeof HTMLTextAreaElement !== 'undefined'
                ? HTMLTextAreaElement.prototype
                : (typeof HTMLInputElement !== 'undefined' ? HTMLInputElement.prototype : null);
            const desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
            if (desc && typeof desc.set === 'function') {
                try { desc.set.call(bestEl, text); } catch (_) { bestEl.value = text; }
            } else {
                bestEl.value = text;
            }
            inserted = bestEl.value === text;
        } else {
            try {
                if (typeof window.getSelection === 'function') {
                    const sel = window.getSelection();
                    if (sel && typeof sel.selectAllChildren === 'function') {
                        sel.selectAllChildren(bestEl);
                    }
                }
            } catch (_) {}
            if (typeof document.execCommand === 'function') {
                try {
                    document.execCommand('selectAll', false, null);
                    document.execCommand('delete', false, null);
                    document.execCommand('insertText', false, text);
                } catch (_) {}
            }
            const curText = String(bestEl.innerText || bestEl.textContent || '');
            if (!curText.includes(text.slice(0, Math.min(24, text.length)))) {
                bestEl.textContent = text;
            }
            inserted = true;
        }

        if (typeof bestEl.dispatchEvent === 'function') {
            try {
                if (typeof InputEvent === 'function') {
                    bestEl.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
                } else if (typeof Event === 'function') {
                    bestEl.dispatchEvent(new Event('input', { bubbles: true }));
                }
                if (typeof Event === 'function') {
                    bestEl.dispatchEvent(new Event('change', { bubbles: true }));
                }
            } catch (_) {}
        }

        let sent = false;
        if (autoSend && inserted) {
            let scope = bestEl.parentElement;
            for (let depth = 0; depth < 4 && scope && !sent; depth++) {
                if (typeof scope.querySelectorAll === 'function') {
                    const buttons = scope.querySelectorAll('button');
                    for (const btn of buttons) {
                        if (!btn || btn.disabled || isInsideOwnOverlayNode(btn)) continue;
                        const label = [
                            btn.type,
                            typeof btn.getAttribute === 'function' ? btn.getAttribute('aria-label') : '',
                            typeof btn.getAttribute === 'function' ? btn.getAttribute('title') : '',
                            typeof btn.getAttribute === 'function' ? btn.getAttribute('data-testid') : '',
                        ].filter(Boolean).join(' ').toLowerCase();
                        if (btn.type === 'submit' || /\b(send|submit)\b|отправ/.test(label)) {
                            if (typeof btn.click === 'function') {
                                btn.click();
                                sent = true;
                                break;
                            }
                        }
                    }
                }
                scope = scope.parentElement;
            }
        }

        return { inserted, sent };
    }

    async function loadAssistantSettingsState() {
        const swarmApi = getSwarmApi();
        if (!swarmApi) return { ok: false };
        try {
            const res = await swarmApi('get_settings');
            if (res && res.ok) {
                if (res.failoverPolicy) failoverPolicy = res.failoverPolicy;
                if (res.modelConfig && typeof res.modelConfig === 'object') modelConfig = res.modelConfig;
                swarmQuotaPlan = res.quotaPlan || null;
            }
            return res || { ok: false };
        } catch (_) {
            return { ok: false };
        }
    }

    async function loadSwarmState() {
        const swarmApi = getSwarmApi();
        if (!swarmApi) return { ok: false, error: 'Swarm API unavailable' };
        try {
            const prevMap = new Map();
            for (const cp of (Array.isArray(swarmCheckpoints) ? swarmCheckpoints : [])) {
                if (cp && cp.task_id) prevMap.set(cp.task_id, cp);
            }
            const res = await swarmApi('list');
            if (res && res.ok) {
                swarmSnapshot = res.monitor || null;
                swarmTooltipLines = Array.isArray(res.tooltipLines) ? res.tooltipLines : null;
                swarmCheckpoints = Array.isArray(res.checkpoints) ? res.checkpoints : [];
                swarmProjects = Array.isArray(res.projects) ? res.projects : [];
                swarmEngineAvailable = Boolean(res.enginePath || swarmEngineAvailable);
                if (!swarmSelectedProjectPath && swarmProjects.length > 0) {
                    const gitProj = swarmProjects.find((p) => p && p.isGit) || swarmProjects[0];
                    swarmSelectedProjectPath = gitProj ? (gitProj.path || '') : '';
                }
                for (const cp of swarmCheckpoints) {
                    const prev = prevMap.get(cp.task_id);
                    if (prev && !prev.is_complete && cp.is_complete) {
                        onSwarmTaskCompleted(cp);
                    }
                }
                renderSwarmPillContent();
            }
            return res || { ok: false };
        } catch (err) {
            return { ok: false, error: err && err.message ? err.message : String(err) };
        }
    }

    function stopDesktopSwarmWatcher() {
        if (swarmPollTimer) {
            clearInterval(swarmPollTimer);
            swarmPollTimer = null;
        }
        stopSwarmLivePoll();
    }

    function startDesktopSwarmWatcher(taskId, initialCheckpoint = null) {
        if (!taskId) return;
        stopDesktopSwarmWatcher();
        watchedSwarmTask = initialCheckpoint || {
            task_id: taskId,
            current_node: 'init',
            done_workers: 0,
            total_workers: 0,
            is_complete: false,
        };
        renderPillContent();
        renderSwarmPillContent();

        const swarmApi = getSwarmApi();
        if (!swarmApi) return;

        let pollInFlight = false;
        swarmPollTimer = setInterval(async () => {
            if (pollInFlight || (typeof document !== 'undefined' && document.hidden)) return;
            pollInFlight = true;
            try {
                const res = await swarmApi('status', { taskId });
                if (res && res.ok && res.checkpoint) {
                    watchedSwarmTask = res.checkpoint;
                    renderPillContent();
                renderSwarmPillContent();
                checkProactiveQuotaWarning();
                    renderSwarmPillContent();
                    if (res.checkpoint.is_complete) {
                        stopDesktopSwarmWatcher();
                        onSwarmTaskCompleted(res.checkpoint);
                    }
                } else if (res && res.ok && !res.checkpoint) {
                    stopDesktopSwarmWatcher();
                    watchedSwarmTask = null;
                    renderPillContent();
                renderSwarmPillContent();
                checkProactiveQuotaWarning();
                    renderSwarmPillContent();
                }
            } catch (_) {}
            finally {
                pollInFlight = false;
            }
        }, 4000);
    }

    function trySwitchDesktopProject(targetPath, targetName) {
        if (!targetPath && !targetName) return false;
        const cleanName = String(targetName || '').trim().toLowerCase();
        const cleanBase = targetPath ? getPathBasename(targetPath).toLowerCase() : '';

        const candidates = Array.from(document.querySelectorAll('aside [role="button"], aside button, aside a, aside [tabindex], [class*="sidebar"] [role="button"], [class*="sidebar"] button, [class*="sidebar"] a, [class*="project"]'));
        for (const el of candidates) {
            if (isInsideOwnOverlayNode(el)) continue;
            const text = (el.innerText || el.textContent || '').trim().toLowerCase();
            const title = (el.getAttribute('title') || el.getAttribute('aria-label') || '').toLowerCase();
            if ((cleanName && (text === cleanName || title.includes(cleanName))) ||
                (cleanBase && (text === cleanBase || title.includes(cleanBase)))) {
                try {
                    el.click();
                    return true;
                } catch (_) {}
            }
        }
        return false;
    }

    function dispatchPreparedSwarmResult(actionLabel, res) {
        const dispatchPrompt = String((res && res.dispatchPrompt) || '');
        const taskId = String((res && res.taskId) || '');
        const targetPath = String((res && (res.projectPath || (res.checkpoint && res.checkpoint.project_path))) || '');
        const projName = targetPath ? getPathBasename(targetPath) : '';

        if (targetPath && projName) {
            trySwitchDesktopProject(targetPath, projName);
        }

        const injectRes = dispatchPrompt ? injectPromptIntoDesktopChat(dispatchPrompt, true) : { inserted: false, sent: false };
        if (taskId) {
            startDesktopSwarmWatcher(taskId, res && res.checkpoint);
        }

        const safeId = escapeHtml(taskId || 'Swarm');
        if (injectRes.sent) {
            showToast(`<b>${escapeHtml(actionLabel)} ${safeId}:</b> промпт отправлен в чат Antigravity 2.0.`, false, 5000);
        } else if (injectRes.inserted) {
            showToast(`<b>${escapeHtml(actionLabel)} ${safeId}:</b> промпт вставлен в поле чата (нажмите Enter для запуска; копия в буфере).`, false, 6000);
        } else {
            showToast(`<b>${escapeHtml(actionLabel)} ${safeId}:</b> промпт скопирован в буфер обмена — нажмите <b>Ctrl+V</b> в чате.`, false, 6500);
        }
        return injectRes;
    }

    async function openSwarmCheckpointsMenu() {
        if (!getSwarmApi()) {
            closeQuickPick();
            showToast('Мост <b>agAPI.swarm</b> не найден в app.asar — перепатчите Antigravity 2.0.', true, 5500);
            return;
        }

        hideTooltip();
        hideSwarmTooltip();
        let backdrop = document.getElementById('ag-qp-backdrop');
        if (!backdrop) {
            backdrop = document.createElement('div');
            backdrop.id = 'ag-qp-backdrop';
            backdrop.onclick = closeQuickPick;
            document.body.appendChild(backdrop);
        }

        let container = document.getElementById('ag-qp-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'ag-qp-container';
            document.body.appendChild(container);
        }

        isMenuOpen = true;
        currentMenuLevel = 'swarm_checkpoints';
        currentFilterText = '';
        backdrop.style.display = 'block';
        container.style.display = 'flex';
        if (_swarmPollActive && _swarmPollIsIdle) {
            _scheduleNextSwarmPoll();
        }

        renderQuickPick();
        const res = await loadSwarmState();
        if (res && !res.ok && res.error) {
            showToast(`Ошибка загрузки Swarm: ${escapeHtml(res.error)}`, true, 4500);
        }
        if (isMenuOpen && (currentMenuLevel === 'swarm_checkpoints' || currentMenuLevel === 'swarm_hub')) {
            renderQuickPick(currentFilterText);
        }
    }

    function stopSwarmModalRefreshTimer() {
        if (swarmModalRefreshTimer) {
            clearInterval(swarmModalRefreshTimer);
            swarmModalRefreshTimer = null;
        }
    }

    function closeSwarmModal() {
        stopSwarmModalRefreshTimer();
        const backdrop = document.getElementById('ag-swarm-modal-backdrop');
        if (backdrop) backdrop.style.display = 'none';
    }

    function renderSwarmModalTasks() {
        const container = document.getElementById('ag-swarm-tasks-list');
        if (!container) return;
        const list = Array.isArray(swarmCheckpoints) ? swarmCheckpoints : [];
        if (list.length === 0) {
            container.innerHTML = `<div class="ag-swarm-hint">Пока нет запущенных задач. Запустите новую ниже или прямо в чате через <b>/swarm ...</b></div>`;
            return;
        }
        const workerStatusIcon = (s) => {
            if (s === 'done') return '✅';
            if (s === 'running') return '⏳';
            if (s === 'failed') return '❌';
            return '⚪';
        };
        let html = '';
        for (const cp of list.slice(0, 8)) {
            const taskId = String(cp.task_id || '');
            const projName = cp.project_name || getPathBasename(cp.project_path || '') || 'проект';
            const stage = formatSwarmStage(cp);
            const done = Number(cp.done_workers || 0);
            const running = Number(cp.running_workers || 0);
            const total = Number(cp.total_workers || 0);
            const errCount = Array.isArray(cp.errors) ? cp.errors.length : 0;
            const orchModel = cp.orchestrator_model || 'Модель чата (умная)';
            const wModels = Array.isArray(cp.worker_models) && cp.worker_models.length
                ? cp.worker_models.join(', ')
                : 'flash';
            let badgeClass = 'ag-swarm-status-running';
            if (cp.is_complete) badgeClass = 'ag-swarm-status-done';
            else if (errCount > 0) badgeClass = 'ag-swarm-status-err';

            const workersArr = Array.isArray(cp.workers) ? cp.workers : [];
            let workersHtml = '';
            if (workersArr.length > 0) {
                workersHtml = `<div class="ag-swarm-workers-list">` + workersArr.slice(0, 6).map((w) => {
                    const filesStr = Array.isArray(w.files) && w.files.length ? ` — ${w.files.join(', ')}` : '';
                    return `<div class="ag-swarm-worker-row">${workerStatusIcon(w.status)} <b>${escapeHtml(w.name)}</b> (${escapeHtml(w.model || 'flash')})${escapeHtml(filesStr)}</div>`;
                }).join('') + `</div>`;
            }

            const countsNote = total > 0
                ? `${done}/${total} готово${running > 0 ? `, ${running} в работе` : ''}`
                : 'план ещё не создан';
            const reqText = String(cp.user_request || '').replace(/\s+/g, ' ').trim();
            const reqShort = reqText.length > 90 ? `${reqText.slice(0, 90)}…` : reqText;

            html += `
                <div class="ag-swarm-task-card">
                    <div class="ag-swarm-task-top">
                        <span style="font-weight:600; color:#f3f4f6;">📁 ${escapeHtml(projName)}</span>
                        <span class="ag-swarm-status-badge ${badgeClass}">${escapeHtml(stage)}</span>
                    </div>
                    ${reqShort ? `<div style="font-size:12px; color:#e5e7eb;">${escapeHtml(reqShort)}</div>` : ''}
                    <div class="ag-swarm-task-meta">Планирует: <b>${escapeHtml(orchModel)}</b> · Агенты: <b>${escapeHtml(countsNote)}</b> (${escapeHtml(wModels)})</div>
                    ${workersHtml}
                    <div class="ag-swarm-card-actions">
                        ${cp.is_complete ? '' : `<button type="button" class="ag-swarm-btn ag-swarm-btn-sm ag-swarm-btn-primary" data-modal-resume="${escapeHtml(taskId)}" data-modal-project="${escapeHtml(cp.project_path || '')}">Продолжить в чате</button>`}
                        <button type="button" class="ag-swarm-btn ag-swarm-btn-sm" data-modal-cleanup="${escapeHtml(taskId)}" data-modal-project="${escapeHtml(cp.project_path || '')}">Удалить</button>
                    </div>
                </div>
            `;
        }
        container.innerHTML = html;

        if (typeof container.querySelectorAll === 'function') {
            container.querySelectorAll('[data-modal-resume]').forEach((btn) => {
                btn.onclick = async () => {
                    const tId = btn.getAttribute('data-modal-resume');
                    const pPath = btn.getAttribute('data-modal-project') || '';
                    closeSwarmModal();
                    await resumeDesktopSwarmTask(tId, pPath);
                };
            });
            container.querySelectorAll('[data-modal-cleanup]').forEach((btn) => {
                btn.onclick = async () => {
                    const tId = btn.getAttribute('data-modal-cleanup');
                    const pPath = btn.getAttribute('data-modal-project') || '';
                    await cleanupDesktopSwarmTask(tId, pPath);
                    renderSwarmModalTasks();
                };
            });
        }
    }

    function populateSwarmModalProjects(forceInputDefault = false) {
        const selectEl = document.getElementById('ag-swarm-project-select');
        const inputEl = document.getElementById('ag-swarm-project-input');
        const list = Array.isArray(swarmProjects) ? swarmProjects : [];

        if (selectEl) {
            let optionsHtml = '<option value="">— Выберите известный git-проект или укажите путь вручную —</option>';
            for (const proj of list) {
                if (!proj || !proj.path) continue;
                const tag = proj.isGit ? '' : ' (не git)';
                optionsHtml += `<option value="${escapeHtml(proj.path)}">${escapeHtml((proj.name || getPathBasename(proj.path)) + tag)} — ${escapeHtml(proj.path)}</option>`;
            }
            selectEl.innerHTML = optionsHtml;
            if (swarmSelectedProjectPath && list.some((p) => p && p.path === swarmSelectedProjectPath)) {
                selectEl.value = swarmSelectedProjectPath;
            }
        }
        if (inputEl && (forceInputDefault || !inputEl.value.trim()) && swarmSelectedProjectPath) {
            inputEl.value = swarmSelectedProjectPath;
        }
    }

    function setSwarmModalError(msg) {
        const errEl = document.getElementById('ag-swarm-modal-error');
        if (!errEl) return;
        if (!msg) {
            errEl.textContent = '';
            errEl.style.display = 'none';
        } else {
            errEl.textContent = msg;
            errEl.style.display = 'block';
        }
    }

    async function launchSwarmFromModal() {
        const swarmApi = getSwarmApi();
        if (!swarmApi || swarmPendingAction) return;

        const pathInput = document.getElementById('ag-swarm-project-input');
        const reqInput = document.getElementById('ag-swarm-request-input');
        const affectedCheckbox = document.getElementById('ag-swarm-affected-tests');
        const launchBtn = document.getElementById('ag-swarm-launch-btn');

        const projectPath = pathInput ? pathInput.value.trim() : swarmSelectedProjectPath;
        const requestText = reqInput ? reqInput.value.trim() : '';
        const useAffectedTests = affectedCheckbox ? Boolean(affectedCheckbox.checked) : true;

        if (!projectPath) {
            setSwarmModalError('Укажите путь к git-проекту (выберите из списка или через Обзор).');
            return;
        }
        if (!requestText) {
            setSwarmModalError('Опишите задачу для Swarm.');
            return;
        }

        setSwarmModalError('');
        swarmPendingAction = true;
        if (launchBtn) {
            launchBtn.disabled = true;
            launchBtn.textContent = 'Подготовка...';
        }

        try {
            const res = await swarmApi('prepare', {
                projectPath,
                request: requestText,
                useAffectedTests,
            });
            if (!res || !res.ok) {
                setSwarmModalError((res && res.error) ? res.error : 'Не удалось подготовить задачу Swarm.');
                return;
            }
            swarmSelectedProjectPath = projectPath;
            closeSwarmModal();
            await loadSwarmState();
            return dispatchPreparedSwarmResult('Команда', res);
        } catch (err) {
            setSwarmModalError(err && err.message ? err.message : String(err));
        } finally {
            swarmPendingAction = false;
            if (launchBtn) {
                launchBtn.disabled = false;
                launchBtn.textContent = 'Запустить команду в чат';
            }
        }
    }

    async function openSwarmRunModal() {
        if (!getSwarmApi()) {
            showToast('Мост <b>agAPI.swarm</b> не найден в app.asar — перепатчите Antigravity 2.0.', true, 5500);
            return;
        }

        let backdrop = document.getElementById('ag-swarm-modal-backdrop');
        if (!backdrop) {
            backdrop = document.createElement('div');
            backdrop.id = 'ag-swarm-modal-backdrop';
            backdrop.onclick = (e) => {
                if (e && e.target === backdrop) closeSwarmModal();
            };
            document.body.appendChild(backdrop);
        }

        backdrop.innerHTML = `
            <div id="ag-swarm-modal-box">
                <div class="ag-dialog-close" id="ag-swarm-close-btn">✕</div>
                <div class="ag-swarm-modal-title">${SWARM_SVG}<span>Команда агентов — запуск задачи</span></div>
                <div>
                    <label class="ag-swarm-field-label" for="ag-swarm-project-select">Проект (автопоиск git-папок)</label>
                    <select class="ag-swarm-select" id="ag-swarm-project-select"></select>
                </div>
                <div>
                    <label class="ag-swarm-field-label" for="ag-swarm-project-input">Путь к папке проекта (git)</label>
                    <div class="ag-swarm-row">
                        <input type="text" class="ag-swarm-input" id="ag-swarm-project-input" placeholder="D:\\path\\to\\git-repo" value="${escapeHtml(swarmSelectedProjectPath)}" autocomplete="off" spellcheck="false" />
                        <button type="button" class="ag-swarm-btn" id="ag-swarm-browse-btn">Обзор...</button>
                    </div>
                </div>
                <div>
                    <label class="ag-swarm-field-label" for="ag-swarm-request-input">Что нужно сделать (лучше для задач по разным файлам)</label>
                    <textarea class="ag-swarm-textarea" id="ag-swarm-request-input" rows="3" placeholder="Например: почини баг в модуле А, добавь функцию Б и обнови README..."></textarea>
                </div>
                <div>
                    <label class="ag-swarm-checkbox-row">
                        <input type="checkbox" id="ag-swarm-affected-tests" checked />
                        <span>Быстрая проверка: только тесты изменённых файлов</span>
                    </label>
                    <div class="ag-swarm-hint" style="margin-left:20px;">Ускоряет проверку в 5–10 раз. Перед финальным слиянием всё равно прогоняются все тесты.</div>
                </div>
                <div class="ag-swarm-chat-tip">
                    <b>Можно запускать прямо в чате без этого окна:</b> напишите в любой чат <code>/swarm ваша задача</code> или «запусти команду агентов: ...» — сработает для открытого проекта.
                </div>
                ${(swarmQuotaPlan && swarmQuotaPlan.strategy === 'degraded_swarm') ? `<div class="ag-swarm-chat-tip" style="border-color:rgba(245,158,11,0.45); background:rgba(245,158,11,0.08); color:#fcd34d;"><b>🛡 Резервный режим роя (Flash):</b> умные модели (Tier-1) сейчас на 0% на всех аккаунтах. Рой всё равно запустится мелкими шагами (1–2 файла) и с обязательной проверкой тестами.</div>` : ''}
                <div class="ag-swarm-modal-error" id="ag-swarm-modal-error"></div>
                <div class="ag-swarm-btn-row">
                    <button type="button" class="ag-swarm-btn" id="ag-swarm-cancel-btn">Закрыть</button>
                    <button type="button" class="ag-swarm-btn ag-swarm-btn-primary" id="ag-swarm-launch-btn">Запустить команду в чат</button>
                </div>
                <div id="ag-swarm-tasks-list" style="display:none;"></div>
            </div>
        `;
        backdrop.style.display = 'flex';
        populateSwarmModalProjects(true);
        renderSwarmModalTasks();

        const closeBtn = document.getElementById('ag-swarm-close-btn');
        if (closeBtn) closeBtn.onclick = closeSwarmModal;
        const cancelBtn = document.getElementById('ag-swarm-cancel-btn');
        if (cancelBtn) cancelBtn.onclick = closeSwarmModal;

        const selectEl = document.getElementById('ag-swarm-project-select');
        const pathInput = document.getElementById('ag-swarm-project-input');
        if (selectEl) {
            selectEl.onchange = () => {
                if (selectEl.value) {
                    swarmSelectedProjectPath = selectEl.value;
                    if (pathInput) pathInput.value = selectEl.value;
                }
            };
        }

        const browseBtn = document.getElementById('ag-swarm-browse-btn');
        if (browseBtn) {
            browseBtn.onclick = async () => {
                const swarmApi = getSwarmApi();
                if (!swarmApi) return;
                const defaultPath = pathInput ? pathInput.value.trim() : swarmSelectedProjectPath;
                const picked = await swarmApi('pick_project', { defaultPath });
                if (picked && picked.ok && !picked.canceled && picked.projectPath) {
                    swarmSelectedProjectPath = picked.projectPath;
                    if (Array.isArray(picked.projects)) swarmProjects = picked.projects;
                    populateSwarmModalProjects(false);
                    if (pathInput) pathInput.value = picked.projectPath;
                    setSwarmModalError(picked.isGit === false ? 'Внимание: выбранная папка не похожа на git-репозиторий (.git не найден).' : '');
                }
            };
        }

        const reqInput = document.getElementById('ag-swarm-request-input');
        if (reqInput) {
            reqInput.focus();
            reqInput.onkeydown = (e) => {
                if (!e) return;
                if (e.key === 'Escape') {
                    closeSwarmModal();
                } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                    if (typeof e.preventDefault === 'function') e.preventDefault();
                    launchSwarmFromModal();
                }
            };
        }

        const launchBtn = document.getElementById('ag-swarm-launch-btn');
        if (launchBtn) {
            launchBtn.onclick = () => launchSwarmFromModal();
        }

        const refreshTasksNow = async () => {
            await loadSwarmState();
            populateSwarmModalProjects(false);
            renderSwarmModalTasks();
        };

        const refreshBtn = document.getElementById('ag-swarm-refresh-btn');
        if (refreshBtn) {
            refreshBtn.onclick = () => refreshTasksNow();
        }

        await refreshTasksNow();
        stopSwarmModalRefreshTimer();
        if (!window.__ag_skip_auto_start) {
            swarmModalRefreshTimer = setInterval(() => {
                const bd = document.getElementById('ag-swarm-modal-backdrop');
                if (!bd || bd.style.display === 'none') {
                    stopSwarmModalRefreshTimer();
                    return;
                }
                refreshTasksNow();
            }, 3000);
        }
    }

    async function resumeDesktopSwarmTask(taskId, projectPath = '') {
        const swarmApi = getSwarmApi();
        if (!swarmApi || !taskId) return;
        const res = await swarmApi('resume', { taskId, projectPath });
        closeQuickPick();
        if (!res || !res.ok) {
            showToast(`Не удалось возобновить ${escapeHtml(taskId)}: ${escapeHtml((res && res.error) || 'ошибка')}`, true, 5000);
            return res;
        }
        return dispatchPreparedSwarmResult('Возобновление', res);
    }

    async function cleanupDesktopSwarmTask(taskId, projectPath = '') {
        const swarmApi = getSwarmApi();
        if (!swarmApi || !taskId) return;
        const res = await swarmApi('cleanup', { taskId, projectPath, deleteState: true });
        if (!res || !res.ok) {
            showToast(`Ошибка очистки ${escapeHtml(taskId)}: ${escapeHtml((res && res.error) || 'ошибка')}`, true, 5000);
            return res;
        }
        if (Array.isArray(res.checkpoints)) {
            swarmCheckpoints = res.checkpoints;
        } else {
            swarmCheckpoints = swarmCheckpoints.filter((cp) => cp.task_id !== taskId);
        }
        if (watchedSwarmTask && watchedSwarmTask.task_id === taskId) {
            stopDesktopSwarmWatcher();
            watchedSwarmTask = null;
            renderPillContent();
        }
        if (isMenuOpen && currentMenuLevel === 'swarm_checkpoints') {
            renderQuickPick(currentFilterText);
        }
        renderSwarmModalTasks();
        const warnText = res.warning ? ` (${escapeHtml(res.warning)})` : '';
        showToast(`Задача <b>${escapeHtml(taskId)}</b> удалена${warnText}.`, false, 4500);
        return res;
    }

    async function handleMainAction(action) {
        if (action === 'switch_account') {
            currentMenuLevel = 'accounts';
            renderQuickPick();
            void triggerSyncAccounts({ silent: true });
        } else if (action === 'quota') {
            closeQuickPick();
            // Opens instantly (0ms) without waiting for network!
            openQuotaModal();
        } else if (action === 'quota_policy') {
            currentMenuLevel = 'quota_policy';
            renderQuickPick();
            await loadAssistantSettingsState();
            if (isMenuOpen && currentMenuLevel === 'quota_policy') renderQuickPick();
        } else if (action === 'model_config') {
            currentMenuLevel = 'model_config';
            renderQuickPick();
            await loadAssistantSettingsState();
            if (isMenuOpen && currentMenuLevel === 'model_config') renderQuickPick();
        } else if (action === 'swarm_hub' || action === 'resume_swarm') {
            await openSwarmCheckpointsMenu();
        } else if (action === 'run_swarm') {
            closeQuickPick();
            await openSwarmRunModal();
        } else if (action === 'refresh_network') {
            closeQuickPick();
            await refreshNetwork();
        } else if (action === 'open_log') {
            closeQuickPick();
            if (window.agAPI && window.agAPI.openLog) {
                window.agAPI.openLog();
            }
        }
    }

    let _addAccountInFlight = false;
    async function triggerAddAccount() {
        if (_addAccountInFlight || !window.agAPI || !window.agAPI.refresh) return;
        _addAccountInFlight = true;
        showToast('🌐 Откройте браузер и войдите в Google-аккаунт (окно вернётся автоматически)...', false, 6000);
        try {
            const res = await window.agAPI.refresh('add_account');
            if (res && res.ok && res.account) {
                const label = escapeHtml((res.account && (res.account.email || res.account.name)) || 'аккаунт');
                await fetchData();
                if (isMenuOpen && currentMenuLevel === 'accounts') {
                    renderQuickPick(currentFilterText);
                }
                showToast(`✅ Добавлен аккаунт <strong>${label}</strong> (всего: ${accountsList.length}).`, false, 5000);
            } else if (res && res.ok && !res.account) {
                showToast('ℹ Перепатчьте Antigravity 2.0 (обновите app.asar в Patcher), чтобы открывать Google-вход прямо отсюда, либо добавьте через Antigravity IDE.', false, 6500);
            } else {
                const errMsg = (res && res.error) ? String(res.error) : 'Не удалось добавить аккаунт';
                showToast(`⚠ Ошибка добавления: ${escapeHtml(errMsg)}`, true, 5500);
            }
        } catch (e) {
            showToast(`⚠ Ошибка добавления: ${escapeHtml((e && e.message) || String(e))}`, true, 5500);
        } finally {
            _addAccountInFlight = false;
        }
    }

    let _syncAccountsInFlight = false;
    async function triggerSyncAccounts({ silent = false } = {}) {
        if (_syncAccountsInFlight || !window.agAPI || !window.agAPI.refresh) return;
        _syncAccountsInFlight = true;
        if (!silent) {
            showToast('↻ Синхронизация аккаунтов из системы...', false, 2500);
        }
        try {
            const prevCount = accountsList.length;
            const prevActive = activeAccount ? activeAccount.id : '';
            const res = await window.agAPI.refresh('sync_accounts');
            await fetchData();
            if (isMenuOpen && currentMenuLevel === 'accounts') {
                if (!silent || accountsList.length !== prevCount || (activeAccount && activeAccount.id !== prevActive)) {
                    renderQuickPick(currentFilterText);
                }
            }
            if (!silent) {
                if (res && res.ok !== false) {
                    showToast(`✅ Список синхронизирован (аккаунтов: ${accountsList.length}).`, false, 3500);
                } else {
                    const errMsg = (res && res.error) ? String(res.error) : 'Ошибка синхронизации';
                    showToast(`⚠ ${escapeHtml(errMsg)}`, true, 4500);
                }
            }
        } catch (e) {
            if (!silent) {
                showToast(`⚠ Ошибка синхронизации: ${escapeHtml((e && e.message) || String(e))}`, true, 4500);
            }
        } finally {
            _syncAccountsInFlight = false;
        }
    }

    async function switchAccount(targetId) {
        if (!targetId || !window.agAPI || !window.agAPI.switchAccount) return;
        _lastPrompted0PctAccId = targetId;
        closeQuickPick();

        const pill = document.getElementById('ag-status-pill');
        if (pill) {
            pill.dataset.sig = '';
            pill.innerHTML = `${USER_SVG} <span style="color:#60a5fa; font-weight:500;">⏳ Переключение...</span>`;
        }

        try {
            const res = await window.agAPI.switchAccount(targetId);
            if (!res || res.ok === false) {
                const errMsg = (res && res.error) ? String(res.error) : 'Не удалось переключить аккаунт';
                showToast(`⚠ Ошибка переключения: ${escapeHtml(errMsg)}`, true, 5000);
                await fetchData();
                renderPillContent();
                renderSwarmPillContent();
                checkProactiveQuotaWarning();
                return;
            }
            const isRestarting = res.restarting !== false;
            if (pill && isRestarting) {
                pill.dataset.sig = '';
                pill.innerHTML = `${USER_SVG} <span style="color:#60a5fa; font-weight:500;">⏳ Перезапуск...</span>`;
            }
            const targetLabel = escapeHtml((res && (res.name || res.email)) || 'аккаунт');
            showToast(
                isRestarting
                    ? `🔄 Перезапуск Antigravity 2.0 и вход в <strong>${targetLabel}</strong>...`
                    : `🔄 Переключение на <strong>${targetLabel}</strong>...`,
                false,
                4500
            );
            if (!isRestarting) {
                setTimeout(() => { fetchData(); }, 3500);
            }
        } catch (e) {
            console.error('[AG Desktop] switch error:', e);
            showToast(`⚠ Ошибка переключения: ${escapeHtml((e && e.message) || String(e))}`, true, 5000);
            await fetchData();
            renderPillContent();
        }
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            const qm = document.getElementById('ag-quota-modal-backdrop');
            if (qm && qm.style.display === 'flex') {
                closeQuotaModal();
                return;
            }
            if (isMenuOpen) {
                if (currentMenuLevel === 'accounts') {
                    currentMenuLevel = 'main';
                    renderQuickPick();
                } else {
                    closeQuickPick();
                }
            }
        }
    });

    let _lastPrompted0PctAccId = '';
    function checkProactiveQuotaWarning() {
        const list = Array.isArray(accountsList) && accountsList.length > 0 ? accountsList : [];
        if (!activeAccount || !Array.isArray(list) || list.length <= 1) return;
        const cPct = (activeAccount.claude_percentage !== undefined && activeAccount.claude_percentage !== null)
            ? Number(activeAccount.claude_percentage) : null;

        if (cPct === 0 && _lastPrompted0PctAccId !== activeAccount.id) {
            const altAccount = list.find((a) => a && a.id !== activeAccount.id && Number(a.claude_percentage) > 0);
            if (altAccount) {
                _lastPrompted0PctAccId = activeAccount.id;
                const altName = altAccount.name || altAccount.email || 'резервный';
                const altPct = altAccount.claude_percentage;
                showToast(
                    `⚠️ <b>Квота Claude исчерпана (0%)</b> на аккаунте <i>${escapeHtml(activeAccount.name || activeAccount.email)}</i>.<br>` +
                    `<button type="button" class="ag-swarm-btn ag-swarm-btn-sm ag-swarm-btn-primary" id="ag-quick-hop-btn" style="margin-top:6px; cursor:pointer;">Переключить на ${escapeHtml(altName)} (${altPct}%)</button>`,
                    true,
                    12000
                );
                setTimeout(() => {
                    const btn = document.getElementById('ag-quick-hop-btn');
                    if (btn) {
                        btn.onclick = () => {
                            _lastPrompted0PctAccId = altAccount.id;
                            switchAccount(altAccount.id);
                        };
                    }
                }, 50);
            }
        } else if (cPct !== null && cPct > 0) {
            _lastPrompted0PctAccId = '';
        }
    }

    async function fetchData() {
        if (!window.agAPI || !window.agAPI.getState) return;
        try {
            const state = await window.agAPI.getState();
            if (state && state.ok) {
                if (state.activeAccount) {
                    activeAccount = state.activeAccount;
                    saveCachedAccount(activeAccount);
                }
                accountsList = state.accounts || [];
                if (state.country) connCountry = state.country;
                if (state.countryCode !== undefined) connCountryCode = state.countryCode;
                if (typeof state.ping === 'number') connPing = state.ping;
                if (typeof state.networkOk === 'boolean') {
                    connOk = state.networkOk;
                } else if (state.country === 'Офлайн') {
                    connOk = false;
                } else if (state.ping > 0) {
                    connOk = true;
                }
                if (state.networkStatus) connStatusText = state.networkStatus;
                if ('proxyUrl' in state) connProxyUrl = state.proxyUrl || null;
                if (connCountry || connCountryCode || connPing || typeof state.networkOk === 'boolean') {
                    saveCachedNet();
                }
                renderPillContent();
                renderSwarmPillContent();
                checkProactiveQuotaWarning();
                const tt = document.getElementById('ag-ide-tooltip');
                if (tt && tt.style.display === 'flex') {
                    renderTooltipContent(tt);
                }
                const qm = document.getElementById('ag-quota-modal-backdrop');
                if (qm && qm.style.display === 'flex') {
                    renderQuotaModalContent();
                }
                if (isMenuOpen && currentMenuLevel === 'accounts') {
                    const container = document.getElementById('ag-qp-container');
                    const nextSig = getAccountsRenderSig();
                    if (!container || container.dataset.accountsSig !== nextSig) {
                        const inp = document.getElementById('ag-qp-input');
                        const curCaret = inp ? inp.selectionStart : null;
                        renderQuickPick(currentFilterText);
                        const newInp = document.getElementById('ag-qp-input');
                        if (newInp && curCaret !== null) {
                            newInp.setSelectionRange(curCaret, curCaret);
                        }
                    }
                }
            }
        } catch (e) {
            console.error('[AG Desktop] fetchData error:', e);
        }
    }

    function triggerLiveRefresh(mode = 'active', force = false) {
        if (!window.agAPI || !window.agAPI.refresh) return Promise.resolve();
        const now = Date.now();
        const minGap = force ? 0 : (mode === 'all' ? 20000 : 10000);
        if (!force && (now - lastLiveRefreshAt) < minGap) {
            return inFlightLiveRefresh || Promise.resolve();
        }
        if (inFlightLiveRefresh) {
            if (force && mode === 'active' && !inFlightLiveForce) {
                pendingForceActiveRefresh = true;
                return inFlightLiveRefresh.then(() => {
                    if (pendingForceActiveRefresh) {
                        pendingForceActiveRefresh = false;
                        return triggerLiveRefresh('active', true);
                    }
                });
            }
            return inFlightLiveRefresh;
        }

        const payload = (force && mode !== 'network')
            ? { mode, force: true, minAgeSec: 0 }
            : mode;
        lastLiveRefreshAt = now;
        inFlightLiveForce = Boolean(force);
        inFlightLiveRefresh = Promise.resolve()
            .then(() => window.agAPI.refresh(payload))
            .then(() => fetchData())
            .catch(() => {})
            .finally(() => {
                inFlightLiveRefresh = null;
                inFlightLiveForce = false;
            });
        return inFlightLiveRefresh;
    }

    let _lastSwarmPushRefreshAt = 0;
    let autoHealEnabled = true;
    let activeAutofixAlert = null;
    let isAutofixBusy = false;
    let autofixStatusText = '';
    let autofixTone = 'err'; // 'err' | 'ok'
    let autofixHideTimer = null;
    const _autofixLastAutoHealAt = new Map();

    const DOM_ERROR_RULES = [
        {
            key: 'user_location_400',
            title: 'Региональная блокировка API (HTTP 400)',
            description: 'Google API отклоняет регион — требуется обход и настройка DNS.',
            re: /User location is not supported|FAILED_PRECONDITION.*400|Account ineligible|available in your location/i,
        },
        {
            key: 'dead_proxy',
            title: 'Недоступен локальный прокси VPN',
            description: 'Остался нерабочий прокси-порт — трафик к Google API отклонён.',
            re: /proxyconnect\s+tcp:|ERR_PROXY_CONNECTION_FAILED|ECONNREFUSED\s+127\.0\.0\.1:\d+/i,
        },
        {
            key: 'tls_expired',
            title: 'Сбой TLS / загрузки моделей',
            description: 'Соединение до Google API оборвалось по таймауту TLS handshake.',
            re: /Error Loading Models|TLS handshake timeout|TLS handshake error|unexpected issue setting up your account/i,
        },
        {
            key: 'dns_lookup_fail',
            title: 'Сбой DNS для Google API',
            description: 'Домен googleapis.com не разрешается текущим DNS-сервером.',
            re: /no such host|ERR_NAME_NOT_RESOLVED|ENOTFOUND.*googleapis\.com/i,
        },
    ];

    function ensureAutofixPopup() {
        let el = document.getElementById('ag-autofix-popup');
        if (!el) {
            el = document.createElement('div');
            el.id = 'ag-autofix-popup';
            document.body.appendChild(el);
        }
        return el;
    }

    function hideAutofixPopup() {
        if (autofixHideTimer) {
            clearTimeout(autofixHideTimer);
            autofixHideTimer = null;
        }
        const el = document.getElementById('ag-autofix-popup');
        if (el) el.style.display = 'none';
        activeAutofixAlert = null;
        if (window.agAPI && typeof window.agAPI.guardian === 'function') {
            Promise.resolve(window.agAPI.guardian('dismiss')).catch(() => {});
        }
    }

    function renderAutofixPopup() {
        const el = ensureAutofixPopup();
        if (!activeAutofixAlert) {
            el.style.display = 'none';
            return;
        }
        el.className = autofixTone === 'ok' ? 'ag-autofix-ok' : 'ag-autofix-err';
        const autoLabel = autoHealEnabled ? 'Авто: ВКЛ' : 'Авто: ВЫКЛ';
        const btnDisabled = isAutofixBusy ? 'disabled' : '';
        const btnLabel = isAutofixBusy ? 'Исправляю...' : 'Починить';
        el.innerHTML = `
            <div class="ag-af-head">
                <div class="ag-af-title-wrap">
                    <span>🛡</span>
                    <span>Защита Antigravity 2.0</span>
                </div>
                <div class="ag-af-controls">
                    <button type="button" id="ag-af-toggle-auto" class="ag-af-auto-pill ${autoHealEnabled ? '' : 'ag-af-off'}" title="Автоматическое исправление ошибок в этой программе">${escapeHtml(autoLabel)}</button>
                    <button type="button" id="ag-af-close" class="ag-af-close" title="Скрыть">×</button>
                </div>
            </div>
            <div class="ag-af-err-title">${escapeHtml(activeAutofixAlert.title || 'Обнаружена ошибка')}</div>
            <div class="ag-af-desc">${escapeHtml(activeAutofixAlert.description || '')}</div>
            ${autofixStatusText ? `<div class="ag-af-status">${escapeHtml(autofixStatusText)}</div>` : ''}
            <div class="ag-af-actions">
                <button type="button" id="ag-af-fix-btn" class="ag-af-btn-fix" ${btnDisabled}>${escapeHtml(btnLabel)}</button>
            </div>
        `;
        el.style.display = 'block';

        const closeBtn = document.getElementById('ag-af-close');
        if (closeBtn) closeBtn.onclick = () => hideAutofixPopup();

        const toggleBtn = document.getElementById('ag-af-toggle-auto');
        if (toggleBtn) {
            toggleBtn.onclick = async () => {
                autoHealEnabled = !autoHealEnabled;
                if (window.agAPI && typeof window.agAPI.guardian === 'function') {
                    await window.agAPI.guardian('set_config', { enabled: autoHealEnabled }).catch(() => null);
                }
                renderAutofixPopup();
            };
        }

        const fixBtn = document.getElementById('ag-af-fix-btn');
        if (fixBtn) {
            fixBtn.onclick = () => {
                void runInAppHeal(false);
            };
        }
    }

    async function runInAppHeal(isAuto = false) {
        if (isAutofixBusy) return;
        isAutofixBusy = true;
        autofixTone = 'err';
        autofixStatusText = isAuto
            ? 'Авто-режим: очищаю прокси/DNS и перезагружаю агент...'
            : 'Применяю точечный фикс для Antigravity 2.0...';
        renderAutofixPopup();

        try {
            let ok = false;
            let detail = '';
            if (window.agAPI && typeof window.agAPI.guardian === 'function') {
                const res = await window.agAPI.guardian('heal', { inPlaceOnly: true });
                const g = (res && res.guardian) || {};
                const netOk = Boolean(res && res.network && res.network.ok);
                ok = Boolean((res && res.ok) || netOk || (Array.isArray(g.actions) && g.actions.length > 0));
                detail = g.diagnosis || (res && (res.backendNote || res.error)) || '';
                if (res && res.network && !res.network.ok && !detail) {
                    detail = res.network.status || 'Серверы Google пока недоступны (проверьте VPN).';
                    ok = false;
                }
            } else if (window.agAPI) {
                if (typeof window.agAPI.fix400 === 'function') {
                    await window.agAPI.fix400().catch(() => null);
                }
                if (typeof window.agAPI.refresh === 'function') {
                    const netRes = await window.agAPI.refresh('network').catch(() => null);
                    ok = Boolean(netRes && netRes.network && netRes.network.ok);
                    detail = (netRes && (netRes.backendNote || (netRes.network && netRes.network.status))) || '';
                }
            }
            if (ok) {
                autofixTone = 'ok';
                autofixStatusText = detail ? `✓ Готово: ${detail}` : '✓ Соединение и сервис восстановлены';
                renderAutofixPopup();
                if (autofixHideTimer) clearTimeout(autofixHideTimer);
                autofixHideTimer = setTimeout(() => hideAutofixPopup(), 4500);
            } else {
                autofixTone = 'err';
                autofixStatusText = detail || 'Не удалось восстановить связь — попробуйте другой VPN или нажмите «Починить».';
                renderAutofixPopup();
            }
        } catch (err) {
            autofixTone = 'err';
            autofixStatusText = `Ошибка: ${(err && err.message) || String(err)}`;
            renderAutofixPopup();
        } finally {
            isAutofixBusy = false;
            if (activeAutofixAlert) renderAutofixPopup();
        }
    }

    function handleGuardianAlert(alert, fromAutoSource = true) {
        if (!alert || !alert.key) return;
        const now = Date.now();
        if (autofixHideTimer) {
            clearTimeout(autofixHideTimer);
            autofixHideTimer = null;
        }
        activeAutofixAlert = alert;
        autofixTone = 'err';
        if (autoHealEnabled && fromAutoSource) {
            const lastTs = _autofixLastAutoHealAt.get(alert.key) || 0;
            if ((now - lastTs) >= 90000) {
                _autofixLastAutoHealAt.set(alert.key, now);
                renderAutofixPopup();
                void runInAppHeal(true);
                return;
            }
        }
        autofixStatusText = autoHealEnabled ? '' : 'Авто-режим выключен — нажмите «Починить».';
        renderAutofixPopup();
    }

    function scanDomForBlockingErrors() {
        try {
            if (!document || !document.body || isAutofixBusy) return null;
            const candidates = document.querySelectorAll('[role="alert"], [role="dialog"], [class*="error" i], [class*="Error" i], [class*="banner" i], [class*="toast" i]');
            for (let i = 0; i < candidates.length; i++) {
                const node = candidates[i];
                if (!node || isInsideOwnOverlay(node) || !isElementVisible(node)) continue;
                const txt = String(node.textContent || '').replace(/\s+/g, ' ').trim();
                if (!txt || txt.length > 900) continue;
                for (const rule of DOM_ERROR_RULES) {
                    if (rule.re.test(txt)) {
                        const alert = { key: rule.key, title: rule.title, description: rule.description };
                        const now = Date.now();
                        const lastTs = _autofixLastAutoHealAt.get(rule.key) || 0;
                        if (!activeAutofixAlert || activeAutofixAlert.key !== rule.key) {
                            if ((now - lastTs) >= 90000) {
                                handleGuardianAlert(alert, true);
                            }
                        }
                        return alert;
                    }
                }
            }
        } catch (_) {}
        return null;
    }

    function waitForAPI(attemptsLeft) {
        if (window.agAPI && window.agAPI.getState) {
            fetchData();
            if (typeof window.agAPI.guardian === 'function') {
                window.agAPI.guardian('get_config').then((res) => {
                    if (res && res.config && typeof res.config.enabled === 'boolean') {
                        autoHealEnabled = res.config.enabled;
                    }
                    if (res && res.alert) {
                        handleGuardianAlert(res.alert, true);
                    }
                }).catch(() => {});
                if (typeof window.agAPI.onGuardianAlert === 'function') {
                    window.agAPI.onGuardianAlert((data) => {
                        if (data && data.config && typeof data.config.enabled === 'boolean') {
                            autoHealEnabled = data.config.enabled;
                        }
                        if (data && data.alert) {
                            handleGuardianAlert(data.alert, true);
                        }
                    });
                }
                setInterval(() => {
                    if (!document.hidden && !isAutofixBusy) {
                        window.agAPI.guardian('scan').then((res) => {
                            if (res && res.config && typeof res.config.enabled === 'boolean') {
                                autoHealEnabled = res.config.enabled;
                            }
                            if (res && res.alert && (!activeAutofixAlert || activeAutofixAlert.key !== res.alert.key)) {
                                handleGuardianAlert(res.alert, true);
                            }
                        }).catch(() => {});
                    }
                }, 22000);
            }
            if (typeof window.agAPI.onStateUpdated === 'function') {
                window.agAPI.onStateUpdated(() => {
                    fetchData();
                    if (getSwarmApi()) {
                        const now = Date.now();
                        if ((now - _lastSwarmPushRefreshAt) >= 400) {
                            _lastSwarmPushRefreshAt = now;
                            loadSwarmState().then(() => {
                                if (_swarmPollActive && _swarmPollIsIdle && _isSwarmPollBusy()) {
                                    _scheduleNextSwarmPoll();
                                }
                            }).catch(() => {});
                        }
                    }
                });
            }
            setTimeout(() => triggerLiveRefresh('all', true), 600);
            setTimeout(() => {
                if (connOk && (!connPing || connCountry === 'Проверка...')) {
                    fetchData();
                }
            }, 1800);
            setInterval(() => {
                if (!document.hidden) fetchData();
            }, 10000);
            setInterval(() => {
                if (!document.hidden) triggerLiveRefresh('active', false);
            }, 15000);
            window.addEventListener('focus', () => {
                fetchData();
                triggerLiveRefresh('active', false);
            });
            return;
        }
        if (attemptsLeft <= 0) return;
        setTimeout(() => waitForAPI(attemptsLeft - 1), 100);
    }

    function isPillConnectedQuick() {
        if (!mountedPillEl || !mountedPillEl.isConnected) return false;
        if (cachedSettingsBtn) {
            return Boolean(
                cachedSettingsBtn.isConnected &&
                mountedPillEl.previousElementSibling === cachedSettingsBtn
            );
        }
        return mountedPillEl.parentElement === document.body;
    }

    function isPillIntact() {
        if (!isPillConnectedQuick()) return false;
        if (!isPillVisibleInViewport(mountedPillEl)) {
            return false;
        }
        if (cachedSettingsBtn) {
            // Pill & cachedSettingsBtn share ancestors; check button's own styles + rect without repeating full ancestor walk
            if (hasOwnHiddenStyle(cachedSettingsBtn)) return false;
            if (hasLayoutGeometry() && typeof cachedSettingsBtn.getBoundingClientRect === 'function') {
                const br = cachedSettingsBtn.getBoundingClientRect();
                if (!br || br.width < 4 || br.height < 4) return false;
            }
        }
        return true;
    }

    function areAllMutationsInsideOverlay(mutations) {
        if (!Array.isArray(mutations) || mutations.length === 0) return false;
        for (let i = 0; i < mutations.length; i++) {
            const m = mutations[i];
            if (!m || !m.target || !isInsideOwnOverlay(m.target)) {
                return false;
            }
        }
        return true;
    }

    const dismissedOnboardingNodes = typeof WeakSet !== 'undefined' ? new WeakSet() : null;

    function dismissOnboardingModalsSafely() {
        try {
            if (typeof document.querySelectorAll !== 'function') return false;
            const seen = new Set();
            const containers = [];
            for (const sel of ['[role="dialog"], dialog', '[role="alertdialog"], [aria-modal="true"], .agy-dialog']) {
                const list = document.querySelectorAll(sel);
                if (!list || !list.length) continue;
                for (let i = 0; i < list.length; i++) {
                    if (list[i] && !seen.has(list[i])) {
                        seen.add(list[i]);
                        containers.push(list[i]);
                    }
                }
            }
            const onboardingTitleRe = /one last thing before getting started|terms of service\s*&\s*data use|security notice\s*&\s*data use|getting started with google antigravity/i;
            const actionBtnRe = /^(?:accept\s*(?:&|and)\s*continue|continue|next|accept|i\s*agree|agree|confirm|get\s*started|ok)$/i;

            // Onboarding screens (e.g. "Security Notice & Data Use") also render inside plain cards without role="dialog"
            const headings = document.querySelectorAll('h1, h2, h3');
            if (headings && headings.length) {
                for (let i = 0; i < headings.length; i++) {
                    const h = headings[i];
                    if (!h || isInsideOwnOverlay(h)) continue;
                    const htxt = String(h.textContent || '').replace(/\s+/g, ' ').trim();
                    if (!onboardingTitleRe.test(htxt)) continue;
                    let cur = h.parentElement;
                    for (let depth = 0; cur && depth < 5 && cur !== document.body; depth++, cur = cur.parentElement) {
                        if (isInsideOwnOverlay(cur) || seen.has(cur)) break;
                        const btnsNow = typeof cur.querySelectorAll === 'function'
                            ? cur.querySelectorAll('button, [role="button"]')
                            : [];
                        let hasActionBtn = false;
                        for (let b = 0; b < btnsNow.length; b++) {
                            const bt = String((btnsNow[b] && btnsNow[b].textContent) || '').replace(/\s+/g, ' ').trim();
                            if (actionBtnRe.test(bt)) {
                                hasActionBtn = true;
                                break;
                            }
                        }
                        if (hasActionBtn) {
                            seen.add(cur);
                            containers.push(cur);
                            break;
                        }
                    }
                }
            }

            if (containers.length === 0) return false;
            let dismissedAny = false;
            for (let i = 0; i < containers.length; i++) {
                const dlg = containers[i];
                if (!dlg || isInsideOwnOverlay(dlg)) continue;
                if (dismissedOnboardingNodes && dismissedOnboardingNodes.has(dlg)) continue;
                if (!isElementVisible(dlg)) continue;
                const txt = String(dlg.textContent || '').replace(/\s+/g, ' ').trim();
                if (!onboardingTitleRe.test(txt)) {
                    continue;
                }
                const btns = typeof dlg.querySelectorAll === 'function'
                    ? dlg.querySelectorAll('button, [role="button"]')
                    : [];
                for (let j = 0; j < btns.length; j++) {
                    const btn = btns[j];
                    if (!btn || btn.disabled) continue;
                    const ariaDisabled = typeof btn.getAttribute === 'function' ? btn.getAttribute('aria-disabled') : null;
                    if (ariaDisabled === 'true') continue;
                    const btxt = String(btn.textContent || '').replace(/\s+/g, ' ').trim();
                    if (actionBtnRe.test(btxt)) {
                        if (dismissedOnboardingNodes) dismissedOnboardingNodes.add(dlg);
                        if (typeof btn.click === 'function') {
                            btn.click();
                            dismissedAny = true;
                        }
                        break;
                    }
                }
            }
            return dismissedAny;
        } catch (_) {
            return false;
        }
    }

    function startDOMGuard() {
        injectStyles();
        mountPill();
        dismissOnboardingModalsSafely();

        let lastGuardCheckAt = Date.now();
        let remountIsUrgent = false;
        let observedTargets = [];
        let observedSubtree = null;

        let observer = null;
        const bindGuardObserver = () => {
            if (!observer) return;
            const bodyFallback = document.body || document.documentElement;
            if (!bodyFallback) return;

            const targets = [];
            let subtree = false;
            if (isPillConnectedQuick() && mountedPillEl && mountedPillEl.parentElement) {
                targets.push(mountedPillEl.parentElement);
                if (mountedSwarmPillEl && mountedSwarmPillEl.isConnected && mountedSwarmPillEl.parentElement &&
                    mountedSwarmPillEl.parentElement !== mountedPillEl.parentElement) {
                    targets.push(mountedSwarmPillEl.parentElement);
                }
                subtree = false;
            } else {
                targets.push(bodyFallback);
                subtree = true;
            }

            const same = observedSubtree === subtree &&
                observedTargets.length === targets.length &&
                targets.every((t, i) => observedTargets[i] === t);
            if (!same) {
                try {
                    observer.disconnect();
                    for (const t of targets) {
                        observer.observe(t, { childList: true, subtree });
                    }
                    observedTargets = targets;
                    observedSubtree = subtree;
                } catch (_) {}
            }
        };

        const runGuardCheck = () => {
            remountTimer = null;
            remountIsUrgent = false;
            lastGuardCheckAt = Date.now();
            if (document.hidden) return;
            dismissOnboardingModalsSafely();
            if (!isPillIntact() || (!cachedSettingsBtn && findSettingsButton())) {
                mountPill();
            }
            bindGuardObserver();
        };

        if (typeof MutationObserver === 'function') {
            observer = new MutationObserver((mutations) => {
                // Ignore mutations triggered inside our own overlay nodes (#ag-status-pill, tooltip, modals)
                if (areAllMutationsInsideOverlay(mutations)) return;

                // Only O(1) DOM reference check synchronously (zero getComputedStyle / getBoundingClientRect reflows)
                if (!isPillConnectedQuick()) {
                    if (remountTimer && !remountIsUrgent) {
                        clearTimeout(remountTimer);
                        remountTimer = null;
                    }
                    if (!remountTimer) {
                        remountIsUrgent = true;
                        remountTimer = setTimeout(runGuardCheck, 80);
                    }
                    return;
                }

                if (!remountTimer) {
                    const elapsed = Date.now() - lastGuardCheckAt;
                    const delay = elapsed >= 650 ? 500 : Math.max(500, 800 - elapsed);
                    remountTimer = setTimeout(runGuardCheck, delay);
                }
            });
            bindGuardObserver();
        }

        setInterval(() => {
            if (document.hidden) return;
            lastGuardCheckAt = Date.now();
            dismissOnboardingModalsSafely();
            scanDomForBlockingErrors();
            if (!isPillIntact() || (!cachedSettingsBtn && findSettingsButton())) {
                mountPill();
            }
            bindGuardObserver();
        }, 3000);

        setInterval(() => {
            if (Array.isArray(swarmCheckpoints) && swarmCheckpoints.some((c) => c && !c.is_complete)) {
                renderSwarmPillContent();
            }
        }, 1000);
    }

    const _agExports = {
        getLimitingQuotaInfo,
        formatResetText,
        renderPillContent,
        renderSwarmPillContent,
        formatAgentPillText,
        formatDuration,
        formatElapsed,
        showSwarmTooltip,
        hideSwarmTooltip,
        renderSwarmTooltipContent,
        renderTooltipContent,
        renderQuotaModalContent,
        dismissOnboardingModalsSafely,
        scanDomForBlockingErrors,
        handleGuardianAlert,
        hideAutofixPopup,
        runInAppHeal,
        openQuickPick,
        closeQuickPick,
        renderQuickPick,
        handleMainAction,
        loadAssistantSettingsState,
        openSwarmRunModal,
        closeSwarmModal,
        renderSwarmModalTasks,
        stopSwarmModalRefreshTimer,
        launchSwarmFromModal,
        openSwarmCheckpointsMenu,
        resumeDesktopSwarmTask,
        cleanupDesktopSwarmTask,
        startDesktopSwarmWatcher,
        stopDesktopSwarmWatcher,
        injectPromptIntoDesktopChat,
        switchAccount,
        refreshNetwork,
        triggerLiveRefresh,
        fetchData,
        mountPill,
        mountDesktopBar: mountPill,
        getLimitingQuotaInfo,
        detectActiveUiModel,
        resolvePillGeminiView,
        renderPillContent,
        renderTooltipContent,
        loadSwarmState,
        getSwarmSnapshot: () => swarmSnapshot,
        setSwarmSnapshot: (snap) => {
            swarmSnapshot = snap;
            swarmTooltipLines = null;
            renderSwarmPillContent();
        },
        startSwarmLivePoll,
        stopSwarmLivePoll,
        getTestState: () => ({
            swarmSnapshot,
            swarmCheckpoints,
            swarmProjects,
            watchedSwarmTask,
            recentCompletionTask,
            activeAccount,
            accountsList,
            activeUiModel: detectActiveUiModel(true),
            connCountryCode,
            connCountry,
            connPing,
            connOk
        }),
        setTestState: ({ account, accounts, activeUiModel, net, swarmCheckpoints: cpList, swarmProjects: projList, watchedSwarmTask: activeSwarm, recentCompletion: recComp, swarmSnapshot: snap } = {}) => {
            if (account !== undefined) activeAccount = account;
            if (accounts !== undefined) accountsList = accounts;
            if (activeUiModel !== undefined) _overrideUiModel = activeUiModel;
            if (cpList !== undefined) {
                swarmCheckpoints = cpList;
                renderSwarmPillContent();
            }
            if (projList !== undefined) swarmProjects = projList;
            if (activeSwarm !== undefined) {
                watchedSwarmTask = activeSwarm;
                renderSwarmPillContent();
            }
            if (recComp !== undefined) {
                recentCompletionTask = recComp;
                renderSwarmPillContent();
            }
            if (snap !== undefined) {
                swarmSnapshot = snap;
                swarmTooltipLines = null;
                renderSwarmPillContent();
            }
            if (net) {
                if ('ok' in net) connOk = Boolean(net.ok);
                if ('ms' in net) connPing = Number(net.ms || 0);
                if ('countryCode' in net) connCountryCode = net.countryCode || '';
                if ('country' in net) connCountry = net.country || '';
                if ('status' in net) connStatusText = net.status || '';
                if ('proxyUrl' in net) connProxyUrl = net.proxyUrl || null;
            }
        }
    };
    window.__agDesktopUi = _agExports;
    if (typeof module === 'object' && module.exports) {
        module.exports = _agExports;
    }

    if (!window.__ag_skip_auto_start) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => {
                startDOMGuard();
                waitForAPI(60);
            });
        } else {
            startDOMGuard();
            waitForAPI(60);
        }
    }
})();
