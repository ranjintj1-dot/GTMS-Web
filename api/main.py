from __future__ import annotations

import math
import os
import threading
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
PRESSURE_LEVELS = [200, 250, 300, 500]
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
    values = fields.get((name, level, step))
    return values[index] if values and index < len(values) else None


def _build_rows(
    model: str,
    cycle: datetime,
    points: list[Point],
    pressure_fields: dict,
    surface_fields: dict,
    hours: int,
) -> list[dict]:
    steps = [step for step in FORECAST_STEPS if step <= hours]
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
                    round(z / 9.80665, 2) if z is not None else None
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
        "source": "ECMWF Open Data AWS mirror",
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
    try:
        # One process-wide lock keeps GRIB downloads and decoding from stampeding.
        with LOCK:
            cycle, pressure_path, surface_path = _get_bundle(model)
            now = time.time()
            cycle_key = cycle.strftime("%Y%m%d%H")
            keys = [
                (model, cycle_key, f"{point.lat:.4f}", f"{point.lon:.4f}", request.hours)
                for point in points
            ]
            rows_by_key: dict[tuple, dict] = {}
            missing_indices: list[int] = []
            for index, key in enumerate(keys):
                cached = ROW_CACHE.get(key)
                if cached and now - cached[0] < ROW_CACHE_TTL:
                    rows_by_key[key] = cached[1]
                else:
                    if cached:
                        ROW_CACHE.pop(key, None)
                    missing_indices.append(index)

            if missing_indices:
                missing_points = [points[index] for index in missing_indices]
                pressure_fields = _read_fields(pressure_path, missing_points)
                surface_fields = _read_fields(surface_path, missing_points) if surface_path else {}
                fresh_rows = _build_rows(
                    model, cycle, missing_points, pressure_fields, surface_fields, request.hours
                )
                for index, row in zip(missing_indices, fresh_rows):
                    key = keys[index]
                    ROW_CACHE[key] = (now, row)
                    rows_by_key[key] = row

            # Keep the in-memory cache bounded on the small free instance.
            if len(ROW_CACHE) > 25000:
                oldest = sorted(ROW_CACHE.items(), key=lambda item: item[1][0])
                for key, _ in oldest[: max(1, len(ROW_CACHE) - 18000)]:
                    ROW_CACHE.pop(key, None)

            return [rows_by_key[key] for key in keys]
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ECMWF {model.upper()} 자료 처리 실패: {exc}",
        ) from exc
