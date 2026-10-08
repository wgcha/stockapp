# 모바일 API 계약 v1

개인용 서버 한 인스턴스는 소유자 한 명을 다룬다. 서버 환경의 `MOBILE_API_TOKEN`은
32자 이상의 무작위 접근 키이며 `/health`를 제외한 요청에 `Authorization: Bearer <키>`를 보낸다.
클라이언트가 사용자 ID나 증권사 키를 보내지는 않는다. 소유자는 `MOBILE_OWNER_USER_ID`(기본 1)로 정한다.
JSON은 UTF-8이며 오류 응답은 `{ "error": { "code": "...", "message": "고정 한국어 메시지" } }`다.

## 상태

`GET /health`: 인증 없이 `{ "status": "ok", "api_version": 1 }`만 반환한다.

`GET /v1/state`: 아래 상태를 반환한다. 다른 저장 요청도 성공하면 같은 형태의 최신 상태를 반환한다.

```json
{
  "mode": "server",
  "guide_only": true,
  "account_equity_krw": null,
  "holdings": [],
  "watchlist": [],
  "market": { "status": "unconfigured", "message": "시세 연결 정보가 없습니다." }
}
```

`mode`는 `demo` 또는 `server`, `market.status`는 `unconfigured`, `configured`, `error` 중 하나다.
`configured`는 설정 존재를 뜻하며 실제 연결 성공이나 최신 시세를 뜻하지 않는다.
보유 원소는 `{ "stock_code": "005930", "quantity": 10, "average_price": 70000 }`이고 가격·금액은 원이다.
관심종목은 문자열 코드 배열이다. 종목명은 앱의 작은 기본 목록으로 보조하며 알 수 없으면 코드를 표시한다.

## 저장

- `PUT /v1/profile`: `{ "account_equity_krw": 10000000 }` — 유한한 양수, 최대 1e15.
- `PUT /v1/holdings/{code}`: `{ "quantity": 10, "average_price": 70000 }` — 수량은 1~1e9 정수,
  평단은 유한한 양수 최대 1e12. 불리언·숫자 문자열·NaN·무한값은 거부한다.
- `DELETE /v1/holdings/{code}`: 해당 보유 삭제. 없는 종목도 성공하는 멱등 요청이다.
- `PUT /v1/watchlist`: `{ "symbols": ["005930", "000660"] }` — 전체 목록 교체, 최대 50개,
  중복 제거. 빈 배열은 목록을 비운다.

종목 코드는 국내 종목을 위한 숫자 6자리다. 잘못된 경로·미지원 필드와 메서드를 거부한다.
저장 요청 크기는 최대 16 KiB다. 오류 시 일부 항목만 저장하지 않는다.

## 가이드

`GET /v1/guides/{code}`:

```json
{
  "stock_code": "005930",
  "action": "hold",
  "label": "유지 / 대기",
  "confidence": 0.0,
  "text": "실제 가이드 포맷의 한국어 응답",
  "current_price": null,
  "price_as_of": null,
  "generated_at": "2026-10-08T00:10:00+00:00",
  "market_status": "unconfigured",
  "is_demo": false
}
```

자료가 없으면 `current_price`와 `price_as_of`는 null이고 판단은 보류한다. 가격·손익을 예시 숫자로 채우지 않는다.
`generated_at`은 응답 생성 시각이며 가격 기준 시각이 아니다. 가격이 있으면 `price_as_of`에
검증된 가격 기준 시각을 ISO 형식으로 함께 반환한다. 기존 입력 생성기의 신선도 검사를 적용하며
기준 시각이 없거나 자료가 오래되었으면 가격을 표시하지 않고 판단을 보류한다.
현재 내부 시장 근거는 나이를 초 단위 정수로 제공하므로 `price_as_of`는 그 나이에서 재구성한
초 단위 근사 시각이다. 공급자의 원본 시각 정밀도와 같다고 간주하지 않는다.
인증정보가 있는 서버는 기존 Toss 입력 생성기와 전략 승인 상태를 사용한다. 공급자 실패는 고정 오류와
자료 부족 상태로 표시한다. 내부 오류나 공급자 응답 원문·비밀값을 반환하지 않는다.

## 실행과 클라이언트

`python -m stock_guide_agent.mobile_api --env-file .env.mobile --host 127.0.0.1 --port 8765 --data-dir data/mobile`
형태의 독립 진입점을 제공한다. `--demo`는 별도 디렉터리에서 예시 자료를 사용하며 일반 모드에
예시를 자동 주입하지 않는다. 기본 경로는 일반 모드 `data/mobile`, 데모 모드 `data/mobile-demo`이며,
`--demo --data-dir data/mobile-demo`처럼 별도 경로를 지정할 수도 있다. Telegram 인증정보와 폴링을
요구하지 않는다.

Android는 최초 데모 모드에서 로컬 자료를 사용한다. 서버 연결 후 자료는 API에서만 읽고 저장한다.
서버 오류가 나도 성공으로 표시하거나 데모 자료로 조용히 바꾸지 않는다. 서버 모드와 데모 모드는
화면·저장 공간에서 구별한다. 알림 화면은 첫 버전의 기기 로컬 리마인더 설정이다.
