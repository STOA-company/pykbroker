# 메리츠 계약 구현 팩 (2026-10-25)
1. 기준: SEAM 2026-10-25, pykbroker base e01a680a.
2. 공식 원본: meritz-securities/open-api.
3. 원본 SHA: 2cef2e9f51f31f0ca13d9cd884bd9c80b685512d.
4. REST 73개, OpenAPI 3.1; 발주서의 62개보다 늘어난 판이다.
5. 정본 필드 증거: tests/fixtures/meritz/official/CONTRACT-2026-10-25.md.
6. 18개 사용 경로 JSON을 원본 그대로 추출했다.
7. 어댑터: src/pykbroker/brokers/meritz.py.
8. factory BROKERS 표에 meritz 한 행을 추가했다.
9. 새 증권사 이름 분기를 제품 공용 경로에 추가하지 않았다.
10. 공식 모의계좌 없음; MERITZ_MOCK_BASE_URL=None.
11. virtual=True와 문서 밖 URL은 거절한다.
12. 토큰 발급은 명시 호출이며 form-urlencoded를 쓴다.
13. 자동 갱신·경고 확인·주문 재송신은 없다.
14. 앱키 귀속 계좌는 호출자가 bound_account로 확인한다.
15. 계좌번호/비밀번호는 요청에 싣지 않는다.
16. mac_address 12자리 대문자 16진수 헤더를 쓴다.
17. endpoint별 10 TPS를 자격별 공유 limiter로 지킨다.
18. KR KRX/NXT/SOR를 데이터 표로 매핑한다.
19. KR 지정가/시장가/조건부/최유리/최우선/스톱/중간/장후종가를 받는다.
20. KR IOC/FOK와 장전/애프터마켓은 문서상 조합만 받는다.
21. 폐지된 시간외단일가 81은 보내지 않는다.
22. US NASDAQ/NYSE/AMEX 지정가·시장가 수량 주문을 받는다.
23. US 단가는 소수 4자리, 고정 소수 문자열로 전송한다.
24. 주문 결과는 접수일 뿐 체결이 아니다.
25. 정정은 amend_order(AmendRequest) 공용 꼴이다.
26. KR 원주문은 최초 번호, US 원주문은 직전 정정 번호다.
27. KR 변경은 원 거래소를 명시해야 한다(조회에 거래소 칸 없음).
28. US 변경은 원주문 일자를 호출자가 제공한다.
29. 취소 응답만으로 원주문 cancelled를 주장하지 않는다.
30. client_key는 공용 safety 원장과 응답 echo로 보존한다.
31. 오류 표에 문서 있는 코드만 rejected/token_expired/rate_limited로 넣었다.
32. 표 밖 코드·응답 유실·비JSON은 unknown이며 재송신하지 않는다.
33. 페이지 키의 공백을 보존하고 누락·반복을 오류로 처리한다.
34. KR 미체결 ncnt_qty, US는 문서에 따라 주문수량-체결수량이다.
35. KR 현금/보유평가와 US USD 주문가능현금/보유평가를 각각 반환한다.
36. KR sellable 미확인: 양수 Position 반환은 아직 미충족이다.
37. US 취소 후 원주문 종료와 클래스주 실제 표기는 미확인이다.
38. KR/US 외 시장·해외 특수주문은 SEAM 칸이 없어 전체 목표 미충족이다.
39. 모의 서버/걸음 시험은 합성 증거이며 운영·실주문·독립 리뷰는 안 했다.
40. connector_ready/live 변경 0; draft PR만, do-not-merge 유지.
