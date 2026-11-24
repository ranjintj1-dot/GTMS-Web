# GTMS AI-assisted Turbulence Risk Model — Technical Note

## 1. Model architecture

This release combines forecast fields from two ECMWF models:

- **AIFS 0.25°**: ECMWF's AI-based Integrated Forecasting System.
- **IFS 0.25°**: physics-based operational numerical weather prediction.

When the hybrid option is selected, the application blends the resulting risk indices with weights of 0.60 (AIFS) and 0.40 (IFS). When one model request fails, it uses the available model and reports a fallback status.

The weather model is AI-based; the turbulence risk calculation in this prototype is a transparent, physics-informed heuristic. It is not yet a supervised machine-learning model trained on airline EDR/FOQA labels.

## 2. Current map layers

### CAT / vertical-shear potential
For each requested grid coordinate and forecast time:

1. Convert wind speed/direction at 300, 250 and 200 hPa into horizontal U/V components.
2. Calculate vector wind difference between 300–250 hPa and 250–200 hPa.
3. Divide vector difference by geopotential-height separation to obtain vertical shear in m/s per km.
4. Calculate potential temperature at 300 and 250 hPa and estimate a bulk gradient Richardson number:
   - N² = (g / mean potential temperature) × (potential-temperature difference / height difference)
   - Ri = N² / (vertical wind-shear squared)
5. Convert shear and Ri into a 0–100 experimental CAT risk index:
   - shear component = clamp(shear / 10, 0, 1)
   - Richardson component = 1.00 when Ri < 0.25; 0.82 for Ri < 0.5; 0.60 for Ri < 1; 0.32 for Ri < 2; otherwise 0.08
   - CAT index = 100 × (0.68 × shear component + 0.32 × Richardson component)

If geopotential height is unavailable, approximate pressure-level heights are used only as a fallback. A row with missing shear inputs does not receive a CAT index.

### Convective turbulence potential
For each forecast time, the prototype combines surface CAPE, precipitation, 500 hPa vertical velocity and total cloud cover:

- convective index = 100 × (
  0.55 × clamp(CAPE / 1800, 0, 1) +
  0.22 × clamp(precipitation / 4 mm h⁻¹, 0, 1) +
  0.15 × clamp(abs(vertical velocity) / 0.5 m s⁻¹, 0, 1) +
  0.08 × clamp(cloud cover / 100, 0, 1)
)

This is a convective-environment screening proxy, not a convective turbulence observation or a calibrated probability.

### Forecast aggregation
The user-selected forecast horizon is searched at each sampled coordinate. The maximum CAT index and maximum convective index within that horizon are displayed separately, including their valid forecast times.

Hybrid mode blends model indices:
- blended index = 0.60 × AIFS index + 0.40 × IFS index

If one model is unavailable, the other model's index is used and the status panel identifies that the result came from a fallback model.

## 3. Display and refresh behaviour

- The analysis grid is generated from the visible Leaflet map bounds.
- Each coordinate is queried through Open-Meteo's multi-location ECMWF API.
- Separate Leaflet layers are used for CAT and convective risk; Cesium ellipses are also created in 3D Globe mode.
- Map move/zoom schedules a new query. The automatic polling interval is 15 minutes.
- This is not a live turbulence sensor feed. AIFS forecasts have six-hourly time steps and are updated every six hours. IFS 0.25° has three-hourly and six-hourly forecast steps and is updated every six hours. A 15-minute screen refresh may therefore display the same model run repeatedly.

## 4. Experimental risk categories

The displayed index bands are prototype UI bands, not aviation regulatory thresholds:

- 0–29: Low
- 30–49: Caution
- 50–69: High
- 70–100: Very high

These values must not be described as calibrated turbulence probabilities, EDR categories, or operational severity thresholds.

## 5. Required validation before operational use

A proper airline turbulence-specific ML model requires historical, time-aligned training and test labels, preferably:
- aircraft EDR / eddy-dissipation-rate measurements with aircraft type, altitude and time;
- FOQA/QAR vertical acceleration, vertical speed and aircraft response variables;
- PIREP/AIREP severity, time, location and flight level;
- SIGMET and convective weather analysis;
- model forecasts archived at the exact issue time to avoid future-data leakage.

Recommended development stages:
1. Build a clean event-level dataset with unique event IDs, UTC timestamps, coordinates, flight level and aircraft type.
2. Align observations with AIFS/IFS forecast values at the exact model issue time and lead time.
3. Split training/validation/test data by time and, where possible, by route/aircraft to avoid leakage.
4. Train and calibrate a supervised classifier/regressor against EDR or a clearly defined turbulence event label.
5. Validate probability calibration, precision/recall by severity, false alarm ratio, lead-time performance and geographical generalisation.
6. Run in shadow mode; compare with dispatch and crew reports before any operational use.

## 6. Safety statement

This prototype is for research and situational awareness only. It is not an approved operational turbulence forecast, does not replace approved weather products, and must not independently drive flight planning, dispatch, altitude selection or crew decisions.
