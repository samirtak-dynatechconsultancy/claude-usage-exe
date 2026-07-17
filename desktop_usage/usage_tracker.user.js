// ==UserScript==
// @name         Claude Usage Tracker
// @namespace    https://github.com/samirtak-dynatechconsultancy/claude-usage-exe
// @version      1.0.0
// @description  Live badge showing Claude Pro/Max 5h and 7d usage %. Daily-max log + CSV export.
// @match        https://claude.ai/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';

    // ── Config ────────────────────────────────────────────────────────────
    const POLL_INTERVAL_MS = 5 * 60 * 1000;   // refresh every 5 min
    const STORAGE_KEY      = 'claude_usage_log';
    const MAX_LOG_ENTRIES  = 365;              // keep ~1 year of daily maxes

    // ── State ─────────────────────────────────────────────────────────────
    let orgUuid = null;
    let badge   = null;

    // ── Org resolution ────────────────────────────────────────────────────
    async function resolveOrg() {
        if (orgUuid) return orgUuid;
        try {
            const resp = await fetch('/api/organizations', { credentials: 'include' });
            if (!resp.ok) return null;
            const orgs = await resp.json();
            for (const org of orgs) {
                if (!org.uuid) continue;
                try {
                    const u = await fetch(`/api/organizations/${org.uuid}/usage`,
                                          { credentials: 'include' });
                    if (u.ok) {
                        const d = await u.json();
                        if (d.five_hour && d.five_hour.utilization != null) {
                            orgUuid = org.uuid;
                            return orgUuid;
                        }
                    }
                } catch (_) { /* skip this org */ }
            }
            if (orgs.length) orgUuid = orgs[0].uuid;
        } catch (_) { /* network error */ }
        return orgUuid;
    }

    // ── Fetch usage ───────────────────────────────────────────────────────
    async function fetchUsage() {
        const uuid = await resolveOrg();
        if (!uuid) return null;
        try {
            const resp = await fetch(`/api/organizations/${uuid}/usage`,
                                     { credentials: 'include' });
            if (!resp.ok) return null;
            const data = await resp.json();
            return {
                s5:  data.five_hour  ? data.five_hour.utilization  : null,
                s7:  data.seven_day  ? data.seven_day.utilization  : null,
                r5:  data.five_hour  ? data.five_hour.resets_at    : null,
                r7:  data.seven_day  ? data.seven_day.resets_at    : null,
                ts:  new Date().toISOString(),
            };
        } catch (_) { return null; }
    }

    // ── Daily-max log ─────────────────────────────────────────────────────
    function todayKey() {
        return new Date().toISOString().slice(0, 10); // YYYY-MM-DD
    }

    function loadLog() {
        try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); }
        catch (_) { return []; }
    }

    function saveLog(log) {
        // Trim to MAX_LOG_ENTRIES.
        if (log.length > MAX_LOG_ENTRIES) log = log.slice(-MAX_LOG_ENTRIES);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(log));
    }

    function updateLog(usage) {
        if (!usage || usage.s5 == null) return;
        const log = loadLog();
        const key = todayKey();
        const last = log.length ? log[log.length - 1] : null;

        if (last && last.date === key) {
            // Update today's entry if this reading is higher.
            if (usage.s5 > (last.s5 || 0)) last.s5 = usage.s5;
            if (usage.s7 > (last.s7 || 0)) last.s7 = usage.s7;
            last.ts = usage.ts;
        } else {
            log.push({ date: key, s5: usage.s5, s7: usage.s7, ts: usage.ts });
        }
        saveLog(log);
    }

    // ── CSV export ────────────────────────────────────────────────────────
    function exportCsv() {
        const log = loadLog();
        if (!log.length) { alert('No usage data logged yet.'); return; }
        let csv = 'date,session_pct_max,weekly_pct_max,last_captured\n';
        for (const e of log) {
            csv += `${e.date},${e.s5 ?? ''},${e.s7 ?? ''},${e.ts ?? ''}\n`;
        }
        const blob = new Blob([csv], { type: 'text/csv' });
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement('a');
        a.href     = url;
        a.download = `claude_usage_${todayKey()}.csv`;
        a.click();
        URL.revokeObjectURL(url);
    }

    // ── Badge UI ──────────────────────────────────────────────────────────
    function createBadge() {
        const el = document.createElement('div');
        el.id = 'claude-usage-badge';
        el.style.cssText = [
            'position: fixed',
            'bottom: 16px',
            'right: 16px',
            'z-index: 99999',
            'background: #1a1a2e',
            'color: #e0e0e0',
            'font-family: "SF Mono", Consolas, monospace',
            'font-size: 12px',
            'padding: 8px 12px',
            'border-radius: 8px',
            'box-shadow: 0 2px 8px rgba(0,0,0,0.3)',
            'cursor: pointer',
            'user-select: none',
            'opacity: 0.85',
            'transition: opacity 0.2s',
        ].join('; ');
        el.addEventListener('mouseenter', () => el.style.opacity = '1');
        el.addEventListener('mouseleave', () => el.style.opacity = '0.85');
        el.addEventListener('click', exportCsv);
        el.title = 'Click to export usage CSV';
        el.textContent = '5h: --% | 7d: --%';
        document.body.appendChild(el);
        return el;
    }

    function colorFor(pct) {
        if (pct == null) return '#888';
        if (pct >= 80) return '#ff4444';
        if (pct >= 50) return '#ffaa00';
        return '#44cc44';
    }

    function updateBadge(usage) {
        if (!badge) badge = createBadge();
        if (!usage) {
            badge.textContent = '5h: err | 7d: err';
            return;
        }
        const s5 = usage.s5 != null ? usage.s5 : '--';
        const s7 = usage.s7 != null ? usage.s7 : '--';
        badge.innerHTML =
            `<span style="color:${colorFor(usage.s5)}">5h: ${s5}%</span>` +
            ` | ` +
            `<span style="color:${colorFor(usage.s7)}">7d: ${s7}%</span>`;
    }

    // ── Main loop ─────────────────────────────────────────────────────────
    async function tick() {
        const usage = await fetchUsage();
        updateBadge(usage);
        updateLog(usage);
    }

    // Kick off after a short delay to let the page settle.
    setTimeout(tick, 3000);
    setInterval(tick, POLL_INTERVAL_MS);
})();
