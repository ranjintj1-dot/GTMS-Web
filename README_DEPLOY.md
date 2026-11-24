# GTMS — 정적 웹사이트 배포 안내

## 배포 방식
이 저장소는 **Render Static Site**로 배포합니다. Python 서버, 백엔드 프로세스, 포트 설정은 사용하지 않습니다.

`render.yaml`에 정적 사이트 설정이 들어 있으므로 Render에서 Blueprint를 생성하고 저장소를 연결하면 됩니다.

1. Render 대시보드에서 **New → Blueprint**를 선택합니다.
2. GitHub 저장소 `ranjintj1-dot/GTMS-Web`을 선택합니다.
3. 설정 파일 `render.yaml`에 표시되는 서비스가 **Static Site**인지 확인합니다.
4. Preview/Plan 내용을 확인한 뒤 배포를 승인합니다.
5. 상태가 **Live**가 되면 발급된 `.onrender.com` 주소로 접속합니다.

직접 생성할 때는 **New → Static Site**를 선택하고 다음처럼 지정합니다.
- Repository: `https://github.com/ranjintj1-dot/GTMS-Web`
- Branch: `main`
- Build Command: 비워 둠
- Publish Directory: `.`

## 접속 방식
- 로그인 없이 URL을 열면 바로 접속합니다.
- 공개 URL을 아는 사람은 누구나 접속할 수 있습니다. 민감한 항공편/운항 자료는 입력하지 마세요.

## 기능 및 외부 API
- 지도/화면/항로 입력 기능은 브라우저에서 실행됩니다.
- Open-Meteo ECMWF IFS 예보는 브라우저에서 직접 조회합니다.
- NOAA Aviation Weather API는 CORS 정책상 브라우저 직접 호출이 차단되어 있습니다. SIGMET/PIREP은 공식 API 링크로 이동해 확인하도록 표시됩니다. 웹 화면 안에서 자동 지도 표시까지 필요하면 별도의 JavaScript 프록시 서비스가 필요합니다.

## 안전상 주의
위험등급은 압력면 바람벡터 차이에 기반한 실험적 스크리닝 지표이며 검증된 EDR 예측/관측값이 아닙니다. 공식 항공기상 정보와 회사 운항통제 절차를 대체하지 않습니다.
