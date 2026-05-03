const config = window.OPENFIRE_SOCAL_CONFIG;
const LARGE_LAYER_TRANSITION_THRESHOLD = 5000;

const map = L.map("map", {
  zoomControl: true,
  preferCanvas: true,
}).setView(config.map.center, config.map.zoom);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 18,
}).addTo(map);

map.setMinZoom(config.map.minZoom || 5);

function configureMapPanes() {
  const paneOrder = [
    ["riskPane", 410],
    ["aoiPane", 430],
    ["labelPane", 610],
  ];

  paneOrder.forEach(([name, zIndex]) => {
    const pane = map.createPane(name);
    pane.style.zIndex = String(zIndex);
    pane.style.pointerEvents = name === "riskPane" ? "auto" : "none";
  });
}

configureMapPanes();

const canvasRenderer = L.canvas({ padding: 0.35, pane: "riskPane" });

const nodes = {
  status: document.getElementById("status-message"),
  source: document.getElementById("source-message"),
  statusMeta: document.getElementById("meta-status"),
  windowDate: document.getElementById("meta-window-date"),
  modelVersion: document.getElementById("meta-model-version"),
  featureCount: document.getElementById("meta-feature-count"),
  renderMode: document.getElementById("meta-render-mode"),
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
  activeSnapshot: null,
  activeSnapshotUri: "",
  activeSnapshotPrecomputed: false,
  activeSnapshotRenderLabel: "Loading...",
  riskLayer: null,
  renderSignature: "",
  renderedFeatureCount: 0,
  windows: [],
  activeIndex: 0,
  cache: new Map(),
  playbackTimer: null,
  playbackLoading: false,
  riskTransitionId: 0,
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

function pointVisualStyle(zoom = map.getZoom()) {
  if (zoom <= 8) {
    return {
      radius: 2.2,
      weight: 0.6,
      opacity: 0.5,
      fillOpacity: 0.34,
    };
  }
  if (zoom <= 9) {
    return {
      radius: 2.7,
      weight: 0.8,
      opacity: 0.62,
      fillOpacity: 0.46,
    };
  }
  return {
    radius: 3.3,
    weight: 1,
    opacity: 0.74,
    fillOpacity: 0.6,
  };
}

function pointStyle(feature, opacityScale = 1) {
  const probability = Number(feature?.properties?.risk_probability ?? 0);
  const band = getBand(probability);
  const visual = pointVisualStyle();
  return {
    renderer: canvasRenderer,
    pane: "riskPane",
    radius: visual.radius,
    fillColor: band.color,
    color: band.color,
    weight: visual.weight,
    opacity: visual.opacity * opacityScale,
    fillOpacity: visual.fillOpacity * opacityScale,
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

function activeLowZoomTier() {
  const settings = config.lowZoomPerformance || {};
  if (!settings.enabled) {
    return null;
  }

  const zoom = map.getZoom();
  const tiers = Array.isArray(settings.tiers) && settings.tiers.length
    ? settings.tiers
    : [settings];
  return tiers
    .filter((tier) => zoom <= (tier.maxZoom || 7))
    .sort((a, b) => (a.maxZoom || 7) - (b.maxZoom || 7))[0] || null;
}

function featureSortKey(feature, fallbackIndex) {
  const coordinates = feature.geometry?.coordinates || [];
  const longitude = Number(coordinates[0]);
  const latitude = Number(coordinates[1]);
  return {
    latitude: Number.isFinite(latitude) ? latitude : Number.NEGATIVE_INFINITY,
    longitude: Number.isFinite(longitude) ? longitude : Number.POSITIVE_INFINITY,
    fallbackIndex,
  };
}

function selectDisplayFeatures(features) {
  if (state.activeSnapshotPrecomputed) {
    return {
      features,
      mode: `precomputed:${state.activeSnapshotUri}`,
    };
  }

  const tier = activeLowZoomTier();
  const sampleStride = Math.max(1, Number(tier?.sampleStride) || 1);
  if (!tier || sampleStride === 1) {
    return {
      features,
      mode: "full",
    };
  }

  const sampleOffset = Math.min(
    sampleStride - 1,
    Math.max(0, Number(tier.sampleOffset ?? Math.floor(sampleStride / 2)))
  );
  const orderedFeatures = features
    .map((feature, index) => ({ feature, sortKey: featureSortKey(feature, index) }))
    .sort((a, b) => (
      b.sortKey.latitude - a.sortKey.latitude ||
      a.sortKey.longitude - b.sortKey.longitude ||
      a.sortKey.fallbackIndex - b.sortKey.fallbackIndex
    ))
    .map((item) => item.feature);

  return {
    features: orderedFeatures.filter((_, index) => index % sampleStride === sampleOffset),
    mode: `low-sampled-${sampleStride}-${sampleOffset}`,
  };
}

function renderAoi(aoi) {
  const boundaryLayer = L.geoJSON(aoi, {
    pane: "aoiPane",
    style: {
      color: "#24392f",
      weight: 2.4,
      fillColor: "#ffffff",
      fillOpacity: 0.08,
      opacity: 0.9,
      pane: "aoiPane",
    },
  }).addTo(map);

  const labelLayer = L.layerGroup();
  aoi.features.forEach((feature) => {
    const name = feature.properties?.NAME || feature.properties?.NAMELSAD;
    const layer = L.geoJSON(feature);
    const center = layer.getBounds().getCenter();
    L.marker(center, {
      interactive: false,
      pane: "labelPane",
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

function buildRiskLayer(features) {
  return L.geoJSON(
    {
      type: "FeatureCollection",
      features,
    },
    {
      pane: "riskPane",
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
    }
  );
}

function setRiskLayerOpacityScale(layer, opacityScale) {
  layer.eachLayer((child) => {
    if (child.setStyle) {
      child.setStyle(pointStyle(child.feature, opacityScale));
    }
  });
}

function restyleRiskLayer() {
  if (state.riskLayer) {
    setRiskLayerOpacityScale(state.riskLayer, 1);
  }
}

function animateRiskLayerOpacity(layer, fromScale, toScale, durationMs, transitionId, onComplete) {
  const start = window.performance.now();

  function tick(now) {
    if (transitionId && transitionId !== state.riskTransitionId) {
      return;
    }

    const progress = Math.min(1, (now - start) / durationMs);
    const eased = 1 - Math.pow(1 - progress, 3);
    const opacityScale = fromScale + ((toScale - fromScale) * eased);
    setRiskLayerOpacityScale(layer, opacityScale);

    if (progress < 1) {
      window.requestAnimationFrame(tick);
      return;
    }

    if (onComplete) {
      onComplete();
    }
  }

  setRiskLayerOpacityScale(layer, fromScale);
  window.requestAnimationFrame(tick);
}

function replaceRiskLayer(features) {
  const previous = state.riskLayer;
  const next = buildRiskLayer(features);
  const transitionId = state.riskTransitionId + 1;
  state.riskTransitionId = transitionId;

  if (features.length > LARGE_LAYER_TRANSITION_THRESHOLD) {
    setRiskLayerOpacityScale(next, 1);
    next.addTo(map);
    state.riskLayer = next;
    if (previous) {
      map.removeLayer(previous);
    }
    return;
  }

  setRiskLayerOpacityScale(next, 0.15);
  next.addTo(map);
  state.riskLayer = next;

  animateRiskLayerOpacity(next, 0.15, 1, 150, transitionId);
  if (previous) {
    animateRiskLayerOpacity(previous, 1, 0, 150, null, () => {
      if (map.hasLayer(previous)) {
        map.removeLayer(previous);
      }
    });
  }
}

function renderActiveSnapshot({ force = false } = {}) {
  const features = state.activeSnapshot?.features || [];
  if (!features.length) {
    return false;
  }

  const display = selectDisplayFeatures(features);
  const signature = `${display.mode}:${display.features.length}`;
  if (!force && signature === state.renderSignature) {
    return false;
  }

  state.renderSignature = signature;
  state.renderedFeatureCount = display.features.length;
  replaceRiskLayer(display.features);
  return true;
}

function normalizeWindows(manifest) {
  const rawWindows = Array.isArray(manifest.windows) && manifest.windows.length
    ? manifest.windows
    : [
        {
          window_start_date: manifest.latest_window_start_date,
          geojson_uri: manifest.latest_geojson_uri,
          geojson_variants: manifest.latest_geojson_variants || {},
          model_version: manifest.model_version,
          updated_at: manifest.updated_at,
        },
      ];
  return rawWindows
    .filter((item) => item.window_start_date && item.geojson_uri)
    .map((item) => ({
      window_start_date: String(item.window_start_date),
      geojson_uri: item.geojson_uri,
      geojson_variants: item.geojson_variants || {},
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

function selectSnapshotSource(windowEntry) {
  const tier = activeLowZoomTier();
  const variantKey = tier?.variantKey;
  const variant = variantKey ? windowEntry.geojson_variants?.[variantKey] : null;
  const tierLabel = variantKey === "low"
    ? "z8"
    : variantKey === "medium"
      ? "z9"
      : tier
        ? `z<=${tier.maxZoom || 7}`
        : "full";
  if (variant?.geojson_uri) {
    return {
      uri: variant.geojson_uri,
      isPrecomputed: true,
      renderLabel: `${tierLabel} precomputed`,
    };
  }
  return {
    uri: windowEntry.geojson_uri,
    isPrecomputed: false,
    renderLabel: tier ? `${tierLabel} client fallback` : "full snapshot",
  };
}

async function loadSnapshot(source) {
  const url = resolveAssetUrl(source.uri);
  if (state.cache.has(url)) {
    const cached = state.cache.get(url);
    cacheSet(url, cached);
    return cached;
  }
  const payload = await fetchJson(url);
  cacheSet(url, payload);
  return payload;
}

function prefetchSnapshots(index, offsets = [-1, 1]) {
  offsets.forEach((offset) => {
    const candidate = index + offset;
    if (candidate < 0 || candidate >= state.windows.length) {
      return;
    }
    const source = selectSnapshotSource(state.windows[candidate]);
    const url = resolveAssetUrl(source.uri);
    if (!state.cache.has(url)) {
      loadSnapshot(source).catch(() => undefined);
    }
  });
}

function updateMetadata(manifest, aoi, riskGeojson, windowEntry) {
  const featureCount = riskGeojson.features?.length || 0;
  const riskCount = riskGeojson.features?.filter(
    (feature) => Number(feature.properties?.risk_probability ?? 0) >= 0.5
  ).length || 0;
  const renderedCount = state.renderedFeatureCount || featureCount;
  const renderedSuffix = renderedCount < featureCount ? `, ${renderedCount.toLocaleString()} rendered` : "";

  nodes.statusMeta.textContent = manifest.status || "live";
  nodes.windowDate.textContent = windowEntry.window_start_date || manifest.latest_window_start_date || "Unavailable";
  nodes.modelVersion.textContent = windowEntry.model_version || manifest.model_version || "Unavailable";
  nodes.featureCount.textContent = `${featureCount.toLocaleString()} (${riskCount.toLocaleString()} elevated${renderedSuffix})`;
  nodes.renderMode.textContent = state.activeSnapshotRenderLabel;
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
    const source = selectSnapshotSource(windowEntry);
    const riskGeojson = await loadSnapshot(source);
    state.activeSnapshot = riskGeojson;
    state.activeSnapshotUri = resolveAssetUrl(source.uri);
    state.activeSnapshotPrecomputed = source.isPrecomputed;
    state.activeSnapshotRenderLabel = source.renderLabel;
    state.renderSignature = "";
    renderActiveSnapshot({ force: true });
    updateMetadata(state.manifest, state.aoi, riskGeojson, windowEntry);
    const renderedCount = state.renderedFeatureCount || riskGeojson.features.length;
    const renderedSuffix = renderedCount < riskGeojson.features.length
      ? ` Rendering ${renderedCount.toLocaleString()} at this zoom.`
      : "";
    setStatus(`Loaded ${riskGeojson.features.length.toLocaleString()} risk features via ${source.renderLabel}.${renderedSuffix}`);
    if (prefetch) {
      prefetchSnapshots(boundedIndex);
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
  state.playbackLoading = false;
  nodes.playbackToggle.textContent = "Play";
  nodes.playbackToggle.setAttribute("aria-label", "Play timeline");
}

async function advancePlayback() {
  if (state.playbackLoading) {
    return;
  }
  state.playbackLoading = true;
  try {
    const nextIndex = (state.activeIndex + 1) % state.windows.length;
    await setActiveIndex(nextIndex, { prefetch: false });
    prefetchSnapshots(nextIndex, [1, 2]);
  } finally {
    state.playbackLoading = false;
  }
}

function startPlayback() {
  if (state.windows.length < 2 || state.playbackTimer) {
    return;
  }
  nodes.playbackToggle.textContent = "Pause";
  nodes.playbackToggle.setAttribute("aria-label", "Pause timeline");
  prefetchSnapshots(state.activeIndex, [1, 2]);
  advancePlayback();
  state.playbackTimer = window.setInterval(advancePlayback, config.playbackIntervalMs || 750);
}

function togglePlayback() {
  if (state.playbackTimer) {
    stopPlayback();
  } else {
    startPlayback();
  }
}

function bindCollapsiblePanels() {
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => {
    const toggle = panel.querySelector(".panel-toggle");
    const body = toggle ? document.getElementById(toggle.getAttribute("aria-controls")) : null;
    if (!toggle || !body) {
      return;
    }

    const setExpanded = (isExpanded) => {
      toggle.setAttribute("aria-expanded", String(isExpanded));
      body.hidden = !isExpanded;
    };

    setExpanded(toggle.getAttribute("aria-expanded") === "true");
    toggle.addEventListener("click", () => {
      setExpanded(toggle.getAttribute("aria-expanded") !== "true");
    });
  });
}

function bindControls() {
  bindCollapsiblePanels();
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
  map.on("zoomend", () => {
    const windowEntry = state.windows[state.activeIndex];
    const source = windowEntry ? selectSnapshotSource(windowEntry) : null;
    if (source && resolveAssetUrl(source.uri) !== state.activeSnapshotUri) {
      setActiveIndex(state.activeIndex);
      return;
    }
    const previousCount = state.renderedFeatureCount;
    const didRender = renderActiveSnapshot();
    if (!didRender) {
      restyleRiskLayer();
    }
    if (state.activeSnapshot && previousCount !== state.renderedFeatureCount) {
      updateMetadata(
        state.manifest,
        state.aoi,
        state.activeSnapshot,
        state.windows[state.activeIndex]
      );
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
