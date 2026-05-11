// OpenFire SoCal UI — MapLibre GL + deck.gl integration.
//
// Architecture: MapLibre GL renders the basemap as WebGL vector tiles in
// a single canvas. deck.gl's MapboxOverlay registers itself with MapLibre
// and renders into the SAME WebGL context. Both layers share the camera,
// so zoom/pan are GPU-accelerated and visually seamless — the heatmap
// follows the basemap pixel-perfect at every animation frame, no fade
// tricks, no offset glitches.

const config = window.OPENFIRE_SOCAL_CONFIG;
const deckApi = window.deck || {};

// MapLibre uses [lon, lat] (GeoJSON convention).
const map = new maplibregl.Map({
  container: "map",
  style: config.basemapStyle,
  center: config.map.center,
  zoom: config.map.zoom,
  minZoom: config.map.minZoom,
  maxZoom: config.map.maxZoom,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
if (typeof window !== "undefined") { window.__map = map; }

// ── Overlay basemap (AOI clip trick) ──────────────────────────────────
// A second MapLibre instance rendered above #map (which itself holds
// the deck.gl heatmap via interleaved MapboxOverlay). The overlay's CSS
// clip-path is set to "outside AOI" — so the duplicate basemap paints
// the rest of the world, but the heatmap underneath shows through INSIDE
// the AOI. The basemap detail (roads, labels, parks) is preserved
// everywhere because the overlay IS a real basemap, not a flat fill.
const overlayMap = new maplibregl.Map({
  container: "map-clip-overlay",
  style: config.basemapStyle,
  center: map.getCenter(),
  zoom: map.getZoom(),
  bearing: map.getBearing(),
  pitch: map.getPitch(),
  minZoom: config.map.minZoom,
  maxZoom: config.map.maxZoom,
  interactive: false, // primary map handles all input
  attributionControl: false,
});
if (typeof window !== "undefined") { window.__overlayMap = overlayMap; }

// Bidirectional sync guard: when we copy state from primary → overlay we
// don't want overlay's "move" event (if any) to fire back.
let _syncingOverlay = false;
function syncOverlayCameraFromPrimary() {
  if (_syncingOverlay) return;
  _syncingOverlay = true;
  overlayMap.jumpTo({
    center: map.getCenter(),
    zoom: map.getZoom(),
    bearing: map.getBearing(),
    pitch: map.getPitch(),
  });
  _syncingOverlay = false;
}
// Sync on every render frame of the primary map so animation interpolation
// stays pixel-perfect. `move` fires throughout zoom/pan animations.
map.on("move", syncOverlayCameraFromPrimary);
map.on("zoom", syncOverlayCameraFromPrimary);
map.on("rotate", syncOverlayCameraFromPrimary);
map.on("pitch", syncOverlayCameraFromPrimary);
map.on("resize", () => {
  overlayMap.resize();
  syncOverlayCameraFromPrimary();
});

// ── AOI clip-path updater ─────────────────────────────────────────────
// Recompute the SVG <clipPath> path string from state.aoi on every move
// event. The path is "outer rect (CCW) + AOI polygons (CW)" with
// fill-rule=evenodd, which produces the inverse-AOI shape — the overlay
// basemap renders everywhere EXCEPT inside the AOI. Coordinates are in
// screen-pixel space (clipPathUnits=userSpaceOnUse).
const _clipPathEl = () => document.getElementById("openfire-aoi-clip-path");
const _clipSvgEl = () => document.getElementById("openfire-aoi-clip-svg");

function _ringToSvg(coords) {
  // coords: array of [lon, lat]. Returns "M x,y L x,y ... Z" in screen px.
  if (!coords || coords.length === 0) return "";
  let out = "";
  for (let i = 0; i < coords.length; i += 1) {
    const p = map.project(coords[i]);
    out += `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)} `;
  }
  return out + "Z ";
}

let _clipUpdatePending = false;
function updateAoiClipPath() {
  if (_clipUpdatePending) return;
  _clipUpdatePending = true;
  requestAnimationFrame(() => {
    _clipUpdatePending = false;
    const pathEl = _clipPathEl();
    const svgEl = _clipSvgEl();
    if (!pathEl || !svgEl) return;
    const aoi = state.aoi;
    const rect = map.getContainer().getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    // Make sure the SVG viewport matches the map container so
    // userSpaceOnUse coordinates align with screen pixels.
    svgEl.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svgEl.setAttribute("width", String(w));
    svgEl.setAttribute("height", String(h));
    // Outer ring: full container, with a small bleed margin so we don't
    // see a 1px gap at the edge during fast zooms.
    const margin = 2;
    let d = `M${-margin},${-margin} L${w + margin},${-margin} L${w + margin},${h + margin} L${-margin},${h + margin} Z `;
    if (aoi && aoi.features) {
      for (const feature of aoi.features) {
        const g = feature.geometry;
        if (!g) continue;
        if (g.type === "Polygon") {
          // For each polygon: outer ring becomes a hole in our clip
          // (even-odd fill rule), and any inner rings of the AOI feature
          // become RE-FILLED islands within the hole. Both directions
          // can be appended; fill-rule handles winding-agnostic toggling.
          for (let ri = 0; ri < g.coordinates.length; ri += 1) {
            d += _ringToSvg(g.coordinates[ri]);
          }
        } else if (g.type === "MultiPolygon") {
          for (const poly of g.coordinates) {
            for (let ri = 0; ri < poly.length; ri += 1) {
              d += _ringToSvg(poly[ri]);
            }
          }
        }
      }
    }
    pathEl.setAttribute("d", d);
  });
}

map.on("move", updateAoiClipPath);
map.on("resize", updateAoiClipPath);

const state = {
  manifest: null,
  aoi: null,
  landMask: null,
  windows: [],
  activeIndex: 0,
  cache: new Map(),
  activeFeatures: [],
  activeWindow: null,
  playbackTimer: null,
  playbackLoading: false,
  overlay: null,
  rebuildPending: false,
  waterBeforeId: null,
};

// ── HeatmapLayer config ──────────────────────────────────────────────────

const HEATMAP_INTENSITY = 1;
// Low threshold = full smooth gradient with the soft kernel tail intact.
// We don't need the threshold to clip ocean bleed anymore — the basemap's
// water layer (rendered ABOVE the heat via interleaved beforeId) does that
// for free, so we can keep the inland gradient buttery-smooth.
const HEATMAP_THRESHOLD = 0.05;

function heatmapRadiusForZoom(zoom) {
  // Kernel radius in pixels. With MEAN aggregation, the radius must be
  // large enough that every pixel has many contributing cells — otherwise
  // low-prob regions show cell-row banding (each pixel only averages 1-2
  // cells, so individual cells become visible). Empirically, ~6× the cell
  // pixel-spacing gives smooth blending at every zoom level.
  if (zoom <= 7) return 36;
  if (zoom <= 8) return 50;
  if (zoom <= 9) return 75;
  if (zoom <= 10) return 110;
  if (zoom <= 11) return 160;
  if (zoom <= 12) return 220;
  return 300;
}

// ── Resolution helpers ──────────────────────────────────────────────────

function resolveAssetUrl(url) {
  if (!url) return "";
  if (url.startsWith("gs://openfire/predictions/")) {
    return `${config.dataProxyPrefix}${url.split("/").pop()}`;
  }
  if (url.startsWith("https://storage.googleapis.com/openfire/predictions/")) {
    return `${config.dataProxyPrefix}${url.split("/").pop()}`;
  }
  if (url.startsWith("../frontend-socal/data/")) {
    return `${config.dataProxyPrefix}${url.split("/").pop()}`;
  }
  if (url.startsWith("/")) return url;
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
    if (config.demoManifestUrl && config.demoManifestUrl !== config.manifestUrl) {
      console.warn("Live manifest unavailable; loading local demo manifest.", error);
      return fetchJson(config.demoManifestUrl);
    }
    throw error;
  }
}

// ── AOI / land-mask point-in-polygon filter ─────────────────────────────

const _ringBoundsCache = new WeakMap();

function _ringBounds(ring) {
  let cached = _ringBoundsCache.get(ring);
  if (cached) return cached;
  let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
  for (let i = 0; i < ring.length; i += 1) {
    const lon = ring[i][0];
    const lat = ring[i][1];
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
  }
  cached = { minLat, maxLat, minLon, maxLon };
  _ringBoundsCache.set(ring, cached);
  return cached;
}

function _pointInRing(lat, lon, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const xi = ring[i][0];
    const yi = ring[i][1];
    const xj = ring[j][0];
    const yj = ring[j][1];
    const intersect =
      (yi > lat) !== (yj > lat) &&
      lon < ((xj - xi) * (lat - yi)) / ((yj - yi) || Number.EPSILON) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

function _polygonContainsPoint(polygon, lat, lon) {
  const outer = polygon[0];
  if (!outer || outer.length < 3) return false;
  const b = _ringBounds(outer);
  if (lat < b.minLat || lat > b.maxLat || lon < b.minLon || lon > b.maxLon) {
    return false;
  }
  if (!_pointInRing(lat, lon, outer)) return false;
  for (let i = 1; i < polygon.length; i += 1) {
    if (_pointInRing(lat, lon, polygon[i])) return false;
  }
  return true;
}

function isPointInsideMask(lat, lon, mask) {
  if (!mask || !Array.isArray(mask.features) || mask.features.length === 0) return true;
  for (let f = 0; f < mask.features.length; f += 1) {
    const geom = mask.features[f].geometry;
    if (!geom) continue;
    if (geom.type === "Polygon") {
      if (_polygonContainsPoint(geom.coordinates, lat, lon)) return true;
    } else if (geom.type === "MultiPolygon") {
      for (let p = 0; p < geom.coordinates.length; p += 1) {
        if (_polygonContainsPoint(geom.coordinates[p], lat, lon)) return true;
      }
    }
  }
  return false;
}

function applyLandMaskToSnapshot(snapshot) {
  if (!snapshot || snapshot._landFiltered) return snapshot;
  const aoi = state.aoi;
  const land = state.landMask;
  if (!aoi && !land) return snapshot;
  const features = snapshot.features || [];
  const filtered = [];
  for (let i = 0; i < features.length; i += 1) {
    const coords = features[i].geometry?.coordinates;
    if (!coords) continue;
    const lon = coords[0];
    const lat = coords[1];
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const inAoi = aoi ? isPointInsideMask(lat, lon, aoi) : true;
    const onLand = land ? isPointInsideMask(lat, lon, land) : true;
    if (inAoi && onLand) filtered.push(features[i]);
  }
  snapshot.features = filtered;
  snapshot._landFiltered = true;
  return snapshot;
}

// ── deck.gl overlay layer construction ──────────────────────────────────

function buildAoiCoverLayer() {
  // Cover polygon = SoCal bbox MINUS land. Holes are the Natural Earth
  // land polygons (mainland + Catalina + San Clemente + Channel Islands +
  // any other land in the bbox). Result: the cover paints ONLY over
  // water — hides any heatmap kernel bleed from coastal land cells while
  // leaving every land area completely uncovered (basemap fully visible,
  // heat shows where data exists).
  if (!deckApi.SolidPolygonLayer || !state.landMask) return null;
  // Outer rings of GeoJSON polygons are CCW (per RFC 7946); to use them as
  // HOLES in deck.gl's SolidPolygonLayer we need them in CW (opposite
  // winding). With non-zero fill rule (used by some WebGL backends) a
  // same-winding "hole" is treated as additive area instead of a hole —
  // that's what made every land area outside the AOI disappear in some
  // browsers. Reversing the rings here ensures the hole is unambiguous
  // under both non-zero and even-odd fill rules.
  const holes = [];
  for (const feature of state.landMask.features) {
    const g = feature.geometry;
    if (!g) continue;
    if (g.type === "Polygon") {
      holes.push([...g.coordinates[0]].reverse());
    } else if (g.type === "MultiPolygon") {
      for (const poly of g.coordinates) holes.push([...poly[0]].reverse());
    }
  }
  // Outer ring (CCW) covering the western-US bbox the land mask was clipped
  // to. Wide enough that the bbox edge is off-screen at any zoom centered
  // on SoCal — eliminates the visible rectangular cutout where the cover
  // ends. Land rings inside (from the mask) are holes (CW) and stay
  // uncovered.
  const outer = [
    [-130, 28],
    [-110, 28],
    [-110, 42],
    [-130, 42],
    [-130, 28],
  ];
  return new deckApi.SolidPolygonLayer({
    id: "openfire-water-cover",
    data: [{ polygon: [outer, ...holes] }],
    pickable: false,
    filled: true,
    stroked: false,
    getPolygon: (d) => d.polygon,
    // Match OpenFreeMap Positron's water color exactly (rgb(194,200,202)
    // pulled from the style JSON's "water" layer paint). This makes the
    // covered water visually indistinguishable from the basemap water,
    // while still hiding any heat-kernel bleed underneath.
    getFillColor: [194, 200, 202, 255],
    parameters: { depthTest: false },
  });
}

function buildHeatmapLayer() {
  if (!deckApi.HeatmapLayer) return null;
  const zoom = map.getZoom();
  // NOTE: deck.gl's MaskExtension does not work with HeatmapLayer (the
  // mask_texture binding doesn't propagate through HeatmapLayer's internal
  // weight-aggregation transforms — same limitation as on Leaflet).
  //
  // Water-clipping strategy: the overlay runs in interleaved mode so the
  // heatmap's `beforeId` places it underneath the basemap's water layer in
  // the MapLibre layer stack. The basemap's own water polygons therefore
  // paint over any kernel bleed into the ocean — we get pixel-perfect
  // water masking for free, no custom geometry required.
  return new deckApi.HeatmapLayer({
    id: "openfire-risk-heat",
    data: state.activeFeatures,
    pickable: false,
    getPosition: (f) => f.geometry?.coordinates || [0, 0],
    getWeight: (f) => Number(f.properties?.risk_probability ?? 0),
    radiusPixels: heatmapRadiusForZoom(zoom),
    aggregation: "MEAN",
    intensity: HEATMAP_INTENSITY,
    threshold: HEATMAP_THRESHOLD,
    colorRange: config.riskGradient,
    // Map [0, 0.7] across the full gradient (instead of [0,1]). The data's
    // p99 is ~81% risk and most cells are well below — using the full [0,1]
    // domain wastes 30% of the gradient on values that almost never occur.
    // [0, 0.7] gives us actual visible reds for the high-risk hotspots.
    colorDomain: [0, 0.7],
    beforeId: state.waterBeforeId || undefined,
    updateTriggers: { radiusPixels: Math.round(zoom * 2) },
  });
}

function buildPickerLayer() {
  // Invisible ScatterplotLayer for hover-picking the original cells. The
  // HeatmapLayer is a texture, so it has no per-feature picking — we layer
  // a transparent ScatterplotLayer so deck.gl's pickObject finds cells.
  // No beforeId — picker stays on top of all basemap layers so hover works
  // anywhere in the AOI even where water would otherwise occlude the heat.
  return new deckApi.ScatterplotLayer({
    id: "openfire-risk-picker",
    data: state.activeFeatures,
    pickable: true,
    stroked: false,
    filled: true,
    radiusUnits: "meters",
    radiusMinPixels: 4,
    parameters: { depthTest: false },
    getPosition: (f) => f.geometry?.coordinates || [0, 0],
    getRadius: 700,
    getFillColor: [0, 0, 0, 0],
  });
}

function rebuildOverlay() {
  if (!state.overlay) return;
  // No cover/mask polygon — different WebGL backends interpret the
  // polygon-with-holes fill rule inconsistently (some treat the inner
  // rings as additive area instead of holes, wiping out the entire
  // rest-of-the-US-basemap outside the AOI). Heat data is already
  // filtered to AOI ∩ land cells, so there's nothing off-shore to mask;
  // the only visible bleed is a soft kernel tail clipped by the
  // HEATMAP_THRESHOLD constant in buildHeatmapLayer.
  const layers = [];
  const heat = buildHeatmapLayer();
  if (heat) layers.push(heat);
  layers.push(buildPickerLayer());
  state.overlay.setProps({ layers });
}

// ── Tooltip on hover ────────────────────────────────────────────────────

function tooltipHtml(feature) {
  const props = feature?.properties || {};
  const probability = Number(props.risk_probability ?? 0);
  const pct = Number.isFinite(probability) ? `${(probability * 100).toFixed(1)}%` : "—";
  const date = props.window_start_date ? new Date(`${props.window_start_date}T00:00:00`).toLocaleDateString() : "—";
  return `
    <div class="tooltip-grid">
      <strong>Risk: ${pct}</strong>
      <span>Window: ${date}</span>
      <span>Model: ${(props.model_version || "n/a").toString().slice(0, 24)}</span>
    </div>
  `;
}

function bindHoverTooltip() {
  const tooltip = document.getElementById("map-tooltip");
  if (!tooltip) return;
  map.getCanvas().addEventListener("mousemove", (event) => {
    if (!state.overlay) {
      tooltip.hidden = true;
      return;
    }
    const rect = map.getCanvas().getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const info = state.overlay.pickObject({ x, y, radius: 6 });
    if (!info?.object) {
      tooltip.hidden = true;
      return;
    }
    tooltip.innerHTML = tooltipHtml(info.object);
    tooltip.style.transform = `translate(${event.clientX + 14}px, ${event.clientY + 14}px)`;
    tooltip.hidden = false;
  });
  map.getCanvas().addEventListener("mouseleave", () => {
    tooltip.hidden = true;
  });
}

// ── AOI overlay (county outlines + labels) ──────────────────────────────

function renderAoi() {
  if (!state.aoi || !map.isStyleLoaded()) return;
  const sourceId = "aoi-source";
  if (!map.getSource(sourceId)) {
    map.addSource(sourceId, { type: "geojson", data: state.aoi });
  } else {
    map.getSource(sourceId).setData(state.aoi);
  }
  // (Black AOI line layer removed — the water cover gives an implicit
  // boundary because heat is only shown over land within the AOI cells'
  // bbox. A hard outline is unnecessary and visually heavy.)
  // County labels at polygon centroid
  const labelFeatures = state.aoi.features
    .map((feature) => {
      const name = feature.properties?.NAME;
      if (!name) return null;
      const center = featureCentroid(feature);
      if (!center) return null;
      return {
        type: "Feature",
        geometry: { type: "Point", coordinates: center },
        properties: { name },
      };
    })
    .filter(Boolean);
  const labelSourceId = "aoi-label-source";
  const labelData = { type: "FeatureCollection", features: labelFeatures };
  if (!map.getSource(labelSourceId)) {
    map.addSource(labelSourceId, { type: "geojson", data: labelData });
  } else {
    map.getSource(labelSourceId).setData(labelData);
  }
  if (!map.getLayer("aoi-labels")) {
    map.addLayer({
      id: "aoi-labels",
      type: "symbol",
      source: labelSourceId,
      layout: {
        "text-field": ["get", "name"],
        "text-size": 12,
        "text-font": ["Noto Sans Bold", "Open Sans Bold", "Arial Unicode MS Bold"],
        "text-anchor": "center",
        "text-allow-overlap": false,
      },
      paint: {
        "text-color": "#1e2b26",
        "text-halo-color": "#ffffff",
        "text-halo-width": 1.4,
      },
    });
  }
}

function featureCentroid(feature) {
  const g = feature.geometry;
  if (!g) return null;
  const ring = g.type === "Polygon"
    ? g.coordinates[0]
    : g.type === "MultiPolygon"
      ? largestRing(g.coordinates)
      : null;
  if (!ring) return null;
  let sumLon = 0, sumLat = 0, count = 0;
  for (const [lon, lat] of ring) {
    sumLon += lon;
    sumLat += lat;
    count += 1;
  }
  return count > 0 ? [sumLon / count, sumLat / count] : null;
}

function largestRing(polygons) {
  let best = null;
  let bestArea = -Infinity;
  for (const polygon of polygons) {
    const ring = polygon[0];
    if (!ring) continue;
    let a = 0;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
      a += (ring[j][0] + ring[i][0]) * (ring[j][1] - ring[i][1]);
    }
    a = Math.abs(a) * 0.5;
    if (a > bestArea) {
      bestArea = a;
      best = ring;
    }
  }
  return best;
}

// ── Snapshot loading + timeline ─────────────────────────────────────────

function normalizeWindows(manifest) {
  const excluded = new Set(config.excludedWindowStartDates || []);
  return (manifest.windows || [])
    .filter((entry) => entry?.window_start_date && !excluded.has(entry.window_start_date))
    .sort((a, b) => a.window_start_date.localeCompare(b.window_start_date));
}

function selectSnapshotSource(windowEntry) {
  const variants = windowEntry.geojson_variants || {};
  // Prefer high-res for the heatmap; deck.gl handles 65k features fine.
  const uri = windowEntry.geojson_uri || variants.medium?.geojson_uri || variants.low?.geojson_uri;
  return { uri };
}

async function loadSnapshot(windowEntry) {
  const source = selectSnapshotSource(windowEntry);
  if (!source.uri) throw new Error(`No snapshot URI for ${windowEntry.window_start_date}`);
  const url = resolveAssetUrl(source.uri);
  if (state.cache.has(url)) return state.cache.get(url);
  const payload = await fetchJson(url);
  applyLandMaskToSnapshot(payload);
  state.cache.set(url, payload);
  // Cap cache size
  while (state.cache.size > (config.snapshotCacheSize || 8)) {
    const firstKey = state.cache.keys().next().value;
    state.cache.delete(firstKey);
  }
  return payload;
}

async function setActiveIndex(index) {
  const boundedIndex = Math.max(0, Math.min(index, state.windows.length - 1));
  const windowEntry = state.windows[boundedIndex];
  if (!windowEntry) return;
  state.activeIndex = boundedIndex;
  const slider = document.getElementById("timeline-slider");
  if (slider) slider.value = String(boundedIndex);
  try {
    const snapshot = await loadSnapshot(windowEntry);
    state.activeWindow = windowEntry;
    state.activeFeatures = snapshot.features || [];
    rebuildOverlay();
  } catch (error) {
    console.error("Failed to load snapshot", error);
  }
}

function configureTimeline() {
  const slider = document.getElementById("timeline-slider");
  if (!slider) return;
  slider.min = "0";
  slider.max = String(Math.max(state.windows.length - 1, 0));
  slider.step = "1";
  slider.disabled = state.windows.length < 2;
  slider.value = String(state.activeIndex);
}

function bindControls() {
  bindCollapsiblePanels();
  bindAddressLookup();
  bindSubscribeForm();
  const slider = document.getElementById("timeline-slider");
  if (slider) {
    slider.addEventListener("input", (event) => {
      stopPlayback();
      setActiveIndex(Number(event.target.value));
    });
  }
  const playToggle = document.getElementById("playback-toggle");
  if (playToggle) {
    playToggle.addEventListener("click", togglePlayback);
  }
}

function bindCollapsiblePanels() {
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => {
    const button = panel.querySelector(".panel-toggle");
    const body = panel.querySelector(".panel-body");
    if (!button || !body) return;
    button.addEventListener("click", () => {
      const expanded = button.getAttribute("aria-expanded") === "true";
      button.setAttribute("aria-expanded", expanded ? "false" : "true");
      body.hidden = expanded;
    });
  });
}

