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
    // Continuous color stops used by the deck.gl HeatmapLayer's colorRange.
    // Stop 0 is visibly green so 0% risk reads as "safe / mapped, just not
    // at risk" rather than disappearing. Combined with colorDomain=[0,0.7]
    // in app.js, this spreads the gradient over the realistic risk range
    // (~99% of cells are ≤70% risk) so users see the full green→red ramp
    // rather than only green/yellow.
    riskGradient: [
      [59, 143, 112, 215],
      [114, 169, 93, 220],
      [168, 189, 81, 228],
      [226, 184, 75, 235],
      [223, 145, 63, 240],
      [216, 100, 63, 245],
      [189, 63, 56, 248],
      [143, 36, 48, 252],
    ],
  };
})();
