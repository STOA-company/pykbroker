# pykbroker 핀 통합 계획 — 2026-10-08 (bx-w0-unify-fix1)
범위: PR #28 P1-1·P1-3 오프라인 수정; src·trader 변경, 배포, 증권사 호출 0. 고객 경로 안 걸어 봄.
비교 기준: main `5ce2bc4`; 시작 HEAD `c830ac37`(통합 SDK `a7bf2a84` 그대로). draft/do-not-merge 유지.
현재 SDK main `5ce2bc4`; trader master gitlink `bee3bac63a838b9bb774004509bac4ff18f0fcfa`(운영 줄기·Prime).
dev T 후보 `codex/t-us-standing-1007`의 `44397541`, 운영 큐 이미지 대응 `daa4c35`는 gitlink `a7bf2a84`; 실제 서빙 SHA·큐 대상 브랜치 미확인.
핀 근거: 이전 조사 `git ls-tree origin/master pykbroker`·`git ls-tree 44397541 pykbroker`·`git ls-tree daa4c35 pykbroker`; stack-map.md §2. 서버 접속 없음.
## main 대비 실주문 계약 변경 — 경로 수는 증권사×시장×주문/취소 단위
공통 G: BaseBroker 기본 OrderSafety(_common.py:96), dry_run=True(safety/gate.py:27), RiskLimits.allow_orders=False·dry_run=True(safety/risk_caps.py:17); 경로는 모두 이 쓰기 게이트 기본 꺼짐.
신규 쓰기 길 14개: 한투 KR/US 취소 2 + 키움 US 2 + NH US 2 + LS KR/US 4 + KB KR/US 4. 한투 US native 긍정 판정은 별도 1개; 조회·판정 자체에 opt-in은 없고 쓰기만 G로 잠김.
| 증권사 | 국내 주문 | 미국 주문 | 취소 | 주문 가능 판정·예산 | 실계좌 허용 옵션·기본값(근거) | 여는 방법 |
|---|---|---|---|---|---|---|
| 한투 KIS | 기존 KR 주문 유지; 통합증거금 CASH100 예산 분리 | 기존 지정가 유지; USD 현금/통합 주문가능금액 분리 | main 미구현 → 신규 KR/US 원주문 ID·종목·잔량·지점 대조 후 1회 취소; US 모의 취소 거절(kis.py:517) | US 기본 미확정/조회 0 → native e_ordyn="매매 가능"만 halted=False(kis.py:215,279); KR is_integrated=True면 cash100(:335,374) | G; is_integrated=False(kis.py:139); 새 실계좌 스위치 없음 | US market="US"+venue; 증권사 긍정 응답으로 판정. 통합예산은 계좌의 기존 약정 확인 후 명시 True; 주문/취소는 G 해제 |
| DBFI | 기존 주문 유지; 잔고 Dps2 유지, available_cash는 MgnRat100pctOrdAbleAmt로 분리(dbfi.py:133,179) | 기존 지정가 유지; USD 현금은 min(예수금,출금가능), 주문가능금액과 분리(:167) | 기존 KR/US 취소 유지; Pydb 재시도 구조는 기존과 같음(:315) | US caller False→None; native 미확정 유지(:79); US 기본 AstkOrdAbleAmt, 명시 통합은 Amt0+CsldtMgnUseYn="Y"(:160) | G; is_integrated=False(dbfi.py:34); 새 실계좌 스위치 없음 | US market+venue; 기존 약정 확인 후 is_integrated=True로 통합예산 선택; 주문/취소는 G 해제 |
| 토스 | 기존 유지 | 기존 유지 | 기존 KR/US 취소 유지 | US caller False→None; True만 거부 보존(toss.py:322); 긍정 native 근거 없이 주문 가능 판정하지 않음 | G; 새 실계좌 스위치 없음 | 기존 계좌·safety 설정으로 G 해제; 이 PR로 native 미확정은 풀리지 않음 |
| 키움 | 기존 KRX 지정가 유지 | 신규 US 지정가 buy/sell ust20000/ust20001(kiwoom.py:604) | KR 기존; 신규 US ust20003은 확인된 전량 잔량만 취소(:628) | US native 거래 상태로 halted 판정(:310,533); 기본 통합 꺼짐, ust31490 USD 주문가능수량; True는 krw_ord_rqst_yn="Y" 필요(:580) | G; is_integrated=False(kiwoom.py:171); allow_live_orders 옵션 없음 | 저장 token/app_key + market="US" + NASDAQ/NYSE/AMEX venue + G 해제; 통합은 기존 약정의 명시 True |
| NH | 기존 KRX/NXT 지정가 유지 | 신규 US 지정가 buy/sell(nh.py:971); venue는 거래소명 대신 "US"(:151) | KR 기존; 신규 US 원주문 us: ID+수량 취소(:994) | US caller False→None(:439); USD buyableAmount 기본 비통합(wtm_cur_knd_cd="4"), True면 "1"(:863) | G + allow_live_orders=False(nh.py:141,632); is_integrated=False(:142) | 저장 키/토큰 + 명시 실계좌 acct_type + US market/venue="US" + allow_live_orders=True + G 해제 |
| LS | main의 live locked → 신규 실계좌 CSPAT00601(ls.py:189,495) | 신규 지정가 COSAT00301(:627); 명시 broker order_date 필수(:220) | 신규 KR CSPAT00801 / US COSAT00301 취소(:519,627) | 신규 US suspend/sellonly 매핑(:334,363); 정상 enum 공식 근거는 기존 리뷰 P2 미해소; is_integrated=True는 계속 거절(:133) | G + allow_live_orders=False(ls.py:126); 저장 실토큰·host·계좌 일치 검사(:161,233) | 저장 token + virtual=False + allow_live_orders=True + G 해제; US market+NYSE/NASDAQ venue+order_date; 주문 전 계좌 확인 |
| KB | 신규 등록(factory.py:18), KR 지정가 ssam1802/ssam1801(kb.py:185) | 신규 US 지정가 skam2101; 기존 Global One Market 약정 필수(:168,198) | 신규 KR ssam1806 / US skam2102; US 현재 전량 잔량·원가격 확인(:365) | KR 100% 주문가능금액(:141); US native 미확정 유지(:133); 통합 금액의 USD 근거는 미확정 | G + writes_enabled=False(kb.py:49,170); is_integrated=False(:48); 환경/client_options로 금융 플래그 지정 거절(:61) | owner-bound account/app_key/token + 명시 writes_enabled=True + G 해제; US market+venue+기존 약정의 is_integrated=True; ledger 조회는 order_date |
표의 *.py는 src/pykbroker/brokers/ 아래, factory.py는 src/pykbroker/ 아래; 줄은 src 불변인 c830ac37 기준. G는 명시 live safety·한도·원장·중복방지·kill switch, US 주문 KRW valuation 포함(gate.py:54).
현금·체결 계약 추가: KIS status는 원장으로 대사(:479), US 복수 체결가는 None; DBFI 수량 검증·US 합계체결금액/체결수량, 키움·LS·KB 복수/미확정 평균가를 추정하지 않음.
출처·미해소 기록: 기존 DBFI/LS/KB/US-native 팩과 REVIEW_PR28.md; DBFI 단일전송은 소비자 DbfiSingleAttempt 조건(리뷰 P2-1). 이번 공식 파일 다운로드 0.
## SDK 머지·기존 PR 처리
이 PR main 머지는 같은 Claude 계열 P1 델타 재검수 + 대표 명시 승인 뒤. P1-2(#23·#24 전체 다른 계열 검수/갈음 결정)는 이번 수정 밖으로 남김.
일반 merge 또는 fast-forward로 a7bf2a84를 main 조상에 보존; squash/rebase 머지하지 않음.
#17 KIS=f92e7a1, #18 DBFI=2de417b로 흡수; 추가 미흡수 변경 없음. #19 키움·#20 NH·#21 LS·#22 KB도 흡수(파일별 근거 PR 본문).
이 PR 승인·머지 뒤 #17~#22는 흡수되어 닫음; #23 3cce252b·#24 8c3bc3f3·#25 dca550a9·#26 463b2e51·#27 a7bf2a84는 조상 전체 포함으로 대체되어 닫음. 지금 닫지 않음.
이어 키움 → NH → LS 후속 PR의 남은 델타만 순차 검수·승인·main 머지; KB 새 차이는 별도 작업, 이번에 가져오지 않음.
대표 10-08 결정: 키움·NH·LS·KB 고객 발급 키 암호화 보관·고객 확인 뒤 주문. SDK에 암호화 저장/고객 확인을 추가한 변경은 아니며 소비자 통합은 별도.
## trader 핀 순서 — dev 확인 뒤 운영 승인
1. SDK 승인·머지로 최종 main SHA를 정한 뒤 dev T `codex/t-us-standing-1007` 계열 gitlink 한 줄 PR부터 반영.
2. 공유 dev에서 담당 메인이 토스·한투 주문·취소·실패를 재확인. 실호출/실주문은 대표 승인 범위·창에서만; 이 작업자는 호출하지 않음.
3. dev 검증 증거·변경 델타·롤백을 제시하고 대표의 trader 운영 반영 명시 승인을 받음.
4. trader master(운영 줄기·Prime) gitlink PR → 운영 반영: 대표 승인 + 동결창 밖, 이전 이미지 기록 후 핵심·실패 재확인.
5. 운영 큐 서버 계열 gitlink PR → 반영: 동일 dev 증거 + 대표 승인 + 동결창 밖; 대상 브랜치는 담당 메인이 확인(daa4c35를 브랜치로 취급하지 않음).
핀 PR에 능력표 재생성·앱 변경을 섞지 않음. 이번 trader 핀 변경·PR 생성·머지·배포 0.
롤백: 각 소비자 gitlink 직전 SHA로 되돌리는 PR + 직전 검증 이미지; SDK revert도 별도 독립 리뷰·대표 승인.
## 검증
명령: PYTHONPATH=src /home/quantus/coding/pykbroker/.venv/bin/python -m pytest -q tests -p no:cacheprovider (기준선/끝 각 1회).
기준선 c830ac37: 5 failed / 1318 passed / 19 skipped(exit 1); 끝: 0 failed / 1362 passed / 19 skipped(exit 0). main에서 통과하던 4개+묶음 안 1개 기대·fixture 수정; 삭제/추가 skip 0.
원본 로그·다섯 node ID·수정 이유·src 불변 확인: PR #28 본문 및 /home/quantus/runs/broker-expansion-1008/w0-unify-fix1/. main 머지·고객 경로·dev/prod·실주문 완료 주장은 없음.
