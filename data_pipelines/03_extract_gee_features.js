// ==========================================
// 1. CONFIGURATION & SETUP
// ==========================================
var TARGET_YEAR = 2018; // Change this to process other years
// For 2017
// var TARGET_YEAR = 2017;
// // Set start date 60 days before 2018
// var startDate = ee.Date.fromYMD(2017, 11, 1); 
// var endDate = ee.Date.fromYMD(2017, 12, 31);

var GCP_PROJECT = 'msds603-mlops-project'; // Your exact GCP Project ID
var BQ_DATASET = 'openfire_features'; // The dataset in BigQuery
var BQ_TABLE = 'silver_features_' + TARGET_YEAR + '_gee_import'; // Creates a new table per year

var FIRE_ASSET = 'projects/msds603-mlops-project/assets/calfire_frap_perimeters_2024';

// Define the 5-day step for the temporal windows
var startDate = ee.Date.fromYMD(TARGET_YEAR, 1, 1);
var endDate = ee.Date.fromYMD(TARGET_YEAR, 12, 31);
var daysInYear = endDate.difference(startDate, 'day');
var dateOffsets = ee.List.sequence(0, daysInYear.subtract(1), 5); // [0, 5, 10, 15...]

// ==========================================
// 2. SPATIAL BASELINE (The 1km Grid)
// ==========================================
// Filter for our 4 specific California counties
var counties = ee.FeatureCollection("TIGER/2018/Counties");
var aoi = counties.filter(ee.Filter.and(
  ee.Filter.eq('STATEFP', '06'),
  ee.Filter.inList('NAME', ['Kern', 'Los Angeles', 'San Luis Obispo', 'Santa Barbara'])
));

// Generate the 1km covering grid
var grid = aoi.geometry().coveringGrid('EPSG:4326', 1000);

// ==========================================
// 3. STATIC FEATURES (Topography & CAL FIRE)
// ==========================================
// A. Topography (Elevation, Slope, Aspect)
var elev = ee.Image("USGS/3DEP/10m");
var topo = ee.Terrain.products(elev);
var elevation = topo.select('elevation').rename('mean_elevation');
var slope = topo.select('slope').rename('mean_slope');

// Convert Aspect to Radians, then calculate Cosine (North/South) and Sine (East/West)
var aspectRad = topo.select('aspect').multiply(Math.PI / 180);
var cosAspect = aspectRad.cos().rename('mean_cos_aspect');
var sinAspect = aspectRad.sin().rename('mean_sin_aspect');
var staticTopo = ee.Image([elevation, slope, cosAspect, sinAspect]);

// B. Historical Fire Dates (Spatial Join)
var fires = ee.FeatureCollection(FIRE_ASSET);
var spatialFilter = ee.Filter.intersects({leftField: '.geo', rightField: '.geo'});
var saveAllJoin = ee.Join.saveAll({matchesKey: 'intersecting_fires'});
var gridWithFires = saveAllJoin.apply(grid, fires, spatialFilter);

// Pre-process the grid to hold the Lat/Lon centroids and the comma-separated fire dates
var baseGrid = gridWithFires.map(function(cell) {
  var fList = ee.List(cell.get('intersecting_fires'));
  
  // Extract all ALARM_DATES, format them cleanly
  var dates = fList.map(function(f) {
    var rawDate = ee.String(ee.Feature(f).get('ALARM_DATE'));
    return rawDate.slice(0, 10).replace('/', '-', 'g');
  });
  
  // Create comma separated string, or 'None' if it never burned
  var dateString = ee.Algorithms.If(dates.length().gt(0), dates.join(','), 'None');
  
  // THE FIX: Add a 1-meter maxError margin to the centroid calculation
  var centroid = cell.geometry().centroid(1).coordinates();
  
  return ee.Feature(cell.geometry(), {
    'latitude': centroid.get(1),
    'longitude': centroid.get(0),
    'historical_fire_dates': dateString
  });
});

