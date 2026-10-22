Copies of the map's two JavaScript libraries, served to the browser tests in place of unpkg.com
so the tests never depend on a CDN:

- `leaflet.js`, `leaflet.css`: Leaflet 1.9.4 (BSD 2-Clause), from github.com/Leaflet/Leaflet/releases
- `esri-leaflet.js`: Esri Leaflet 3.0.12 (Apache 2.0), from github.com/Esri/esri-leaflet/releases

Keep the versions in step with the `<script>` tags in `map/rhode-island-flood-map.html`.
