from __future__ import annotations

import math
import os
import threading
import requests
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eccodes import (
    codes_get,
    codes_grib_find_nearest_multiple,
    codes_grib_new_from_file,
    codes_release,
)
from ecmwf.opendata import Client
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "1.0.0"
CACHE_DIR = Path(os.getenv("GTMS_GRIB_CACHE", "/tmp/gtms-ecmwf"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FORECAST_STEPS = list(range(0, 49, 6))
GFS_FORECAST_STEPS = list(range(0, 73, 6))
PRESSURE_LEVELS = [200, 250, 300, 500]
NOAA_GFS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
LOCK = threading.RLock()
ROW_CACHE: dict[tuple[str, str, str, str, int], tuple[float, dict]] = {}
ROW_CACHE_TTL = 6 * 3600

app = FastAPI(
    title="GTMS ECMWF Open Data Gateway",
    version=APP_VERSION,
    description="Direct ECMWF AIFS/IFS Open Data access for GTMS. No Open-Meteo API dependency.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://gtms-static.onrender.com",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class ForecastRequest(BaseModel):
    model: str
    points: list[Point] = Field(min_length=1, max_length=84)
    hours: int = Field(default=48, ge=6, le=48)


def _run_candidates() -> list[datetime]:
    now = datetime.now(timezone.utc)
    cycle_hour = (now.hour // 6) * 6
    anchor = now.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    return [anchor - timedelta(hours=6 * n) for n in range(0, 5)]


def _cache_file(model: str, cycle: datetime, part: str) -> Path:
    stamp = cycle.strftime("%Y%m%d_%H")
    return CACHE_DIR / f"{model}_{stamp}_{part}.grib2"


def _retrieve(model: str, cycle: datetime, part: str) -> Path:
    path = _cache_file(model, cycle, part)
    if path.exists() and path.stat().st_size > 1000:
        # ECMWF forecasts are issued every six hours; keep a run for up to 8 hours.
        if time.time() - path.stat().st_mtime < 8 * 3600:
            return path
        path.unlink(missing_ok=True)

    source_model = "aifs-single" if model == "aifs" else "ifs"
    request: dict[str, Any] = {
        "date": cycle.strftime("%Y%m%d"),
        "time": cycle.hour,
        "stream": "oper",
        "type": "fc",
        "step": FORECAST_STEPS,
    }
    if part == "pressure":
        request.update({
            "levtype": "pl",
            "levelist": PRESSURE_LEVELS,
            "param": ["u", "v", "t", "z", "w"],
        })
    else:
        request.update({
            "levtype": "sfc",
            # AIFS does not publish MUCAPE; IFS does. Both publish TP and TCC.
            "param": (["mucape", "tp", "tcc"] if model == "ifs" else ["tp", "tcc"]),
        })

    temp_path = path.with_suffix(".part")
    errors: list[str] = []
    # The first AWS mirror may temporarily return HTTP 503. Do not let the
    # library's default 500 retries × 120 seconds leave browser requests pending.
    # Try the other official ECMWF mirrors with one short retry each.
    for source in ("aws", "google", "azure", "ecmwf"):
        temp_path.unlink(missing_ok=True)
        try:
            client = Client(
                source=source,
                model=source_model,
                resol="0p25",
                maximum_retries=1,
                retry_after=3,
                use_server_retry_after=False,
            )
            client.retrieve(request=request, target=str(temp_path))
            if not temp_path.exists() or temp_path.stat().st_size < 1000:
                raise RuntimeError("ECMWF에서 비어 있거나 불완전한 GRIB 파일을 받았습니다.")
            temp_path.replace(path)
            return path
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}: {exc}")
            temp_path.unlink(missing_ok=True)

    raise RuntimeError(
        "ECMWF 미러(AWS/Google/Azure/ECMWF)에서 자료를 받지 못했습니다. "
        + " | ".join(errors[-4:])
    )

def _get_bundle(model: str) -> tuple[datetime, Path, Path | None]:
    errors: list[str] = []
    for cycle in _run_candidates():
        try:
            pressure = _retrieve(model, cycle, "pressure")
            surface: Path | None = None
            try:
                surface = _retrieve(model, cycle, "surface")
            except Exception:
                # Pressure-level CAT fields remain usable if surface convective fields
                # are temporarily absent from a newly disseminated cycle.
                surface = None
            return cycle, pressure, surface
        except Exception as exc:
            errors.append(f"{cycle:%Y-%m-%d %H}Z: {exc}")
    raise RuntimeError("최근 ECMWF 예보 실행을 찾지 못했습니다. " + " | ".join(errors[-3:]))


def _read_fields(path: Path, points: list[Point]) -> dict[tuple[str, int, int], list[float | None]]:
    fields: dict[tuple[str, int, int], list[float | None]] = {}
    with path.open("rb") as stream:
        while True:
            gid = codes_grib_new_from_file(stream)
            if gid is None:
                break
            try:
                short_name = str(codes_get(gid, "shortName")).lower()
                level = int(codes_get(gid, "level"))
                try:
                    step = int(codes_get(gid, "endStep"))
                except Exception:
                    step = int(codes_get(gid, "step"))
                try:
                    missing_value = float(codes_get(gid, "missingValue"))
                except Exception:
                    missing_value = float("nan")
                nearest = codes_grib_find_nearest_multiple(
                    gid,
                    False,
                    [point.lat for point in points],
                    [point.lon for point in points],
                )
                values: list[float | None] = []
                for item in nearest:
                    value = float(item.value)
                    is_missing = (
                        not math.isfinite(value)
                        or (math.isfinite(missing_value) and value == missing_value)
                    )
                    values.append(None if is_missing else value)
                fields[(short_name, level, step)] = values
            finally:
                codes_release(gid)
    return fields


def _field(fields: dict, name: str, level: int, step: int, index: int):
    aliases = {
        "z": ("z", "gh"),
        "mucape": ("mucape", "cape"),
        "cape": ("cape", "mucape"),
        "tp": ("tp", "apcp"),
        "tcc": ("tcc", "tcdc"),
    }
    for candidate in aliases.get(name, (name,)):
        values = fields.get((candidate, level, step))
        if values and index < len(values):
            return values[index]
    return None

def _build_rows(
    model: str,
    cycle: datetime,
    points: list[Point],
    pressure_fields: dict,
    surface_fields: dict,
    hours: int,
) -> list[dict]:
    available_steps = GFS_FORECAST_STEPS if model == "gfs" else FORECAST_STEPS
    elapsed_hours = max(0, int((datetime.now(timezone.utc) - cycle).total_seconds() // 3600))
    last_step = min(max(available_steps), hours + elapsed_hours)
    steps = [step for step in available_steps if step <= last_step]
    rows: list[dict] = []
    for point_index, _ in enumerate(points):
        hourly: dict[str, list] = {
            "time": [],
            "cape": [],
            "precipitation": [],
            "cloud_cover": [],
            "vertical_velocity_500hPa": [],
        }
        for level in [300, 250, 200]:
            for name in ["wind_speed", "wind_direction", "temperature", "geopotential_height"]:
                hourly[f"{name}_{level}hPa"] = []

        previous_tp: float | None = None
        previous_step: int | None = None
        for step in steps:
            valid = cycle + timedelta(hours=step)
            hourly["time"].append(valid.strftime("%Y-%m-%dT%H:%M"))
            wind_by_level: dict[int, tuple[float | None, float | None]] = {}
            for level in [300, 250, 200]:
                u = _field(pressure_fields, "u", level, step, point_index)
                v = _field(pressure_fields, "v", level, step, point_index)
                t = _field(pressure_fields, "t", level, step, point_index)
                z = _field(pressure_fields, "z", level, step, point_index)
                if u is not None and v is not None:
                    speed = math.hypot(u, v)
                    direction = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
                    wind_by_level[level] = (speed, direction)
                else:
                    wind_by_level[level] = (None, None)
                hourly[f"wind_speed_{level}hPa"].append(
                    round(wind_by_level[level][0], 3) if wind_by_level[level][0] is not None else None
                )
                hourly[f"wind_direction_{level}hPa"].append(
                    round(wind_by_level[level][1], 2) if wind_by_level[level][1] is not None else None
                )
                hourly[f"temperature_{level}hPa"].append(
                    round(t - 273.15, 3) if t is not None else None
                )
                hourly[f"geopotential_height_{level}hPa"].append(
                    round(z if model == "gfs" else z / 9.80665, 2) if z is not None else None
                )

            omega = _field(pressure_fields, "w", 500, step, point_index)
            cape = _field(surface_fields, "mucape", 0, step, point_index)
            tp = _field(surface_fields, "tp", 0, step, point_index)
            cloud = _field(surface_fields, "tcc", 0, step, point_index)
            # ECMWF total cloud cover is stored as a 0–1 fraction; GTMS expects percent.
            cloud_percent = cloud * 100.0 if cloud is not None and 0.0 <= cloud <= 1.0 else cloud

            hourly["vertical_velocity_500hPa"].append(round(omega, 6) if omega is not None else None)
            hourly["cape"].append(round(cape, 2) if cape is not None else None)
            hourly["cloud_cover"].append(round(cloud_percent, 2) if cloud_percent is not None else None)

            precipitation = None
            if tp is not None:
                if previous_tp is None or previous_step is None or step <= previous_step:
                    precipitation = 0.0 if step == 0 else None
                else:
                    elapsed = step - previous_step
                    precipitation = max(0.0, (tp - previous_tp) * 1000.0 / elapsed)
                previous_tp, previous_step = tp, step
            hourly["precipitation"].append(
                round(precipitation, 3) if precipitation is not None else None
            )

        rows.append({"hourly": hourly})
    return rows


def _gfs_complete_cycles() -> list[datetime]:
    now = datetime.now(timezone.utc)
    current_hour = (now.hour // 6) * 6
    current = now.replace(hour=current_hour, minute=0, second=0, microsecond=0)
    # NOMADS output for a newly started cycle may not yet be complete.
    candidates = [current - timedelta(hours=6 * n) for n in range(0, 5)]
    return [cycle for cycle in candidates if (now - cycle).total_seconds() >= 4 * 3600] or candidates[1:]


def _gfs_cache_file(cycle: datetime) -> Path:
    return CACHE_DIR / f"noaa_gfs_{cycle:%Y%m%d_%H}_0p25.grib2"


def _download_gfs_cycle(cycle: datetime, points: list[Point], hours: int) -> Path:
    cache_path = _gfs_cache_file(cycle)
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        if time.time() - cache_path.stat().st_mtime < 6 * 3600:
            return cache_path
        cache_path.unlink(missing_ok=True)

    # NOMADS uses 0..360 degree east longitude for the 0.25-degree global grid.
    lons = [point.lon % 360.0 for point in points]
    left = max(0.0, math.floor(min(lons) - 1.0))
    right = min(359.75, math.ceil(max(lons) + 1.0))
    bottom = max(-89.75, math.floor(min(p.lat for p in points) - 1.0))
    top = min(89.75, math.ceil(max(p.lat for p in points) + 1.0))
    if right - left > 180 or top - bottom > 90:
        raise RuntimeError(
            "NOAA GFS 영역이 너무 넓습니다. 지도를 분석 지역으로 확대하거나 항로 분석 구간을 나눠 주세요."
        )

    # Use 6-hourly forecast slices. Data are cached as one multi-message GRIB2 file.
    elapsed = max(0, (datetime.now(timezone.utc) - cycle).total_seconds() / 3600.0)
    first_step = min(72, max(0, int(math.ceil(elapsed / 6.0) * 6)))
    final_step = min(72, int(math.ceil((elapsed + hours) / 6.0) * 6))
    steps = [step for step in GFS_FORECAST_STEPS if first_step <= step <= final_step]
    if not steps:
        steps = [min(72, first_step)]

    temp_path = cache_path.with_suffix(".part")
    errors: list[str] = []
    session = requests.Session()
    session.headers.update({
        "User-Agent": "GTMS-Weather-Gateway/1.0 (NOAA NOMADS public data client)",
        "Accept": "*/*",
    })
    try:
        with temp_path.open("wb") as output:
            for step in steps:
                file_name = f"gfs.t{cycle:%H}z.pgrb2.0p25.f{step:03d}"
                params = {
                    "file": file_name,
                    "dir": f"/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos",
                    "subregion": "",
                    "leftlon": f"{left:.2f}",
                    "rightlon": f"{right:.2f}",
                    "toplat": f"{top:.2f}",
                    "bottomlat": f"{bottom:.2f}",
                    "var_UGRD": "on",
                    "var_VGRD": "on",
                    "var_TMP": "on",
                    "var_HGT": "on",
                    "var_VVEL": "on",
                    "var_CAPE": "on",
                    "var_APCP": "on",
                    "var_TCDC": "on",
                    "lev_200_mb": "on",
                    "lev_250_mb": "on",
                    "lev_300_mb": "on",
                    "lev_500_mb": "on",
                    "lev_surface": "on",
                    "lev_entire_atmosphere_single_layer": "on",
                }
                try:
                    response = session.get(
                        NOAA_GFS_FILTER,
                        params=params,
                        timeout=(12, 45),
                    )
                    response.raise_for_status()
                    content = response.content
                    if len(content) < 1000 or not content.startswith(b"GRIB"):
                        preview = content[:180].decode("utf-8", "replace")
                        raise RuntimeError(
                            f"NOMADS f{step:03d} 응답이 GRIB2가 아닙니다: {preview}"
                        )
                    output.write(content)
                    output.flush()
                except Exception as exc:
                    errors.append(f"f{step:03d}: {type(exc).__name__}: {exc}")
                    raise RuntimeError(
                        "NOAA NOMADS GFS 자료 다운로드 실패 (" + "; ".join(errors[-3:]) + ")"
                    ) from exc
                time.sleep(0.25)
        if not temp_path.exists() or temp_path.stat().st_size < 1000:
            raise RuntimeError("NOAA GFS GRIB2 자료가 비어 있습니다.")
        temp_path.replace(cache_path)
        return cache_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        session.close()


def _get_gfs_bundle(points: list[Point], hours: int) -> tuple[datetime, Path]:
    errors: list[str] = []
    for cycle in _gfs_complete_cycles():
        try:
            return cycle, _download_gfs_cycle(cycle, points, hours)
        except Exception as exc:
            errors.append(f"{cycle:%Y-%m-%d %H}Z: {exc}")
    raise RuntimeError("NOAA GFS 공개 자료를 가져오지 못했습니다. " + " | ".join(errors[-3:]))


@app.get("/api/noaa-smoke")
def noaa_smoke(lat: float = 37.5, lon: float = 127.0):
    point = Point(lat=lat, lon=lon)
    try:
        with LOCK:
            cycle, path = _get_gfs_bundle([point], 12)
            fields = _read_fields(path, [point])
            rows = _build_rows("gfs", cycle, [point], fields, fields, 12)
        hourly = rows[0]["hourly"]
        required = [
            "wind_speed_300hPa", "wind_speed_250hPa", "wind_speed_200hPa",
            "temperature_250hPa", "geopotential_height_250hPa",
        ]
        counts = {
            key: sum(value is not None for value in hourly.get(key, []))
            for key in required
        }
        return {
            "status": "ok" if any(counts.values()) else "no_data",
            "source": "NOAA NOMADS GFS 0.25 degree",
            "cycle_utc": cycle.strftime("%Y-%m-%dT%H:%MZ"),
            "cache_file_bytes": path.stat().st_size,
            "forecast_times_utc": hourly["time"],
            "valid_values_by_field": counts,
            "sample_wind_speed_250hPa_ms": hourly["wind_speed_250hPa"],
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"NOAA GFS self-test failed: {type(exc).__name__}: {exc}") from exc


@app.get("/")
def root():
    return health()


@app.head("/")
def root_head():
    return Response(status_code=200)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "GTMS ECMWF Open Data Gateway",
        "version": APP_VERSION,
        "source": "ECMWF Open Data + NOAA NOMADS GFS 0.25",
        "fallback_source": "NOAA NOMADS GFS",
        "noaa_gfs_filter": NOAA_GFS_FILTER,
        "open_meteo_dependency": False,
        "license": "CC-BY-4.0",
        "forecast_step_hours": 6,
        "forecast_coverage_hours": 48,
        "mirror_fallbacks": ["aws", "google", "azure", "ecmwf"],
        "retry_policy": {"maximum_retries": 1, "retry_after_seconds": 3},
    }


@app.post("/api/forecast")
def forecast(request: ForecastRequest):
    model_alias = request.model.strip().lower()
    if model_alias in ("ecmwf_aifs025_single", "aifs", "aifs-single"):
        model = "aifs"
    elif model_alias in ("ecmwf_ifs025", "ifs"):
        model = "ifs"
    else:
        raise HTTPException(status_code=400, detail="지원하지 않는 모델입니다. aifs 또는 ifs를 사용하세요.")

    points = request.points
    source_errors: list[str] = []
    try:
        with LOCK:
            try:
                cycle, pressure_path, surface_path = _get_bundle(model)
                pressure_fields = _read_fields(pressure_path, points)
                surface_fields = _read_fields(surface_path, points) if surface_path else {}
                rows = _build_rows(
                    model, cycle, points, pressure_fields, surface_fields, request.hours
                )
                # A syntactically valid GRIB may still contain no usable requested fields.
                if not any(
                    row.get("hourly", {}).get("wind_speed_250hPa")
                    and any(value is not None for value in row["hourly"]["wind_speed_250hPa"])
                    for row in rows
                ):
                    raise RuntimeError("ECMWF 자료에서 항공 고도 바람 변수를 찾지 못했습니다.")
                for row in rows:
                    row["source"] = "ECMWF " + model.upper()
                return rows
            except Exception as exc:
                source_errors.append(f"ECMWF: {type(exc).__name__}: {exc}")

            # Independent fallback: NOAA's official GFS 0.25-degree GRIB filter.
            try:
                cycle, gfs_path = _get_gfs_bundle(points, request.hours)
                fields = _read_fields(gfs_path, points)
                rows = _build_rows("gfs", cycle, points, fields, fields, request.hours)
                usable = [
                    row for row in rows
                    if any(value is not None for value in row["hourly"]["wind_speed_250hPa"])
                ]
                if not usable:
                    raise RuntimeError(
                        "GFS GRIB에서 U/V 바람 자료를 찾지 못했습니다. 요청 변수·고도 조합을 확인해야 합니다."
                    )
                for row in rows:
                    row["source"] = "NOAA GFS 0.25°"
                return rows
            except Exception as exc:
                source_errors.append(f"NOAA GFS: {type(exc).__name__}: {exc}")
                raise RuntimeError("모든 공개 기상자료 수신에 실패했습니다. " + " | ".join(source_errors)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"기상자료 처리 실패: {exc}",
        ) from exc
