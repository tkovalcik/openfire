(function () {
  const useLiveManifest = window.OPENFIRE_USE_LIVE_MANIFEST ?? true;

  window.OPENFIRE_SOCAL_CONFIG = {
    manifestUrl: useLiveManifest ? "/data/manifest.json" : "./data/socal_demo_manifest.json",
    dataProxyPrefix: "/data/",
    demoManifestUrl: "./data/socal_demo_manifest.json",
    excludedWindowStartDates: ["2025-12-23", "2025-12-28"],
    playbackIntervalMs: 750,
    snapshotCacheSize: 8,
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
