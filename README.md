# GTMS v1.0 — Global Turbulence Monitoring System

항공사 운항안전팀용 브라우저 기반 난기류 모니터링 웹 프로토타입입니다.

## 접속 및 배포
- HTML, CSS, JavaScript만으로 실행되는 정적 웹사이트입니다.
- Python, Node.js 서버, 별도 실행 프로그램이 필요하지 않습니다.
- Render Static Site로 배포하며, 브라우저에서 URL을 열면 바로 사용할 수 있습니다.
- 상세 배포 절차는 `README_DEPLOY.md`를 참고하세요.

## 포함 기능
- 2D 세계지도 (OpenStreetMap / Leaflet)
- 3D 지구본 전환 (CesiumJS)
- 항공편 번호, 출발/도착, 순항고도 입력
- 수동 좌표, KML, Excel/CSV 항적 입력
- Open-Meteo ECMWF IFS 압력면 바람 자료 직접 조회
- 항로 지점별 상·하층 바람벡터 차이 기반 실험적 스크리닝
- PC 및 태블릿 반응형 화면

## 데이터 연결 제한
NOAA Aviation Weather API는 CORS 정책상 일반 브라우저 웹페이지에서 직접 호출할 수 없습니다. 따라서 정적 웹 버전에서는 SIGMET/PIREP 자동 지도 표시를 비활성화하고, 공식 API 결과를 새 탭에서 확인할 수 있도록 링크를 제공합니다. NOAA 자료를 웹페이지 안에서 자동 표시하려면 Python이 아닌 JavaScript 기반 별도 프록시(예: Cloudflare Worker)가 추가로 필요합니다.

## 안전상 주의
바람벡터 차이 기반 위험지표는 검증된 EDR 실측값 또는 공식 난기류 예보가 아닌 실험적 스크리닝 지표입니다. 공식 항공기상 정보, 운항승인, 디스패치 판단, 조종사 판단을 대체하지 않습니다.