// ── Playback (auto-loop through the timeline) ───────────────────────────

function stopPlayback() {
  if (state.playbackTimer) {
    window.clearInterval(state.playbackTimer);
    state.playbackTimer = null;
  }
  state.playbackLoading = false;
  const playToggle = document.getElementById("playback-toggle");
  if (playToggle) {
    playToggle.textContent = "Play";
    playToggle.setAttribute("aria-label", "Play timeline");
  }
}

async function advancePlayback() {
  if (state.playbackLoading) return;
  state.playbackLoading = true;
  try {
    const nextIndex = (state.activeIndex + 1) % state.windows.length;
    await setActiveIndex(nextIndex);
  } finally {
    state.playbackLoading = false;
  }
}

function startPlayback() {
  if (state.windows.length < 2 || state.playbackTimer) return;
  const playToggle = document.getElementById("playback-toggle");
  if (playToggle) {
    playToggle.textContent = "Pause";
    playToggle.setAttribute("aria-label", "Pause timeline");
  }
  advancePlayback();
  state.playbackTimer = window.setInterval(advancePlayback, config.playbackIntervalMs || 750);
}

function togglePlayback() {
  if (state.playbackTimer) stopPlayback();
  else startPlayback();
}

// ── Address lookup (Nominatim) ──────────────────────────────────────────

