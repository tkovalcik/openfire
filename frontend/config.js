const backendBaseUrl =
  window.OPENFIRE_BACKEND_BASE_URL ||
  new URLSearchParams(window.location.search).get("backend") ||
  "http://127.0.0.1:8000";

window.OPENFIRE_CONFIG = {
  runtimeMode: "demo",
  backendBaseUrl,
  map: {
    center: [37.25, -119.75],
    zoom: 6,
    minZoom: 5,
  },
  riskBands: [
    { min: 0.0, max: 0.25, label: "Low (0.00-0.25)", color: "#f4e3a3" },
    { min: 0.25, max: 0.5, label: "Moderate (0.25-0.50)", color: "#f6a04d" },
    { min: 0.5, max: 0.75, label: "High (0.50-0.75)", color: "#d65438" },
    { min: 0.75, max: 1.01, label: "Very high (0.75-1.00)", color: "#7f1d1d" },
  ],
  sources: {
    demo: {
      // Demo mode uses the backend frozen-GeoJSON endpoint so the map can load a known-good layer
      // from GCS or local fallback storage without making the browser understand gs:// URIs.
      mode: "api-geojson",
      url: `${backendBaseUrl}/demo/geojson`,
      metadataUrl: `${backendBaseUrl}/metadata`,
      requestBodyUrl: "./data/sample_predict_request.json",
      fallbackInferenceDate: "2026-04-01",
      fallbackDataWindow: "2024-06-01 to 2024-06-30",
    },
    live: {
      // Live mode uses the API-backed prediction flow against the currently loaded serving model.
      mode: "predict-geojson",
      url: `${backendBaseUrl}/predict_geojson`,
      metadataUrl: `${backendBaseUrl}/metadata`,
      requestBodyUrl: "./data/sample_predict_request.json",
      fallbackInferenceDate: "2026-04-01",
      fallbackDataWindow: "2024-06-01 to 2024-06-30",
    },
  },
};
