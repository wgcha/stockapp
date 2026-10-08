# Android 앱 사용과 운영

## 먼저 써 보기

Android 8.0 이상에서 사용한다. 개발 빌드 APK를 휴대폰에 옮겨 열고, 해당 파일 앱에 설치 권한을
허용한다. 첫 실행의 **데모 모드**는 인터넷과 API 키 없이 동작한다. 시험용 투자금·보유·관심종목을
입력하면 기기에 저장된다. 데모에는 실시간 시세나 실제 수익률이 없다.

GitHub에서는 [자동 빌드](https://github.com/wgcha/stockapp/actions/workflows/verify.yml)의 성공한 실행을
열고 **Artifacts → stock-guide-debug-apk**를 내려받아 압축을 푼다. GitHub 로그인이 필요할 수 있으며
파일은 실행 후 14일간 보관된다. 로컬 APK와 GitHub APK는 개발 서명이 달라 서로 덮어 설치되지
않을 수 있다. 시험 데이터를 유지하며 갱신하려면 같은 서명 환경에서 만든 APK를 사용한다.
정식 배포 전에는 고정된 비공개 배포 서명키를 구성해야 한다.

- **오늘**: 투자 기준금액과 자료 상태를 읽고 종목 가이드를 확인한다.
- **보유·관심**: 종목 코드, 수량, 평균 매수가를 직접 입력한다. 거래 후에도 직접 갱신한다.
- **알림**: 평일 오전 9시 10분(한국 시간)에 앱 확인 알림을 받는다. Android 알림 권한이 필요하다.
  이 알림은 기기 로컬 리마인더이며 공휴일을 제외하는 거래일 알림이나 실시간 매매 신호가 아니다.
  절전 상태 등으로 늦어질 수 있다.
- **설정**: 투자 기준금액을 저장하고 데모와 서버를 선택한다. 서버 주소·개인용 접근 키도 여기서 입력한다.

모바일 API는 저장된 전략 배분이 없으면 신규 진입 비중을 0으로 제한한다. 앱에서 투자금만
설정하는 것은 전략 승인이나 배분 설정을 대신하지 않는다. 전략 검증·승인 절차는
[전략 검증 안내](STRATEGY_VALIDATION.md)를 따른다.

가이드를 참고한 실제 매매는 증권사 앱에서 직접 한다. 이 앱은 주문을 전송하지 않는다.

## 설정자료란 무엇인가

앱을 데모로 쓰는 데 준비할 자료는 없다. 실제 운영에는 아래 세 종류를 구분하면 된다.

| 항목 | 준비할 내용 | 저장 위치 |
|---|---|---|
| 연결 설정 | Python 서버 주소, 개인용 API 접근 키 | 휴대폰; 키는 Android Keystore로 암호화 |
| 시세 설정 | Toss Client ID·Secret, 서버 출발 IP 허용 등록 | Python 서버의 비공개 환경 파일 |
| 내 투자정보 | 투자 기준금액, 보유 수량·평단, 관심종목 코드 | 데모는 기기, 연결 모드는 서버 SQLite |

처음에는 본인 한 명과 관심종목 3~5개로 시작하는 구성을 권한다. 투자금은 가이드 계산의
기준금액이며 증권사 계좌 잔고가 아니다. 과거 시세·검증 증거는 분석 과정에서 수집·관리할
자료이며 사용자가 앱을 켜기 위해 30일치 거래 기록을 준비할 필요는 없다.

## PC에서 모바일 서버 실행

Python 3.11 이상에서 저장소 루트의 터미널을 사용한다. 모바일 API에는 Telegram 토큰이 필요 없다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
if (-not (Test-Path .env.mobile)) { Copy-Item .env.mobile.example .env.mobile }
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

출력된 무작위 키를 `.env.mobile`의 `MOBILE_API_TOKEN`에 넣는다. 채팅·GitHub·스크린샷에
올리지 않는다. 나머지 시세 키는 없으면 비워 두어도 저장·조회와 자료 부족 가이드를 시험할 수 있다.

```powershell
.\.venv\Scripts\python.exe -m stock_guide_agent.mobile_api --env-file .env.mobile --host 127.0.0.1 --port 8765 --data-dir data/mobile
```

서버를 종료하려면 해당 터미널에서 Ctrl+C를 누른다. 서버 PC가 꺼지거나 절전되면 연결 기능도
중단된다. 데모 자료를 서버에 자동 복사하지 않으므로 서버 연결 후 본인의 자료를 입력한다.
데이터 폴더에는 실행 모드 표식이 남으며 같은 폴더를 데모·실제 모드로 번갈아 사용할 수 없다.
기존 Telegram DB를 모바일 폴더에 임의로 복사하는 것도 지원하지 않는다.

### USB로 휴대폰 연결 시험

Android 개발자 옵션의 USB 디버깅을 켜고 PC 연결을 허용한 뒤 SDK `platform-tools`의 `adb`로
아래 명령을 실행한다. 앱에는 `http://127.0.0.1:8765`와 위의 접근 키를 넣는다.

```powershell
adb reverse tcp:8765 tcp:8765
```

Android 에뮬레이터에서는 `http://10.0.2.2:8765`도 사용할 수 있다. 로컬 HTTP 시험은 디버그 APK에
한정된다. 외부 상시 운영은 HTTPS 주소와 TLS 프록시, 비공개 환경 파일, 서비스 재시작 및 백업
구성이 필요하다. 인터넷에 개발용 HTTP 포트를 직접 공개하지 않는다.

### 실제 시세 사용

[Toss Invest OpenAPI 안내](https://developers.tossinvest.com/docs)에 따라 Client ID·Secret을
발급하고 API 요청을 보내는 서버의 출발 IP를 등록한다. 해당 값을 `.env.mobile`에 설정하고 서버를
재시작한다. 앱의 설정 존재 표시는 실제 시세 조회 성공과 다르다. 종목 가이드에서 조회 결과를
확인한다. 자료가 없거나 전략이 승인되지 않았으면 보수적인 대기 판단을 유지한다.

## 개발과 빌드

Android Studio에서 저장소의 `android` 폴더를 연다. SDK 36, JDK 17과 Gradle 8.13을 사용한다.
Android Studio의 Gradle JDK도 17로 선택한다. 이 작업환경에 설치된 JBR 25는 Gradle 8.13과
호환되지 않아 빌드에 별도 JDK 17을 사용했다.

```powershell
Set-Location android
.\gradlew.bat testDebugUnitTest lintDebug assembleDebug
```

APK는 `android/app/build/outputs/apk/debug/app-debug.apk`에 생성된다. GitHub Actions도 Python 테스트와
Android 검사·빌드를 실행하고 `stock-guide-debug-apk` 아티팩트를 보관한다. 디버그 APK는 개인 시험용이다.
Play Store 배포용 서명키와 배포 설정은 별도로 준비한다.

검증 기록은 [개발 계획](ANDROID_PLAN.md), 요청 형식은 [API 계약](MOBILE_API.md)을 참고한다.
