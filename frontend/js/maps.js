/* ============================================================
   maps.js — Leaflet route maps for finalized itineraries.
   Expects route_map payload from build_route_map on the backend.
   ============================================================ */

(() => {
  'use strict';

  const _maps = [];

  function destroyAll() {
    _maps.forEach((m) => { try { m.remove(); } catch (_) { /* noop */ } });
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

  function initMap(el, routeMap, options = {}) {
    if (!window.L || !el || !routeMap?.available) return null;

    const center = routeMap.destination_center;
    const map = L.map(el, { scrollWheelZoom: false });
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      maxZoom: 18,
    }).addTo(map);

    let bounds = [];
    const days = options.dayFilter != null
      ? routeMap.days.filter((d) => d.day_number === options.dayFilter)
      : routeMap.days;

    days.forEach((day) => {
      const markersToPlot = day.markers || [];

      markersToPlot.forEach((m, idx) => {
        const isStart = idx === 0;
        const isEnd = idx === markersToPlot.length - 1 && markersToPlot.length > 1;

        let labelStr = m.title;
        if (isStart) labelStr += ' (Start of day)';
        else if (isEnd) labelStr += ' (End of day)';

        L.circleMarker([m.lat, m.lon], {
          radius: 8,
          color: '#fff',
          weight: 2,
          fillColor: day.color || '#0d9488',
          fillOpacity: 0.95,
        })
          .addTo(map)
          .bindPopup(
            `<strong>${labelStr || m.name}</strong>` +
              (m.time ? `<br><small>${m.time}</small>` : '')
          );
        bounds.push([m.lat, m.lon]);
      });

      (day.legs || []).forEach((leg) => {
        const coords = (leg.geometry || []).map((c) => [c[1], c[0]]);
        if (coords.length >= 2) {
          L.polyline(coords, {
            color: day.color || '#0d9488',
            weight: 4,
            opacity: 0.85,
          }).addTo(map);
        }
      });
    });

    if (center && bounds.length > 1) {
      const maxKm = 50;
      const toRad = (d) => (d * Math.PI) / 180;
      const distKm = (a, b) => {
        const R = 6371;
        const dLat = toRad(b[0] - a[0]);
        const dLon = toRad(b[1] - a[1]);
        const x =
          Math.sin(dLat / 2) ** 2 +
          Math.cos(toRad(a[0])) * Math.cos(toRad(b[0])) * Math.sin(dLon / 2) ** 2;
        return 2 * R * Math.asin(Math.sqrt(x));
      };
      const hub = [center.lat, center.lon];
      bounds = bounds.filter((b) => distKm(hub, b) <= maxKm);
    }

    if (bounds.length) {
      const validBounds = bounds.filter(
        (b) => Math.abs(b[0]) <= 90 && Math.abs(b[1]) <= 180
      );
      if (validBounds.length) {
        map.fitBounds(validBounds, { padding: [40, 40], maxZoom: 15 });
      } else if (center) {
        map.setView([center.lat, center.lon], 13);
      }
    } else if (center) {
      map.setView([center.lat, center.lon], 13);
    }

    _maps.push(map);
    setTimeout(() => map.invalidateSize(), 120);
    return map;
  }

  function renderRouteSection(routeMap) {
    if (!routeMap?.available) return '';

    const t = routeMap.totals || {};
    const days = routeMap.days || [];

    const stats = `
      <div class="route-stats">
        <div class="route-stat"><span class="k">Total distance</span><span class="v">${t.distance_km ?? 0} km</span></div>
        <div class="route-stat"><span class="k">Travel time</span><span class="v">${formatDuration(t.duration_minutes)}</span></div>
        <div class="route-stat"><span class="k">Stops</span><span class="v">${t.stops ?? 0}</span></div>
        <div class="route-stat"><span class="k">Legs</span><span class="v">${t.legs ?? 0}</span></div>
      </div>`;

    const legRows = days
      .flatMap((d) =>
        (d.legs || []).map(
          (leg) => `
        <tr>
          <td><span class="day-dot" style="background:${d.color}"></span> Day ${d.day_number}</td>
          <td>${leg.from?.title || leg.from?.name || '—'}</td>
          <td>${leg.to?.title || leg.to?.name || '—'}</td>
          <td>${leg.distance_km} km</td>
          <td>${formatDuration(leg.duration_minutes)}</td>
          <td><i class="fa-solid ${modeIcon(leg.mode)}"></i> ${leg.mode || 'walking'}</td>
        </tr>`
        )
      )
      .join('');

    return `
      <div class="route-map-card" id="route-map-section">
        <h3><i class="fa-solid fa-map-location-dot"></i> Trip route map</h3>
        <p class="route-map-sub">Distances and travel times estimated between consecutive stops on your itinerary.</p>
        ${stats}
        <div class="route-map-wrap" id="route-map-overview"></div>
        <div class="route-legs-table-wrap" style="margin-top: 20px;">
          <table class="route-legs-table">
            <thead>
              <tr><th>Day</th><th>From</th><th>To</th><th>Distance</th><th>Time</th><th>Mode</th></tr>
            </thead>
            <tbody>${legRows}</tbody>
          </table>
        </div>
      </div>`;
  }

  function bindRouteSection(routeMap) {
    if (!routeMap?.available || !window.L) return;
    const overview = document.getElementById('route-map-overview');
    if (overview) initMap(overview, routeMap);
  }

  window.WayfarerMaps = {
    destroyAll,
    renderRouteSection,
    bindRouteSection,
    formatDuration,
    modeIcon,
  };
})();
