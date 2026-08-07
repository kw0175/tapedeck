// Cloudflare Worker: serves the UI and forwards /api/* to whichever machine is
// currently registered.
//
// The Worker cannot do the work itself - no Python, no ffmpeg, no filesystem to
// write your music folder to. It is the public front door; server.py on your PC
// is the thing that actually downloads.
//
// Quick tunnels get a new hostname on every restart, so the backend URL is not
// baked in. tunnel.py POSTs the current one to /_register on startup and the
// Worker stores it in KV.

import PAGE from "../../web/index.html";

const json = (status, obj) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // --- the local side announcing where it can be reached -------------------
    if (url.pathname === "/_register") {
      if (request.method !== "POST") return json(405, { error: "POST only" });
      if (!env.ADMIN_TOKEN) return json(500, { error: "ADMIN_TOKEN secret is not set" });
      if ((request.headers.get("X-Admin-Token") || "") !== env.ADMIN_TOKEN) {
        return json(401, { error: "bad admin token" });
      }
      let body;
      try {
        body = await request.json();
      } catch {
        return json(400, { error: "bad JSON" });
      }
      const backend = String(body.backend || "").replace(/\/+$/, "");
      // https only - this hop crosses the public internet.
      if (!/^https:\/\/[^\s/]+$/.test(backend)) {
        return json(400, { error: "backend must be a bare https:// origin" });
      }
      await env.STATE.put("backend", backend);
      return json(200, { ok: true, backend });
    }

    // --- proxy the API to that machine ---------------------------------------
    if (url.pathname.startsWith("/api/")) {
      const backend = await env.STATE.get("backend");
      if (!backend) {
        return json(503, {
          error: "No machine registered yet - start server.py and tunnel.py at home.",
        });
      }
      const headers = new Headers(request.headers);
      headers.delete("host");           // must not leak the workers.dev host onward
      let resp;
      try {
        resp = await fetch(backend + url.pathname + url.search, {
          method: request.method,
          headers,
          body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
        });
      } catch {
        return json(502, {
          error: "Machine unreachable - is the PC on and the tunnel running?",
        });
      }
      const out = new Headers(resp.headers);
      out.set("Cache-Control", "no-store");
      return new Response(resp.body, { status: resp.status, headers: out });
    }

    // --- the page -------------------------------------------------------------
    if (url.pathname === "/" || url.pathname === "/index.html") {
      return new Response(PAGE, {
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    }

    return json(404, { error: "not found" });
  },
};
