(function () {
  const useLiveManifest = window.OPENFIRE_USE_LIVE_MANIFEST ?? true;
  const uiPerformanceEnabled = window.OPENFIRE_UI_PERF_ENABLED ?? true;

  // Basemap style URL. Defaults to OpenFreeMap Positron (no key required,
  // CC-licensed). Override via OPENFIRE_BASEMAP_STYLE for MapTiler / Stadia
  // / custom styles. Examples:
  //   - "https://api.maptiler.com/maps/streets-v2/style.json?key=..."
  //   - "https://tiles.stadiamaps.com/styles/alidade_smooth.json?api_key=..."
  const basemapStyle =
    window.OPENFIRE_BASEMAP_STYLE ||
    "https://tiles.openfreemap.org/styles/positron";

  window.OPENFIRE_SOCAL_CONFIG = {
    manifestUrl: useLiveManifest ? "/data/manifest.json" : "./data/socal_demo_manifest.json",
    dataProxyPrefix: "/data/",
    demoManifestUrl: "./data/socal_demo_manifest.json",
    excludedWindowStartDates: ["2025-12-23", "2025-12-28"],
    playbackIntervalMs: 750,
    snapshotCacheSize: 8,
    basemapStyle,
    performanceTracking: {
      enabled: uiPerformanceEnabled,
      uiVariant: window.OPENFIRE_UI_VARIANT || "deckgl-scatterplot",
      appVersion: window.OPENFIRE_UI_VERSION || "socal-ui-deckgl",
      telemetryEndpoint: "/metrics/ui/performance",
      storageKey: "openfire-socal-deckgl-performance",
      webVitals: {
        enabled: window.OPENFIRE_WEB_VITALS_ENABLED ?? true,
        scriptUrl: window.OPENFIRE_WEB_VITALS_SCRIPT_URL || "",
      },
      faro: {
        enabled: Boolean(window.OPENFIRE_FARO_ENABLED ?? window.OPENFIRE_FARO_COLLECTOR_URL),
        collectorUrl: window.OPENFIRE_FARO_COLLECTOR_URL || "",
        appName: window.OPENFIRE_FARO_APP_NAME || "openfire-ui-socal",
      },
    },
    lowZoomPerformance: {
      enabled: false,
      tiers: [],
    },
    map: {
      // MapLibre uses [lon, lat] order (GeoJSON convention), unlike Leaflet.
      center: [-119.2, 35.2],
      zoom: 6.5,
      minZoom: 5,
      maxZoom: 16,
    },
    // Continuous color stops keyed at the same probability points the legend
    // gradient uses. Used by the deck.gl HeatmapLayer's colorRange.
    riskGradient: [
      [59, 143, 112, 0],
      [114, 169, 93, 200],
      [168, 189, 81, 220],
      [226, 184, 75, 230],
      [223, 145, 63, 235],
      [216, 100, 63, 240],
      [189, 63, 56, 245],
      [143, 36, 48, 250],
    ],
  };
})();
