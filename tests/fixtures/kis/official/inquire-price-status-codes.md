# KIS 국내주식 현재가 시세 — 종목 상태 구분 코드

- 조회일: 2026-11-03.
- 출처: KIS Developers API 포털, 「주식현재가 시세」 FHKST01010100, 응답 `output.iscd_stat_cls_code`.
- 포털 URL: https://apiportal.koreainvestment.com/apiservice/apiservice-domestic-stock-quotations
- API 경로: `/uapi/domestic-stock/v1/quotations/inquire-price`.
- 수록 경위: D12021 발주자가 포털에서 확인하여 제공한 원문 전사. 이번 수정 작업자는 증권사 네트워크로 재조회하지 않았다.
- 같은 원문 사본: https://github.com/stjeong/KIS/blob/master/eFriendOpenAPI/Packet/주식현재가시세DTO.cs#L13-L21
- 사본 Git blob SHA: `1c3d3e589710fa0a20ebf8f6c6c7eb48655d982f` (발주자 제공).

## 공식 설명 원문 (발주자 제공 그대로)

> 종목 상태 구분 코드 00 : 그외 · 51 : 관리종목 · 52 : 투자위험 · 53 : 투자경고 · 54 : 투자주의 · 55 : 신용가능 · 57 : 증거금 100% · 58 : 거래정지 · 59 : 단기과열

판정: `58`만 정지(True). `00·51·52·53·54·55·57·59`는 정지 아님(False). 표 밖 값·빈 값은 모름(None). `temp_stop_yn=Y`와 외부 정지 통보는 항상 정지로 우선한다.

공식 GitHub 예제는 필드 이름만 확인할 수 있다: https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_price/chk_inquire_price.py#L22 (`종목 상태 구분 코드`; 발주자 제공 참조).
