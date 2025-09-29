from flask import Flask, request, jsonify, send_from_directory
import ee
import json
import os
# NEW IMPORT: Need datetime and timedelta for robust client-side date iteration
from datetime import datetime, timedelta

# Initialize Flask app
app = Flask(__name__, static_folder='.')

# =========================================================================================
# GEE HELPER FUNCTIONS
# =========================================================================================

def add_ndvi(image):
    """Calculates and adds the Normalized Difference Vegetation Index (NDVI) band."""
    # NDVI: (NIR - Red) / (NIR + Red) -> (B8 - B4) / (B8 + B4)
    ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')
    return image.addBands(ndvi)

# =========================================================================================
# ANALYSIS FUNCTIONS
# =========================================================================================

def multiple_gee(polygon, start_date, end_date, timeframe):
    """
    Performs GEE time-series analysis (NDVI trend) on a given polygon.
    This function now uses robust Python iteration to define periods, avoiding the 
    'Date' object error encountered previously.
    """
    try:
        # 1. Setup
        aoi = ee.Geometry.Polygon(polygon['coordinates'])
        
        # Convert user date strings to Python datetime objects
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')

        # Define time step based on timeframe
        if timeframe == 'monthly':
            # Use 30 days as a standard approximation for the step
            time_step = timedelta(days=30)
        elif timeframe == 'quarterly':
            time_step = timedelta(days=90)
        elif timeframe == 'bi-weekly':
            time_step = timedelta(days=14)
        else:
            return {"error": "Invalid timeframe specified."}
        
        # Filter Image Collection and Add NDVI (only done once)
        collection = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(aoi) \
            .filterDate(start_date, end_date) \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
            .map(add_ndvi)
            
        if collection.size().getInfo() == 0:
            return {"error": "No suitable imagery found for the selected area and date range."}

        # 2. Iterate through time periods using standard Python logic
        dates = []
        values = []
        current_start = start_dt
        
        while current_start < end_dt:
            # Calculate the end of the current period, clamped by the overall end_dt
            current_end = current_start + time_step
            if current_end > end_dt:
                current_end = end_dt
            
            # Convert Python dates to string for GEE filtering
            gee_start = current_start.strftime('%Y-%m-%d')
            gee_end = current_end.strftime('%Y-%m-%d')
            
            # 3. GEE Calculation for the current period (all server-side)
            period_collection = collection.filterDate(gee_start, gee_end)
            
            # Only perform reduction if imagery exists for the period
            if period_collection.size().getInfo() > 0:
                # Create a median composite for the period
                composite = period_collection.median().clip(aoi)
                
                # Calculate the mean NDVI over the geometry for the period
                mean_ndvi = composite.select('NDVI').reduceRegion(
                    reducer=ee.Reducer.mean(),
                    geometry=aoi,
                    scale=10, # Sentinel-2 resolution
                    maxPixels=1e9
                ).get('NDVI').getInfo()
                
                # Store the result only if the GEE reduction returned a number
                if mean_ndvi is not None:
                    dates.append(gee_start)
                    values.append(round(mean_ndvi, 4))

            # Advance the start date for the next loop iteration
            current_start = current_end

        # 4. Process and Format Results
        total_samples = len(values)
        if total_samples == 0:
            return {"error": f"Time series analysis found no suitable data points for {timeframe} periods. Try a larger date range."}

        # Calculate overall health statistics for the report based on the time series
        # Using standard NDVI thresholds: Poor (<0.2), Moderate (0.2-0.5), Good (>=0.5)
        poor_count = sum(1 for v in values if v < 0.2)
        moderate_count = sum(1 for v in values if 0.2 <= v < 0.5)
        good_count = sum(1 for v in values if v >= 0.5)

        avg_poor = (poor_count / total_samples) * 100
        avg_moderate = (moderate_count / total_samples) * 100
        avg_good = (good_count / total_samples) * 100
        
        return {
            "timeSeriesData": {
                "dates": dates,
                "values": values,
                "averagePoor": round(avg_poor, 2),
                "averageModerate": round(avg_moderate, 2),
                "averageGood": round(avg_good, 2)
            }
        }

    except Exception as e:
        # Re-raise the error with context for debugging
        print(f"GEE time-series analysis failed: {e}")
        return {"error": str(e)}


