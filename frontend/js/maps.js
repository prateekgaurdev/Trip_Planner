/* ============================================================
   maps.js — Interactive Day-Wise Travel Map & Sequence Roadmap
   Matches the Freepik visual travel map aesthetic (Image 3):
   • Custom speech-bubble callout pins (Times, Titles, Badges)
   • Sequential A ➔ B ➔ C landmark pins
   • Dotted connector paths (Hotel to events)
   • Day-wise pagination: Day 1 active by default
   • Smart neighborhood zoom (maxZoom 17 for tight clusters)
   • Synchronized day sequence with "Show more" for All Days
   ============================================================ */

(() => {
  'use strict';

  const _maps = [];
  let _activeRouteMap = null;
  let _currentDayFilter = 1; // Default to Day 1 by user requirement
  let _allDaysExpanded = false;
  let _cartoApiKey = '';

  function setApiKey(key) {
    _cartoApiKey = key || '';
  }

  const PIN_PALETTES = [
    { main: '#2563eb', bg: '#eff6ff', border: '#93c5fd', text: '#1d4ed8' }, // Ocean Blue
    { main: '#059669', bg: '#ecfdf5', border: '#6ee7b7', text: '#047857' }, // Emerald Green
    { main: '#e11d48', bg: '#fff1f2', border: '#fda4af', text: '#be123c' }, // Rose Pink
    { main: '#d97706', bg: '#fffbeb', border: '#fcd34d', text: '#b45309' }, // Amber Yellow
    { main: '#7c3aed', bg: '#f5f3ff', border: '#c4b5fd', text: '#6d28d9' }, // Purple
    { main: '#0891b2', bg: '#ecfeff', border: '#67e8f9', text: '#0e7490' }, // Cyan
  ];

  const HOTEL_PALETTE = {
    main: '#4338ca',
    bg: '#eef2ff',
    border: '#a5b4fc',
    text: '#3730a3',
  };

  function destroyAll() {
    _maps.forEach((m) => {
      try {
        m.remove();
      } catch (_) {}
    });
    _maps.length = 0;
  }

  function formatDuration(minutes) {
    const m = Number(minutes) || 0;
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60);
    const r = m % 60;
    return r ? `${h}h ${r}m` : `${h}h`;
  }

  function modeIcon(mode) {
    return mode === 'driving' ? 'fa-car' : 'fa-person-walking';
  }

  const esc = (s) =>
    String(s ?? '').replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );

  let _activeMarkerObjects = [];

  function getStopLetter(index, totalMarkers, isLodging) {
    const isStart = index === 0;
    const isEnd = index === totalMarkers - 1 && totalMarkers > 1;
    if (isLodging || isStart || isEnd) {
      return {
        isHotel: true,
        letter: 'HOTEL',
        symbolHtml: '<i class="fa-solid fa-hotel"></i>',
        tagText: isStart ? 'START' : (isEnd ? 'RETURN' : 'HOTEL'),
        badgeText: 'HOTEL',
      };
    }
    // Activity stops start from 'A' at index 1
    const charCode = 65 + Math.max(0, index - 1);
    const letter = String.fromCharCode(charCode);
    return {
      isHotel: false,
      letter: letter,
      symbolHtml: letter,
      tagText: `STOP ${letter}`,
      badgeText: letter,
    };
  }

  function formatTime12(timeStr) {
    if (!timeStr) return '';
    const raw = String(timeStr).trim();
    if (raw.toLowerCase().includes('am') || raw.toLowerCase().includes('pm')) {
      return raw;
    }
    const m = raw.match(/(\d{1,2}):(\d{2})/);
    if (!m) return raw;
    let h = parseInt(m[1], 10);
    const min = m[2];
    const ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12;
    h = h ? h : 12;
    return `${h}:${min} ${ampm}`;
  }

  function createCalloutIcon(m, index, isLodging, totalMarkers, dayColor) {
    const stopInfo = getStopLetter(index, totalMarkers, isLodging || m.is_lodging);
    const symbol = stopInfo.symbolHtml;

    const palette = (stopInfo.isHotel || m.is_lodging)
      ? HOTEL_PALETTE
      : PIN_PALETTES[Math.max(0, index - 1) % PIN_PALETTES.length] || PIN_PALETTES[0];

    const rawTitle = m.title || m.name || m.label || 'Stop';
    let shortTitle = rawTitle;
    if (shortTitle.length > 22) {
      shortTitle = shortTitle.slice(0, 20).trim() + '…';
    }

    const timeStr = m.time ? formatTime12(m.time) : '';
    const badgeTag = stopInfo.tagText;

    const html = `
      <div class="wayfarer-map-callout ${stopInfo.isHotel ? 'is-lodging' : ''}" style="--pin-color:${palette.main}; --pin-bg:${palette.bg}; --pin-border:${palette.border}; --pin-text:${palette.text};">
        <div class="callout-bubble">
          <div class="bubble-row">
            ${timeStr ? `<span class="bubble-time"><i class="fa-regular fa-clock"></i> ${timeStr}</span>` : ''}
            <span class="bubble-tag">${badgeTag}</span>
          </div>
          <div class="bubble-title">${esc(shortTitle)}</div>
        </div>
        <div class="callout-needle"></div>
        <div class="callout-anchor">
          <span>${symbol}</span>
        </div>
      </div>
    `;

    return L.divIcon({
      className: 'wayfarer-pin-wrapper',
      html: html,
      iconSize: [140, 68],
      iconAnchor: [70, 66],
      popupAnchor: [0, -68],
    });
  }

  function initMap(el, routeMap, options = {}) {
    if (!window.L || !el || !routeMap?.available) return null;

    _activeRouteMap = routeMap;
    const center = routeMap.destination_center;
    const map = L.map(el, { scrollWheelZoom: false, zoomControl: true });

    // 1. High-clarity OpenStreetMap (Default - Clean, zero watermark, full detail)
    const osmTiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    });

    // 2. Official Carto Voyager spec (direct basemaps.cartocdn.com)
    const cartoUrl = _cartoApiKey 
      ? `https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=${encodeURIComponent(_cartoApiKey)}`
      : 'https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png';

    const voyagerTiles = L.tileLayer(cartoUrl, {
      attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; OpenStreetMap contributors',
      maxZoom: 19,
    });

    // 3. ESRI World Imagery / Satellite layer
    const satelliteTiles = L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      {
        attribution: 'Tiles &copy; Esri &mdash; Earthstar Geographics',
        maxZoom: 19,
      }
    );

    // Default to OpenStreetMap so the real map (roads, rivers, landmarks) is 100% visible immediately without watermarks!
    osmTiles.addTo(map);

    // Add layer control so user can toggle between clean Street Map, Carto Voyager, and Satellite
    L.control.layers(
      {
        '🗺️ Street Map': osmTiles,
        '🎨 Carto Voyager': voyagerTiles,
        '🛰️ Satellite': satelliteTiles,
      },
      null,
      { position: 'topright', collapsed: true }
    ).addTo(map);

    let bounds = [];
    const filterDay = options.dayFilter != null ? options.dayFilter : _currentDayFilter;
    const days = filterDay != null
      ? routeMap.days.filter((d) => d.day_number === filterDay)
      : routeMap.days;

    const markerObjects = [];

    days.forEach((day, dayIdx) => {
      const markersToPlot = day.markers || [];
      const dayColor = day.color || PIN_PALETTES[dayIdx % PIN_PALETTES.length].main;

    markersToPlot.forEach((m, idx) => {
      const isLodging = m.is_lodging || (idx === 0 && markersToPlot.length > 2) || (idx === markersToPlot.length - 1 && markersToPlot.length > 2);
      const icon = createCalloutIcon(m, idx, isLodging, markersToPlot.length, dayColor);
      const stopInfo = getStopLetter(idx, markersToPlot.length, isLodging);
      const stepId = `step-${day.day_number}-${idx}`;

      const marker = L.marker([m.lat, m.lon], { icon: icon }).addTo(map);

      const destName = routeMap.destination_center?.name || '';
      const searchQ = encodeURIComponent(`${m.title || m.name} ${destName}`);
      const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${searchQ}`;

      // Leg from previous stop
      const prevLeg = idx > 0 && day.legs ? day.legs[idx - 1] : null;
      let legInfoHtml = '';
      if (prevLeg && (prevLeg.distance_km || prevLeg.duration_minutes)) {
        const mIcon = modeIcon(prevLeg.mode);
        const durStr = formatDuration(prevLeg.duration_minutes);
        legInfoHtml = `
          <div class="popup-travel-from">
            <i class="fa-solid ${mIcon}"></i>
            <span><strong>${prevLeg.distance_km} km</strong> (${durStr}) from previous stop</span>
          </div>
        `;
      }

      const formattedTime = m.time ? formatTime12(m.time) : '';

      const popupContent = `
        <div class="map-popup-card">
          <div class="popup-head">
            <span class="popup-pill" style="background:${dayColor}; color:#ffffff;">${stopInfo.tagText} &bull; DAY ${day.day_number}</span>
            <h4>${esc(m.title || m.name)}</h4>
            ${destName ? `<span class="popup-loc-sub"><i class="fa-solid fa-location-dot"></i> ${esc(destName)}</span>` : ''}
          </div>
          <div class="popup-body">
            ${formattedTime ? `<div class="popup-time"><i class="fa-regular fa-clock"></i> <span>Scheduled: <strong>${esc(formattedTime)}</strong></span></div>` : ''}
            ${legInfoHtml}
            <div class="popup-actions-row">
              <a href="${mapsUrl}" target="_blank" rel="noopener noreferrer" class="popup-maps-link">
                <i class="fa-solid fa-diamond-turn-right"></i> Directions &bull; Google Maps
              </a>
            </div>
          </div>
        </div>
      `;

      marker.bindPopup(popupContent, { maxWidth: 300, className: 'wayfarer-leaflet-popup' });

      // Bi-directional link: clicking marker or opening popup highlights matching step in arrow flow
      marker.on('click', () => {
        highlightSequenceStep(stepId);
      });
      marker.on('popupopen', () => {
        highlightSequenceStep(stepId);
      });

      bounds.push([m.lat, m.lon]);
      markerObjects.push({ marker, data: m, dayNumber: day.day_number, order: m.order, stepId: stepId });
    });

      // Render dotted sequential path connecting hotel to events (like Image 3)
      const coords = markersToPlot.map((m) => [m.lat, m.lon]);
      if (coords.length >= 2) {
        L.polyline(coords, {
          color: dayColor,
          weight: 3.5,
          opacity: 0.88,
          dashArray: '5, 8',
          lineCap: 'round',
          lineJoin: 'round',
        }).addTo(map);
      }
    });

    // Smart neighborhood zoom calculation (zooms in closely for tight areas like Image 3)
    if (bounds.length) {
      const validBounds = bounds.filter(
        (b) => Math.abs(b[0]) <= 90 && Math.abs(b[1]) <= 180
      );
      if (validBounds.length) {
        // Calculate spread
        const lats = validBounds.map((b) => b[0]);
        const lons = validBounds.map((b) => b[1]);
        const latSpan = Math.max(...lats) - Math.min(...lats);
        const lonSpan = Math.max(...lons) - Math.min(...lons);
        const isTightCluster = latSpan < 0.05 && lonSpan < 0.05;

        // If tight neighborhood cluster (e.g. Rishikesh walking area), zoom in generously (maxZoom 16-17)
        const targetMaxZoom = isTightCluster ? 16 : 14;
        map.fitBounds(validBounds, { padding: [60, 60], maxZoom: targetMaxZoom });
      } else if (center) {
        map.setView([center.lat, center.lon], 14);
      }
    } else if (center) {
      map.setView([center.lat, center.lon], 14);
    }

    _activeMarkerObjects = markerObjects;
    _maps.push(map);
    setTimeout(() => map.invalidateSize(), 150);

    // Bind interactive sequence timeline clicks
    bindSequenceClicks(map, markerObjects);

    return map;
  }

  function highlightSequenceStep(stepId) {
    if (!stepId) return;
    document.querySelectorAll('.sequence-step').forEach((s) => s.classList.remove('active-step'));
    const stepEl = document.querySelector(`.sequence-step[data-step-id="${stepId}"]`);
    if (stepEl) {
      stepEl.classList.add('active-step');
      stepEl.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    }
  }

  function bindSequenceClicks(map, markerObjects) {
    const list = (markerObjects && markerObjects.length) ? markerObjects : _activeMarkerObjects;
    document.querySelectorAll('.sequence-step[data-step-id]').forEach((step) => {
      step.onclick = () => {
        const stepId = step.dataset.stepId;
        const lat = parseFloat(step.dataset.lat);
        const lon = parseFloat(step.dataset.lon);

        highlightSequenceStep(stepId);

        if (!isNaN(lat) && !isNaN(lon) && map) {
          map.panTo([lat, lon], { animate: true, duration: 0.5 });
          const match = list.find((o) => o.stepId === stepId);
          if (match && match.marker) {
            setTimeout(() => {
              match.marker.openPopup();
            }, 250);
          }
        }
      };
    });
  }

  function renderSequenceFlow(routeMap, activeDay, showAllExpanded = false) {
    const days = routeMap.days || [];
    if (!days.length) return '';

    let daysToRender = days;
    let hasMoreDays = false;

    if (activeDay != null) {
      // Day-wise pagination: show only the active day
      daysToRender = days.filter((d) => d.day_number === activeDay);
    } else if (!showAllExpanded && days.length > 1) {
      // "All Days" clicked: show Day 1 first with "Show more" button for remaining days
      daysToRender = [days[0]];
      hasMoreDays = true;
    }

    let html = '<div class="sequence-timeline-wrapper">';

    daysToRender.forEach((day) => {
      const markers = day.markers || [];
      if (!markers.length) return;

      const dayTitle = day.title || `Day ${day.day_number}`;

      html += `
        <div class="day-sequence-track">
          <div class="track-header">
            <span class="track-dot" style="background:${day.color || '#2563eb'};"></span>
            <strong>Day ${day.day_number}: ${esc(dayTitle)}</strong>
            <span class="track-sub">Start from hotel base to consecutive events</span>
          </div>
          <div class="track-flow">
      `;

      markers.forEach((m, idx) => {
        const isLodging = m.is_lodging || idx === 0 || idx === markers.length - 1;
        const stopInfo = getStopLetter(idx, markers.length, isLodging);
        const letter = stopInfo.symbolHtml;
        const color = isLodging ? '#4338ca' : (day.color || '#2563eb');
        const stepId = `step-${day.day_number}-${idx}`;
        const timeStr = m.time ? formatTime12(m.time) : '';

        html += `
          <div class="sequence-step ${isLodging ? 'is-hotel-step' : ''}" data-step-id="${stepId}" data-lat="${m.lat}" data-lon="${m.lon}" title="Click to view on map">
            <span class="seq-badge" style="background:${color};">${letter}</span>
            <div class="seq-info">
              <span class="seq-title">${esc(m.title || m.name)}</span>
              ${timeStr ? `<span class="seq-time"><i class="fa-regular fa-clock"></i> ${esc(timeStr)}</span>` : ''}
            </div>
          </div>
        `;

        if (idx < markers.length - 1) {
          const nextLeg = day.legs ? day.legs[idx] : null;
          let legMetricHtml = '';
          if (nextLeg && (nextLeg.distance_km || nextLeg.duration_minutes)) {
            const mIcon = modeIcon(nextLeg.mode);
            const dText = formatDuration(nextLeg.duration_minutes);
            legMetricHtml = `
              <div class="seq-leg-connector" title="${nextLeg.distance_km} km (${dText})">
                <span class="seq-leg-pill"><i class="fa-solid ${mIcon}"></i> ${nextLeg.distance_km} km (${dText})</span>
                <i class="fa-solid fa-arrow-right seq-arrow"></i>
              </div>
            `;
          } else {
            legMetricHtml = `<i class="fa-solid fa-arrow-right seq-arrow"></i>`;
          }
          html += legMetricHtml;
        }
      });

      html += `</div></div>`;
    });

    if (hasMoreDays && activeDay === null) {
      html += `
        <div style="text-align: center; margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--line);">
          <button class="btn btn-ghost btn-sm" id="btn-show-more-days" style="font-weight: 700;">
            <i class="fa-solid fa-chevron-down"></i> Show more days (${days.length - 1} remaining)
          </button>
        </div>
      `;
    } else if (showAllExpanded && activeDay === null && days.length > 1) {
      html += `
        <div style="text-align: center; margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--line);">
          <button class="btn btn-ghost btn-sm" id="btn-collapse-days" style="font-weight: 700;">
            <i class="fa-solid fa-chevron-up"></i> Show less (collapse)
          </button>
        </div>
      `;
    }

    html += '</div>';
    return html;
  }

  function renderRouteSection(routeMap) {
    if (!routeMap?.available) return '';

    const days = routeMap.days || [];
    const dest = routeMap.destination_center?.name || 'Destination';

    // Day filter pills: Day 1 active by default
    let dayFilterButtons = `<div class="map-day-pills">`;
    days.forEach((d) => {
      const isDayActive = _currentDayFilter === d.day_number;
      dayFilterButtons += `
        <button class="day-pill-btn ${isDayActive ? 'active' : ''}" data-day="${d.day_number}">
          <span class="pill-dot" style="background:${d.color || '#2563eb'};"></span> Day ${d.day_number}
        </button>
      `;
    });
    dayFilterButtons += `
      <button class="day-pill-btn ${_currentDayFilter === null ? 'active' : ''}" data-day="all">
        <i class="fa-solid fa-layer-group"></i> All Days
      </button>
    </div>`;

    const sequenceFlow = renderSequenceFlow(routeMap, _currentDayFilter, _allDaysExpanded);

    return `
      <div class="route-map-card" id="route-map-section">
        <div class="map-card-header">
          <div>
            <h3><i class="fa-solid fa-map-location-dot" style="color:var(--brand);"></i> Day-Wise Itinerary Map <span class="carto-power-badge"><i class="fa-solid fa-bolt"></i> Powered by CARTO Spatial AI</span></h3>
            <p class="route-map-sub">
              Visual roadmap for <strong>${esc(dest)}</strong> · Computed with CARTO TomTom LDS spatial routing &amp; sequential point marks.
            </p>
          </div>
          ${dayFilterButtons}
        </div>
        <div class="route-map-wrap" id="route-map-overview"></div>
        <div id="map-sequence-container" style="margin-top: 16px;">
          ${sequenceFlow}
        </div>
      </div>`;
  }

  function bindRouteSection(routeMap) {
    if (!routeMap?.available || !window.L) return;
    const overview = document.getElementById('route-map-overview');
    if (!overview) return;

    _activeRouteMap = routeMap;
    destroyAll();
    let currentMap = initMap(overview, routeMap, { dayFilter: _currentDayFilter });

    function rebindUI() {
      // Re-bind sequence steps click with active marker objects and map
      bindSequenceClicks(currentMap, _activeMarkerObjects);

      // Re-bind Show More button
      const showMoreBtn = document.getElementById('btn-show-more-days');
      if (showMoreBtn) {
        showMoreBtn.addEventListener('click', () => {
          _allDaysExpanded = true;
          const seqContainer = document.getElementById('map-sequence-container');
          if (seqContainer) {
            seqContainer.innerHTML = renderSequenceFlow(routeMap, _currentDayFilter, true);
            rebindUI();
          }
        });
      }

      // Re-bind Collapse button
      const collapseBtn = document.getElementById('btn-collapse-days');
      if (collapseBtn) {
        collapseBtn.addEventListener('click', () => {
          _allDaysExpanded = false;
          const seqContainer = document.getElementById('map-sequence-container');
          if (seqContainer) {
            seqContainer.innerHTML = renderSequenceFlow(routeMap, _currentDayFilter, false);
            rebindUI();
          }
        });
      }
    }

    // Bind Day Filter buttons
    document.querySelectorAll('.day-pill-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.day-pill-btn').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');

        const dayVal = btn.dataset.day;
        _currentDayFilter = dayVal === 'all' ? null : parseInt(dayVal, 10);
        _allDaysExpanded = false;

        destroyAll();
        currentMap = initMap(overview, routeMap, { dayFilter: _currentDayFilter });

        // Update sequence container
        const seqContainer = document.getElementById('map-sequence-container');
        if (seqContainer) {
          seqContainer.innerHTML = renderSequenceFlow(routeMap, _currentDayFilter, _allDaysExpanded);
          rebindUI();
        }
      });
    });

    rebindUI();
  }

  window.WayfarerMaps = {
    setApiKey,
    destroyAll,
    renderRouteSection,
    bindRouteSection,
    formatDuration,
    modeIcon,
  };
})();
