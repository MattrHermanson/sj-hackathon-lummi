// ---------------------------------------------------------------------------
// Map setup
// ---------------------------------------------------------------------------
const map = L.map("map", { preferCanvas: true }).setView(
  [48.7767, -122.5942],
  11,
);

const osmLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

// Backup basemap, used automatically if OpenStreetMap's tile server refuses
// our requests (it blocks requests that have no Referer, for example when the
// page is opened from disk instead of through the server).
const backupLayer = L.tileLayer(
  "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
  {
    maxZoom: 19,
    subdomains: "abcd",
    attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
  },
);

let osmLoaded = 0;
let osmErrors = 0;
osmLayer.on("tileload", () => {
  osmLoaded++;
});
osmLayer.on("tileerror", () => {
  osmErrors++;
  if (osmLoaded === 0 && osmErrors >= 3 && !map.hasLayer(backupLayer)) {
    console.warn(
      "OpenStreetMap tiles failed to load; switching to the backup basemap.",
    );
    map.removeLayer(osmLayer);
    backupLayer.addTo(map);
    backupLayer.bringToBack();
  }
});

// Layer order matters: paths are added first so markers draw on top of them.
const pathsLayer = L.layerGroup().addTo(map);
const markers = L.layerGroup().addTo(map);
let searchCircle = null;

async function loadBoundaries() {
  try {
    const res = await fetch("/api/boundaries");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const fc = await res.json();

    L.geoJSON(fc, {
      interactive: false, // clicks pass through so the map can still set the search center
      style: {
        color: "#b5541c",
        weight: 2,
        dashArray: "6 4",
        fillOpacity: 0.08,
      },
    })
      .addTo(map)
      .bringToBack();
  } catch (err) {
    console.error("Could not load boundaries:", err);
  }
}

loadBoundaries();

// ---------------------------------------------------------------------------
// Elements
// ---------------------------------------------------------------------------
const el = {
  lat: document.getElementById("lat"),
  lon: document.getElementById("lon"),
  radius: document.getElementById("radius"),
  start: document.getElementById("start"),
  end: document.getElementById("end"),
  search: document.getElementById("search"),
  status: document.getElementById("status"),
  liveToggle: document.getElementById("live-toggle"),
  liveStatus: document.getElementById("live-status"),
};

