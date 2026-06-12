// translAItarr2 anonymous active-instance counter.
//
// Receives a daily ping {id, version} from instances that have telemetry enabled
// and stores id -> {version} in KV with a 30-day TTL (refreshed on each ping).
// "Active instances" = distinct ids seen in the last ~30 days. No IPs are stored,
// nothing identifiable is kept - just a random id and the app version.

const TTL = 60 * 60 * 24 * 30; // 30 days
const ID_RE = /^[a-f0-9]{8,64}$/i;

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
  });
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Compare version strings like "0.1.10" numerically, newest first.
function vcmp(a, b) {
  const pa = a.split(".").map((n) => parseInt(n, 10) || 0);
  const pb = b.split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pb[i] || 0) !== (pa[i] || 0)) return (pb[i] || 0) - (pa[i] || 0);
  }
  return 0;
}

// translAItarr2 dark theme - mirrors the app (bg #15171c, accent #4f9dff).
function statsPage(total, byVersion) {
  const versions = Object.keys(byVersion).sort(vcmp);
  const max = versions.reduce((m, v) => Math.max(m, byVersion[v]), 0) || 1;
  const rows = versions.map((v) => {
    const n = byVersion[v];
    const pct = Math.round((n / max) * 100);
    const label = v === "unknown" ? "unknown" : "v" + esc(v);
    return `<li><span class="vname">${label}</span>`
      + `<span class="bar"><span class="fill" style="width:${pct}%"></span></span>`
      + `<span class="vcount">${n}</span></li>`;
  }).join("");
  const favicon = "data:image/svg+xml,"
    + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
      + '<rect width="32" height="32" rx="7" fill="#15171c" stroke="#2d323c"/>'
      + '<text x="16" y="22" font-family="system-ui,sans-serif" font-size="15" '
      + 'font-weight="700" text-anchor="middle" fill="#4f9dff">AI</text></svg>');
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>translAItarr2 &mdash; active instances</title>
<link rel="icon" href="${favicon}">
<style>
  :root { --bg:#15171c; --card:#1b1e25; --border:#2d323c; --text:#e6e8ec;
          --dim:#9aa3b2; --accent:#4f9dff; }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background: radial-gradient(1200px 600px at 50% -10%, #1c2330 0%, var(--bg) 60%);
         color: var(--text); font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         padding: 24px; }
  .card { width:100%; max-width:520px; background:var(--card); border:1px solid var(--border);
          border-radius:16px; padding:32px 30px; box-shadow:0 12px 40px rgba(0,0,0,.35); }
  .brand { font-size:20px; font-weight:600; letter-spacing:.2px; margin:0 0 4px; }
  .brand .ai { color:var(--accent); }
  .tag { color:var(--dim); font-size:13px; margin:0 0 26px; }
  .count { font-size:72px; font-weight:800; line-height:1; letter-spacing:-1px;
           background:linear-gradient(180deg,#cfe2ff,var(--accent)); -webkit-background-clip:text;
           background-clip:text; color:transparent; }
  .count-label { color:var(--dim); font-size:14px; margin-top:8px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:var(--dim);
       margin:30px 0 12px; font-weight:600; }
  ul { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:9px; }
  li { display:grid; grid-template-columns:74px 1fr 42px; align-items:center; gap:12px; font-size:13px; }
  .vname { color:var(--text); font-variant-numeric:tabular-nums; }
  .vcount { text-align:right; color:var(--dim); font-variant-numeric:tabular-nums; }
  .bar { height:8px; background:#11141a; border:1px solid var(--border); border-radius:6px; overflow:hidden; }
  .fill { display:block; height:100%; background:linear-gradient(90deg,#3b7fe0,var(--accent)); border-radius:6px; }
  .empty { color:var(--dim); font-size:13px; }
  footer { margin-top:28px; padding-top:18px; border-top:1px solid var(--border);
           color:var(--dim); font-size:12px; line-height:1.6; }
  footer a { color:var(--accent); text-decoration:none; }
  footer a:hover { text-decoration:underline; }
</style>
</head>
<body>
  <main class="card">
    <p class="brand">transl<span class="ai">AI</span>tarr2</p>
    <p class="tag">Anonymous active-instance counter</p>
    <div class="count">${total.toLocaleString("en-US")}</div>
    <div class="count-label">active instances &middot; last 30 days</div>
    <h2>By version</h2>
    ${rows ? `<ul>${rows}</ul>` : '<p class="empty">No instances reporting yet.</p>'}
    <footer>
      Each instance sends only a random id and the app version, once a day &mdash; no IPs,
      file paths, API keys or library data. Counting is opt-out in Settings.
      <br><a href="https://github.com/trelowney/translaitarr2">github.com/trelowney/translaitarr2</a>
    </footer>
  </main>
</body>
</html>`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/ping") {
      let body;
      try { body = await request.json(); } catch { return json({ error: "bad json" }, 400); }
      const id = String(body.id || "");
      const version = String(body.version || "").slice(0, 32);
      if (!ID_RE.test(id)) return json({ error: "bad id" }, 400);
      // Store the version in the key's metadata so /stats can aggregate without
      // reading each value. TTL refreshes on every ping, so inactive ids fall off.
      await env.STATS.put(id, "", { metadata: { v: version }, expirationTtl: TTL });
      return json({ ok: true });
    }

    if (request.method === "GET" && url.pathname === "/stats") {
      let cursor, total = 0, done = false;
      const byVersion = {};
      while (!done) {
        const list = await env.STATS.list({ cursor, limit: 1000 });
        for (const k of list.keys) {
          total++;
          const v = (k.metadata && k.metadata.v) || "unknown";
          byVersion[v] = (byVersion[v] || 0) + 1;
        }
        cursor = list.cursor;
        done = list.list_complete;
      }
      // Browsers get the styled page; ?format=json or non-HTML clients get JSON.
      const wantsJson = url.searchParams.get("format") === "json"
        || !(request.headers.get("accept") || "").includes("text/html");
      if (wantsJson) {
        return json({ active_instances: total, by_version: byVersion, window_days: 30 });
      }
      return new Response(statsPage(total, byVersion), {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=300" },
      });
    }

    return json({ ok: true, service: "translaitarr2-telemetry" });
  },
};
