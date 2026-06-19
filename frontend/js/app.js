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
    thinkingTimer: null,
    thinkingIdx: 0,
    travelPick: { flight: null, hotel: null },
    travelPickData: null,
  };

  const STEPS = ['researching', 'planning', 'awaiting_review', 'finalizing', 'completed'];
  const STEP_LABEL = {
    researching: 'Researching your destination…',
    planning: 'Planning your itinerary…',
    awaiting_review: 'Paused — waiting for your review.',
    finalizing: 'Finalizing your approved trip…',
    completed: 'Plan finalized.',
  };
  const THINKING_LINES = {
    researching: [
      'Scanning travel guides and local tips…',
      'Checking weather for your travel dates…',
      'Looking up currency rates…',
      'Distilling highlights with AI…',
    ],
    planning: [
      'Splitting your budget across lodging, food & activities…',
      'Scheduling sights day by day…',
      'Matching indoor/outdoor plans to the forecast…',
      'Writing your draft itinerary…',
    ],
    finalizing: [
      'Expanding activities with times and descriptions…',
      'Mapping routes between sights…',
      'Applying your travel selections…',
    ],
    modifying: [
      'Applying your feedback to the draft…',
      'Rebalancing days and activities…',
    ],
    rejecting: [
      'Running a fresh web search…',
      'Rebuilding research from scratch…',
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
    thinkingLine: $('#thinking-line'),
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
  async function checkApi() {
    const ok = await WayfarerAPI.ping();
    els.apiStatus.classList.toggle('online', ok);
    els.apiStatus.classList.toggle('offline', !ok);
    els.apiStatusText.textContent = ok
      ? `API connected${WayfarerAPI.base ? '' : ' (same origin)'}`
      : 'API offline';
  }

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

  function stopThinking() {
    if (state.thinkingTimer) clearInterval(state.thinkingTimer);
    state.thinkingTimer = null;
  }

  function startThinking(status, serverMessage) {
    stopThinking();
    const key = status === 'planning' && state.reviewMode ? state.reviewMode : status;
    const lines = THINKING_LINES[key] || THINKING_LINES[status] || [];
    if (serverMessage) {
      els.thinkingLine.textContent = serverMessage;
    } else if (lines.length) {
      state.thinkingIdx = 0;
      els.thinkingLine.textContent = lines[0];
      state.thinkingTimer = setInterval(() => {
        state.thinkingIdx = (state.thinkingIdx + 1) % lines.length;
        els.thinkingLine.textContent = lines[state.thinkingIdx];
      }, 2800);
    } else {
      els.thinkingLine.textContent = '';
    }
  }

  function showWorking(status, progressMessage) {
    const busy = ['researching', 'planning', 'finalizing'].includes(status) || !status;
    els.workingBanner.style.display = busy ? 'flex' : 'none';
    els.workingText.textContent = STEP_LABEL[status] || 'Agents are working…';
    if (busy) {
      startThinking(status, progressMessage);
    } else {
      stopThinking();
      els.thinkingLine.textContent = progressMessage || '';
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
    const include_flights = !!origin;
    const include_hotels = true;

    // client-side validation mirroring schemas.py
    if (destination.length < 2) return formErr('Please enter a destination (at least 2 characters).');
    if (!start_date || !end_date) return formErr('Please choose both start and end dates.');
    if (new Date(end_date) < new Date(start_date)) return formErr('End date must be on or after the start date.');
    if (!(budget > 0)) return formErr('Budget must be greater than 0.');
    if (!(travelers >= 1 && travelers <= 20)) return formErr('Travelers must be between 1 and 20.');

    const payload = {
      destination, start_date, end_date, budget, currency, travelers,
      interests: state.interests.slice(),
      origin, flight_destination: '',
      include_flights, include_hotels,
    };

    setSubmitting(true);
    try {
      const res = await WayfarerAPI.createPlan(payload);
      state.planId = res && (res.plan_id || res.id || res.planId);
      if (!state.planId) throw new Error('No plan id returned by the server.');

      openWorkspace(destination, payload);
      saveSession({ destination, payload });
      toast('Plan started — agents are working.', 'ok');
      startPolling();
      poll();
    } catch (err) {
      formErr(err.message || 'Could not create the plan. Is the API running?');
      toast(err.message || 'Failed to create plan.', 'bad');
    } finally {
      setSubmitting(false);
    }
  });

  function formErr(msg) { els.formError.textContent = msg; els.formError.hidden = false; }
  function setSubmitting(on) {
    els.submitBtn.disabled = on;
    els.submitBtn.innerHTML = on
      ? '<span class="spinner" style="border-color:rgba(255,255,255,.4);border-top-color:#fff"></span> Creating…'
      : '<i class="fa-solid fa-route"></i> Build my itinerary';
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
      showWorking('researching');
      els.workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // ─── Polling state machine ──────────────────────────────
  function startPolling() {
    stopPolling();
    state.polling = true;
    poll();
  }
  function stopPolling() {
    state.polling = false;
    state.poll404Retries = 0;
    if (state.pollTimer) clearTimeout(state.pollTimer);
    stopThinking();
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

  function handleState(data) {
    if (!data) return;
    const status = data.status || data.plan_status;
    const progress = data.progress_message || data.progressMessage || '';
    state.status = status;
    state.reviewMode = null;
    setPipeline(status);
    showWorking(status, progress);
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
        els.workingBanner.style.display = 'flex';
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

  function travelPickKey(type, offer) {
    if (!offer) return '';
    return `${type}:${offer.route_iata || offer.name || ''}:${offer.price}:${offer.airlines || ''}`;
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
      rec.flights.forEach((f) => {
        const o = f.offer || {};
        html += `
          <button type="button" class="travel-pick-card" data-pick-type="flight" data-pick-key="${esc(travelPickKey('flight', o))}">
            <span class="pick-badge"><i class="fa-solid fa-plane"></i> ${esc(f.label)}</span>
            <strong>${esc(o.airlines || 'Flight')}</strong>
            <span class="pick-meta">${esc(o.route_iata || '')} · ${esc(formatOfferPrice(o.price, currency))}</span>
            <span class="pick-meta">${esc(o.stops === 0 ? 'Non-stop' : `${o.stops} stop(s)`)} · ${formatFlightDuration(o.total_duration)}</span>
            <span class="pick-select-label">Tap to select</span>
          </button>`;
      });
      html += '</div>';
    }

    if ((rec.hotels || []).length) {
      html += `<p class="travel-section-label"><i class="fa-solid fa-hotel"></i> Top hotels</p><div class="travel-review-grid">`;
      rec.hotels.forEach((h) => {
        const o = h.offer || {};
        html += `
          <button type="button" class="travel-pick-card" data-pick-type="hotel" data-pick-key="${esc(travelPickKey('hotel', o))}">
            <span class="pick-badge pick-badge-hotel"><i class="fa-solid fa-hotel"></i> ${esc(h.label)}</span>
            <strong>${esc(o.name || 'Hotel')}</strong>
            <span class="pick-meta">${o.rating ? `<i class="fa-solid fa-star"></i> ${esc(o.rating)} · ` : ''}${esc(formatOfferPrice(o.price, currency))}</span>
            <span class="pick-select-label">Tap to select</span>
          </button>`;
      });
      html += '</div>';
    }

    html += `<p class="travel-review-skip"><i class="fa-solid fa-circle-info"></i> No selection? We keep your simple sightseeing-only flow.</p></div>`;

    root.innerHTML = html;
    root.hidden = false;

    root.querySelectorAll('.travel-pick-card').forEach((btn) => {
      btn.addEventListener('click', () => {
        const type = btn.dataset.pickType;
        const key = btn.dataset.pickKey;
        let offer = null;
        if (type === 'flight') {
          const hit = (rec.flights || []).find((f) => travelPickKey('flight', f.offer) === key);
          offer = hit?.offer || null;
        } else if (type === 'hotel') {
          const hit = (rec.hotels || []).find((h) => travelPickKey('hotel', h.offer) === key);
          offer = hit?.offer || null;
        }
        const already = state.travelPick[type] && travelPickKey(type, state.travelPick[type]) === key;
        state.travelPick[type] = already ? null : offer;
        root.querySelectorAll(`.travel-pick-card[data-pick-type="${type}"]`).forEach((el) => {
          const selected = state.travelPick[type] && el.dataset.pickKey === travelPickKey(type, state.travelPick[type]);
          el.classList.toggle('selected', !!selected);
          el.querySelector('.pick-select-label').textContent = selected ? 'Selected ✓' : 'Tap to select';
        });
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
      showWorking('researching', 'Restarting — fresh web search and weather for your trip…');
    } else if (action === 'modify') {
      setPipeline('planning');
      showWorking('planning', 'Sending your notes to the planner agent…');
    } else {
      setPipeline('finalizing');
      showWorking('finalizing', 'Approved — expanding your itinerary and mapping your route…');
    }

    try {
      const travelSelections = action === 'approve' ? getTravelSelectionsForApprove() : null;
      const data = await WayfarerAPI.review(state.planId, action, feedback, travelSelections);
      handleState(data);
      startPolling();
      const msg = {
        approve: 'Finalizing in the background — watch the progress above.',
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
      showWorking('awaiting_review');
    }
  }

  // ─── Final plan ─────────────────────────────────────────
  async function loadFinal(isRestore = false) {
    setPipeline('completed');
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
    els.itinerary.innerHTML = renderPlanHTML(plan, false, data);
  }
  function renderFinal(data) {
    const plan = pickPlan(data);
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
    els.itinerary.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // Find the itinerary payload regardless of nesting.
  function pickPlan(data) {
    if (!data) return {};
    return data.final_plan || data.draft_itinerary || data.plan || data.itinerary || data.draft || data.result || data;
  }

  function renderPlanHTML(plan, isFinal, fullData = {}) {
    const parts = [];

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

    parts.push(`
      <div class="summary-card">
        <h3><i class="fa-solid fa-map-location-dot"></i> ${esc(dest)}</h3>
        ${revisionHtml}
        ${summary ? `<p class="sum-sub">${esc(typeof summary === 'string' ? summary : JSON.stringify(summary))}</p>` : ''}
        <div class="sum-stats">${summaryStats(plan)}</div>
      </div>`);

    if (isFinal && plan.selected_travel) {
      parts.push(renderSelectedTravel(plan.selected_travel, plan.budget?.currency || plan.currency || 'USD'));
    }

    if (isFinal && plan.travel_options?.recommendations?.has_picks && !plan.selected_travel) {
      parts.push(renderTravelRecommendations(plan));
    }

    if (isFinal && plan.travel_options && (plan.travel_options.flights?.available || plan.travel_options.hotels?.available)) {
      parts.push(renderTravelOptions(plan, true));
    }

    if (isFinal && plan.route_map && window.WayfarerMaps) {
      parts.push(window.WayfarerMaps.renderRouteSection(plan.route_map));
    }

    const budget = plan.budget || plan.budget_breakdown || plan.budget_allocation || plan.allocation;
    if (budget && typeof budget === 'object') parts.push(renderBudget(budget, plan));

    const routeByDay = {};
    if (plan.route_map?.days) {
      plan.route_map.days.forEach((d) => { routeByDay[d.day_number] = d; });
    }

    // Days / itinerary
    const days = findDays(plan);
    if (days.length) {
      days.forEach((d, i) => {
        const num = d.day ?? d.day_number ?? d.index ?? (i + 1);
        parts.push(renderDay(d, i, dest, !isFinal, routeByDay[num]));
      });
    } else {
      // fallback: dump notes / research / raw json
      const notes = plan.notes || plan.research || plan.context || plan.findings;
      if (notes) parts.push(renderNote('Notes & research', notes));
      else parts.push(renderNote('Plan data', plan));
    }
    
    // Explicitly show planner notes and tips
    if (plan.planner_notes && plan.planner_notes.length) {
      parts.push(renderNote('Planner Notes', plan.planner_notes));
    }
    if (plan.packing_list && plan.packing_list.length) {
      parts.push(renderNote('Recommended Packing List', plan.packing_list));
    }
    if (plan.tips && plan.tips.length) {
      parts.push(renderNote('Travel Tips', plan.tips));
    }

    // Explicit Research Section
    if (fullData.research) {
        parts.push(renderResearch(fullData.research));
    }

    return parts.join('');
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

  function renderDay(day, i, destination, isDraft, dayRoute) {
    const num = day.day ?? day.day_number ?? day.index ?? (i + 1);
    const title = day.title || day.theme || day.name || `Day ${num}`;
    const date = day.date || day.day_date || '';
    const weather = day.weather || day.forecast;
    const acts = day.activities || day.items || day.schedule || day.plan || [];

    const weatherHTML = weather ? `<span class="day-weather"><i class="fa-solid fa-cloud-sun"></i> ${esc(weatherText(weather))}</span>` : '';
    const actsHTML = Array.isArray(acts) && acts.length
      ? acts.map(a => renderActivity(a, destination, isDraft)).join('')
      : (typeof day.description === 'string' && !isDraft ? `<p class="a-desc" style="padding:12px 0">${esc(day.description)}</p>` : '<p class="a-desc" style="padding:12px 0">No activities listed.</p>');

    // If day has a description, show it above the activities
    const dayDesc = day.description && Array.isArray(acts) && acts.length && !isDraft ? `<p class="d-desc" style="color:var(--ink-soft); font-size: 0.95rem; margin-bottom: 12px;">${esc(day.description)}</p>` : '';

    const dayRouteMeta = !isDraft && dayRoute;
    const routeBadge = dayRouteMeta && dayRouteMeta.distance_km > 0
      ? `<span class="day-route"><i class="fa-solid fa-route"></i> ${dayRouteMeta.distance_km} km · ${window.WayfarerMaps ? window.WayfarerMaps.formatDuration(dayRouteMeta.duration_minutes) : dayRouteMeta.duration_minutes + ' min'} total travel</span>`
      : '';

    return `<div class="day-card">
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

  function renderActivity(a, destination, isDraft) {
    if (typeof a === 'string') {
      return `<div class="activity">
                <div class="a-body" style="flex-direction: row; text-align: left;">
                  <div class="a-text"><p class="a-title" style="margin:0;">${esc(a)}</p></div>
                </div>
              </div>`;
    }
    const time = a.time || a.start || a.when || a.period || '';
    const title = a.title || a.name || a.activity || a.label || '';
    const desc = a.description || a.detail || a.notes || '';
    const cost = a.cost ?? a.price ?? a.amount;
    
    // Images disabled for faster loading
    const destName = typeof destination === 'string' ? destination.split(',')[0] : '';
    
    const titleHTML = `<a href="https://www.google.com/search?q=${encodeURIComponent(destName + ' ' + title)}" target="_blank" rel="noopener noreferrer" style="color: inherit; text-decoration: none; border-bottom: 1px dotted var(--line);">${esc(title)}</a>`;

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

    return `<div class="activity">
      ${time && !isDraft ? `<span class="a-time">${esc(time)}</span>` : ''}
      <div class="a-body" ${isDraft ? 'style="flex-direction: row; text-align: left; align-items: center;"' : ''}>
        <div class="a-text">
          <p class="a-title" style="${isDraft ? 'margin:0;' : ''}">${titleHTML}</p>
          ${desc && !isDraft ? `<p class="a-desc">${esc(desc)}</p>` : ''}
          ${travelHTML}
          ${cost != null ? `<span class="a-cost">${money(cost)}</span>` : ''}
        </div>
      </div>
    </div>`;
  }

  function weatherText(w) {
    if (typeof w === 'string') return w;
    const cond = w.condition || w.summary || w.description || '';
    const temp = w.temp ?? w.temperature ?? w.high;
    return [cond, temp != null ? `${temp}°` : ''].filter(Boolean).join(' · ') || 'Forecast';
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
    await checkApi();
    await restoreSession();
  }
  init();
})();
