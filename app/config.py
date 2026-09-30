from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://skycoach:skycoach@db:5432/skycoach"

    gemini_api_key: str = ""
    gemini_base_url: str = ""
    # auto | openai | google — auto: sk-aitunnel* / aitunnel URL → openai
    llm_api_style: str = "auto"
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_models: str = "gemini-3.5-flash,gemini-3.1-flash-lite"
    gemini_media_resolution: str = "MEDIA_RESOLUTION_MEDIUM"
    gemini_max_concurrency: int = 3
    gemini_price_input_per_m: float = 150.0
    gemini_price_output_per_m: float = 750.0
    gemini_price_currency: str = "₽"

    gemini_cascade: bool = False
    gemini_cheap_model: str = "gemini-3.1-flash-lite"
    gemini_cheap_price_input_per_m: float = 76.0
    gemini_cheap_price_output_per_m: float = 455.0

    apify_token: str = ""
    apify_actor: str = "apify~instagram-scraper"
    apify_price_per_result: float = 0.0027

    instagram_cookies_file: str = ""

    worker_concurrency: int = 3
    # 0 = без лимита (берётся max_urls_per_request)
    prep_concurrency: int = 0
    ai_concurrency: int = 3
    max_urls_per_request: int = 20
    max_attempts: int = 3
    stale_job_minutes: int = 8

    max_video_seconds: int = 1800
    max_download_mb: int = 300
    max_frames: int = 120
    max_fps: float = 2.0
    frame_height: int = 960
    frame_batch_size: int = 60

    work_dir: str = "/tmp/skycoach"
    tunnel_metrics_url: str = "http://tunnel:2000/quicktunnel"


settings = Settings()
