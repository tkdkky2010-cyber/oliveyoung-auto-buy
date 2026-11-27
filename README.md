# 올리브영 자동구매 (로컬 웹 프로그램)

안드로이드 폰(ADB 연결)의 올리브영 앱으로 쓰레드 글 링크 / 올리브영 상품 링크의 상품을 계정별로 구매한다.
Claude·외부 OCR API 없이 혼자 돈다. 화면 글자 인식은 PP-OCRv6(검출) + 한국어 PP-OCRv5(인식)를 ONNX Runtime 으로 로컬 실행.

> 작업 전 [READ_FIRST.md](READ_FIRST.md), 화면별 실측 경로는 [phone_auto/ORDER_STEPMAP.md](phone_auto/ORDER_STEPMAP.md).

## 설치 (처음 한 번)
필요: Python 3.11 이상(3.11 권장), Android platform-tools(adb), 폰 USB 디버깅 또는 무선 디버깅.

| OS | 설치 | 실행 |
|---|---|---|
| Windows 10/11 | `install.bat` 더블클릭 | `start.bat` 더블클릭 |
| macOS / Linux | `./install.sh` | `./start.sh` |

설치가 하는 일: `.venv` 생성 → `requirements.txt`(버전 고정) 설치 → OCR 모델 3개를 PaddlePaddle 공식 저장소에서 고정 리비전으로 받고
sha256 확인 → 모델 로딩·추론 검증 → adb 위치 확인. `start` 는 설치가 안 돼 있으면 설치부터 한다.

그 다음 계정·카드 파일을 만든다(둘 다 `.gitignore` 됨, 외부로 보내지 않음):
- `accounts.json` — `accounts.example.json` 형식
- `phone_auto/card_secrets.json` — PIN 자동 입력을 쓸 때만. 안 만들면 대시보드에서 결제 인증을 **직접 입력**으로 선택

선택:
- `python install.py --shortcut` — 바탕화면 바로가기
- `python install.py --autostart on` — 컴퓨터 시작 시 서버 자동 실행 (`off` 로 해제)

## 사용
`start` 를 실행하면 서버가 백그라운드로 뜨고 서버 준비(OCR 모델 사전 로딩·ADB 확인)가 끝나면 브라우저가
**http://127.0.0.1:8000** 을 연다. 이미 떠 있으면 새로 띄우지 않고 브라우저만 연다. 브라우저를 닫아도 서버·작업은 계속된다.

화면 맨 위는 **기기** 연결, 우측 상단 ☰ 메뉴로 `작업` / `실행 로그`(OCR·오늘 현황 포함) / `주문내역` 화면을 오간다.

1. **기기**: 연결된 기기 목록에서 `사용` (1대면 자동 선택). 무선은 `IP:포트` 로 연결.
2. **작업 등록**: 링크, 계정(`1-10` / `1,3,5`), 카드(롯데/현대), 옵션 번호, 수량, **배송지 확인(필수)**, 최대 금액(선택), 실행 모드.
   - 배송지 확인 = 수령인 이름 또는 주소 일부. 주문서 배송지 이름(`addrSummaryName`) 이나 주소 줄에 없으면 결제하지 않고 멈춘다.
   - 계정 사이 쉬는 시간: 기본 30초(0~600). 한 계정이 끝나고 다음 계정을 시작하기 전에 기다린다. 대기 중에도 일시정지·중지는 바로 걸린다.
   - 계정별/전체 최대 결제금액은 선택(0 = 제한 없음). 가격 변동·포인트 미적용·수량 오선택 등으로 예상보다 비싸면 결제 직전에 멈추는 안전장치.
   - `점검(dry-run)` — 결제하기 직전까지 실제 폰에서 진행하고 멈춘다 (결제 없음). **처음엔 반드시 이걸로.**
   - `모의 실행` — 폰 없이 상태 흐름·한도·DB 만 확인 (결제 없음)
   - `실제 결제`
3. 작업 목록에서 **실행**. 일시정지·재개·중지·실패 계정 다시 대기열로.
4. **사람 인증 대기**: 로그아웃·로그인을 짧게 반복하면 로그인 화면에 Cloudflare `사람인지 확인하십시오` 체크 칸이 뜬다(봇 방지).
   프로그램은 이 칸을 누르지 않는다. 처음 즉시 확인하고 3초마다 다시 보며 **최대 60초** 기다린다(화면에 남은 시간 표시).
   `성공!` 이 보이면 바로 로그인 버튼을 누르고, 60초가 지나면 그 계정은 로그인 실패(`CLOUDFLARE_TIMEOUT`)로 멈춘다. 대기 중 일시정지·중지는 바로 걸린다.
   Cloudflare 사람 확인이 뜨거나 결제 인증을 직접 입력으로 고른 경우 화면 위 노란 배너가 뜬다 → 폰에서 직접 처리하면 자동으로 이어간다.
