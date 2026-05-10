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
  perfLatest: document.getElementById("perf-latest"),
  perfSnapshotLoad: document.getElementById("perf-snapshot-load"),
  perfRender: document.getElementById("perf-render"),
  perfPaint: document.getElementById("perf-paint"),
  perfP95Paint: document.getElementById("perf-p95-paint"),
  perfLongTasks: document.getElementById("perf-long-tasks"),
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
  performance: {
    samples: loadStoredPerformanceSamples(),
    longTaskCount: 0,
    longTaskTotalMs: 0,
    lastLongTaskMs: 0,
    longTaskObserver: null,
    telemetryQueue: [],
    telemetryFlushInFlight: false,
    telemetryTimer: null,
    sessionId: loadPerformanceSessionId(),
    faro: null,
    faroReady: false,
    faroMeasurementQueue: [],
  },
};

const scriptLoadPromises = new Map();

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

function performanceSettings() {
  return config.performanceTracking || {};
}

function isPerformanceTrackingEnabled() {
  return performanceSettings().enabled !== false;
}

function performanceNow() {
  return window.performance?.now ? window.performance.now() : Date.now();
}

function booleanSetting(value, fallback = false) {
  if (value === undefined || value === null) {
    return fallback;
  }
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    return ["1", "true", "yes"].includes(value.toLowerCase());
  }
  return Boolean(value);
}

function performanceStorageKey() {
  return performanceSettings().storageKey || "";
}

function webVitalsSettings() {
  return performanceSettings().webVitals || {};
}

function faroSettings() {
  return performanceSettings().faro || {};
}

function loadExternalScript(url, globalName) {
  if (!url) {
    return Promise.reject(new Error("Missing script URL."));
  }
  if (globalName && window[globalName]) {
    return Promise.resolve(window[globalName]);
  }
  if (scriptLoadPromises.has(url)) {
    return scriptLoadPromises.get(url);
  }

  const promise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.async = true;
    script.crossOrigin = "anonymous";
    script.onload = () => resolve(globalName ? window[globalName] : script);
    script.onerror = () => reject(new Error(`Failed to load script: ${url}`));
    script.src = url;
    document.head.appendChild(script);
  });
  scriptLoadPromises.set(url, promise);
  return promise;
}

function performanceSessionStorageKey() {
  return `${performanceStorageKey() || "openfire-socal-ui-performance"}:session`;
}

function randomSessionId() {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  if (window.crypto?.getRandomValues) {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
    return [
      hex.slice(0, 4).join(""),
      hex.slice(4, 6).join(""),
      hex.slice(6, 8).join(""),
      hex.slice(8, 10).join(""),
      hex.slice(10, 16).join(""),
    ].join("-");
  }
  return `session-${Date.now()}`;
}

function loadPerformanceSessionId() {
  if (!isPerformanceTrackingEnabled()) {
    return "";
  }

  const key = performanceSessionStorageKey();
  try {
    const existing = window.sessionStorage?.getItem(key);
    if (existing) {
      return existing;
    }
    const created = randomSessionId();
    window.sessionStorage?.setItem(key, created);
    return created;
  } catch (error) {
    return randomSessionId();
  }
}

function loadStoredPerformanceSamples() {
  if (!isPerformanceTrackingEnabled() || !performanceStorageKey()) {
    return [];
  }

  try {
    const raw = window.sessionStorage?.getItem(performanceStorageKey());
    const samples = raw ? JSON.parse(raw) : [];
    return Array.isArray(samples) ? samples : [];
  } catch (error) {
    return [];
  }
}

function persistPerformanceSamples() {
  if (!isPerformanceTrackingEnabled() || !performanceStorageKey()) {
    return;
  }

  try {
    window.sessionStorage?.setItem(
      performanceStorageKey(),
      JSON.stringify(state.performance.samples)
    );
  } catch (error) {
    // Session storage can be unavailable in private or embedded contexts.
  }
}

function formatDuration(ms) {
  if (ms === null || ms === undefined) {
    return "n/a";
  }
  const numeric = Number(ms);
  if (!Number.isFinite(numeric)) {
    return "n/a";
  }
  if (numeric < 1000) {
    return `${Math.round(numeric)} ms`;
  }
  return `${(numeric / 1000).toFixed(2)} s`;
}

function formatFeatureCount(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toLocaleString() : "0";
}

