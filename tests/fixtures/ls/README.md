# LS 오프라인 합성 계약 (2026-10-01)

모든 JSON은 **합성(fake)**이며 증권사 녹화·공식 샘플이 아니다. 계좌·토큰을 넣지 않았다.

기존 요청 경로/TR/블록은 pykbroker `experimental/ls.py`(c9dbecd)와
trader `quantus_backend/traders/ls_order.py`(8e3b7d43)에서 재사용했다.
이 기존 코드도 `LS_SPEC_VERIFIED=False`이고 열거값은 미확인이다.
시험의 `fixture-*` 코드는 의도적인 가짜값이며 LS 코드로 사용할 수 없다.

**UNVERIFIED(명세 미확인)** 목록:
- 모든 요청 열거값, `rsp_cd` 성공/실패의 의미, 주문/취소 응답 블록.
- `t0424OutBlock.cts_expcode`, `t0425OutBlock.cts_ordno` 페이지 끝 판정.
- 새 체결 매핑 `t0425OutBlock1.qty/price/cheqty/cheprice` 및 평균 체결가 의미.
  기존 코드는 `ordno/expcode/ordrem`만 사용했다. 새 필드는 후보 계약이다.
- 조회 범위가 당일 체결 전부를 담는지, 실시간성, 모의 도메인/왕복.

`fill_price`는 `cheprice` 후보값만 담는다. 주문 지정가 `price`나 보유 평균가에서
만들지 않는다. 체결가 누락/0은 None이다. 명세 잠금이 닫혀 있으면 주문 상태는
체결 수량이 있어도 unknown이며 capability는 전부 꺼짐이다.

공식 출처(상세 확인 실패, 이번 조회는 1회만):
- https://openapi.ls-sec.co.kr/apiservice
- https://openapi.ls-sec.co.kr/apiservice?api_id=d0e216e0-10d9-479f-8a4d-e175b8bae307
- https://openapi.ls-sec.co.kr/guide/api/wqs8bq62-huk0-wb6z-7x83-nt78d4ax2yb6?type=rest

명세 대조와 키 받은 뒤 H-LS 왕복까지 실사용 잠금은 해제하지 않는다.
