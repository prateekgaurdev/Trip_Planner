/* ============================================================
   api.js — thin client for the AI Travel Planner FastAPI backend.

   Endpoints (from main.py):
     POST /plan              → create plan (JSON, poll fallback)
     POST /plan/stream       → create plan + SSE stream
     GET  /plan/{id}         → poll live checkpoint state
     GET  /plan/{id}/stream  → SSE stream of live execution events
     POST /plan/{id}/review  → resume (JSON, poll fallback)
     POST /plan/{id}/review/stream → resume + SSE stream
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
    
    // Auto-detect fallback for cross-port usage (e.g. running on 5500 while API is on 8000)
    if (location.port && location.port !== '8000' && (location.hostname === 'localhost' || location.hostname === '127.0.0.1')) {
      return `http://${location.hostname}:8000`;
    }
    
    return ''; // same origin
  }

  let BASE = resolveBase();

  const TOKEN_KEY = 'wayfarer_auth_token';
  const USER_KEY = 'wayfarer_auth_user';

  function getToken() {
    return localStorage.getItem(TOKEN_KEY);
  }

  function setToken(token) {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  }

  function getUser() {
    try {
      const u = localStorage.getItem(USER_KEY);
      return u ? JSON.parse(u) : null;
    } catch {
      return null;
    }
  }

  function setUser(user) {
    if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
    else localStorage.removeItem(USER_KEY);
  }

  function url(path) {
    return `${BASE}${path}`;
  }

  async function request(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    const token = getToken();
    if (token && !headers['Authorization']) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    const res = await fetch(url(path), {
      headers,
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

  /**
   * Read an SSE text/event-stream response from a fetch() call.
   * Parses `data: {...}\n\n` lines and calls onEvent for each parsed JSON.
   * Returns an AbortController the caller can use to cancel.
   */
  function _readSSEStream(response, onEvent, onError, onDone) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    function processChunk({ done, value }) {
      if (done) {
        // Process any remaining buffer
        if (buffer.trim()) {
          _parseSSELines(buffer, onEvent);
        }
        onDone?.();
        return;
      }
      buffer += decoder.decode(value, { stream: true });

      // SSE messages are separated by double newlines
      const parts = buffer.split('\n\n');
      // Keep the last (possibly incomplete) chunk in buffer
      buffer = parts.pop() || '';

      for (const part of parts) {
        _parseSSELines(part, onEvent);
      }

      reader.read().then(processChunk).catch((err) => {
        if (err.name !== 'AbortError') {
          onError?.(err);
        }
      });
    }

    reader.read().then(processChunk).catch((err) => {
      if (err.name !== 'AbortError') {
        onError?.(err);
      }
    });
  }

  function _parseSSELines(block, onEvent) {
    for (const line of block.split('\n')) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          onEvent(data);
        } catch { /* skip malformed lines */ }
      }
    }
  }

  return {
    get base() { return BASE; },
    setBase(b) { BASE = (b || '').replace(/\/$/, ''); localStorage.setItem('wayfarer_api', BASE); },

    /** Health probe — tries OpenAPI docs/openapi.json then root, with a timeout. */
    async ping() {
      const candidates = ['/health', '/openapi.json', '/docs', '/'];
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

    /** Fetch server config including carto_api_key */
    async getConfig() {
      try {
        const res = await fetch(url('/health'));
        if (res.ok) return await res.json();
      } catch (err) {
        console.warn('Failed to load server config', err);
      }
      return {};
    },

    /** POST /plan (JSON fallback) */
    
    async chatPlace(payload) {
      const res = await request('/place/chat', { method: 'POST', body: JSON.stringify(payload) });
      return res;
    },
    async replacePlace(payload) {
      const res = await request('/place/replace', { method: 'POST', body: JSON.stringify(payload) });
      return res;
    },

    /** POST /plan (JSON fallback) */
    createPlan(payload) {
      return request('/plan', { method: 'POST', body: JSON.stringify(payload) });
    },

    /**
     * POST /plan/stream — create a plan and stream SSE events.
     * Returns { abort() } so the caller can cancel.
     */
    createPlanSSE(payload, onEvent, onError, onDone) {
      const ctrl = new AbortController();
      fetch(url('/plan/stream'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
      }).then((res) => {
        if (!res.ok) {
          res.text().then((txt) => {
            let detail = txt;
            try { detail = JSON.parse(txt).detail || txt; } catch {}
            const err = new Error(detail || `SSE request failed (${res.status})`);
            err.status = res.status;
            onError?.(err);
          });
          return;
        }
        _readSSEStream(res, onEvent, onError, onDone);
      }).catch((err) => {
        if (err.name !== 'AbortError') {
          onError?.(err);
        }
      });
      return { abort: () => ctrl.abort() };
    },

    /** GET /plan/{id} (poll fallback) */
    getPlan(id) {
      return request(`/plan/${encodeURIComponent(id)}`, { method: 'GET' });
    },

    /** GET /plan/{id}/stream — EventSource for status checks / session restore */
    streamPlan(id, onEvent, onError, onDone) {
      const source = new EventSource(url(`/plan/${encodeURIComponent(id)}/stream`));
      source.onmessage = (e) => {
        try {
          onEvent(JSON.parse(e.data));
        } catch { /* skip malformed */ }
      };
      source.onerror = (e) => {
        onError?.(e);
        source.close();
      };
      return source;
    },

    /** POST /plan/{id}/review (JSON fallback) */
    review(id, action, feedback, travelSelections) {
      const body = { action };
      if (feedback != null && feedback !== '') body.feedback = feedback;
      if (travelSelections) body.travel_selections = travelSelections;
      return request(`/plan/${encodeURIComponent(id)}/review`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
    },

    /**
     * POST /plan/{id}/review/stream — resume + SSE stream.
     * Returns { abort() } so the caller can cancel.
     */
    reviewSSE(id, action, feedback, travelSelections, onEvent, onError, onDone) {
      const reqBody = { action };
      if (feedback != null && feedback !== '') reqBody.feedback = feedback;
      if (travelSelections) reqBody.travel_selections = travelSelections;

      const ctrl = new AbortController();
      fetch(url(`/plan/${encodeURIComponent(id)}/review/stream`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(reqBody),
        signal: ctrl.signal,
      }).then((res) => {
        if (!res.ok) {
          res.text().then((txt) => {
            let detail = txt;
            try { detail = JSON.parse(txt).detail || txt; } catch {}
            const err = new Error(detail || `SSE review failed (${res.status})`);
            err.status = res.status;
            onError?.(err);
          });
          return;
        }
        _readSSEStream(res, onEvent, onError, onDone);
      }).catch((err) => {
        if (err.name !== 'AbortError') {
          onError?.(err);
        }
      });
      return { abort: () => ctrl.abort() };
    },

    /** GET /plan/{id}/final */
    getFinal(id) {
      return request(`/plan/${encodeURIComponent(id)}/final`, { method: 'GET' });
    },

    /** POST /images/lookup — one activity, safe to call in parallel from the UI */
    lookupActivityImage({ destination, title, location_name }) {
      return request('/images/lookup', {
        method: 'POST',
        body: JSON.stringify({
          destination,
          title: title || '',
          location_name: location_name || title || '',
        }),
      });
    },

    /** MCP (Model Context Protocol) endpoints */
    getPdfUrl(id) {
      return url(`/plan/${encodeURIComponent(id)}/pdf`);
    },

    getCalendarUrl(id) {
      return url(`/plan/${encodeURIComponent(id)}/calendar.ics`);
    },

    getMcpTools() {
      return request('/mcp/tools', { method: 'GET' });
    },

    /** Supabase Auth & Session */
    getToken,
    getUser,
    isLoggedIn() {
      return Boolean(getToken());
    },
    logout() {
      setToken(null);
      setUser(null);
    },
    async signup(payload) {
      const res = await request('/auth/signup', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (res && res.access_token) {
        setToken(res.access_token);
        setUser(res.user);
      }
      return res;
    },
    async login(payload) {
      const res = await request('/auth/login', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (res && res.access_token) {
        setToken(res.access_token);
        setUser(res.user);
      }
      return res;
    },
    async getMe() {
      const user = await request('/auth/me', { method: 'GET' });
      if (user) setUser(user);
      return user;
    },

    /** Community Trips & Reviews */
    getCommunityTrips({ destination = '', tag = '', sort = 'likes', limit = 40 } = {}) {
      const q = new URLSearchParams();
      if (destination) q.set('destination', destination);
      if (tag) q.set('tag', tag);
      if (sort) q.set('sort', sort);
      if (limit) q.set('limit', String(limit));
      return request(`/community/trips?${q.toString()}`, { method: 'GET' });
    },
    getCommunityTrip(id) {
      return request(`/community/trips/${encodeURIComponent(id)}`, { method: 'GET' });
    },
    createCommunityTrip(payload) {
      return request('/community/trips', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    },
    addCommunityReview(id, reviewData) {
      return request(`/community/trips/${encodeURIComponent(id)}/reviews`, {
        method: 'POST',
        body: JSON.stringify(reviewData),
      });
    },
    toggleCommunityLike(id) {
      return request(`/community/trips/${encodeURIComponent(id)}/like`, {
        method: 'POST',
      });
    },
    remixCommunityTrip(id, customInstruction = '') {
      return request(`/community/trips/${encodeURIComponent(id)}/remix-ai`, {
        method: 'POST',
        body: JSON.stringify({ custom_instruction: customInstruction }),
      });
    },
  };
})();