let _lastNominatimAt = 0;

async function nominatimGeocode(query) {
  const since = Date.now() - _lastNominatimAt;
  if (since < 1100) {
    await new Promise((resolve) => setTimeout(resolve, 1100 - since));
  }
  _lastNominatimAt = Date.now();
  const url =
    "https://nominatim.openstreetmap.org/search?" +
    new URLSearchParams({
      format: "json",
      limit: "1",
      countrycodes: "us",
      addressdetails: "1",
      q: query,
    });
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Geocoding failed (${response.status})`);
  return response.json();
}

function nearestRiskFeature(features, lat, lon) {
  let best = null;
  let bestDistance = Infinity;
  for (let i = 0; i < features.length; i += 1) {
    const coords = features[i].geometry?.coordinates;
    if (!coords) continue;
    const dlat = coords[1] - lat;
    const dlon = coords[0] - lon;
    const distance = dlat * dlat + dlon * dlon;
    if (distance < bestDistance) {
      bestDistance = distance;
      best = features[i];
    }
  }
  return best;
}

function shortPlaceName(displayName) {
  if (!displayName) return "";
  return displayName.split(",").map((p) => p.trim()).filter(Boolean).slice(0, 2).join(", ");
}

function bindAddressLookup() {
  const form = document.getElementById("address-form");
  if (!form) return;
  const input = document.getElementById("address-input");
  const result = document.getElementById("address-result");
  const submit = form.querySelector("button[type=submit]");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    result.removeAttribute("data-state");
    const query = input.value.trim();
    if (!query) {
      result.dataset.state = "err";
      result.textContent = "Enter an address or place name.";
      return;
    }
    submit.disabled = true;
    result.textContent = "Looking up...";
    try {
      const hits = await nominatimGeocode(query);
      if (!Array.isArray(hits) || hits.length === 0) {
        result.dataset.state = "err";
        result.textContent = "No match found in the United States.";
        return;
      }
      const hit = hits[0];
      const lat = Number(hit.lat);
      const lon = Number(hit.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        result.dataset.state = "err";
        result.textContent = "Could not parse coordinates from match.";
        return;
      }
      // MapLibre uses [lon, lat] (GeoJSON convention).
      map.flyTo({ center: [lon, lat], zoom: 12, duration: 1200 });
      const cell = nearestRiskFeature(state.activeFeatures, lat, lon);
      const placeLabel = shortPlaceName(hit.display_name) || query;
      result.dataset.state = "ok";
      if (cell) {
        const probability = Number(cell.properties?.risk_probability ?? 0);
        const pct = Number.isFinite(probability) ? `${(probability * 100).toFixed(0)}%` : "n/a";
        result.textContent = `${placeLabel} — predicted risk ${pct}`;
      } else {
        result.textContent = `${placeLabel} — outside the SoCal AOI; no risk available.`;
      }
    } catch (err) {
      result.dataset.state = "err";
      result.textContent = err.message || "Lookup failed. Try again.";
    } finally {
      submit.disabled = false;
    }
  });
}

// ── Subscribe form ──────────────────────────────────────────────────────

function bindSubscribeForm() {
  const form = document.getElementById("subscribe-form");
  if (!form) return;
  const status = document.getElementById("subscribe-status");
  const output = document.getElementById("threshold-output");
  const range = form.elements.threshold;
  const submit = form.querySelector("button[type=submit]");

  range.addEventListener("input", () => {
    output.textContent = range.value;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    status.removeAttribute("data-state");
    status.textContent = "";
    const email = form.elements.email.value.trim();
    const zip = form.elements.zip.value.trim();
    const threshold = Number(range.value) / 100;
    if (!/^\S+@\S+\.\S+$/.test(email) || !/^\d{5}$/.test(zip)) {
      status.dataset.state = "err";
      status.textContent = "Please provide a valid email and 5-digit ZIP code.";
      return;
    }
    submit.disabled = true;
    try {
      const response = await fetch("/api/subscriptions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, zip, risk_threshold: threshold }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status})`);
      }
      status.dataset.state = "ok";
      status.textContent = "You're subscribed. We'll be in touch when risk crosses your threshold.";
      form.reset();
      output.textContent = "50";
    } catch (err) {
      status.dataset.state = "err";
      status.textContent = err.message || "Subscription failed. Try again later.";
    } finally {
      submit.disabled = false;
    }
  });
}

