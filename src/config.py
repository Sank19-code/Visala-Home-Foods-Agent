# Central config. Loads .env and refuses to run against Razorpay LIVE keys.
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str

    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""

    database_url: str = "sqlite:///./visala_demo.db"
    public_base_url: str = "http://localhost:8000"

    max_order_amount_inr: int = 2000
    max_daily_amount_inr: int = 5000
    require_user_confirmation: bool = True

    def validate_test_mode(self) -> None:
        # Hard stop: this project is a public demo and must never touch real money.
        if not self.razorpay_key_id.startswith("rzp_test_"):
            raise RuntimeError(
                "Refusing to start: RAZORPAY_KEY_ID is not a test-mode key. "
                "This project runs on Razorpay test mode only."
            )


settings = Settings()
settings.validate_test_mode()
