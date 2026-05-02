(function () {
  const useLiveManifest = window.OPENFIRE_USE_LIVE_MANIFEST ?? true;

  window.OPENFIRE_SOCAL_CONFIG = {
    manifestUrl: useLiveManifest ? "/data/manifest.json" : "./data/socal_demo_manifest.json",
    dataProxyPrefix: "/data/",
    demoManifestUrl: "./data/socal_demo_manifest.json",
    playbackIntervalMs: 750,
    snapshotCacheSize: 8,
    lowZoomPerformance: {
      enabled: true,
      tiers: [
        { maxZoom: 8, sampleStride: 6, sampleOffset: 2 },
        { maxZoom: 9, sampleStride: 3, sampleOffset: 1 },
      ],
    },
    map: {
      center: [35.2, -119.2],
      zoom: 7,
      minZoom: 6,
    },
    riskBands: [
      { min: 0.0, max: 0.25, label: "Low (0.00-0.25)", color: "#3b8f70" },
      { min: 0.25, max: 0.5, label: "Moderate (0.25-0.50)", color: "#e2b84b" },
      { min: 0.5, max: 0.75, label: "High (0.50-0.75)", color: "#d8643f" },
      { min: 0.75, max: 1.01, label: "Very high (0.75-1.00)", color: "#8f2430" },
    ],
  };
})();
