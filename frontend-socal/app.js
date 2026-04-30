const config = window.OPENFIRE_SOCAL_CONFIG;
const canvasRenderer = L.canvas({ padding: 0.35 });

const map = L.map("map", {
  zoomControl: true,
  preferCanvas: true,
}).setView(config.map.center, config.map.zoom);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 18,
}).addTo(map);

map.setMinZoom(config.map.minZoom || 5);

const nodes = {
  status: document.getElementById("status-message"),
  source: document.getElementById("source-message"),
  statusMeta: document.getElementById("meta-status"),
  windowDate: document.getElementById("meta-window-date"),
  modelVersion: document.getElementById("meta-model-version"),
  featureCount: document.getElementById("meta-feature-count"),
  countyCount: document.getElementById("meta-county-count"),
};

function setStatus(message, isError = false) {
  nodes.status.textContent = message;
  nodes.status.style.color = isError ? "#a33a2a" : "";
}

function resolveAssetUrl(url) {
  return new URL(url, window.location.href).toString();
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Request failed for ${url} with status ${response.status}`);
  }
  return response.json();
}

function getBand(probability) {
  return (
    config.riskBands.find((band) => probability >= band.min && probability < band.max) ||
    config.riskBands[config.riskBands.length - 1]
  );
}

function createLegend() {
  const legendRoot = document.getElementById("legend");
  legendRoot.innerHTML = "";

  config.riskBands.forEach((band) => {
    const row = document.createElement("div");
    row.className = "legend-row";
    row.innerHTML = `
      <span class="legend-swatch" style="background:${band.color}"></span>
      <span>${band.label}</span>
    `;
    legendRoot.appendChild(row);
  });
}

function pointStyle(feature) {
  const probability = Number(feature?.properties?.risk_probability ?? 0);
  const band = getBand(probability);
  return {
    renderer: canvasRenderer,
    radius: 3,
    fillColor: band.color,
    color: band.color,
    weight: 1,
    opacity: 0.72,
    fillOpacity: 0.58,
  };
}

function formatProbability(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(3) : "n/a";
}

function tooltipHtml(feature) {
  const properties = feature.properties || {};
  const probability = Number(properties.risk_probability ?? 0);
  const band = getBand(probability);
  return `
    <div class="tooltip-grid">
      <strong>${band.label}</strong>
      <span>Risk probability: ${formatProbability(probability)}</span>
      <span>Window: ${properties.window_start_date ?? "n/a"}</span>
      <span>Predicted label: ${properties.predicted_label ?? "n/a"}</span>
      <span>Model version: ${properties.model_version ?? "n/a"}</span>
    </div>
  `;
}

function renderAoi(aoi) {
  const boundaryLayer = L.geoJSON(aoi, {
    style: {
      color: "#24392f",
      weight: 2,
      fillColor: "#ffffff",
      fillOpacity: 0.08,
    },
  }).addTo(map);

  const labelLayer = L.layerGroup();
  aoi.features.forEach((feature) => {
    const name = feature.properties?.NAME || feature.properties?.NAMELSAD;
    const layer = L.geoJSON(feature);
    const center = layer.getBounds().getCenter();
    L.marker(center, {
      interactive: false,
      icon: L.divIcon({
        className: "county-label",
        html: `<span>${name}</span>`,
      }),
    }).addTo(labelLayer);
  });
  labelLayer.addTo(map);

  const bounds = boundaryLayer.getBounds();
  if (bounds.isValid()) {
    map.fitBounds(bounds.pad(0.05));
  }

  return boundaryLayer;
}

function renderRisk(riskGeojson) {
  return L.geoJSON(riskGeojson, {
    pointToLayer(feature, latlng) {
      return L.circleMarker(latlng, pointStyle(feature));
    },
    onEachFeature(feature, layer) {
      layer.bindTooltip(tooltipHtml(feature), {
        direction: "top",
        sticky: true,
        className: "risk-tooltip",
      });
    },
  }).addTo(map);
}

function updateMetadata(manifest, aoi, riskGeojson) {
  const featureCount = riskGeojson.features?.length || 0;
  const riskCount = riskGeojson.features?.filter(
    (feature) => Number(feature.properties?.risk_probability ?? 0) >= 0.5
  ).length || 0;

  nodes.statusMeta.textContent = manifest.status || "static";
  nodes.windowDate.textContent = manifest.latest_window_start_date || "Unavailable";
  nodes.modelVersion.textContent = manifest.model_version || "Unavailable";
  nodes.featureCount.textContent = `${featureCount.toLocaleString()} (${riskCount.toLocaleString()} elevated)`;
  nodes.countyCount.textContent = `${aoi.features?.length || 0}`;
  nodes.source.textContent = manifest.source || "Static manifest";
}

async function boot() {
  createLegend();
  setStatus("Loading SoCal manifest...");

  try {
    const manifest = await fetchJson(config.manifestUrl);
    const [aoi, riskGeojson] = await Promise.all([
      fetchJson(resolveAssetUrl(manifest.aoi_geojson_uri || "./data/aoi_counties.geojson")),
      fetchJson(resolveAssetUrl(manifest.latest_geojson_uri)),
    ]);

    setStatus("Rendering AOI boundary...");
    renderAoi(aoi);
    setStatus("Rendering risk layer...");
    renderRisk(riskGeojson);
    updateMetadata(manifest, aoi, riskGeojson);
    setStatus(`Loaded ${riskGeojson.features.length.toLocaleString()} risk features.`);
  } catch (error) {
    console.error(error);
    setStatus(error instanceof Error ? error.message : "Failed to load SoCal UI.", true);
  }
}

boot();