function setStatus(text, isError = false) {
  el.status.textContent = text;
  el.status.classList.toggle("error", isError);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Escape text before putting it in popup HTML (vessel names come from the data)
function esc(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}

// Stable color per vessel, derived from its MMSI
function colorFor(mmsi) {
  let h = 0;
  for (const c of String(mmsi)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h}, 70%, 40%)`;
}

// Turn a FastAPI error response into a readable message
async function errorMessage(res) {
  const body = await res.json().catch(() => ({}));
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail) && body.detail[0]?.msg) {
    const field = body.detail[0].loc?.slice(-1)[0];
    return field ? `${field}: ${body.detail[0].msg}` : body.detail[0].msg;
  }
  return `HTTP ${res.status}`;
}

// Split a vessel's points into segments wherever there is a long time gap,
// so a vessel that left AIS range doesn't get a straight line across the gap.
const GAP_MS = 30 * 60 * 1000; // 30 minutes

function splitOnGaps(points) {
  const segments = [];
  let current = [];
  let prevTime = null;

  for (const [lat, lon, time] of points) {
    const t = new Date(time).getTime();
    if (prevTime !== null && t - prevTime > GAP_MS && current.length) {
      segments.push(current);
      current = [];
    }
    current.push([lat, lon]);
    prevTime = t;
  }
  if (current.length) segments.push(current);
  return segments;
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------
async function fetchNearby(lat, lon, radiusKm, start, end) {
  const params = new URLSearchParams({ lat, lon, radius_km: radiusKm });
  if (start) params.set("start", start);
  if (end) params.set("end", end);

  const res = await fetch(`/api/vessels/nearby?${params}`);
  if (!res.ok) throw new Error(await errorMessage(res));
  return res.json();
}

// The path endpoint accepts at most 200 MMSIs per request, so send chunks.
// This also keeps the URLs short.
async function fetchPaths(mmsis, start, end) {
  const CHUNK = 100;
  const requests = [];

  for (let i = 0; i < mmsis.length; i += CHUNK) {
    const params = new URLSearchParams();
    for (const m of mmsis.slice(i, i + CHUNK)) params.append("mmsi", m);
    if (start) params.set("start", start);
    if (end) params.set("end", end);

    requests.push(
      fetch(`/api/vessels/path?${params}`).then(async (res) => {
        if (!res.ok) throw new Error(await errorMessage(res));
        return res.json();
      }),
    );
  }

  const results = await Promise.all(requests);
  return {
    paths: results.flatMap((r) => r.paths),
    truncated: results.some((r) => r.truncated),
  };
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------
function drawPaths(paths) {
  pathsLayer.clearLayers();

  for (const p of paths) {
    const color = colorFor(p.mmsi);
    const popup =
      `${esc(p.vessel_name ?? "Unknown")}<br>` +
      `MMSI: ${esc(p.mmsi)}<br>` +
      `${p.points.length} points`;

    for (const segment of splitOnGaps(p.points)) {
      if (segment.length < 2) continue;
      L.polyline(segment, { color, weight: 2, opacity: 0.7 })
        .bindPopup(popup)
        .addTo(pathsLayer);
    }
  }
}

function drawMarkers(results) {
  markers.clearLayers();

  for (const v of results) {
    L.circleMarker([v.lat, v.lon], {
      radius: 4,
      color: colorFor(v.mmsi),
      weight: 1,
      fillOpacity: 0.9,
    })
      .bindPopup(
        `${esc(v.vessel_name ?? "Unknown")}<br>` +
          `MMSI: ${esc(v.mmsi)}<br>` +
          `Speed: ${esc(v.sog ?? "n/a")} kn<br>` +
          `Time: ${esc(v.base_datetime)}<br>` +
          `Distance: ${v.distance_km.toFixed(2)} km`,
      )
      .addTo(markers);
  }
}

// ---------------------------------------------------------------------------
// Live vessels (polls /api/vessels/live, which is fed by the AISstream ingestor)
// ---------------------------------------------------------------------------
const LIVE_POLL_MS = 5000;
const liveLayer = L.layerGroup().addTo(map);
const liveMarkers = new Map(); // mmsi -> L.marker
let liveTimer = null;

// Moving vessels get an arrow pointing along their heading (or course if the
// heading isn't reported). Stationary vessels get a dot.
function vesselIcon(v) {
  const color = colorFor(v.mmsi);
  const angle = v.heading ?? v.cog;
  const moving = (v.sog ?? 0) >= 0.5 && angle != null;

  const shape = moving
    ? `<polygon points="10,1 17,18 10,14 3,18" fill="${color}" stroke="#fff" stroke-width="1.5"
         stroke-linejoin="round" transform="rotate(${angle} 10 10)" />`
    : `<circle cx="10" cy="10" r="5" fill="${color}" stroke="#fff" stroke-width="1.5" />`;

  return L.divIcon({
    className: "live-vessel",
    html: `<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">${shape}</svg>`,
    iconSize: [20, 20],
    iconAnchor: [10, 10],
  });
}

function livePopup(v) {
  const size = v.length && v.width ? `${v.length} x ${v.width} m<br>` : "";
  const ageMin = Math.floor(v.age_seconds / 60);
  const age =
    v.age_seconds < 60 ? `${v.age_seconds} s ago` : `${ageMin} min ago`;
  return (
    `<strong>${esc(v.vessel_name ?? "Unknown")}</strong><br>` +
    `MMSI: ${esc(v.mmsi)}<br>` +
    size +
    `Speed: ${esc(v.sog ?? "n/a")} kn<br>` +
    `Course: ${esc(v.cog ?? "n/a")}&deg;<br>` +
    `Last report: ${age}`
  );
}

