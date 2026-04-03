const config = window.OPENFIRE_CONFIG;
const activeSource = config.sources?.[config.runtimeMode] || config.sources?.demo;

const map = L.map("map", {
  zoomControl: true,
}).setView(config.map.center, config.map.zoom);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 18,
}).addTo(map);

map.setMinZoom(config.map.minZoom || 5);

const statusMessage = document.getElementById("status-message");
const modelVersionNode = document.getElementById("meta-model-version");
const inferenceDateNode = document.getElementById("meta-inference-date");
const dataWindowNode = document.getElementById("meta-data-window");
const sourceModeNode = document.getElementById("meta-source-mode");

let geoJsonLayer;

function setStatus(message, isError = false) {
  statusMessage.textContent = message;
  statusMessage.style.color = isError ? "#9f2d20" : "";
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

function formatProbability(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(2) : "n/a";
}

function layerStyle(feature) {
  const probability = Number(feature?.properties?.probability ?? 0);
  const band = getBand(probability);
  return {
    radius: 7,
    fillColor: band.color,
    color: "#3c2d20",
    weight: 1,
    opacity: 0.9,
    fillOpacity: 0.82,
  };
}

function tooltipHtml(feature) {
  const properties = feature.properties || {};
  const probability = Number(properties.probability ?? 0);
  const band = getBand(probability);
  return `
    <div class="tooltip-grid">
      <strong>${band.label}</strong>
      <span>Probability: ${formatProbability(probability)}</span>
      <span>Predicted label: ${properties.predicted_label ?? "n/a"}</span>
      <span>Model version: ${properties.model_version ?? "n/a"}</span>
    </div>
  `;
}

function updateMetadataPanel({ geojsonMetadata = {}, backendMetadata = {}, sourceMode }) {
  const modelVersion =
    geojsonMetadata.model_version ||
    backendMetadata.model_version ||
    "Unavailable";
  const inferenceDate =
    geojsonMetadata.inference_date ||
    activeSource?.fallbackInferenceDate ||
    new Date().toISOString().slice(0, 10);
  const dataWindow =
    geojsonMetadata.data_window ||
    activeSource?.fallbackDataWindow ||
    "Unavailable";

  modelVersionNode.textContent = modelVersion;
  inferenceDateNode.textContent = inferenceDate;
  dataWindowNode.textContent = dataWindow;
  sourceModeNode.textContent = `${config.runtimeMode}:${sourceMode}`;
}

function normalizeGeoJsonPayload(payload) {
  if (payload?.type === "FeatureCollection") {
    return {
      featureCollection: payload,
      metadata: payload.metadata || {},
      modelVersion: payload.metadata?.model_version || null,
    };
  }

  if (payload?.feature_collection?.type === "FeatureCollection") {
    return {
      featureCollection: payload.feature_collection,
      metadata: {
        model_version: payload.model_version,
        ...payload.feature_collection.metadata,
      },
      modelVersion: payload.model_version || null,
    };
  }

  throw new Error("Unsupported GeoJSON payload shape.");
}

async function fetchBackendMetadata() {
  if (!activeSource?.metadataUrl) {
    return {};
  }

  const response = await fetch(activeSource.metadataUrl);
  if (!response.ok) {
    throw new Error(`Metadata request failed with status ${response.status}`);
  }
  return response.json();
}

async function loadRiskLayer() {
  if (!activeSource) {
    throw new Error(`No frontend data source is configured for runtimeMode=${config.runtimeMode}.`);
  }
  const { mode, url, requestBodyUrl } = activeSource;

  if (mode === "static" || mode === "api-geojson") {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`GeoJSON request failed with status ${response.status}`);
    }
    return normalizeGeoJsonPayload(await response.json());
  }

  if (mode === "predict-geojson") {
    const bodyResponse = await fetch(requestBodyUrl);
    if (!bodyResponse.ok) {
      throw new Error(`Request body fetch failed with status ${bodyResponse.status}`);
    }
    const requestBody = await bodyResponse.json();

    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
    if (!response.ok) {
      throw new Error(`Predict GeoJSON request failed with status ${response.status}`);
    }
    return normalizeGeoJsonPayload(await response.json());
  }

  throw new Error(`Unsupported data source mode: ${mode}`);
}

function renderGeoJson(featureCollection) {
  if (geoJsonLayer) {
    geoJsonLayer.remove();
  }

  geoJsonLayer = L.geoJSON(featureCollection, {
    pointToLayer(feature, latlng) {
      return L.circleMarker(latlng, layerStyle(feature));
    },
    onEachFeature(feature, layer) {
      layer.bindTooltip(tooltipHtml(feature), {
        direction: "top",
        sticky: true,
        className: "risk-tooltip",
      });

      layer.on({
        mouseover: () => layer.setStyle({ radius: 9, weight: 2 }),
        mouseout: () => layer.setStyle(layerStyle(feature)),
      });
    },
  }).addTo(map);

  const bounds = geoJsonLayer.getBounds();
  if (bounds.isValid()) {
    map.fitBounds(bounds.pad(0.15));
  }
}

async function boot() {
  createLegend();
  setStatus(`Loading ${config.runtimeMode} risk layer...`);

  try {
    const [geojsonResult, backendMetadata] = await Promise.all([
      loadRiskLayer(),
      fetchBackendMetadata().catch(() => ({})),
    ]);

    renderGeoJson(geojsonResult.featureCollection);
    updateMetadataPanel({
      geojsonMetadata: geojsonResult.metadata,
      backendMetadata,
      sourceMode: activeSource.mode,
    });
    setStatus(
      `Loaded ${geojsonResult.featureCollection.features.length} features from ${config.runtimeMode} mode.`
    );
  } catch (error) {
    console.error(error);
    setStatus(
      error instanceof Error ? error.message : "Failed to load risk layer.",
      true
    );
    updateMetadataPanel({
      sourceMode: `${activeSource?.mode || "unconfigured"} (error)`,
    });
  }
}

boot();