// ── Boot ────────────────────────────────────────────────────────────────

async function boot() {
  if (!deckApi.MapboxOverlay) {
    console.error("deck.gl MapboxOverlay not available — heatmap cannot start.");
    return;
  }
  bindControls();

  try {
    const manifest = await fetchManifest();
    state.manifest = manifest;
    state.windows = normalizeWindows(manifest);
    const latestIndex = Math.max(
      state.windows.findIndex((item) => item.window_start_date === manifest.latest_window_start_date),
      0,
    );
    state.activeIndex = latestIndex;

    state.aoi = await fetchJson(resolveAssetUrl(manifest.aoi_geojson_uri || "./data/aoi_counties.geojson"));
    try {
      state.landMask = await fetchJson(resolveAssetUrl("./data/socal_land_mask.geojson"));
    } catch (err) {
      console.warn("SoCal land mask unavailable; falling back to AOI polygons.", err);
      state.landMask = null;
    }

    // First clip-path computation now that the AOI is loaded — the path
    // is empty until this fires, so the overlay basemap covers the whole
    // map at boot (heat invisible). Once this runs, the AOI hole is cut
    // and the heat shows through.
    updateAoiClipPath();

    // Wait for the basemap style to load before adding deck.gl overlay
    // and AOI sources (MapLibre rejects addLayer/addSource before "load").
    await new Promise((resolve) => {
      if (map.isStyleLoaded() || map.loaded()) resolve();
      else map.once("load", resolve);
    });
    // Same wait for the overlay basemap, plus a resize so its canvas
    // matches the container size before the first render.
    await new Promise((resolve) => {
      if (overlayMap.isStyleLoaded() || overlayMap.loaded()) resolve();
      else overlayMap.once("load", resolve);
    });
    overlayMap.resize();
    syncOverlayCameraFromPrimary();
    updateAoiClipPath();

    // Discover the basemap's water layer id so the heatmap can render
    // BENEATH it via deck.gl's `beforeId` prop. The basemap's own water
    // polygons then paint over any heatmap-kernel bleed into the ocean —
    // pixel-perfect water masking with zero custom geometry.
    try {
      const styleLayers = map.getStyle()?.layers || [];
      const waterLayer = styleLayers.find(
        (l) =>
          l["source-layer"] === "water" ||
          /^water($|[-_])/i.test(l.id || "")
      );
      state.waterBeforeId = waterLayer?.id || null;
    } catch (err) {
      console.warn("Could not locate basemap water layer for beforeId", err);
      state.waterBeforeId = null;
    }

    // Register deck.gl as a MapLibre control in INTERLEAVED mode so each
    // deck layer can specify a `beforeId` to position itself in the
    // MapLibre layer stack (heat below water, picker above everything).
    state.overlay = new deckApi.MapboxOverlay({
      interleaved: true,
      layers: [],
    });
    map.addControl(state.overlay);

    // Rebuild the deck.gl layers on zoom changes so the heatmap kernel
    // radius (heatmapRadiusForZoom) is recomputed for the new zoom — keeps
    // the heatmap smooth at every zoom level instead of showing cell-row
    // striping when the kernel becomes too small relative to cell spacing.
    let _lastZoomBucket = -999;
    const onZoomChange = () => {
      const bucket = Math.round(map.getZoom() * 2);
      if (bucket === _lastZoomBucket) return;
      _lastZoomBucket = bucket;
      rebuildOverlay();
    };
    map.on("zoomend", onZoomChange);
    map.on("zoom", onZoomChange);

    renderAoi();
    bindHoverTooltip();
    configureTimeline();
    await setActiveIndex(latestIndex);
  } catch (error) {
    console.error("Failed to start OpenFire UI:", error);
  }
}

boot();
