# Official PLUG source snapshot — 2026-10-08

공개 문서 GET만 수행; 증권사 API 호출 0건. 원본은 런 폴더 sources에 보존.
JSON은 위 8개 경로의 요청·응답 스키마와 참조 components만 발췌; example/examples는 제거했다.
필드·required·설명은 원본 그대로이며 업무 응답의 rsp_cd·가짜 데이터는 시험에서 별도로 넣는다.
brand는 SDK brand_of의 nhplug(나무) / n2plug(N2); 허용 주소는 각 브랜드 api/moapi의 HTTPS :8443뿐.
토큰·acctinfo는 common 운영 전용 명세; N2의 인증/조회/주문 도메인은 공식 SDK로 교차 대조.

| 원본 | 받은 시각(KST) | 원본 SHA256 |
| --- | --- | --- |
| [common.json](https://www.nhplug.com/openapi-docs/common/openapi.json) | 2026-10-08T09:08:05.092903+09:00 | `f77ba5479f6bc9f4006316f98bc200cda6d5a0214c14201369be4f357c5366de` |
| [krstock.json](https://www.nhplug.com/openapi-docs/krstock/openapi.json) | 2026-10-08T09:08:05.241320+09:00 | `694fc0e709b46c6f8466b98450a2f0564371ba02bd71ec771d223ef2c47b2448` |
| [gbstock.json](https://www.nhplug.com/openapi-docs/gbstock/openapi.json) | 2026-10-08T09:08:05.302754+09:00 | `88944f68f582db1b95ec7b8853cf62f497cd65365c7499da481bd4916078942e` |
| [llms.txt](https://www.nhplug.com/llms.txt) | 2026-10-08T09:08:05.351298+09:00 | `d5922899690c78ea71e52d4ad6230b5553df881f04cc25cb96d9fb8693e28f82` |
| [sdk-readme.md](https://raw.githubusercontent.com/PLUG-OpenAPI/nhplug-sdk/main/README.md) | 2026-10-08T09:08:05.724252+09:00 | `fa50e4fc513c223f631725f1732c79bd4ee0039ce4edb4b2b6602a4877ffedbb` |
| [sdk-auth.py](https://raw.githubusercontent.com/PLUG-OpenAPI/nhplug-sdk/main/nhplug/auth.py) | 2026-10-08T09:08:05.980992+09:00 | `94a8d1137c56c6b2a4433fa3feb12432bcf350a3f1b117545fe2d462ae084fd6` |

발췌: common.json(token/acctinfo), krstock.json(balance/dailyOrderExecution), gbstock.json(buy/sell/cancel/unexecuted).
sdk-domains.txt: sdk-auth.py의 brand_of·ALLOWED_HOSTS, SDK README의 브랜드·도메인 설명 발췌.
llms.txt 원본 SHA256은 위 표에 기록; 토큰 재발급 보안 알림 설명을 팩에 반영.
원본의 계좌 예시와 인증 응답 예시는 커밋하지 않으며 시험 입력·응답은 모두 가짜 값이다.
