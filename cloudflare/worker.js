const ALLOWED_ORIGINS = new Set([
  "https://gtms-static.onrender.com"
]);

const UPSTREAMS = {
  sigmets: "https://aviationweather.gov/api/data/sigmet?format=geojson",
  pireps: "https://aviationweather.gov/api/data/pirep?format=geojson&age=6"
};

const EMPTY_FEATURES = { type: "FeatureCollection", features: [] };
const CACHE_TTL_SECONDS = 60;

function jsonResponse(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/geo+json; charset=utf-8",
      "Cache-Control": "public, max-age=60, s-maxage=60",
      ...headers
    }
  });
}

function corsResponse(request, response) {
  const origin = request.headers.get("Origin");
  const headers = new Headers(response.headers);

  if (origin && ALLOWED_ORIGINS.has(origin)) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");
    headers.set("Access-Control-Allow-Headers", "Content-Type");
    headers.set("Access-Control-Max-Age", "86400");
    headers.append("Vary", "Origin");
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers
  });
}

function asFeatureCollection(payload) {
  if (payload && payload.type === "FeatureCollection" && Array.isArray(payload.features)) {
    return payload;
  }

  if (payload && payload.type === "Feature") {
    return { type: "FeatureCollection", features: [payload] };
  }

  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.features)
      ? payload.features
      : [];

  const features = rows.map((item) => {
    if (item?.type === "Feature") return item;

    const props = item?.properties || item || {};
    const lat = Number(props.lat ?? props.latitude ?? props.LAT);
    const lon = Number(props.lon ?? props.longitude ?? props.LON);
    const geometry = item?.geometry || (
      Number.isFinite(lat) && Number.isFinite(lon)
        ? { type: "Point", coordinates: [lon, lat] }
        : null
    );

    return {
      type: "Feature",
      geometry,
      properties: props
    };
  }).filter((feature) => feature.geometry);

  return { type: "FeatureCollection", features };
}

async function getCachedUpstream(path, upstreamUrl) {
  const cache = caches.default;
  const cacheKey = new Request("https://gtms-noaa-cache.invalid" + path, { method: "GET" });

  const cached = await cache.match(cacheKey);
  if (cached) {
    return new Response(cached.body, {
      status: cached.status,
      headers: cached.headers
    });
  }

  const upstream = await fetch(upstreamUrl, {
    method: "GET",
    headers: {
      "Accept": "application/geo+json, application/json",
      "User-Agent": "GTMS-Cloudflare-Worker/1.0 (aviation weather visualization)"
    },
    cf: { cacheTtl: CACHE_TTL_SECONDS, cacheEverything: true }
  });

  if (upstream.status === 204) {
    const empty = jsonResponse(EMPTY_FEATURES);
    await cache.put(cacheKey, empty.clone());
    return empty;
  }

  if (!upstream.ok) {
    const detail = (await upstream.text()).slice(0, 700);
    return jsonResponse({
      error: "NOAA upstream request failed",
      upstreamStatus: upstream.status,
      detail
    }, upstream.status === 429 ? 429 : 502, {
      "Cache-Control": "no-store"
    });
  }

  const text = await upstream.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : EMPTY_FEATURES;
  } catch {
    return jsonResponse({
      error: "NOAA returned a non-JSON response",
      detail: text.slice(0, 400)
    }, 502, { "Cache-Control": "no-store" });
  }

  const collection = asFeatureCollection(payload);
  const response = jsonResponse(collection);
  await cache.put(cacheKey, response.clone());
  return response;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin");

    if (origin && !ALLOWED_ORIGINS.has(origin)) {
      return jsonResponse({ error: "Origin not allowed" }, 403, {
        "Cache-Control": "no-store"
      });
    }

    if (request.method === "OPTIONS") {
      if (!origin || !ALLOWED_ORIGINS.has(origin)) {
        return new Response(null, { status: 403 });
      }
      return corsResponse(request, new Response(null, {
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": origin,
          "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
          "Access-Control-Max-Age": "86400",
          "Vary": "Origin"
        }
      }));
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return corsResponse(request, jsonResponse({ error: "Method not allowed" }, 405));
    }

    if (url.pathname === "/api/health") {
      return corsResponse(request, jsonResponse({
        status: "ok",
        service: "GTMS NOAA proxy",
        products: ["SIGMET", "PIREP"],
        cacheSeconds: CACHE_TTL_SECONDS,
        utc: new Date().toISOString()
      }));
    }

    const product = url.pathname === "/api/sigmets"
      ? "sigmets"
      : url.pathname === "/api/pireps"
        ? "pireps"
        : null;

    if (!product) {
      return corsResponse(request, jsonResponse({ error: "Not found" }, 404));
    }

    try {
      const response = await getCachedUpstream(url.pathname, UPSTREAMS[product]);
      return corsResponse(request, response);
    } catch (error) {
      return corsResponse(request, jsonResponse({
        error: "Unable to retrieve NOAA aviation weather",
        detail: String(error?.message || error)
      }, 502, { "Cache-Control": "no-store" }));
    }
  }
};
