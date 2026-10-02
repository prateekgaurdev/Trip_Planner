/* ============================================================
   community.js — Dedicated Community Hub Controller for Wayfarer.
   Interacts with Supabase PostgreSQL via WayfarerAPI.
   ============================================================ */

(() => {
  'use strict';

  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function money(val) {
    if (val == null || val === '') return '';
    const num = Number(val);
    if (isNaN(num)) return String(val);
    return num.toLocaleString('en-US');
  }

  function toast(msg, type = 'info') {
    const stack = document.getElementById('toast-stack');
    if (!stack) return;
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.setAttribute('role', 'status');
    const icon = type === 'ok' ? 'circle-check' : type === 'warn' ? 'triangle-exclamation' : type === 'bad' ? 'circle-exclamation' : 'circle-info';
    el.innerHTML = `<i class="fa-solid fa-${icon}"></i><span>${esc(msg)}</span>`;
    stack.appendChild(el);
    setTimeout(() => {
      el.classList.add('fade-out');
      setTimeout(() => el.remove(), 260);
    }, 3800);
  }

  // ─── Supabase Authentication ───────────────────────────────────────
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
        loadCommunityFeed();
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
      loadCommunityFeed();
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
      toast('Account created! Welcome to Wayfarer Community.', 'ok');
      loadCommunityFeed();
    } catch (err) {
      if (signupError) {
        signupError.textContent = err.message || 'Registration failed.';
        signupError.hidden = false;
      }
    }
  });

  // ─── Live Community Feed ───────────────────────────────────────────
  const commGrid = document.getElementById('community-trips-grid');
  const searchInput = document.getElementById('community-search-input');
  const sortSelect = document.getElementById('community-sort-select');
  const tagsWrap = document.getElementById('community-tags-wrap');
  const refreshBtn = document.getElementById('refresh-community-btn');

  let _activeTag = '';
  let _searchTimer = null;

  async function loadCommunityFeed() {
    if (!commGrid) return;
    commGrid.innerHTML = `
      <div class="community-empty-notice" style="grid-column:1/-1; text-align:center; padding:40px;">
        <span class="spinner"></span> Loading community journeys from Supabase...
      </div>
    `;

    try {
      const destination = searchInput?.value.trim() || '';
      const sort = sortSelect?.value || 'likes';
      const trips = await WayfarerAPI.getCommunityTrips({ destination, tag: _activeTag, sort });

      if (!trips || !trips.length) {
        commGrid.innerHTML = `
          <div style="grid-column:1/-1; text-align:center; padding:60px 20px; color:var(--ink-soft); background:#ffffff; border-radius:var(--radius); border:1px dashed var(--line);">
            <i class="fa-solid fa-map-location-dot" style="font-size:2.4rem; margin-bottom:14px; color:#cbd5e1;"></i>
            <h3 style="margin:0 0 8px; color:var(--ink);">No itineraries found</h3>
            <p style="margin:0 0 16px;">Try adjusting your search query or explore all destination tags.</p>
            <a href="/" class="btn btn-primary btn-sm"><i class="fa-solid fa-plus"></i> Build the First Itinerary</a>
          </div>
        `;
        return;
      }

      commGrid.innerHTML = trips.map((t) => {
        const tagsHtml = (t.tags || []).map(tg => `<span class="comm-tag-badge">${esc(tg)}</span>`).join('');
        const authorAvatar = t.author_avatar || `https://ui-avatars.com/api/?name=${encodeURIComponent(t.author_name)}&background=0d9488&color=fff`;
        const ratingStars = '★'.repeat(Math.round(t.average_rating || 5));
        const likedClass = t.user_has_liked ? 'liked' : '';
        const corridorHtml = t.origin ? `<span class="comm-chip"><i class="fa-solid fa-plane-departure"></i> ${esc(t.origin)} &rarr;</span>` : '';

        return `
          <article class="comm-trip-card" data-trip-id="${esc(t.id)}">
            <div class="comm-card-header">
              <div class="comm-card-author-row">
                <div class="comm-author-info">
                  <img src="${esc(authorAvatar)}" alt="${esc(t.author_name)}" class="comm-author-img" />
                  <span>${esc(t.author_name)}</span>
                </div>
                <span class="comm-dest-pill"><i class="fa-solid fa-location-dot"></i> ${esc(t.destination)}</span>
              </div>
              <h3 class="comm-card-title">${esc(t.title)}</h3>
              <p class="comm-card-desc">${esc(t.description || 'Curated multi-day itinerary with neighborhood clustering.')}</p>
            </div>
            <div class="comm-card-body">
              <div class="comm-card-meta-chips">
                ${corridorHtml}
                <span class="comm-chip"><i class="fa-solid fa-calendar-days"></i> ${t.duration_days} Days</span>
                <span class="comm-chip"><i class="fa-solid fa-users"></i> ${t.travelers} Travelers</span>
                ${t.budget ? `<span class="comm-chip"><i class="fa-solid fa-coins"></i> ${money(t.budget)} ${esc(t.currency)}</span>` : ''}
              </div>
              <div class="comm-tags-list">${tagsHtml}</div>
            </div>
            <div class="comm-card-footer">
              <div class="comm-stats-left">
                <span class="comm-rating-badge" title="${t.average_rating} out of 5 stars">${ratingStars} <span>${t.average_rating.toFixed(1)}</span></span>
                <span><i class="fa-solid fa-comments"></i> ${t.reviews_count}</span>
              </div>
              <div class="comm-actions-right">
                <button type="button" class="comm-like-btn ${likedClass}" data-trip-id="${esc(t.id)}" title="Upvote this trip">
                  <i class="fa-solid fa-heart"></i> <span class="like-cnt">${t.likes_count}</span>
                </button>
                <button type="button" class="btn btn-primary btn-sm btn-open-comm-trip" data-trip-id="${esc(t.id)}">View &amp; Review</button>
              </div>
            </div>
          </article>
        `;
      }).join('');

      // Bind Card Actions
      commGrid.querySelectorAll('.comm-like-btn').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!WayfarerAPI.isLoggedIn()) {
            toast('Please sign in to upvote community itineraries!', 'warn');
            if (authModal) authModal.hidden = false;
            return;
          }
          const tripId = btn.dataset.tripId;
          try {
            const res = await WayfarerAPI.toggleCommunityLike(tripId);
            btn.classList.toggle('liked', res.liked);
            const cntSpan = btn.querySelector('.like-cnt');
            if (cntSpan) cntSpan.textContent = res.likes_count;
          } catch (err) {
            toast(err.message || 'Failed to update like.', 'bad');
          }
        });
      });

      commGrid.querySelectorAll('.btn-open-comm-trip').forEach((btn) => {
        btn.addEventListener('click', () => {
          openCommunityTripModal(btn.dataset.tripId);
        });
      });
    } catch (err) {
      commGrid.innerHTML = `<div style="grid-column:1/-1; text-align:center; padding:40px; color:var(--bad);">Failed to load community feed: ${esc(err.message)}</div>`;
    }
  }

  searchInput?.addEventListener('input', () => {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(loadCommunityFeed, 300);
  });

  sortSelect?.addEventListener('change', loadCommunityFeed);
  refreshBtn?.addEventListener('click', loadCommunityFeed);

  tagsWrap?.querySelectorAll('.comm-filter-tag').forEach((btn) => {
    btn.addEventListener('click', () => {
      tagsWrap.querySelectorAll('.comm-filter-tag').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _activeTag = btn.dataset.tag || '';
      loadCommunityFeed();
    });
  });

  // ─── Community Detail & Structured Review Modal ───────────────────
  const commModal = document.getElementById('community-detail-modal');
  const commClose = document.getElementById('comm-detail-close');
  const commHead = document.getElementById('comm-detail-head');
  const commItin = document.getElementById('comm-detail-itinerary');
  const commRevs = document.getElementById('comm-reviews-feed');
  const reviewForm = document.getElementById('comm-review-form');
  const remixBtn = document.getElementById('comm-remix-btn');
  const remixResult = document.getElementById('comm-remix-result');
  let _activeModalTripId = null;

  commClose?.addEventListener('click', () => { if (commModal) commModal.hidden = true; });

  async function openCommunityTripModal(tripId) {
    _activeModalTripId = tripId;
    if (commModal) commModal.hidden = false;
    if (remixResult) { remixResult.hidden = true; remixResult.innerHTML = ''; }
    if (commHead) commHead.innerHTML = '<div style="padding:20px; text-align:center;"><span class="spinner"></span> Loading itinerary...</div>';
    if (commItin) commItin.innerHTML = '';
    if (commRevs) commRevs.innerHTML = '';

    try {
      const trip = await WayfarerAPI.getCommunityTrip(tripId);
      if (!trip) return;

      const itin = trip.itinerary_data || {};
      const days = itin.days || itin.itinerary || [];
      const authorAvatar = trip.author_avatar || `https://ui-avatars.com/api/?name=${encodeURIComponent(trip.author_name)}&background=0d9488&color=fff`;

      if (commHead) {
        commHead.innerHTML = `
          <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:12px;">
            <div>
              <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
                <img src="${esc(authorAvatar)}" alt="" style="width:34px; height:34px; border-radius:50%; object-fit:cover;" />
                <div>
                  <div style="font-weight:700; color:var(--ink);">${esc(trip.author_name)}</div>
                  <div style="font-size:0.78rem; color:var(--ink-soft);">&bull; Published on Wayfarer &bull; ${trip.destination}</div>
                </div>
              </div>
              <h2 style="margin:4px 0 6px; font-size:1.5rem; color:var(--ink); line-height:1.25;">${esc(trip.title)}</h2>
              <div style="font-size:0.9rem; color:var(--ink-soft); max-width:680px; line-height:1.45;">${esc(trip.description || '')}</div>
            </div>
            <div style="text-align:right;">
              <span class="comm-rating-badge" style="font-size:1.2rem; font-weight:800;"><i class="fa-solid fa-star"></i> ${trip.average_rating.toFixed(1)}</span>
              <div style="font-size:0.8rem; color:var(--ink-soft); margin-top:4px;">Based on ${trip.reviews_count} reviews</div>
            </div>
          </div>
        `;
      }

      // Render days
      if (commItin) {
        let itinHtml = `<div class="comm-itin-wrap"><h4 style="margin:0 0 14px; color:var(--ink);"><i class="fa-solid fa-route"></i> Day-by-Day Journey Schedule</h4>`;
        if (days.length) {
          days.forEach((d, idx) => {
            const acts = d.activities || [];
            const actsHtml = acts.map(a => `
              <div style="display:flex; gap:12px; margin-bottom:8px; font-size:0.85rem; padding:8px 10px; background:#f8fafc; border-radius:6px; border:1px solid #f1f5f9;">
                <span style="font-weight:700; color:var(--brand); min-width:55px;">${esc(a.time || 'Day')}</span>
                <div>
                  <strong style="color:var(--ink);">${esc(a.title || a.location_name)}</strong>
                  ${a.description ? `<p style="margin:2px 0 0; font-size:0.8rem; color:var(--ink-soft);">${esc(a.description)}</p>` : ''}
                </div>
              </div>
            `).join('');

            itinHtml += `
              <div style="margin-bottom:16px; border:1px solid var(--line); border-radius:var(--radius-sm); padding:12px 14px; background:#ffffff;">
                <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                  <strong style="color:var(--ink); font-size:0.95rem;">Day ${d.day_number || idx + 1}: ${esc(d.title || 'Sightseeing')}</strong>
                  ${d.neighborhood_cluster ? `<span style="font-size:0.75rem; background:#f0fdfa; color:#0f766e; padding:2px 6px; border-radius:4px; font-weight:600;">${esc(d.neighborhood_cluster)}</span>` : ''}
                </div>
                ${actsHtml}
              </div>
            `;
          });
        } else {
          itinHtml += `<p style="color:var(--ink-soft); font-size:0.85rem;">Detailed schedule available upon remixing.</p>`;
        }
        itinHtml += `</div>`;
        commItin.innerHTML = itinHtml;
      }

      renderReviewsList(trip.reviews || []);
    } catch (err) {
      if (commHead) commHead.innerHTML = `<div style="color:var(--bad);">Error loading trip: ${esc(err.message)}</div>`;
    }
  }

  function renderReviewsList(reviews) {
    if (!commRevs) return;
    if (!reviews.length) {
      commRevs.innerHTML = `<p style="font-size:0.85rem; color:var(--ink-soft); text-align:center; padding:16px; background:#ffffff; border-radius:6px; border:1px dashed var(--line);">No reviews yet. Be the first to share pro tips!</p>`;
      return;
    }
    commRevs.innerHTML = reviews.map((r) => {
      const stars = '★'.repeat(r.rating) + '☆'.repeat(5 - r.rating);
      const userAvatar = r.user_avatar || `https://ui-avatars.com/api/?name=${encodeURIComponent(r.user_name)}&background=0284c7&color=fff`;

      return `
        <div class="comm-review-item">
          <div class="comm-rev-head">
            <div class="comm-rev-user">
              <img src="${esc(userAvatar)}" alt="" />
              <span>${esc(r.user_name)}</span>
            </div>
            <span class="comm-rev-stars">${stars}</span>
          </div>
          <div class="comm-rev-section" style="background:#f0fdf4; border-left:3px solid #16a34a; padding:6px 10px; border-radius:4px; margin-bottom:6px;">
            <strong style="color:#166534;"><i class="fa-solid fa-thumbs-up"></i> What I Liked:</strong>
            <div style="color:#14532d; font-size:0.82rem; margin-top:2px;">${esc(r.liked_aspects)}</div>
          </div>
          <div class="comm-rev-section" style="background:#fffbeb; border-left:3px solid #f59e0b; padding:6px 10px; border-radius:4px;">
            <strong style="color:#b45309;"><i class="fa-solid fa-lightbulb"></i> What to Add:</strong>
            <div style="color:#78350f; font-size:0.82rem; margin-top:2px;">${esc(r.suggested_additions)}</div>
          </div>
          ${r.comment ? `<div class="comm-rev-comment">"${esc(r.comment)}"</div>` : ''}
        </div>
      `;
    }).join('');
  }

  // Star rating picker
  const starContainer = document.getElementById('review-star-rating');
  const ratingInput = document.getElementById('review-rating-val');
  starContainer?.querySelectorAll('.star-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const val = parseInt(btn.dataset.val, 10);
      if (ratingInput) ratingInput.value = val;
      starContainer.querySelectorAll('.star-btn').forEach((b) => {
        const bVal = parseInt(b.dataset.val, 10);
        b.classList.toggle('active', bVal <= val);
      });
    });
  });

  // Review submit
  reviewForm?.addEventListener('submit', async () => {
    if (!_activeModalTripId) return;
    const rating = parseInt(ratingInput?.value || '5', 10);
    const liked_aspects = document.getElementById('review-liked')?.value.trim();
    const suggested_additions = document.getElementById('review-add')?.value.trim();
    const comment = document.getElementById('review-comment')?.value.trim() || '';

    if (!liked_aspects || !suggested_additions) {
      toast('Please share both what you liked and what should be added!', 'warn');
      return;
    }

    try {
      await WayfarerAPI.addCommunityReview(_activeModalTripId, {
        rating,
        liked_aspects,
        suggested_additions,
        comment,
      });
      toast('Review & suggestions posted! Community wisdom updated.', 'ok');
      document.getElementById('review-liked').value = '';
      document.getElementById('review-add').value = '';
      if (document.getElementById('review-comment')) document.getElementById('review-comment').value = '';
      openCommunityTripModal(_activeModalTripId);
      loadCommunityFeed();
    } catch (err) {
      toast(err.message || 'Failed to submit review.', 'bad');
    }
  });

  // AI Remix trigger
  remixBtn?.addEventListener('click', async () => {
    if (!_activeModalTripId) return;
    remixBtn.disabled = true;
    remixBtn.innerHTML = `<span class="spinner"></span> Synthesizing Community Tips...`;
    try {
      const res = await WayfarerAPI.remixCommunityTrip(_activeModalTripId);
      if (remixResult) {
        remixResult.hidden = false;
        remixResult.innerHTML = `
          <div style="font-weight:700; color:#0f766e; margin-bottom:6px;"><i class="fa-solid fa-wand-magic-sparkles"></i> AI Community Upgrade Generated:</div>
          <div>${esc(res.remixed_summary)}</div>
          ${res.incorporated_suggestions && res.incorporated_suggestions.length ? `
            <div style="margin-top:8px; font-size:0.78rem; color:var(--ink-soft);">
              <strong>Adopted community recommendations:</strong>
              <ul style="margin:4px 0 0; padding-left:16px;">${res.incorporated_suggestions.map(s => `<li>${esc(s)}</li>`).join('')}</ul>
            </div>
          ` : ''}
        `;
      }
      toast('AI Community Remix completed!', 'ok');
    } catch (err) {
      toast(err.message || 'Remix failed.', 'bad');
    } finally {
      remixBtn.disabled = false;
      remixBtn.innerHTML = `<i class="fa-solid fa-bolt"></i> Remix with Community Wisdom`;
    }
  });

  // Initialize on load
  updateAuthNav();
  loadCommunityFeed();

})();
