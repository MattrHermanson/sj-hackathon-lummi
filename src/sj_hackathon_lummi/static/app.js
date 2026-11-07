const map = L.map("map").setView([48.7767, -122.5942], 11);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

const markers = L.layerGroup().addTo(map);
let searchCircle = null;

async function search() {
  const lat = parseFloat(document.getElementById("lat").value);
  const lon = parseFloat(document.getElementById("lon").value);
  const radius = parseFloat(document.getElementById("radius").value);
  const status = document.getElementById("status");

  status.textContent = "Loading...";
  try {
    const params = new URLSearchParams({ lat, lon, radius_km: radius });
    const res = await fetch(`/api/vessels/nearby?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Clear previous results
    markers.clearLayers();
    if (searchCircle) map.removeLayer(searchCircle);

    // Draw search area
    searchCircle = L.circle([lat, lon], { radius: radius * 1000 }).addTo(map);

    // Plot each vessel
    for (const v of data.results) {
      L.circleMarker([v.lat, v.lon], { radius: 4 })
        .bindPopup(
          `${v.vessel_name ?? "Unknown"}<br>` +
            `MMSI: ${v.mmsi}<br>` +
            `Speed: ${v.sog ?? "n/a"} kn<br>` +
            `Time: ${v.base_datetime}<br>` +
            `Distance: ${v.distance_km.toFixed(2)} km`,
        )
        .addTo(markers);
    }

    map.fitBounds(searchCircle.getBounds());
    status.textContent = `${data.count} points`;
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
  }
}

document.getElementById("search").addEventListener("click", search);

// Click the map to set the search center
map.on("click", (e) => {
  document.getElementById("lat").value = e.latlng.lat.toFixed(5);
  document.getElementById("lon").value = e.latlng.lng.toFixed(5);
});