function percentile(values, percentileRank) {
  const numericValues = values
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value))
    .sort((a, b) => a - b);
  if (!numericValues.length) {
    return null;
  }

  const index = Math.ceil(percentileRank * numericValues.length) - 1;
  return numericValues[Math.max(0, Math.min(index, numericValues.length - 1))];
}

function summarizePerformanceSamples(samples = state.performance.samples) {
  const paintValues = samples.map((sample) => sample.paintReadyMs);
  const renderValues = samples.map((sample) => sample.renderSyncMs);
  const loadValues = samples
    .filter((sample) => !sample.cacheHit)
    .map((sample) => sample.snapshotLoadMs);

  return {
    sampleCount: samples.length,
    p95PaintMs: percentile(paintValues, 0.95),
    p95RenderMs: percentile(renderValues, 0.95),
    p95SnapshotLoadMs: percentile(loadValues, 0.95),
    longTaskCount: state.performance.longTaskCount,
    longTaskTotalMs: state.performance.longTaskTotalMs,
    lastSample: samples[samples.length - 1] || null,
  };
}

function updatePerformancePanel() {
  if (!isPerformanceTrackingEnabled() || !nodes.perfLatest) {
    return;
  }

  const samples = state.performance.samples;
  const latest = samples[samples.length - 1];
  if (!latest) {
    nodes.perfLatest.textContent = "Waiting...";
    nodes.perfSnapshotLoad.textContent = "Waiting...";
    nodes.perfRender.textContent = "Waiting...";
    nodes.perfPaint.textContent = "Waiting...";
    nodes.perfP95Paint.textContent = "Waiting...";
    nodes.perfLongTasks.textContent = `${state.performance.longTaskCount}`;
    return;
  }

  const summary = summarizePerformanceSamples(samples);
  const loadLabel = latest.cacheHit
    ? "cache"
    : formatDuration(latest.snapshotLoadMs);
  nodes.perfLatest.textContent = `${latest.action} ${latest.windowStartDate || ""} z${latest.zoom}`;
  nodes.perfSnapshotLoad.textContent = loadLabel;
  nodes.perfRender.textContent = `${formatDuration(latest.renderSyncMs)} (${formatFeatureCount(latest.renderedFeatureCount)} pts)`;
  nodes.perfPaint.textContent = formatDuration(latest.paintReadyMs);
  nodes.perfP95Paint.textContent = `${formatDuration(summary.p95PaintMs)} / ${summary.sampleCount}`;
  nodes.perfLongTasks.textContent = `${state.performance.longTaskCount} (${formatDuration(state.performance.longTaskTotalMs)})`;
}

function isSlowPerformanceSample(sample) {
  const settings = performanceSettings();
  return (
    Number(sample.paintReadyMs) >= (settings.slowPaintMs || 1000) ||
    Number(sample.renderSyncMs) >= (settings.slowRenderMs || 300) ||
    (!sample.cacheHit && Number(sample.snapshotLoadMs) >= (settings.slowSnapshotLoadMs || 1500))
  );
}

function recordPerformanceSample(sample) {
  if (!isPerformanceTrackingEnabled()) {
    return;
  }

  const settings = performanceSettings();
  const sampleLimit = Math.max(1, Number(settings.sampleLimit) || 80);
  const enrichedSample = {
    timestamp: new Date().toISOString(),
    ...sample,
  };

  state.performance.samples.push(enrichedSample);
  while (state.performance.samples.length > sampleLimit) {
    state.performance.samples.shift();
  }

  persistPerformanceSamples();
  updatePerformancePanel();
  enqueuePerformanceTelemetry(enrichedSample);
  pushFaroMeasurement(enrichedSample);

  if (booleanSetting(settings.logSamples, false)) {
    const log = isSlowPerformanceSample(enrichedSample) ? console.warn : console.debug;
    if (log) {
      log.call(console, "[OpenFire UI perf]", enrichedSample);
    }
  }
}

function schedulePerformanceSample(sample) {
  if (!isPerformanceTrackingEnabled()) {
    return;
  }

  const scheduledAt = performanceNow();
  const interactionStartedAt = sample.interactionStartedAt || scheduledAt;
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      const paintedAt = performanceNow();
      recordPerformanceSample({
        ...sample,
        paintReadyMs: paintedAt - interactionStartedAt,
        frameWaitMs: paintedAt - scheduledAt,
      });
    });
  });
}

function telemetryEndpoint() {
  return performanceSettings().telemetryEndpoint || "";
}

function telemetryBatchSize() {
  return Math.max(1, Number(performanceSettings().telemetryBatchSize) || 20);
}

