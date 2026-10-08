# pykbroker 핀 통합 계획 — 2026-10-08 (bx-w0-unify)
범위: 오프라인 SDK 통합 후보; trader 수정·배포·증권사 호출 0건. 고객 주문 경로는 안 걸어 봄.
기준: main `5ce2bc4`; 통합 시작 `a7bf2a84`(main 후손, 23커밋 앞). PR #17~#27 모두 draft/do-not-merge.
근거: `git rev-parse origin/main`; `git rev-list --count origin/main..a7bf2a84`; `gh api repos/STOA-company/pykbroker/pulls?state=open&per_page=100`.
## 현재 핀 — main과 소비자 두 판으로 갈린 세 경로
- SDK main: `5ce2bc4ff14ea66ddf49a8c521cb54db01d85141`.
- trader master: `bee3bac63a838b9bb774004509bac4ff18f0fcfa` (`git -C /home/quantus/coding/quantus-trader ls-tree origin/master pykbroker`).
- dev T 후보 `44397541`: `a7bf2a84562073807118ab06b2bd83c122a1bb2c` (`git -C /home/quantus/coding/quantus-trader ls-tree 44397541 pykbroker`).
- 운영 큐 이미지 대응 커밋 `daa4c35`: 같은 `a7bf2a84` (`git -C /home/quantus/coding/quantus-trader ls-tree daa4c35 pykbroker`).
실제 dev T·운영 큐 서빙 SHA는 미확인. 이미지 대응 근거는 조사 지도 `/home/quantus/runs/broker-expansion-1008/research/stack-map.md` §2.
## SDK 머지 순서와 닫을 PR
1. 이 draft PR 하나로 #23~#27과 흡수된 KIS·DBFI 변경을 main에 넣는다. 추가 미흡수 KIS·DBFI 변경 없음.
2. 다른 계열 독립 리뷰 + 대표 명시 승인 뒤에만 이 PR main 머지; 작업자가 리뷰 호출·머지하지 않는다.
3. 키움 → NH → LS 후속 PR을 이 브랜치 위로 옮겨 남은 델타만 순서대로 검수·main 머지한다. 각 돈 경로 승인 유지.
4. KB 후속 차이는 비교 보고 뒤 대표 결정; 이번 통합은 기존 a7bf2a84의 KB를 그대로 보존한다.

| 기존 PR | 이 PR 머지 뒤 처리 / 이유 |
|---|---|
| #17 KIS | 흡수됨 → 닫음; patch-equivalent f92e7a1, 후속 수정 보존 |
| #18 DBFI | 흡수됨 → 닫음; 2de417b에 동작 흡수, 공통 US 시험 정리만 patch 차이 |
| #19 키움 | 흡수됨 → 닫음; af072d9=089995b, 후속 델타는 키움 작업자 PR에 남음 |
| #20 NH | 흡수됨 → 닫음; 0730bd5=7ea931d, 후속 델타는 NH 작업자 PR에 남음 |
| #21 LS | 흡수됨 → 닫음; 두 patch 흡수, 후속 델타는 LS 작업자 PR에 남음 |
| #22 KB | 흡수됨 → 닫음; 두 patch 흡수, 새 변경 여부는 KB 비교 보고 뒤 결정 |
| #23 | 대체됨 → 닫음; 3cce252b 조상 전체 포함 |
| #24 | 대체됨 → 닫음; 8c3bc3f3 조상 전체 포함 |
| #25 | 대체됨 → 닫음; dca550a9 조상 전체 포함 |
| #26 | 대체됨 → 닫음; 463b2e51 조상 전체 포함 |
| #27 | 대체됨 → 닫음; a7bf2a84 전체 포함 |
근거: 각 #17~#22 `git cherry a7bf2a84 <head>`·`git range-diff origin/main..<head> origin/main..a7bf2a84`·파일별 diff; #23~#27 `git merge-base --is-ancestor <head> a7bf2a84`.
## trader 핀·반영·되돌리기
- SDK 후속 승인·머지 뒤 최종 main SHA 하나를 정한다. master → dev T `codex/t-us-standing-1007` 계열 → 운영 큐 `daa4c35` 배포 계열 순으로 각각 gitlink 한 줄 PR.
- 핀 PR에 능력표 재생성·앱 변경을 섞지 않는다. 이번에는 trader를 수정하지 않는다.
- dev T 공유 dev 검증: 이전에 걸은 토스·한투 주문/취소/실패 경로를 담당 메인이 재확인; 실주문은 대표 승인된 창에서만.
- 운영 큐 반영은 dev 증거·대표 명시 prod 승인·동결창 밖에서만. 이전 이미지 식별자를 반영 전 기록하고 핵심·실패 경로 재검증.
- 롤백: 각 대상 gitlink를 직전 SHA로 되돌리는 PR + 직전 검증 이미지 재반영. SDK main 롤백이 필요하면 별도 revert PR·독립 리뷰·대표 승인.
## 검증과 경계
기준선/끝: 지정 venv·PYTHONPATH=src 전체 pytest 각 1회; 모두 1318 passed / 19 skipped / 5 failed, 새 실패 0 (원본 로그는 런 폴더).
기존 실패: factory 등록 집합, NH US venue, DBFI D+2/주문가능금액 기대, LS 에러 문구, KIS US rules fixture; 고치지 않음. 상세 node ID는 통합 PR 본문.
겹침표·함수별 차이·원본 증거: 통합 PR 본문 및 `/home/quantus/runs/broker-expansion-1008/w0-unify/`. 독립 리뷰·main 머지·dev/prod·실주문 완료 주장은 없음.
