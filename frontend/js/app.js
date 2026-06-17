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
  const state = {
    planId: null,
    status: null,
    interests: ['food', 'culture'],
    polling: false,
    pollTimer: null,
  };

  const STEPS = ['researching', 'planning', 'awaiting_review', 'completed'];
  const STEP_LABEL = {
    researching: 'Research agent is gathering context…',
    planning: 'Planner agent is drafting your itinerary…',
    awaiting_review: 'Paused — waiting for your review.',
    completed: 'Plan finalized.',
  };

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

    draftArea: $('#draft-area'),
    reviewGate: $('#review-gate'),
    feedbackBox: $('#feedback-box'),
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
    const symbol = { 'USD': '$', 'EUR': '€', 'GBP': '£', 'JPY': '¥', 'AUD': '$', 'CAD': '$' }[currency] || currency + ' ';
    return symbol + Number(n || 0).toLocaleString('en-US', { maximumFractionDigits: 0 });
  };

  // ─── Interests chips ────────────────────────────────────
  function renderChips() {
    els.interestChips.innerHTML = state.interests.map((it, i) =>
      `<span class="chip">${esc(it)}<button type="button" data-i="${i}" aria-label="Remove ${esc(it)}"><i class="fa-solid fa-xmark"></i></button></span>`
    ).join('');
    els.interestChips.querySelectorAll('button').forEach((b) =>
      b.addEventListener('click', () => { state.interests.splice(+b.dataset.i, 1); renderChips(); })
    );
  }
  function addInterest(val) {
    const v = val.trim().toLowerCase();
    if (v && !state.interests.includes(v)) { state.interests.push(v); renderChips(); }
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

  function showWorking(status) {
    const busy = status === 'researching' || status === 'planning' || !status;
    els.workingBanner.style.display = busy ? 'flex' : 'none';
    els.workingText.textContent = STEP_LABEL[status] || 'Agents are working…';
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

    // client-side validation mirroring schemas.py
    if (destination.length < 2) return formErr('Please enter a destination (at least 2 characters).');
    if (!start_date || !end_date) return formErr('Please choose both start and end dates.');
    if (new Date(end_date) < new Date(start_date)) return formErr('End date must be on or after the start date.');
    if (!(budget > 0)) return formErr('Budget must be greater than 0.');
    if (!(travelers >= 1 && travelers <= 20)) return formErr('Travelers must be between 1 and 20.');

    const payload = { destination, start_date, end_date, budget, currency, travelers, interests: state.interests.slice() };

    setSubmitting(true);
    try {
      const res = await WayfarerAPI.createPlan(payload);
      state.planId = res && (res.plan_id || res.id || res.planId);
      if (!state.planId) throw new Error('No plan id returned by the server.');

      openWorkspace(destination, payload);
      toast('Plan created — agents are working.', 'ok');
      startPolling();
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

  function openWorkspace(destination, payload) {
    els.workspace.hidden = false;
    els.wsDestination.textContent = destination;
    const nights = Math.max(0, Math.round((new Date(payload.end_date) - new Date(payload.start_date)) / 864e5));
    els.wsMeta.textContent = `${payload.start_date} → ${payload.end_date} · ${nights} night${nights === 1 ? '' : 's'} · ${payload.travelers} traveler${payload.travelers === 1 ? '' : 's'} · ${money(payload.budget, payload.currency)}`;
    els.draftArea.hidden = true;
    els.reviewGate.hidden = true;
    els.itinerary.innerHTML = '';
    setPipeline('researching');
    showWorking('researching');
    els.workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ─── Polling state machine ──────────────────────────────
  function startPolling() {
    stopPolling();
    state.polling = true;
    poll();
  }
  function stopPolling() {
    state.polling = false;
    if (state.pollTimer) clearTimeout(state.pollTimer);
  }

  async function poll() {
    if (!state.polling || !state.planId) return;
    try {
      const data = await WayfarerAPI.getPlan(state.planId);
      handleState(data);
    } catch (err) {
      // transient errors: keep trying a few times silently
      console.warn('poll error', err);
    }
    if (state.polling && state.status !== 'completed') {
      state.pollTimer = setTimeout(poll, 1800);
    }
  }

  function handleState(data) {
    if (!data) return;
    const status = data.status || data.plan_status;
    state.status = status;
    setPipeline(status);
    showWorking(status);

    if (status === 'awaiting_review') {
      stopPolling();
      renderDraft(data);
      showReviewGate(true);
    } else if (status === 'completed') {
      stopPolling();
      loadFinal();
    } else {
      // researching / planning — keep gate hidden
      showReviewGate(false);
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
    }
  }

  els.reviewGate.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const action = btn.dataset.action;
      if (action === 'modify') { els.feedbackBox.hidden = false; els.feedback.focus(); return; }
      submitReview(action);
    });
  });
  els.cancelFeedback.addEventListener('click', () => { els.feedbackBox.hidden = true; });
  els.sendFeedback.addEventListener('click', () => {
    const fb = els.feedback.value.trim();
    if (!fb) { toast('Add a note so the planner knows what to change.', 'warn'); return; }
    submitReview('modify', fb);
  });

  async function submitReview(action, feedback) {
    showReviewGate(false);
    els.workingBanner.style.display = 'flex';
    const verb = { approve: 'Approving', reject: 'Rejecting & restarting', modify: 'Sending changes to the planner' }[action];
    els.workingText.textContent = `${verb}…`;
    setPipeline(action === 'approve' ? 'completed' : action === 'modify' ? 'planning' : 'researching');
    try {
      await WayfarerAPI.review(state.planId, action, feedback);
      toast(`Review submitted: ${action}.`, 'ok');
      startPolling();
    } catch (err) {
      toast(err.message || 'Review failed.', 'bad');
      showReviewGate(true);
    }
  }

  // ─── Final plan ─────────────────────────────────────────
  async function loadFinal() {
    setPipeline('completed');
    els.workingBanner.style.display = 'none';
    try {
      const data = await WayfarerAPI.getFinal(state.planId);
      renderFinal(data);
      toast('Your itinerary is finalized! 🎉', 'ok');
    } catch (err) {
      // final may not be ready instantly; retry once
      setTimeout(async () => {
        try { renderFinal(await WayfarerAPI.getFinal(state.planId)); }
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

    // Budget
    const budget = plan.budget || plan.budget_breakdown || plan.budget_allocation || plan.allocation;
    if (budget && typeof budget === 'object') parts.push(renderBudget(budget, plan));

    // Days / itinerary
    const days = findDays(plan);
    if (days.length) {
      days.forEach((d, i) => parts.push(renderDay(d, i, dest)));
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
      parts.push(renderNote('<i class="fa-solid fa-suitcase"></i> Recommended Packing List', plan.packing_list));
    }
    if (plan.tips && plan.tips.length) {
      parts.push(renderNote('<i class="fa-solid fa-lightbulb"></i> Travel Tips', plan.tips));
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
    if (plan.start_date && plan.end_date) stats.push(['Dates', `${plan.start_date} → ${plan.end_date}`]);
    if (!stats.length) stats.push(['Status', 'Draft ready']);
    return stats.map(([k, v]) => `<div class="sum-stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join('');
  }

  function renderBudget(budget, plan) {
    let rows = [];
    const currency = plan.currency || budget.currency || 'USD';
    
    if (Array.isArray(budget.items)) {
      rows = budget.items.map((it) => [it.category || it.name || it.label, Number(it.amount ?? it.value ?? it.cost ?? 0)]);
    } else {
      rows = Object.entries(budget)
        .filter(([k, v]) => typeof v === 'number' && !['total', 'budget_usd', 'budget_total', 'total_budget'].includes(k))
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

  function renderDay(day, i, destination) {
    const num = day.day ?? day.day_number ?? day.index ?? (i + 1);
    const title = day.title || day.theme || day.name || `Day ${num}`;
    const date = day.date || day.day_date || '';
    const weather = day.weather || day.forecast;
    const acts = day.activities || day.items || day.schedule || day.plan || [];

    const weatherHTML = weather ? `<span class="day-weather"><i class="fa-solid fa-cloud-sun"></i> ${esc(weatherText(weather))}</span>` : '';
    const actsHTML = Array.isArray(acts) && acts.length
      ? acts.map(a => renderActivity(a, destination)).join('')
      : (typeof day.description === 'string' ? `<p class="a-desc" style="padding:12px 0">${esc(day.description)}</p>` : '<p class="a-desc" style="padding:12px 0">No activities listed.</p>');

    // If day has a description, show it above the activities
    const dayDesc = day.description && Array.isArray(acts) && acts.length ? `<p class="d-desc" style="color:var(--ink-soft); font-size: 0.95rem; margin-bottom: 12px;">${esc(day.description)}</p>` : '';

    return `<div class="day-card">
      <div class="day-head">
        <span class="d-title"><span class="day-num">${esc(num)}</span><span><h4>${esc(title)}</h4>${date ? `<span class="d-date">${esc(date)}</span>` : ''}</span></span>
        ${weatherHTML}
      </div>
      <div class="day-body">
        ${dayDesc}
        ${actsHTML}
      </div>
    </div>`;
  }

  function renderActivity(a, destination) {
    if (typeof a === 'string') return `<div class="activity"><div class="a-body"><div class="a-text"><p class="a-title">${esc(a)}</p></div></div></div>`;
    const time = a.time || a.start || a.when || a.period || '';
    const title = a.title || a.name || a.activity || a.label || '';
    const desc = a.description || a.detail || a.notes || '';
    const cost = a.cost ?? a.price ?? a.amount;
    
    // Support image_keyword from our finalize node
    const imageKeyword = a.image_keyword || a.image || a.keyword;
    const destName = typeof destination === 'string' ? destination.split(',')[0] : '';
    
    // Fallback to title if no explicit keyword
    const searchKeyword = imageKeyword || title;
    
    // Only show images if there is a description (i.e. it's the finalized plan, not the short draft)
    const imgHTML = (desc && searchKeyword) ? 
        `<div class="a-image"><img src="https://loremflickr.com/600/400/${encodeURIComponent(destName + ',' + searchKeyword)}?lock=${Math.floor(Math.random() * 1000)}" alt="${esc(searchKeyword)}" loading="lazy" onerror="this.parentElement.style.display='none'"/></div>` : '';

    return `<div class="activity">
      ${time ? `<span class="a-time">${esc(time)}</span>` : ''}
      <div class="a-body">
        <div class="a-text">
          <p class="a-title">${esc(title)}</p>
          ${desc ? `<p class="a-desc">${esc(desc)}</p>` : ''}
          ${cost != null ? `<span class="a-cost">${money(cost)}</span>` : ''}
        </div>
        ${imgHTML}
      </div>
    </div>`;
  }

  function weatherText(w) {
    if (typeof w === 'string') return w;
    const cond = w.condition || w.summary || w.description || '';
    const temp = w.temp ?? w.temperature ?? w.high;
    return [cond, temp != null ? `${temp}°` : ''].filter(Boolean).join(' · ') || 'Forecast';
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
    state.planId = null; state.status = null;
    els.workspace.hidden = true;
    els.draftArea.hidden = true;
    els.itinerary.innerHTML = '';
    document.getElementById('planner').scrollIntoView({ behavior: 'smooth' });
  });

  // ─── Init ───────────────────────────────────────────────
  function init() {
    renderChips();
    // sensible default dates: 30 days out, 4-night trip
    const start = new Date(Date.now() + 30 * 864e5);
    const end = new Date(start.getTime() + 4 * 864e5);
    const iso = (d) => d.toISOString().slice(0, 10);
    els.form.start_date.value = iso(start);
    els.form.end_date.value = iso(end);
    checkApi();
  }
  init();
})();
