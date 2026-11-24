# GTMS — 정적 웹사이트 및 NOAA 데이터 연결

## 웹사이트
GTMS는 HTML, CSS, JavaScript만 사용하는 정적 웹사이트입니다. Python 서버나 백엔드는 사용하지 않습니다.

- 웹주소: https://gtms-static.onrender.com
- 저장소: https://github.com/ranjintj1-dot/GTMS-Web
- 배포: Render Static Site
- 브랜치: main
- GitHub에 커밋하면 Render가 자동으로 웹사이트를 재배포합니다.

## NOAA SIGMET / PIREP 자동 지도 표출 연결

NOAA Aviation Weather API는 브라우저 CORS 요청을 허용하지 않으므로 Cloudflare Worker가 중계합니다. Worker 소스와 Wrangler 설정은 `cloudflare/` 폴더에 있습니다.

1. Cloudflare Dashboard의 **Workers & Pages**로 이동합니다.
2. **Create application → Workers → Connect to Git**을 선택합니다.
3. GitHub 계정 연결을 승인하고 저장소 `ranjintj1-dot/GTMS-Web`을 선택합니다.
4. Worker 이름을 **gtms-noaa-proxy**로 지정합니다.
5. Root directory를 **cloudflare**로 설정합니다.
6. Build command는 비워두고 Deploy command는 `npx wrangler deploy`로 둡니다.
7. **Save and Deploy**를 누릅니다.
8. Cloudflare가 발급한 `https://gtms-noaa-proxy.<account-subdomain>.workers.dev` 주소를 확인합니다.
9. GTMS 저장소의 루트 파일 `noaa-proxy-config.js`에서 다음 줄에 실제 Worker 주소를 입력합니다.
   `window.GTMS_NOAA_PROXY_URL = "https://gtms-noaa-proxy.<account-subdomain>.workers.dev";`
10. 해당 변경을 GitHub `main` 브랜치에 커밋합니다. Render가 정적 웹사이트를 자동 재배포합니다.

### Worker 테스트 주소
실제 발급 URL 뒤에 아래 경로를 붙여 확인합니다.

- `/api/health` — Worker 상태
- `/api/sigmets` — SIGMET GeoJSON
- `/api/pireps` — 최근 6시간 PIREP/AIREP GeoJSON

### 중계 및 새로고침 정책
- Worker는 허용된 GTMS 웹사이트 Origin만 브라우저 호출을 허용합니다.
- NOAA 응답은 60초 캐시하여 잦은 원본 API 호출을 방지합니다.
- NOAA는 API 요청 제한(분당 100회 및 개별 스레드의 엔드포인트별 최소 1분 간격)을 명시합니다. GTMS 새로고침은 5분 간격입니다.

## 데이터 범위 및 운항상 주의
- NOAA SIGMET: 전 세계 항공기상 특보 자료.
- NOAA PIREP/AIREP: 주로 미국 및 북대서양 자료로, 전 세계 조종사 보고를 모두 포함하지 않습니다.
- Open-Meteo ECMWF IFS 바람 자료 기반 난기류 지표는 실험적 바람벡터 차이 스크리닝입니다. 검증된 EDR 관측값이나 공식 난기류 예보가 아니며, 승인된 운항 기상정보·운항통제 절차를 대체하지 않습니다.
