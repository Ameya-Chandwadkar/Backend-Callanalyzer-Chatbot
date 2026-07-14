"""
chat_web.py
Local web interface for MasonMart Data Assistant.

Three sections on one page:
  1. Live Dashboard — employee-wise stats for the last 7 days, computed
     directly in SQLite (no LLM, exact numbers, loads fast).
  2. Chat — the natural language interface over the same data.
  3. Payroll — drop the payroll-specific reports (Never Attended Report,
     customer-salesperson mapping), generate the combined audit/salary
     .xlsx, and download past reports. See payroll/README.md for what
     each input is and why some sections stay gated until provided.

The /dashboard endpoint returns JSON so the browser can refresh the
stats independently without reloading the whole page.
"""

import json
import os
import re
import threading
import time
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chat_query
from common import get_connection, now_iso
import ingest_callyzer
from payroll import ingest_never_attended, ingest_customer_map, generate_report as payroll_report

PORT = 5050

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MasonMart Data Assistant</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', -apple-system, sans-serif; background: #f0f2f5; color: #1e293b; }

  /* ── Header ── */
  header {
    background: linear-gradient(135deg, #1e293b, #0f172a);
    color: white; padding: 16px 28px;
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 18px; font-weight: 600; }
  header p  { font-size: 12px; color: #94a3b8; margin-top: 2px; }
  #refresh-btn {
    background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2);
    color: white; padding: 6px 14px; border-radius: 6px; cursor: pointer;
    font-size: 13px; transition: background 0.15s;
  }
  #refresh-btn:hover { background: rgba(255,255,255,0.2); }

  /* ── Tab bar ── */
  .tabs { display: flex; background: white; border-bottom: 1px solid #e2e8f0; padding: 0 20px; }
  .tab {
    padding: 14px 20px; cursor: pointer; font-size: 14px; font-weight: 500;
    color: #64748b; border-bottom: 2px solid transparent; transition: all 0.15s;
  }
  .tab.active { color: #2563eb; border-bottom-color: #2563eb; }
  .tab:hover:not(.active) { color: #1e293b; }

  /* ── Panels ── */
  .panel { display: none; }
  .panel.active { display: block; }

  /* ── Dashboard ── */
  #dashboard-panel { padding: 24px 20px; max-width: 1100px; margin: 0 auto; }

  .section-title {
    font-size: 13px; font-weight: 600; color: #64748b;
    text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px;
  }
  .freshness-note {
    font-size: 12px; color: #94a3b8; margin-bottom: 20px;
  }
  .upload-card {
    background: white; border: 1px dashed #93c5fd; border-radius: 10px;
    padding: 18px 20px; margin-bottom: 20px;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
  }
  .upload-card.dragover { border-color: #2563eb; background: #eff6ff; }
  .upload-copy strong { display: block; font-size: 14px; margin-bottom: 4px; color: #0f172a; }
  .upload-copy span { font-size: 12px; color: #64748b; }
  .upload-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .secondary-btn {
    background: #2563eb; color: white; border: none; border-radius: 8px;
    padding: 10px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .secondary-btn:hover:not(:disabled) { background: #1d4ed8; }
  .secondary-btn:disabled { background: #93c5fd; cursor: default; }
  .upload-status { font-size: 12px; color: #64748b; min-height: 18px; }

  /* Summary row */
  .summary-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 28px; }
  .summary-card {
    background: white; border-radius: 10px; padding: 18px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07);
  }
  .summary-card .label { font-size: 12px; color: #64748b; margin-bottom: 6px; }
  .summary-card .value { font-size: 26px; font-weight: 700; color: #1e293b; }
  .summary-card .sub   { font-size: 12px; color: #94a3b8; margin-top: 4px; }

  /* Employee table */
  .table-wrap {
    background: white; border-radius: 10px; overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07); margin-bottom: 28px;
  }
  table { width: 100%; border-collapse: collapse; }
  th {
    background: #f8fafc; text-align: left; padding: 11px 16px;
    font-size: 12px; font-weight: 600; color: #64748b;
    text-transform: uppercase; letter-spacing: 0.04em;
    border-bottom: 1px solid #e2e8f0;
  }
  td { padding: 13px 16px; font-size: 14px; border-bottom: 1px solid #f1f5f9; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #f8fafc; }

  /* Badge */
  .badge {
    display: inline-block; padding: 3px 10px; border-radius: 12px;
    font-size: 12px; font-weight: 600;
  }
  .badge.high { background: #dcfce7; color: #166534; }
  .badge.mid  { background: #fef9c3; color: #854d0e; }
  .badge.low  { background: #fee2e2; color: #991b1b; }

  /* Bar spark */
  .bar-wrap { display: flex; align-items: center; gap: 8px; }
  .bar-track { flex: 1; background: #f1f5f9; border-radius: 4px; height: 7px; overflow: hidden; }
  .bar-fill  { height: 100%; background: #2563eb; border-radius: 4px; transition: width 0.4s; }
  .bar-label { font-size: 12px; color: #64748b; min-width: 28px; text-align: right; }

  /* Targets */
  .target-cell { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 12px; }
  .target-sub { color: #64748b; font-size: 11px; }
  .target-unset { color: #94a3b8; font-style: italic; }
  .target-edit { color: #2563eb; text-decoration: none; font-size: 11px; }
  .target-edit:hover { text-decoration: underline; }

  /* Payroll */
  .payroll-status-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid #f1f5f9; flex-wrap: wrap; }
  .payroll-status-row:last-child { border-bottom: none; }
  .channel-tag { font-size: 11px; color: #475569; background: #f1f5f9; border-radius: 10px; padding: 2px 9px; white-space: nowrap; }
  .report-row { display: flex; align-items: center; gap: 14px; padding: 8px 0; border-bottom: 1px solid #f1f5f9; }
  .report-row:last-child { border-bottom: none; }
  #generate-btn { background: #2563eb; color: #fff; border: none; padding: 10px 20px; border-radius: 8px; font-weight: 600; cursor: pointer; }
  #generate-btn:hover { background: #1d4ed8; }
  #generate-btn:disabled { opacity: 0.6; cursor: default; }

  /* Loading / error */
  .loading { text-align: center; padding: 48px; color: #94a3b8; font-size: 14px; }
  .error-note { background: #fff7ed; border: 1px solid #fed7aa; border-radius: 8px;
    padding: 12px 16px; font-size: 13px; color: #9a3412; margin-bottom: 20px; }

  /* ── Chat panel ── */
  #chat-panel {
    display: none; flex-direction: column;
    height: calc(100vh - 109px);
  }
  #chat-panel.active { display: flex; }

  #chat-container { flex: 1; overflow-y: auto; padding: 20px 0; }
  #chat { max-width: 760px; margin: 0 auto; padding: 0 20px; }

  .row { display: flex; margin-bottom: 14px; }
  .row.user      { justify-content: flex-end; }
  .row.assistant { justify-content: flex-start; }
  .row.system    { justify-content: center; }

  .bubble {
    max-width: 72%; padding: 12px 16px; border-radius: 16px;
    font-size: 14.5px; line-height: 1.5; white-space: pre-wrap;
    box-shadow: 0 1px 2px rgba(0,0,0,0.06);
  }
  .row.user .bubble      { background: #2563eb; color: white; border-bottom-right-radius: 4px; }
  .row.assistant .bubble { background: white; color: #1e293b; border-bottom-left-radius: 4px; border: 1px solid #e2e8f0; }
  .row.system .bubble    { background: transparent; color: #94a3b8; font-style: italic; font-size: 13px; box-shadow: none; padding: 4px 12px; }

  .timestamp { font-size: 10.5px; color: #94a3b8; margin-top: 4px; }
  .row.assistant .timestamp { text-align: left; }
  .row.user .timestamp { text-align: right; }

  .typing-dots span {
    display: inline-block; width: 6px; height: 6px; margin: 0 1px;
    background: #94a3b8; border-radius: 50%;
    animation: blink 1.2s infinite ease-in-out;
  }
  .typing-dots span:nth-child(2) { animation-delay: 0.2s; }
  .typing-dots span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes blink { 0%,80%,100% { opacity:0.3; } 40% { opacity:1; } }

  #input-bar { background: white; border-top: 1px solid #e2e8f0; padding: 14px 20px; }
  #input-row { max-width: 760px; margin: 0 auto; display: flex; gap: 10px; }
  #question {
    flex: 1; padding: 12px 16px; font-size: 14.5px;
    border: 1px solid #cbd5e1; border-radius: 24px; outline: none;
    transition: border-color 0.15s;
  }
  #question:focus { border-color: #2563eb; }
  #ask-btn {
    padding: 0 24px; font-size: 14.5px; font-weight: 600;
    background: #2563eb; color: white; border: none; border-radius: 24px;
    cursor: pointer; transition: background 0.15s;
  }
  #ask-btn:hover:not(:disabled) { background: #1d4ed8; }
  #ask-btn:disabled { background: #93c5fd; cursor: default; }

  .examples { max-width: 760px; margin: 0 auto 12px; padding: 0 20px; display: flex; gap: 8px; flex-wrap: wrap; }
  .chip {
    background: white; border: 1px solid #cbd5e1; color: #475569;
    padding: 6px 14px; border-radius: 16px; font-size: 12.5px;
    cursor: pointer; transition: all 0.15s;
  }
  .chip:hover { background: #f1f5f9; border-color: #94a3b8; }

  @media (max-width: 640px) {
    .summary-row { grid-template-columns: repeat(2, 1fr); }
    .upload-card { flex-direction: column; align-items: flex-start; }
  }
</style>
</head>
<body>

<header>
  <div>
    <h1>MasonMart Data Assistant</h1>
    <p>Live dashboard + natural language chat</p>
  </div>
  <button id="refresh-btn" onclick="loadDashboard()">↻ Refresh</button>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('dashboard')">📊 Dashboard</div>
  <div class="tab" onclick="switchTab('chat')">💬 Chat</div>
  <div class="tab" onclick="switchTab('payroll')">🧾 Payroll</div>
</div>

<!-- DASHBOARD PANEL -->
<div id="dashboard-panel" class="panel active">
  <div id="dash-content" class="loading">Loading dashboard…</div>
</div>

<!-- CHAT PANEL -->
<div id="chat-panel" class="panel">
  <div id="chat-container">
    <div id="chat"></div>
  </div>
  <div id="input-bar">
    <div class="upload-card" id="upload-card" ondragover="handleUploadDragOver(event)" ondragleave="handleUploadDragLeave(event)" ondrop="handleUploadDrop(event)">
      <div class="upload-copy">
        <strong>Upload Today's Data</strong>
        <span>Drop a Callyzer CSV here or choose a file to sync the dashboard instantly.</span>
      </div>
      <div class="upload-actions">
        <input type="file" id="upload-input" accept=".csv,text/csv" style="display:none" onchange="handleUploadInputChange(event)">
        <button class="secondary-btn" id="upload-btn" onclick="openUploadPicker()">Choose CSV</button>
        <div class="upload-status" id="upload-status"></div>
      </div>
    </div>
    <div class="examples" id="examples">
      <div class="chip" onclick="useChip(this)">who made the most calls this week?</div>
      <div class="chip" onclick="useChip(this)">which IndiaMart leads are stale?</div>
      <div class="chip" onclick="useChip(this)">total sales this month</div>
      <div class="chip" onclick="useChip(this)">how many leads assigned to Sara are still open?</div>
    </div>
    <div id="input-row">
      <input type="text" id="question" placeholder="Type a question and press Enter…">
      <button id="ask-btn" onclick="ask()">Ask</button>
    </div>
  </div>
</div>

<!-- PAYROLL PANEL -->
<div id="payroll-panel" class="panel">
  <div id="payroll-content" class="loading">Loading payroll status…</div>
</div>

<script>
// ─── Tabs ───────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['dashboard','chat','payroll'][i]===name));
  document.getElementById('dashboard-panel').classList.toggle('active', name==='dashboard');
  document.getElementById('chat-panel').classList.toggle('active', name==='chat');
  document.getElementById('payroll-panel').classList.toggle('active', name==='payroll');
  if (name==='chat') document.getElementById('question').focus();
  if (name==='payroll') { payrollFlash = null; loadPayroll(); }
}

// ─── Dashboard ──────────────────────────────────────────
async function loadDashboard() {
  document.getElementById('dash-content').innerHTML = '<div class="loading">Loading dashboard…</div>';
  try {
    const resp = await fetch('/dashboard');
    const data = await resp.json();
    renderDashboard(data);
  } catch(e) {
    document.getElementById('dash-content').innerHTML =
      `<div class="error-note">Could not load dashboard: ${e}</div>`;
  }
}

function renderDashboard(d) {
  const reps = d.reps || [];
  const totals = d.totals || {};
  const freshness = d.freshness || {};
  const maxCalls = Math.max(...reps.map(r=>r.total_calls), 1);
  const maxConn  = Math.max(...reps.map(r=>r.connected_calls), 1);
  const maxValid = Math.max(...reps.map(r=>r.valid_connections), 1);
  const maxRevenue = Math.max(...reps.map(r=>r.attributed_revenue), 1);

  function pct(n, total) {
    if (!total) return '—';
    return Math.round(100*n/total)+'%';
  }
  function badge(rate) {
    if (rate >= 40) return `<span class="badge high">${rate}%</span>`;
    if (rate >= 20) return `<span class="badge mid">${rate}%</span>`;
    return `<span class="badge low">${rate}%</span>`;
  }
  function bar(val, max) {
    const w = max ? Math.round(100*val/max) : 0;
    return `<div class="bar-wrap">
      <div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div>
      <div class="bar-label">${val}</div>
    </div>`;
  }
  function money(n) {
    return '₹' + Math.round(n||0).toLocaleString('en-IN');
  }
  // Progress-vs-target cell. target null/undefined = never set; shows a
  // "set target" link instead of a misleading 0%.
  function targetCell(value, target, repSim, metric, fmt) {
    const editLink = `<a href="#" class="target-edit" onclick="setTarget('${repSim}','${metric}');return false;">${target==null?'set target':'edit'}</a>`;
    if (target == null) {
      return `<div class="target-cell"><span class="target-unset">no target</span> ${editLink}</div>`;
    }
    const p = target > 0 ? Math.round(100*value/target) : 0;
    const cls = p >= 100 ? 'high' : (p >= 50 ? 'mid' : 'low');
    return `<div class="target-cell">
      <span class="badge ${cls}">${p}%</span>
      <span class="target-sub">${fmt(value)} / ${fmt(target)}</span>
      ${editLink}
    </div>`;
  }

  // Sort reps by connected calls desc for the ranking
  const ranked = [...reps].sort((a,b) =>
    (b.connected_calls - a.connected_calls) || (b.valid_connections - a.valid_connections) || (b.total_calls - a.total_calls)
  );
  const topConnCount = ranked[0] ? ranked[0].connected_calls : 0;
  const topConn = topConnCount > 0 ? ranked[0].rep_name : 'No connected calls';
  const topValidCount = ranked[0] ? ranked[0].valid_connections : 0;

  const freshDate = freshness.latest_call_data_through
    ? freshness.latest_call_data_through.slice(0,10) : '—';
  const orderFreshDate = freshness.latest_order_data_through
    ? freshness.latest_order_data_through.slice(0,10) : '—';
  const today = freshness.todays_actual_date || '';
  const callStale = (freshDate !== today && freshDate !== '—')
    ? `⚠ Call data is only loaded through <strong>${freshDate}</strong> — today is ${today}. Upload today's Callyzer export to see current numbers.`
    : '';
  const orderStale = (orderFreshDate !== today && orderFreshDate !== '—')
    ? `⚠ Order data is only synced through <strong>${orderFreshDate}</strong> — today is ${today}. Run the Shopify sync to see current numbers.`
    : '';
  const staleNote = (callStale || orderStale)
    ? `<div class="error-note">${[callStale, orderStale].filter(Boolean).join('<br>')}</div>`
    : '';

  let rows = reps.map(r => {
    const connRate = r.total_calls ? Math.round(100*r.connected_calls/r.total_calls) : 0;
    const validRate = r.total_calls ? Math.round(100*r.valid_connections/r.total_calls) : 0;
    return `<tr>
      <td><strong>${r.rep_name}</strong></td>
      <td>${bar(r.total_calls, maxCalls)}</td>
      <td>${bar(r.connected_calls, maxConn)}</td>
      <td>${bar(r.valid_connections, maxValid)}</td>
      <td>${badge(connRate)}</td>
      <td>${badge(validRate)}</td>
      <td>${bar(r.attributed_revenue, maxRevenue)}<div class="target-sub">${r.attributed_orders} order(s)</div></td>
      <td>${r.avg_duration_sec ? Math.round(r.avg_duration_sec)+'s' : '—'}</td>
      <td>${r.outgoing_calls}</td>
      <td>${r.incoming_calls}</td>
      <td>${r.leads_assigned}</td>
      <td>${r.leads_attempted}</td>
      <td>${targetCell(r.calls_today, r.daily_call_target, r.rep_sim_number, 'calls', v=>v)}</td>
      <td>${targetCell(r.attributed_revenue, r.weekly_revenue_target, r.rep_sim_number, 'revenue', money)}</td>
    </tr>`;
  }).join('');

  if (!rows) rows = '<tr><td colspan="14" style="text-align:center;color:#94a3b8;padding:28px">No call data found for the last 7 days.</td></tr>';

  document.getElementById('dash-content').innerHTML = `
    ${staleNote}
    <div class="section-title">Last 7 Days — Team Summary</div>
    <div class="freshness-note">Data through ${freshDate} · Today is ${today}</div>

    <div class="summary-row">
      <div class="summary-card">
        <div class="label">Total Calls</div>
        <div class="value">${totals.total_calls ?? '—'}</div>
        <div class="sub">all reps combined</div>
      </div>
      <div class="summary-card">
        <div class="label">Connected Calls</div>
        <div class="value">${totals.connected_calls ?? '—'}</div>
        <div class="sub">${pct(totals.connected_calls, totals.total_calls)} with duration > 0s</div>
      </div>
      <div class="summary-card">
        <div class="label">Top Connector</div>
        <div class="value" style="font-size:18px">${topConn}</div>
        <div class="sub">${topConnCount} connected calls · ${topValidCount} valid connections</div>
      </div>
      <div class="summary-card">
        <div class="label">Valid Connections</div>
        <div class="value">${totals.valid_connections ?? '—'}</div>
        <div class="sub">${pct(totals.valid_connections, totals.total_calls)} with duration > 45s</div>
      </div>
      <div class="summary-card" title="An order counts here only if a rep's outgoing call to that customer landed in the 7 days before the order — not just any call ever.">
        <div class="label">Attributed Revenue</div>
        <div class="value">${money(totals.attributed_revenue)}</div>
        <div class="sub">${totals.attributed_orders ?? 0} order(s) traced to a call within 7 days</div>
      </div>
    </div>

    <div class="section-title">Employee Breakdown</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Rep</th>
            <th>Total Calls</th>
            <th>Connected</th>
            <th>Valid Connections</th>
            <th>Connect Rate</th>
            <th>Valid Rate</th>
            <th>Attributed Revenue (7d)</th>
            <th>Avg Duration</th>
            <th>Outgoing</th>
            <th>Incoming</th>
            <th>Leads Assigned</th>
            <th>Leads Attempted</th>
            <th>Calls Today vs Target</th>
            <th>Revenue (7d) vs Target</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

async function setTarget(repSim, metric) {
  const label = metric === 'calls' ? 'Daily call target' : 'Weekly revenue target (₹)';
  const val = prompt(label + ':');
  if (val === null || val.trim() === '') return;
  const num = Number(val);
  if (!Number.isFinite(num) || num < 0) { alert('Enter a valid non-negative number.'); return; }
  const body = { rep_sim_number: repSim };
  if (metric === 'calls') body.daily_call_target = num;
  else body.weekly_revenue_target = num;
  try {
    const resp = await fetch('/set-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'Failed to save target.');
    loadDashboard();
  } catch (e) {
    alert('Could not save target: ' + (e.message || e));
  }
}

function openUploadPicker() {
  document.getElementById('upload-input')?.click();
}
function setUploadStatus(text, isError) {
  const el = document.getElementById('upload-status');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? '#b91c1c' : '#64748b';
}
function setUploadBusy(isBusy) {
  const btnEl = document.getElementById('upload-btn');
  if (btnEl) btnEl.disabled = isBusy;
}
function handleUploadDragOver(event) {
  event.preventDefault();
  document.getElementById('upload-card')?.classList.add('dragover');
}
function handleUploadDragLeave(event) {
  event.preventDefault();
  document.getElementById('upload-card')?.classList.remove('dragover');
}
function handleUploadDrop(event) {
  event.preventDefault();
  document.getElementById('upload-card')?.classList.remove('dragover');
  const file = event.dataTransfer?.files?.[0];
  if (file) uploadCallyzerFile(file);
}
function handleUploadInputChange(event) {
  const file = event.target.files?.[0];
  if (file) uploadCallyzerFile(file);
  event.target.value = '';
}
async function uploadCallyzerFile(file) {
  if (!file) return;
  setUploadBusy(true);
  setUploadStatus(`Uploading ${file.name}...`, false);
  const form = new FormData();
  form.append('file', file, file.name);
  try {
    const resp = await fetch('/upload-callyzer', { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error(data.error || 'Upload failed');
    }
    setUploadStatus(data.message || 'Upload completed.', false);
    await loadDashboard();
    setUploadStatus(data.message || 'Upload completed.', false);
  } catch (e) {
    setUploadStatus(`Upload failed: ${e.message || e}`, true);
  } finally {
    setUploadBusy(false);
  }
}

// ─── Payroll ────────────────────────────────────────────
// Survives the full-panel re-render that loadPayroll() does, so an upload
// or generate result stays on screen instead of flashing away. {ok, text}.
let payrollFlash = null;

async function loadPayroll() {
  const el = document.getElementById('payroll-content');
  el.innerHTML = '<div class="loading">Loading payroll status…</div>';
  try {
    const resp = await fetch('/payroll/status');
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    renderPayroll(data);
  } catch (e) {
    el.innerHTML = `<div class="error-note">Could not load payroll status: ${e}</div>`;
  }
}

function renderPayroll(d) {
  const inputs = d.inputs || [];
  let flashHtml = '';
  if (payrollFlash) {
    const bg = payrollFlash.ok ? '#dcfce7' : '#fee2e2';
    const bd = payrollFlash.ok ? '#86efac' : '#fca5a5';
    const fg = payrollFlash.ok ? '#166534' : '#991b1b';
    const icon = payrollFlash.ok ? '✓' : '✕';
    flashHtml = `<div style="background:${bg};border:1px solid ${bd};color:${fg};border-radius:8px;padding:12px 14px;margin-bottom:14px;font-size:13px;display:flex;gap:8px;align-items:flex-start">
      <span style="font-weight:700">${icon}</span><span>${payrollFlash.text}</span>
    </div>`;
  }
  const channelTag = {
    chat: 'Chat tab upload', payroll: 'drop below', api: 'Shopify auto-sync',
    config: 'payroll_config.json', derived: 'auto-derived',
  };
  function inputRow(r) {
    return `
      <div class="payroll-status-row">
        <span class="badge ${r.satisfied ? 'high' : 'low'}">${r.satisfied ? 'Ready' : 'Needed'}</span>
        <strong>${r.name}</strong>
        <span class="channel-tag">${channelTag[r.channel] || r.channel}</span>
        <span class="target-sub">${r.detail}</span>
      </div>`;
  }
  const perfRows = inputs.filter(r => r.group === 'Performance').map(inputRow).join('');
  const salaryRows = inputs.filter(r => r.group === 'Salary').map(inputRow).join('');
  const readyCount = inputs.filter(r => r.satisfied).length;

  const reportsHtml = d.reports.length ? d.reports.map(r => `
    <div class="report-row">
      <span>${r.name}</span>
      <span class="target-sub">${r.generated_at} · ${r.size_kb} KB</span>
      <a href="/payroll/download/${encodeURIComponent(r.name)}" class="secondary-btn" style="text-decoration:none;display:inline-block;padding:6px 14px;">Download</a>
    </div>`).join('') : '<div class="target-sub">No reports generated yet.</div>';

  document.getElementById('payroll-content').innerHTML = `
    ${flashHtml}
    <div class="section-title">Payroll / Combined Audit Report</div>
    <div class="freshness-note">Mirrors MasonMart_Combined_Audit_Salary_Jun2026.xlsx — the 7 inputs below are exactly what that report was built from. ${readyCount} of ${inputs.length} ready.</div>

    <div class="section-title" style="margin-top:20px">Performance Inputs</div>
    <div class="table-wrap" style="padding:16px">${perfRows}</div>
    <div class="section-title" style="margin-top:16px">Salary Inputs</div>
    <div class="table-wrap" style="padding:16px">${salaryRows}</div>

    <div class="section-title" style="margin-top:20px">Drop Reports</div>
    <div class="upload-card" id="na-upload-card" ondragover="handlePayrollDragOver(event,'na')" ondragleave="handlePayrollDragLeave(event,'na')" ondrop="handlePayrollDrop(event,'na')">
      <div class="upload-copy">
        <strong>Never Attended Report</strong>
        <span>Drop the Callyzer "Never Attended Report" CSV export here.</span>
      </div>
      <div class="upload-actions">
        <input type="file" id="na-input" accept=".csv,text/csv" style="display:none" onchange="handlePayrollInputChange(event,'na')">
        <button class="secondary-btn" onclick="document.getElementById('na-input').click()">Choose CSV</button>
        <div class="upload-status" id="na-status"></div>
      </div>
    </div>
    <div class="upload-card" id="cm-upload-card" ondragover="handlePayrollDragOver(event,'cm')" ondragleave="handlePayrollDragLeave(event,'cm')" ondrop="handlePayrollDrop(event,'cm')">
      <div class="upload-copy">
        <strong>Customer → Salesperson Mapping</strong>
        <span>Drop a CSV with columns Customer Name / Customer Phone / Salesperson, OR a Callyzer Lead Data Report export (uses its Assign To field — see payroll/README.md for the caveat on that source).</span>
      </div>
      <div class="upload-actions">
        <input type="file" id="cm-input" accept=".csv,text/csv" style="display:none" onchange="handlePayrollInputChange(event,'cm')">
        <button class="secondary-btn" onclick="document.getElementById('cm-input').click()">Choose CSV</button>
        <div class="upload-status" id="cm-status"></div>
      </div>
    </div>

    <div class="section-title" style="margin-top:20px">Generate & Download</div>
    <div style="padding:0 16px 16px">
      <button id="generate-btn" onclick="generatePayrollReport()">Generate Report</button>
      <div class="upload-status" id="generate-status"></div>
    </div>
    <div class="table-wrap" style="padding:16px">${reportsHtml}</div>
  `;
}

function handlePayrollDragOver(event, kind) {
  event.preventDefault();
  document.getElementById(`${kind}-upload-card`)?.classList.add('dragover');
}
function handlePayrollDragLeave(event, kind) {
  event.preventDefault();
  document.getElementById(`${kind}-upload-card`)?.classList.remove('dragover');
}
function handlePayrollDrop(event, kind) {
  event.preventDefault();
  document.getElementById(`${kind}-upload-card`)?.classList.remove('dragover');
  const file = event.dataTransfer?.files?.[0];
  if (file) uploadPayrollFile(file, kind);
}
function handlePayrollInputChange(event, kind) {
  const file = event.target.files?.[0];
  if (file) uploadPayrollFile(file, kind);
  event.target.value = '';
}
async function uploadPayrollFile(file, kind) {
  const statusEl = document.getElementById(`${kind}-status`);
  const label = kind === 'na' ? 'Never Attended Report' : 'Customer mapping';
  const endpoint = kind === 'na' ? '/payroll/upload-never-attended' : '/payroll/upload-customer-map';
  if (statusEl) { statusEl.style.color = '#64748b'; statusEl.textContent = `Uploading ${file.name}…`; }
  const form = new FormData();
  form.append('file', file, file.name);
  try {
    const resp = await fetch(endpoint, { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.message || 'Upload failed');
    payrollFlash = { ok: true, text: `${label} — ${file.name}: ${data.message}` };
  } catch (e) {
    payrollFlash = { ok: false, text: `${label} — ${file.name} was NOT ingested. ${e.message || e}` };
  }
  await loadPayroll();  // re-render so the Ready/Needed badges AND the flash update together
}
async function generatePayrollReport() {
  const btn = document.getElementById('generate-btn');
  const statusEl = document.getElementById('generate-status');
  if (btn) btn.disabled = true;
  if (statusEl) { statusEl.style.color = '#64748b'; statusEl.textContent = 'Generating…'; }
  try {
    const resp = await fetch('/payroll/generate', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'Generation failed');
    payrollFlash = { ok: true, text: `Report generated: ${data.filename} — download it in the list below.` };
  } catch (e) {
    payrollFlash = { ok: false, text: `Report generation failed. ${e.message || e}` };
  }
  await loadPayroll();
}

// ─── Chat ───────────────────────────────────────────────
const chatContainer = document.getElementById('chat-container');
const chatEl = document.getElementById('chat');
const input = document.getElementById('question');
const btn = document.getElementById('ask-btn');
const examples = document.getElementById('examples');

function timeNow() {
  return new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}
function addRow(text, role, isTyping) {
  const row = document.createElement('div');
  row.className = 'row ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (isTyping) {
    bubble.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';
    row.id = 'typing-row';
  } else {
    bubble.textContent = text;
    const ts = document.createElement('div');
    ts.className = 'timestamp';
    ts.textContent = timeNow();
    row.appendChild(bubble);
    row.appendChild(ts);
  }
  row.appendChild(bubble);
  chatEl.appendChild(row);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}
function useChip(el) { input.value = el.textContent; ask(); }
async function ask() {
  const question = input.value.trim();
  if (!question) return;
  input.value = '';
  examples.style.display = 'none';
  addRow(question, 'user');
  addRow('', 'assistant', true);
  btn.disabled = true;
  try {
    const resp = await fetch('/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question})
    });
    const data = await resp.json();
    document.getElementById('typing-row')?.remove();
    addRow(data.answer, 'assistant');
  } catch(e) {
    document.getElementById('typing-row')?.remove();
    addRow('Something went wrong: '+e, 'system');
  }
  btn.disabled = false;
  input.focus();
}
input.addEventListener('keydown', e => { if (e.key==='Enter') ask(); });

async function loadHistory() {
  try {
    const resp = await fetch('/history');
    const data = await resp.json();
    if (data.history && data.history.length > 0) {
      examples.style.display = 'none';
      for (const turn of data.history) {
        addRow(turn.question, 'user');
        addRow(turn.answer, 'assistant');
      }
    }
  } catch(e) {}
}

// ─── Init ───────────────────────────────────────────────
loadDashboard();
loadHistory();
</script>
</body>
</html>
"""


def get_dashboard_data():
    """All dashboard numbers come from direct SQL — no LLM involved.
    Every number here is exact, not estimated."""
    conn = get_connection()
    try:
        reps = [dict(r) for r in conn.execute("""
            WITH call_stats AS (
                SELECT
                    c.rep_sim_number AS rep_key,
                    COALESCE(r.canonical_name, MIN(c.rep_name)) AS rep_name,
                    COUNT(*) AS total_calls,
                    COALESCE(SUM(CASE WHEN COALESCE(c.duration_seconds, 0) > 0 THEN 1 ELSE 0 END), 0) AS connected_calls,
                    COALESCE(SUM(CASE WHEN COALESCE(c.duration_seconds, 0) > 45 THEN 1 ELSE 0 END), 0) AS valid_connections,
                    SUM(CASE WHEN c.direction='outgoing' THEN 1 ELSE 0 END) AS outgoing_calls,
                    SUM(CASE WHEN c.direction='incoming' THEN 1 ELSE 0 END) AS incoming_calls,
                    ROUND(AVG(CASE WHEN COALESCE(c.duration_seconds, 0) > 0 THEN c.duration_seconds END), 1) AS avg_duration_sec,
                    SUM(CASE WHEN date(c.call_timestamp) = date('now', 'localtime') THEN 1 ELSE 0 END) AS calls_today
                FROM callyzer_calls c
                LEFT JOIN reps r ON r.rep_sim_number = c.rep_sim_number
                WHERE date(c.call_timestamp) >= date('now', 'localtime', '-7 days')
                  AND c.rep_sim_number IS NOT NULL
                  AND c.rep_sim_number != ''
                GROUP BY c.rep_sim_number
            ),
            -- Attribution: order credited to the rep whose most recent outgoing
            -- call to that customer fell in the 7 days before the order (see
            -- v_order_attribution in schema.sql). Far stronger than "this rep
            -- ever called this customer" — it's time-ordered, single-rep-credited.
            attribution_stats AS (
                SELECT
                    attributed_rep_sim AS rep_key,
                    COUNT(*) AS attributed_orders,
                    SUM(total_price) AS attributed_revenue
                FROM v_order_attribution
                WHERE attributed_rep_sim IS NOT NULL
                  AND date(created_at, 'localtime') >= date('now', 'localtime', '-7 days')
                GROUP BY attributed_rep_sim
            ),
            lead_stats AS (
                SELECT
                    ra.rep_sim_number AS rep_key,
                    COUNT(DISTINCT l.lead_no) AS leads_assigned,
                    COUNT(DISTINCT CASE WHEN l.no_of_attempts > 0 THEN l.lead_no END) AS leads_attempted
                FROM callyzer_leads l
                JOIN rep_name_aliases ra ON ra.alias_key = lower(trim(CASE
                    WHEN instr(l.assigned_to, '(') > 0
                    THEN substr(l.assigned_to, 1, instr(l.assigned_to, '(') - 1)
                    ELSE l.assigned_to
                END))
                WHERE l.assigned_to IS NOT NULL
                  AND l.assigned_to != ''
                  AND ra.rep_sim_number IS NOT NULL
                GROUP BY ra.rep_sim_number
            )
                SELECT
                    cs.rep_key AS rep_sim_number,
                    cs.rep_name,
                    cs.total_calls,
                    cs.connected_calls,
                    cs.valid_connections,
                    cs.outgoing_calls,
                    cs.incoming_calls,
                    cs.avg_duration_sec,
                    cs.calls_today,
                    COALESCE(ast.attributed_orders, 0) AS attributed_orders,
                    COALESCE(ast.attributed_revenue, 0.0) AS attributed_revenue,
                    COALESCE(ls.leads_assigned, 0) AS leads_assigned,
                    COALESCE(ls.leads_attempted, 0) AS leads_attempted,
                    t.daily_call_target,
                    t.weekly_revenue_target
            FROM call_stats cs
            LEFT JOIN attribution_stats ast ON ast.rep_key = cs.rep_key
            LEFT JOIN lead_stats ls ON ls.rep_key = cs.rep_key
            LEFT JOIN rep_targets t ON t.rep_sim_number = cs.rep_key
            ORDER BY cs.total_calls DESC
        """).fetchall()]

        totals = dict(conn.execute("""
            WITH call_totals AS (
                SELECT
                    COUNT(*) AS total_calls,
                    COALESCE(SUM(CASE WHEN COALESCE(duration_seconds, 0) > 0 THEN 1 ELSE 0 END), 0) AS connected_calls,
                    COALESCE(SUM(CASE WHEN COALESCE(duration_seconds, 0) > 45 THEN 1 ELSE 0 END), 0) AS valid_connections
                FROM callyzer_calls
                WHERE date(call_timestamp) >= date('now', 'localtime', '-7 days')
            ),
            attribution_totals AS (
                SELECT
                    COUNT(*) AS attributed_orders,
                    SUM(total_price) AS attributed_revenue
                FROM v_order_attribution
                WHERE attributed_rep_sim IS NOT NULL
                  AND date(created_at, 'localtime') >= date('now', 'localtime', '-7 days')
            ),
            lead_totals AS (
                SELECT
                    COUNT(DISTINCT lead_no) AS leads_assigned,
                    COUNT(DISTINCT CASE WHEN no_of_attempts > 0 THEN lead_no END) AS leads_attempted
                FROM callyzer_leads
            )
            SELECT
                ct.total_calls,
                ct.connected_calls,
                ct.valid_connections,
                COALESCE(at.attributed_orders, 0) AS attributed_orders,
                COALESCE(at.attributed_revenue, 0.0) AS attributed_revenue,
                lt.leads_assigned,
                lt.leads_attempted
            FROM call_totals ct
            CROSS JOIN attribution_totals at
            CROSS JOIN lead_totals lt
        """).fetchone())

        from datetime import date as _date
        latest_call = conn.execute("SELECT MAX(call_timestamp) FROM callyzer_calls").fetchone()[0]
        latest_order = conn.execute("SELECT MAX(created_at) FROM shopify_orders").fetchone()[0]
        freshness = {
            "todays_actual_date": _date.today().isoformat(),
            "latest_call_data_through": latest_call,
            "latest_order_data_through": latest_order,
        }
        return {"reps": reps, "totals": totals, "freshness": freshness}
    finally:
        conn.close()


def _parse_uploaded_file(headers, body):
    content_type = headers.get("Content-Type", "")
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if "multipart/form-data" not in content_type or not match:
        raise ValueError("Expected multipart/form-data upload.")

    boundary = ("--" + match.group(1)).encode("utf-8")
    for part in body.split(boundary):
        part = part.strip()
        if not part or part == b"--":
            continue
        header_blob, sep, file_blob = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        header_text = header_blob.decode("utf-8", "replace")
        if 'name="file"' not in header_text:
            continue
        filename_match = re.search(r'filename="([^"]*)"', header_text)
        filename = filename_match.group(1) if filename_match else "upload.csv"
        file_bytes = file_blob.rstrip(b"\r\n")
        if file_bytes.endswith(b"--"):
            file_bytes = file_bytes[:-2].rstrip(b"\r\n")
        return filename, file_bytes
    raise ValueError("No file was attached.")


def _ingest_uploaded_callyzer_file(filename, file_bytes):
    safe_name = os.path.basename(filename or "upload.csv")
    if not safe_name.lower().endswith(".csv"):
        raise ValueError("Please upload a CSV file.")

    os.makedirs(ingest_callyzer.INCOMING_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    upload_name = f"browser-{stamp}-{safe_name}"
    upload_path = os.path.join(ingest_callyzer.INCOMING_DIR, upload_name)

    with open(upload_path, "wb") as f:
        f.write(file_bytes)

    conn = get_connection()
    try:
        ingest_callyzer.process_file(conn, upload_path)
    finally:
        conn.close()

    return upload_name


def _set_rep_target(payload):
    rep_sim = (payload.get("rep_sim_number") or "").strip()
    if not rep_sim:
        raise ValueError("rep_sim_number is required.")
    daily_call_target = payload.get("daily_call_target")
    weekly_revenue_target = payload.get("weekly_revenue_target")
    if daily_call_target is None and weekly_revenue_target is None:
        raise ValueError("Provide daily_call_target and/or weekly_revenue_target.")
    for label, val in (("daily_call_target", daily_call_target), ("weekly_revenue_target", weekly_revenue_target)):
        if val is not None and (not isinstance(val, (int, float)) or val < 0):
            raise ValueError(f"{label} must be a non-negative number.")

    conn = get_connection()
    try:
        exists = conn.execute(
            "SELECT rep_sim_number FROM reps WHERE rep_sim_number = ?", (rep_sim,)
        ).fetchone()
        if not exists:
            raise ValueError("Unknown rep_sim_number — not in the rep directory.")

        row = conn.execute(
            "SELECT daily_call_target, weekly_revenue_target FROM rep_targets WHERE rep_sim_number = ?",
            (rep_sim,),
        ).fetchone()
        merged_calls = daily_call_target if daily_call_target is not None else (row["daily_call_target"] if row else None)
        merged_revenue = weekly_revenue_target if weekly_revenue_target is not None else (row["weekly_revenue_target"] if row else None)

        conn.execute(
            """INSERT INTO rep_targets (rep_sim_number, daily_call_target, weekly_revenue_target, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(rep_sim_number) DO UPDATE SET
                 daily_call_target=excluded.daily_call_target,
                 weekly_revenue_target=excluded.weekly_revenue_target,
                 updated_at=excluded.updated_at""",
            (rep_sim, merged_calls, merged_revenue, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def _payroll_status():
    """Everything the Payroll tab needs to render: the full 7-input
    provenance checklist (mirroring the June audit's own input list),
    grouped Performance / Salary, plus the list of generated files.

    Each input names HOW it flows in — some arrive here (drop card), some
    via the Chat tab's Callyzer upload, some via the Shopify API, and one
    (offer letters) is transcribed into payroll_config.json rather than
    parsed. The UI reads this so it always agrees with what's actually
    ingested, never a hardcoded assumption."""
    conn = get_connection()
    try:
        config = payroll_report.load_config()
        calls_n = conn.execute("SELECT COUNT(*) FROM callyzer_calls").fetchone()[0]
        never_attended_n = conn.execute("SELECT COUNT(*) FROM callyzer_never_attended").fetchone()[0]
        leads_n = conn.execute("SELECT COUNT(*) FROM callyzer_leads").fetchone()[0]
        reps_n = conn.execute("SELECT COUNT(*) FROM reps").fetchone()[0]
        orders_n = conn.execute("SELECT COUNT(*) FROM shopify_orders").fetchone()[0]
        customer_map_n = conn.execute("SELECT COUNT(*) FROM customer_salesperson_map").fetchone()[0]
        customer_map_manual_n = conn.execute(
            "SELECT COUNT(*) FROM customer_salesperson_map WHERE source = 'manual_csv'").fetchone()[0]
        customer_map_lead_n = conn.execute(
            "SELECT COUNT(*) FROM customer_salesperson_map WHERE source = 'lead_assignment'").fetchone()[0]
        valid_call_definition = config.get("valid_call_definition")
        employees = {k: v for k, v in config.get("employees", {}).items() if not k.startswith("_")}
        # "Offer letters" are considered captured once every configured
        # employee has their type-appropriate pay term filled in.
        offer_terms_ok = bool(employees) and all(
            (e.get("fixed_salary") is not None) if e.get("employment_type") == "full_time"
            else (e.get("per_call_rate") is not None)
            for e in employees.values()
        )
    finally:
        conn.close()

    # channel: how this input reaches the system.
    #   'payroll'  -> drop card on this tab
    #   'chat'     -> Chat tab's Callyzer CSV upload
    #   'api'      -> automatic Shopify sync (no manual step)
    #   'config'   -> transcribed into payroll_config.json (not parsed)
    #   'derived'  -> computed from another input, no separate file needed
    inputs = [
        {"group": "Performance", "name": "Periodic Call History",
         "channel": "chat", "satisfied": calls_n > 0,
         "detail": f"{calls_n:,} calls ingested" if calls_n else "Not ingested — upload on the Chat tab."},
        {"group": "Performance", "name": "Never Attended Report",
         "channel": "payroll", "endpoint": "never_attended", "satisfied": never_attended_n > 0,
         "detail": f"{never_attended_n:,} missed-call rows" if never_attended_n
                   else "Not ingested — drop the export below (format needs a real sample to confirm)."},
        {"group": "Performance", "name": "Lead Data Report",
         "channel": "chat", "satisfied": leads_n > 0,
         "detail": f"{leads_n:,} leads ingested" if leads_n else "Not ingested — upload on the Chat tab."},
        {"group": "Performance", "name": "Sales Person Info (employee → SIM)",
         "channel": "derived", "satisfied": reps_n > 0,
         "detail": f"{reps_n} rep(s) mapped — currently derived from call history (SIM + name). "
                   f"A dedicated Sales Person Info file isn't required, but would be authoritative."},
        {"group": "Salary", "name": "Orders Export",
         "channel": "api", "satisfied": orders_n > 0,
         "detail": f"{orders_n:,} orders — auto-synced from Shopify every 15 min (no upload needed)."
                   if orders_n else "No orders synced yet — check the Shopify sync."},
        {"group": "Salary", "name": "Customer Export (customer → salesperson)",
         "channel": "payroll", "endpoint": "customer_map", "satisfied": customer_map_n > 0,
         "detail": (
             (f"{customer_map_manual_n:,} manually mapped" if customer_map_manual_n else "")
             + (", " if customer_map_manual_n and customer_map_lead_n else "")
             + (f"{customer_map_lead_n:,} derived from lead assignment (⚠ who a LEAD was assigned to, "
                f"not a confirmed conversion — review before trusting for payout)" if customer_map_lead_n else "")
         ) if customer_map_n else "No mappings yet — drop a CSV below (Shopify orders carry no salesperson tag)."},
        {"group": "Salary", "name": "Offer Letters / Agreements",
         "channel": "config", "satisfied": offer_terms_ok,
         "detail": ("Salary terms captured in payroll_config.json"
                    + ("" if valid_call_definition
                       else " — but \"valid call\" definition still UNSET (blocks part-time pay).")
                    ) if offer_terms_ok else "Salary terms missing in payroll_config.json."},
    ]

    reports = []
    if os.path.isdir(payroll_report.OUTPUT_DIR):
        for name in os.listdir(payroll_report.OUTPUT_DIR):
            if not name.lower().endswith(".xlsx"):
                continue
            path = os.path.join(payroll_report.OUTPUT_DIR, name)
            stat = os.stat(path)
            reports.append({
                "name": name,
                "size_kb": round(stat.st_size / 1024, 1),
                "generated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                "mtime": stat.st_mtime,
            })
    reports.sort(key=lambda r: r["mtime"], reverse=True)

    return {
        "valid_call_definition": valid_call_definition,
        "never_attended_rows": never_attended_n,
        "customer_map_rows": customer_map_n,
        "inputs": inputs,
        "reports": reports,
    }


def _payroll_upload(headers, body, kind):
    """kind: 'never_attended' or 'customer_map'. Shares the same multipart
    parsing as the Callyzer upload — see _parse_uploaded_file."""
    filename, file_bytes = _parse_uploaded_file(headers, body)
    safe_name = os.path.basename(filename or "upload.csv")
    if not safe_name.lower().endswith(".csv"):
        raise ValueError("Please upload a CSV file.")

    payroll_dir = os.path.dirname(os.path.abspath(payroll_report.__file__))
    stamp = time.strftime("%Y%m%d-%H%M%S")

    if kind == "never_attended":
        os.makedirs(ingest_never_attended.INCOMING_DIR, exist_ok=True)
        upload_path = os.path.join(ingest_never_attended.INCOMING_DIR, f"browser-{stamp}-{safe_name}")
        with open(upload_path, "wb") as f:
            f.write(file_bytes)
        conn = get_connection()
        try:
            with open(upload_path, "r", encoding="utf-8-sig", newline="") as f:
                import csv as csv_module
                reader = csv_module.DictReader(f)
                if not reader.fieldnames:
                    return {"ok": False, "message": f"{safe_name} is empty or unreadable."}
                mapped_headers = ingest_never_attended._match_headers(reader.fieldnames)
                has_timestamp = "timestamp" in mapped_headers or \
                    ("call_date" in mapped_headers and "call_time" in mapped_headers)
                missing = []
                if not has_timestamp:
                    missing.append("a date/time column")
                if "rep_sim" not in mapped_headers:
                    missing.append("an employee/SIM number column")
                if "customer_number" not in mapped_headers:
                    missing.append("a customer/to-number column")
                if missing:
                    return {
                        "ok": False,
                        "message": (f"Refusing to guess: couldn't confidently find {', '.join(missing)}. "
                                    f"Headers seen: {reader.fieldnames}. Nothing was ingested — the file "
                                    f"format needs confirming (see payroll/README.md)."),
                    }
                read, ins, flg, dup = ingest_never_attended.ingest_never_attended(conn, upload_path, reader, mapped_headers)
            os.makedirs(ingest_never_attended.PROCESSED_DIR, exist_ok=True)
            import shutil as shutil_module
            shutil_module.move(upload_path, os.path.join(ingest_never_attended.PROCESSED_DIR, os.path.basename(upload_path)))
            return {"ok": True, "message": f"{safe_name}: read={read} inserted={ins} duplicate={dup} flagged={flg}"}
        finally:
            conn.close()

    elif kind == "customer_map":
        uploads_dir = os.path.join(payroll_dir, "incoming", "customer_map_uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        upload_path = os.path.join(uploads_dir, f"{stamp}-{safe_name}")
        with open(upload_path, "wb") as f:
            f.write(file_bytes)
        conn = get_connection()
        try:
            stats = ingest_customer_map.ingest_customer_map(conn, upload_path)
        finally:
            conn.close()

        if stats.get("format_error"):
            return {"ok": False, "message": stats["message"]}

        msg = (f"Mapped {stats['mapped']} customer(s). "
               f"{stats['unresolved_phone']} unresolvable phone(s), "
               f"{stats['unresolved_rep']} rep(s) with no call history yet.")
        return {"ok": True, "message": msg, "warnings": stats["warnings"]}

    else:
        raise ValueError(f"Unknown upload kind: {kind}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the console quiet

    def do_GET(self):
        if self.path == "/":
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/dashboard":
            try:
                data = get_dashboard_data()
            except Exception as e:
                data = {"error": str(e)}
            body = json.dumps(data, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/history":
            history = chat_query.get_recent_history(limit=20)
            body = json.dumps({"history": history}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/payroll/status":
            try:
                data = _payroll_status()
            except Exception as e:
                data = {"error": str(e)}
            body = json.dumps(data, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/payroll/download/"):
            requested = os.path.basename(self.path[len("/payroll/download/"):])
            # basename() strips any path components, so a '..' segment
            # can't escape OUTPUT_DIR regardless of how it's encoded.
            file_path = os.path.join(payroll_report.OUTPUT_DIR, requested)
            if not requested.lower().endswith(".xlsx") or not os.path.isfile(file_path):
                self.send_response(404)
                self.end_headers()
                return
            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{requested}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/ask":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                question = json.loads(raw).get("question", "")
                answer = chat_query.ask(question)
            except Exception as e:
                answer = f"Something went wrong answering that: {e}"
            body = json.dumps({"answer": answer}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/upload-callyzer":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                filename, file_bytes = _parse_uploaded_file(self.headers, raw)
                stored_name = _ingest_uploaded_callyzer_file(filename, file_bytes)
                payload = {
                    "ok": True,
                    "message": f"{stored_name} uploaded and synced into the dashboard."
                }
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/set-target":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
                _set_rep_target(payload)
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/payroll/upload-never-attended", "/payroll/upload-customer-map"):
            kind = "never_attended" if self.path.endswith("never-attended") else "customer_map"
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                result = _payroll_upload(self.headers, raw, kind)
                body = json.dumps(result).encode("utf-8")
                self.send_response(200 if result.get("ok") else 400)
            except Exception as e:
                body = json.dumps({"ok": False, "message": str(e)}).encode("utf-8")
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/payroll/generate":
            try:
                out_path = payroll_report.generate_report_file()
                payload = {"ok": True, "filename": os.path.basename(out_path)}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    if not chat_query.PRIMARY.configured:
        print(f"WARNING: {chat_query.PRIMARY.key_env} not set in .env — chat will fail.")

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"MasonMart Data Assistant running at {url}")
    print(f"Using {chat_query.PRIMARY.name} ({chat_query.PRIMARY.model})")
    print("Leave this window open. Close it to stop the assistant.")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
