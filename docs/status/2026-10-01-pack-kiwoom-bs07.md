# BS-07 키움 SDK 등록 — 현황 팩 (2026-10-01)

## 범위와 근거
- 기준 main: c9dbecde08e22a1e3ec6a041d86dff11e1be36de; BS-07 발주가 상위 기준이다.
- 읽은 문서: plan-broker-support §4.4·§9.2, map-m6 §3 키움·§8, 규칙 스펙 §0.5·§17.1.
- 기존 src/pykbroker/brokers/kiwoom.py와 examples/kiwoom_mock_roundtrip.py를 재사용했다.
- 구현·검증은 오프라인만: 실제 증권사·dev·운영·DB·VM 호출 및 배포 0.
- H-KIWOOM 미발급: 실제 모의 왕복은 메인 담당이며 이번 작업에서는 안 걸어 봄.

## 등록과 기본 꺼짐
- factory.py의 import·등록·오류 안내·옵션 전달 4줄만 변경했다.
- create_broker("kiwoom")은 정식 REST Kiwoom을 고르고 experimental 키움은 고르지 않는다.
- account·저장 token·app_key가 필요하다. 생성만으로 토큰을 발급하지 않는다.
- 가짜 client_factory를 넣어도 명시 client_options 또는 기존 env 옵션을 전달한다.
- virtual=True가 모의 호스트를 고른다. 새 env·자동 주문 허용 설정은 없다.
- 기존 OrderSafety 기본 dry-run이 주문과 취소를 전송 전에 거부한다.

## 오프라인 계약과 가격 부호
- 기존 주문·조회·취소 계약 시험도 정식 factory 경로를 지나도록 바꿨다.
- 주문: kt10000 매수·kt10001 매도, 지정가만; 취소: kt10003 정확 주문번호.
- 조회: ka10075 미체결·kt00007 당일 원장; 목록에서 사라졌다고 체결로 단정하지 않는다.
- 공통 SDK에 직접 정정 계약은 없다. kt10002 전송 기능을 새로 만들지 않았다.
- 가짜 정정확인·취소확인 원장은 완전 체결 외 원주문을 unknown으로 남긴다.
- + 가격은 크기 유지; - 가격은 기존 SpecUnverifiedError로 거부한다.
- 같은 종류 전수: 현재가·매수/매도호가·상/하한가·기준가·매입/잔고현재가·주문가.
- 현금·수량에는 가격 부호 처리를 적용하지 않는다. 음수는 잘못된 응답이다.
- 근거는 공식 샘플 기반 기존 fixture와 합성 응답이다. 실측 가격 부호 증거는 없다.
- - 시세 가짜 응답: token·ka10001까지만, 주문·취소 0. + 가격 왕복: 가짜 8호출.

## 모의 왕복 스크립트
- 기본 dry-run: 키를 읽지 않고 계획만 출력한다. 네트워크 0.
- --execute여도 키 누락·공백이면 missing_keys·키 없음·종료 0, 클라이언트 생성 0.
- missing_keys는 실행 생략이다. 왕복 성공 또는 실제 API 검증으로 세지 않는다.
- 기존 모의 호스트 고정·리다이렉트 거부·1999 무재시도·비밀값 미출력 가드를 유지했다.

## 검증 기록
- 수정 전 전체: 779 passed / 19 skipped; 등록·키 없음 새 시험은 8 failed를 재현했다.
- 키움 4개 시험 파일: 228 passed; 전체 회귀: 795 passed / 19 skipped / 0 failed.
- ruff check·format 검사와 git diff --check 통과; 독립 검수는 메인 Claude 담당이다.
- 계획의 실제 모의 왕복 끝 기준은 발주서대로 스크립트 준비로 한정; 공개·머지 승인은 아니다.
