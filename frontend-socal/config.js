(function () {
  const useLiveManifest = window.OPENFIRE_USE_LIVE_MANIFEST ?? true;

  window.OPENFIRE_SOCAL_CONFIG = {
    manifestUrl: useLiveManifest ? "/data/manifest.json" : "./data/socal_demo_manifest.json",
    dataProxyPrefix: "/data/",
    demoManifestUrl: "./data/socal_demo_manifest.json",
    excludedWindowStartDates: ["2025-12-23", "2025-12-28"],
    playbackIntervalMs: 750,
    snapshotCacheSize: 8,
    performanceTracking: {
      enabled: true,
      uiVariant: window.OPENFIRE_UI_VARIANT || "leaflet-canvas",
      appVersion: window.OPENFIRE_UI_VERSION || "socal-ui",
      telemetryEndpoint: "/metrics/ui/performance",
      telemetryFlushIntervalMs: 10000,
      telemetryBatchSize: 20,
      sampleLimit: 80,
      slowPaintMs: 1000,
      slowRenderMs: 300,
      slowSnapshotLoadMs: 1500,
      longTaskMs: 50,
      logSamples: window.OPENFIRE_PERF_LOG_SAMPLES ?? false,
      storageKey: "openfire-socal-ui-performance",
      webVitals: {
        enabled: window.OPENFIRE_WEB_VITALS_ENABLED ?? true,
        scriptUrl: window.OPENFIRE_WEB_VITALS_SCRIPT_URL || "https://unpkg.com/web-vitals@5/dist/web-vitals.iife.js",
      },
      faro: {
        enabled: Boolean(window.OPENFIRE_FARO_ENABLED ?? window.OPENFIRE_FARO_COLLECTOR_URL),
        collectorUrl: window.OPENFIRE_FARO_COLLECTOR_URL || "",
        scriptUrl: window.OPENFIRE_FARO_SCRIPT_URL || "https://unpkg.com/@grafana/faro-web-sdk@^1.0.0/dist/bundle/faro-web-sdk.iife.js",
        appName: window.OPENFIRE_FARO_APP_NAME || "openfire-ui-socal",
        appVersion: window.OPENFIRE_FARO_APP_VERSION || window.OPENFIRE_UI_VERSION || "socal-ui",
        appNamespace: window.OPENFIRE_FARO_APP_NAMESPACE || "openfire",
        environment: window.OPENFIRE_FARO_ENVIRONMENT || "production",
      },
    },
    lowZoomPerformance: {
      enabled: true,
      tiers: [
        { maxZoom: 7, sampleStride: 6, sampleOffset: 2, variantKey: "low" },
        { maxZoom: 9, sampleStride: 3, sampleOffset: 1, variantKey: "medium" },
      ],
    },
    map: {
      center: [35.2, -119.2],
      zoom: 7,
      minZoom: 6,
    },
    riskBands: [
      { min: 0.0, max: 0.125, label: "Model risk 0.000-0.125", color: "#3b8f70" },
      { min: 0.125, max: 0.25, label: "Model risk 0.125-0.250", color: "#72a95d" },
      { min: 0.25, max: 0.375, label: "Model risk 0.250-0.375", color: "#a8bd51" },
      { min: 0.375, max: 0.5, label: "Model risk 0.375-0.500", color: "#e2b84b" },
      { min: 0.5, max: 0.625, label: "Model risk 0.500-0.625", color: "#df913f" },
      { min: 0.625, max: 0.75, label: "Model risk 0.625-0.750", color: "#d8643f" },
      { min: 0.75, max: 0.875, label: "Model risk 0.750-0.875", color: "#bd3f38" },
      { min: 0.875, max: 1.01, label: "Model risk 0.875-1.000", color: "#8f2430" },
    ],
  };
})();
