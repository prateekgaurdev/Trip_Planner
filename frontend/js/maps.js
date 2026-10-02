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

  function createCalloutIcon(m, index, isLodging, totalMarkers, dayColor) {
    const isStart = index === 0;
    const isEnd = index === totalMarkers - 1 && totalMarkers > 1;

    let symbol = String.fromCharCode(65 + Math.max(0, index - (isLodging ? 1 : 0))); // A, B, C, D...
    if (isLodging || m.is_lodging || isStart || isEnd) {
      symbol = '<i class="fa-solid fa-hotel"></i>';
    }

    const palette = (isLodging || m.is_lodging)
      ? HOTEL_PALETTE
      : PIN_PALETTES[(index - 1) % PIN_PALETTES.length] || PIN_PALETTES[0];

    const rawTitle = m.title || m.name || m.label || 'Stop';
    let shortTitle = rawTitle;
    if (shortTitle.length > 22) {
      shortTitle = shortTitle.slice(0, 20).trim() + '…';
    }

    const timeStr = m.time ? esc(m.time) : '';
    const badgeTag = isLodging ? 'HOTEL' : (isStart ? 'START' : (isEnd ? 'RETURN' : `STOP ${symbol}`));

    const html = `
      <div class="wayfarer-map-callout ${isLodging ? 'is-lodging' : ''}" style="--pin-color:${palette.main}; --pin-bg:${palette.bg}; --pin-border:${palette.border}; --pin-text:${palette.text};">
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

    // CartoDB Voyager tiles (warm pastel travel aesthetic matching Image 3)
    const cartoUrl = _cartoApiKey 
      ? `https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?key=${encodeURIComponent(_cartoApiKey)}`
      : 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png';

    const voyagerTiles = L.tileLayer(cartoUrl, {
        attribution:
          '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
        maxZoom: 19,
        subdomains: 'abcd',
      }
    );

    const osmTiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      maxZoom: 19,
    });

    voyagerTiles.addTo(map);
    voyagerTiles.on('tileerror', () => {
      map.removeLayer(voyagerTiles);
      osmTiles.addTo(map);
    });

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

        const marker = L.marker([m.lat, m.lon], { icon: icon }).addTo(map);

        const destName = routeMap.destination_center?.name || '';
        const searchQ = encodeURIComponent(`${m.title || m.name} ${destName}`);
        const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${searchQ}`;

        const popupContent = `
          <div class="map-popup-card">
            <div class="popup-head" style="background:${dayColor};">
              <span class="popup-pill">${isLodging ? 'LODGING ANCHOR' : `DAY ${day.day_number} STOP`}</span>
              <h4>${esc(m.title || m.name)}</h4>
            </div>
            <div class="popup-body">
              ${m.time ? `<p class="popup-time"><i class="fa-regular fa-clock"></i> Scheduled: <strong>${esc(m.time)}</strong></p>` : ''}
              <p class="popup-tip">Explore location details, reviews &amp; photos:</p>
              <a href="${mapsUrl}" target="_blank" rel="noopener noreferrer" class="popup-maps-link">
                Open in Google Maps <i class="fa-solid fa-arrow-up-right-from-square"></i>
              </a>
            </div>
          </div>
        `;

        marker.bindPopup(popupContent, { maxWidth: 280, className: 'wayfarer-leaflet-popup' });
        bounds.push([m.lat, m.lon]);
        markerObjects.push({ marker, data: m, dayNumber: day.day_number, order: m.order });
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

    _maps.push(map);
    setTimeout(() => map.invalidateSize(), 150);

    // Bind interactive sequence timeline clicks
    bindSequenceClicks(map, markerObjects);

    return map;
  }

  function bindSequenceClicks(map, markerObjects) {
    document.querySelectorAll('.sequence-step[data-lat][data-lon]').forEach((step) => {
      step.addEventListener('click', () => {
        const lat = parseFloat(step.dataset.lat);
        const lon = parseFloat(step.dataset.lon);
        if (!isNaN(lat) && !isNaN(lon)) {
          map.flyTo([lat, lon], 16, { duration: 0.8 });
          const match = markerObjects.find(
            (o) => Math.abs(o.data.lat - lat) < 0.0001 && Math.abs(o.data.lon - lon) < 0.0001
          );
          if (match && match.marker) {
            setTimeout(() => match.marker.openPopup(), 800);
          }
        }
      });
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
        const letter = isLodging
          ? '<i class="fa-solid fa-hotel"></i>'
          : String.fromCharCode(65 + Math.max(0, idx - 1));
        const color = isLodging ? '#4338ca' : (day.color || '#2563eb');

        html += `
          <div class="sequence-step ${isLodging ? 'is-hotel-step' : ''}" data-lat="${m.lat}" data-lon="${m.lon}" title="Click to focus on map">
            <span class="seq-badge" style="background:${color};">${letter}</span>
            <div class="seq-info">
              <span class="seq-title">${esc(m.title || m.name)}</span>
              ${m.time ? `<span class="seq-time"><i class="fa-regular fa-clock"></i> ${esc(m.time)}</span>` : ''}
            </div>
          </div>
        `;

        if (idx < markers.length - 1) {
          html += `<i class="fa-solid fa-arrow-right seq-arrow"></i>`;
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
            <h3><i class="fa-solid fa-map-location-dot" style="color:var(--brand);"></i> Day-Wise Itinerary Map</h3>
            <p class="route-map-sub">
              Visual roadmap for <strong>${esc(dest)}</strong> · Start from hotel base with sequential point marks.
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
    const map = initMap(overview, routeMap, { dayFilter: _currentDayFilter });

    function rebindUI() {
      // Re-bind sequence steps click
      bindSequenceClicks(map, []);

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
        const newMap = initMap(overview, routeMap, { dayFilter: _currentDayFilter });

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
