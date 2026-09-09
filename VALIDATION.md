# SEO/GEO 2차 검증 — 2026-09-09

이번 작업은 pykbroker 저장소 내부의 문서·예제·패키지 메타데이터 준비입니다.
배포, push, PyPI 업로드, MCP 레지스트리 등록, public 전환, GitHub 메타데이터 변경,
인증된 증권사 호출과 실주문은 실행하지 않았습니다. 1차 기록은 아래에 역사적 기록으로 보존합니다.

## 변경 결과

- README: 한국어·영어 정의, 실측 숫자 3개, 30초 조회 예제, 기존 다이어그램·역할 표,
  검색 질문 형태의 FAQ 8개, 관련 프로젝트 순서로 재구성했습니다.
- Python 최소 버전은 pyproject의 `>=3.11`, 등록 어댑터는 `BROKERS`의 3곳입니다.
  이번 Python 3.12 환경에서 141 passed / 1 skipped를 실측했습니다. Python 3.11 실행 검증은 미실시입니다.
- 한국어 16페이지와 대응 영어 16페이지, 총 32페이지에 description·lang 메타를 넣었습니다.
  pycex의 mkdocs.yml과 docs 파일 목록을 읽기 전용으로 확인했고 다국어 구조가 없어 `docs/en/`을 사용했습니다.
- `site_url`은 계획 주소 `https://stoa-company.github.io/pykbroker/`입니다.
  공개 가용성은 미확인이고 실제 게시를 의미하지 않습니다.
- `docs/_hooks/seo.py`는 모든 Markdown의 본문·표·예제·안전 설명을 보존하고 상대 링크를
  절대 URL로 바꿔 llms 자산을 생성합니다. YAML 머리말과 JSON-LD script는 본문에서 제외합니다.
  재생성 명령은 `python docs/_hooks/seo.py`; MkDocs pre-build에서도 실행합니다.
- MkDocs core sitemap에 맞춰 site_url을 설정했고, docs/sitemap.xml은 오프라인 정적 대체본입니다.
  빌드에서는 대체본을 제외하고 MkDocs core가 sitemap을 생성합니다. 존재하지 않는 sitemap 플러그인을 추가하지 않았습니다.
- `docs/robots.txt`는 프로젝트 경로에 복사될 준비 자산입니다. 호스트 루트 `/robots.txt`의 효력을
  대신하지 않습니다. 이 저장소 밖의 호스트 정책 변경은 하지 않았습니다.
- 한국어·영어 index의 SoftwareSourceCode JSON-LD는 name/codeRepository/programmingLanguage/
  license/author를 포함합니다. raw HTML 보존 설정과 페이지별 HTML lang·hreflang 훅을 준비했습니다.
- 새 KIS 잔고 조회 예제는 조회만 수행하며 기존 예제는 보존했습니다.
  Claude 설정은 실제 `pykbroker-mcp` 진입점을 사용하고 인증 환경 주입을 별도 준비 조건으로 명시합니다.
- pyproject에 요청 keywords/URLs/classifiers를 병합했고 README를 패키지 설명으로 유지했습니다.
  author는 요청된 조직명 Quantus Technologies로 일치시켰습니다. 기존 저작권·NOTICE는 보존했습니다.
- CITATION.cff의 date-released는 2026-09-09 소스 스냅샷 날짜입니다.
  message와 주석에 PyPI 릴리스를 뜻하지 않음을 명시했고 0.1.0은 Unreleased 상태를 유지했습니다.

## 직접 실행한 검증

