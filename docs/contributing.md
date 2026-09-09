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