5. **결제 결과 확인 필요**(빨간 배너): 결제 인증 후 완료 화면을 못 본 건. 앱 주문내역을 보고 `결제됨(주문번호)` / `결제 안 됨` 을 입력하기 전까지
   그 계정은 다시 결제하지 않고, 그 작업은 멈춘다.

페이지에 접속하거나 새로고침하는 것만으로는 아무것도 실행되지 않는다.

## 안전 장치
- 삼성카드 금지(현대·롯데만), 일시불·기본배송지 확인, 배송지 확인값(수령인 이름·주소 일부)이 주문서 배송지에 없으면 멈춤
- 계정별 최대 결제금액 / 작업 전체 최대 결제금액 (선택). 하루 구매 건수 한도는 없다
- 결제하기 누르기 **전에** 결제 기록(PENDING)을 DB 에 남긴다 → 프로그램이 죽어도 다시 켜면 `결과 불명`으로 잡혀 자동 재결제 안 함
- 이미 그 링크 상품을 산 계정은 건너뜀(옵션으로 해제), 같은 작업 두 번 실행 불가, 한 기기에 작업 하나(프로세스 간 잠금)
- 일시정지·중지는 결제 전 안전 지점에서만 걸림. ADB 가 끊기면 결제 전 단계에서만 재연결 후 그 계정을 처음부터 1회 재시도
- 로그인 화면의 자동 로그인·아이디 저장 체크박스는 절대 누르지 않음. Cloudflare 체크 칸도 누르지 않고 사람 인증을 기다림
- 카드앱 결제 인증 화면은 일반 OCR 로 조작하지 않음(키패드는 UIAutomator content-desc 로만)
- 서버는 127.0.0.1 에서만, 다른 Host·다른 사이트에서 온 상태 변경 요청은 거절. 비밀번호는 API·로그에 나가지 않고 로그에서 가려짐
- 실패 화면 스크린샷은 `logs/fail_shots` 에 최근 50장만, 카드앱 화면은 저장 안 함 (`SAVE_FAIL_SHOTS=0` 으로 끔)

## 명령줄 (기존 방식, 그대로 동작)
```bash
.venv/bin/python phone_auto/threads_buy.py prepare <계정번호> <lotte|hyundai> <링크> [옵션번호]
.venv/bin/python phone_auto/phone_pay.py relogin-to-pin <계정번호> lotte
```
Windows 는 `.venv\Scripts\python.exe`. 기기가 여러 대면 `ANDROID_SERIAL=<serial>`.

## 구조
```
install.py / start.py / *.bat / *.sh    설치·실행
requirements.txt                       버전 고정 (Python 3.11, Win/mac Intel·ARM/Linux 휠 확인)
phone_auto/
  adb_manager.py     ADB 공통 (경로 탐색, 기기 선택, 재연결, 해상도 환산, 기기 잠금)
  ocr_engine.py      PP-OCRv6 + korean PP-OCRv5, ONNX Runtime, 한 번 로딩·캐시
  screen_reader.py   스크린샷 재사용, dump·OCR 교차 확인, 화면 변화 감지, 보안 화면 차단
  phone_login.py     로그인·로그아웃 (기존 실측 경로)
  phone_pay.py       주문서·카드·결제 (기존 실측 경로 + 검증 훅)
  threads_buy.py     쓰레드/상품 링크 → 바로구매 (기존 + 수량)
  ocr_keypad.py      카드앱 셔플 키패드 PIN (dump 전용)
  state_machine.py   화면 상태 기계
  order_manager.py   주문 검증·한도·결제 기록
  task_queue.py      계정별 작업 큐 (실행·일시정지·재개·중지, 모의 실행)
  storage.py         SQLite (database/app.db)
web/                 FastAPI 대시보드 (한국어, SSE 실시간)
models/              OCR 모델 (설치 때 받음)
tests/               pytest + OCR 벤치마크(bench_ocr.py)
docs/OCR_BENCHMARK.md
```

## 테스트
```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

## 환경변수
`OY_PORT`(8000) · `ANDROID_SERIAL` · `ADB_PATH` · `OCR_DET_MODEL`(PP-OCRv6_small_det) · `OCR_PROVIDER=gpu`(CUDA/DirectML/CoreML 이 설치돼 있을 때만) ·
`OCR_THREADS` · `PIN_MODE`(auto/manual, 명령줄용) · `SAVE_FAIL_SHOTS`
