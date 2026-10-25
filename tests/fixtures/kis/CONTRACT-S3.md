# S-추가3 공식 해외 계약 (2026-10-25)

정본은 official/SOURCE.md의 KIS 고정 SHA와 동봉 원문이다. 주문 코드·거래소·매매구분은 official/order.txt, 시세 EXCD는 price_detail.txt, 현지통화 잔고 국가는 inquire_present_balance.txt, 주문 상태는 inquire_ccnl.txt에 근거한다. 실계좌·공식 모의 호출은 하지 않았다.

- KIS US: limit=00, loo=32, loc=34; 매도만 moo=31, moc=33. 모의는 limit만. 일반 market/VWAP/TWAP은 해당 주문 API에 없다.
- HK=SEHK, CN_SH=SHAA, CN_SZ=SZAA, JP_TSE=TKSE, VN_HOSE=VNSE, VN_HNX=HASE: limit. HK 매도 단주50은 수량 로트 계약이 추가로 필요하여 이번 공용 종류에 매핑하지 않는다.
- JP_OSE/JP_JASDAQ는 공식 주문 거래소 목록에 없다. JP_TSE로 바꾸어 보내지 않는다.
- 비미국 신규 경로와 미국 특수주문은 기존 인증 transport의 단일 시도 경계를 사용하며 ORD_QTY·OVRS_ORD_UNPR는 문자열이다. 기존 KR/US limit 호출 경로는 유지한다.
- 공식 설명이 ORD_DVSN을 제거하도록 한 홍콩 매수·중국·일본·베트남은 해당 필드를 생략한다. 공식 예제 함수는 무조건 필드를 추가하지만, 설명문의 TR별 규칙을 우선한다.
- 기존 Pykis 체결조회 helper는 NASD를 고정한다. 비미국은 동봉 inquire_ccnl의 OVRS_EXCG_CD를 전달하고 페이지 키를 보존한다. 과거 30일 조회이며 없는 행을 종결로 바꾸지 않는다.
- 현지통화 잔고·현금은 국가/시장 코드 및 통화와 대사한다. 미국 통합증거금 칸을 다른 시장에 재사용하지 않는다.
- close는 기존 국내 장후 시간외다. S-추가3 loc(거래소 장마감 경매 지정가)나 moc와 같지 않다. 호환용 기존 값 그대로 유지하며 의미가 다른 별칭을 만들지 않는다.
