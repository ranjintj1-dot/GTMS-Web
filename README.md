# GTMS v1.1 — Global Turbulence Monitoring System

항공사 운항안전팀용 브라우저 기반 난기류·위험기상 모니터링 프로토타입입니다.

## 웹사이트
- https://gtms-static.onrender.com
- HTML, CSS, JavaScript만 실행하는 Render Static Site입니다.
- Python 서버, Node.js 서버, 별도 설치 프로그램이 필요하지 않습니다.
- GitHub `main` 브랜치 커밋 시 Render가 자동 재배포합니다.

## 지도와 데이터 레이어
- Leaflet 2D 세계지도와 CesiumJS 3D 지구본
- 항공편 번호, 출발지/도착지, 고도 입력
- 수동 좌표, KML, Excel/CSV 항적 입력
- SIGMET 영역 및 PIREP/AIREP 보고 지점 (Cloudflare Worker 연결 필요)
- 항로 위험도 및 비행 항적 표시
- ECMWF AIFS AI 기상예측 + ECMWF IFS 수치예보 기반 위험도 레이어
- CAT / 수직 바람시어 위험지수 레이어
- 대류성 난기류 잠재 위험지수 레이어
- 선택 고도 바람 흐름 애니메이션
- 등압선·지위고도 등고선은 표시하지 않음
- 예보시각(+0~48시간) 및 바람장 고도(300/250/200hPa) 선택
- 지도 범위 기반 다중 격자 조회, 지도 이동·확대 시 재분석, 15분 주기 갱신 확인

## AI 위험도 계산의 의미
AIFS는 ECMWF의 인공지능 기반 기상예측 모델입니다. GTMS는 AIFS 및 IFS의 압력면 바람·온도·지위고도와 CAPE·강수·연직속도 등의 예보 변수를 사용해 해석 가능한 물리 기반 위험지수를 계산합니다.

**이 버전은 실제 EDR/FOQA 라벨로 학습·검증된 난기류 전용 지도학습 모델이 아닙니다.** AIFS 자체는 AI 기상예측 모델이지만, 난기류 위험지수는 실험적인 물리기반 스크리닝입니다. 지수는 확률이나 발생 보증값이 아닙니다. 세부 계산과 검증 계획은 `AI_MODEL_SPEC.md`에 설명되어 있습니다.

## NOAA 실시간 보고 레이어
NOAA Aviation Weather API는 브라우저 CORS를 허용하지 않으므로 `cloudflare/worker.js`의 JavaScript 중계가 필요합니다. Cloudflare Worker를 최초 배포한 뒤 실제 `workers.dev` 주소를 `noaa-proxy-config.js`에 입력하면 SIGMET/PIREP 레이어를 활성화할 수 있습니다. 자세한 절차는 `cloudflare/README.md` 및 `README_DEPLOY.md`를 참고하세요.

## 사용 데이터 및 제약
- ECMWF AIFS 0.25°: 6시간 간격 예보·모델 갱신 주기
- ECMWF IFS 0.25°: 3시간/6시간 간격 출력, 모델 6시간 주기 갱신
- 화면은 15분마다 갱신 여부를 확인합니다. 15분마다 새로운 기상모델이 생성되는 것은 아닙니다.
- PIREP/AIREP은 주로 미국·북대서양 범위입니다.
- Open-Meteo 자료는 데이터 출처 표기와 이용 조건을 준수해야 합니다.

## 안전상 주의
본 시스템은 연구·상황인식용 프로토타입입니다. 승인된 항공기상 정보, 공식 SIGMET, WAFS, EDR 측정, 운항통제 절차, 조종사 판단을 대체하지 않습니다. 실제 운항 의사결정에 사용하기 전에 과거 EDR/FOQA·PIREP 자료와 대조한 성능 검증, 위험등급 보정, 독립 검토 및 SMS 변경관리 승인이 필요합니다.