// ==========================================
// 4. DYNAMIC FEATURES (The 5-Day Loop)
// ==========================================
// Map over each 5-day offset to create the time-series
var timeSeriesData = ee.FeatureCollection(dateOffsets.map(function(offset) {
  var t0 = startDate.advance(offset, 'day');
  var t1 = t0.advance(5, 'day');
  var timestampStr = t0.format('YYYY-MM-dd');
  
  // A. Sentinel-2 Optical & Indices
  var s2_col = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
    .filterBounds(aoi)
    .filterDate(t0, t1)
    .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
    .map(function(img) {
      // Sentinel-2 SR is scaled by 10000. Convert to 0-1 for accurate EVI math.
      var scaled = img.multiply(0.0001); 
      
      var ndvi = scaled.normalizedDifference(['B8', 'B4']).rename('mean_NDVI');
      var ndwi = scaled.normalizedDifference(['B3', 'B8']).rename('mean_NDWI');
      var nbr = scaled.normalizedDifference(['B8', 'B12']).rename('mean_NBR');
      var evi = scaled.expression(
        '2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))', {
        'NIR': scaled.select('B8'),
        'RED': scaled.select('B4'),
        'BLUE': scaled.select('B2')
      }).rename('mean_EVI');
      
      return img.addBands([ndvi, ndwi, nbr, evi]);
    });

  // The Failsafe: Create a masked (Null) dummy image for cloudy weeks
  var expectedBands = ee.List(['B2','B3','B4','B8','B11','B12','mean_NDVI','mean_EVI','mean_NDWI','mean_NBR']);
  var dummyImage = ee.Image.constant(ee.List.repeat(0, expectedBands.length()))
                     .rename(expectedBands)
                     .updateMask(0); // Masks everything so it exports to BQ as Null

  // If collection has images, use median. If empty, use dummy image.
  var s2 = ee.Image(ee.Algorithms.If(
    s2_col.size().gt(0),
    s2_col.median().select(expectedBands),
    dummyImage
  ));
  
  // B. The 10km Spatial Context (Neighborhood)
  var kernel = ee.Kernel.circle({radius: 5000, units: 'meters'});
  var s2_10km = s2.select(['mean_NDVI', 'mean_NDWI', 'mean_NBR'])
    .reduceNeighborhood({reducer: ee.Reducer.mean(), kernel: kernel})
    .rename(['neighborhood_ndvi_10km', 'neighborhood_ndwi_10km', 'neighborhood_nbr_10km']);
    
  // C. GRIDMET Weather
  var gridmet = ee.ImageCollection("IDAHO_EPSCOR/GRIDMET").filterDate(t0, t1);
  var temp_max = gridmet.select('tmmx').max().subtract(273.15).rename('gridmet_temp_max'); 
  var humid_min = gridmet.select('rmin').min().rename('gridmet_humidity_min');
  var precip_sum = gridmet.select('pr').sum().rename('gridmet_precip_sum');
  var wind_max = gridmet.select('vs').max().rename('gridmet_wind_max');
  var weather = ee.Image([temp_max, humid_min, precip_sum, wind_max]);

  // D. Combine Everything
  var combinedRaster = ee.Image([
    s2, 
    s2_10km,
    weather,
    staticTopo
  ]);

  // E. Extract 20m pixels into the 1km grid
  var sampled = combinedRaster.reduceRegions({
    collection: baseGrid,
    reducer: ee.Reducer.mean(),
    scale: 20, 
    tileScale: 4 
  });

  // Attach the temporal window label to every row
  return sampled.map(function(f) {
    return f.set('timestamp', timestampStr);
  });
})).flatten();

// ==========================================
// 5. EXPORT TO BIGQUERY
// ==========================================
Export.table.toBigQuery({
  collection: timeSeriesData,
  description: 'export_features_' + TARGET_YEAR,
  table: GCP_PROJECT + '.' + BQ_DATASET + '.' + BQ_TABLE,
  append: false // Set to true if you are appending months instead of years
});