async function refreshLive() {
  try {
    const res = await fetch("/api/vessels/live");
    if (!res.ok) throw new Error(await errorMessage(res));
    const data = await res.json();

    const seen = new Set();
    for (const v of data.vessels) {
      seen.add(v.mmsi);
      let marker = liveMarkers.get(v.mmsi);
      if (marker) {
        // Update in place so markers move smoothly and open popups stay open
        marker.setLatLng([v.lat, v.lon]);
        marker.setIcon(vesselIcon(v));
        marker.vessel = v;
        const popup = marker.getPopup();
        if (popup && popup.isOpen()) popup.setContent(livePopup(v));
      } else {
        marker = L.marker([v.lat, v.lon], {
          icon: vesselIcon(v),
          keyboard: false,
        });
        marker.vessel = v;
        marker.bindPopup(() => livePopup(marker.vessel));
        marker.addTo(liveLayer);
        liveMarkers.set(v.mmsi, marker);
      }
    }

    // Remove vessels that dropped out of the live window
    for (const [mmsi, marker] of liveMarkers) {
      if (!seen.has(mmsi)) {
        liveLayer.removeLayer(marker);
        liveMarkers.delete(mmsi);
      }
    }

    el.liveStatus.textContent = `${data.count} vessels, updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    el.liveStatus.textContent = `Live feed unavailable: ${err.message}`;
  }
}

async function liveLoop() {
  clearTimeout(liveTimer);
  if (!el.liveToggle.checked) return;

  if (document.visibilityState === "visible") await refreshLive();

  // Schedule the next poll only after this one finishes, so requests never overlap
  if (el.liveToggle.checked) liveTimer = setTimeout(liveLoop, LIVE_POLL_MS);
}

function clearLive() {
  liveLayer.clearLayers();
  liveMarkers.clear();
}

el.liveToggle.addEventListener("change", () => {
  if (el.liveToggle.checked) {
    liveLoop();
  } else {
    clearTimeout(liveTimer);
    clearLive();
    el.liveStatus.textContent = "Live feed off";
  }
});

// Refresh immediately when the tab becomes visible again
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && el.liveToggle.checked)
    liveLoop();
});

liveLoop();

// ---------------------------------------------------------------------------
// Search flow
// ---------------------------------------------------------------------------
async function search() {
  const lat = parseFloat(el.lat.value);
  const lon = parseFloat(el.lon.value);
  const radius = parseFloat(el.radius.value);
  const start = el.start.value; // "" when empty
  const end = el.end.value;

  if (![lat, lon, radius].every(Number.isFinite)) {
    setStatus("Enter a latitude, longitude and radius.", true);
    return;
  }

  el.search.disabled = true;
  setStatus("Searching...");

  try {
    // 1. Which vessels have points inside the circle?
    const data = await fetchNearby(lat, lon, radius, start, end);

    markers.clearLayers();
    pathsLayer.clearLayers();
    if (searchCircle) map.removeLayer(searchCircle);
    searchCircle = L.circle([lat, lon], { radius: radius * 1000 }).addTo(map);

    // 2. Fetch the full track of each of those vessels
    const mmsis = [...new Set(data.results.map((v) => v.mmsi).filter(Boolean))];
    let truncated = false;

    if (mmsis.length > 0) {
      setStatus(`Loading tracks for ${mmsis.length} vessels...`);
      const pathData = await fetchPaths(mmsis, start, end);
      truncated = pathData.truncated;
      drawPaths(pathData.paths);
    }

    // 3. Draw the in-area points on top of the tracks
    drawMarkers(data.results);

    map.fitBounds(searchCircle.getBounds());

    let message = `${data.count} points from ${mmsis.length} vessels.`;
    if (data.count === 0)
      message = "No vessels found. Try a larger radius or wider dates.";
    if (truncated) message += " Some tracks were cut off by the server limit.";
    setStatus(message);
  } catch (err) {
    setStatus(`Search failed. ${err.message}`, true);
  } finally {
    el.search.disabled = false;
  }
}

el.search.addEventListener("click", search);

// Click the map to set the search center
map.on("click", (e) => {
  el.lat.value = e.latlng.lat.toFixed(5);
  el.lon.value = e.latlng.lng.toFixed(5);
});