def single_gee(polygon_geojson, start_date, end_date):
    """
    Performs GEE analysis on a given polygon and date range.
    Returns NDVI/BSI map URL and health statistics.
    (Existing function logic preserved)
    """
    try:
        # Define the Area of Interest (AOI)
        aoi = ee.Geometry.Polygon(polygon_geojson['coordinates'])

        # Filter Sentinel-2 Image Collection for the best image with low cloud cover
        collection = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
            .filterBounds(aoi) \
            .filterDate(start_date, end_date) \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)) \
            .sort('CLOUDY_PIXEL_PERCENTAGE')

        if collection.size().getInfo() == 0:
            return {"error": "No suitable imagery found for the selected area and date range."}

        # Select the median composite over the period
        image = collection.median().clip(aoi)
        
        # Get the image acquisition date for the report
        acquisition_date = ee.Date(collection.first().get('system:time_start')).format('YYYY-MM-DD').getInfo()

        # Get the bounds of the AOI in [min_lon, min_lat, max_lon, max_lat] format
        bounds_geojson = aoi.bounds().getInfo()['coordinates'][0]
        min_lon = min(p[0] for p in bounds_geojson)
        min_lat = min(p[1] for p in bounds_geojson)
        max_lon = max(p[0] for p in bounds_geojson)
        max_lat = max(p[1] for p in bounds_geojson)
        bounds = [min_lon, min_lat, max_lon, max_lat]

        # ==================================
        # 1. VEGETATION HEALTH ANALYSIS (NDVI)
        # ==================================
        # Calculate NDVI: (NIR - Red) / (NIR + Red) and rename the band to 'NDVI'
        ndvi = image.normalizedDifference(['B8', 'B4']).rename('NDVI')

        # Define NDVI visualization parameters for the map
        ndvi_palette = ['d73027', 'fee08b', 'a6d96a', '1a9850'] # Red, Yellow, Light Green, Dark Green
        
        # Generate a thumbnail URL
        ndvi_map_url = ndvi.getThumbUrl({
            'dimensions': 512,
            'format': 'png',
            'crs': 'EPSG:3857',
            'region': aoi.bounds(),
            'min': 0.1,
            'max': 0.9,
            'palette': ndvi_palette
        })

        # Calculate the area of each NDVI health category (in square meters)
        pixel_area = ee.Image.pixelArea()
        
        poor_ndvi_area = ndvi.lt(0.2).multiply(pixel_area).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('NDVI').getInfo()

        moderate_ndvi_area = ndvi.gte(0.2).And(ndvi.lt(0.5)).multiply(pixel_area).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('NDVI').getInfo()

        good_ndvi_area = ndvi.gte(0.5).multiply(pixel_area).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('NDVI').getInfo()

        # ==================================
        # 2. SOIL ANALYSIS (BSI)
        # ==================================
        # Calculate BSI and rename the band to 'BSI' (BSI = ((SWIR1 + Red) - (NIR + Blue)) / ((SWIR1 + Red) + (NIR + Blue)))
        bsi = image.expression(
            '((B11 + B4) - (B8 + B2)) / ((B11 + B4) + (B8 + B2))', {
                'B11': image.select('B11'), # SWIR1
                'B4': image.select('B4'),   # Red
                'B8': image.select('B8'),   # NIR
                'B2': image.select('B2')    # Blue
            }).rename('BSI')

        # Define BSI visualization parameters
        bsi_palette = ['0000FF', '87CEFA', 'FFFFFF', 'FFB6C1', 'FF0000'] # Blue (water) to White to Red (bare soil)

        # Generate a thumbnail URL for BSI
        bsi_map_url = bsi.getThumbUrl({
            'dimensions': 512,
            'format': 'png',
            'crs': 'EPSG:3857',
            'region': aoi.bounds(),
            'min': -0.5,
            'max': 0.5,
            'palette': bsi_palette
        })

        # Calculate the area of each BSI soil category (BSI > 0.2 indicates high bareness)
        high_bsi_area = bsi.gte(0.2).multiply(pixel_area).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('BSI').getInfo()

        med_bsi_area = bsi.gte(-0.2).And(bsi.lt(0.2)).multiply(pixel_area).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('BSI').getInfo()
        
        low_bsi_area = bsi.lt(-0.2).multiply(pixel_area).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('BSI').getInfo()


        # Handle None values and calculate total areas
        poor_ndvi_area = poor_ndvi_area if poor_ndvi_area is not None else 0
        moderate_ndvi_area = moderate_ndvi_area if moderate_ndvi_area is not None else 0
        good_ndvi_area = good_ndvi_area if good_ndvi_area is not None else 0
        total_ndvi_area = poor_ndvi_area + moderate_ndvi_area + good_ndvi_area

        high_bsi_area = high_bsi_area if high_bsi_area is not None else 0
        med_bsi_area = med_bsi_area if med_bsi_area is not None else 0
        low_bsi_area = low_bsi_area if low_bsi_area is not None else 0
        total_bsi_area = high_bsi_area + med_bsi_area + low_bsi_area
        
        if total_ndvi_area == 0 or total_bsi_area == 0:
            return {"error": "No pixels found for analysis. The polygon may be too small or off-land."}
        
        # Calculate percentages
        poor_ndvi_percent = (poor_ndvi_area / total_ndvi_area) * 100
        moderate_ndvi_percent = (moderate_ndvi_area / total_ndvi_area) * 100
        good_ndvi_percent = (good_ndvi_area / total_ndvi_area) * 100

        high_bsi_percent = (high_bsi_area / total_bsi_area) * 100
        med_bsi_percent = (med_bsi_area / total_bsi_area) * 100
        low_bsi_percent = (low_bsi_area / total_bsi_area) * 100

        # Return the results
        return {
            "ndviMapUrl": ndvi_map_url,
            "bsiMapUrl": bsi_map_url,
            "bounds": bounds,
            "report": {
                "ndvi": {
                    "poor": round(poor_ndvi_percent, 2),
                    "moderate": round(moderate_ndvi_percent, 2),
                    "good": round(good_ndvi_percent, 2)
                },
                "bsi": {
                    "high": round(high_bsi_percent, 2),
                    "medium": round(med_bsi_percent, 2),
                    "low": round(low_bsi_percent, 2)
                }
            },
            "acquisitionDate": acquisition_date
        }

    except Exception as e:
        print(f"GEE analysis failed: {e}")
        return {"error": str(e)}

