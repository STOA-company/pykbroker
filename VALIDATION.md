# Extraction verification — 2026-09-09

This is an offline source delivery. No push, deployment, authenticated broker
request, or real order was performed. The source checkout remains at
`d14719529541f2c556795f59f2f68c45b6b7715e` with an empty working-tree diff.

## Executed checks

- Python 3.12.13; Pydantic 2.13.4; MCP 1.29.1.
- `python -m pip install --no-index --no-build-isolation -e '.[mcp,toss]'`: installed from locally cached dependencies.
- Import with Pykis, Pydb and toss_openapi absent: successful; none loaded into sys.modules.
- `pytest -q -ra`: **140 passed, 1 skipped**.
- Ruff lint and formatting checks: passed.
- Offline quote example: parsed last=70000, bid=69900, ask=70100 from synthetic data.
- CLI help and broker registry: read-only commands and 3 implementation entries.
- Wheel and sdist built; package modules, py.typed, Apache license, NOTICE,
  documentation and tests inspected in archive contents.
- Dependency consistency: `pip check` passed.
- Requested address, credential, forbidden-import and account-schema grep groups:
  **0 matches** in the requested source/documentation/example scopes.
- Execution contract AST: every method body is only `raise NotImplementedError`.
- HTML static inspection: 10 valid anchors, 7 tables, no external resources.
  Markdown link targets exist. This is not a browser-rendering verdict.

## Limits measured in this environment

- The sandbox forbids writes to this checkout's `.git`. `git add .` failed at
  `.git/index.lock` with `Read-only file system`; this checkout cannot be committed
  by the current session. A separate writable staging repository and bundle are
  used to deliver a commit without modifying the protected Git metadata.
- The stdio subprocess smoke test skips after a capability probe: local
  `socketpair().send()` raises `PermissionError: EPERM`, preventing asyncio's
  worker-result wakeup. In-process FastMCP tool registration and invocation tests
  pass, including default invisibility, dry-run, daily caps, revocation and dedup.
- Chromium launch fails with `Operation not permitted` in this sandbox. The HTML
  copy has not passed desktop/mobile, theme, overflow or click QA on a Mac.
- MkDocs Material is unavailable in the local cache and network DNS is blocked;
  a MkDocs build was not claimed.
- No authenticated or recorded brokerage responses were supplied. Fixtures are
  explicitly labeled synthetic snapshots, with mocks and outbound socket/DNS
  guards. Vendor SDK installation/runtime integration requires later validation.

## Remaining implementation TODOs

1. Connect reviewed, installable vendor distributions to extras. Current extras
   provide runtime prerequisites; Pykis/Pydb/toss_openapi source or client
   injection remains necessary. No unrelated same-name distribution was chosen.
2. LS/Kiwoom US mappings and Kiwoom single-cancel/order-list schemas require
   verified specifications. Their domestic mappings remain locked. NH/KB are
   intentionally offline skeletons with no transport.
3. Run the stdio subprocess test and HTML browser checks in an environment that
   permits local IPC. Render MkDocs and check navigation before publishing docs.

## 출하 판정: 공개 패키지·문서

판정: 아직 아니다 (8항목 중 6 통과)
받는 사람이 아는가: README 첫 문장·숫자·조회 예제로 목적을 드러냄. 외부 사용자 평가는 미실시.
One Thing: 한국 증권사의 조회와 주문 변환을 공통 계약으로 제공하고, 설정 없는 주문을 차단한다.
약속을 지키는가: 오프라인 계약·안전 검사는 통과. 벤더 설치 배포와 화면 QA는 남아 있음.
차갑게 써본 결과: editable 설치, 키 없는 import, 합성 시세 예제, CLI와 MCP 실패 경로를 실행함.
잘라낼 것: 실행기·자동 재시도·회사별 정책과 저장소·실거래 자동 검증.
고칠 것: 벤더 배포 연결, 미검증 범위의 공식 스펙 확보, 브라우저 QA.
안 보이는 표면: stdio 별도 프로세스와 HTML 실제 렌더는 sandbox 제약으로 미확인.
남은 위험 / 되돌리는 법: 공개 전 검증 필요. 설치 제거 또는 이전 배포판 복원; 불명확한 주문의 원장은 보존.

The verdict concerns public release, not permission to push. The requested
source extraction is delivered with its explicit TODOs and a reviewable commit
bundle; the original checkout's Git metadata remains unchanged.
