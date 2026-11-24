# GTMS v1.0 — Global Turbulence Monitoring System

항공사 운항안전팀용 난기류 모니터링 웹 1차 프로토타입.

## 실행 방법 (Windows)
1. Python 3.10 이상 설치
2. 압축을 푼 폴더에서 명령 프롬프트 실행
3. 아래 명령 실행
   ```bash
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   py server.py
   ```
4. 브라우저에서 `http://127.0.0.1:8000` 접속

## 포함 기능
- 2D 세계지도 (OpenStreetMap / Leaflet)
- 3D 지구본 전환 (CesiumJS)
- 항공편 번호, 출발/도착, 순항고도 입력
- 수동 좌표, KML, Excel/CSV 항적 입력
- NOAA Aviation Weather SIGMET / PIREP 프록시
- Open-Meteo의 ECMWF IFS pressure-level wind 자료 기반 항로 지점별 벡터 윈드시어 스크리닝
- 5분 주기 기상정보 새로고침
- 항로 위험도 구간 색상 표시

## 입력 형식
### 수동 좌표
한 줄에 `위도,경도` 형식.

### KML
KML Point / gx:Track / LineString 좌표를 읽습니다. KML의 altitude는 ft로 변환해 보존 표시합니다. MSL/타원체고의 완전한 지오이드 변환은 이 초기 버전에서 수행하지 않으므로, 항공운항 분석 시 원본 고도 기준을 확인해야 합니다.

### Excel / CSV
첫 번째 시트/파일에 `latitude` 또는 `lat`, `longitude` 또는 `lon` 열이 필요합니다. 선택 열: `altitude_ft`.

## 데이터 범위 및 한계
- NOAA Aviation Weather API: SIGMET은 전 세계, PIREP/AIREP은 주로 미국 및 북대서양 범위.
- Open-Meteo ECMWF IFS pressure-level forecast: 선택 지점의 48시간 범위에서 고도별 바람을 조회.
- 위험등급은 두 압력면 사이의 벡터풍 차이를 이용한 **실험적 스크리닝 지표**입니다. 실제 난기류 강도 예측모델, EDR, 공식 항공기상 예보와 동일하지 않습니다.
- 이 프로토타입은 운항승인, 디스패치 브리핑, 회피 지시 또는 조종사 판단을 대체하지 않습니다.
- NOAA 요청은 서버에서 중계합니다. NOAA 문서상 브라우저 CORS 직접 호출이 허용되지 않기 때문입니다.
- 인터넷 연결이 필요하며 지도 타일과 일부 라이브러리는 CDN에서 로드됩니다.
- CesiumJS 3D는 별도 Cesium Ion 토큰 없이 기본 지구본으로 구동합니다. 고해상도 3D 타일은 별도 토큰/서비스가 필요합니다.

## 운영환경 적용 전 필수 개발
1. 사내 인증·권한관리 및 감사로그
2. 서버측 캐시, 재시도, 장애감시 및 데이터 품질 모니터링
3. WAFS/SIGWX/WAFC 및 항공사 승인 기상 데이터 계약 검토
4. EDR 또는 검증된 난기류 진단 알고리즘과 실제 난기류 보고를 이용한 검증
5. 비행계획/OFP 및 실제 항적 자동연계
6. 항로를 시간-고도 4D로 매칭하고 ETA 기반 예측
7. 다중 모델/앙상블 불확실성 표시
8. 안전관리시스템(SMS) 위험평가 및 변경관리 절차

## 출처
- NOAA Aviation Weather Data API: https://aviationweather.gov/data/api/
- ECMWF Open Data: https://www.ecmwf.int/en/forecasts/datasets/open-data
- Open-Meteo ECMWF API: https://open-meteo.com/en/docs/ecmwf-api
- OpenStreetMap: https://www.openstreetmap.org/


## 인터넷 웹사이트 배포
Render 배포 방법은 `README_DEPLOY.md`를 참고하세요. 기본 배포는 로그인 없이 공개 URL로 바로 접속합니다. URL을 아는 사람은 누구나 접속할 수 있으므로 민감한 운항자료를 공개 환경에 올리지 마세요. 선택적으로 GTMS_USER와 GTMS_PASSWORD를 모두 설정하면 HTTP Basic Authentication을 켤 수 있습니다.