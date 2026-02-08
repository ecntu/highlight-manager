from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./highlights.db"
    secret_key: str = "dev-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080
    session_cookie_name: str = "phm_session"
    session_max_age_seconds: int = 2592000
    session_same_site: str = "lax"
    session_https_only: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
