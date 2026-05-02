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
  slider: document.getElementById("timeline-slider"),
  timelineDate: document.getElementById("timeline-date"),
  timelinePosition: document.getElementById("timeline-position"),
  timelineTicks: document.getElementById("timeline-ticks"),
  playbackToggle: document.getElementById("playback-toggle"),
};

const state = {
  manifest: null,
  aoi: null,
  riskLayer: null,
  windows: [],
  activeIndex: 0,
  cache: new Map(),
  playbackTimer: null,
};

function setStatus(message, isError = false) {
  nodes.status.textContent = message;
  nodes.status.style.color = isError ? "#a33a2a" : "";
}

function isLocalDevelopment() {
  return ["", "localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
}

function resolveAssetUrl(url) {
  if (!url) {
    return "";
  }
  if (url.startsWith("gs://openfire/predictions/")) {
    return `${config.dataProxyPrefix || "/data/"}${url.split("/").pop()}`;
  }
  if (url.startsWith("https://storage.googleapis.com/openfire/predictions/")) {
    return `${config.dataProxyPrefix || "/data/"}${url.split("/").pop()}`;
  }
  if (url.startsWith("/")) {
    return url;
  }
  return new URL(url, window.location.href).toString();
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Request failed for ${url} with status ${response.status}`);
  }
  return response.json();
}

async function fetchManifest() {
  try {
    return await fetchJson(config.manifestUrl);
  } catch (error) {
    if (!isLocalDevelopment() || !config.demoManifestUrl || config.demoManifestUrl === config.manifestUrl) {
      throw error;
    }
    setStatus("Live manifest unavailable; loading local demo manifest.");
    return fetchJson(config.demoManifestUrl);
  }
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

function formatDate(value) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) {
    return value || "Unavailable";
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(date);
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

function buildRiskLayer(riskGeojson) {
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
  });
}

function setLayerOpacity(layer, opacity) {
  layer.eachLayer((child) => {
    if (child.setStyle) {
      child.setStyle({ opacity, fillOpacity: opacity * 0.8 });
    }
  });
}

function replaceRiskLayer(riskGeojson) {
  const previous = state.riskLayer;
  const next = buildRiskLayer(riskGeojson);
  setLayerOpacity(next, 0.15);
  next.addTo(map);
  state.riskLayer = next;

  window.setTimeout(() => setLayerOpacity(next, 0.72), 20);
  if (previous) {
    setLayerOpacity(previous, 0.1);
    window.setTimeout(() => map.removeLayer(previous), 150);
  }
}

function normalizeWindows(manifest) {
  const rawWindows = Array.isArray(manifest.windows) && manifest.windows.length
    ? manifest.windows
    : [
        {
          window_start_date: manifest.latest_window_start_date,
          geojson_uri: manifest.latest_geojson_uri,
          model_version: manifest.model_version,
          updated_at: manifest.updated_at,
        },
      ];
  return rawWindows
    .filter((item) => item.window_start_date && item.geojson_uri)
    .map((item) => ({
      window_start_date: String(item.window_start_date),
      geojson_uri: item.geojson_uri,
      model_version: item.model_version || manifest.model_version || "Unavailable",
      updated_at: item.updated_at || manifest.updated_at || "",
    }))
    .sort((a, b) => a.window_start_date.localeCompare(b.window_start_date));
}

function cacheSet(key, value) {
  if (state.cache.has(key)) {
    state.cache.delete(key);
  }
  state.cache.set(key, value);
  while (state.cache.size > (config.snapshotCacheSize || 8)) {
    state.cache.delete(state.cache.keys().next().value);
  }
}

async function loadSnapshot(windowEntry) {
  const url = resolveAssetUrl(windowEntry.geojson_uri);
  if (state.cache.has(url)) {
    const cached = state.cache.get(url);
    cacheSet(url, cached);
    return cached;
  }
  const payload = await fetchJson(url);
  cacheSet(url, payload);
  return payload;
}

function prefetchNeighbors(index) {
  if (state.playbackTimer) {
    return;
  }
  [index - 1, index + 1].forEach((candidate) => {
    if (candidate < 0 || candidate >= state.windows.length) {
      return;
    }
    const url = resolveAssetUrl(state.windows[candidate].geojson_uri);
    if (!state.cache.has(url)) {
      loadSnapshot(state.windows[candidate]).catch(() => undefined);
    }
  });
}

function updateMetadata(manifest, aoi, riskGeojson, windowEntry) {
  const featureCount = riskGeojson.features?.length || 0;
  const riskCount = riskGeojson.features?.filter(
    (feature) => Number(feature.properties?.risk_probability ?? 0) >= 0.5
  ).length || 0;

  nodes.statusMeta.textContent = manifest.status || "live";
  nodes.windowDate.textContent = windowEntry.window_start_date || manifest.latest_window_start_date || "Unavailable";
  nodes.modelVersion.textContent = windowEntry.model_version || manifest.model_version || "Unavailable";
  nodes.featureCount.textContent = `${featureCount.toLocaleString()} (${riskCount.toLocaleString()} elevated)`;
  nodes.countyCount.textContent = `${aoi.features?.length || 0}`;
  nodes.source.textContent = manifest.source || "GCS prediction manifest";
}

function renderTimelineTicks() {
  nodes.timelineTicks.innerHTML = "";
  state.windows.forEach((windowEntry, index) => {
    const tick = document.createElement("button");
    tick.className = "timeline-tick";
    tick.type = "button";
    tick.title = formatDate(windowEntry.window_start_date);
    tick.setAttribute("aria-label", `Load ${formatDate(windowEntry.window_start_date)}`);
    tick.addEventListener("click", () => setActiveIndex(index));
    nodes.timelineTicks.appendChild(tick);
  });
}

function updateTimelineUi() {
  nodes.slider.value = String(state.activeIndex);
  nodes.timelineDate.textContent = formatDate(state.windows[state.activeIndex]?.window_start_date);
  nodes.timelinePosition.textContent = `${state.activeIndex + 1} / ${state.windows.length}`;
  [...nodes.timelineTicks.children].forEach((tick, index) => {
    tick.classList.toggle("is-active", index === state.activeIndex);
  });
}

function configureTimeline() {
  nodes.slider.min = "0";
  nodes.slider.max = String(Math.max(state.windows.length - 1, 0));
  nodes.slider.step = "1";
  nodes.slider.disabled = state.windows.length < 2;
  renderTimelineTicks();
  updateTimelineUi();
}

async function setActiveIndex(index, { prefetch = true } = {}) {
  const boundedIndex = Math.max(0, Math.min(index, state.windows.length - 1));
  const windowEntry = state.windows[boundedIndex];
  if (!windowEntry) {
    return;
  }

  state.activeIndex = boundedIndex;
  updateTimelineUi();
  setStatus(`Loading ${windowEntry.window_start_date} snapshot...`);
  try {
    const riskGeojson = await loadSnapshot(windowEntry);
    replaceRiskLayer(riskGeojson);
    updateMetadata(state.manifest, state.aoi, riskGeojson, windowEntry);
    setStatus(`Loaded ${riskGeojson.features.length.toLocaleString()} risk features.`);
    if (prefetch) {
      prefetchNeighbors(boundedIndex);
    }
  } catch (error) {
    stopPlayback();
    console.error(error);
    setStatus(error instanceof Error ? error.message : "Failed to load snapshot.", true);
  }
}

function stopPlayback() {
  if (state.playbackTimer) {
    window.clearInterval(state.playbackTimer);
    state.playbackTimer = null;
  }
  nodes.playbackToggle.textContent = "Play";
  nodes.playbackToggle.setAttribute("aria-label", "Play timeline");
}

function startPlayback() {
  if (state.windows.length < 2 || state.playbackTimer) {
    return;
  }
  nodes.playbackToggle.textContent = "Pause";
  nodes.playbackToggle.setAttribute("aria-label", "Pause timeline");
  state.playbackTimer = window.setInterval(() => {
    const nextIndex = (state.activeIndex + 1) % state.windows.length;
    const nextUrl = resolveAssetUrl(state.windows[nextIndex].geojson_uri);
    if (!state.cache.has(nextUrl)) {
      return;
    }
    setActiveIndex(nextIndex, { prefetch: false });
  }, config.playbackIntervalMs || 750);
}

function togglePlayback() {
  if (state.playbackTimer) {
    stopPlayback();
  } else {
    startPlayback();
  }
}

function bindControls() {
  nodes.slider.addEventListener("input", (event) => {
    stopPlayback();
    setActiveIndex(Number(event.target.value));
  });
  nodes.playbackToggle.addEventListener("click", togglePlayback);
  window.addEventListener("keydown", (event) => {
    if (event.target && ["INPUT", "BUTTON", "A"].includes(event.target.tagName)) {
      return;
    }
    if (event.key === "ArrowLeft") {
      stopPlayback();
      setActiveIndex(state.activeIndex - 1);
    } else if (event.key === "ArrowRight") {
      stopPlayback();
      setActiveIndex(state.activeIndex + 1);
    } else if (event.key === " ") {
      event.preventDefault();
      togglePlayback();
    }
  });
}

async function boot() {
  createLegend();
  bindControls();
  setStatus("Loading SoCal manifest...");

  try {
    const manifest = await fetchManifest();
    state.manifest = manifest;
    state.windows = normalizeWindows(manifest);
    const latestIndex = Math.max(
      state.windows.findIndex((item) => item.window_start_date === manifest.latest_window_start_date),
      0
    );
    state.activeIndex = latestIndex;

    const aoi = await fetchJson(resolveAssetUrl(manifest.aoi_geojson_uri || "./data/aoi_counties.geojson"));
    state.aoi = aoi;

    setStatus("Rendering AOI boundary...");
    renderAoi(aoi);
    configureTimeline();
    await setActiveIndex(latestIndex);
  } catch (error) {
    console.error(error);
    setStatus(error instanceof Error ? error.message : "Failed to load SoCal UI.", true);
  }
}

boot();
