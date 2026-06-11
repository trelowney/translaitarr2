// translAItarr2 anonymous active-instance counter.
//
// Receives a daily ping {id, version} from instances that have telemetry enabled
// and stores id -> {version} in KV with a 30-day TTL (refreshed on each ping).
// "Active instances" = distinct ids seen in the last ~30 days. No IPs are stored,
// nothing identifiable is kept — just a random id and the app version.

const TTL = 60 * 60 * 24 * 30; // 30 days
const ID_RE = /^[a-f0-9]{8,64}$/i;

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
  });
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
      return json({ active_instances: total, by_version: byVersion, window_days: 30 });
    }

    return json({ ok: true, service: "translaitarr2-telemetry" });
  },
};
