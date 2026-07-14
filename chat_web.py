"""
chat_web.py
Local web interface for MasonMart Data Assistant.

Two sections on one page:
  1. Live Dashboard — employee-wise stats for the last 7 days, computed
     directly in SQLite (no LLM, exact numbers, loads fast).
  2. Chat — the natural language interface over the same data.

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
from common import get_connection
import ingest_callyzer

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

<script>
// ─── Tabs ───────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', ['dashboard','chat'][i]===name));
  document.getElementById('dashboard-panel').classList.toggle('active', name==='dashboard');
  document.getElementById('chat-panel').classList.toggle('active', name==='chat');
  if (name==='chat') document.getElementById('question').focus();
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
  const maxConverted = Math.max(...reps.map(r=>r.leads_converted), 1);

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
      <td>${bar(r.leads_converted, maxConverted)}</td>
      <td>${r.avg_duration_sec ? Math.round(r.avg_duration_sec)+'s' : '—'}</td>
      <td>${r.outgoing_calls}</td>
      <td>${r.incoming_calls}</td>
      <td>${r.leads_assigned}</td>
      <td>${r.leads_attempted}</td>
    </tr>`;
  }).join('');

  if (!rows) rows = '<tr><td colspan="12" style="text-align:center;color:#94a3b8;padding:28px">No call data found for the last 7 days.</td></tr>';

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
            <th>Converted Leads</th>
            <th>Avg Duration</th>
            <th>Outgoing</th>
            <th>Incoming</th>
            <th>Leads Assigned</th>
            <th>Leads Attempted</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
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
                    lower(rep_name) AS rep_key,
                    MIN(rep_name) AS rep_name,
                    COUNT(*) AS total_calls,
                    COALESCE(SUM(CASE WHEN COALESCE(duration_seconds, 0) > 0 THEN 1 ELSE 0 END), 0) AS connected_calls,
                    COALESCE(SUM(CASE WHEN COALESCE(duration_seconds, 0) > 45 THEN 1 ELSE 0 END), 0) AS valid_connections,
                    SUM(CASE WHEN direction='outgoing' THEN 1 ELSE 0 END) AS outgoing_calls,
                    SUM(CASE WHEN direction='incoming' THEN 1 ELSE 0 END) AS incoming_calls,
                    ROUND(AVG(CASE WHEN COALESCE(duration_seconds, 0) > 0 THEN duration_seconds END), 1) AS avg_duration_sec
                FROM callyzer_calls
                WHERE date(call_timestamp) >= date('now', 'localtime', '-7 days')
                  AND rep_name IS NOT NULL
                  AND rep_name != ''
                GROUP BY lower(rep_name)
            ),
            conversion_stats AS (
                SELECT
                    lower(c.rep_name) AS rep_key,
                    COUNT(DISTINCT o.customer_phone_norm) AS leads_converted
                FROM callyzer_calls c
                JOIN shopify_orders o
                    ON o.customer_phone_norm = c.customer_number_norm
                WHERE date(c.call_timestamp) >= date('now', 'localtime', '-7 days')
                  AND date(o.created_at, 'localtime') >= date('now', 'localtime', '-7 days')
                  AND c.rep_name IS NOT NULL
                  AND c.rep_name != ''
                  AND o.customer_phone_norm IS NOT NULL
                GROUP BY lower(c.rep_name)
            ),
            lead_stats AS (
                SELECT
                    lower(trim(CASE
                        WHEN instr(assigned_to, '(') > 0
                        THEN substr(assigned_to, 1, instr(assigned_to, '(') - 1)
                        ELSE assigned_to
                    END)) AS rep_key,
                    COUNT(DISTINCT lead_no) AS leads_assigned,
                    COUNT(DISTINCT CASE WHEN no_of_attempts > 0 THEN lead_no END) AS leads_attempted
                FROM callyzer_leads
                WHERE assigned_to IS NOT NULL
                  AND assigned_to != ''
                GROUP BY rep_key
            )
                SELECT
                    cs.rep_name,
                    cs.total_calls,
                    cs.connected_calls,
                    cs.valid_connections,
                    cs.outgoing_calls,
                    cs.incoming_calls,
                    cs.avg_duration_sec,
                    COALESCE(cv.leads_converted, 0) AS leads_converted,
                    COALESCE(ls.leads_assigned, 0) AS leads_assigned,
                COALESCE(ls.leads_attempted, 0) AS leads_attempted
            FROM call_stats cs
            LEFT JOIN conversion_stats cv ON cv.rep_key = cs.rep_key
            LEFT JOIN lead_stats ls ON ls.rep_key = cs.rep_key
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
            conversion_totals AS (
                SELECT COUNT(DISTINCT o.customer_phone_norm) AS leads_converted
                FROM callyzer_calls c
                JOIN shopify_orders o
                    ON o.customer_phone_norm = c.customer_number_norm
                WHERE date(c.call_timestamp) >= date('now', 'localtime', '-7 days')
                  AND date(o.created_at, 'localtime') >= date('now', 'localtime', '-7 days')
                  AND o.customer_phone_norm IS NOT NULL
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
                cv.leads_converted,
                lt.leads_assigned,
                lt.leads_attempted
            FROM call_totals ct
            CROSS JOIN conversion_totals cv
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
