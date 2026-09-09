---
description: "pykbroker 기여 절차, 안전 변경 리뷰, 오프라인 테스트와 어댑터 문서·라이선스 갱신 기준."
lang: ko
---

# Contributing

버그 리포트와 PR을 받습니다. 병합 권한은 지정된 메인테이너 1인에게 있습니다. 안전 관련 변경은 작성자 외 추가 리뷰를 거친 뒤 메인테이너가 병합합니다.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,mcp]'
pytest
ruff check src tests examples
ruff format --check src tests examples
```

모든 테스트는 오프라인이어야 합니다. 벤더 응답 fixture에는 실계좌·인증값을 넣지 마세요. 주문·취소 변경은 정확한 요청 인자, 드라이런, 한도, 중복·타임아웃을 검증합니다. 새 어댑터는 문서·테스트·capability·지원 표를 함께 갱신합니다. 미검증 스펙 잠금 해제는 문서와 독립 검증 증거가 필요합니다.

Python 소스는 `src/`, Pydantic 모델은 `models/`, JSON fixture는 `tests/fixtures/<broker>/`에 둡니다. pycex와 같이 hatchling, ruff, pytest-asyncio, MkDocs Material을 사용합니다. 요청 실행기·전략 실행·회사별 저장소는 추가하지 않습니다.

기여물은 Apache-2.0으로 제공할 권리가 있어야 합니다. 기존 코드를 참고했다면 출처와 실제 재사용 범위를 NOTICE에 명시하세요.

## 문서와 검색 자산 갱신

한국어 Markdown을 바꾸면 대응하는 `docs/en/` 번역과 양쪽 `description`을 함께 갱신하세요. pycex에는 다국어 설정이 없어 이 저장소는 `docs/en/`과 한·영 nav를 사용합니다. 새 페이지는 양쪽 nav에 등록합니다.

```bash
pip install -e '.[docs]'
python docs/_hooks/seo.py
mkdocs build --strict
```

생성기는 실제 Markdown 전체에서 `llms.txt`, `llms-full.txt`, `robots.txt`, 오프라인 확인용 `sitemap.xml`을 갱신합니다. 빌드 전 훅도 같은 생성기를 실행하며 MkDocs core가 배포 산출물의 sitemap을 만듭니다. 페이지 훅은 HTML 언어와 대응 번역의 `hreflang` 링크를 넣습니다. 계획 주소는 `mkdocs.yml`의 `site_url`에서 읽으며, 변경하면 생성물을 다시 확인하세요. 이 명령은 문서를 게시하지 않습니다.

프로젝트 경로의 `robots.txt`는 호스트 루트의 robots 정책을 대신하지 않습니다. 공개 가용성, 실제 MkDocs 렌더, 데스크톱·모바일 탐색과 테마·표 overflow 확인은 게시 전에 별도로 필요합니다.

[English](en/contributing.md)
