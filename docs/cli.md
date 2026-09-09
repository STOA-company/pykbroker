---
description: "pykbroker CLI로 시세·잔고·보유 종목·주문 상태·규칙을 조회하는 명령과 오류 종료 코드."
lang: ko
---

# CLI

```bash
pykbroker --help
pykbroker --version
pykbroker brokers
pykbroker --broker kis quote 005930
pykbroker --broker dbfi balance
pykbroker --broker dbfi positions
pykbroker --broker kis orders --symbol 005930
pykbroker --broker kis rules 005930
```

인증 설정은 [환경변수 안내](getting-started.md)를 따릅니다. CLI는 조회 전용입니다. 설정·벤더 오류는 계좌·인증값을 포함하지 않는 오류와 종료 코드 2로 보고합니다.

[English](en/cli.md)
