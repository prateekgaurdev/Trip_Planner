/* ============================================================
   community-trip.js — Dedicated Full-Page Community Trip Inspector
   Renders complete itinerary, interactive Leaflet map, lodging base,
   structured reviews, upvoting, and AI Community Remix.
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

  const urlParams = new URLSearchParams(window.location.search);
  const tripId = urlParams.get('id');

  if (!tripId) {
    window.location.href = 'community.html';
  }

  // ── Auth Elements
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

  openAuthBtn?.addEventListener('click', () => {
    if (authModal) {
      authModal.hidden = false;
      document.body.classList.add('modal-open');
    }
  });
  authClose?.addEventListener('click', () => {
    if (authModal) {
      authModal.hidden = true;
      document.body.classList.remove('modal-open');
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && authModal) {
      authModal.hidden = true;
      document.body.classList.remove('modal-open');
    }
  });

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

  // ── Load and Render Dedicated Full-Page Trip
  let _currentTrip = null;

  async function loadTripPage() {
    try {
      const trip = await WayfarerAPI.getCommunityTrip(tripId);
      if (!trip) {
        toast('Trip not found.', 'bad');
        window.location.href = 'community.html';
        return;
      }
      _currentTrip = trip;

      const itin = trip.itinerary_data || {};
      const days = itin.days || itin.itinerary || [];
      const lodging = itin.lodging_anchor || itin.lodging || {};
      const corridor = itin.journey_corridor || itin.corridor || {};

      // 1. Populate Hero
      document.title = `${trip.title} · Wayfarer Community`;
      document.getElementById('trip-title').textContent = trip.title;
      document.getElementById('trip-sub').textContent = trip.description || itin.summary || 'A curated multi-day journey with neighborhood-first spatial clustering.';
      document.getElementById('trip-dest-badge').innerHTML = `<i class="fa-solid fa-location-dot"></i> ${esc(trip.destination)}`;
      document.getElementById('trip-duration-badge').innerHTML = `<i class="fa-solid fa-calendar-days"></i> ${trip.duration_days} Days / ${Math.max(1, trip.duration_days - 1)} Nights`;
      document.getElementById('trip-rating-badge').innerHTML = `<span class="star-icon">★</span> ${trip.average_rating.toFixed(1)} (${trip.reviews_count} reviews)`;

      const authorAvatar = trip.author_avatar || `https://ui-avatars.com/api/?name=${encodeURIComponent(trip.author_name)}&background=0d9488&color=fff`;
      document.getElementById('trip-author-avatar').src = authorAvatar;
      document.getElementById('trip-author-name').textContent = trip.author_name;
      document.getElementById('trip-like-count').textContent = trip.likes_count || 0;

      const likeBtn = document.getElementById('trip-like-btn');
      if (likeBtn) {
        likeBtn.classList.toggle('liked', Boolean(trip.user_has_liked));
        likeBtn.onclick = async () => {
          if (!WayfarerAPI.isLoggedIn()) {
            toast('Please sign in to upvote itineraries!', 'warn');
            if (authModal) authModal.hidden = false;
            return;
          }
          try {
            const res = await WayfarerAPI.toggleCommunityLike(trip.id);
            likeBtn.classList.toggle('liked', res.liked);
            document.getElementById('trip-like-count').textContent = res.likes_count;
          } catch (err) {
            toast(err.message || 'Failed to update like.', 'bad');
          }
        };
      }

      // Customize in Studio button
      const forkBtn = document.getElementById('btn-fork-trip');
      if (forkBtn) {
        forkBtn.href = `index.html?destination=${encodeURIComponent(trip.destination)}&budget=${encodeURIComponent(trip.budget || 2000)}&currency=${encodeURIComponent(trip.currency || 'USD')}&travelers=${encodeURIComponent(trip.travelers || 2)}${trip.origin ? '&origin=' + encodeURIComponent(trip.origin) : ''}`;
      }

      // 2. Populate Specs Strip
      const specsBox = document.getElementById('trip-specs-strip');
      if (specsBox) {
        const tagsHtml = (trip.tags || []).map(t => `<span class="comm-tag-badge">${esc(t)}</span>`).join('');
        specsBox.innerHTML = `
          <div class="spec-pod">
            <span class="pod-lbl">DESTINATION</span>
            <strong class="pod-val">${esc(trip.destination)}</strong>
          </div>
          ${trip.origin ? `
            <div class="spec-pod">
              <span class="pod-lbl">CORRIDOR ROUTE</span>
              <strong class="pod-val"><i class="fa-solid fa-plane-departure"></i> ${esc(trip.origin)} &rarr; ${esc(trip.destination)}</strong>
            </div>
          ` : ''}
          <div class="spec-pod">
            <span class="pod-lbl">TRAVELERS &amp; BUDGET</span>
            <strong class="pod-val">${trip.travelers} Travelers &bull; ${trip.budget ? money(trip.budget) + ' ' + esc(trip.currency) : 'Moderate'}</strong>
          </div>
          <div class="spec-pod" style="flex:1;">
            <span class="pod-lbl">THEMES</span>
            <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:4px;">${tagsHtml}</div>
          </div>
        `;
      }

      // 3. Lodging Base Camp Anchor Card
      const lodgingBox = document.getElementById('trip-lodging-box');
      if (lodgingBox) {
        const hotelName = lodging.name || 'Boutique Centrally-Located Hotel Base';
        const hotelArea = lodging.neighborhood || lodging.area || 'Historic Center';
        const hotelReason = lodging.reason || lodging.why_recommended || 'Persistent base camp for daily excursions, eliminating repetitive hotel moves.';
        lodgingBox.innerHTML = `
          <div class="lodging-badge-tag"><i class="fa-solid fa-hotel"></i> Persistent Lodging Anchor (Home Base)</div>
          <div style="display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px;">
            <h4 style="margin:0 0 4px; font-size:1.15rem; color:#0f172a;">${esc(hotelName)}</h4>
            <span style="font-size:0.82rem; font-weight:700; color:#0f766e; background:#f0fdfa; border:1px solid #99f6e4; padding:2px 8px; border-radius:999px;">${esc(hotelArea)}</span>
          </div>
          <p style="margin:6px 0 0; font-size:0.86rem; color:#475569; line-height:1.5;">${esc(hotelReason)}</p>
        `;
      }

      // 4. Populate Days List
      const daysContainer = document.getElementById('trip-days-list');
      if (daysContainer) {
        if (days.length) {
          daysContainer.innerHTML = days.map((d, idx) => {
            const dayNum = d.day_number || idx + 1;
            const cluster = d.neighborhood_cluster || d.theme || '';
            const acts = d.activities || [];

            const actsHtml = acts.map((a) => {
              const timeSlot = a.time || 'Daytime';
              const title = a.title || a.location_name || 'Activity';
              const desc = a.description || '';
              const dist = a.travel_from_prev ? (a.travel_from_prev.duration_mins ? `${a.travel_from_prev.duration_mins} min transit` : '') : '';

              return `
                <div class="day-activity-item">
                  <span class="act-time-pill">${esc(timeSlot)}</span>
                  <div style="flex:1;">
                    <div style="display:flex; justify-content:space-between; align-items:baseline;">
                      <strong style="font-size:0.95rem; color:#0f172a;">${esc(title)}</strong>
                      ${dist ? `<span style="font-size:0.75rem; color:#0f766e; font-weight:600;"><i class="fa-solid fa-person-walking"></i> ${esc(dist)}</span>` : ''}
                    </div>
                    ${desc ? `<p style="margin:4px 0 0; font-size:0.85rem; color:#475569; line-height:1.5;">${esc(desc)}</p>` : ''}
                  </div>
                </div>
              `;
            }).join('');

            return `
              <div class="trip-day-card">
                <div class="trip-day-header">
                  <div style="display:flex; align-items:center; gap:10px;">
                    <span class="day-number-badge">Day ${dayNum}</span>
                    <h4 style="margin:0; font-size:1.1rem; color:#0f172a;">${esc(d.title || 'Sightseeing')}</h4>
                  </div>
                  ${cluster ? `<span class="cluster-pill"><i class="fa-solid fa-map-pin"></i> ${esc(cluster)}</span>` : ''}
                </div>
                <div class="day-activities-list">${actsHtml}</div>
              </div>
            `;
          }).join('');
        } else {
          daysContainer.innerHTML = `<p style="color:#64748b; font-size:0.9rem;">Full itinerary available in the studio.</p>`;
        }
      }

      // 5. Render Interactive Route Map
      const mapData = itin.route_map || itin.map;
      if (mapData && window.WayfarerMaps && document.getElementById('trip-map-container')) {
        try {
          window.WayfarerMaps.destroyAll();
          window.WayfarerMaps.initMap('trip-map-container', mapData);
        } catch (e) {
          console.warn('Map rendering note:', e);
        }
      }

      // 6. Render Reviews & Ratings
      renderReviewsFeed(trip.reviews || []);
      document.getElementById('reviews-count-tag').textContent = `${(trip.reviews || []).length} reviews`;

    } catch (err) {
      toast(`Failed to load trip: ${err.message}`, 'bad');
    }
  }

  function renderReviewsFeed(reviews) {
    const feed = document.getElementById('trip-reviews-feed');
    if (!feed) return;

    if (!reviews.length) {
      feed.innerHTML = `
        <div style="text-align:center; padding:24px 16px; background:#f8fafc; border-radius:12px; border:1px dashed #cbd5e1; color:#64748b;">
          <i class="fa-solid fa-comments" style="font-size:1.6rem; color:#cbd5e1; margin-bottom:8px;"></i>
          <p style="margin:0; font-size:0.88rem;">No reviews yet. Share what you liked and tips to make it wonderful!</p>
        </div>
      `;
      return;
    }

    feed.innerHTML = reviews.map((r) => {
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
          <div class="comm-rev-section" style="background:#f0fdf4; border-left:3px solid #16a34a; padding:8px 12px; border-radius:6px; margin-bottom:8px;">
            <strong style="color:#166534;"><i class="fa-solid fa-thumbs-up"></i> What I Liked:</strong>
            <div style="color:#14532d; font-size:0.84rem; margin-top:2px; line-height:1.45;">${esc(r.liked_aspects)}</div>
          </div>
          <div class="comm-rev-section" style="background:#fffbeb; border-left:3px solid #f59e0b; padding:8px 12px; border-radius:6px;">
            <strong style="color:#b45309;"><i class="fa-solid fa-lightbulb"></i> What to Add:</strong>
            <div style="color:#78350f; font-size:0.84rem; margin-top:2px; line-height:1.45;">${esc(r.suggested_additions)}</div>
          </div>
          ${r.comment ? `<div class="comm-rev-comment">"${esc(r.comment)}"</div>` : ''}
        </div>
      `;
    }).join('');
  }

  // Star Rating Picker
  const starPicker = document.getElementById('star-picker');
  const ratingVal = document.getElementById('form-rating-val');
  starPicker?.querySelectorAll('.star-pick-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const val = parseInt(btn.dataset.val, 10);
      if (ratingVal) ratingVal.value = val;
      starPicker.querySelectorAll('.star-pick-btn').forEach((b) => {
        const bVal = parseInt(b.dataset.val, 10);
        b.classList.toggle('active', bVal <= val);
      });
    });
  });

  // Submit Review Form
  const reviewForm = document.getElementById('trip-review-form');
  reviewForm?.addEventListener('submit', async () => {
    const rating = parseInt(ratingVal?.value || '5', 10);
    const liked = document.getElementById('review-liked-input')?.value.trim();
    const add = document.getElementById('review-add-input')?.value.trim();
    const comment = document.getElementById('review-comment-input')?.value.trim() || '';

    if (!liked || !add) {
      toast('Please share both what you liked and what should be added!', 'warn');
      return;
    }

    try {
      await WayfarerAPI.addCommunityReview(tripId, {
        rating,
        liked_aspects: liked,
        suggested_additions: add,
        comment,
      });
      toast('Review & suggestions posted! Community wisdom updated.', 'ok');
      document.getElementById('review-liked-input').value = '';
      document.getElementById('review-add-input').value = '';
      if (document.getElementById('review-comment-input')) document.getElementById('review-comment-input').value = '';
      await loadTripPage();
    } catch (err) {
      toast(err.message || 'Failed to submit review.', 'bad');
    }
  });

  // AI Community Remix Trigger
  const remixBtn = document.getElementById('btn-trigger-remix');
  const remixBox = document.getElementById('remix-result-display');
  remixBtn?.addEventListener('click', async () => {
    remixBtn.disabled = true;
    remixBtn.innerHTML = `<span class="spinner"></span> Synthesizing Community Wisdom...`;
    const note = document.getElementById('remix-custom-note')?.value.trim() || '';

    try {
      const res = await WayfarerAPI.remixCommunityTrip(tripId, note);
      if (remixBox) {
        remixBox.hidden = false;
        remixBox.innerHTML = `
          <div style="font-weight:700; color:#0f766e; margin-bottom:6px;"><i class="fa-solid fa-sparkles"></i> AI Community Upgrade Generated:</div>
          <div>${esc(res.remixed_summary)}</div>
          ${res.incorporated_suggestions && res.incorporated_suggestions.length ? `
            <div style="margin-top:10px; font-size:0.78rem; color:#475569;">
              <strong>Adopted community recommendations:</strong>
              <ul style="margin:4px 0 0; padding-left:16px;">${res.incorporated_suggestions.map(s => `<li>${esc(s)}</li>`).join('')}</ul>
            </div>
          ` : ''}
        `;
      }
      toast('AI Community Remix complete!', 'ok');
    } catch (err) {
      toast(err.message || 'Remix failed.', 'bad');
    } finally {
      remixBtn.disabled = false;
      remixBtn.innerHTML = `<i class="fa-solid fa-bolt"></i> Remix with Community Wisdom`;
    }
  });

  // Init on load
  updateAuthNav();
  loadTripPage();

})();
