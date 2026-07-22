from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None

    database_dsn: str = "postgresql://jhkang:jhkang@127.0.0.1:5432/jhkang_network"

    # --- 실시간 시각화 (9월 졸업작품 범위) ---
    # 논문 실험에는 사용하지 않으므로 기본 비활성.
    # true 로 바꾸면 200ms Window 동기화, WS /frames, frame 저장,
    # /position/latest 가 함께 켜진다.
    enable_realtime: bool = False

    window_size_ms: int = 200
    window_flush_interval_ms: int = 50
    node_timeout_seconds: float = 5.0
    timestamp_max_skew_ms: int = 600_000
    rssi_min: int = -100
    rssi_max: int = -10

    # --- 논문 실험 (7/23 강의실 측정) ---
    # experiment.db 와 ingest_raw.jsonl 이 저장되는 폴더
    experiment_data_dir: str = "data"
    # CSV / config 산출물이 생성되는 폴더 (계획서 §10 구조)
    export_root: str = "experiments"
    # 위치당 측정 시간 (계획서 §3.3)
    default_session_seconds: int = 30
    # 위치당 기대 샘플 수. 약 1초 주기 × 30초.
    expected_samples_per_point: int = 30
    # 펌웨어가 Filtered RSSI 를 정수 보존을 위해 ×10 으로 보내는 경우 10 으로 설정.
    # 계획서 §10 7/25 점검 항목 "Filtered RSSI x10 변환 오류" 대응.
    rssi_filtered_scale: float = 1.0

settings = Settings()
