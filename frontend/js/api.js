/* ============================================================
   api.js — thin client for the AI Travel Planner FastAPI backend.

   Endpoints (from main.py):
     POST /plan              → create plan, returns paused at HITL gate
     GET  /plan/{id}         → poll live checkpoint state
     POST /plan/{id}/review  → resume: { action: approve|reject|modify, feedback }
     GET  /plan/{id}/final   → final plan once status == completed

   The base URL is configurable so the static frontend can point at a
   FastAPI server running on a different origin/port. Order of precedence:
     1) ?api=<url> query param   2) localStorage "wayfarer_api"   3) same origin
   ============================================================ */

const WayfarerAPI = (() => {
  function resolveBase() {
    const params = new URLSearchParams(location.search);
    const fromQuery = params.get('api');
    if (fromQuery) {
      localStorage.setItem('wayfarer_api', fromQuery);
      return fromQuery.replace(/\/$/, '');
    }
    const stored = localStorage.getItem('wayfarer_api');
    if (stored) return stored.replace(/\/$/, '');
    return ''; // same origin
  }

  let BASE = resolveBase();

  function url(path) {
    return `${BASE}${path}`;
  }

  async function request(path, options = {}) {
    const res = await fetch(url(path), {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });

    // 204 No Content
    if (res.status === 204) return null;

    let body = null;
    const text = await res.text();
    if (text) {
      try { body = JSON.parse(text); } catch { body = { raw: text }; }
    }

    if (!res.ok) {
      const detail = body && (body.detail || body.message || body.error);
      const err = new Error(detail || `Request failed (${res.status})`);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  return {
    get base() { return BASE; },
    setBase(b) { BASE = (b || '').replace(/\/$/, ''); localStorage.setItem('wayfarer_api', BASE); },

    /** Health probe — tries OpenAPI docs/openapi.json then root, with a timeout. */
    async ping() {
      const candidates = ['/openapi.json', '/docs', '/'];
      for (const c of candidates) {
        try {
          const ctrl = new AbortController();
          const t = setTimeout(() => ctrl.abort(), 2500);
          const res = await fetch(url(c), { method: 'GET', signal: ctrl.signal });
          clearTimeout(t);
          if (res.ok) return true;
        } catch { /* try next */ }
      }
      return false;
    },

    /** POST /plan */
    createPlan(payload) {
      return request('/plan', { method: 'POST', body: JSON.stringify(payload) });
    },

    /** GET /plan/{id} */
    getPlan(id) {
      return request(`/plan/${encodeURIComponent(id)}`, { method: 'GET' });
    },

    /** POST /plan/{id}/review  body: { action, feedback? } */
    review(id, action, feedback) {
      const body = { action };
      if (feedback != null && feedback !== '') body.feedback = feedback;
      return request(`/plan/${encodeURIComponent(id)}/review`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },

    /** GET /plan/{id}/final */
    getFinal(id) {
      return request(`/plan/${encodeURIComponent(id)}/final`, { method: 'GET' });
    },
  };
})();
