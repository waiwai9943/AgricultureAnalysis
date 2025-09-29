from flask import Flask, request, jsonify, send_from_directory
import ee
import json
import os

# Initialize Flask app
app = Flask(__name__, static_folder='.')
def multiple_gee(polygon, start_date, end_date, timeframe):
    return {"error": "Time-series analysis not yet implemented."}
# GEE analysis function
def single_gee(polygon_geojson, start_date, end_date):
    """
    Performs GEE analysis on a given polygon and date range.
    Returns NDVI map URL and health statistics.
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

        # Select the least cloudy image
        image = collection.median().clip(aoi)
        
        # Get the image acquisition date for the report
        acquisition_date = ee.Date(collection.first().get('system:time_start')).format('YYYY-MM-DD').getInfo()

        # Get the bounds of the AOI in [min_lon, min_lat, max_lon, max_lat] format
        bounds_geojson = aoi.bounds().getInfo()['coordinates'][0]
        # The GEE bounds format is a list of [lon, lat] pairs.
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

        # Calculate the area of each NDVI health category
        poor_ndvi_area = ndvi.lt(0.2).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('NDVI').getInfo()

        moderate_ndvi_area = ndvi.gte(0.2).And(ndvi.lt(0.5)).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('NDVI').getInfo()

        good_ndvi_area = ndvi.gte(0.5).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('NDVI').getInfo()

        # ==================================
        # 2. SOIL ANALYSIS (BSI)
        # ==================================
        # Calculate BSI and rename the band to 'BSI'
        bsi = image.expression(
            '((B11 + B4) - (B8 + B2)) / ((B11 + B4) + (B8 + B2))', {
                'B11': image.select('B11'),
                'B4': image.select('B4'),
                'B8': image.select('B8'),
                'B2': image.select('B2')
            }).rename('BSI')

        # Define BSI visualization parameters
        # Higher values indicate bare soil
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

        # Calculate the area of each BSI soil category
        high_bsi_area = bsi.gte(0.2).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('BSI').getInfo()

        med_bsi_area = bsi.gte(-0.2).And(bsi.lt(0.2)).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('BSI').getInfo()
        
        low_bsi_area = bsi.lt(-0.2).multiply(ee.Image.pixelArea()).reduceRegion(
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
                    "poor": poor_ndvi_percent,
                    "moderate": moderate_ndvi_percent,
                    "good": good_ndvi_percent
                },
                "bsi": {
                    "high": high_bsi_percent,
                    "medium": med_bsi_percent,
                    "low": low_bsi_percent
                }
            },
            "acquisitionDate": acquisition_date
        }

    except Exception as e:
        print(f"GEE analysis failed: {e}")
        return {"error": str(e)}


# Route for the main pageear
@app.route('/')
def serve_index():
    return send_from_directory(app.static_folder, 'index.html')

# Route for the analysis endpoint
@app.route('/analyze', methods=['POST'])
def analyze():
    # Attempt GEE initialization on each request.
    # This is a good practice for servers that might idle.
    try:
        ee.Initialize(credentials)
    except Exception as e:
        return jsonify({"error": f"GEE initialization failed: {e}"}), 500

    data = request.json
    polygon = data.get('polygon')
    start_date = data.get('startDate')
    end_date = data.get('endDate')
    analysistype = data.get('analysisType')

    if not polygon or not start_date or not end_date:
        return jsonify({"error": "Missing required parameters"}), 400

    if analysistype == 'single':
        single = single_gee(polygon, start_date, end_date)
        if "error" in single:
            return jsonify(single), 400
        else:
            return jsonify(single)
    elif analysistype == 'time-series':
        multiple = multiple_gee(polygon, start_date, end_date, timeframe = data.get(''))
        if "error" in multiple:
            return jsonify(multiple), 400
        else:
            return jsonify(multiple)

    


if __name__ == '__main__':
    service_ac = "user-374@agri-471404.iam.gserviceaccount.com"
    credentials = ee.ServiceAccountCredentials(service_ac, 'credentials/agri-471404-201b5260c966.json')
    # Initialize GEE once when the app starts
    try:
        ee.Initialize(credentials)
        print("Google Earth Engine initialized successfully!")
    except Exception as e:
        print(f"Failed to initialize GEE: {e}")
        print("Please ensure you have run 'earthengine authenticate' and have internet access.")
        exit()
    
    # Run the Flask server in debug mode
    # This automatically reloads the server on code changes
    app.run(debug=False)
