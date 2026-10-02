/* ============================================================
   app.js — UI logic & the plan state machine.

   Flow:
     submit form → POST /plan → poll GET /plan/{id}
       status: researching → planning → awaiting_review → completed
     at awaiting_review: show review gate (approve / modify / reject)
     review → POST /plan/{id}/review → keep polling
     completed: GET /plan/{id}/final → render final itinerary
   ============================================================ */

(() => {
  'use strict';

  // ─── State ──────────────────────────────────────────────
  const SESSION_KEY = 'wayfarer_session';
  const state = {
    planId: null,
    status: null,
    interests: ['food', 'culture'],
    polling: false,
    pollTimer: null,
    restoring: false,
    poll404Retries: 0,
    progressTimer: null,
    lineTimer: null,
    lineIdx: 0,
    lineSlot: 'a',
    progressPct: 0,
    stepStartedAt: 0,
    lastServerMessage: '',
    workingPhase: null,
    travelPick: { flight: null, hotel: null },
    travelPickData: null,
    sseHandle: null,   // current SSE fetch handle (has .abort())
  };

  const STEPS = ['researching', 'planning', 'awaiting_review', 'finalizing', 'completed'];
  const STEP_META = {
    researching: { n: 1, label: 'Researching', title: 'Researching your destination…' },
    planning: { n: 2, label: 'Planning', title: 'Planning your itinerary…' },
    awaiting_review: { n: 3, label: 'Your review', title: 'Paused — waiting for your review.' },
    finalizing: { n: 4, label: 'Finalizing', title: 'Finalizing your approved trip…' },
    completed: { n: 5, label: 'Complete', title: 'Plan finalized.' },
  };
  const STEP_PROGRESS = {
    researching: { base: 8, max: 32 },
    planning: { base: 34, max: 58 },
    awaiting_review: { base: 60, max: 62 },
    finalizing: { base: 64, max: 94 },
    completed: { base: 100, max: 100 },
  };
  const PIPELINE_LINES = {
    researching: [
      'Starting research on your destination…',
      'Searching web, weather & currency in parallel…',
      'Tavily — scanning travel guides and local tips…',
      'Open-Meteo — checking forecast for your dates…',
      'Gemini — distilling highlights and practical tips…',
    ],
    planning: [
      'Research complete — building your day-by-day plan…',
      'Splitting budget across lodging, food & activities…',
      'Gemini.planner_draft — scheduling real venues by day…',
      'SerpAPI — fetching optional flight & hotel picks…',
      'Draft ready for your review…',
    ],
    finalizing: [
      'Approved — expanding your itinerary…',
      'Gemini.finalize_expand — adding times & descriptions…',
      'Geocoding every stop on your route…',
      'RouteMap — computing walking & driving legs…',
      'Almost there…',
    ],
    modifying: [
      'Sending your notes to the planner agent…',
      'Rebalancing days and activities…',
      'Gemini.planner_draft — revising with your feedback…',
    ],
    rejecting: [
      'Restarting — fresh web search and weather…',
      'Tavily — gathering new travel context…',
      'Gemini — rebuilding research from scratch…',
    ],
    awaiting_review: [
      'Draft ready — optionally pick a flight or hotel.',
      'Approve when you\'re happy, or request changes.',
      'Nothing finalizes without your say-so.',
    ],
  };
  const POLL_MS = 750;
  const POLL_MS_IDLE = 1200;

  // ─── DOM ────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const els = {
    form: $('#plan-form'),
    submitBtn: $('#submit-btn'),
    formError: $('#form-error'),
    interestChips: $('#interest-chips'),
    interestInput: $('#interest-input'),

    workspace: $('#workspace'),
    wsDestination: $('#ws-destination'),
    wsMeta: $('#ws-meta'),
    newPlanBtn: $('#new-plan-btn'),
    pipeline: $('#pipeline'),
    workingBanner: $('#working-banner'),
    workingText: $('#working-text'),
    progressStep: $('#progress-step'),
    progressFill: $('#progress-fill'),
    statusLineA: $('#status-line-a'),
    statusLineB: $('#status-line-b'),
    travelReview: $('#travel-review'),

    draftArea: $('#draft-area'),
    reviewGate: $('#review-gate'),
    feedbackBox: $('#feedback-box'),
    feedbackHint: $('#feedback-hint'),
    feedbackLabel: $('#feedback-label'),
    feedback: $('#feedback'),
    sendFeedback: $('#send-feedback'),
    cancelFeedback: $('#cancel-feedback'),
    itinerary: $('#itinerary'),

    apiStatus: $('#api-status'),
    apiStatusText: $('#api-status-text'),
    toastStack: $('#toast-stack'),
  };

  // ─── Helpers ────────────────────────────────────────────
  function toast(msg, kind = '', ms = 4200) {
    const t = document.createElement('div');
    t.className = `toast ${kind}`;
    const icon = kind === 'ok' ? 'circle-check' : kind === 'bad' ? 'circle-exclamation' : kind === 'warn' ? 'triangle-exclamation' : 'circle-info';
    t.innerHTML = `<i class="fa-solid fa-${icon}"></i><span></span>`;
    t.querySelector('span').textContent = msg;
    els.toastStack.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateY(8px)'; setTimeout(() => t.remove(), 300); }, ms);
  }

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (n, currency = 'USD') => {
    const symbol = { 'USD': '$', 'EUR': '€', 'GBP': '£', 'JPY': '¥', 'AUD': '$', 'CAD': '$', 'INR': '₹' }[currency] || currency + ' ';
    return symbol + Number(n || 0).toLocaleString('en-US', { maximumFractionDigits: 0 });
  };

  // ─── Session persistence (survives refresh) ─────────────
  function loadSession() {
    try {
      const raw = localStorage.getItem(SESSION_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function buildPayloadFromPrefs(prefs) {
    if (!prefs) return null;
    return {
      destination: prefs.destination,
      start_date: prefs.start_date,
      end_date: prefs.end_date,
      budget: prefs.budget,
      currency: prefs.currency || 'USD',
      travelers: prefs.travelers,
      interests: Array.isArray(prefs.interests) ? prefs.interests.slice() : [],
      origin: prefs.origin || '',
      flight_destination: prefs.flight_destination || '',
      include_flights: Boolean(prefs.include_flights),
      include_hotels: Boolean(prefs.include_hotels),
    };
  }

  function saveSession(extra = {}) {
    if (!state.planId) {
      localStorage.removeItem(SESSION_KEY);
      return;
    }
    const prev = loadSession();
    const prefs = extra.preferences || extra.prefs;
    const payload = extra.payload
      || (prefs ? buildPayloadFromPrefs(prefs) : null)
      || prev?.payload
      || null;
    const destination = extra.destination
      || prefs?.destination
      || prev?.destination
      || '';

    localStorage.setItem(SESSION_KEY, JSON.stringify({
      planId: state.planId,
      status: state.status,
      destination,
      payload,
      savedAt: new Date().toISOString(),
    }));
  }

  function clearSession() {
    localStorage.removeItem(SESSION_KEY);
  }

  function restoreFormFields(payload, destination) {
    if (!payload) return;
    if (destination) els.form.destination.value = destination;
    if (payload.start_date) els.form.start_date.value = payload.start_date;
    if (payload.end_date) els.form.end_date.value = payload.end_date;
    if (payload.budget != null) els.form.budget.value = payload.budget;
    if (payload.currency) els.form.currency.value = payload.currency;
    if (payload.travelers != null) els.form.travelers.value = payload.travelers;
    if (els.form.origin && payload.origin) els.form.origin.value = payload.origin;
    if (Array.isArray(payload.interests) && payload.interests.length) {
      state.interests = payload.interests.slice();
      renderChips();
    }
  }

  async function restoreSession() {
    const session = loadSession();
    if (!session?.planId) return;

    state.restoring = true;
    state.planId = session.planId;
    state.status = session.status || null;

    const payload = session.payload || {
      destination: session.destination,
      start_date: '',
      end_date: '',
      budget: 0,
      currency: 'USD',
      travelers: 2,
      interests: state.interests.slice(),
    };
    restoreFormFields(payload, session.destination);
    
    // Ensure default dates if restore didn't provide them
    if (!els.form.start_date.value || !els.form.end_date.value) {
      const dStart = new Date(Date.now() + 30 * 864e5);
      const dEnd = new Date(dStart.getTime() + 4 * 864e5);
      const iso = (d) => d.toISOString().slice(0, 10);
      els.form.start_date.value = iso(dStart);
      els.form.end_date.value = iso(dEnd);
      if (typeof updateNightCounter === "function") updateNightCounter();
    }

    try {
      const data = await WayfarerAPI.getPlan(session.planId);
      const prefs = data.preferences || {};
      const livePayload = buildPayloadFromPrefs(prefs) || payload;
      const destination = session.destination || prefs.destination || 'Your trip';

      openWorkspace(destination, livePayload, { restore: true });
      handleState(data);

      if (data.status === 'researching' || data.status === 'planning') {
        startPolling();
      }

      toast('Restored your trip plan.', 'ok');
      els.workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      if (err.status === 404) {
        clearSession();
        state.planId = null;
        state.status = null;
      } else {
        console.warn('session restore failed', err);
        toast('Could not restore your saved session. Is the API running?', 'warn');
      }
    } finally {
      state.restoring = false;
    }
  }

  // ─── Interests chips ────────────────────────────────────
  function renderChips() {
    els.interestChips.innerHTML = state.interests.map((it, i) =>
      `<span class="chip">${esc(it)}<button type="button" data-i="${i}" aria-label="Remove ${esc(it)}"><i class="fa-solid fa-xmark"></i></button></span>`
    ).join('');
    els.interestChips.querySelectorAll('button').forEach((b) =>
      b.addEventListener('click', () => {
        state.interests.splice(+b.dataset.i, 1);
        renderChips();
        saveSession();
      })
    );
  }
  function addInterest(val) {
    const v = val.trim().toLowerCase();
    if (v && !state.interests.includes(v)) {
      state.interests.push(v);
      renderChips();
      saveSession();
    }
  }
  els.interestInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addInterest(els.interestInput.value); els.interestInput.value = ''; }
  });

  // ─── API health pill ────────────────────────────────────
  // ─── Date Logic ───────────────────────────────────────────────
  function updateNightCounter() {
    const s = els.form.start_date.value;
    const e = els.form.end_date.value;
    const counter = document.getElementById('night-counter');
    if (!counter) return;
    if (s && e) {
      const ms = new Date(e) - new Date(s);
      const nights = Math.max(0, Math.round(ms / 864e5));
      const days = nights + 1;
      counter.textContent = `${nights} Night${nights !== 1 ? 's' : ''} · ${days} Day${days !== 1 ? 's' : ''}`;
    } else {
      counter.textContent = 'Select dates';
    }
  }
  if (els.form.start_date) els.form.start_date.addEventListener('change', updateNightCounter);
  if (els.form.end_date) els.form.end_date.addEventListener('change', updateNightCounter);

  // ─── UX Pacing & Themes ─────────────────────────────────────────
  let _selectedPace = 'Balanced';
  const _selectedThemes = new Set();
  
  document.querySelectorAll('.pace-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.pace-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _selectedPace = btn.dataset.pace;
    });
  });

  document.querySelectorAll('.theme-tag').forEach(btn => {
    btn.addEventListener('click', () => {
      const theme = btn.dataset.theme;
      if (_selectedThemes.has(theme)) {
        _selectedThemes.delete(theme);
        btn.classList.remove('selected');
      } else {
        _selectedThemes.add(theme);
        btn.classList.add('selected');
      }
    });
  });

  // ─── Mode Tabs Selection ─────────────────────────────────────
  document.querySelectorAll('.mode-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.mode-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const mode = tab.dataset.mode;
      if (mode === 'weekend') {
        const now = new Date();
        const start = new Date(now.getTime() + 7 * 864e5);
        const end = new Date(start.getTime() + 2 * 864e5);
        if (els.form.start_date) els.form.start_date.value = start.toISOString().split('T')[0];
        if (els.form.end_date) els.form.end_date.value = end.toISOString().split('T')[0];
        updateNightCounter();
        if (els.form.custom_notes) {
          els.form.custom_notes.value = 'Action-packed weekend getaway with highlights and rooftop dining.';
        }
      } else if (mode === 'romantic') {
        if (els.form.custom_notes) {
          els.form.custom_notes.value = 'Romantic honeymoon with slow mornings, private sunset dinner, and scenic walks.';
        }
      } else if (mode === 'wellness') {
        if (els.form.custom_notes) {
          els.form.custom_notes.value = 'Rejuvenating wellness retreat, daily yoga, clean dining, and serene nature.';
        }
      }
    });
  });

  // ─── Quick Fill from Trending Escapes & Vibe Pills ────────────
  function populateQuickTrip(opts) {
    if (opts.dest && els.form.destination) els.form.destination.value = opts.dest;
    if (opts.origin && els.form.origin) els.form.origin.value = opts.origin;
    if (opts.budget && els.form.budget) els.form.budget.value = opts.budget;
    if (opts.curr && els.form.currency) els.form.currency.value = opts.curr;
    if (opts.vision && els.form.custom_notes) els.form.custom_notes.value = opts.vision;

    if (opts.pacing) {
      document.querySelectorAll('.pace-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.pace.toLowerCase() === opts.pacing.toLowerCase());
      });
      _selectedPace = opts.pacing;
    }

    if (opts.theme) {
      document.querySelectorAll('.theme-tag').forEach(t => {
        if (t.dataset.theme && t.dataset.theme.toLowerCase().includes(opts.theme.toLowerCase())) {
          t.classList.add('selected');
          _selectedThemes.add(t.dataset.theme);
        }
      });
    }

    // Set default dates starting 14 days from today for 4 nights
    const now = new Date();
    const start = new Date(now.getTime() + 14 * 864e5);
    const end = new Date(start.getTime() + 4 * 864e5);
    if (els.form.start_date) els.form.start_date.value = start.toISOString().split('T')[0];
    if (els.form.end_date) els.form.end_date.value = end.toISOString().split('T')[0];
    updateNightCounter();

    // Pulse highlight the form card
    const card = document.getElementById('form-card');
    if (card) {
      card.classList.add('form-flash');
      setTimeout(() => card.classList.remove('form-flash'), 900);
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    toast(`Loaded ${opts.dest || 'destination'} details! Click 'Build My Detailed Itinerary' to start.`, 'ok');
  }

  document.querySelectorAll('.trending-escape-card').forEach((card) => {
    card.addEventListener('click', () => {
      populateQuickTrip({
        dest: card.dataset.dest,
        origin: card.dataset.origin,
        budget: card.dataset.budget,
        curr: card.dataset.curr,
        pacing: card.dataset.pacing,
        theme: card.dataset.theme,
        vision: card.dataset.vision,
      });
    });
  });

  document.querySelectorAll('.vibe-quick-pill').forEach((pill) => {
    pill.addEventListener('click', () => {
      populateQuickTrip({
        dest: pill.dataset.dest,
        origin: pill.dataset.origin,
        budget: pill.dataset.budget,
        curr: pill.dataset.curr,
        pacing: pill.dataset.pacing,
        theme: pill.dataset.theme,
        vision: pill.dataset.vision,
      });
    });
  });

  document.querySelectorAll('.marquee-loc-card').forEach((card) => {
    card.addEventListener('click', () => {
      populateQuickTrip({
        dest: card.dataset.dest,
        origin: card.dataset.origin,
        budget: card.dataset.budget,
        curr: card.dataset.curr,
        pacing: card.dataset.pacing,
        theme: card.dataset.theme,
        vision: card.dataset.vision,
      });
    });
  });

  document.querySelectorAll('.vision-chip').forEach((chip) => {
    chip.addEventListener('click', (e) => {
      e.preventDefault();
      const textarea = document.getElementById('custom_notes');
      if (!textarea) return;
      const prompt = chip.dataset.prompt;
      if (textarea.value.trim()) {
        textarea.value += ', ' + prompt;
      } else {
        textarea.value = prompt;
      }
      textarea.focus();
    });
  });

  let _apiCheckInterval = null;
  async function checkApi() {
    const ok = await WayfarerAPI.ping();
    els.apiStatus.classList.toggle('online', ok);
    els.apiStatus.classList.toggle('offline', !ok);
    els.apiStatusText.textContent = ok
      ? `API connected${WayfarerAPI.base ? '' : ' (same origin)'}`
      : 'API offline';
      
    // Once online, we can clear the aggressive interval and poll slowly or not at all
    if (ok && _apiCheckInterval) {
      clearInterval(_apiCheckInterval);
      _apiCheckInterval = null;
    }
  }

  // Set up background heartbeat
  _apiCheckInterval = setInterval(checkApi, 3000);

  // ─── Pipeline stepper ───────────────────────────────────
  function setPipeline(status) {
    const idx = STEPS.indexOf(status);
    els.pipeline.querySelectorAll('.step').forEach((li) => {
      const s = li.dataset.step;
      const si = STEPS.indexOf(s);
      li.classList.remove('active', 'done', 'gate-wait');
      if (status === 'completed') {
        li.classList.add('done');
      } else if (si < idx) {
        li.classList.add('done');
      } else if (si === idx) {
        li.classList.add(status === 'awaiting_review' ? 'gate-wait' : 'active');
      }
    });
  }

  function stopProgressAnim() {
    if (state.progressTimer) clearInterval(state.progressTimer);
    if (state.lineTimer) clearInterval(state.lineTimer);
    state.progressTimer = null;
    state.lineTimer = null;
  }

  function setStatusLine(text) {
    if (!text) return;
    const a = els.statusLineA;
    const b = els.statusLineB;
    const active = state.lineSlot === 'a' ? a : b;
    if (active.textContent === text) return;
    const next = state.lineSlot === 'a' ? b : a;
    next.textContent = text;
    next.classList.add('visible');
    active.classList.remove('visible');
    state.lineSlot = state.lineSlot === 'a' ? 'b' : 'a';
  }

  function pipelineKey(status) {
    if (status === 'planning' && state.reviewMode === 'modifying') return 'modifying';
    if (status === 'researching' && state.reviewMode === 'rejecting') return 'rejecting';
    return status;
  }

  function linesForStatus(status, serverMessage) {
    const key = pipelineKey(status);
    const lines = (PIPELINE_LINES[key] || PIPELINE_LINES[status] || []).slice();
    if (serverMessage && !lines.includes(serverMessage)) {
      lines.unshift(serverMessage);
    }
    return lines;
  }

  function bumpProgress(status) {
    const cfg = STEP_PROGRESS[status];
    if (!cfg || !els.progressFill) return;
    const elapsed = (Date.now() - state.stepStartedAt) / 1000;
    const creep = Math.min(1, elapsed / (status === 'finalizing' ? 28 : 18));
    const target = cfg.base + (cfg.max - cfg.base) * creep;
    state.progressPct = Math.max(state.progressPct, Math.min(cfg.max, target));
    els.progressFill.style.width = `${state.progressPct.toFixed(1)}%`;
  }

  function startProgressAnim(status, serverMessage) {
    stopProgressAnim();
    state.stepStartedAt = Date.now();
    const cfg = STEP_PROGRESS[status] || { base: 4, max: 12 };
    state.progressPct = cfg.base;
    if (els.progressFill) els.progressFill.style.width = `${state.progressPct}%`;

    const meta = STEP_META[status];
    if (meta && els.progressStep) {
      els.progressStep.textContent = `Step ${meta.n} of 5 · ${meta.label}`;
    }

    const lines = linesForStatus(status, serverMessage);
    if (lines.length) {
      state.lineIdx = 0;
      setStatusLine(lines[0]);
      if (lines.length > 1) {
        state.lineTimer = setInterval(() => {
          const fresh = linesForStatus(status, state.lastServerMessage);
          state.lineIdx = (state.lineIdx + 1) % fresh.length;
          setStatusLine(fresh[state.lineIdx]);
        }, 3200);
      }
    }

    state.progressTimer = setInterval(() => bumpProgress(status), 400);
  }

  function startGateDisplay(status, serverMessage) {
    stopProgressAnim();
    state.workingPhase = status;
    state.stepStartedAt = Date.now();
    const cfg = STEP_PROGRESS[status] || { base: 60, max: 62 };
    state.progressPct = cfg.base;
    if (els.progressFill) els.progressFill.style.width = `${state.progressPct}%`;

    const meta = STEP_META[status];
    if (meta && els.progressStep) {
      els.progressStep.textContent = `Step ${meta.n} of 5 · ${meta.label}`;
    }
    if (meta) els.workingText.textContent = meta.title;

    const lines = linesForStatus(status, serverMessage || state.lastServerMessage);
    if (lines.length) {
      state.lineIdx = 0;
      setStatusLine(lines[0]);
      if (lines.length > 1) {
        state.lineTimer = setInterval(() => {
          const fresh = linesForStatus(status, state.lastServerMessage);
          state.lineIdx = (state.lineIdx + 1) % fresh.length;
          setStatusLine(fresh[state.lineIdx]);
        }, 4000);
      }
    }
  }

  function showWorking(status, progressMessage, restartAnim = true) {
    const busy = ['researching', 'planning', 'finalizing'].includes(status) || !status;
    const atGate = status === 'awaiting_review';
    const visible = busy || atGate;

    els.workingBanner.style.display = visible ? 'flex' : 'none';
    els.workingBanner.classList.toggle('gate-pause', atGate);
    els.workingBanner.classList.toggle('over-draft', atGate || status === 'finalizing');

    const meta = STEP_META[status];
    if (meta && !atGate) els.workingText.textContent = meta.title || 'Agents are working…';
    else if (meta && atGate) els.workingText.textContent = meta.title;

    if (progressMessage) {
      state.lastServerMessage = progressMessage;
      setStatusLine(progressMessage);
    }

    if (busy) {
      const phase = pipelineKey(status);
      if (restartAnim || phase !== state.workingPhase) {
        state.workingPhase = phase;
        startProgressAnim(status, progressMessage || state.lastServerMessage);
      }
    } else if (atGate) {
      if (restartAnim || state.workingPhase !== status) {
        startGateDisplay(status, progressMessage || state.lastServerMessage);
      } else if (progressMessage) {
        setStatusLine(progressMessage);
      }
    } else {
      state.workingPhase = null;
      stopProgressAnim();
      if (!progressMessage) {
        els.statusLineA.textContent = '';
        els.statusLineB.textContent = '';
        els.statusLineA.classList.add('visible');
        els.statusLineB.classList.remove('visible');
        state.lineSlot = 'a';
      }
    }
  }

  // ─── Form submit → create plan ──────────────────────────
  els.form.addEventListener('submit', async (e) => {
    e.preventDefault();
    els.formError.hidden = true;

    const fd = new FormData(els.form);
    const destination = String(fd.get('destination') || '').trim();
    const start_date = String(fd.get('start_date') || '');
    const end_date = String(fd.get('end_date') || '');
    const budget = Number(fd.get('budget'));
    const currency = String(fd.get('currency') || 'USD');
    const travelers = Number(fd.get('travelers'));
    const origin = String(fd.get('origin') || '').trim();
    let custom_notes = String(fd.get('custom_notes') || '').trim();
    
    // Inject pacing and themes
    const themesStr = Array.from(_selectedThemes).join(', ');
    if (themesStr) {
      custom_notes = `Trip Themes: ${themesStr}.
${custom_notes}`.trim();
    }
    if (_selectedPace) {
      custom_notes = `Trip Pacing: ${_selectedPace}.
${custom_notes}`.trim();
    }

    const include_flights = !!origin;
    const include_hotels = true;

    // client-side validation mirroring schemas.py
    if (destination.length < 2) return formErr('Please enter a destination (at least 2 characters).', 'destination');
    if (!start_date || !end_date) {
      // Auto-fill dates if missing to prevent silent drops
      const dStart = new Date(Date.now() + 30 * 864e5);
      const dEnd = new Date(dStart.getTime() + 4 * 864e5);
      const iso = (d) => d.toISOString().slice(0, 10);
      els.form.start_date.value = iso(dStart);
      els.form.end_date.value = iso(dEnd);
      if (typeof updateNightCounter === "function") updateNightCounter();
      return formErr('Please choose both start and end dates. We have populated some default dates for you.', 'start_date');
    }
    if (new Date(end_date) < new Date(start_date)) return formErr('End date must be on or after the start date.', 'end_date');
    if (!(budget > 0)) return formErr('Budget must be greater than 0.', 'budget');
    if (!(travelers >= 1 && travelers <= 20)) return formErr('Travelers must be between 1 and 20.', 'travelers');

    const payload = {
      destination, start_date, end_date, budget, currency, travelers,
      interests: state.interests.slice(),
      origin, flight_destination: '',
      include_flights, include_hotels,
      custom_notes,
    };

    setSubmitting(true);
    els.workspace.hidden = false;
    els.workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
    openWorkspace(destination, payload);
    showWorking('researching', `Starting research on ${destination}…`, true);
    state.status = 'researching';

    try {
      state._sseDest = destination;
      stopSSEStream();
      stopPolling();

      state.sseHandle = WayfarerAPI.createPlanSSE(
        payload,
        (event) => { handleSSEEvent(event); },
        (err) => {
          // SSE failed — fall back to JSON create + polling
          console.warn('SSE create failed, falling back to JSON', err);
          stopSSEStream();
          (async () => {
            try {
              const res = await WayfarerAPI.createPlan(payload);
              state.planId = res && (res.plan_id || res.id || res.planId);
              if (!state.planId) throw new Error('No plan id returned by the server.');
              saveSession({ destination, payload });
              startPolling();
              poll();
            } catch (fallbackErr) {
              els.workspace.hidden = true;
              stopProgressAnim();
              els.workingBanner.style.display = 'none';
              formErr(fallbackErr.message || 'Could not create the plan. Is the API running?');
              toast(fallbackErr.message || 'Failed to create plan.', 'bad');
              setSubmitting(false);
            }
          
  // ─── Slide-Out Drawer Logic (Chat / Replace) ───────────────────────
  const elsDrawer = {
    overlay: document.getElementById('drawer-overlay'),
    drawer: document.getElementById('side-drawer'),
    closeBtn: document.getElementById('drawer-close'),
    img: document.getElementById('drawer-img'),
    title: document.getElementById('drawer-title'),
    loc: document.getElementById('drawer-loc'),
    body: document.getElementById('drawer-body'),
    chatFooter: document.getElementById('drawer-footer-chat'),
    chatInput: document.getElementById('chat-input-box'),
    chatSendBtn: document.getElementById('chat-send-btn')
  };

  let _drawerContext = null;

  function closeDrawer() {
    if (elsDrawer.overlay) elsDrawer.overlay.classList.remove('open');
    if (elsDrawer.drawer) elsDrawer.drawer.classList.remove('open');
    _drawerContext = null;
  }

  if (elsDrawer.closeBtn) elsDrawer.closeBtn.addEventListener('click', closeDrawer);
  if (elsDrawer.overlay) elsDrawer.overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDrawer();
  });

  // Extract activity from plan state
  function getActivityData(dayNum, idx) {
    const finalPlan = pickPlan(state.data);
    if (!finalPlan || !finalPlan.days) return null;
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == dayNum);
    if (!day || !day.activities || !day.activities[idx]) return null;
    const act = day.activities[idx];
    
    let title = ''; let locationName = ''; let time = ''; let desc = ''; let category = '';
    if (typeof act === 'string') {
      title = act.trim();
    } else {
      time = act.time || act.start || act.when || act.period || '';
      title = (act.title || act.name || act.activity || act.label || '').trim();
      locationName = (act.location_name || act.location || extractLandmarkKeyword(title) || title).trim();
      desc = act.description || act.detail || act.notes || '';
      category = act.category || '';
    }
    
    const searchTarget = locationName || title;
    const imgKey = activityImageKey(title, searchTarget);
    const imgSrc = _imageStore.get(imgKey) || 'https://images.unsplash.com/photo-1488646953014-85cb44e25828?auto=format&fit=crop&w=480&q=80';
    
    return { title, locationName, time, desc, category, searchTarget, imgSrc, actObj: act, dayNum, idx };
  }

  // Get all existing places across itinerary
  function getExistingPlaces() {
    const places = [];
    const finalPlan = pickPlan(state.data);
    if (!finalPlan || !finalPlan.days) return places;
    
    finalPlan.days.forEach(day => {
      if (!day.activities) return;
      day.activities.forEach(act => {
        let title = '';
        if (typeof act === 'string') title = act.trim();
        else title = (act.title || act.name || act.activity || act.label || '').trim();
        if (title) places.push(title);
      });
    });
    return places;
  }

  // Delegate clicks for hover actions
  els.itinerary.addEventListener('click', (e) => {
    const btn = e.target.closest('.action-btn');
    if (!btn) return;
    
    const action = btn.dataset.action;
    const dayNum = parseInt(btn.dataset.day, 10);
    const idx = parseInt(btn.dataset.idx, 10);
    
    const actData = getActivityData(dayNum, idx);
    if (!actData) return;
    
    if (action === 'remove') {
      handleRemove(actData);
    } else if (action === 'replace') {
      openReplaceDrawer(actData);
    } else if (action === 'chat') {
      openChatDrawer(actData);
    }
  });

  // Handle Remove
  let _lastRemoved = null; // For undo
  function handleRemove(actData) {
    const finalPlan = pickPlan(state.data);
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == actData.dayNum);
    
    // Save for undo
    _lastRemoved = {
      dayNum: actData.dayNum,
      idx: actData.idx,
      actObj: day.activities[actData.idx]
    };
    
    // Remove from array
    day.activities.splice(actData.idx, 1);
    
    // Update UI
    toast(`Removed ${esc(actData.title)} from Day ${actData.dayNum}. <button onclick="window.undoRemove()" style="background:transparent;border:0;color:inherit;text-decoration:underline;cursor:pointer;font-weight:bold;margin-left:8px;">Undo</button>`, 'ok');
    
    loadFinal(false); // Re-render itinerary and map
  }
  
  window.undoRemove = function() {
    if (!_lastRemoved) return;
    const finalPlan = pickPlan(state.data);
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == _lastRemoved.dayNum);
    if (day) {
      day.activities.splice(_lastRemoved.idx, 0, _lastRemoved.actObj);
      _lastRemoved = null;
      toast('Activity restored.', 'ok');
      loadFinal(false);
    }
  };

  // Open Chat Drawer
  function openChatDrawer(actData) {
    _drawerContext = { type: 'chat', data: actData, history: [] };
    
    elsDrawer.img.src = actData.imgSrc;
    elsDrawer.title.textContent = actData.title;
    const dest = (pickPlan(state.data).destination || els.wsDestination.textContent || '').split(',')[0].trim();
    elsDrawer.loc.textContent = dest;
    
    elsDrawer.chatFooter.hidden = false;
    
    // Initial UI state
    elsDrawer.body.innerHTML = `
      <div class="vibe-badges">
        <span class="vibe-badge">Loading Vibes...</span>
      </div>
      <div class="chat-stream" id="chat-stream">
        <div class="chat-bubble chat-ai">
          Hi! I'm your local insider for ${esc(actData.title)}. What would you like to know about the vibe, crowd, or tips?
        </div>
      </div>
      <div class="chat-chips" id="chat-chips">
        <button class="chat-chip" data-query="Is it good for a date night?">Is it good for a date night?</button>
        <button class="chat-chip" data-query="What is the dress code?">What is the dress code?</button>
        <button class="chat-chip" data-query="Are vegan options available?">Are vegan options available?</button>
        <button class="chat-chip" data-query="What's the crowd like?">What's the crowd like?</button>
      </div>
    `;
    
    elsDrawer.overlay.classList.add('open');
    elsDrawer.drawer.classList.add('open');
    elsDrawer.chatInput.focus();
  }

  // Handle sending chat messages
  async function sendChatMessage(query) {
    if (!query || !_drawerContext || _drawerContext.type !== 'chat') return;
    
    const stream = document.getElementById('chat-stream');
    const chipsDiv = document.getElementById('chat-chips');
    
    // Add user message
    stream.innerHTML += `<div class="chat-bubble chat-user">${esc(query)}</div>`;
    elsDrawer.chatInput.value = '';
    chipsDiv.innerHTML = ''; // Clear chips while loading
    elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    
    // Add loading indicator
    const loadingId = 'loading-' + Date.now();
    stream.innerHTML += `<div class="chat-bubble chat-ai" id="${loadingId}"><i class="fa-solid fa-circle-notch fa-spin"></i> Getting the vibe...</div>`;
    elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    
    const actData = _drawerContext.data;
    const payload = {
      place_name: actData.title,
      destination: pickPlan(state.data).destination || '',
      query: query,
      description: actData.desc,
      category: actData.category,
      chat_history: _drawerContext.history
    };
    
    // Append to history for next time
    _drawerContext.history.push({ role: 'user', content: query });
    
    try {
      const res = await WayfarerAPI.chatPlace(payload);
      
      // Update vibe tags in header if provided
      if (res.vibe_tags && res.vibe_tags.length) {
        const badgesHtml = res.vibe_tags.map(t => `<span class="vibe-badge">${esc(t)}</span>`).join('');
        const badgeContainer = elsDrawer.body.querySelector('.vibe-badges');
        if (badgeContainer) badgeContainer.innerHTML = badgesHtml;
      }
      
      // Replace loading bubble with AI response
      const loader = document.getElementById(loadingId);
      if (loader) loader.outerHTML = `<div class="chat-bubble chat-ai">${esc(res.reply)}</div>`;
      
      _drawerContext.history.push({ role: 'assistant', content: res.reply });
      
      // Update quick chips
      if (res.suggested_followups && res.suggested_followups.length) {
        chipsDiv.innerHTML = res.suggested_followups.map(q => `<button class="chat-chip" data-query="${esc(q)}">${esc(q)}</button>`).join('');
      }
      
      elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    } catch (err) {
      console.error("Chat error", err);
      const loader = document.getElementById(loadingId);
      if (loader) loader.outerHTML = `<div class="chat-bubble chat-ai" style="color:var(--bad);">Failed to fetch vibe. Please try again.</div>`;
    }
  }

  if (elsDrawer.chatSendBtn) {
    elsDrawer.chatSendBtn.addEventListener('click', () => sendChatMessage(elsDrawer.chatInput.value.trim()));
  }
  if (elsDrawer.chatInput) {
    elsDrawer.chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); sendChatMessage(elsDrawer.chatInput.value.trim()); }
    });
  }
  // Delegate clicks for chat chips
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('chat-chip')) {
      sendChatMessage(e.target.dataset.query);
    }
  });

  // Open Replace Drawer
  function openReplaceDrawer(actData) {
    _drawerContext = { type: 'replace', data: actData };
    
    elsDrawer.img.src = actData.imgSrc;
    elsDrawer.title.textContent = `Replacing: ${actData.title}`;
    elsDrawer.loc.textContent = `Day ${actData.dayNum} · ${actData.time}`;
    
    elsDrawer.chatFooter.hidden = true;
    
    elsDrawer.body.innerHTML = `
      <div class="replace-search-box">
        <button type="button" class="replace-preprompt" id="btn-preprompt">
          <i class="fa-solid fa-wand-magic-sparkles"></i> Suggest me some alternatives
        </button>
        <div class="replace-or">or</div>
        <div class="chat-input-area" style="padding:0; border:none; border-radius:999px;">
          <input type="text" id="replace-input" placeholder="e.g. cozy riverside cafe..." autocomplete="off" />
          <button type="button" class="chat-send-btn" id="replace-search-btn"><i class="fa-solid fa-magnifying-glass"></i></button>
        </div>
      </div>
      <div id="replace-results" style="display:flex; flex-direction:column; gap:16px;"></div>
    `;
    
    elsDrawer.overlay.classList.add('open');
    elsDrawer.drawer.classList.add('open');
    
    document.getElementById('btn-preprompt').addEventListener('click', () => doReplaceSearch(''));
    document.getElementById('replace-search-btn').addEventListener('click', () => {
      const val = document.getElementById('replace-input').value.trim();
      if (val) doReplaceSearch(val);
    });
    document.getElementById('replace-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const val = e.target.value.trim();
        if (val) doReplaceSearch(val);
      }
    });
  }

  async function doReplaceSearch(pref) {
    const resDiv = document.getElementById('replace-results');
    resDiv.innerHTML = `<div style="text-align:center; padding: 40px; color: var(--brand-ink);"><i class="fa-solid fa-circle-notch fa-spin fa-2x"></i><p style="margin-top:10px;font-weight:600;">Finding unique alternatives...</p></div>`;
    
    const actData = _drawerContext.data;
    const payload = {
      current_place: actData.title,
      destination: pickPlan(state.data).destination || '',
      time_slot: actData.time || 'Daytime',
      day_number: actData.dayNum,
      user_preference: pref,
      existing_places: getExistingPlaces()
    };
    
    try {
      const res = await WayfarerAPI.replacePlace(payload);
      
      if (!res.alternatives || !res.alternatives.length) {
        resDiv.innerHTML = `<p style="text-align:center; color:var(--bad);">No alternatives found. Try a different request.</p>`;
        return;
      }
      
      // Store globally for the swap button to access
      window._replaceAlts = res.alternatives;
      
      let html = '';
      res.alternatives.forEach((alt, i) => {
        html += `
          <div class="alt-card">
            <div class="alt-header">
              <div>
                <h4 class="alt-title">${esc(alt.title)}</h4>
                <div class="alt-loc">${esc(alt.location_name)}</div>
              </div>
              <div class="alt-cost">${money(alt.estimated_cost)}</div>
            </div>
            <p class="alt-desc">${esc(alt.description)}</p>
            <button type="button" class="btn-swap" data-alt-idx="${i}">Swap This In</button>
          </div>
        `;
      });
      resDiv.innerHTML = html;
      
    } catch (err) {
      console.error(err);
      resDiv.innerHTML = `<p style="text-align:center; color:var(--bad);">Failed to fetch alternatives. Ensure API is running.</p>`;
    }
  }

  // Handle Swap Click
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('btn-swap')) {
      const idx = parseInt(e.target.dataset.altIdx, 10);
      const alt = window._replaceAlts[idx];
      if (!alt || !_drawerContext || _drawerContext.type !== 'replace') return;
      
      const actData = _drawerContext.data;
      const finalPlan = pickPlan(state.data);
      const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == actData.dayNum);
      
      // Perform the swap
      const newAct = {
        title: alt.title,
        location_name: alt.location_name,
        time: alt.time || actData.time,
        description: alt.description,
        cost: alt.estimated_cost,
        category: alt.category
      };
      day.activities[actData.idx] = newAct;
      
      closeDrawer();
      toast(`Swapped in ${esc(alt.title)}!`, 'ok');
      loadFinal(false); // Re-render itinerary and map
    }
  });


})();
        },
        () => { state.sseHandle = null; }, // onDone
      );

      toast('Plan started — streaming live progress.', 'ok');
    } catch (err) {
      els.workspace.hidden = true;
      stopProgressAnim();
      els.workingBanner.style.display = 'none';
      formErr(err.message || 'Could not create the plan. Is the API running?');
      toast(err.message || 'Failed to create plan.', 'bad');
      setSubmitting(false);
    }
  });

  function formErr(msg, fieldName) {
    els.formError.textContent = msg;
    els.formError.hidden = false;
    
    // Remove previous highlights
    document.querySelectorAll('.input-wrap').forEach(el => el.classList.remove('invalid'));

    if (fieldName && els.form[fieldName]) {
      const field = els.form[fieldName];
      field.closest('.input-wrap').classList.add('invalid');
      field.focus();
      field.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else {
      els.formError.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    
    toast(msg, 'warn');
  }
  function setSubmitting(on) {
    els.submitBtn.disabled = on;
    if (on) {
      els.submitBtn.style.opacity = '0.8';
      els.submitBtn.style.cursor = 'not-allowed';
      els.submitBtn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> PLANNING...';
    } else {
      els.submitBtn.style.opacity = '1';
      els.submitBtn.style.cursor = 'pointer';
      els.submitBtn.innerHTML = 'BUILD MY DETAILED ITINERARY';
    }
  }

  function openWorkspace(destination, payload, opts = {}) {
    els.workspace.hidden = false;
    els.wsDestination.textContent = destination;
    const nights = Math.max(0, Math.round((new Date(payload.end_date) - new Date(payload.start_date)) / 864e5));
    els.wsMeta.textContent = `${payload.start_date} → ${payload.end_date} · ${nights} night${nights === 1 ? '' : 's'} · ${payload.travelers} traveler${payload.travelers === 1 ? '' : 's'} · ${money(payload.budget, payload.currency)}`;
    if (!opts.restore) {
      els.draftArea.hidden = true;
      els.reviewGate.hidden = true;
      els.itinerary.innerHTML = '';
      if (window.WayfarerMaps) window.WayfarerMaps.destroyAll();
      setPipeline('researching');
      els.workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // ─── Polling state machine (fallback) ──────────────────
  function startPolling() {
    stopPolling();
    state.polling = true;
    poll();
  }
  function stopPolling() {
    state.polling = false;
    state.poll404Retries = 0;
    if (state.pollTimer) clearTimeout(state.pollTimer);
  }

  async function poll() {
    if (!state.polling || !state.planId) return;
    try {
      const data = await WayfarerAPI.getPlan(state.planId);
      state.poll404Retries = 0;
      handleState(data);
    } catch (err) {
      console.warn('poll error', err);
      if (err && err.status === 404 && state.poll404Retries < 8) {
        state.poll404Retries += 1;
      } else if (err && err.status === 404) {
        stopPolling();
        clearSession();
        toast('Plan was deleted or not found.', 'bad');
      }
    }
    if (state.polling && state.status !== 'completed') {
      const delay = ['researching', 'planning', 'finalizing'].includes(state.status) ? POLL_MS : POLL_MS_IDLE;
      state.pollTimer = setTimeout(poll, delay);
    }
  }

  // ─── SSE streaming (primary) ────────────────────────────
  function stopSSEStream() {
    if (state.sseHandle) {
      try { state.sseHandle.abort(); } catch { /* already closed */ }
      state.sseHandle = null;
    }
  }

  const SSE_NODE_STATUS_MAP = {
    orchestrator: 'researching',
    research_fetch: 'researching',
    research_agent: 'researching',
    planner_agent: 'planning',
    human_gate: 'awaiting_review',
    finalize_expand: 'finalizing',
    finalize_pack: 'finalizing',
  };

  function handleSSEEvent(event) {
    // plan_created — capture plan_id
    if (event.event === 'plan_created' && event.plan_id) {
      state.planId = event.plan_id;
      saveSession({ destination: state._sseDest });
      return;
    }

    // error event
    if (event.event === 'error' || event.status === 'error') {
      toast(event.message || 'An error occurred.', 'bad');
      stopProgressAnim();
      els.workingBanner.style.display = 'none';
      setSubmitting(false);
      stopSSEStream();
      return;
    }

    // paused at HITL gate — render draft and review UI
    if (event.event === 'paused') {
      stopSSEStream();
      handleState(event);
      return;
    }

    // completed — render final plan
    if (event.event === 'completed' || event.status === 'completed') {
      stopSSEStream();
      state.status = 'completed';
      setPipeline('completed');
      showWorking('completed', '', false);
      loadFinal();
      return;
    }

    // Regular node progress update
    if (event.message) {
      setStatusLine(event.message);
    }
    const mapped = SSE_NODE_STATUS_MAP[event.node] || event.status;
    if (mapped && mapped !== state.status) {
      state.status = mapped;
      setPipeline(mapped);
      showWorking(mapped, event.message || '', true);
      saveSession();
    } else if (event.message) {
      showWorking(state.status, event.message, false);
    }
  }

  function onSSEError(err) {
    console.warn('SSE error, falling back to polling', err);
    stopSSEStream();
    if (state.planId && !['completed', 'awaiting_review'].includes(state.status)) {
      toast('Live stream interrupted — switching to polling.', 'warn');
      startPolling();
    }
  }

  function handleState(data) {
    if (!data) return;
    const status = data.status || data.plan_status;
    const progress = data.progress_message || data.progressMessage || '';
    const prevStatus = state.status;
    const statusChanged = status !== prevStatus;
    state.status = status;

    if (statusChanged) {
      if (status === 'awaiting_review' || status === 'completed') {
        state.reviewMode = null;
      }
      state.lastServerMessage = '';
      state.workingPhase = null;
    }

    setPipeline(status);
    showWorking(status, progress, statusChanged);
    saveSession({ preferences: data.preferences, destination: data.preferences?.destination });

    if (status === 'awaiting_review') {
      stopPolling();
      renderDraft(data);
      renderTravelReviewPicker(data);
      showReviewGate(true);
    } else if (status === 'completed') {
      stopPolling();
      loadFinal(state.restoring);
    } else {
      showReviewGate(false);
      if (status !== 'finalizing') {
        els.draftArea.hidden = true;
        els.itinerary.innerHTML = '';
      } else {
        els.draftArea.hidden = false;
      }
    }
  }

  // ─── Review gate ────────────────────────────────────────
  function showReviewGate(on) {
    if (on) {
      els.draftArea.hidden = false;
      els.reviewGate.hidden = false;
      els.feedbackBox.hidden = true;
    } else {
      els.reviewGate.hidden = true;
      if (els.travelReview) els.travelReview.hidden = true;
    }
  }

  function renderTravelReviewPicker(data) {
    const root = els.travelReview;
    if (!root) return;
    const opts = data.travel_options;
    const prefs = data.preferences || {};
    const currency = prefs.currency || 'USD';
    state.travelPickData = opts;
    state.travelPick = { flight: null, hotel: null };

    if (!opts?.recommendations?.has_picks) {
      root.hidden = true;
      root.innerHTML = '';
      return;
    }

    const rec = opts.recommendations;
    let html = `
      <div class="travel-review-inner">
        <h4><i class="fa-solid fa-hand-pointer"></i> Optional — pick flight &amp; hotel</h4>
        <p class="travel-review-sub">Skip if you only want the sightseeing plan. Selections appear on your approved itinerary (not on the route map).</p>`;

    if ((rec.flights || []).length) {
      html += `<p class="travel-section-label"><i class="fa-solid fa-plane"></i> Top flights</p><div class="travel-review-grid">`;
      rec.flights.forEach((f, idx) => {
        const o = f.offer || {};
        const depTime = o.departure_time ? ` · Dep ${esc(o.departure_time)}` : '';
        const fNum = o.flight_number ? ` · ${esc(o.flight_number)}` : '';
        html += `
          <div class="travel-pick-card" tabindex="0" role="button" data-pick-type="flight" data-pick-index="${idx}">
            <span class="pick-badge"><i class="fa-solid fa-plane"></i> ${esc(f.label)}</span>
            <strong>${esc(o.airlines || 'Flight')}${fNum}</strong>
            <span class="pick-meta">${esc(o.route_iata || '')} · ${esc(formatOfferPrice(o.price, currency))}</span>
            <span class="pick-meta">${esc(o.stops === 0 ? 'Non-stop' : `${o.stops} stop(s)`)} · ${formatFlightDuration(o.total_duration)}${depTime}</span>
            ${f.reason ? `<span class="pick-reason" style="display:block; font-size:0.80rem; color:var(--brand); margin:4px 0 2px;"><i class="fa-solid fa-check"></i> ${esc(f.reason)}</span>` : ''}
            <span class="pick-select-label">Tap to select</span>
            ${o.link ? `<a href="${esc(o.link)}" target="_blank" rel="noopener noreferrer" class="pick-open-link" onclick="event.stopPropagation()">Open link <i class="fa-solid fa-arrow-up-right-from-square"></i></a>` : ''}
          </div>`;
      });
      html += '</div>';
    }

    if ((rec.hotels || []).length) {
      html += `<p class="travel-section-label"><i class="fa-solid fa-hotel"></i> Top hotels</p><div class="travel-review-grid">`;
      rec.hotels.forEach((h, idx) => {
        const o = h.offer || {};
        const ratingStr = o.rating ? `<i class="fa-solid fa-star" style="color:#eab308;"></i> <strong>${esc(o.rating)}</strong>` : '';
        const priceStr = formatOfferPrice(o.price, currency);
        const reviewsStr = o.reviews ? ` (${esc(o.reviews)} reviews)` : '';
        html += `
          <div class="travel-pick-card" tabindex="0" role="button" data-pick-type="hotel" data-pick-index="${idx}">
            <span class="pick-badge pick-badge-hotel"><i class="fa-solid fa-hotel"></i> ${esc(h.label)}</span>
            <strong>${esc(o.name || 'Hotel')}</strong>
            <span class="pick-meta">${ratingStr}${reviewsStr} · ${esc(priceStr)}</span>
            ${h.reason ? `<span class="pick-reason" style="display:block; font-size:0.80rem; color:var(--brand); margin:4px 0 2px;"><i class="fa-solid fa-award"></i> ${esc(h.reason)}</span>` : ''}
            <span class="pick-select-label">Tap to select</span>
            ${o.link ? `<a href="${esc(o.link)}" target="_blank" rel="noopener noreferrer" class="pick-open-link" onclick="event.stopPropagation()">Open link <i class="fa-solid fa-arrow-up-right-from-square"></i></a>` : ''}
          </div>`;
      });
      html += '</div>';
    }

    html += `<p class="travel-review-skip"><i class="fa-solid fa-circle-info"></i> No selection? We keep your simple sightseeing-only flow.</p></div>`;

    root.innerHTML = html;
    root.hidden = false;

    root.querySelectorAll('.travel-pick-card').forEach((btn) => {
      btn.addEventListener('click', () => {
        const type = btn.dataset.pickType;
        const idx = parseInt(btn.dataset.pickIndex, 10);
        let offer = null;
        if (type === 'flight') {
          offer = (rec.flights || [])[idx]?.offer || null;
        } else if (type === 'hotel') {
          offer = (rec.hotels || [])[idx]?.offer || null;
        }
        
        const already = state.travelPick[type] === offer;
        state.travelPick[type] = already ? null : offer;
        
        root.querySelectorAll(`.travel-pick-card[data-pick-type="${type}"]`).forEach((el) => {
          const elIdx = parseInt(el.dataset.pickIndex, 10);
          const elOffer = type === 'flight' ? (rec.flights || [])[elIdx]?.offer : (rec.hotels || [])[elIdx]?.offer;
          const selected = state.travelPick[type] && state.travelPick[type] === elOffer;
          el.classList.toggle('selected', !!selected);
          el.querySelector('.pick-select-label').textContent = selected ? 'Selected ✓' : 'Tap to select';
        });
      });
      
      // Handle keyboard activation
      btn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          btn.click();
        }
      });
    });
  }

  function getTravelSelectionsForApprove() {
    const { flight, hotel } = state.travelPick;
    if (!flight && !hotel) return null;
    return { flight: flight || null, hotel: hotel || null };
  }

  const FEEDBACK_MODES = {
    modify: {
      hint: 'The planner will adjust the draft. Research and weather stay as-is.',
      label: 'What should the planner change?',
      placeholder: 'e.g. Add more food experiences and keep day 3 indoors.',
      button: '<i class="fa-solid fa-paper-plane"></i> Send to planner',
      sendClass: 'btn-primary',
    },
    reject: {
      hint: 'This discards the draft and restarts from research — web search, weather, and a brand-new plan.',
      label: 'Why restart? (helps the research agent)',
      placeholder: 'e.g. Focus on adventure sports, not temples. Shorter days.',
      button: '<i class="fa-solid fa-rotate-right"></i> Restart from scratch',
      sendClass: 'btn-reject',
    },
  };

  function openFeedbackMode(action) {
    const mode = FEEDBACK_MODES[action] || FEEDBACK_MODES.modify;
    els.feedback.dataset.pendingAction = action;
    els.feedbackHint.textContent = mode.hint;
    els.feedbackLabel.textContent = mode.label;
    els.feedback.placeholder = mode.placeholder;
    els.sendFeedback.className = `btn ${mode.sendClass}`;
    els.sendFeedback.innerHTML = mode.button;
    els.feedbackBox.hidden = false;
    els.feedback.value = '';
    els.feedback.focus();
  }

  function closeFeedbackMode() {
    els.feedbackBox.hidden = true;
    els.feedback.value = '';
    delete els.feedback.dataset.pendingAction;
    els.sendFeedback.className = 'btn btn-primary';
    els.sendFeedback.innerHTML = '<i class="fa-solid fa-paper-plane"></i> Send to planner';
  }

  els.reviewGate.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const action = btn.dataset.action;
      if (action === 'modify') {
        openFeedbackMode('modify');
        return;
      }
      if (action === 'reject') {
        openFeedbackMode('reject');
        return;
      }
      submitReview(action);
    });
  });
  els.cancelFeedback.addEventListener('click', closeFeedbackMode);
  els.sendFeedback.addEventListener('click', () => {
    const fb = els.feedback.value.trim();
    const action = els.feedback.dataset.pendingAction || 'modify';
    if (!fb) {
      toast(action === 'reject'
        ? 'Add a short note so the research agent knows what to fix.'
        : 'Add a note so the planner knows what to change.', 'warn');
      return;
    }
    closeFeedbackMode();
    submitReview(action, fb);
  });

  async function submitReview(action, feedback) {
    showReviewGate(false);
    closeFeedbackMode();
    els.draftArea.hidden = false;
    els.workingBanner.style.display = 'flex';

    state.reviewMode = action === 'modify' ? 'modifying' : action === 'reject' ? 'rejecting' : null;

    if (action === 'reject') {
      setPipeline('researching');
      showWorking('researching', 'Restarting — fresh web search and weather for your trip…', true);
    } else if (action === 'modify') {
      setPipeline('planning');
      showWorking('planning', 'Sending your notes to the planner agent…', true);
    } else {
      setPipeline('finalizing');
      showWorking('finalizing', 'Approved — expanding your itinerary and mapping your route…', true);
    }

    try {
      const travelSelections = action === 'approve' ? getTravelSelectionsForApprove() : null;

      stopSSEStream();
      stopPolling();

      state.sseHandle = WayfarerAPI.reviewSSE(
        state.planId,
        action,
        feedback,
        travelSelections,
        (event) => { handleSSEEvent(event); },
        (err) => {
          // SSE review failed — fall back to JSON review + polling
          console.warn('SSE review failed, falling back to JSON', err);
          stopSSEStream();
          (async () => {
            try {
              const data = await WayfarerAPI.review(state.planId, action, feedback, travelSelections);
              handleState(data);
              startPolling();
            } catch (fallbackErr) {
              toast(fallbackErr.message || 'Review failed.', 'bad');
              showReviewGate(true);
              setPipeline('awaiting_review');
              showWorking('awaiting_review', 'Draft ready — pick optional flight/hotel, then approve.', true);
            }
          
  // ─── Slide-Out Drawer Logic (Chat / Replace) ───────────────────────
  const elsDrawer = {
    overlay: document.getElementById('drawer-overlay'),
    drawer: document.getElementById('side-drawer'),
    closeBtn: document.getElementById('drawer-close'),
    img: document.getElementById('drawer-img'),
    title: document.getElementById('drawer-title'),
    loc: document.getElementById('drawer-loc'),
    body: document.getElementById('drawer-body'),
    chatFooter: document.getElementById('drawer-footer-chat'),
    chatInput: document.getElementById('chat-input-box'),
    chatSendBtn: document.getElementById('chat-send-btn')
  };

  let _drawerContext = null;

  function closeDrawer() {
    if (elsDrawer.overlay) elsDrawer.overlay.classList.remove('open');
    if (elsDrawer.drawer) elsDrawer.drawer.classList.remove('open');
    _drawerContext = null;
  }

  if (elsDrawer.closeBtn) elsDrawer.closeBtn.addEventListener('click', closeDrawer);
  if (elsDrawer.overlay) elsDrawer.overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDrawer();
  });

  // Extract activity from plan state
  function getActivityData(dayNum, idx) {
    const finalPlan = pickPlan(state.data);
    if (!finalPlan || !finalPlan.days) return null;
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == dayNum);
    if (!day || !day.activities || !day.activities[idx]) return null;
    const act = day.activities[idx];
    
    let title = ''; let locationName = ''; let time = ''; let desc = ''; let category = '';
    if (typeof act === 'string') {
      title = act.trim();
    } else {
      time = act.time || act.start || act.when || act.period || '';
      title = (act.title || act.name || act.activity || act.label || '').trim();
      locationName = (act.location_name || act.location || extractLandmarkKeyword(title) || title).trim();
      desc = act.description || act.detail || act.notes || '';
      category = act.category || '';
    }
    
    const searchTarget = locationName || title;
    const imgKey = activityImageKey(title, searchTarget);
    const imgSrc = _imageStore.get(imgKey) || 'https://images.unsplash.com/photo-1488646953014-85cb44e25828?auto=format&fit=crop&w=480&q=80';
    
    return { title, locationName, time, desc, category, searchTarget, imgSrc, actObj: act, dayNum, idx };
  }

  // Get all existing places across itinerary
  function getExistingPlaces() {
    const places = [];
    const finalPlan = pickPlan(state.data);
    if (!finalPlan || !finalPlan.days) return places;
    
    finalPlan.days.forEach(day => {
      if (!day.activities) return;
      day.activities.forEach(act => {
        let title = '';
        if (typeof act === 'string') title = act.trim();
        else title = (act.title || act.name || act.activity || act.label || '').trim();
        if (title) places.push(title);
      });
    });
    return places;
  }

  // Delegate clicks for hover actions
  els.itinerary.addEventListener('click', (e) => {
    const btn = e.target.closest('.action-btn');
    if (!btn) return;
    
    const action = btn.dataset.action;
    const dayNum = parseInt(btn.dataset.day, 10);
    const idx = parseInt(btn.dataset.idx, 10);
    
    const actData = getActivityData(dayNum, idx);
    if (!actData) return;
    
    if (action === 'remove') {
      handleRemove(actData);
    } else if (action === 'replace') {
      openReplaceDrawer(actData);
    } else if (action === 'chat') {
      openChatDrawer(actData);
    }
  });

  // Handle Remove
  let _lastRemoved = null; // For undo
  function handleRemove(actData) {
    const finalPlan = pickPlan(state.data);
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == actData.dayNum);
    
    // Save for undo
    _lastRemoved = {
      dayNum: actData.dayNum,
      idx: actData.idx,
      actObj: day.activities[actData.idx]
    };
    
    // Remove from array
    day.activities.splice(actData.idx, 1);
    
    // Update UI
    toast(`Removed ${esc(actData.title)} from Day ${actData.dayNum}. <button onclick="window.undoRemove()" style="background:transparent;border:0;color:inherit;text-decoration:underline;cursor:pointer;font-weight:bold;margin-left:8px;">Undo</button>`, 'ok');
    
    loadFinal(false); // Re-render itinerary and map
  }
  
  window.undoRemove = function() {
    if (!_lastRemoved) return;
    const finalPlan = pickPlan(state.data);
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == _lastRemoved.dayNum);
    if (day) {
      day.activities.splice(_lastRemoved.idx, 0, _lastRemoved.actObj);
      _lastRemoved = null;
      toast('Activity restored.', 'ok');
      loadFinal(false);
    }
  };

  // Open Chat Drawer
  function openChatDrawer(actData) {
    _drawerContext = { type: 'chat', data: actData, history: [] };
    
    elsDrawer.img.src = actData.imgSrc;
    elsDrawer.title.textContent = actData.title;
    const dest = (pickPlan(state.data).destination || els.wsDestination.textContent || '').split(',')[0].trim();
    elsDrawer.loc.textContent = dest;
    
    elsDrawer.chatFooter.hidden = false;
    
    // Initial UI state
    elsDrawer.body.innerHTML = `
      <div class="vibe-badges">
        <span class="vibe-badge">Loading Vibes...</span>
      </div>
      <div class="chat-stream" id="chat-stream">
        <div class="chat-bubble chat-ai">
          Hi! I'm your local insider for ${esc(actData.title)}. What would you like to know about the vibe, crowd, or tips?
        </div>
      </div>
      <div class="chat-chips" id="chat-chips">
        <button class="chat-chip" data-query="Is it good for a date night?">Is it good for a date night?</button>
        <button class="chat-chip" data-query="What is the dress code?">What is the dress code?</button>
        <button class="chat-chip" data-query="Are vegan options available?">Are vegan options available?</button>
        <button class="chat-chip" data-query="What's the crowd like?">What's the crowd like?</button>
      </div>
    `;
    
    elsDrawer.overlay.classList.add('open');
    elsDrawer.drawer.classList.add('open');
    elsDrawer.chatInput.focus();
  }

  // Handle sending chat messages
  async function sendChatMessage(query) {
    if (!query || !_drawerContext || _drawerContext.type !== 'chat') return;
    
    const stream = document.getElementById('chat-stream');
    const chipsDiv = document.getElementById('chat-chips');
    
    // Add user message
    stream.innerHTML += `<div class="chat-bubble chat-user">${esc(query)}</div>`;
    elsDrawer.chatInput.value = '';
    chipsDiv.innerHTML = ''; // Clear chips while loading
    elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    
    // Add loading indicator
    const loadingId = 'loading-' + Date.now();
    stream.innerHTML += `<div class="chat-bubble chat-ai" id="${loadingId}"><i class="fa-solid fa-circle-notch fa-spin"></i> Getting the vibe...</div>`;
    elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    
    const actData = _drawerContext.data;
    const payload = {
      place_name: actData.title,
      destination: pickPlan(state.data).destination || '',
      query: query,
      description: actData.desc,
      category: actData.category,
      chat_history: _drawerContext.history
    };
    
    // Append to history for next time
    _drawerContext.history.push({ role: 'user', content: query });
    
    try {
      const res = await WayfarerAPI.chatPlace(payload);
      
      // Update vibe tags in header if provided
      if (res.vibe_tags && res.vibe_tags.length) {
        const badgesHtml = res.vibe_tags.map(t => `<span class="vibe-badge">${esc(t)}</span>`).join('');
        const badgeContainer = elsDrawer.body.querySelector('.vibe-badges');
        if (badgeContainer) badgeContainer.innerHTML = badgesHtml;
      }
      
      // Replace loading bubble with AI response
      const loader = document.getElementById(loadingId);
      if (loader) loader.outerHTML = `<div class="chat-bubble chat-ai">${esc(res.reply)}</div>`;
      
      _drawerContext.history.push({ role: 'assistant', content: res.reply });
      
      // Update quick chips
      if (res.suggested_followups && res.suggested_followups.length) {
        chipsDiv.innerHTML = res.suggested_followups.map(q => `<button class="chat-chip" data-query="${esc(q)}">${esc(q)}</button>`).join('');
      }
      
      elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    } catch (err) {
      console.error("Chat error", err);
      const loader = document.getElementById(loadingId);
      if (loader) loader.outerHTML = `<div class="chat-bubble chat-ai" style="color:var(--bad);">Failed to fetch vibe. Please try again.</div>`;
    }
  }

  if (elsDrawer.chatSendBtn) {
    elsDrawer.chatSendBtn.addEventListener('click', () => sendChatMessage(elsDrawer.chatInput.value.trim()));
  }
  if (elsDrawer.chatInput) {
    elsDrawer.chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); sendChatMessage(elsDrawer.chatInput.value.trim()); }
    });
  }
  // Delegate clicks for chat chips
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('chat-chip')) {
      sendChatMessage(e.target.dataset.query);
    }
  });

  // Open Replace Drawer
  function openReplaceDrawer(actData) {
    _drawerContext = { type: 'replace', data: actData };
    
    elsDrawer.img.src = actData.imgSrc;
    elsDrawer.title.textContent = `Replacing: ${actData.title}`;
    elsDrawer.loc.textContent = `Day ${actData.dayNum} · ${actData.time}`;
    
    elsDrawer.chatFooter.hidden = true;
    
    elsDrawer.body.innerHTML = `
      <div class="replace-search-box">
        <button type="button" class="replace-preprompt" id="btn-preprompt">
          <i class="fa-solid fa-wand-magic-sparkles"></i> Suggest me some alternatives
        </button>
        <div class="replace-or">or</div>
        <div class="chat-input-area" style="padding:0; border:none; border-radius:999px;">
          <input type="text" id="replace-input" placeholder="e.g. cozy riverside cafe..." autocomplete="off" />
          <button type="button" class="chat-send-btn" id="replace-search-btn"><i class="fa-solid fa-magnifying-glass"></i></button>
        </div>
      </div>
      <div id="replace-results" style="display:flex; flex-direction:column; gap:16px;"></div>
    `;
    
    elsDrawer.overlay.classList.add('open');
    elsDrawer.drawer.classList.add('open');
    
    document.getElementById('btn-preprompt').addEventListener('click', () => doReplaceSearch(''));
    document.getElementById('replace-search-btn').addEventListener('click', () => {
      const val = document.getElementById('replace-input').value.trim();
      if (val) doReplaceSearch(val);
    });
    document.getElementById('replace-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const val = e.target.value.trim();
        if (val) doReplaceSearch(val);
      }
    });
  }

  async function doReplaceSearch(pref) {
    const resDiv = document.getElementById('replace-results');
    resDiv.innerHTML = `<div style="text-align:center; padding: 40px; color: var(--brand-ink);"><i class="fa-solid fa-circle-notch fa-spin fa-2x"></i><p style="margin-top:10px;font-weight:600;">Finding unique alternatives...</p></div>`;
    
    const actData = _drawerContext.data;
    const payload = {
      current_place: actData.title,
      destination: pickPlan(state.data).destination || '',
      time_slot: actData.time || 'Daytime',
      day_number: actData.dayNum,
      user_preference: pref,
      existing_places: getExistingPlaces()
    };
    
    try {
      const res = await WayfarerAPI.replacePlace(payload);
      
      if (!res.alternatives || !res.alternatives.length) {
        resDiv.innerHTML = `<p style="text-align:center; color:var(--bad);">No alternatives found. Try a different request.</p>`;
        return;
      }
      
      // Store globally for the swap button to access
      window._replaceAlts = res.alternatives;
      
      let html = '';
      res.alternatives.forEach((alt, i) => {
        html += `
          <div class="alt-card">
            <div class="alt-header">
              <div>
                <h4 class="alt-title">${esc(alt.title)}</h4>
                <div class="alt-loc">${esc(alt.location_name)}</div>
              </div>
              <div class="alt-cost">${money(alt.estimated_cost)}</div>
            </div>
            <p class="alt-desc">${esc(alt.description)}</p>
            <button type="button" class="btn-swap" data-alt-idx="${i}">Swap This In</button>
          </div>
        `;
      });
      resDiv.innerHTML = html;
      
    } catch (err) {
      console.error(err);
      resDiv.innerHTML = `<p style="text-align:center; color:var(--bad);">Failed to fetch alternatives. Ensure API is running.</p>`;
    }
  }

  // Handle Swap Click
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('btn-swap')) {
      const idx = parseInt(e.target.dataset.altIdx, 10);
      const alt = window._replaceAlts[idx];
      if (!alt || !_drawerContext || _drawerContext.type !== 'replace') return;
      
      const actData = _drawerContext.data;
      const finalPlan = pickPlan(state.data);
      const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == actData.dayNum);
      
      // Perform the swap
      const newAct = {
        title: alt.title,
        location_name: alt.location_name,
        time: alt.time || actData.time,
        description: alt.description,
        cost: alt.estimated_cost,
        category: alt.category
      };
      day.activities[actData.idx] = newAct;
      
      closeDrawer();
      toast(`Swapped in ${esc(alt.title)}!`, 'ok');
      loadFinal(false); // Re-render itinerary and map
    }
  });


})();
        },
        () => { state.sseHandle = null; }, // onDone
      );

      const msg = {
        approve: 'Finalizing — streaming live progress.',
        modify: 'Planner is revising your draft.',
        reject: 'Research restarted from scratch.',
      }[action] || `Review submitted: ${action}.`;
      toast(msg, action === 'reject' ? 'warn' : 'ok');
      if (action === 'reject') {
        els.itinerary.innerHTML = '';
        if (window.WayfarerMaps) window.WayfarerMaps.destroyAll();
      }
    } catch (err) {
      toast(err.message || 'Review failed.', 'bad');
      showReviewGate(true);
      setPipeline('awaiting_review');
      showWorking('awaiting_review', 'Draft ready — pick optional flight/hotel, then approve.', true);
    }
  }

  // ─── Final plan ─────────────────────────────────────────
  async function loadFinal(isRestore = false) {
    setPipeline('completed');
    stopProgressAnim();
    if (els.progressFill) els.progressFill.style.width = '100%';
    els.workingBanner.style.display = 'none';
    try {
      const data = await WayfarerAPI.getFinal(state.planId);
      renderFinal(data);
      saveSession({ preferences: data.preferences, destination: data.final_plan?.destination });
      if (!isRestore) toast('Your itinerary is finalized! 🎉', 'ok');
    } catch (err) {
      // final may not be ready instantly; retry once
      setTimeout(async () => {
        try {
          const data = await WayfarerAPI.getFinal(state.planId);
          renderFinal(data);
          saveSession({ preferences: data.preferences, destination: data.final_plan?.destination });
          if (!isRestore) toast('Your itinerary is finalized! 🎉', 'ok');
        }
        catch (e2) { toast('Plan completed, but the final document is not ready yet.', 'warn'); }
      }, 1500);
    }
  }

  // ─── Rendering (defensive: handles several shapes) ──────
  function renderDraft(data) {
    const plan = pickPlan(data);
    const dest = plan.destination || els.wsDestination.textContent || (data.preferences && data.preferences.destination) || '';
    els.itinerary.innerHTML = renderPlanHTML(plan, false, data);
    const mapData = plan.route_map || data.route_map || (data.draft_itinerary && data.draft_itinerary.route_map);
    if (window.WayfarerMaps && mapData && mapData.available) {
      window.WayfarerMaps.destroyAll();
      window.WayfarerMaps.bindRouteSection(mapData);
    }
    bindTravelTabs();
    bindItineraryDayFilter();
    hydrateActivityImages(dest);
  }
  function renderFinal(data) {
    const plan = pickPlan(data);
    const dest = plan.destination || els.wsDestination.textContent || '';
    els.draftArea.hidden = false;
    els.reviewGate.hidden = true;
    els.itinerary.innerHTML =
      `<div class="completed-banner"><i class="fa-solid fa-circle-check"></i> Approved &amp; finalized — bon voyage!</div>` +
      renderPlanHTML(plan, true, data);
    if (window.WayfarerMaps && plan.route_map) {
      window.WayfarerMaps.destroyAll();
      window.WayfarerMaps.bindRouteSection(plan.route_map);
    }
    bindTravelTabs();
    bindItineraryDayFilter();
    hydrateActivityImages(dest);
    els.itinerary.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // Find the itinerary payload regardless of nesting.
  function pickPlan(data) {
    if (!data) return {};
    return data.final_plan || data.draft_itinerary || data.plan || data.itinerary || data.draft || data.result || data;
  }

  function renderPlanHTML(plan, isFinal, fullData = {}) {
    const mainParts = [];
    const sideParts = [];

    // Summary
    const summary = plan.summary || plan.overview || plan.description;
    const dest = plan.destination || (els.wsDestination.textContent);

    // Add Revision Info
    let revisionHtml = '';
    if (!isFinal && fullData.revision_count > 0) {
      revisionHtml = `<div class="revision-info">
        <span class="rev-badge"><i class="fa-solid fa-code-branch"></i> Revision ${fullData.revision_count}</span>
        ${fullData.revision_notes && fullData.revision_notes.length ? `
          <div class="rev-notes">
            <strong>Previous feedback:</strong>
            <ul>${fullData.revision_notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>
          </div>
        ` : ''}
      </div>`;
    }

    const vision = plan.custom_notes || fullData.custom_notes || (fullData.preferences && fullData.preferences.custom_notes);
    let visionHtml = '';
    if (vision && typeof vision === 'string' && vision.trim()) {
      visionHtml = `
        <div class="custom-vision-card" style="margin-top: 14px; padding: 12px 16px; background: rgba(13, 148, 136, 0.08); border-left: 3.5px solid var(--brand); border-radius: 4px; font-size: 0.92rem; color: var(--ink);">
          <span style="font-weight: 600; color: var(--brand); display: inline-flex; align-items: center; gap: 6px;">
            <i class="fa-solid fa-wand-magic-sparkles"></i> Your Trip Vision:
          </span>
          <span style="font-style: italic; color: var(--ink-soft); margin-left: 6px;">"${esc(vision.trim())}"</span>
        </div>`;
    }

    mainParts.push(`
      <div class="summary-card">
        <h3><i class="fa-solid fa-map-location-dot"></i> ${esc(dest)}</h3>
        ${revisionHtml}
        ${summary ? `<p class="sum-sub">${esc(typeof summary === 'string' ? summary : JSON.stringify(summary))}</p>` : ''}
        ${visionHtml}
        <div class="sum-stats">${summaryStats(plan)}</div>
      </div>`);

    const activePlanId = fullData.plan_id || plan.plan_id || state.planId;
    if (isFinal && activePlanId) {
      mainParts.push(renderMcpExportBar(activePlanId, isFinal));
    }

    const corridor = plan.journey_corridor || fullData.journey_corridor;
    if (corridor) {
      mainParts.push(renderJourneyCorridor(corridor));
    }

    // Weather Data -> sent to dedicated side column widget!
    const weatherData = plan.weather || fullData.research?.weather || fullData.weather || fullData.draft_itinerary?.weather;
    if (weatherData && weatherData.available) {
      sideParts.push(renderSideWeatherStation(weatherData, dest));
    }

    if (isFinal && plan.selected_travel) {
      sideParts.push(renderSelectedTravel(plan.selected_travel, plan.budget?.currency || plan.currency || 'USD'));
    }

    if (isFinal && plan.travel_options?.recommendations?.has_picks && !plan.selected_travel) {
      sideParts.push(renderTravelRecommendations(plan));
    }

    if (isFinal && plan.travel_options && (plan.travel_options.flights?.available || plan.travel_options.hotels?.available)) {
      sideParts.push(renderTravelOptions(plan, true));
    }

    const mapData = plan.route_map || fullData.route_map || (fullData.draft_itinerary && fullData.draft_itinerary.route_map);
    if (mapData && mapData.available && window.WayfarerMaps) {
      mainParts.push(window.WayfarerMaps.renderRouteSection(mapData));
    }

    const routeByDay = {};
    if (plan.route_map?.days) {
      plan.route_map.days.forEach((d) => { routeByDay[d.day_number] = d; });
    }

    // Days / itinerary
    const days = findDays(plan);
    if (days.length) {
      if (days.length > 1) {
        mainParts.push(`
          <div class="itinerary-days-header" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; margin:26px 0 16px;">
            <h3 style="margin:0; font-size:1.2rem; display:flex; align-items:center; gap:8px;">
              <i class="fa-solid fa-calendar-days" style="color:var(--brand);"></i> Day-by-Day Itinerary
            </h3>
            <div class="itinerary-day-filter-pills" id="itinerary-day-tabs" style="display:flex; gap:6px; flex-wrap:wrap;">
              <button class="itin-pill-btn active" data-itin-day="1" style="border:1.5px solid var(--brand); background:var(--brand); color:#fff; border-radius:999px; padding:6px 14px; font-size:0.82rem; font-weight:700; cursor:pointer;">Day 1</button>
              ${days.slice(1).map((d, idx) => `<button class="itin-pill-btn" data-itin-day="${d.day_number || (idx + 2)}" style="border:1.5px solid var(--line); background:#fff; color:var(--ink-soft); border-radius:999px; padding:6px 14px; font-size:0.82rem; font-weight:700; cursor:pointer;">Day ${d.day_number || (idx + 2)}</button>`).join('')}
              <button class="itin-pill-btn" data-itin-day="all" style="border:1.5px solid var(--line); background:#fff; color:var(--ink-soft); border-radius:999px; padding:6px 14px; font-size:0.82rem; font-weight:700; cursor:pointer;"><i class="fa-solid fa-layer-group"></i> All Days</button>
            </div>
          </div>
        `);
      }
      mainParts.push('<div id="itinerary-days-container">');
      days.forEach((d, i) => {
        const num = d.day ?? d.day_number ?? d.index ?? (i + 1);
        mainParts.push(renderDay(d, i, dest, !isFinal, routeByDay[num]));
      });
      mainParts.push('</div>');
    } else {
      // fallback: dump notes / research / raw json
      const notes = plan.notes || plan.research || plan.context || plan.findings;
      if (notes) mainParts.push(renderNote('Notes & research', notes));
      else mainParts.push(renderNote('Plan data', plan));
    }

    const budget = plan.budget || plan.budget_breakdown || plan.budget_allocation || plan.allocation;
    if (budget && typeof budget === 'object') mainParts.push(renderBudget(budget, plan));
    
    // Explicitly show planner notes and tips
    if (plan.planner_notes && plan.planner_notes.length) {
      mainParts.push(renderNote('Planner Notes', plan.planner_notes));
    }
    if (plan.packing_list && plan.packing_list.length) {
      mainParts.push(renderNote('Recommended Packing List', plan.packing_list));
    }
    const advisories = fullData.travel_advisories || plan.travel_advisories;
    if (advisories) {
      mainParts.push(renderMcpAdvisories(advisories));
    }
    if (plan.tips && plan.tips.length) {
      mainParts.push(renderNote('Travel Tips', plan.tips));
    }

    // Explicit Research Section
    if (fullData.research) {
        mainParts.push(renderResearch(fullData.research));
    }

    return `
      <div class="plan-dashboard-grid">
        <div class="plan-main-column">
          ${mainParts.join('')}
        </div>
        <aside class="plan-side-column">
          ${sideParts.join('')}
        </aside>
      </div>
    `;
  }

  function summaryStats(plan) {
    const stats = [];
    // budget object now uses chosen currency directly instead of just USD
    const budgetData = plan.budget || plan.budget_breakdown || plan.budget_allocation || plan.allocation;
    const tot = plan.total_cost ?? plan.budget_total ?? (budgetData && (budgetData.total ?? budgetData.total_budget ?? budgetData.total_usd));
    
    // In our payload we now have fullData via the parent function, but the currency is often known from the plan structure. 
    // We'll extract it if it's there.
    const currency = plan.currency || (budgetData && budgetData.currency) || 'USD';
    
    if (tot != null) stats.push(['Budget', money(tot, currency)]);
    const days = findDays(plan);
    if (days.length) stats.push(['Days', days.length]);
    if (plan.travelers != null) stats.push(['Travelers', plan.travelers]);
    if (plan.route_map?.available && plan.route_map.totals) {
      stats.push(['Total travel', `${plan.route_map.totals.distance_km} km`]);
      if (window.WayfarerMaps) {
        stats.push(['On the road', window.WayfarerMaps.formatDuration(plan.route_map.totals.duration_minutes)]);
      }
    }
    if (plan.dates?.start && plan.dates?.end) {
      stats.push(['Dates', `${plan.dates.start} → ${plan.dates.end}`]);
    } else if (plan.start_date && plan.end_date) {
      stats.push(['Dates', `${plan.start_date} → ${plan.end_date}`]);
    }
    if (!stats.length) stats.push(['Status', 'Draft ready']);
    return stats.map(([k, v]) => `<div class="sum-stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join('');
  }

  function renderBudget(budget, plan) {
    let rows = [];
    const currency = plan.currency || budget.currency || 'USD';
    
    if (Array.isArray(budget.items)) {
      rows = budget.items.map((it) => [it.category || it.name || it.label, Number(it.amount ?? it.value ?? it.cost ?? 0)]);
    } else if (budget.categories && typeof budget.categories === 'object') {
      rows = Object.entries(budget.categories).map(([k, v]) => [k.replace(/_/g, ' '), Number(v)]);
    } else {
      rows = Object.entries(budget)
        .filter(([k, v]) => typeof v === 'number' && !['total', 'budget_usd', 'budget_total', 'total_budget', 'per_traveler', 'lodging_per_night', 'daily_food'].includes(k))
        .map(([k, v]) => [k.replace(/_/g, ' '), v]);
    }
    if (!rows.length) return '';
    const total = rows.reduce((s, [, v]) => s + v, 0) || 1;
    const body = rows.map(([label, amt]) => {
      const pct = Math.max(2, Math.round((amt / total) * 100));
      return `<div class="budget-row">
        <span class="b-label">${esc(label)}</span>
        <span class="b-bar"><span style="width:${pct}%"></span></span>
        <span class="b-amt">${money(amt, currency)}</span>
      </div>`;
    }).join('');
    return `<div class="budget-card"><h3><i class="fa-solid fa-coins"></i> Budget breakdown</h3>${body}</div>`;
  }

  // Locate the array of day objects in many possible shapes.
  function findDays(plan) {
    const candidates = [plan.days, plan.itinerary, plan.schedule, plan.daily, plan.day_schedule];
    for (const c of candidates) if (Array.isArray(c) && c.length) return c;
    if (plan.itinerary && Array.isArray(plan.itinerary.days)) return plan.itinerary.days;
    return [];
  }

  function renderSideWeatherStation(weather, destination) {
    if (!weather || !weather.available || !Array.isArray(weather.daily) || !weather.daily.length) {
      return '';
    }
    const destName = typeof destination === 'string' ? destination.split(',')[0].trim() : 'Destination';
    const days = weather.daily;
    const heroDay = days[0];

    const themeStyles = {
      'sunny': { bg: 'linear-gradient(135deg, #d97706 0%, #b45309 100%)', icon: 'sun', label: 'Sunny & Clear' },
      'partly-cloudy': { bg: 'linear-gradient(135deg, #0d9488 0%, #0f766e 100%)', icon: 'cloud-sun', label: 'Partly Cloudy' },
      'cloudy': { bg: 'linear-gradient(135deg, #475569 0%, #334155 100%)', icon: 'cloud', label: 'Overcast' },
      'rainy': { bg: 'linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%)', icon: 'cloud-showers-heavy', label: 'Rain Expected' },
      'stormy': { bg: 'linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%)', icon: 'bolt', label: 'Thunderstorms' },
      'snowy': { bg: 'linear-gradient(135deg, #0284c7 0%, #0369a1 100%)', icon: 'snowflake', label: 'Snow Conditions' },
      'fair': { bg: 'linear-gradient(135deg, #0d9488 0%, #115e59 100%)', icon: 'cloud-sun', label: 'Mild & Pleasant' },
    };

    const theme = heroDay.theme || (heroDay.rain_mm >= 3 ? 'rainy' : 'sunny');
    const heroStyle = themeStyles[theme] || themeStyles['fair'];
    const heroIcon = heroDay.icon || heroStyle.icon;

    const tMaxC = heroDay.t_max_c != null ? `${Math.round(heroDay.t_max_c)}°C` : '—';
    const tMinC = heroDay.t_min_c != null ? `${Math.round(heroDay.t_min_c)}°C` : '—';
    const tMaxF = heroDay.t_max_f != null ? `${Math.round(heroDay.t_max_f)}°F` : '';
    const tMinF = heroDay.t_min_f != null ? `${Math.round(heroDay.t_min_f)}°F` : '';

    const uv = heroDay.uv_index != null ? heroDay.uv_index : 5.8;
    const uvText = uv >= 8 ? 'Very High (SPF 50+)' : (uv >= 6 ? 'High (Sunscreen)' : (uv >= 3 ? 'Moderate' : 'Low'));
    const rainProb = heroDay.rain_prob_pct != null ? `${heroDay.rain_prob_pct}%` : (heroDay.rain_mm ? '60%' : '5%');
    const windSpeed = heroDay.wind_kmh != null ? `${Math.round(heroDay.wind_kmh)} km/h` : '8.6 km/h';
    const sunrise = heroDay.sunrise || '06:10';
    const sunset = heroDay.sunset || '18:01';

    const miniRowsHtml = days.map((d, idx) => {
      const dDate = new Date(d.date + 'T00:00:00');
      const dayLabel = !isNaN(dDate) ? dDate.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }) : `Day ${idx + 1}`;
      const dIcon = d.icon || 'sun';
      const dMax = d.t_max_c != null ? `${Math.round(d.t_max_c)}°` : '—';
      const dMin = d.t_min_c != null ? `${Math.round(d.t_min_c)}°` : '—';
      const dRain = d.rain_mm ? `${d.rain_mm}mm` : 'Dry';

      return `
        <div class="weather-mini-row">
          <span style="font-weight:700; color:var(--ink);">${esc(dayLabel)}</span>
          <span style="color:var(--brand); display:inline-flex; align-items:center; gap:5px;"><i class="fa-solid fa-${esc(dIcon)}"></i></span>
          <span style="font-weight:700; color:var(--ink);">${dMax} <span style="font-weight:500; color:var(--muted); font-size:0.75rem;">/ ${dMin}</span></span>
          <span style="font-size:0.75rem; color:var(--ink-soft);">${esc(dRain)}</span>
        </div>
      `;
    }).join('');

    return `
      <div class="weather-side-widget">
        <div class="weather-side-head">
          <h4><i class="fa-solid fa-cloud-sun" style="color:var(--brand);"></i> Weather &amp; Climate</h4>
          <span style="font-size:0.72rem; font-weight:700; color:var(--brand); background:#eef6f5; padding:3px 9px; border-radius:999px;">
            Open-Meteo
          </span>
        </div>

        <div class="weather-side-hero" style="background:${heroStyle.bg};">
          <div class="weather-hero-temps">
            <span style="font-size:0.70rem; text-transform:uppercase; font-weight:800; letter-spacing:0.04em; opacity:0.88;">Forecast · ${esc(destName)}</span>
            <div class="weather-hero-big">${tMaxC}</div>
            <div class="weather-hero-sub">Low: ${tMinC} ${tMaxF ? `· (${tMaxF} / ${tMinF})` : ''}</div>
            <div style="font-size:0.80rem; font-weight:700; margin-top:3px;">${esc(heroDay.condition || heroStyle.label)}</div>
          </div>
          <div class="weather-hero-icon">
            <i class="fa-solid fa-${esc(heroIcon)}"></i>
          </div>
        </div>

        <div class="weather-metric-grid">
          <div class="weather-metric-item">
            <span class="metric-k"><i class="fa-solid fa-sun" style="color:#f59e0b;"></i> UV Index</span>
            <span class="metric-v">${uv} <small style="font-size:0.68rem; font-weight:600; color:var(--muted);">${esc(uvText)}</small></span>
          </div>
          <div class="weather-metric-item">
            <span class="metric-k"><i class="fa-solid fa-droplet" style="color:#3b82f6;"></i> Rain Risk</span>
            <span class="metric-v">${esc(rainProb)} <small style="font-size:0.68rem; font-weight:600; color:var(--muted);">${heroDay.rain_mm || 0}mm</small></span>
          </div>
          <div class="weather-metric-item">
            <span class="metric-k"><i class="fa-solid fa-wind" style="color:#14b8a6;"></i> Wind</span>
            <span class="metric-v">${esc(windSpeed)}</span>
          </div>
          <div class="weather-metric-item">
            <span class="metric-k"><i class="fa-solid fa-mountain-sun" style="color:#ea580c;"></i> Daylight</span>
            <span class="metric-v" style="font-size:0.78rem;">🌅 ${esc(sunrise)} · 🌇 ${esc(sunset)}</span>
          </div>
        </div>

        <div style="background:#f0fdfa; border:1px solid #99f6e4; border-radius:8px; padding:10px 12px; font-size:0.80rem; color:#0f766e; line-height:1.45;">
          <strong><i class="fa-solid fa-person-walking"></i> Outdoor Window:</strong> Optimal sightseeing hours are <strong>07:30–11:00 AM</strong> &amp; <strong>04:30–07:00 PM</strong> (avoiding peak noon heat).
        </div>

        <div>
          <span style="font-size:0.72rem; text-transform:uppercase; font-weight:800; color:var(--muted); letter-spacing:0.04em;">Trip Days Outlook</span>
          <div class="weather-mini-timeline" style="margin-top:6px;">
            ${miniRowsHtml}
          </div>
        </div>

        <div style="background:#f8fafc; border:1px solid var(--line); border-radius:8px; padding:12px; font-size:0.78rem; color:var(--ink-soft); line-height:1.5;">
          <strong style="color:var(--ink); display:block; margin-bottom:4px;"><i class="fa-solid fa-suitcase"></i> Climate Checklist:</strong>
          <span>• Sunglasses &amp; UV protection</span><br/>
          <span>• Breathable daytime cotton layers</span><br/>
          <span>• Sturdy footwear for walking/bridges</span><br/>
          <span>• Light wrap or scarf for evening breeze</span>
        </div>
      </div>
    `;
  }

  function renderWeatherReport(weather, destination) {
    if (!weather || !weather.available || !Array.isArray(weather.daily) || !weather.daily.length) {
      return '';
    }
    const destName = typeof destination === 'string' ? destination.split(',')[0].trim() : 'Destination';
    const days = weather.daily;

    const themeStyles = {
      'sunny': { bg: 'linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%)', border: '#fde68a', color: '#b45309', iconColor: '#f59e0b' },
      'partly-cloudy': { bg: 'linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%)', border: '#bbf7d0', color: '#15803d', iconColor: '#22c55e' },
      'cloudy': { bg: 'linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%)', border: '#cbd5e1', color: '#475569', iconColor: '#64748b' },
      'rainy': { bg: 'linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%)', border: '#bfdbfe', color: '#1d4ed8', iconColor: '#3b82f6' },
      'stormy': { bg: 'linear-gradient(135deg, #f5f3ff 0%, #ede9fe 100%)', border: '#ddd6fe', color: '#6d28d9', iconColor: '#8b5cf6' },
      'snowy': { bg: 'linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%)', border: '#bae6fd', color: '#0369a1', iconColor: '#0ea5e9' },
      'fair': { bg: 'linear-gradient(135deg, #f0fdfa 0%, #ccfbf1 100%)', border: '#99f6e4', color: '#0f766e', iconColor: '#14b8a6' },
    };

    const cardsHtml = days.map((d) => {
      const dDate = new Date(d.date + 'T00:00:00');
      const dayName = !isNaN(dDate) ? dDate.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }) : d.date;
      const theme = d.theme || (d.rain_mm >= 3 ? 'rainy' : 'sunny');
      const style = themeStyles[theme] || themeStyles['fair'];
      const icon = d.icon || (theme === 'rainy' ? 'cloud-showers-heavy' : 'sun');
      const cond = d.condition || (d.rain_mm >= 3 ? 'Rain Expected' : 'Dry & Clear');
      const tip = d.activity_tip || d.summary || '';
      
      const tMaxC = d.t_max_c != null ? `${Math.round(d.t_max_c)}°C` : '—';
      const tMinC = d.t_min_c != null ? ` / ${Math.round(d.t_min_c)}°C` : '';
      const tMaxF = d.t_max_f != null ? `(${Math.round(d.t_max_f)}°F)` : '';

      return `
        <div class="weather-day-card" style="background:${style.bg}; border:1.5px solid ${style.border}; border-radius:var(--radius-sm); padding:16px; display:flex; flex-direction:column; gap:8px;">
          <div style="display:flex; align-items:center; justify-content:space-between;">
            <span style="font-weight:700; font-size:0.95rem; color:var(--ink);">${esc(dayName)}</span>
            <div style="width:38px; height:38px; border-radius:50%; background:#fff; display:grid; place-items:center; box-shadow:var(--shadow-sm); color:${style.iconColor}; font-size:1.15rem;">
              <i class="fa-solid fa-${esc(icon)}"></i>
            </div>
          </div>
          <div style="font-size:1.1rem; font-weight:800; color:${style.color}; display:flex; align-items:baseline; gap:6px;">
            <span>${tMaxC}</span>
            <span style="font-size:0.85rem; font-weight:600; color:var(--ink-soft);">${tMinC}</span>
            ${tMaxF ? `<span style="font-size:0.75rem; color:var(--muted); font-weight:500; margin-left:auto;">${tMaxF}</span>` : ''}
          </div>
          <div style="font-size:0.85rem; font-weight:600; color:var(--ink); display:flex; align-items:center; gap:6px;">
            <i class="fa-solid fa-droplet" style="color:#60a5fa; font-size:0.80rem;"></i>
            <span>${d.rain_mm ? `${d.rain_mm} mm rain` : '0 mm · Dry'}</span>
            <span style="margin-left:auto; font-size:0.78rem; font-weight:700; color:${style.color}; background:rgba(255,255,255,0.7); padding:2px 8px; border-radius:999px;">${esc(cond)}</span>
          </div>
          <div style="font-size:0.82rem; color:var(--ink-soft); line-height:1.45; border-top:1px solid rgba(0,0,0,0.06); padding-top:8px; margin-top:2px;">
            ${esc(tip)}
          </div>
        </div>
      `;
    }).join('');

    return `
      <div class="weather-report-card" style="margin-bottom:24px; padding:22px; background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow-sm);">
        <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; margin-bottom:16px;">
          <div>
            <h3 style="margin:0 0 4px; font-size:1.15rem; display:flex; align-items:center; gap:8px;">
              <i class="fa-solid fa-cloud-sun" style="color:var(--brand);"></i> Weather Report &amp; Climate Guide
            </h3>
            <p style="margin:0; font-size:0.86rem; color:var(--ink-soft);">
              Daily forecast for <strong>${esc(destName)}</strong> · Tailored for outdoor sightseeing vs. indoor pacing.
            </p>
          </div>
          <span style="font-size:0.80rem; font-weight:700; background:#eef6f5; color:var(--brand); padding:5px 12px; border-radius:999px;">
            <i class="fa-solid fa-satellite-dish"></i> Open-Meteo Verified
          </span>
        </div>
        <div class="weather-grid" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:14px;">
          ${cardsHtml}
        </div>
      </div>
    `;
  }

  function extractLandmarkKeyword(text) {
    if (!text) return '';
    const atMatch = text.match(/(?:at|along|near|around|across|in|of)\s+([A-Z][a-zA-Z0-9\s]{2,30})/);
    if (atMatch && atMatch[1]) {
      return atMatch[1].replace(/\s+(to|and|with|for|the|its|our).*$/i, '').trim();
    }
    let cleaned = text.replace(/^(visit|explore|enjoy|see|attend|cross|experience|discover|head to|walk to|stroll along|savor|sample|taste|take a|ride the|walk across|tour the)\s+/i, '');
    cleaned = cleaned.replace(/\s+(pedestrian|suspension|bridge|ceremony|prayer|temple|ashram|market|steps|along|to explore|of|for|at).*$/i, '');
    const words = cleaned.trim().split(/\s+/);
    if (words.length > 4) {
      return words.slice(0, 4).join(' ');
    }
    return cleaned.trim() || text.split(/\s+/).slice(0, 3).join(' ');
  }

  function bindItineraryDayFilter() {
    const tabsContainer = document.getElementById('itinerary-day-tabs');
    if (!tabsContainer) return;
    tabsContainer.querySelectorAll('.itin-pill-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        tabsContainer.querySelectorAll('.itin-pill-btn').forEach((b) => {
          b.classList.remove('active');
          b.style.background = '#fff';
          b.style.color = 'var(--ink-soft)';
          b.style.borderColor = 'var(--line)';
        });
        btn.classList.add('active');
        btn.style.background = 'var(--brand)';
        btn.style.color = '#fff';
        btn.style.borderColor = 'var(--brand)';

        const dayVal = btn.dataset.itinDay;
        const allDays = dayVal === 'all';
        const targetDay = parseInt(dayVal, 10);

        document.querySelectorAll('#itinerary-days-container .day-card').forEach((card) => {
          const cardDay = parseInt(card.dataset.dayNum, 10);
          if (allDays || cardDay === targetDay) {
            card.style.display = 'block';
          } else {
            card.style.display = 'none';
          }
        });

        // Also synchronize with the Map day filter!
        const mapDayBtn = document.querySelector(`.day-pill-btn[data-day="${dayVal}"]`);
        if (mapDayBtn && !mapDayBtn.classList.contains('active')) {
          mapDayBtn.click();
        }
      });
    });

    // Default to Day 1 filter!
    const defaultDay1Btn = tabsContainer.querySelector('.itin-pill-btn[data-itin-day="1"]');
    if (defaultDay1Btn) {
      document.querySelectorAll('#itinerary-days-container .day-card').forEach((card) => {
        const cardDay = parseInt(card.dataset.dayNum, 10);
        if (cardDay === 1) {
          card.style.display = 'block';
        } else {
          card.style.display = 'none';
        }
      });
    }
  }

  function renderDay(day, i, destination, isDraft, dayRoute) {
    const num = day.day ?? day.day_number ?? day.index ?? (i + 1);
    const title = day.title || day.theme || day.name || `Day ${num}`;
    const date = day.date || day.day_date || '';
    let weather = day.weather || day.forecast;
    const acts = day.activities || day.items || day.schedule || day.plan || [];

    const wIcon = (weather && typeof weather === 'object' && weather.icon) ? weather.icon : 'cloud-sun';
    const weatherHTML = weather ? `<span class="day-weather"><i class="fa-solid fa-${wIcon}"></i> ${esc(weatherText(weather))}</span>` : '';
    const actsHTML = Array.isArray(acts) && acts.length
      ? acts.map((a, actIdx) => renderActivity(a, destination, isDraft, num, actIdx)).join('')
      : (typeof day.description === 'string' && !isDraft ? `<p class="a-desc" style="padding:12px 0">${esc(day.description)}</p>` : '<p class="a-desc" style="padding:12px 0">No activities listed.</p>');

    // If day has a description, show it above the activities
    const dayDesc = day.description && Array.isArray(acts) && acts.length && !isDraft ? `<p class="d-desc" style="color:var(--ink-soft); font-size: 0.95rem; margin-bottom: 12px;">${esc(day.description)}</p>` : '';

    const dayRouteMeta = !isDraft && dayRoute;
    const routeBadge = dayRouteMeta && dayRouteMeta.distance_km > 0
      ? `<span class="day-route"><i class="fa-solid fa-route"></i> ${dayRouteMeta.distance_km} km · ${window.WayfarerMaps ? window.WayfarerMaps.formatDuration(dayRouteMeta.duration_minutes) : dayRouteMeta.duration_minutes + ' min'} total travel</span>`
      : '';

    return `<div class="day-card" data-day-num="${num}">
      <div class="day-head">
        <span class="d-title"><span class="day-num">${esc(num)}</span><span><h4>${esc(title)}</h4>${date ? `<span class="d-date">${esc(date)}</span>` : ''}</span></span>
        <span class="day-head-meta">${routeBadge}${weatherHTML}</span>
      </div>
      <div class="day-body">
        ${dayDesc}
        ${actsHTML}
      </div>
    </div>`;
  }

  function activityImageKey(title, location) {
    return `${(location || '').trim().toLowerCase()}|${(title || '').trim().toLowerCase()}`;
  }

  function injectActivityImage(activityEl, url, alt) {
    const existingImg = activityEl.querySelector('.a-image img');
    if (existingImg && url) {
      activityEl.dataset.imageLoaded = '1';
      existingImg.src = url;
      if (alt) existingImg.alt = alt;
      return;
    }
    const body = activityEl.querySelector('.a-body');
    if (!body || body.querySelector('.a-image')) return;
    activityEl.dataset.imageLoaded = '1';
    const wrap = document.createElement('div');
    wrap.className = 'a-image';
    const img = document.createElement('img');
    img.src = url;
    img.alt = alt || '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.classList.add('a-image-reveal');
    img.onerror = () => wrap.remove();
    wrap.appendChild(img);
    body.appendChild(wrap);
  }

  function hydrateActivityImages(destination) {
    const acts = els.itinerary.querySelectorAll('.activity[data-image-key]:not([data-image-loaded])');
    if (!acts.length || !destination) return;

    const resolved = state.imageResolved || (state.imageResolved = new Set());
    const inflight = state.imageInflight || (state.imageInflight = new Set());
    const cache = state.imageUrlCache || (state.imageUrlCache = {});

    acts.forEach((el) => {
      const key = el.dataset.imageKey;
      if (!key || resolved.has(key) || inflight.has(key)) return;

      const title = el.dataset.title || '';
      const location = el.dataset.location || title;

      if (cache[key]) {
        injectActivityImage(el, cache[key], title || location);
        resolved.add(key);
        return;
      }

      inflight.add(key);
      WayfarerAPI.lookupActivityImage({ destination, title, location_name: location })
        .then((res) => {
          resolved.add(key);
          if (res?.image_url) {
            cache[key] = res.image_url;
            injectActivityImage(el, res.image_url, title || location);
          }
        })
        .catch(() => resolved.add(key))
        .finally(() => inflight.delete(key));
    });
  }

  function renderActivity(a, destination, isDraft, dayNum, actIdx) {
    let title = '';
    let locationName = '';
    let desc = '';
    let time = '';
    let cost = null;
    let imageUrl = null;

    if (typeof a === 'string') {
      title = a.trim();
      locationName = extractLandmarkKeyword(title);
    } else if (typeof a === 'object' && a !== null) {
      time = a.time || a.start || a.when || a.period || '';
      title = (a.title || a.name || a.activity || a.label || '').trim();
      locationName = (a.location_name || a.location || extractLandmarkKeyword(title) || title).trim();
      desc = a.description || a.detail || a.notes || '';
      cost = a.cost ?? a.price ?? a.amount;
      imageUrl = a.image_url || null;
    }

    if (!title && !locationName) return '';

    const destName = typeof destination === 'string' ? destination.split(',')[0].trim() : '';
    const searchTarget = locationName || title;
    const searchUrl = `https://www.google.com/search?q=${encodeURIComponent(destName + ' ' + searchTarget)}`;
    
    // Always render clickable hyperlink for the activity title
    const titleHTML = `<a href="${searchUrl}" target="_blank" rel="noopener noreferrer" class="activity-title-link" title="Explore ${esc(searchTarget)} on Google">
      <span>${esc(title)}</span>
      <i class="fa-solid fa-arrow-up-right-from-square activity-link-icon"></i>
    </a>`;

    const imgKey = activityImageKey(title, searchTarget);
    const defaultPlaceholder = "https://images.unsplash.com/photo-1488646953014-85cb44e25828?auto=format&fit=crop&w=480&q=80";

    // Show image container for both draft and final views
    const imgHTML = `
      <div class="a-image" data-title="${esc(searchTarget)}">
        <img src="${esc(imageUrl || defaultPlaceholder)}" alt="${esc(title)}" loading="lazy" referrerpolicy="no-referrer" class="a-image-reveal" onerror="this.onerror=null; this.src='${defaultPlaceholder}';" />
      </div>
    `;

    const activityAttrs = ` data-image-key="${esc(imgKey)}" data-title="${esc(searchTarget)}" data-location="${esc(searchTarget)}"`;

    const cumulative = a.cumulative_distance_km;
    const travel = a.travel_from_prev || a.travel_to_next;
    let travelHTML = '';
    
    if (!isDraft) {
      if (cumulative === 0) {
        travelHTML = `<div class="a-travel"><strong>0 km</strong> — Start of day</div>`;
      } else if (cumulative > 0 && travel) {
        travelHTML = `
          <div class="a-travel">
            <strong>${cumulative} km</strong> 
            <span style="opacity: 0.6; margin: 0 6px;">|</span>
            <i class="fa-solid ${window.WayfarerMaps ? window.WayfarerMaps.modeIcon(travel.mode) : 'fa-person-walking'}"></i>
            +${travel.distance_km} km (${window.WayfarerMaps ? window.WayfarerMaps.formatDuration(travel.duration_minutes) : travel.duration_minutes + ' min'})
          </div>`;
      }
    }


    // Add hover actions for final itinerary (not draft)
    let hoverActions = '';
    if (!isDraft && dayNum !== undefined && actIdx !== undefined) {
      hoverActions = `
      <div class="activity-hover-actions">
        <button type="button" class="action-btn btn-chat" data-action="chat" data-day="${dayNum}" data-idx="${actIdx}" title="Talk to this place">
          <i class="fa-solid fa-comments"></i> Chat
        </button>
        <button type="button" class="action-btn btn-replace" data-action="replace" data-day="${dayNum}" data-idx="${actIdx}" title="Replace activity">
          <i class="fa-solid fa-arrows-rotate"></i> Replace
        </button>
        <button type="button" class="action-btn btn-remove" data-action="remove" data-day="${dayNum}" data-idx="${actIdx}" title="Remove activity">
          <i class="fa-solid fa-trash-can"></i> Remove
        </button>
      </div>`;
    }

    return `<div class="activity"${activityAttrs}>
      ${hoverActions}
      ${time ? `<span class="a-time">${esc(time)}</span>` : ''}
      <div class="a-body">
        <div class="a-text">
          <p class="a-title">${titleHTML}</p>
          ${desc ? `<p class="a-desc">${esc(desc)}</p>` : ''}
          ${travelHTML}
          ${cost != null ? `<span class="a-cost">${money(cost)}</span>` : ''}
        </div>
        ${imgHTML}
      </div>
    </div>`;
  }

  function weatherText(w) {
    if (!w) return '';
    if (typeof w === 'string') return w;
    const cond = w.condition || w.summary || w.description || '';
    const tMax = w.t_max_c != null ? `${Math.round(w.t_max_c)}°C` : (w.temp != null ? `${w.temp}°` : '');
    const tMin = w.t_min_c != null ? ` / ${Math.round(w.t_min_c)}°C` : '';
    const rain = w.rain_mm ? ` · ${w.rain_mm}mm rain` : '';
    return [cond, tMax + tMin].filter(Boolean).join(' · ') + rain;
  }

  function formatFlightDuration(minutes) {
    const m = Number(minutes) || 0;
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60);
    const r = m % 60;
    return r ? `${h}h ${r}m` : `${h}h`;
  }

  function formatOfferPrice(price, currency = 'USD') {
    if (price == null || price === '') return '—';
    if (typeof price === 'number') return money(price, currency);
    return String(price);
  }

  function renderJourneyCorridor(corridor) {
    if (!corridor || !corridor.departure_city || !corridor.arrival_city) return '';
    const dep = corridor.departure_city;
    const arr = corridor.arrival_city;
    const mode = corridor.recommended_transit_mode || 'Direct Transit';
    const time = corridor.travel_time_estimate || '';
    const transfer = corridor.arrival_transfer_guidance || '';
    const contrast = corridor.climate_and_cultural_contrast || '';
    const retTip = corridor.return_departure_tip || '';
    const facts = corridor.grounded_route_facts || [];

    return `
      <div class="journey-corridor-card" style="margin-bottom: 24px; padding: 20px 24px; background: linear-gradient(135deg, #042f2e 0%, #115e59 100%); color: #fff; border-radius: var(--radius); box-shadow: 0 4px 16px rgba(13, 148, 136, 0.18);">
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 12px;">
          <div style="display: flex; align-items: center; gap: 10px;">
            <span style="font-size: 1.15rem; font-weight: 700; letter-spacing: -0.01em;">
              <i class="fa-solid fa-plane-departure" style="color: #5eead4; margin-right: 6px;"></i>${esc(dep)}
            </span>
            <i class="fa-solid fa-arrow-right-long" style="color: #99f6e4; font-size: 1.1rem;"></i>
            <span style="font-size: 1.15rem; font-weight: 700; letter-spacing: -0.01em;">
              <i class="fa-solid fa-location-dot" style="color: #5eead4; margin-right: 6px;"></i>${esc(arr)}
            </span>
          </div>
          <span style="background: rgba(255,255,255,0.18); padding: 4px 12px; border-radius: 999px; font-size: 0.85rem; font-weight: 600; display: inline-flex; align-items: center; gap: 6px;">
            <i class="fa-solid fa-bolt"></i> ${esc(mode)} ${time ? '· ' + esc(time) : ''}
          </span>
        </div>
        
        ${transfer ? `
          <div style="margin-top: 10px; font-size: 0.92rem; line-height: 1.5; color: #ccfbf1;">
            <strong><i class="fa-solid fa-taxi" style="color: #5eead4;"></i> Arrival Transfer:</strong> ${esc(transfer)}
          </div>
        ` : ''}

        ${contrast ? `
          <div style="margin-top: 8px; font-size: 0.90rem; line-height: 1.45; color: #e6fffa; opacity: 0.95;">
            <strong><i class="fa-solid fa-compass" style="color: #5eead4;"></i> Route Context & Contrast:</strong> ${esc(contrast)}
          </div>
        ` : ''}

        ${retTip ? `
          <div style="margin-top: 6px; font-size: 0.88rem; line-height: 1.4; color: #99f6e4;">
            <strong><i class="fa-solid fa-clock-rotate-left"></i> Return Logistics:</strong> ${esc(retTip)}
          </div>
        ` : ''}

        ${facts && facts.length ? `
          <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.15); font-size: 0.82rem; color: #a7f3d0;">
            <i class="fa-solid fa-circle-info"></i> Grounded Route Facts: ${facts.map(f => esc(f)).join(' · ')}
          </div>
        ` : ''}
      </div>
    `;
  }

  function renderSelectedTravel(selected, currency) {
    if (!selected?.summary) return '';
    const flight = selected.flight;
    const hotel = selected.hotel;
    let detail = '';
    if (flight) {
      detail += `<p><i class="fa-solid fa-plane"></i> <strong>Flight:</strong> ${esc(selected.summary.split(';')[0] || '')}</p>`;
    }
    if (hotel) {
      detail += `<p><i class="fa-solid fa-hotel"></i> <strong>Hotel:</strong> ${esc(hotel.name || '')}${hotel.price ? ` · ${esc(formatOfferPrice(hotel.price, currency))}` : ''}</p>`;
    }
    const ap = selected.airports;
    const apHtml = ap ? `<p class="pick-meta"><strong>Airports:</strong> ${esc((ap.origin || []).map((a) => a.iata).join(', '))} → ${esc((ap.destination || []).map((a) => a.iata).join(', '))}</p>` : '';
    return `
      <div class="selected-travel-card">
        <h3><i class="fa-solid fa-suitcase-rolling"></i> Your travel choices</h3>
        ${detail}
        ${apHtml}
        <p class="pick-meta">${esc(selected.note || '')}</p>
      </div>`;
  }

  function renderTravelRecommendations(plan) {
    const rec = plan.travel_options?.recommendations;
    const currency = plan.budget?.currency || plan.currency || 'USD';
    if (!rec?.has_picks) return '';

    let html = `
      <div class="travel-picks-card">
        <h3><i class="fa-solid fa-wand-magic-sparkles"></i> Our picks for you</h3>
        <p class="travel-picks-sub">Top flights and hotels from live prices for your ${esc(money(typeof plan.budget === 'object' ? (plan.budget.total ?? plan.budget.amount ?? 0) : (plan.budget || 0), currency))} budget.</p>`;

    if ((rec.flights || []).length) {
      html += `<p class="travel-section-label"><i class="fa-solid fa-plane"></i> Top flights</p><div class="travel-picks-grid">`;
      rec.flights.forEach((flight) => {
        const o = flight.offer || {};
        html += `
          <article class="pick-card pick-flight">
            <span class="pick-badge"><i class="fa-solid fa-plane"></i> ${esc(flight.label)}</span>
            <div class="pick-main">
              <div>
                ${o.route_iata ? `<span class="route-iata-badge">${esc(o.route_iata)}</span>` : ''}
                <h4>${esc(o.airlines || 'Flight')}</h4>
                <p class="pick-meta">${esc(o.stops === 0 ? 'Non-stop' : `${o.stops} stop${o.stops === 1 ? '' : 's'}`)} · ${formatFlightDuration(o.total_duration)}</p>
              </div>
              <div class="pick-price">${esc(formatOfferPrice(o.price, currency))}</div>
            </div>
            <p class="pick-reason">${esc(flight.reason)}</p>
          </article>`;
      });
      html += '</div>';
    }

    if ((rec.hotels || []).length) {
      html += `<p class="travel-section-label"><i class="fa-solid fa-hotel"></i> Top hotels</p><div class="travel-picks-grid">`;
      rec.hotels.forEach((h) => {
        const o = h.offer || {};
        html += `
          <article class="pick-card pick-hotel">
            <span class="pick-badge pick-badge-hotel"><i class="fa-solid fa-hotel"></i> ${esc(h.label)}</span>
            <div class="pick-main">
              <div class="pick-hotel-info">
                <h4>${esc(o.name || 'Hotel')}</h4>
                ${o.rating ? `<p class="pick-meta"><i class="fa-solid fa-star"></i> ${esc(o.rating)}</p>` : ''}
              </div>
              <div class="pick-price">${esc(formatOfferPrice(o.price, currency))}</div>
            </div>
            <p class="pick-reason">${esc(h.reason)}</p>
          </article>`;
      });
      html += '</div>';
    }

    html += '</div>';
    return html;
  }

  function renderTravelOptions(plan, collapsed = false) {
    const opts = plan.travel_options || {};
    const flights = opts.flights;
    const hotels = opts.hotels;
    const currency = plan.budget?.currency || plan.currency || 'USD';
    const hasFlights = flights?.available && (flights.offers || []).length;
    const hasHotels = hotels?.available && (hotels.offers || []).length;
    if (!hasFlights && !hasHotels) return '';

    const activeTab = hasFlights ? 'flights' : 'hotels';
    const tabs = [];
    if (hasFlights) {
      tabs.push('<button type="button" class="travel-tab active" data-travel-tab="flights"><i class="fa-solid fa-plane"></i> Flights</button>');
    }
    if (hasHotels) {
      tabs.push(`<button type="button" class="travel-tab${hasFlights ? '' : ' active'}" data-travel-tab="hotels"><i class="fa-solid fa-hotel"></i> Hotels</button>`);
    }

    let flightsHtml = '';
    if (hasFlights) {
      const fromCity = flights.origin_city || flights.origin || '';
      const toCity = flights.destination_city || flights.destination || '';
      const ap = flights.airports || {};
      const originAp = (ap.origin || []).map((a) => `${a.iata} · ${a.name}`).join(' · ');
      const destAp = (ap.destination || []).map((a) => `${a.iata} · ${a.name}`).join(' · ');
      flightsHtml = `
        <span class="travel-route-badge"><i class="fa-solid fa-plane"></i> ${esc(fromCity)} → ${esc(toCity)} · ${esc(flights.outbound_date || '')} – ${esc(flights.return_date || '')}</span>
        ${originAp || destAp ? `<div class="travel-airports"><div><strong>From airports:</strong> ${esc(originAp || '—')}</div><div><strong>To airports:</strong> ${esc(destAp || '—')}</div></div>` : ''}
        <div class="travel-grid">${flights.offers.map((o) => `
          <article class="travel-offer">
            <div class="travel-offer-head">
              <div>
                ${o.route_iata ? `<span class="route-iata-badge">${esc(o.route_iata)}</span>` : ''}
                <div class="travel-meta"><strong>${esc(o.airlines || 'Airline')}</strong></div>
                <div class="travel-meta">${esc(o.stops === 0 ? 'Non-stop' : `${o.stops} stop${o.stops === 1 ? '' : 's'}`)} · ${formatFlightDuration(o.total_duration)}</div>
              </div>
              <div class="travel-price">${esc(formatOfferPrice(o.price, currency))}</div>
            </div>
            <div class="travel-meta">
              <strong>${esc(o.departure || '—')}</strong> ${esc(o.departure_time || '')}<br/>
              → <strong>${esc(o.arrival || '—')}</strong> ${esc(o.arrival_time || '')}
            </div>
          </article>`).join('')}</div>`;
    } else if (flights && !flights.available) {
      flightsHtml = `<p class="travel-empty">${esc(flights.reason || 'Flight search unavailable.')}</p>`;
    }

    let hotelsHtml = '';
    if (hasHotels) {
      hotelsHtml = `
        <span class="travel-route-badge"><i class="fa-solid fa-bed"></i> ${esc(hotels.destination)} · ${esc(hotels.check_in || '')} – ${esc(hotels.check_out || '')}</span>
        <div class="travel-grid">${hotels.offers.map((o) => `
          <article class="travel-offer hotel-offer">
            ${o.thumbnail ? `<img class="hotel-thumb" src="${esc(o.thumbnail)}" alt="${esc(o.name)}" loading="lazy" referrerpolicy="no-referrer"/>` : ''}
            <div class="hotel-body">
              <div class="travel-offer-head">
                <div class="travel-meta"><strong>${esc(o.name)}</strong>${o.hotel_class ? `<br/>${esc(o.hotel_class)}` : ''}</div>
                <div class="travel-price">${esc(formatOfferPrice(o.price, currency))}</div>
              </div>
              ${o.rating ? `<div class="hotel-rating"><i class="fa-solid fa-star"></i> ${esc(o.rating)}${o.reviews ? ` · ${esc(o.reviews)} reviews` : ''}</div>` : ''}
              ${o.description ? `<div class="travel-meta">${esc(o.description)}</div>` : ''}
              ${o.link ? `<a href="${esc(o.link)}" target="_blank" rel="noopener noreferrer" class="btn btn-ghost" style="align-self:flex-start;padding:6px 12px;font-size:.82rem;">View deal</a>` : ''}
            </div>
          </article>`).join('')}</div>`;
    } else if (hotels && !hotels.available) {
      hotelsHtml = `<p class="travel-empty">${esc(hotels.reason || 'Hotel search unavailable.')}</p>`;
    }

    return `
      <details class="travel-options-card" ${collapsed ? '' : 'open'}>
        <summary><i class="fa-solid fa-list"></i> All flight &amp; hotel options</summary>
        <p class="travel-options-sub">Full search results — not on your sightseeing route map.</p>
        ${tabs.length > 1 ? `<div class="travel-tabs">${tabs.join('')}</div>` : ''}
        <div class="travel-pane" data-pane="flights" ${activeTab !== 'flights' ? 'hidden' : ''}>${flightsHtml}</div>
        <div class="travel-pane" data-pane="hotels" ${activeTab !== 'hotels' ? 'hidden' : ''}>${hotelsHtml}</div>
      </details>`;
  }

  function bindTravelTabs() {
    document.querySelectorAll('.travel-options-card').forEach((root) => {
      const tabs = root.querySelectorAll('[data-travel-tab]');
      const panes = root.querySelectorAll('.travel-pane');
      tabs.forEach((tab) => {
        tab.addEventListener('click', () => {
          const name = tab.dataset.travelTab;
          tabs.forEach((t) => t.classList.toggle('active', t === tab));
          panes.forEach((p) => { p.hidden = p.dataset.pane !== name; });
        });
      });
    });
  }

  function renderResearch(research) {
    if (!research || typeof research !== 'object') return '';
    
    let html = '<div class="research-card"><h3><i class="fa-solid fa-book-open"></i> Research Findings</h3>';
    
    if (research.local_currency_hint) {
      html += `<div class="r-section"><h4><i class="fa-solid fa-money-bill"></i> Currency Exchange</h4><p style="color: var(--ink-soft); font-size: 0.9rem; margin-top: 5px;">${esc(research.local_currency_hint)}</p></div>`;
    }

    if (research.highlights && research.highlights.length) {
      html += '<div class="r-section"><h4>Highlights</h4><ul>' + 
              research.highlights.map(h => `<li>${esc(h)}</li>`).join('') + 
              '</ul></div>';
    }
    if (research.weather && research.weather.daily && research.weather.daily.length) {
      html += '<div class="r-section"><h4>Weather Outlook</h4><div class="r-weather-grid">' + 
              research.weather.daily.map(w => `<div class="r-weather-item"><strong>${esc(w.date)}:</strong> ${esc(w.summary)}</div>`).join('') + 
              '</div></div>';
    }
    if (research.sources && research.sources.length) {
      html += '<div class="r-section"><h4>Sources</h4><ul class="r-sources">' + 
              research.sources.map(s => `<li><a href="${esc(s)}" target="_blank" rel="noopener noreferrer">${esc(s)}</a></li>`).join('') + 
              '</ul></div>';
    }
    
    html += '</div>';
    return html;
  }
  function renderMcpExportBar(planId, isFinal) {
    if (!planId) return '';
    const pdfUrl = WayfarerAPI.getPdfUrl(planId);
    const calUrl = WayfarerAPI.getCalendarUrl(planId);
    return `
      <div class="mcp-export-card">
        <div class="mcp-export-top">
          <div class="mcp-brand-group">
            <span class="mcp-pill"><i class="fa-solid fa-microchip"></i> Model Context Protocol (MCP)</span>
            <span class="mcp-protocol-meta">Anthropic MCP 2024-11-05 &bull; JSON-RPC 2.0</span>
          </div>
          <span class="mcp-tagline">Export Suite &amp; Concierge Tools</span>
        </div>
        <div class="mcp-buttons-grid">
          <a href="${pdfUrl}" target="_blank" download class="btn-mcp-action btn-mcp-pdf" title="Download high-resolution vector PDF dossier">
            <i class="fa-solid fa-file-pdf"></i>
            <div class="mcp-btn-text">
              <strong>Download PDF Itinerary</strong>
              <span>Visual multi-page dossier</span>
            </div>
          </a>
          <a href="${calUrl}" target="_blank" download class="btn-mcp-action btn-mcp-cal" title="Sync with Google, Apple, or Outlook calendar">
            <i class="fa-solid fa-calendar-plus"></i>
            <div class="mcp-btn-text">
              <strong>Export to Calendar (.ics)</strong>
              <span>RFC 5545 Apple / Google sync</span>
            </div>
          </a>
          <button type="button" class="btn-mcp-action btn-mcp-print" onclick="window.print()" title="Open browser print preview">
            <i class="fa-solid fa-print"></i>
            <div class="mcp-btn-text">
              <strong>Print / Save View</strong>
              <span>Clean vector layout</span>
            </div>
          </button>
          <button type="button" class="btn-mcp-action btn-mcp-share" id="btn-share-comm-trigger" title="Publish itinerary to the Community feed">
            <i class="fa-solid fa-globe" style="color:#0f766e;"></i>
            <div class="mcp-btn-text">
              <strong>Share to Community</strong>
              <span>Get reviews &amp; pro tips</span>
            </div>
          </button>
        </div>
      </div>
    `;
  }

  function renderMcpAdvisories(adv) {
    if (!adv || (!adv.emergency_numbers && !adv.cultural_etiquette)) return '';
    const nums = adv.emergency_numbers || {};
    const tips = adv.cultural_etiquette || [];
    const tipping = adv.tipping_culture || '';
    const transitCard = adv.transit_card_recommendation || '';

    const numPills = Object.entries(nums).map(([k, v]) => `
      <span class="mcp-emergency-pill"><strong>${esc(k)}:</strong> ${esc(v)}</span>
    `).join('');

    const tipItems = tips.map(t => `<li><i class="fa-solid fa-circle-check"></i> ${esc(t)}</li>`).join('');

    return `
      <div class="mcp-advisory-card">
        <div class="mcp-advisory-head">
          <h4><i class="fa-solid fa-shield-halved"></i> Safety &amp; Cultural Etiquette Bulletin</h4>
          <span class="mcp-pill-small"><i class="fa-solid fa-bolt"></i> MCP Tool: fetch_travel_safety_and_etiquette</span>
        </div>
        ${numPills ? `<div class="mcp-emergency-row">${numPills}</div>` : ''}
        ${tipping ? `<p class="mcp-tipping-note"><i class="fa-solid fa-coins"></i> <strong>Tipping &amp; Gratuity:</strong> ${esc(tipping)}</p>` : ''}
        ${transitCard ? `<p class="mcp-transit-note"><i class="fa-solid fa-ticket"></i> <strong>Transit Pass Recommendation:</strong> ${esc(transitCard)}</p>` : ''}
        ${tipItems ? `<ul class="mcp-etiquette-list">${tipItems}</ul>` : ''}
      </div>
    `;
  }

  function renderNote(heading, content) {
    let inner;
    if (Array.isArray(content)) {
      inner = `<ul>${content.map((c) => `<li>${esc(typeof c === 'string' ? c : (c.title || c.content || JSON.stringify(c)))}</li>`).join('')}</ul>`;
    } else if (typeof content === 'string') {
      inner = `<p>${esc(content)}</p>`;
    } else {
      inner = `<pre>${esc(JSON.stringify(content, null, 2))}</pre>`;
    }
    return `<div class="note-block"><h3>${esc(heading)}</h3>${inner}</div>`;
  }

  // ─── New plan reset ─────────────────────────────────────
  els.newPlanBtn.addEventListener('click', () => {
    stopPolling();
    state.planId = null;
    state.status = null;
    clearSession();
    els.workspace.hidden = true;
    els.draftArea.hidden = true;
    if (window.WayfarerMaps) window.WayfarerMaps.destroyAll();
    els.itinerary.innerHTML = '';
    document.getElementById('planner').scrollIntoView({ behavior: 'smooth' });
  });

  // ─── Init ───────────────────────────────────────────────
  async function init() {
    renderChips();
    // sensible default dates: 30 days out, 4-night trip
    const start = new Date(Date.now() + 30 * 864e5);
    const end = new Date(start.getTime() + 4 * 864e5);
    const iso = (d) => d.toISOString().slice(0, 10);
    els.form.start_date.value = iso(start);
    els.form.end_date.value = iso(end);
    if (typeof updateNightCounter === "function") updateNightCounter();

    const apiConfig = await WayfarerAPI.getConfig();
    if (apiConfig.carto_api_key && window.WayfarerMaps) {
      window.WayfarerMaps.setApiKey(apiConfig.carto_api_key);
    }

    await checkApi();

    // Check if user reached page with form GET parameters
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has('destination') && urlParams.get('destination').trim()) {
      if (els.form.destination) els.form.destination.value = urlParams.get('destination');
      if (urlParams.get('origin') && els.form.origin) els.form.origin.value = urlParams.get('origin');
      if (urlParams.get('start_date') && els.form.start_date) els.form.start_date.value = urlParams.get('start_date');
      if (urlParams.get('end_date') && els.form.end_date) els.form.end_date.value = urlParams.get('end_date');
      if (urlParams.get('budget') && els.form.budget) els.form.budget.value = urlParams.get('budget');
      if (urlParams.get('currency') && els.form.currency) els.form.currency.value = urlParams.get('currency');
      if (urlParams.get('travelers') && els.form.travelers) els.form.travelers.value = urlParams.get('travelers');
      if (urlParams.get('custom_notes') && els.form.custom_notes) els.form.custom_notes.value = urlParams.get('custom_notes');

      // Clean query string from browser URL bar without reloading
      window.history.replaceState({}, document.title, window.location.pathname);

      // Trigger automatic plan submission!
      setTimeout(() => {
        els.form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
      }, 150);
      return;
    }

    await restoreSession();
    initAuthAndShare();
  }

  // ─── Supabase Auth & Community Share Controller ──────────────────
  function initAuthAndShare() {
    const authPill = document.getElementById('auth-nav-pill');
    const openAuthBtn = document.getElementById('open-auth-btn');
    const authModal = document.getElementById('auth-modal');
    const authClose = document.getElementById('auth-modal-close');
    const tabLogin = document.getElementById('tab-login-btn');
    const tabSignup = document.getElementById('tab-signup-btn');
    const loginForm = document.getElementById('login-form');
    const signupForm = document.getElementById('signup-form');
    const loginError = document.getElementById('login-error');
    const signupError = document.getElementById('signup-error');

    function updateAuthNav() {
      const user = WayfarerAPI.getUser();
      if (!authPill) return;
      if (user) {
        authPill.innerHTML = `
          <div class="user-logged-wrap" style="display:flex; align-items:center; gap:8px;">
            <img src="${esc(user.avatar_url || 'https://ui-avatars.com/api/?name=User')}" alt="${esc(user.full_name)}" class="user-avatar-tiny" />
            <span style="font-size:0.85rem; font-weight:700; color:var(--ink);">${esc(user.full_name.split(' ')[0])}</span>
            <button type="button" class="btn btn-ghost" id="logout-btn" style="padding:4px 8px; font-size:0.75rem; color:var(--ink-soft);" title="Sign out"><i class="fa-solid fa-arrow-right-from-bracket"></i></button>
          </div>
        `;
        document.getElementById('logout-btn')?.addEventListener('click', () => {
          WayfarerAPI.logout();
          updateAuthNav();
          toast('Signed out successfully.', 'ok');
        });
      } else {
        authPill.innerHTML = `
          <button type="button" class="btn btn-auth-pill" id="open-auth-btn"><i class="fa-solid fa-user"></i> <span>Sign In</span></button>
        `;
        document.getElementById('open-auth-btn')?.addEventListener('click', () => {
          if (authModal) authModal.hidden = false;
        });
      }
    }

    openAuthBtn?.addEventListener('click', () => { if (authModal) authModal.hidden = false; });
    authClose?.addEventListener('click', () => { if (authModal) authModal.hidden = true; });

    tabLogin?.addEventListener('click', () => {
      tabLogin.classList.add('active');
      tabSignup?.classList.remove('active');
      if (loginForm) loginForm.hidden = false;
      if (signupForm) signupForm.hidden = true;
    });

    tabSignup?.addEventListener('click', () => {
      tabSignup.classList.add('active');
      tabLogin?.classList.remove('active');
      if (signupForm) signupForm.hidden = false;
      if (loginForm) loginForm.hidden = true;
    });

    loginForm?.addEventListener('submit', async () => {
      const email = document.getElementById('login-email')?.value;
      const password = document.getElementById('login-password')?.value;
      if (loginError) loginError.hidden = true;
      try {
        await WayfarerAPI.login({ email, password });
        if (authModal) authModal.hidden = true;
        updateAuthNav();
        toast('Welcome back! Signed in to Supabase.', 'ok');
      } catch (err) {
        if (loginError) {
          loginError.textContent = err.message || 'Login failed.';
          loginError.hidden = false;
        }
      }
    });

    signupForm?.addEventListener('submit', async () => {
      const full_name = document.getElementById('signup-name')?.value;
      const email = document.getElementById('signup-email')?.value;
      const password = document.getElementById('signup-password')?.value;
      const bio = document.getElementById('signup-bio')?.value || '';
      if (signupError) signupError.hidden = true;
      try {
        await WayfarerAPI.signup({ full_name, email, password, bio });
        if (authModal) authModal.hidden = true;
        updateAuthNav();
        toast('Account created! Welcome to Wayfarer.', 'ok');
      } catch (err) {
        if (signupError) {
          signupError.textContent = err.message || 'Registration failed.';
          signupError.hidden = false;
        }
      }
    });

    updateAuthNav();

    // ── Share Itinerary Modal
    const shareModal = document.getElementById('share-modal');
    const shareClose = document.getElementById('share-modal-close');
    const shareForm = document.getElementById('share-form');
    const shareError = document.getElementById('share-error');

    shareClose?.addEventListener('click', () => { if (shareModal) shareModal.hidden = true; });

    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-share-comm-trigger')) {
        const finalPlan = pickPlan(state.data);
        if (!finalPlan || !finalPlan.destination) {
          toast('Plan must be finalized before publishing to the community.', 'warn');
          return;
        }
        if (!WayfarerAPI.isLoggedIn()) {
          toast('Please sign in to publish your itinerary to the community!', 'warn');
          if (authModal) authModal.hidden = false;
          return;
        }
        const titleInput = document.getElementById('share-title');
        if (titleInput) {
          titleInput.value = `${finalPlan.destination} ${finalPlan.days ? finalPlan.days.length : 4}-Day Journey`;
        }
        if (shareModal) shareModal.hidden = false;
      }
    });

    shareForm?.addEventListener('submit', async () => {
      const finalPlan = pickPlan(state.data);
      if (!finalPlan) return;

      const title = document.getElementById('share-title')?.value.trim();
      const desc = document.getElementById('share-desc')?.value.trim();
      const tagBoxes = document.querySelectorAll('#share-tags-picker input[type="checkbox"]:checked');
      const tags = Array.from(tagBoxes).map(cb => cb.value);

      if (shareError) shareError.hidden = true;

      const prefs = state.data?.preferences || {};
      const payload = {
        plan_id: state.planId,
        destination: finalPlan.destination || prefs.destination || 'Destination',
        title: title || `${finalPlan.destination} Journey`,
        description: desc || finalPlan.summary || 'A curated multi-day journey.',
        origin: prefs.origin || finalPlan.origin || null,
        duration_days: (finalPlan.days || []).length || 4,
        budget: prefs.budget || (finalPlan.budget && finalPlan.budget.total) || null,
        currency: prefs.currency || 'USD',
        travelers: prefs.travelers || 1,
        tags: tags.length ? tags : ['Culture', 'Sightseeing'],
        itinerary_data: finalPlan,
      };

      try {
        await WayfarerAPI.createCommunityTrip(payload);
        if (shareModal) shareModal.hidden = true;
        toast('🎉 Itinerary published! Visit the Community Hub to see reviews.', 'ok');
      } catch (err) {
        if (shareError) {
          shareError.textContent = err.message || 'Failed to publish itinerary.';
          shareError.hidden = false;
        }
      }
    });
  }

  init();

  // ─── Slide-Out Drawer Logic (Chat / Replace) ───────────────────────
  const elsDrawer = {
    overlay: document.getElementById('drawer-overlay'),
    drawer: document.getElementById('side-drawer'),
    closeBtn: document.getElementById('drawer-close'),
    img: document.getElementById('drawer-img'),
    title: document.getElementById('drawer-title'),
    loc: document.getElementById('drawer-loc'),
    body: document.getElementById('drawer-body'),
    chatFooter: document.getElementById('drawer-footer-chat'),
    chatInput: document.getElementById('chat-input-box'),
    chatSendBtn: document.getElementById('chat-send-btn')
  };

  let _drawerContext = null;

  function closeDrawer() {
    if (elsDrawer.overlay) elsDrawer.overlay.classList.remove('open');
    if (elsDrawer.drawer) elsDrawer.drawer.classList.remove('open');
    _drawerContext = null;
  }

  if (elsDrawer.closeBtn) elsDrawer.closeBtn.addEventListener('click', closeDrawer);
  if (elsDrawer.overlay) elsDrawer.overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDrawer();
  });

  // Extract activity from plan state
  function getActivityData(dayNum, idx) {
    const finalPlan = pickPlan(state.data);
    if (!finalPlan || !finalPlan.days) return null;
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == dayNum);
    if (!day || !day.activities || !day.activities[idx]) return null;
    const act = day.activities[idx];
    
    let title = ''; let locationName = ''; let time = ''; let desc = ''; let category = '';
    if (typeof act === 'string') {
      title = act.trim();
    } else {
      time = act.time || act.start || act.when || act.period || '';
      title = (act.title || act.name || act.activity || act.label || '').trim();
      locationName = (act.location_name || act.location || extractLandmarkKeyword(title) || title).trim();
      desc = act.description || act.detail || act.notes || '';
      category = act.category || '';
    }
    
    const searchTarget = locationName || title;
    const imgKey = activityImageKey(title, searchTarget);
    const imgSrc = _imageStore.get(imgKey) || 'https://images.unsplash.com/photo-1488646953014-85cb44e25828?auto=format&fit=crop&w=480&q=80';
    
    return { title, locationName, time, desc, category, searchTarget, imgSrc, actObj: act, dayNum, idx };
  }

  // Get all existing places across itinerary
  function getExistingPlaces() {
    const places = [];
    const finalPlan = pickPlan(state.data);
    if (!finalPlan || !finalPlan.days) return places;
    
    finalPlan.days.forEach(day => {
      if (!day.activities) return;
      day.activities.forEach(act => {
        let title = '';
        if (typeof act === 'string') title = act.trim();
        else title = (act.title || act.name || act.activity || act.label || '').trim();
        if (title) places.push(title);
      });
    });
    return places;
  }

  // Delegate clicks for hover actions
  els.itinerary.addEventListener('click', (e) => {
    const btn = e.target.closest('.action-btn');
    if (!btn) return;
    
    const action = btn.dataset.action;
    const dayNum = parseInt(btn.dataset.day, 10);
    const idx = parseInt(btn.dataset.idx, 10);
    
    const actData = getActivityData(dayNum, idx);
    if (!actData) return;
    
    if (action === 'remove') {
      handleRemove(actData);
    } else if (action === 'replace') {
      openReplaceDrawer(actData);
    } else if (action === 'chat') {
      openChatDrawer(actData);
    }
  });

  // Handle Remove
  let _lastRemoved = null; // For undo
  function handleRemove(actData) {
    const finalPlan = pickPlan(state.data);
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == actData.dayNum);
    
    // Save for undo
    _lastRemoved = {
      dayNum: actData.dayNum,
      idx: actData.idx,
      actObj: day.activities[actData.idx]
    };
    
    // Remove from array
    day.activities.splice(actData.idx, 1);
    
    // Update UI
    toast(`Removed ${esc(actData.title)} from Day ${actData.dayNum}. <button onclick="window.undoRemove()" style="background:transparent;border:0;color:inherit;text-decoration:underline;cursor:pointer;font-weight:bold;margin-left:8px;">Undo</button>`, 'ok');
    
    loadFinal(false); // Re-render itinerary and map
  }
  
  window.undoRemove = function() {
    if (!_lastRemoved) return;
    const finalPlan = pickPlan(state.data);
    const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == _lastRemoved.dayNum);
    if (day) {
      day.activities.splice(_lastRemoved.idx, 0, _lastRemoved.actObj);
      _lastRemoved = null;
      toast('Activity restored.', 'ok');
      loadFinal(false);
    }
  };

  // Open Chat Drawer
  function openChatDrawer(actData) {
    _drawerContext = { type: 'chat', data: actData, history: [] };
    
    elsDrawer.img.src = actData.imgSrc;
    elsDrawer.title.textContent = actData.title;
    const dest = (pickPlan(state.data).destination || els.wsDestination.textContent || '').split(',')[0].trim();
    elsDrawer.loc.textContent = dest;
    
    elsDrawer.chatFooter.hidden = false;
    
    // Initial UI state
    elsDrawer.body.innerHTML = `
      <div class="vibe-badges">
        <span class="vibe-badge">Loading Vibes...</span>
      </div>
      <div class="chat-stream" id="chat-stream">
        <div class="chat-bubble chat-ai">
          Hi! I'm your local insider for ${esc(actData.title)}. What would you like to know about the vibe, crowd, or tips?
        </div>
      </div>
      <div class="chat-chips" id="chat-chips">
        <button class="chat-chip" data-query="Is it good for a date night?">Is it good for a date night?</button>
        <button class="chat-chip" data-query="What is the dress code?">What is the dress code?</button>
        <button class="chat-chip" data-query="Are vegan options available?">Are vegan options available?</button>
        <button class="chat-chip" data-query="What's the crowd like?">What's the crowd like?</button>
      </div>
    `;
    
    elsDrawer.overlay.classList.add('open');
    elsDrawer.drawer.classList.add('open');
    elsDrawer.chatInput.focus();
  }

  // Handle sending chat messages
  async function sendChatMessage(query) {
    if (!query || !_drawerContext || _drawerContext.type !== 'chat') return;
    
    const stream = document.getElementById('chat-stream');
    const chipsDiv = document.getElementById('chat-chips');
    
    // Add user message
    stream.innerHTML += `<div class="chat-bubble chat-user">${esc(query)}</div>`;
    elsDrawer.chatInput.value = '';
    chipsDiv.innerHTML = ''; // Clear chips while loading
    elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    
    // Add loading indicator
    const loadingId = 'loading-' + Date.now();
    stream.innerHTML += `<div class="chat-bubble chat-ai" id="${loadingId}"><i class="fa-solid fa-circle-notch fa-spin"></i> Getting the vibe...</div>`;
    elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    
    const actData = _drawerContext.data;
    const payload = {
      place_name: actData.title,
      destination: pickPlan(state.data).destination || '',
      query: query,
      description: actData.desc,
      category: actData.category,
      chat_history: _drawerContext.history
    };
    
    // Append to history for next time
    _drawerContext.history.push({ role: 'user', content: query });
    
    try {
      const res = await WayfarerAPI.chatPlace(payload);
      
      // Update vibe tags in header if provided
      if (res.vibe_tags && res.vibe_tags.length) {
        const badgesHtml = res.vibe_tags.map(t => `<span class="vibe-badge">${esc(t)}</span>`).join('');
        const badgeContainer = elsDrawer.body.querySelector('.vibe-badges');
        if (badgeContainer) badgeContainer.innerHTML = badgesHtml;
      }
      
      // Replace loading bubble with AI response
      const loader = document.getElementById(loadingId);
      if (loader) loader.outerHTML = `<div class="chat-bubble chat-ai">${esc(res.reply)}</div>`;
      
      _drawerContext.history.push({ role: 'assistant', content: res.reply });
      
      // Update quick chips
      if (res.suggested_followups && res.suggested_followups.length) {
        chipsDiv.innerHTML = res.suggested_followups.map(q => `<button class="chat-chip" data-query="${esc(q)}">${esc(q)}</button>`).join('');
      }
      
      elsDrawer.body.scrollTo({ top: elsDrawer.body.scrollHeight, behavior: 'smooth' });
    } catch (err) {
      console.error("Chat error", err);
      const loader = document.getElementById(loadingId);
      if (loader) loader.outerHTML = `<div class="chat-bubble chat-ai" style="color:var(--bad);">Failed to fetch vibe. Please try again.</div>`;
    }
  }

  if (elsDrawer.chatSendBtn) {
    elsDrawer.chatSendBtn.addEventListener('click', () => sendChatMessage(elsDrawer.chatInput.value.trim()));
  }
  if (elsDrawer.chatInput) {
    elsDrawer.chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); sendChatMessage(elsDrawer.chatInput.value.trim()); }
    });
  }
  // Delegate clicks for chat chips
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('chat-chip')) {
      sendChatMessage(e.target.dataset.query);
    }
  });

  // Open Replace Drawer
  function openReplaceDrawer(actData) {
    _drawerContext = { type: 'replace', data: actData };
    
    elsDrawer.img.src = actData.imgSrc;
    elsDrawer.title.textContent = `Replacing: ${actData.title}`;
    elsDrawer.loc.textContent = `Day ${actData.dayNum} · ${actData.time}`;
    
    elsDrawer.chatFooter.hidden = true;
    
    elsDrawer.body.innerHTML = `
      <div class="replace-search-box">
        <button type="button" class="replace-preprompt" id="btn-preprompt">
          <i class="fa-solid fa-wand-magic-sparkles"></i> Suggest me some alternatives
        </button>
        <div class="replace-or">or</div>
        <div class="chat-input-area" style="padding:0; border:none; border-radius:999px;">
          <input type="text" id="replace-input" placeholder="e.g. cozy riverside cafe..." autocomplete="off" />
          <button type="button" class="chat-send-btn" id="replace-search-btn"><i class="fa-solid fa-magnifying-glass"></i></button>
        </div>
      </div>
      <div id="replace-results" style="display:flex; flex-direction:column; gap:16px;"></div>
    `;
    
    elsDrawer.overlay.classList.add('open');
    elsDrawer.drawer.classList.add('open');
    
    document.getElementById('btn-preprompt').addEventListener('click', () => doReplaceSearch(''));
    document.getElementById('replace-search-btn').addEventListener('click', () => {
      const val = document.getElementById('replace-input').value.trim();
      if (val) doReplaceSearch(val);
    });
    document.getElementById('replace-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const val = e.target.value.trim();
        if (val) doReplaceSearch(val);
      }
    });
  }

  async function doReplaceSearch(pref) {
    const resDiv = document.getElementById('replace-results');
    resDiv.innerHTML = `<div style="text-align:center; padding: 40px; color: var(--brand-ink);"><i class="fa-solid fa-circle-notch fa-spin fa-2x"></i><p style="margin-top:10px;font-weight:600;">Finding unique alternatives...</p></div>`;
    
    const actData = _drawerContext.data;
    const payload = {
      current_place: actData.title,
      destination: pickPlan(state.data).destination || '',
      time_slot: actData.time || 'Daytime',
      day_number: actData.dayNum,
      user_preference: pref,
      existing_places: getExistingPlaces()
    };
    
    try {
      const res = await WayfarerAPI.replacePlace(payload);
      
      if (!res.alternatives || !res.alternatives.length) {
        resDiv.innerHTML = `<p style="text-align:center; color:var(--bad);">No alternatives found. Try a different request.</p>`;
        return;
      }
      
      // Store globally for the swap button to access
      window._replaceAlts = res.alternatives;
      
      let html = '';
      res.alternatives.forEach((alt, i) => {
        html += `
          <div class="alt-card">
            <div class="alt-header">
              <div>
                <h4 class="alt-title">${esc(alt.title)}</h4>
                <div class="alt-loc">${esc(alt.location_name)}</div>
              </div>
              <div class="alt-cost">${money(alt.estimated_cost)}</div>
            </div>
            <p class="alt-desc">${esc(alt.description)}</p>
            <button type="button" class="btn-swap" data-alt-idx="${i}">Swap This In</button>
          </div>
        `;
      });
      resDiv.innerHTML = html;
      
    } catch (err) {
      console.error(err);
      resDiv.innerHTML = `<p style="text-align:center; color:var(--bad);">Failed to fetch alternatives. Ensure API is running.</p>`;
    }
  }

  // Handle Swap Click
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('btn-swap')) {
      const idx = parseInt(e.target.dataset.altIdx, 10);
      const alt = window._replaceAlts[idx];
      if (!alt || !_drawerContext || _drawerContext.type !== 'replace') return;
      
      const actData = _drawerContext.data;
      const finalPlan = pickPlan(state.data);
      const day = finalPlan.days.find(d => (d.day ?? d.day_number ?? d.index) == actData.dayNum);
      
      // Perform the swap
      const newAct = {
        title: alt.title,
        location_name: alt.location_name,
        time: alt.time || actData.time,
        description: alt.description,
        cost: alt.estimated_cost,
        category: alt.category
      };
      day.activities[actData.idx] = newAct;
      
      closeDrawer();
      toast(`Swapped in ${esc(alt.title)}!`, 'ok');
      loadFinal(false); // Re-render itinerary and map
    }
  });


})();
