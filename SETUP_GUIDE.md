# `finance_v1` 설치 및 지인별 개별 배포 가이드 (SETUP_GUIDE)

## 1. 기본 구조
- **코드 공유**: 원본 저장소를 **Fork**하여 사용합니다. 원본이 업데이트되면 GitHub의 **`Sync fork`** 버튼으로 충돌 없이 최신 알고리즘을 반영할 수 있습니다.
- **데이터 완전 분리**: 상태 JSON을 Git에 커밋하지 않으며, 모든 매매 원장(`Settings`, `Algorithms`, `Lots`, `Fills`, `Signals`, `Audit`)은 **본인의 개인 Google Spreadsheet**에만 저장됩니다.
- **시장 및 실행 환경**:
  - **국내 주식 (KR)**: Streamlit Cloud에서 월간추세, 컵앤핸들, 후지모토 1:2:6 추천 조회 및 확인 후 주문
  - **미국 무한매수 (US)**: 본인이 Fork한 저장소의 GitHub Actions에서 매일 자동 실행

---

## 2. 개인 Google Sheet 준비
1. 본인 Google Drive에서 빈 Google Spreadsheet를 1개 생성합니다.
2. URL(`https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`)에서 `<SHEET_ID>` 부분을 복사합니다.
3. 우측 상단 **공유(Share)**를 눌러 Google Cloud 서비스 계정 이메일(`client_email`)에 **편집자(Editor)** 권한을 부여합니다.
4. 앱이 최초 실행될 때 필요한 6개 탭과 헤더 스키마를 자동으로 생성합니다.

---

## 3. Streamlit Cloud 배포 (Fork 저장소 실행)
1. [share.streamlit.io](https://share.streamlit.io)에 본인 GitHub 계정으로 로그인 → **`Create app`** 클릭
2. 본인이 Fork한 저장소(`finance_v1`)와 `app.py`를 선택합니다.
3. **`Advanced settings` → `Secrets`** 에 [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example) 형식으로 본인 키 1세트를 입력하고 **`Deploy`** 를 누릅니다.

---

## 4. GitHub Actions 활성화 (미국 무한매수 자동매매)
> **주의**: GitHub 정책상 Fork한 저장소는 처음에 Actions가 비활성화되어 있습니다.
1. 본인 Fork 저장소의 **`Actions`** 탭으로 이동 → 녹색 버튼 **`I understand my workflows, go ahead and enable them`** 을 1회 클릭합니다.
2. **`Settings` → `Secrets and variables` → `Actions`** 에 아래 Repository Secrets를 등록합니다:
   - `USER_ID`, `SHEET_ID`, `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`, `IS_MOCK` (`true` 또는 `false`)
   - `GCP_SERVICE_ACCOUNT_JSON` (서비스 계정 JSON 전체 문자열)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (선택)
   - `US_INFINITE_SYMBOLS` (예: `TQQQ,SOXL`)

---

## 5. 기존 보유 종목(히스토리 없음) 초기 등록 방법
1. Streamlit 앱의 **① 대시보드**에서 **`🔄 KIS 보유잔고 불러오기 / 대사`** 를 클릭합니다.
2. 원장에 없던 기존 보유 종목이 `manual`(미할당) Lot으로 자동 생성됩니다.
3. 카드 우측의 **`알고리즘 연결`** 버튼을 눌러 최초 매수일, 담당 알고리즘(월간추세/컵앤핸들/후지모토 1:2:6/미국 무한매수), 그리고 단계/진행 회차(T값)를 저장하면 다음 거래일부터 선택한 알고리즘이 청산 및 추가매수를 이어받습니다.
