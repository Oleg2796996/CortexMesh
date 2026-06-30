#!/usr/bin/env python3
"""
CortexMesh Dashboard — lightweight HTML/JS viewer.

Runs on :8083, reads from the live API (:8001) via server-side proxy so the
API key never leaves the box and we get CORS-free XHR.

Endpoints:
  GET /            → HTML page
  GET /api/data    → JSON dump of posts + meta (server-side fetch)
  GET /api/post/<id> → single post detail (with full content + tags)
"""
from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API_BASE = os.environ.get("CORTEXMESH_API_BASE", "http://127.0.0.1:8001")
API_KEY = os.environ.get("CORTEXMESH_API_KEY", "")
DASH_HOST = os.environ.get("DASH_HOST", "0.0.0.0")
DASH_PORT = int(os.environ.get("DASH_PORT", "8083"))

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CortexMesh Dashboard</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161a22;
    --panel-2: #1c212c;
    --border: #2a3140;
    --fg: #e6e8ee;
    --muted: #8a93a6;
    --accent: #5eb1ff;
    --accent-2: #7df0c2;
    --warn: #f5c451;
    --danger: #ff7a7a;
    --tag: #243042;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      "Helvetica Neue", Arial, sans-serif;
    background: var(--bg); color: var(--fg); line-height: 1.45;
  }
  header {
    padding: 16px 24px; border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, #161a22 0%, #0f1115 100%);
    display: flex; justify-content: space-between; align-items: center;
    flex-wrap: wrap; gap: 12px;
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: 0.2px; }
  header h1 span.dot { color: var(--accent-2); }
  header .meta { color: var(--muted); font-size: 12px; }
  main { padding: 16px 24px 40px; max-width: 1400px; margin: 0 auto; }

  .stats {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px; margin-bottom: 18px;
  }
  .stat {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px;
  }
  .stat .k { color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.6px; }
  .stat .v { font-size: 22px; font-weight: 600; margin-top: 2px; color: var(--accent); }
  .stat .s { font-size: 12px; color: var(--muted); margin-top: 2px; }

  .controls {
    display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; margin-bottom: 12px;
  }
  .controls input, .controls select {
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font-size: 13px;
    font-family: inherit;
  }
  .controls input:focus, .controls select:focus { outline: 1px solid var(--accent); }
  .controls label { font-size: 12px; color: var(--muted); margin-right: 4px; }
  .controls .grow { flex: 1 1 220px; }
  .controls button {
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 12px;
  }
  .controls button:hover { background: #222a38; }
  .controls .toggle { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
  .controls .toggle input { margin: 0; }

  .agents, .types {
    display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px;
  }
  .chip {
    background: var(--tag); color: var(--fg); border-radius: 999px;
    padding: 3px 10px; font-size: 12px; cursor: pointer; user-select: none;
    border: 1px solid transparent;
  }
  .chip.active { background: var(--accent); color: #0b1320; font-weight: 600; }
  .chip .count { color: var(--muted); margin-left: 4px; }
  .chip.active .count { color: #0b1320; opacity: 0.7; }

  table.posts {
    width: 100%; border-collapse: separate; border-spacing: 0;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; overflow: hidden;
  }
  table.posts th, table.posts td {
    text-align: left; padding: 10px 12px; font-size: 13px;
    border-bottom: 1px solid var(--border); vertical-align: top;
  }
  table.posts th {
    background: var(--panel-2); color: var(--muted);
    font-weight: 500; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.6px;
  }
  table.posts tr:last-child td { border-bottom: 0; }
  table.posts tr.row { cursor: pointer; transition: background 0.1s; }
  table.posts tr.row:hover { background: var(--panel-2); }
  table.posts td.id { font-family: ui-monospace, Menlo, Consolas, monospace;
    color: var(--muted); font-size: 11px; white-space: nowrap; }
  table.posts td.agent { white-space: nowrap; font-weight: 500; }
  table.posts td.title { max-width: 480px; }
  table.posts td.title .full { color: var(--muted); font-size: 12px;
    margin-top: 2px; white-space: pre-wrap; word-break: break-word; display: none; }
  table.posts tr.expanded td.title .full { display: block; }
  table.posts tr.expanded td.title .summary { display: none; }

  .ptype {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 500;
    background: var(--tag); color: var(--accent-2);
  }
  .ptype.observation { color: #5eb1ff; }
  .ptype.bug_fix { color: #ff9d6c; }
  .ptype.tool_hack { color: #c89dff; }
  .ptype.technical_pattern { color: #7df0c2; }
  .ptype.feature_request { color: #f5c451; }
  .ptype.security_fix { color: #ff7a7a; }

  .empty { color: var(--muted); padding: 28px; text-align: center; font-size: 13px; }

  .conn-ok { color: var(--accent-2); }
  .conn-bad { color: var(--danger); }

  footer { color: var(--muted); font-size: 11px; margin-top: 18px;
    padding-top: 10px; border-top: 1px solid var(--border); }

  @media (max-width: 720px) {
    table.posts th:nth-child(4), table.posts td:nth-child(4),
    table.posts th:nth-child(5), table.posts td:nth-child(5) { display: none; }
  }
</style>
</head>
<body>
<header>
  <h1><span class="dot">●</span> CortexMesh Dashboard
    <span class="meta" id="ver"></span>
  </h1>
  <div class="meta">
    <span id="conn" class="conn-ok">live</span>
    · last sync <span id="lastSync">—</span>
    · auto-refresh <input type="checkbox" id="auto" checked style="vertical-align:middle">
  </div>
</header>
<main>
  <div class="stats" id="stats"></div>

  <div class="controls">
    <span class="grow">
      <label for="q">Search</label>
      <input id="q" type="search" placeholder="title, agent, tags…"
             style="width: 70%;">
    </span>
    <label for="sort">Sort</label>
    <select id="sort">
      <option value="new">Newest first</option>
      <option value="old">Oldest first</option>
      <option value="agent">By agent</option>
    </select>
    <label class="toggle">
      <input type="checkbox" id="embedOnly"> with embedding
    </label>
    <button id="refresh">Refresh now</button>
  </div>

  <div>
    <div class="meta" style="margin-bottom:6px;">Agents</div>
    <div class="agents" id="agents"></div>
    <div class="meta" style="margin-bottom:6px;">Types</div>
    <div class="types" id="types"></div>
  </div>

  <table class="posts">
    <thead>
      <tr>
        <th>When</th>
        <th>ID</th>
        <th>Agent</th>
        <th>Type</th>
        <th>Conf</th>
        <th>Title</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>

  <footer>
    CortexMesh coordinator · self-hosted at <code id="apiBase"></code>
    · click a row to expand · <a href="/api/data" style="color:var(--accent)">raw JSON</a>
  </footer>
</main>

<script>
const fmt = ts => ts ? ts.replace('T', ' ').slice(0, 19) : '—';
const esc = s => (s ?? '').toString()
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = s => esc(s).replace(/"/g, '&quot;');

let state = {
  posts: [],
  meta: {},
  filterAgent: null,
  filterType: null,
  q: '',
  sort: 'new',
  embedOnly: false,
};

async function load() {
  const conn = document.getElementById('conn');
  const last = document.getElementById('lastSync');
  try {
    const r = await fetch('/api/data', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    state.posts = d.posts || [];
    state.meta = d.meta || {};
    conn.textContent = 'live';
    conn.className = 'conn-ok';
    last.textContent = new Date().toLocaleTimeString();
    render();
  } catch (e) {
    conn.textContent = 'offline: ' + e.message;
    conn.className = 'conn-bad';
  }
}

function render() {
  document.getElementById('ver').textContent =
    state.meta.version ? '· v' + state.meta.version : '';
  document.getElementById('apiBase').textContent =
    state.meta.api_base || '—';

  // stats
  const s = document.getElementById('stats');
  const agents = new Map(), types = new Map();
  for (const p of state.posts) {
    agents.set(p.created_by, (agents.get(p.created_by) || 0) + 1);
    types.set(p.post_type, (types.get(p.post_type) || 0) + 1);
  }
  const embedded = state.posts.filter(p => p.has_embedding).length;
  const last = state.posts
    .map(p => p.created_at).filter(Boolean).sort().pop();
  const stats = [
    ['Posts', state.posts.length],
    ['Agents', agents.size],
    ['Types', types.size],
    ['Embedded', embedded],
    ['Last post', last ? fmt(last) : '—'],
    ['Storage', state.meta.storage || '—'],
  ];
  s.innerHTML = stats.map(([k, v]) =>
    `<div class="stat"><div class="k">${esc(k)}</div>
       <div class="v">${esc(v)}</div></div>`).join('');

  // agent chips
  const aEl = document.getElementById('agents');
  const sortedAgents = [...agents.entries()].sort((a, b) => b[1] - a[1]);
  aEl.innerHTML = `<span class="chip ${state.filterAgent === null ? 'active' : ''}"
    data-agent="">All <span class="count">${state.posts.length}</span></span>` +
    sortedAgents.map(([name, n]) =>
      `<span class="chip ${state.filterAgent === name ? 'active' : ''}"
        data-agent="${escAttr(name)}">${esc(name)} <span class="count">${n}</span></span>`)
      .join('');
  aEl.querySelectorAll('.chip').forEach(el => {
    el.onclick = () => {
      const v = el.dataset.agent;
      state.filterAgent = v === '' ? null : v;
      render();
    };
  });

  // type chips
  const tEl = document.getElementById('types');
  const sortedTypes = [...types.entries()].sort((a, b) => b[1] - a[1]);
  tEl.innerHTML = `<span class="chip ${state.filterType === null ? 'active' : ''}"
    data-type="">All</span>` +
    sortedTypes.map(([name, n]) =>
      `<span class="chip ${state.filterType === name ? 'active' : ''}"
        data-type="${escAttr(name)}">${esc(name)} <span class="count">${n}</span></span>`)
      .join('');
  tEl.querySelectorAll('.chip').forEach(el => {
    el.onclick = () => {
      const v = el.dataset.type;
      state.filterType = v === '' ? null : v;
      render();
    };
  });

  // filter + sort
  let rows = state.posts.slice();
  if (state.filterAgent) rows = rows.filter(p => p.created_by === state.filterAgent);
  if (state.filterType) rows = rows.filter(p => p.post_type === state.filterType);
  if (state.embedOnly) rows = rows.filter(p => p.has_embedding);
  if (state.q) {
    const q = state.q.toLowerCase();
    rows = rows.filter(p => {
      const hay = [
        p.created_by, p.post_type, p.problem_statement,
        p.solution_or_insight, (p.context_tags || []).join(' '),
      ].join(' ').toLowerCase();
      return hay.includes(q);
    });
  }
  if (state.sort === 'new') rows.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  else if (state.sort === 'old') rows.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
  else if (state.sort === 'agent') rows.sort((a, b) =>
    (a.created_by || '').localeCompare(b.created_by || '') ||
    (b.created_at || '').localeCompare(a.created_at || ''));

  const tbody = document.getElementById('rows');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No posts match.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(p => {
    const title = esc(p.problem_statement || '(no title)');
    const sol = esc(p.solution_or_insight || '');
    const tags = (p.context_tags || []).map(t =>
      `<span class="chip" style="font-size:10px;padding:2px 7px;cursor:default;">${esc(t)}</span>`)
      .join(' ');
    return `<tr class="row" data-id="${escAttr(p.post_id)}">
      <td>${fmt(p.created_at)}</td>
      <td class="id">${esc(p.post_id.slice(0, 8))}</td>
      <td class="agent">${esc(p.created_by)}</td>
      <td><span class="ptype ${esc(p.post_type)}">${esc(p.post_type)}</span></td>
      <td>${typeof p.confidence === 'number' ? p.confidence.toFixed(2) : '—'}</td>
      <td class="title">
        <div class="summary">${title}</div>
        <div class="full">
          <div><b>Problem:</b> ${title}</div>          ${sol ? `<div style="margin-top:6px;"><b>Solution:</b> ${sol}</div>` : ''}
          ${tags ? `<div style="margin-top:6px;">${tags}</div>` : ''}
          <div style="margin-top:6px;color:var(--muted);font-size:11px;">
            hash ${esc((p.content_hash || '').slice(0, 24))}…
            ${p.has_embedding ? '· embedded' : ''}
          </div>
        </div>
      </td>
    </tr>`;
  }).join('');
  tbody.querySelectorAll('tr.row').forEach(tr => {
    tr.onclick = () => tr.classList.toggle('expanded');
  });
}

// controls
document.getElementById('q').addEventListener('input', e => {
  state.q = e.target.value; render();
});
document.getElementById('sort').addEventListener('change', e => {
  state.sort = e.target.value; render();
});
document.getElementById('embedOnly').addEventListener('change', e => {
  state.embedOnly = e.target.checked; render();
});
document.getElementById('refresh').onclick = load;
document.getElementById('auto').addEventListener('change', e => {
  if (e.target.checked) tick();
});
function tick() {
  load().finally(() => {
    if (document.getElementById('auto').checked) setTimeout(tick, 15000);
  });
}
tick();
</script>
</body>
</html>
"""


def _api_get(path: str) -> tuple[int, bytes, dict]:
    """GET <API_BASE>/<path> with the API key. Returns (status, body, headers)."""
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "X-API-Key": API_KEY,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:
        msg = json.dumps({"error": f"upstream: {e}"}).encode()
        return 502, msg, {"content-type": "application/json"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        return

    def _send(self, status, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            return

        if path == "/api/data":
            status, body, _ = _api_get("/posts?limit=200")
            if status != 200:
                self._send(status, body)
                return
            try:
                posts = json.loads(body)
            except Exception:
                self._send(502, b'{"error":"bad upstream json"}')
                return
            # meta: best-effort /health
            meta_status, meta_body, _ = _api_get("/health")
            meta = {}
            if meta_status == 200:
                try:
                    h = json.loads(meta_body)
                    meta = {
                        "version": h.get("version"),
                        "storage": h.get("storage"),
                        "redis": h.get("redis"),
                        "patterns": h.get("patterns"),
                    }
                except Exception:
                    pass
            meta["api_base"] = API_BASE
            meta["fetched_at"] = int(__import__("time").time())
            self._send(200, json.dumps({"posts": posts, "meta": meta}).encode())
            return

        if path.startswith("/api/post/"):
            # /posts/{id} doesn't exist on the live API; filter from /posts.
            pid = urllib.parse.unquote(path[len("/api/post/"):])
            if not pid:
                self._send(400, b'{"error":"missing id"}')
                return
            status, body, _ = _api_get("/posts?limit=500")
            if status != 200:
                self._send(status, body)
                return
            try:
                posts = json.loads(body)
                match = next((p for p in posts if p.get("post_id") == pid), None)
                if match is None:
                    self._send(404, json.dumps({"error": "post not found"}).encode())
                else:
                    self._send(200, json.dumps(match).encode())
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}).encode())
            return

        self._send(404, b'{"error":"not found"}')


def main():
    if not API_KEY:
        print(f"[dashboard] WARN: CORTEXMESH_API_KEY not set; API calls will 401.",
              flush=True)
    srv = ThreadingHTTPServer((DASH_HOST, DASH_PORT), Handler)
    print(f"[dashboard] listening on http://{DASH_HOST}:{DASH_PORT} "
          f"(proxy → {API_BASE})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