# =========================================================================================
# FLASK ROUTES
# =========================================================================================

# Route for the main page
@app.route('/')
def serve_index():
    return send_from_directory(app.static_folder, 'index.html')

# Route for the analysis endpoint
@app.route('/analyze', methods=['POST'])
def analyze():
    # Attempt GEE initialization on each request.
    try:
        # NOTE: credentials must be defined globally or passed if ee.Initialize() fails
        ee.Initialize(credentials) 
    except Exception as e:
        return jsonify({"error": f"GEE initialization failed: {e}"}), 500

    data = request.json
    polygon = data.get('polygon')
    start_date = data.get('startDate')
    end_date = data.get('endDate')
    analysistype = data.get('analysisType')
    timeframe = data.get('timeFrame') # Correctly retrieve the timeframe

    if not polygon or not start_date or not end_date:
        return jsonify({"error": "Missing required parameters"}), 400

    if analysistype == 'single':
        single = single_gee(polygon, start_date, end_date)
        if "error" in single:
            return jsonify(single), 400
        else:
            return jsonify(single)
            
    elif analysistype == 'time-series':
        if not timeframe:
             return jsonify({"error": "Timeframe is required for time-series analysis."}), 400
             
        multiple = multiple_gee(polygon, start_date, end_date, timeframe=timeframe)
        if "error" in multiple:
            return jsonify(multiple), 400
        else:
            return jsonify(multiple)
    
    else:
        return jsonify({"error": "Invalid analysis type specified."}), 400


if __name__ == '__main__':
    # NOTE: Since I cannot access your credentials file, I'm providing this placeholder.
    # In a real environment, ensure 'credentials/agri-471404-201b5260c966.json' is accessible.
    service_ac = "user-374@agri-471404.iam.gserviceaccount.com"
    
    # Check if credentials file exists (local execution guard)
    credentials_path = 'credentials/agri-471404-201b5260c966.json'
    if not os.path.exists(credentials_path):
         print(f"Warning: Credentials file not found at {credentials_path}. GEE will likely fail.")
         # Using placeholder to allow the code to execute locally for structure testing
         credentials = None
    else:
         credentials = ee.ServiceAccountCredentials(service_ac, credentials_path)
         
    # Initialize GEE once when the app starts
    try:
        ee.Initialize(credentials)
        print("Google Earth Engine initialized successfully!")
    except Exception as e:
        print(f"Failed to initialize GEE: {e}")
        print("Ensure 'earthengine authenticate' was run or service account credentials are correct.")
        
    # The application will run even if GEE init fails, but requests will fail at the route level.
    app.run(debug=True)
