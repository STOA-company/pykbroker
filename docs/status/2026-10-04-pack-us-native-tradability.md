# 미국 주식 native 주문 가능 판정 — 2026-10-04
범위: DBFI·NH·Toss SDK 어댑터; 공식 문서 조사와 오프라인 구현만.
기준: pykbroker 8c3bc3f39e63d9c3115e6139508ea880d4e6fece (Kiwoom 본보기).
실물: 실제 고객 카드·dev·증권사 응답·주문 경로는 안 걸어 봄.
계약: 뜻이 증명되지 않으면 halted=None; 시세 성공은 정상 근거가 아니다.
caller True는 거부를 보존하고 False는 native 미확인을 풀지 않는다.
공식 파일별 URL/경로·SHA256·행: ../contracts/us-native-tradability-sources.json.
원본·실패 시도·검증 로그: /home/quantus/runs/opus55-us-native-1004/.
## DBFI — fail-closed 유지
새 공식 경로: examples/ov_stock_quote/ov_stock_inquire_orderbook.py.
정본: DBsecurities/dbsec-open-api 564b5100b80a3a785468b77ee8c0557f6edcfdeb.
FSTKHOGA 응답 18–53행은 호가·잔량; 주문 가능 상태 enum이 없다.
새 경로 dbsec_sdk/apis/ov_stock_order/endpoints.py 417–449행도 확인했다.
CAZCQ01300의 계좌별 금액·수량을 종목 정상 거래 판정으로 쓰지 않는다.
새 공식 자료실 목록과 Excel 현재가·일봉 게시물을 확인했다.
현재가 ZIP를 받았고 XLSM 문자열에 국내 예제 005930이 있다; 실행하지 않았다.
VBA 미해독이며 그 부분에 대한 필드 부재 주장은 하지 않는다.
후보 /api/library와 잘못 추정한 public 첨부 경로는 404로 종료했다.
## NH — fail-closed 유지
새 공식 저장소: https://github.com/PLUG-OpenAPI/nhplug-sdk.
고정 커밋 13866ff45dadb25d89e7e68db69ffe79620b198a의 ZIP를 읽었다.
instruments/headers/m_gtsstock.h 28–29행과 해외 시세 샘플을 확인했다.
정본 https://www.nhplug.com/instruments/m_gtsstock.h 103–104행도 읽었다.
gLock/lock_cls는 코드 뜻이 없고 gPosTrade/trade_flag는 실측 전량 1로만 적혔다.
실측 분포를 1=정상으로 추정하지 않으며 마스터 다운로드를 추가하지 않는다.
current_price 샘플은 티커 요청·Output_0 확인뿐이며 정상 enum은 없다.
## Toss — fail-closed 유지
새 로컬 사본 /home/quantus/coding/toss-openapi/api_docs.json (공식 v1.2.17).
900–913행 AAPL의 ACTIVE는 7861–7870행에서 상장 상태로 정의된다.
7909–7920행의 koreanMarketDetail은 해외 종목에서 null이다.
7940–7952행 krxTradingSuspended/nxtTradingSuspended는 국내 거래소 전용이다.
이전 최신 schema URL을 재요청하지 않았고 비공식 GitHub는 근거로 쓰지 않았다.
## 구현·검증의 경계
세 get_rules() US 경로는 caller False를 None으로 제한하고 True를 보존한다.
미국 종목 입력 검증·기존 Rules 계약을 재사용; KR 처리는 그대로다.
native 정상 매핑·새 API·캐시·의존성·공통 게이트를 추가하지 않았다.
기존 tests/test_us_contract.py 안에 세 증권사 × 기본/False/True 9개 회귀만 추가했다.
수정 전 False 세 경우 실패·여섯 통과; 가격·호가는 공식 형태의 합성 데이터다.
검증 명령·숫자·환경 실패는 발주 폴더 REPORT.md에 기록한다.
독립 리뷰·trader gitlink·공유 dev·CI·실주문 검증은 담당 메인이 수행한다.
