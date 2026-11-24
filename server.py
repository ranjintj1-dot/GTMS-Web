from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs, urlencode
import base64, hmac
import json, math, datetime, os

HOST, PORT = "0.0.0.0", int(os.environ.get("PORT", "8000"))
APP_USER = os.environ.get("GTMS_USER", "").strip()
APP_PASSWORD = os.environ.get("GTMS_PASSWORD", "")
AWC = "https://aviationweather.gov/api/data"
OM = "https://api.open-meteo.com/v1/forecast"
UA = "GTMS-Airline-Safety-Prototype/1.0"

def fetch_json(url, timeout=30):
    req=Request(url,headers={"User-Agent":UA,"Accept":"application/geo+json, application/json"})
    with urlopen(req,timeout=timeout) as response:
        body=response.read()
        if not body: return {"type":"FeatureCollection","features":[]}
        return json.loads(body.decode("utf-8"))

class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        raw=json.dumps(data,ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store")
        self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        u=urlparse(self.path); path=u.path; q=parse_qs(u.query)
        # Optional HTTP Basic Auth. Configure both GTMS_USER and GTMS_PASSWORD
        # in the hosting provider before making the service publicly accessible.
        if APP_USER and APP_PASSWORD and path != "/api/health":
            supplied = self.headers.get("Authorization", "")
            expected = "Basic " + base64.b64encode(f"{APP_USER}:{APP_PASSWORD}".encode()).decode()
            if not hmac.compare_digest(supplied, expected):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="GTMS"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        try:
            if path=="/" or path=="/index.html":
                p=os.path.join(os.path.dirname(__file__),"index.html")
                with open(p,"rb") as f: raw=f.read()
                self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if path=="/api/health":
                return self.send_json({"status":"ok","utc":datetime.datetime.now(datetime.timezone.utc).isoformat()})
            if path=="/api/noaa/sigmets":
                errors=[]
                for product in ("airsigmet","sigmet"):
                    try:
                        data=fetch_json(f"{AWC}/{product}?format=geojson")
                        return self.send_json(data)
                    except Exception as e: errors.append(str(e))
                return self.send_json({"error":"NOAA SIGMET feed unavailable","detail":errors},502)
            if path=="/api/noaa/pireps":
                errors=[]
                for fmt in ("geojson","json"):
                    try:
                        data=fetch_json(f"{AWC}/aircraftreport?format={fmt}&age=6")
                        if isinstance(data,list):
                            feats=[]
                            for x in data:
                                lat=x.get("lat") or x.get("latitude");lon=x.get("lon") or x.get("longitude")
                                geom={"type":"Point","coordinates":[float(lon),float(lat)]} if lat is not None and lon is not None else None
                                feats.append({"type":"Feature","geometry":geom,"properties":x})
                            data={"type":"FeatureCollection","features":feats}
                        return self.send_json(data)
                    except Exception as e: errors.append(str(e))
                return self.send_json({"error":"NOAA PIREP feed unavailable","detail":errors},502)
            if path=="/api/forecast":
                lat=float(q.get("lat",[""])[0]);lon=float(q.get("lon",[""])[0])
                alt=float(q.get("alt_ft",["35000"])[0]);hours=max(1,min(48,int(q.get("hours",["48"])[0])))
                if not(-90<=lat<=90 and -180<=lon<=180): raise ValueError("Invalid coordinates")
                levels={20000:(500,400),25000:(400,300),30000:(300,250),35000:(250,200),39000:(200,150),41000:(200,150)}
                lo,hi=min(levels.items(),key=lambda kv:abs(kv[0]-alt))[1]
                hourly=",".join([f"wind_speed_{lo}hPa",f"wind_speed_{hi}hPa",f"wind_direction_{lo}hPa",f"wind_direction_{hi}hPa",f"temperature_{lo}hPa",f"temperature_{hi}hPa"])
                params={"latitude":lat,"longitude":lon,"hourly":hourly,"forecast_days":2,"timezone":"UTC","models":"ecmwf_ifs025"}
                data=fetch_json(OM+"?"+urlencode(params))
                h=data.get("hourly",{});times=h.get("time",[])
                if not times: raise RuntimeError("No hourly forecast values")
                now=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None);idx=0
                for i,t in enumerate(times):
                    try:
                        if datetime.datetime.fromisoformat(t)>=now: idx=i;break
                    except Exception: pass
                def val(key):
                    arr=h.get(key,[]);return arr[idx] if idx<len(arr) else None
                s1,s2=val(f"wind_speed_{lo}hPa"),val(f"wind_speed_{hi}hPa")
                d1,d2=val(f"wind_direction_{lo}hPa"),val(f"wind_direction_{hi}hPa")
                shear=None
                if None not in (s1,s2,d1,d2):
                    u1=-s1*math.sin(math.radians(d1));v1=-s1*math.cos(math.radians(d1))
                    u2=-s2*math.sin(math.radians(d2));v2=-s2*math.cos(math.radians(d2))
                    shear=math.sqrt((u2-u1)**2+(v2-v1)**2)
                score=0 if shear is None else (4 if shear>=25 else 3 if shear>=18 else 2 if shear>=12 else 1 if shear>=7 else 0)
                category=["낮음","낮음","주의","높음","매우 높음"][score]
                return self.send_json({"time":times[idx],"wind_speed":s1,"wind_direction":d1,
                    "shear":round(shear,1) if shear is not None else None,"score":score,"category":category,
                    "pressure_levels_hpa":[lo,hi],"source":"Open-Meteo / ECMWF IFS pressure-level wind; heuristic vector-shear screen"})
            return self.send_json({"error":"Not found"},404)
        except Exception as e:
            return self.send_json({"error":str(e)},502)

    def log_message(self, fmt, *args):
        print("%s - %s"%(self.address_string(),fmt%args))

if __name__=="__main__":
    print(f"GTMS listening on {HOST}:{PORT}")
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()