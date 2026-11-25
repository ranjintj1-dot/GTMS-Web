from __future__ import annotations

import math
import os
import logging
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
    codes_get_values,
    codes_release,
)
from ecmwf.opendata import Client
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "1.0.7"
APP_RELEASE_ID = "gtms-noaa-primary-20261125-r4"
logger = logging.getLogger("uvicorn.error")
CACHE_DIR = Path(os.getenv("GTMS_GRIB_CACHE", "/tmp/gtms-ecmwf"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
FORECAST_STEPS = list(range(0, 49, 6))
GFS_FORECAST_STEPS = list(range(0, 73, 6))
PRESSURE_LEVELS = [200, 250, 300, 500]
NOAA_GFS_FILTER = "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
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
    # Render may serve previews/custom subdomains; allow HTTPS pages hosted on
    # Render while keeping arbitrary external origins blocked.
    allow_origin_regex=r"https://[a-zA-Z0-9-]+\\.onrender\\.com",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
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


def _sample_regular_global_ll(
    gid: Any,
    points: list[Point],
    missing_value: float,
) -> list[float | None] | None:
    """
    Fast point sampling for global regular lat/lon GRIB grids such as NOAA GFS 0.25°.
    ecCodes' nearest-search helper can be expensive when called for dozens of points
    across the whole globe. For regular_ll grids, compute the nearest flattened array
    indices directly. Return None for any unsupported scan layout so the safe ecCodes
    nearest-search path can be used instead.
    """
    try:
        if str(codes_get(gid, "gridType")) != "regular_ll":
            return None

        ni = int(codes_get(gid, "Ni"))
        nj = int(codes_get(gid, "Nj"))
        lat0 = float(codes_get(gid, "latitudeOfFirstGridPointInDegrees"))
        lon0 = float(codes_get(gid, "longitudeOfFirstGridPointInDegrees"))
        di = float(codes_get(gid, "iDirectionIncrementInDegrees"))
        dj = float(codes_get(gid, "jDirectionIncrementInDegrees"))
        i_negative = bool(int(codes_get(gid, "iScansNegatively")))
        j_positive = bool(int(codes_get(gid, "jScansPositively")))
        j_consecutive = bool(int(codes_get(gid, "jPointsAreConsecutive")))
        alternate_rows = bool(int(codes_get(gid, "alternativeRowScanning")))

        # This shortcut is intentionally restricted to a complete global grid.
        if ni <= 0 or nj <= 0 or di <= 0 or dj <= 0:
            return None
        if abs(ni * di - 360.0) > max(0.01, di * 0.1):
            return None
        if j_consecutive and alternate_rows:
            return None

        data = codes_get_values(gid)
        if len(data) != ni * nj:
            return None

        first_lon = lon0 % 360.0
        result: list[float | None] = []
        for point in points:
            target_lon = point.lon % 360.0
            if i_negative:
                i = int(round(((first_lon - target_lon) % 360.0) / di)) % ni
            else:
                i = int(round(((target_lon - first_lon) % 360.0) / di)) % ni

            if j_positive:
                j = int(round((point.lat - lat0) / dj))
            else:
                j = int(round((lat0 - point.lat) / dj))
            if j < 0 or j >= nj:
                return None

            if alternate_rows and (j % 2 == 1):
                i = ni - 1 - i

            flat_index = i * nj + j if j_consecutive else j * ni + i
            value = float(data[flat_index])
            is_missing = (
                not math.isfinite(value)
                or (math.isfinite(missing_value) and value == missing_value)
            )
            result.append(None if is_missing else value)
        return result
    except Exception:
        return None


def _read_fields(path: Path, points: list[Point]) -> dict[tuple[str, int, int], list[float | None]]:
    fields: dict[tuple[str, int, int], list[float | None]] = {}
    message_count = 0
    started = time.monotonic()
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

                values = _sample_regular_global_ll(gid, points, missing_value)
                if values is None:
                    nearest = codes_grib_find_nearest_multiple(
                        gid,
                        False,
                        [point.lat for point in points],
                        [point.lon for point in points],
                    )
                    values = []
                    for item in nearest:
                        value = float(item.value)
                        is_missing = (
                            not math.isfinite(value)
                            or (math.isfinite(missing_value) and value == missing_value)
                        )
                        values.append(None if is_missing else value)

                fields[(short_name, level, step)] = values
                message_count += 1
            finally:
                codes_release(gid)

    logger.info(
        "GTMS GRIB extraction complete: messages=%s points=%s elapsed_seconds=%.2f",
        message_count, len(points), time.monotonic() - started,
    )
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


def _gfs_cache_file(cycle: datetime, points: list[Point]) -> Path:
    lons = [point.lon % 360.0 for point in points]
    left = max(0.0, math.floor(min(lons) - 1.0))
    right = min(359.75, math.ceil(max(lons) + 1.0))
    bottom = max(-89.75, math.floor(min(point.lat for point in points) - 1.0))
    top = min(89.75, math.ceil(max(point.lat for point in points) + 1.0))
    region = f"{left:.0f}_{right:.0f}_{bottom:.0f}_{top:.0f}"
    return CACHE_DIR / f"noaa_gfs_{cycle:%Y%m%d_%H}_{region}_0p25.grib2"


def _download_gfs_cycle(cycle: datetime, points: list[Point], hours: int) -> Path:
    cache_path = _gfs_cache_file(cycle, points)
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        if time.time() - cache_path.stat().st_mtime < 6 * 3600:
            return cache_path
        cache_path.unlink(missing_ok=True)

    # NOAA's anonymous public S3 bucket serves the operational GFS GRIB2 files
    # together with byte-offset indexes. Pull only the required GRIB messages.
    file_name = f"gfs.t{cycle:%H}z.pgrb2.0p25"
    base_url = (
        f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
        f"gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/{file_name}"
    )
    lons = [point.lon % 360.0 for point in points]
    left = max(0.0, math.floor(min(lons) - 1.0))
    right = min(359.75, math.ceil(max(lons) + 1.0))
    bottom = max(-89.75, math.floor(min(p.lat for p in points) - 1.0))
    top = min(89.75, math.ceil(max(p.lat for p in points) + 1.0))
    # NOAA GRIB messages are global fields. Sample the requested points from the
    # global grid with ecCodes; do not reject wide viewports or dateline crossings.

    elapsed = max(0, (datetime.now(timezone.utc) - cycle).total_seconds() / 3600.0)
    first_step = min(72, max(0, int(math.ceil(elapsed / 6.0) * 6)))
    final_step = min(72, int(math.ceil((elapsed + hours) / 6.0) * 6))
    steps = [step for step in GFS_FORECAST_STEPS if first_step <= step <= final_step]
    if not steps:
        steps = [min(72, first_step)]

    session = requests.Session()
    session.headers.update({
        "User-Agent": "GTMS-Weather-Gateway/1.0 (NOAA GFS public AWS data client)",
        "Accept-Encoding": "identity",
    })
    temp_path = cache_path.with_suffix(".part")
    downloaded_messages = 0
    try:
        with temp_path.open("wb") as output:
            for step in steps:
                object_url = f"{base_url}.f{step:03d}"
                index_response = session.get(object_url + ".idx", timeout=(12, 45))
                index_response.raise_for_status()
                index_lines = [
                    line.strip() for line in index_response.text.splitlines() if line.strip()
                ]
                entries: list[dict[str, Any]] = []
                for line in index_lines:
                    parts = line.split(":")
                    if len(parts) < 5:
                        continue
                    try:
                        offset = int(parts[1])
                    except ValueError:
                        continue
                    entries.append({
                        "offset": offset,
                        "variable": parts[3].strip().upper(),
                        "level": parts[4].strip().lower(),
                        "line": line,
                    })
                if not entries:
                    raise RuntimeError(f"GFS f{step:03d} 색인(.idx)을 해석하지 못했습니다.")

                desired = []
                for index, entry in enumerate(entries):
                    variable = entry["variable"]
                    level = entry["level"]
                    wanted = (
                        variable in ("UGRD", "VGRD", "TMP", "HGT")
                        and any(level.startswith(f"{p} mb") for p in (200, 250, 300))
                    ) or (variable == "VVEL" and level.startswith("500 mb"))
                    if not wanted:
                        continue
                    next_offset = (
                        entries[index + 1]["offset"]
                        if index + 1 < len(entries)
                        else None
                    )
                    desired.append({
                        "start": entry["offset"],
                        "end": next_offset - 1 if next_offset is not None else None,
                        "variable": variable,
                        "level": level,
                    })

                if not desired:
                    raise RuntimeError(
                        f"GFS f{step:03d} 색인에 상층 U/V/T/HGT 메시지가 없습니다."
                    )

                # Determine the final message length only when an indexed message
                # extends to the end of the GRIB object.
                if any(item["end"] is None for item in desired):
                    head = session.head(object_url, timeout=(12, 30))
                    head.raise_for_status()
                    total_size = int(head.headers.get("Content-Length", "0"))
                    if total_size <= 0:
                        raise RuntimeError("GFS GRIB 객체 크기를 확인하지 못했습니다.")
                    for item in desired:
                        if item["end"] is None:
                            item["end"] = total_size - 1

                desired.sort(key=lambda item: item["start"])
                # Merge nearby messages into fewer S3 byte-range requests.
                groups: list[dict[str, Any]] = []
                for item in desired:
                    if groups and item["start"] - groups[-1]["end"] - 1 <= 256 * 1024:
                        groups[-1]["end"] = item["end"]
                    else:
                        groups.append({"start": item["start"], "end": item["end"]})

                for group in groups:
                    response = session.get(
                        object_url,
                        headers={"Range": f"bytes={group['start']}-{group['end']}"},
                        timeout=(12, 90),
                    )
                    response.raise_for_status()
                    if response.status_code != 206:
                        raise RuntimeError(
                            f"GFS S3 부분 다운로드가 HTTP {response.status_code}를 반환했습니다."
                        )
                    content = response.content
                    if len(content) < 100 or not content.startswith(b"GRIB"):
                        raise RuntimeError(
                            f"GFS f{step:03d} GRIB 메시지 범위가 올바르지 않습니다."
                        )
                    output.write(content)
                    downloaded_messages += len([
                        item for item in desired
                        if group["start"] <= item["start"] <= group["end"]
                    ])
                time.sleep(0.15)

        if not temp_path.exists() or temp_path.stat().st_size < 1000:
            raise RuntimeError("NOAA GFS GRIB2 자료가 비어 있습니다.")
        temp_path.replace(cache_path)
        logger.info(
            "NOAA GFS AWS data cached: cycle=%s bytes=%s indexed_messages=%s steps=%s",
            cycle.strftime("%Y%m%d%H"), cache_path.stat().st_size, downloaded_messages, steps,
        )
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
            "source": "NOAA GFS BDP S3 0.25 degree",
            "cycle_utc": cycle.strftime("%Y-%m-%dT%H:%MZ"),
            "cache_file_bytes": path.stat().st_size,
            "forecast_times_utc": hourly["time"],
            "valid_values_by_field": counts,
            "sample_wind_speed_250hPa_ms": hourly["wind_speed_250hPa"],
        }
    except Exception as exc:
        logger.exception("GTMS NOAA GFS self-test failed")
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
        "release": APP_RELEASE_ID,
        "source": "NOAA GFS BDP public S3 0.25 degree (primary)",
        "backup_source": "ECMWF AIFS / IFS Open Data",
        "noaa_gfs_source": "NOAA GFS BDP public S3 + .idx byte-range retrieval",
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
    if model_alias not in ("ecmwf_aifs025_single", "aifs", "aifs-single", "ecmwf_ifs025", "ifs"):
        raise HTTPException(status_code=400, detail="지원하지 않는 모델입니다. aifs 또는 ifs를 사용하세요.")

    points = request.points
    source_errors: list[str] = []
    with LOCK:
        # Primary source: NOAA GFS public S3. The .idx byte-range reader fetches
        # only upper-air messages, so the first forecast is not blocked by a
        # large ECMWF download or a third-party daily API quota.
        try:
            cycle, gfs_path = _get_gfs_bundle(points, request.hours)
            fields = _read_fields(gfs_path, points)
            rows = _build_rows("gfs", cycle, points, fields, fields, request.hours)
            if not any(
                any(value is not None for value in row["hourly"].get("wind_speed_250hPa", []))
                and any(value is not None for value in row["hourly"].get("wind_speed_300hPa", []))
                for row in rows
            ):
                raise RuntimeError("GFS GRIB에서 250/300 hPa 바람값을 찾지 못했습니다.")
            for row in rows:
                row["source"] = "NOAA GFS BDP S3 0.25°"
            return rows
        except Exception as exc:
            logger.exception("Primary NOAA GFS BDP S3 forecast failed")
            source_errors.append(f"NOAA GFS BDP S3: {type(exc).__name__}: {exc}")

        # Backup source: ECMWF AIFS / IFS Open Data, used only when NOAA GFS is unavailable.
        try:
            model = "aifs" if model_alias in ("ecmwf_aifs025_single", "aifs", "aifs-single") else "ifs"
            cycle, pressure_path, surface_path = _get_bundle(model)
            pressure_fields = _read_fields(pressure_path, points)
            surface_fields = _read_fields(surface_path, points) if surface_path else {}
            rows = _build_rows(
                model, cycle, points, pressure_fields, surface_fields, request.hours
            )
            if not any(
                any(value is not None for value in row["hourly"].get("wind_speed_250hPa", []))
                for row in rows
            ):
                raise RuntimeError("ECMWF 자료에서 항공 고도 바람 변수를 찾지 못했습니다.")
            for row in rows:
                row["source"] = "ECMWF " + model.upper()
            return rows
        except Exception as exc:
            logger.exception("Backup ECMWF forecast failed")
            source_errors.append(f"ECMWF: {type(exc).__name__}: {exc}")
            raise HTTPException(
                status_code=502,
                detail="공개 기상자료를 모두 수신하지 못했습니다. " + " | ".join(source_errors),
            ) from exc
