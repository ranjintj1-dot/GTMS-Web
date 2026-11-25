// GTMS proxies NOAA aviation weather through its own Render API gateway.
// This avoids a separate Cloudflare Worker deployment and its workers.dev routing.
// SIGMET and PIREP routes are served by the same backend as the GFS forecast.
window.GTMS_NOAA_PROXY_URL = "https://gtms-weather-api.onrender.com";
