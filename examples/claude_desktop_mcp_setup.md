# 클로드 데스크톱에 한국 증권사 MCP 서버 연결하기

pykbroker는 `pykbroker-mcp` 명령으로 기본 읽기 전용 stdio 서버를 제공합니다. 이 가이드는 소스 체크아웃 기준이며 PyPI 설치나 MCP 레지스트리 등록을 전제로 하지 않습니다. Claude Desktop의 로컬 서버 설정 위치와 재시작 절차는 [MCP 공식 로컬 서버 가이드](https://modelcontextprotocol.io/docs/develop/connect-local-servers)를 참고했습니다.

## 1. 설치와 인증 준비

저장소에서 Python 3.11 이상으로 가상환경을 만들고 설치합니다.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[mcp,kis]'
```

Windows에서는 `.venv\Scripts\python.exe`를 사용합니다. `kis` extra는 보조 의존성만 설치합니다. [설치 안내](../docs/getting-started.md)에 따라 호환 `Pykis` 소스 또는 인증 클라이언트 구성을 준비해야 실제 조회가 가능합니다.

`PYKBROKER_ACCOUNT`와 JSON 객체인 `PYKBROKER_CLIENT_OPTIONS`는 외부 비밀 관리 도구가 **서버 프로세스 환경에** 주입하도록 구성하세요. GUI 앱은 터미널의 환경변수를 자동으로 물려받지 않을 수 있습니다. 필요하면 비밀 관리 도구의 실행 래퍼에서 `pykbroker-mcp`를 시작하도록 `command`와 `args`를 바꾸세요. pykbroker는 이 래퍼나 인증 토큰 관리 기능을 제공하지 않습니다. 실제 계좌·키를 설정 예제, 대화, 명령 이력에 복사하지 마세요.

## 2. claude_desktop_config.json 설정

Claude Desktop의 Settings → Developer → Edit Config에서 기존 `mcpServers`에 `pykbroker` 항목을 병합합니다. 기존 서버 설정을 덮어쓰지 마세요.

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

다음은 macOS 경로 템플릿입니다. `/absolute/path/to/pykbroker`를 실제 체크아웃의 **절대경로**로 바꾸세요. `command` 안의 `~`나 `$VARIABLE`은 셸처럼 확장된다고 가정하지 않습니다.

```json
{
  "mcpServers": {
    "pykbroker": {
      "command": "/absolute/path/to/pykbroker/.venv/bin/pykbroker-mcp",
      "args": [],
      "env": {
        "PYKBROKER_BROKER": "kis"
      }
    }
  }
}
```

Windows의 `command` 예시는 `C:\\absolute\\path\\to\\pykbroker\\.venv\\Scripts\\pykbroker-mcp.exe`입니다. JSON에서는 역슬래시를 두 번 씁니다. 계좌와 인증 옵션은 위에서 준비한 프로세스 환경으로 공급합니다. 이 JSON만으로 인증이 완료되지는 않습니다.

## 3. 읽기 전용 확인

Claude Desktop을 완전히 종료하고 다시 실행합니다. 서버 도구 목록에서 `get_quote`, `get_balance`, `get_positions`, `list_orders`, `get_rules`를 확인한 뒤 “한국투자증권에서 삼성전자 005930 현재가를 조회해 줘”처럼 요청합니다. `get_rules`는 기본 규칙을 반환하며 모든 상품의 호가 단위를 알 수 있는 것은 아닙니다.

`~/.pykbroker/limits.toml`이 없는 기본 환경에서는 `place_order`, `cancel_order`가 **목록에 없어야** 합니다. 기존에 주문 허용 설정이 있는 계정은 이 기본 조건과 다르므로 [MCP 안전 설정](../docs/mcp.md)을 먼저 확인하세요. 이 조회 가이드는 주문 허용 파일을 만들지 않습니다.

## 연결 실패 시

| 증상 | 확인할 것 |
| --- | --- |
| 서버가 표시되지 않음 / ENOENT | JSON 문법, 실제 절대 실행 경로, 설치한 가상환경, 앱 완전 재시작 |
| 도구는 보이나 조회 실패 | 서버 프로세스의 환경변수 주입, 호환 벤더 소스 import 경로, 인증 클라이언트 설정 |
| 네트워크가 없거나 인증 실패 | 실제 시세 대신 오류가 반환되는지 확인; 오프라인 확인은 `python examples/offline_quote.py`의 합성 시세 사용 |
| 주문 도구가 보이지 않음 | 기본 동작; 조회를 위해 주문을 활성화할 필요 없음 |

로그는 macOS `~/Library/Logs/Claude/`, Windows `%APPDATA%\Claude\logs`에서 확인합니다. 공유 전 계좌·토큰과 벤더 SDK 로그를 점검하세요. 이번 sandbox에서는 Claude Desktop 실제 연결을 검증하지 않았으며, 오프라인 도구 등록·호출 테스트와 실제 데스크톱 연결 검증은 별개입니다.
