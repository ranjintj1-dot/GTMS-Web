# GTMS NOAA Proxy — Cloudflare Worker

This Worker is a narrow, read-only proxy for NOAA Aviation Weather Center data.
It is not a general-purpose open proxy.

## Endpoints

- `GET /api/health` — Worker health and configured products.
- `GET /api/sigmets` — Current worldwide SIGMET GeoJSON.
- `GET /api/pireps` — PIREP/AIREP GeoJSON, recent 6-hour window.

Responses are cached for 60 seconds to avoid repeatedly querying NOAA.
The Worker accepts browser requests only from:

`https://gtms-static.onrender.com`

## Deploy through Cloudflare Workers Builds

1. Open Cloudflare Dashboard → **Workers & Pages** → **Create application**.
2. Choose **Workers** / **Connect to Git** (GitHub).
3. Authorize the Cloudflare GitHub application and select `ranjintj1-dot/GTMS-Web`.
4. Set the Worker name to **gtms-noaa-proxy**.
5. Set the root directory to **cloudflare**.
6. Leave the build command empty. Use the default deploy command `npx wrangler deploy`.
7. Save and deploy. Cloudflare provides a URL similar to:
   `https://gtms-noaa-proxy.<your-account-subdomain>.workers.dev`
8. Test these URLs:
   - `https://<worker-url>/api/health`
   - `https://<worker-url>/api/sigmets`
   - `https://<worker-url>/api/pireps`

## Connect the URL to GTMS

After Cloudflare displays the actual `workers.dev` URL, put that exact URL in the root file
`noaa-proxy-config.js` as the value of `window.GTMS_NOAA_PROXY_URL`, then commit the change
to `main`. Render automatically publishes the updated static website.

Do not add API keys or passwords to this Worker. The NOAA public API does not require an API key.

## Data notes

- NOAA identifies SIGMET coverage as worldwide.
- NOAA PIREP/AIREP coverage is primarily the United States and North Atlantic, not a complete global pilot-report feed.
- NOAA documents a limit of 100 API requests per minute and asks clients not to query an endpoint more frequently than once per minute per thread. The Worker cache therefore uses a 60-second TTL.
- These public feeds are situational-awareness references and should not replace approved operational weather sources or company procedures.
