# 공식 키움 예시 고정본

- 출처: https://raw.githubusercontent.com/Kiwoom-Securities/Kiwoom-REST-API/69642586f7d84ba9fd8a6faf1f1537c7fda6568b/kiwoom/_data/kiwoom_api_spec.json
- 커밋 전체 SHA: `69642586f7d84ba9fd8a6faf1f1537c7fda6568b`
- 받은 시각(KST): 2026-10-08T09:07:51.351689+09:00
- 원본 sha256: `42a7b3912c9d9588c83bdc2db7779c8d2e038703a2b5562e54ef46ae905cba79`
- 원본은 런 폴더의 kiwoom_api_spec.json에 보관; fixture는 요청·응답 예시만 추출.
- ka10076·kt10002는 이 어댑터가 호출하지 않아 제외. ka10095는 가격 방향 근거만 보존하며 호출하지 않음.
- ka10075 acnt_no는 가짜 `0000000000`으로 치환. kt10000 ord_no 뒤 누락 쉼표만 복원.
- error_codes.json은 공식 오류표의 1700~1702 행만 보존.
- ka10001 값 어긋남은 원본 그대로; 취소확인·다주 평균가 예시는 원본에 없어 시험에서 파생 사례라고 명시.
- P1-1: usa20100·usa20101·ust21070·ust21050 요청·응답은 위 원본을 재사용해 그대로 추출(재다운로드 없음, 원본 sha256 일치). 미국 음수 가격 시험은 원본 예시의 가격 부호만 바꾼 파생 사례이며, usa20100 연중최저 `-164.2700`은 원본 그대로.
