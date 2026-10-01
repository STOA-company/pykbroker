# HTTP 리다이렉트 차단 팩 — 2026-10-01

## 범위
- 기준 main: 2f0d1133e5c0702e018148b98eb1be89c697bd37.
- 발주: pyk-noredirect-1001; 실주문·실계좌·dev·운영 호출 금지.
- src/pykbroker 전체에서 HTTP 라이브러리와 위임 경계를 조사했다.
- 직접 urllib 전송은 키움과 NH 두 곳이다.
- NH brokers/nh.py와 LS는 발주대로 수정하지 않았다.
- 외부 Pykis·Pydb SDK 코드는 수정하지 않았다.
- KIS·DB 외부 SDK까지 포함한 전체 차단은 아직 완료가 아니다.

## 구현
- 공용 src/pykbroker/_http.py는 HTTPRedirectHandler만 교체한다.
- redirect_request가 None을 반환해 urllib의 기존 HTTPError 처리를 쓴다.
- 301·302·303·307·308을 포함한 3xx를 따라가지 않는다.
- 전역 urllib 기본 opener를 바꾸거나 새 의존성을 추가하지 않는다.
- 키움 _KiwoomRestClient.post가 이 공용 urlopen을 쓴다.
- 키움 조회·주문·취소·토큰 발급은 같은 전송 경계를 지난다.
- 기존 어댑터가 HTTPError를 TransportError로 바꾸는 처리는 유지한다.
- 주문 허용 설정·env·재시도·새 API는 추가하지 않았다.

## 조사
- 키움 brokers/kiwoom.py:78: 공용 urllib; 리다이렉트 거부.
- NH brokers/nh.py:62: 기본 urllib; 기존 리다이렉트 동작 잔존(제외).
- KIS brokers/kis.py:156: 외부 Pykis에 위임; requests Session.send.
- DB brokers/dbfi.py:49: 외부 Pydb에 위임; requests.request/post.
- 토스 brokers/toss.py:116: 외부 HttpTransport; httpx.AsyncClient 기본 거부.
- experimental/_rest.py:48: 주입된 client.post; 기본 전송 없음.
- requests 2.34.2 로컬 302 실측: 둘째 포트 요청 1건.
- requests는 다른 포트의 Authorization을 제거하지만 appkey는 재전송했다.
- httpx 0.28.1 로컬 302 실측: 응답 302, 둘째 포트 요청 0건.

## 시험과 한계
- 수정 전 키움 조회·발급: 301·302·303의 둘째 요청 때문에 6 failed / 4 passed.
- test_kiwoom_redirect_never_reaches_second_server: 조회·발급·가짜 주문 × 5종.
- test_shared_opener_blocks_redirect_status: GET·POST × 5종; HTTPError 코드 보존.
- 200·400·401·429·500 응답과 외부 DNS/연결 차단도 시험했다.
- 서버 두 개만 연결 허용; thread 종료·join과 서버 close까지 시험 안에서 끝낸다.
- NO-REDIRECT 변이는 기본 opener로 되돌리고 시험 실패를 요구한다.
- 전체 826 passed / 19 skipped; ruff·diff check 통과; 변이 1/1 검출. 독립 검수는 메인 담당.
