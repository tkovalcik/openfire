# OpenFire UI: problems we hit and what to learn from them

Notes from rebuilding the SoCal risk visualization. Each one is a trap that looked unrelated until we hit it.

## Architecture / rendering

1. **Not every deck.gl extension works with every layer.**
   `MaskExtension` clips normal layers fine but silently fails on `HeatmapLayer` (multi-pass GPU aggregation drops the mask binding). Check per-layer compatibility — aggregation layers are special.

2. **Polygon-with-holes is browser-inconsistent in WebGL.**
   Even-odd vs non-zero fill rules render the same geometry differently across GPUs. Avoid holes; **compose via layer ordering (`beforeId`) instead.**

3. **GPU textures have no picking.**
   `HeatmapLayer` is a rasterized output — no per-feature hover. Add a transparent `ScatterplotLayer` on top for hit-testing. **Decouple visual from interaction.**

## Heatmap-specific

4. **Kernel radius is zoom-dependent.**
   Fixed-radius heatmaps checkerboard at high zoom (kernel < cell spacing). Scale radius with zoom (`heatmapRadiusForZoom`). Rule of thumb: **radius ≥ ~6× on-screen cell width.** And listen to `zoom` events — deck.gl won't recompute for you.

5. **MEAN vs SUM aggregation matters.**
   SUM stacks weights, so a cluster of low-risk cells reads as high-risk. Use **MEAN for scores/probabilities, SUM for counts.**

6. **Visual params interact non-linearly.**
   Bumping `threshold` to clip ocean bleed also chopped the inland gradient. Change one knob at a time, screenshot, repeat.

## Debugging hygiene

7. **"Data wrong" vs "renderer wrong" are different bugs.**
   Ask the question explicitly before fixing. Often the answer is both (filter cells AND clip visually).

8. **Static server vs production server resolve URLs differently.**
   `python -m http.server` and FastAPI handle relative paths differently. JSON parse errors like `Unexpected token '<'` usually mean a 404 HTML page got fetched. **Centralize asset-URL resolution in one helper.**

9. **Playwright screenshots beat verbal bug reports.**
   Reproduce locally, screenshot at multiple zooms, eyeball it. 25 lines of Playwright > 5 rounds of "still looks wrong."

10. **Browser cache eats fixes.**
    Confirm hard-refresh (Cmd+Shift+R) before assuming a fix didn't land.

## Meta-lesson

For high-data-volume UIs, the bottleneck is rarely the math — **it's the layer architecture and rendering pipeline.** Most traps come from fighting the renderer instead of working with it.
