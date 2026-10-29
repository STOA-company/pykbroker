# DBFI 공식 demo — 2026-10-29

## 계약
- `DBFI(virtual=True)`는 생성 시 CSPAQ01800 읽기 증명을 통과해야 반환한다.
- 허용 응답은 HTTP 200 + `rsp_cd == "M100"` + `rsp_msg == "모의투자에서는 해당업무가 제공되지 않습니다."`의 정확 일치뿐이다.
- 성공 응답·다른 코드/문구·빈/손상 응답·통신 오류·리다이렉트는 거부하며 주문은 전송하지 않는다.
- 증명은 재시도/연속조회 없이 POST 1회, `In.QryTp=2`, 리다이렉트 금지다.
- 키·시크릿·실제 인증 헤더·계좌를 묶은 digest만 프로세스 메모리에 보관한다. 값/지문을 출력하지 않는다.
- 매 연결 때 입력 자격과 실제 세션을 대조한다. 키·토큰·계좌가 바뀌면 재증명한다.
- 해외 세션도 국내 증명과 같은 인증 객체여야 한다.
- 모의 client/API/service 사본의 헤더 getter가 실제 전송 직전마다 증명한다. Pydb 재발급·재시도도 새 헤더 증명 전에는 전송하지 않는다.
- 성공/실패 digest를 모두 캐시해 같은 지문은 probe 1회다. 거부된 지문은 같은 프로세스에서 재조회하지 않고, 새 토큰/키/계좌/헤더는 별도 증명한다.
- 이미 캐시된 거래/시세/차트 service의 auth가 API auth와 다르면 주문 전에 거부한다. 원본 client와 live 경로는 바꾸지 않는다.
- REST 주소는 동일하다. 현재 어댑터에는 WS 구현이 없어 새 WS 경로를 만들지 않았다.
- 모의 NXT 주문/취소는 `NotSupportedError`. 공개 SDK에 CSPAQ01800/CAZCQ03400 조회 메서드는 없다.
- 기존 KR/US 허용 조회·주문·취소 경로를 사용한다. 실전 `virtual=False`는 증명 호출 없이 유지한다.
- 주문·취소·내역·상태가 반환하는 `Order.virtual`은 계좌 모드를 보존한다(누락한 같은 종류 3곳 수정).

## 공식 근거 (564b5100b80a3a785468b77ee8c0557f6edcfdeb)
- [README 688–693](https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/README.md#L688): REST 공용, WS 모의 17070.
- [지원표 KR 33–47](https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/docs/api_support_matrix.md#L33): CSPAQ01800/NXT 미지원.
- [지원표 US 163–171](https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/docs/api_support_matrix.md#L163): CAZCQ00400 지원, CAZCQ03400 미지원.
- [오류표 162–173,192–193](https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/docs/errors_and_rate_limits.md#L162): M100 재사용 때문에 문구도 대조.
- [읽기 전문 564–611](https://github.com/DBsecurities/dbsec-open-api/blob/564b5100b80a3a785468b77ee8c0557f6edcfdeb/dbsec_sdk/apis/kr_stock_order/endpoints.py#L564).

## 검증
- 실제 loopback HTTP 요청을 계수하는 신규 회귀 31개 포함 관련 시험: 323 passed, 4 skipped, exit 0.
- 기존 동일 5개 파일 base a673c82: 293 passed/4 skipped; head: 292 passed/4 skipped, 양쪽 exit 0.
- 1개 감소는 DB를 «모의 도메인 없음»으로 거부하던 기존 매개변수 항목 제거다. 새 회귀는 head에서 별도 검증.
- 원문/명령/종료: `/home/quantus/coding/cx-db-mock-1029/worker-evidence/repair-{pyk-head,pyk-existing-base,pyk-existing-head}.log`, `repair-commands.txt`.
- 기존 격리 runner 사용: 외부 네트워크 없음, `/run` 가림, docker/삭제 명령 차단. 새 의존성 설치 없음.
- dev·브로커·실키·DB·고객 경로는 안 걸어 봄(이번 발주에서 금지). push/PR/머지/배포하지 않음.
- trader의 원래 핀 `7b6431ff5e41bc874f6abb836924de6b8e336e63` → 승인 기반 `a673c82cc9b064f01bbb065f526bc014b9b1d37e`에는 기존 43커밋이 있다(src 12파일 +1272/−253).
- 이 기존 변화는 공통 거절 매핑·KIS/Toss/KB/NH/LS/Kiwoom·DB 잔고의 주문가능액 조회 추가를 포함한다. 이번 모의 수리 델타와 구분한다.
- 전체 pytest base/head(각 900초, 수집 오류 뒤 계속): 1856/1886 passed, 공통 3 failed+2 errors, head-only 0. 최종 원문 `delta-pyk-full-base-continued.log`/`delta-pyk-full-final.log`, 목록 `delta-pyk-final-failed-name-diff.json`.
- 전역 RLock의 프로브 중 직렬화와 공개 virtual setter는 이번 세션 재현과 별개인 남은 제한으로 두었다.
