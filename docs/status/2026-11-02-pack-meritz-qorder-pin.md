# Meritz + q-order 합친 핀 — 2026-11-02
- 목적: trader #777 메리츠와 release/q-order-d37c-1028의 NH·LS·KB를 한 pykbroker 핀에 보존.
- 가지: release/pin-meritz-qorder-1103. 핀 전용 draft PR; main 머지 금지, do-not-merge 유지.
- 첫 부모: 1f683f7f40e1e00488454cecf118ef9d6d967e31 (NH·LS·KB/q-order).
- 둘째 부모: b568d9eb442ddd2688f2b460b829b13ab1f32c6a (Meritz).
- 공통 조상: 68fbc525392c7680f3ddd05136188ce5ba3b0a8e.
## 보존과 충돌
- src 충돌 없음. 양쪽 기존 구현·시험·fixture를 그대로 보존했다.
- docs/BROKER_MATRIX.md: q-order의 LS·Kiwoom·NH·KB 최신 행 유지, Meritz 행 추가.
- 폐기: Meritz 부모 docs/BROKER_MATRIX.md:15-18의 조상 시대 LS·Kiwoom·NH·KB 설명.
- tests/test_contract_uniformity.py: 등록 키에 Meritz 추가, 등록 클래스 일치 검사도 유지.
- SHIPPED에 Meritz를 포함하고 등록 클래스 집합 == SHIPPED 불변식을 보존한다.
- 공통시험 모듈에서만 Meritz spec을 monkeypatch하고 synthetic client로 검증한다.
- 실제 Meritz 기본 쓰기 잠금은 유지; 원본 전용 계약시험의 잠금·실패·주문/취소 검사는 그대로다.
- 충돌 두 파일 이외 부모 변경 132개를 Git blob 대조하여 모두 그대로임을 확인.
## 검증 경계
- 모든 pytest: bwrap --unshare-net + /run 마스킹 + docker/rm 실행차단 + env -i.
- 외부 네트워크 차단·빈 /run·명령 차단·loopback 자기연결을 실제 probe로 확인.
- Meritz 서버는 in-process OpenAPI 계약 서버이며 실증권사 API가 아니다.
- 시험 명령·원출력·exit·환경·최초 실패: /home/quantus/coding/cx-pyk-merge-1103/.
- 운영 VM·DB·실주문·실증권사 호출·배포 0. dev/운영 고객 경로는 안 걸어 봄.
- 새 프로젝트 의존성·설정·상한·동작 변경 0. 외부 전용 시험 venv만 준비했다.
- 본 핀을 trader에 이식하는 일과 운영 승인은 별도 작업이다.
