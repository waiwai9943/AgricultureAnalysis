from flask import Flask, request, jsonify, send_from_directory
import ee
import json
import os

# Initialize Flask app
app = Flask(__name__, static_folder='.')

# GEE analysis function
def analyze_gee(polygon_geojson, start_date, end_date):
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
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 40)) \
            .sort('CLOUDY_PIXEL_PERCENTAGE')

        if collection.size().getInfo() == 0:
            return {"error": "No suitable imagery found for the selected area and date range."}

        # Select the least cloudy image
        image = collection.first().clip(aoi)

        # Get the image acquisition date for the report
        acquisition_date = ee.Date(image.get('system:time_start')).format('YYYY-MM-DD').getInfo()

        # Calculate NDVI
        ndvi = image.normalizedDifference(['B8', 'B4']) # B8 is NIR, B4 is Red

        # Define NDVI visualization parameters for the map
        ndvi_palette = ['d73027', 'fee08b', 'a6d96a', '1a9850'] # Red, Yellow, Light Green, Dark Green
        
        # Generate a thumbnail URL
        map_url = ndvi.getThumbUrl({
            'dimensions': 512,
            'format': 'png',
            'crs': 'EPSG:3857',
            'region': aoi.bounds(),
            'min': 0.1,
            'max': 0.9,
            'palette': ndvi_palette
        })

        # Calculate the area of each health category
        poor_area = ndvi.lt(0.2).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('nd').getInfo()

        moderate_area = ndvi.gte(0.2).And(ndvi.lt(0.5)).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('nd').getInfo()

        good_area = ndvi.gte(0.5).multiply(ee.Image.pixelArea()).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=aoi, scale=10, maxPixels=1e9
        ).get('nd').getInfo()

        # Handle cases where area is None and calculate total area
        poor_area = poor_area if poor_area is not None else 0
        moderate_area = moderate_area if moderate_area is not None else 0
        good_area = good_area if good_area is not None else 0
        
        total_calculated_area = poor_area + moderate_area + good_area
        
        if total_calculated_area == 0:
             return {"error": "No pixels found for analysis. The polygon may be too small or off-land."}
        
        # Calculate percentages
        poor_percent = (poor_area / total_calculated_area) * 100
        moderate_percent = (moderate_area / total_calculated_area) * 100
        good_percent = (good_area / total_calculated_area) * 100

        # Return the results
        return {
            "mapUrl": map_url,
            "report": {
                "poor": poor_percent,
                "moderate": moderate_percent,
                "good": good_percent
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

    if not polygon or not start_date or not end_date:
        return jsonify({"error": "Missing required parameters"}), 400

    results = analyze_gee(polygon, start_date, end_date)
    
    if "error" in results:
        return jsonify(results), 400

    return jsonify(results)

if __name__ == '__main__':
    service_ac = "user-374@agri-471404.iam.gserviceaccount.com"
    credentials = ee.ServiceAccountCredentials(service_ac, 'gee/agri-471404-6da4ecee8723.json')  
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