| 검사 | 결과와 한계 |
| --- | --- |
| `pytest -q -ra --basetemp=artifacts/pytest-seo-final` | **141 passed, 1 skipped**, 1.27초. 전체 스위트 실행; 실패 없음 |
| skip 사유 | `tests/test_mcp_stdio.py:19`: sandbox가 socketpair의 local IPC send를 EPERM으로 차단. stdio 별도 프로세스 검증은 미실시 |
| `python -c "import tomllib, pathlib; tomllib.load(pathlib.Path('pyproject.toml').open('rb'))"` | TOML 문법 통과; `.venv/bin/python` 사용 |
| `mkdocs build --strict` | PATH에 가상환경을 포함해 시도했으나 exit 127, `mkdocs: command not found` |
| docs 의존성 설치 시도 | mkdocs-material 설치 실패: pypi.org DNS NameResolutionError. 전역 설치·파일 수정 없음 |
| MkDocs 대체 검사 | 시스템 PyYAML 5.4.1 safe_load 통과; nav 32개가 실제 Markdown 32개와 정확히 일치, 모든 description/lang 및 번역 상대 파일 확인 |
| CFF | YAML 파싱, 필수 필드·조직 author·스냅샷 날짜 확인. 외부 CFF schema validator는 미실행 |
| 로컬 링크 | README·docs·Claude 예제의 상대 링크 110개가 실제 파일을 가리킴 |
| crawler 자산 | sitemap XML 파싱, 32개 중복 없는 URL과 문서 대응 확인. llms 본문 32개 모두 포함, 모든 Markdown 링크가 절대 HTTPS URL, 재생성 결과 동일 |
| JSON-LD·언어 훅 | index 2곳의 JSON 파싱·요구 필드 확인. 32페이지 출력에서 HTML lang과 한·영 hreflang 2개 확인 |
| 정적 렌더 | MarkdownIt로 32페이지 HTML 생성 후 제목·본문·JSON-LD 확인. **실제 MkDocs/Python-Markdown/Material 빌드 또는 브라우저 QA를 대체하지 않음** |
| 새 잔고 예제 | 기존 KIS 합성 fixture로 현금 200000 KRW 출력과 계좌 결박 확인. 설정 오류 경로는 exit 2, 원시 예외·민감 문자열 미노출 확인 |
| 키 없는 예제 | 기존 offline_quote 출력 last 70000, bid 69900, ask 70100 확인. 새 잔고 스크립트는 인증 환경 제거 시 안내와 exit 2 확인 |
| Ruff | 새 Python 예제와 SEO 훅 lint·format 검사 통과 |
| 패키징 | `python -m build --no-isolation --outdir artifacts/seo-dist`로 wheel·sdist 생성. wheel METADATA의 라이선스·classifiers·Homepage·README 본문을 열어 확인 |
| diff | `git diff --check` 통과. src/, tests/, 기존 예제와 .gitignore 변경 없음 |

**mkdocs build 미실행: MkDocs가 설치되어 있지 않고, 의존성 다운로드가 DNS 차단으로 실패하여 실제 빌드를 시작하지 못했습니다.**

`command -v agent-browser` 결과 이 환경에는 PATH의 안전 래퍼가 없습니다. 로컬 Mac GUI도 제공되지 않아
실제 브라우저를 열지 못했습니다. Chromium 실행 실패라는 1차 결과를 이번에 재실행한 것으로 간주하지 않습니다.
문서의 실제 데스크톱·모바일, 밝은/어두운 테마, 테이블 overflow, 내부 링크 클릭, 404·빈 화면 QA는 TODO입니다.

## HTTPS 링크 확인

README와 docs의 Markdown/HTML/TXT/XML에서 `rg --only-matching`으로 HTTPS URL을 전부 추출하고
중복 제거한 **44개** 각각에 `curl --location --head --connect-timeout 3 --max-time 5`를 실행했습니다.
**44개 모두 exit 6 (Could not resolve host)**로 실제 HTTP 응답을 얻지 못했습니다.
URL의 HTTPS 스킴·호스트·형식은 통과했으며, **실제 curl 검증은 driver가 한다**.
404라고 단정하거나 HTTP 200/검색 수집 성공으로 기록하지 않습니다. 상세 실행 결과는
`artifacts/seo-link-checks.json`, 추출 목록은 `artifacts/seo-https-urls.txt`에 있습니다.

별도 웹 읽기 도구에서는 아래 공식 문서의 내용을 확인했습니다. 이는 저장소·계획 문서 주소의 가용성 증거가 아닙니다.

