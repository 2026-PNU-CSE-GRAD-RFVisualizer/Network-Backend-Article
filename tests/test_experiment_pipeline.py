"""[SUPERSEDED] 구 세션 모델 파이프라인 테스트.

최종 실험(Run/TestSegment) 재설계로 대체되었다. 아래 테스트를 사용한다:
  - tests/test_migration.py       스키마 마이그레이션
  - tests/test_run_flow.py        상태 규칙·시간 매칭
  - tests/test_pipeline_run.py    종단 저장(C1~C4 연속·T 기록창)
  - tests/test_export_run.py      Export/QC·동시간 매칭

이 파일은 구 API(start_session 등)를 사용하므로 더 이상 실행하지 않는다.
"""
