# FireWatch: Real-Time Temporal Fire Analysis

**Live Demo:** [https://wvr-spatial.github.io/burningGeoJSON/index.html]

## 🌍 Overview
FireWatch is a specialized geospatial visualization tool designed to monitor near real-time thermal anomalies across Australia and Southeast Asia. 

Unlike static web maps, this application consumes live satellite telemetry (MODIS/VIIRS) from the **NASA FIRMS (Fire Information for Resource Management System)** API. 
It processes raw data streams on the client side to visualize the propagation and intensity of wildfires over a rolling 7-day window.

## 🚀 Key Features
* **Live Data Ingestion:** Fetches raw CSV data dynamically from NASA servers on every load.
* **Client-Side ETL:** parsing and transformation of non-spatial formats (CSV) into strictly formatted **GeoJSON** within the browser using `PapaParse`.
* **Temporal Filtering:** Interactive time-slider control allowing users to isolate fire events by acquisition date.
* **Data-Driven Styling:** Vector styling expressions that adjust visualization based on attribute data:
    * *Color:* Represents Brightness Temperature (Kelvin).
    * *Radius:* Represents Detection Confidence (%).

## 🛠️ Technical Architecture & Decisions

### 1. Why MapLibre GL JS over Leaflet?
Standard libraries like Leaflet render points as individual DOM elements. When visualizing thousands of fire points, this causes significant DOM manipulation overhead. 
* **Solution:** This project uses **MapLibre GL JS**, which utilizes WebGL to render points on the GPU. This allows for smooth panning and zooming even with datasets exceeding 10,000+ points.

### 2. The GeoJSON Implementation
The application demonstrates advanced handling of the GeoJSON format. Rather than loading a static `.geojson` file, the application constructs the GeoJSON object programmatically:
```javascript
// Dynamic Construction Logic
const geojson = {
    type: "FeatureCollection",
    features: results.data.map(row => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [row.longitude, row.latitude] },
        properties: { ... }
    }))
};
```
### 3. Data-Driven Styling Expressions
Instead of using slow if/else loops to style markers, I utilized MapLibre's style specification expressions. This offloads the logic to the rendering engine for maximum performance:
```javascript
'circle-color': [
    'step', ['get', 'brightness'],
    '#ffeb3b', // Low Intensity
    320, '#ff9800', // Medium
    350, '#ff2a00'  // High
]
```

## 📦 Data Source
Provider: NASA FIRMS (Fire Information for Resource Management System)

Sensor: MODIS (Moderate Resolution Imaging Spectroradiometer)

Format: Live CSV Stream (Converted to GeoJSON)

## 🔧 Installation & Usage
Clone the repository.

No build step or Node.js server required (Vanilla JS).

Open index.html in any modern web browser.

Note: A CORS proxy is utilized to handle cross-origin requests from the NASA server during local development.

Created By Warren van Ryn