- [MkDocs 설정](https://www.mkdocs.org/user-guide/configuration/): site_url, canonical, nav와 hooks 구성 참고.
- [MkDocs core sitemap 템플릿](https://github.com/mkdocs/mkdocs/blob/master/mkdocs/templates/sitemap.xml): 기본 sitemap 생성 확인.
- [MkDocs 문서 메타데이터](https://www.mkdocs.org/user-guide/writing-your-docs/#meta-data), [Material 참조](https://squidfunk.github.io/mkdocs-material/reference/): front-matter와 description 참고.
- [MCP 로컬 서버 연결](https://modelcontextprotocol.io/docs/develop/connect-local-servers): Claude 설정 위치·절대경로·재시작 절차 참고.
- [SoftwareSourceCode](https://schema.org/SoftwareSourceCode): 저장소와 프로그래밍 언어 속성 참고.

웹 도구로도 GitHub pykbroker·pycex와 제품 링크를 추가 확인하려 했으나 Internal Error로 본문을 얻지 못했습니다.
기존 KRX 링크는 보존했으며, 이번 작업은 거래 규칙 자체를 갱신하거나 재검증한 작업이 아닙니다.

## 범위·커밋 전달

모든 명시적 파일 쓰기는 `/home/quantus/coding/pykbroker` 내부로 한정했습니다. 임시 디렉터리,
의존성 캐시, pytest basetemp, 패키지 산출물과 별도 Git 전달 저장소도 내부 `artifacts/`를 사용합니다.
타 저장소는 pycex 문서 관례 확인을 위한 읽기만 수행했습니다. 타 저장소 파일·페이지·메타데이터 변경은 없습니다.
README/문서의 quantus 제품 링크는 **README의 https://www.quantus.kr/q 1개**이며,
요구된 pyproject Homepage는 별도로 유지했습니다.

원본 `.git`이 읽기 전용이어서 `git add`가 `.git/index.lock: Read-only file system`으로 실패했습니다.
원본 브랜치 커밋은 미완료입니다. 검증된 변경을 원본 HEAD를 부모로 하는 별도 커밋으로 저장소 안의
`artifacts/seo-delivery.git`에 만들고 `artifacts/pykbroker-seo-geo.bundle`로 전달합니다.
원본 `.git`을 우회 수정하지 않으며 push하지 않습니다. 적용은 쓰기 가능한 driver 체크아웃에서 해야 합니다.
커밋 메시지 마지막에는 요청한 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`을 넣습니다.

## TODO — 환경 제약으로 남은 검증

1. driver 환경에서 docs extra 설치 후 `mkdocs build --strict`, 실제 Material 출력의 description·JSON-LD·canonical·hreflang·sitemap·llms를 확인합니다.
2. Mac의 안전 브라우저 세션에서 한·영 32페이지, 모바일/데스크톱, 테마, 표 overflow, 내부/외부 링크, 404·오프라인 경로를 확인합니다.
3. 위 44개 URL의 실제 curl 검증과 계획 문서 주소의 공개 가용성을 확인합니다. 게시·등록·public 전환은 이 작업에서 승인되지 않았습니다.
4. 로컬 IPC를 허용하는 환경에서 stdio 테스트를 실행하고, 별도로 준비된 인증 환경에서 Claude Desktop 읽기 전용 연결을 확인합니다. 실 API/모의계좌 검증 완료를 주장하지 않습니다.
5. 쓰기 가능한 driver 체크아웃에 전달 bundle을 적용하여 원본 브랜치 커밋을 완료합니다. push는 하지 않습니다.

## 출하 판정: SEO/GEO 문서의 외부 공개

판정: **아직 아니다 (8항목 중 6 통과)**
받는 사람이 아는가: 첫 문장·숫자·예제로 한국 증권사 조회 라이브러리의 목적을 드러냄. 외부 사용자 반응은 미수집.
One Thing: 처음 온 개발자와 생성엔진이 실제 지원 범위와 주문 차단 기본값을 이해하고 조회 시작 경로를 찾는다.
약속을 지키는가: 문서·메타데이터·예제의 오프라인 검증은 통과. 외부 공개 가용성과 실제 브라우저 검증은 미확인.
차갑게 써본 결과: 키 없는 시세, 합성 잔고, 인증 누락과 민감 오류 차단 경로를 실행함. UI를 직접 사용하는 항목은 미통과.
잘라낼 것: 미검증 지원 주장, 모의 체결 주장, 새 SDK 기능, 실제 배포·등록.
고칠 것: 위 TODO의 strict 빌드·실제 UI·외부 링크 검증을 완료해 공개 판정을 다시 낼 것.
안 보이는 표면: Material 실제 렌더/404/모바일/테마와 외부 링크 클릭은 이번 환경에서 확인하지 못함.
남은 위험 / 되돌리는 법: 계획 URL의 공개 가용성과 렌더 차이. 원본 적용 전에는 전달 커밋을 적용하지 않으면 되며,
적용 후에는 SEO/GEO 전달 커밋만 revert하고 문서 자산을 재생성한다. SDK/주문 로직과 주문 원장은 변경하지 않았다.

이 판정은 공개에 대한 판정이며, 사용자가 요청한 **문서 준비와 로컬 커밋 전달**을 중단하거나
배포 승인을 요청하는 근거로 사용하지 않습니다.

## 검사한 HTTPS URL 전체 목록

아래 각 URL의 curl 결과는 모두 DNS 실패(exit 6)입니다.

```text
https://github.com/Jaeminyx-Stoa/pycex
https://github.com/STOA-company/pykbroker
https://github.com/STOA-company/pykbroker/blob/main/LICENSE
https://github.com/STOA-company/pykbroker/blob/main/examples/kis_balance_check.py
https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T1.jsp
https://global.krx.co.kr/contents/GLB/06/0602/0602010201/GLB0602010201T3.jsp
https://modelcontextprotocol.io/docs/develop/connect-local-servers
https://schema.org
https://stoa-company.github.io/pykbroker/
https://stoa-company.github.io/pykbroker/BROKER_ADAPTER_RECIPE/
https://stoa-company.github.io/pykbroker/BROKER_MATRIX/
https://stoa-company.github.io/pykbroker/api/brokers/
https://stoa-company.github.io/pykbroker/api/models/
https://stoa-company.github.io/pykbroker/api/rules/
https://stoa-company.github.io/pykbroker/changelog/
https://stoa-company.github.io/pykbroker/claude-desktop/
https://stoa-company.github.io/pykbroker/cli/
https://stoa-company.github.io/pykbroker/contributing/
https://stoa-company.github.io/pykbroker/en/
https://stoa-company.github.io/pykbroker/en/BROKER_ADAPTER_RECIPE/
https://stoa-company.github.io/pykbroker/en/BROKER_MATRIX/
https://stoa-company.github.io/pykbroker/en/api/brokers/
https://stoa-company.github.io/pykbroker/en/api/models/
https://stoa-company.github.io/pykbroker/en/api/rules/
https://stoa-company.github.io/pykbroker/en/changelog/
https://stoa-company.github.io/pykbroker/en/claude-desktop/
https://stoa-company.github.io/pykbroker/en/cli/
https://stoa-company.github.io/pykbroker/en/contributing/
https://stoa-company.github.io/pykbroker/en/execution-sdk-contract/
https://stoa-company.github.io/pykbroker/en/getting-started/
https://stoa-company.github.io/pykbroker/en/mcp/
https://stoa-company.github.io/pykbroker/en/notice/
https://stoa-company.github.io/pykbroker/en/safety/
https://stoa-company.github.io/pykbroker/en/security/
https://stoa-company.github.io/pykbroker/execution-sdk-contract/
https://stoa-company.github.io/pykbroker/getting-started/
https://stoa-company.github.io/pykbroker/llms-full.txt
https://stoa-company.github.io/pykbroker/llms.txt
https://stoa-company.github.io/pykbroker/mcp/
https://stoa-company.github.io/pykbroker/notice/
https://stoa-company.github.io/pykbroker/safety/
https://stoa-company.github.io/pykbroker/security/
https://stoa-company.github.io/pykbroker/sitemap.xml
https://www.quantus.kr/q
```

---

# 1차 검증 기록 — 과거 상태, 이번 결과와 구분

아래 커밋·테스트 수·환경 판정은 1차 시점의 기록입니다. 현재 상태는 위 2차 기록을 따릅니다.

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