function telemetryEvent(sample) {
  return {
    action: sample.action,
    timestamp: sample.timestamp,
    window_start_date: sample.windowStartDate,
    zoom: sample.zoom,
    source_label: sample.sourceLabel,
    source_url: sample.sourceUrl,
    display_mode: sample.displayMode,
    cache_hit: sample.cacheHit,
    total_feature_count: sample.totalFeatureCount,
    rendered_feature_count: sample.renderedFeatureCount,
    snapshot_load_ms: sample.snapshotLoadMs,
    render_sync_ms: sample.renderSyncMs,
    select_ms: sample.selectMs,
    layer_swap_ms: sample.layerSwapMs,
    paint_ready_ms: sample.paintReadyMs,
    frame_wait_ms: sample.frameWaitMs,
    long_task_count: state.performance.longTaskCount,
    long_task_total_ms: state.performance.longTaskTotalMs,
    web_vital_name: sample.webVitalName,
    web_vital_id: sample.webVitalId,
    web_vital_value: sample.webVitalValue,
    web_vital_delta: sample.webVitalDelta,
    web_vital_rating: sample.webVitalRating,
    web_vital_navigation_type: sample.webVitalNavigationType,
    error_type: sample.errorType,
    error_message: sample.errorMessage,
    error_source: sample.errorSource,
    error_line: sample.errorLine,
    error_column: sample.errorColumn,
    error_stack_hash: sample.errorStackHash,
  };
}

function telemetryPayload(events) {
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection || {};
  const settings = performanceSettings();
  return {
    schema_version: "ui_performance_v1",
    session_id: state.performance.sessionId,
    ui_variant: settings.uiVariant || "unknown",
    app_version: settings.appVersion || "unknown",
    page_path: window.location.pathname || "/",
    viewport_width: window.innerWidth,
    viewport_height: window.innerHeight,
    device_pixel_ratio: window.devicePixelRatio || 1,
    hardware_concurrency: navigator.hardwareConcurrency || null,
    device_memory_gb: navigator.deviceMemory || null,
    connection_effective_type: connection.effectiveType || null,
    save_data: Boolean(connection.saveData),
    events,
  };
}

function finiteNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function truncateText(value, maxLength) {
  const text = String(value || "");
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, Math.max(0, maxLength - 3))}...`;
}

function hashText(value) {
  const text = String(value || "");
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function errorDetails(errorLike) {
  if (errorLike instanceof Error) {
    return {
      type: errorLike.name || "Error",
      message: errorLike.message || "Unhandled error",
      stack: errorLike.stack || "",
    };
  }
  if (typeof errorLike === "string") {
    return { type: "Error", message: errorLike, stack: "" };
  }
  return {
    type: errorLike?.name || "Error",
    message: errorLike?.message || String(errorLike || "Unhandled error"),
    stack: errorLike?.stack || "",
  };
}

function recordBrowserError(details) {
  if (!isPerformanceTrackingEnabled()) {
    return;
  }

  enqueuePerformanceTelemetry({
    action: "browser-error",
    timestamp: new Date().toISOString(),
    windowStartDate: state.windows[state.activeIndex]?.window_start_date || null,
    zoom: map.getZoom(),
    sourceLabel: details.kind || "browser-error",
    errorType: truncateText(details.type, 128),
    errorMessage: truncateText(details.message, 512),
    errorSource: truncateText(details.source, 2048),
    errorLine: Number.isFinite(details.line) ? details.line : null,
    errorColumn: Number.isFinite(details.column) ? details.column : null,
    errorStackHash: details.stack ? hashText(details.stack) : null,
  });
}

function buildFaroMeasurement(sample) {
  const values = {
    snapshot_load_ms: finiteNumber(sample.snapshotLoadMs),
    render_sync_ms: finiteNumber(sample.renderSyncMs),
    select_ms: finiteNumber(sample.selectMs),
    layer_swap_ms: finiteNumber(sample.layerSwapMs),
    paint_ready_ms: finiteNumber(sample.paintReadyMs),
    frame_wait_ms: finiteNumber(sample.frameWaitMs),
    total_feature_count: finiteNumber(sample.totalFeatureCount),
    rendered_feature_count: finiteNumber(sample.renderedFeatureCount),
    zoom: finiteNumber(sample.zoom),
    long_task_count: finiteNumber(state.performance.longTaskCount),
    long_task_total_ms: finiteNumber(state.performance.longTaskTotalMs),
  };

  Object.keys(values).forEach((key) => {
    if (values[key] === null) {
      delete values[key];
    }
  });

  return {
    type: "openfire_ui_interaction",
    values,
    context: {
      action: sample.action || "unknown",
      ui_variant: performanceSettings().uiVariant || "unknown",
      app_version: performanceSettings().appVersion || "unknown",
      window_start_date: sample.windowStartDate || "unknown",
      source_label: sample.sourceLabel || "unknown",
      display_mode: sample.displayMode || "unknown",
      cache_hit: String(Boolean(sample.cacheHit)),
    },
  };
}

function pushFaroMeasurement(sample) {
  const settings = faroSettings();
  if (!booleanSetting(settings.enabled, false) || !settings.collectorUrl) {
    return;
  }

  const measurement = buildFaroMeasurement(sample);
  if (!Object.keys(measurement.values).length) {
    return;
  }

  if (!state.performance.faroReady || !state.performance.faro?.api?.pushMeasurement) {
    state.performance.faroMeasurementQueue.push(measurement);
    while (state.performance.faroMeasurementQueue.length > 50) {
      state.performance.faroMeasurementQueue.shift();
    }
    return;
  }

  state.performance.faro.api.pushMeasurement(
    { type: measurement.type, values: measurement.values },
    { context: measurement.context }
  );
}

function flushFaroMeasurements() {
  if (!state.performance.faroReady || !state.performance.faro?.api?.pushMeasurement) {
    return;
  }

  const queued = state.performance.faroMeasurementQueue.splice(0);
  queued.forEach((measurement) => {
    state.performance.faro.api.pushMeasurement(
      { type: measurement.type, values: measurement.values },
      { context: measurement.context }
    );
  });
}

function enqueuePerformanceTelemetry(sample) {
  if (!isPerformanceTrackingEnabled() || !telemetryEndpoint() || !state.performance.sessionId) {
    return;
  }

  state.performance.telemetryQueue.push(telemetryEvent(sample));
  if (state.performance.telemetryQueue.length >= telemetryBatchSize()) {
    flushPerformanceTelemetry();
  }
}

function removeFlushedTelemetryEvents(count) {
  state.performance.telemetryQueue.splice(0, count);
}

async function flushPerformanceTelemetry({ useBeacon = false } = {}) {
  if (
    !isPerformanceTrackingEnabled() ||
    !telemetryEndpoint() ||
    !state.performance.telemetryQueue.length ||
    state.performance.telemetryFlushInFlight ||
    window.location.protocol === "file:"
  ) {
    return;
  }

  const batch = state.performance.telemetryQueue.slice(0, telemetryBatchSize());
  const payload = JSON.stringify(telemetryPayload(batch));

  if (useBeacon && navigator.sendBeacon) {
    const accepted = navigator.sendBeacon(
      telemetryEndpoint(),
      new Blob([payload], { type: "application/json" })
    );
    if (accepted) {
      removeFlushedTelemetryEvents(batch.length);
    }
    return;
  }

  state.performance.telemetryFlushInFlight = true;
  try {
    const response = await fetch(telemetryEndpoint(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      keepalive: true,
    });
    if (response.ok) {
      removeFlushedTelemetryEvents(batch.length);
    }
  } catch (error) {
    // Keep queued events for the next interval.
  } finally {
    state.performance.telemetryFlushInFlight = false;
  }
}

function bindPerformanceTelemetry() {
  if (!isPerformanceTrackingEnabled() || !telemetryEndpoint()) {
    return;
  }

  const intervalMs = Math.max(1000, Number(performanceSettings().telemetryFlushIntervalMs) || 10000);
  state.performance.telemetryTimer = window.setInterval(() => {
    flushPerformanceTelemetry();
  }, intervalMs);

  window.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      flushPerformanceTelemetry({ useBeacon: true });
    }
  });
  window.addEventListener("pagehide", () => {
    flushPerformanceTelemetry({ useBeacon: true });
  });
}

function bindPerformanceObservers() {
  if (!isPerformanceTrackingEnabled() || !("PerformanceObserver" in window)) {
    return;
  }

  try {
    const observer = new PerformanceObserver((list) => {
      const threshold = Number(performanceSettings().longTaskMs) || 50;
      list.getEntries().forEach((entry) => {
        if (entry.duration < threshold) {
          return;
        }
        state.performance.longTaskCount += 1;
        state.performance.longTaskTotalMs += entry.duration;
        state.performance.lastLongTaskMs = entry.duration;
      });
      updatePerformancePanel();
    });
    observer.observe({ entryTypes: ["longtask"] });
    state.performance.longTaskObserver = observer;
  } catch (error) {
    state.performance.longTaskObserver = null;
  }
}

function initializeFaroTelemetry() {
  const settings = faroSettings();
  if (
    !isPerformanceTrackingEnabled() ||
    !booleanSetting(settings.enabled, false) ||
    !settings.collectorUrl
  ) {
    return;
  }

  loadExternalScript(settings.scriptUrl, "GrafanaFaroWebSdk")
    .then((sdk) => {
      if (!sdk?.initializeFaro) {
        throw new Error("Grafana Faro SDK did not expose initializeFaro.");
      }
      state.performance.faro = sdk.initializeFaro({
        url: settings.collectorUrl,
        app: {
          name: settings.appName || "openfire-ui-socal",
          version: settings.appVersion || performanceSettings().appVersion || "socal-ui",
          namespace: settings.appNamespace || "openfire",
          environment: settings.environment || "production",
        },
      });
      state.performance.faroReady = true;
      flushFaroMeasurements();
    })
    .catch((error) => {
      state.performance.faroReady = false;
      console.warn("[OpenFire UI perf] Grafana Faro disabled:", error);
    });
}

function recordWebVitalMetric(metric) {
  if (!metric?.name) {
    return;
  }

  const windowEntry = state.windows[state.activeIndex] || {};
  enqueuePerformanceTelemetry({
    action: "web-vital",
    timestamp: new Date().toISOString(),
    windowStartDate: windowEntry.window_start_date || null,
    zoom: map.getZoom(),
    sourceLabel: "web-vitals",
    webVitalName: metric.name,
    webVitalId: metric.id,
    webVitalValue: metric.value,
    webVitalDelta: metric.delta,
    webVitalRating: metric.rating,
    webVitalNavigationType: metric.navigationType,
  });
}

function bindWebVitals() {
  const settings = webVitalsSettings();
  if (!isPerformanceTrackingEnabled() || !booleanSetting(settings.enabled, true)) {
    return;
  }

  loadExternalScript(settings.scriptUrl, "webVitals")
    .then((webVitals) => {
      ["onCLS", "onFCP", "onINP", "onLCP", "onTTFB"].forEach((methodName) => {
        if (typeof webVitals?.[methodName] === "function") {
          webVitals[methodName](recordWebVitalMetric);
        }
      });
    })
    .catch((error) => {
      console.warn("[OpenFire UI perf] Web Vitals disabled:", error);
    });
}

function bindBrowserErrorTelemetry() {
  if (!isPerformanceTrackingEnabled()) {
    return;
  }

  window.addEventListener("error", (event) => {
    const detail = errorDetails(event.error || event.message);
    recordBrowserError({
      kind: "window-error",
      type: detail.type,
      message: detail.message,
      stack: detail.stack,
      source: event.filename || "",
      line: event.lineno,
      column: event.colno,
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    const detail = errorDetails(event.reason);
    recordBrowserError({
      kind: "unhandledrejection",
      type: detail.type,
      message: detail.message,
      stack: detail.stack,
      source: "promise",
      line: null,
      column: null,
    });
  });
}

function installPerformanceExport() {
  if (!isPerformanceTrackingEnabled()) {
    return;
  }

  window.OPENFIRE_SOCAL_PERFORMANCE = {
    getSamples() {
      return [...state.performance.samples];
    },
    getSummary() {
      return summarizePerformanceSamples();
    },
    getPendingTelemetry() {
      return [...state.performance.telemetryQueue];
    },
    getFaroStatus() {
      return {
        enabled: booleanSetting(faroSettings().enabled, false) && Boolean(faroSettings().collectorUrl),
        ready: state.performance.faroReady,
        queuedMeasurements: state.performance.faroMeasurementQueue.length,
      };
    },
    flushTelemetry() {
      return flushPerformanceTelemetry();
    },
    clear() {
      state.performance.samples = [];
      state.performance.telemetryQueue = [];
      state.performance.longTaskCount = 0;
      state.performance.longTaskTotalMs = 0;
      state.performance.lastLongTaskMs = 0;
      persistPerformanceSamples();
      updatePerformancePanel();
    },
  };
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
  const start = performanceNow();

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

function renderActiveSnapshot({ force = false, perfContext = null } = {}) {
  const features = state.activeSnapshot?.features || [];
  if (!features.length) {
    return false;
  }

  const renderStartedAt = performanceNow();
  const selectStartedAt = renderStartedAt;
  const display = selectDisplayFeatures(features);
  const selectMs = performanceNow() - selectStartedAt;
  const signature = `${display.mode}:${display.features.length}`;
  if (!force && signature === state.renderSignature) {
    return false;
  }

  state.renderSignature = signature;
  state.renderedFeatureCount = display.features.length;
  const layerStartedAt = performanceNow();
  replaceRiskLayer(display.features);
  const layerSwapMs = performanceNow() - layerStartedAt;
  const renderSyncMs = performanceNow() - renderStartedAt;

  if (perfContext) {
    schedulePerformanceSample({
      ...perfContext,
      displayMode: display.mode,
      totalFeatureCount: features.length,
      renderedFeatureCount: display.features.length,
      selectMs,
      layerSwapMs,
      renderSyncMs,
    });
  }

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
  const excludedWindowStartDates = new Set(config.excludedWindowStartDates || []);
  return rawWindows
    .filter((item) => item.window_start_date && item.geojson_uri)
    .filter((item) => !excludedWindowStartDates.has(String(item.window_start_date)))
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
  const startedAt = performanceNow();
  if (state.cache.has(url)) {
    const cached = state.cache.get(url);
    cacheSet(url, cached);
    return {
      payload: cached,
      metrics: {
        sourceUrl: url,
        cacheHit: true,
        snapshotLoadMs: 0,
        snapshotFeatureCount: cached.features?.length || 0,
      },
    };
  }
  const payload = await fetchJson(url);
  cacheSet(url, payload);
  return {
    payload,
    metrics: {
      sourceUrl: url,
      cacheHit: false,
      snapshotLoadMs: performanceNow() - startedAt,
      snapshotFeatureCount: payload.features?.length || 0,
    },
  };
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
  const interactionStartedAt = performanceNow();
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
    const snapshot = await loadSnapshot(source);
    const riskGeojson = snapshot.payload;
    state.activeSnapshot = riskGeojson;
    state.activeSnapshotUri = snapshot.metrics.sourceUrl;
    state.activeSnapshotPrecomputed = source.isPrecomputed;
    state.activeSnapshotRenderLabel = source.renderLabel;
    state.renderSignature = "";
    renderActiveSnapshot({
      force: true,
      perfContext: {
        action: "snapshot",
        interactionStartedAt,
        windowStartDate: windowEntry.window_start_date,
        zoom: map.getZoom(),
        sourceLabel: source.renderLabel,
        sourceUrl: snapshot.metrics.sourceUrl,
        cacheHit: snapshot.metrics.cacheHit,
        snapshotLoadMs: snapshot.metrics.snapshotLoadMs,
      },
    });
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
    const interactionStartedAt = performanceNow();
    const windowEntry = state.windows[state.activeIndex];
    const source = windowEntry ? selectSnapshotSource(windowEntry) : null;
    if (source && resolveAssetUrl(source.uri) !== state.activeSnapshotUri) {
      setActiveIndex(state.activeIndex);
      return;
    }
    const previousCount = state.renderedFeatureCount;
    const perfContext = state.activeSnapshot && windowEntry
      ? {
          action: "zoom",
          interactionStartedAt,
          windowStartDate: windowEntry.window_start_date,
          zoom: map.getZoom(),
          sourceLabel: state.activeSnapshotRenderLabel,
          sourceUrl: state.activeSnapshotUri,
          cacheHit: true,
          snapshotLoadMs: 0,
        }
      : null;
    const didRender = renderActiveSnapshot({ perfContext });
    if (!didRender) {
      const restyleStartedAt = performanceNow();
      restyleRiskLayer();
      if (perfContext) {
        const renderSyncMs = performanceNow() - restyleStartedAt;
        schedulePerformanceSample({
          ...perfContext,
          action: "zoom-style",
          displayMode: state.renderSignature || "unchanged",
          totalFeatureCount: state.activeSnapshot?.features?.length || 0,
          renderedFeatureCount: state.renderedFeatureCount,
          selectMs: 0,
          layerSwapMs: renderSyncMs,
          renderSyncMs,
        });
      }
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
  installPerformanceExport();
  bindPerformanceObservers();
  initializeFaroTelemetry();
  bindWebVitals();
  bindBrowserErrorTelemetry();
  bindPerformanceTelemetry();
  updatePerformancePanel();
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
