# PC 빠른 시작

이 문서는 Telegram 기반 Python 프로그램을 PC에서 실행하는 안내입니다.
Android 설치형 앱과 독립 모바일 API는 [Android 앱 사용과 운영](ANDROID_GUIDE_KO.md)을 따릅니다.
모바일 API는 Telegram 토큰 없이 사용할 수 있습니다.

## 선택한 앱 방향과 추천 구성

사용자는 Android 설치형 앱을 선택했다. 화면은 `오늘`, `보유·관심`, `알림`, `설정`
네 탭으로 구성한다. Android UI 도구인
[Kotlin·Jetpack Compose](https://developer.android.com/compose)를 사용하고, 기존 Python의
분석·저장 기능은 인증된 모바일 API에서 재사용한다.

처음에는 이 PC를 시험 서버로 사용하고, 매일 계속 사용할 때 상시 서버로 옮긴다. 서버가
Telegram 정기 가이드를 처리하므로 휴대폰 앱을 닫아도 봇 작업을 계속할 수 있다. 향후 실시간 푸시 알림은
[Firebase Cloud Messaging](https://firebase.google.com/docs/cloud-messaging/server-environment)으로
연결할 수 있다. 현재 Android 앱 알림은 기기 로컬 확인 리마인더이며, 상시 서버 배포와 FCM 연결은 별도다.

실제 시세 연결에는 Toss API 키 두 개와 API를 호출하는 서버의 허용 IP 등록이 필요하다.
비밀키는 서버에 보관하고 휴대폰 앱에는 포함하지 않는다. 초기에는 본인 1명, 관심종목 3~5개,
보유정보 수동 입력으로 시작하는 안을 추천한다. 종목 개수는
운영 편의를 위한 제안이며 투자 종목이나 매매 비중 추천이 아니다. Telegram 자동 가이드는 거래일
개장 후 10분에, Android 리마인더는 공휴일 여부와 무관하게 평일 09:10에 예정된다.
아래 봇 실행에는 Telegram 인증정보가 필수이고 독립 모바일 API에는 필요하지 않다.

## 현재 준비 상태

2026-10-08 현재 이 작업공간에는 `.env`, 가상환경, Telegram/Toss 인증정보가 없습니다.
따라서 봇은 아직 실행 가능한 상태가 아닙니다. 아래의 검사와 실행 명령은 필요한 정보를
직접 설정한 뒤 사용하세요. 이 PC에서 확인된 개발용 Python은 Codex 번들 런타임이고,
의존성은 저장소의 `.deps`에 있습니다. 이는 시험용 개발 환경이며 관리형 운영 설치가 아닙니다.
빈 `.env.pc.example`로 `--doctor`를 실행해 일반 설정과 KRX 달력 로딩은 확인했다. 비어 있는
인증정보와 소유자 ID 때문에 실행 준비 상태는 실패로 표시되며, 실제 외부 연결 검사는 수행하지 않았다.

## 먼저 준비할 것

1. Telegram의 공식 [BotFather 안내](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)에
   따라 봇을 만들고 토큰을 받습니다. 토큰은 `.env`에 직접 입력하고 채팅이나 URL에 붙여넣지 마세요.
2. Telegram에서 방금 만든 봇의 대화창을 열고 본인 계정으로 `/start`를 보냅니다. `TELEGRAM_OWNER_USER_ID`에는 사용자 이름이
   아니라 숫자 ID가 필요합니다. 봇 polling이 중지된 상태에서 공식 `getUpdates` 응답의
   `message.from.id`를 확인하세요. 토큰을 로컬 `.env`에 넣은 다음 ID 확인이 필요하면 이 채팅에
   토큰을 보내지 말고 도와달라고 하세요.
3. Toss Invest PC 설정의 OpenAPI 메뉴에서 키를 만들고 허용 출발 IP를 등록합니다. 절차는
   [Toss Invest OpenAPI 문서](https://developers.tossinvest.com/docs)를 따릅니다. 현재 실행에는
   `TOSSINVEST_CLIENT_ID`와 `TOSSINVEST_CLIENT_SECRET`이 필요합니다. 초기 가이드 운영에서는
   `TOSSINVEST_ACCOUNT_SEQ`를 비워 둡니다. 값을 넣으면 실제 계좌의 보유·손익 조회 작업도 실행됩니다.
4. [.env.pc.example](../.env.pc.example)을 복사해 `.env`를 만들고 필요한 값을 입력합니다.
   기존 `.env`는 덮어쓰지 마세요. PowerShell에서 아래 명령은 `.env`가 없을 때만 복사합니다.

   ```powershell
   Set-Location E:\work
   if (-not (Test-Path .env)) { Copy-Item .env.pc.example .env }
   ```

   `TELEGRAM_ALLOWED_USER_IDS`를 비워 두면 소유자만 사용할 수 있습니다. 동료 접근을 허용할 때만
   승인된 숫자 ID를 직접 추가하세요. `TELEGRAM_OWNER_CHAT_ID`는 보통 비워 두며, 지정한다면
   소유자 ID와 같아야 합니다.

## PC에서 설정 확인과 실행

새 PowerShell 창을 열 때마다 다음 경로를 설정합니다. 이 작업공간에서 확인된 번들 Python과
로컬 의존성을 가리킵니다.

```powershell
$agentPython = 'C:\Users\coolc\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = 'E:\work\src;E:\work\.deps'
Set-Location E:\work
```

`.env`를 저장한 뒤 순서대로 검사합니다.

```powershell
& $agentPython -m stock_guide_agent.cli --env-file .env --doctor
& $agentPython -m stock_guide_agent.cli --env-file .env --check-telegram
& $agentPython -m stock_guide_agent.cli --env-file .env --check-market
```

검사가 실패하면 봇을 시작하지 말고 출력된 누락 설정과 로컬 경로를 먼저 확인하세요.
`--doctor`는 설정을 점검합니다. `--check-telegram`은 Telegram 연결을, `--check-market`은 Toss 인증과
현재가 응답을 확인합니다. 시세 점검을 할 때는 같은 인증정보를 쓰는 실행 중인 봇을 먼저 멈추세요.
이 점검은 주문을 내지 않습니다.

모든 검사가 통과한 뒤 봇을 실행합니다.

```powershell
& $agentPython -m stock_guide_agent.cli --env-file .env --run-bot
```

프로그램이 실행되는 동안 PC 전원과 인터넷 연결을 유지하세요. KRX 거래일 오전 9시 10분 전후의
정기 가이드를 받으려면 해당 시간에 PC가 켜져 있고 봇이 실행 중이어야 합니다. 중지는 PowerShell
창에서 `Ctrl+C`를 누릅니다. 실행을 닫거나 PC가 절전·종료되면 봇도 동작하지 않습니다.

## 무엇을 설정하고 무엇을 봇에 입력하나요?

- **실행 설정**은 봇 토큰, 소유자 숫자 ID, Toss OpenAPI 키, 로컬 데이터 폴더입니다. `.env`에
  저장하며 실제 값은 이 채팅이나 문서에 넣지 않습니다.
- **내 투자 정보**는 실제 본인 자금과 보유 종목, 관심 종목입니다. 실행 후 Telegram에서 `내 설정`,
  `내 보유`, `관심종목`으로 확인하고 본인이 정한 실제 정보를 직접 입력합니다. 문서와 미리보기의
  금액·종목·수량은 사용법을 보여주는 예시입니다.
- **자동 수집 근거**는 시세, 과거 분봉, 내부 모의평가 날짜입니다. 사용자가 30일 자료나 장중
  378개 세션을 수작업으로 준비할 필요는 없습니다. 이는 전략을 실증하거나 새 전략을 활성화할 때
  필요한 별도의 근거 기준이며, 현재 충족됐다는 뜻은 아닙니다.

프로그램은 가이드 전용입니다. `mock` 설정은 내부 연구 환경 이름이며 사용자 주문 기능을 켜지 않습니다.
일반 사용자용 모의주문도 제공하지 않습니다. 실제 매매는 증권사 앱에서 직접 하세요.

연속 사용이 필요해지면 PC 시험을 마친 뒤 고정 IP를 사용하는 상시 운영 호스트를 검토할 수 있습니다.
그때 관리형 Python 3.11 이상 또는 Docker 배포 구성을 별도로 준비해야 합니다. 현재 이 작업공간에서는
Docker를 사용할 수 없고, 실제 API·봇 메시지·배포·연속 운영도 확인되지 않았습니다.
