# Central config. Loads .env and refuses to run against Razorpay LIVE keys.
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str

    # Buyer agent LLM, called through LiteLLM. Defaults to Claude.
    #   LLM_PROVIDER + LLM_MODEL -> "anthropic/claude-sonnet-5" (a full "provider/model" also works)
    #   LLM_API_BASE set         -> requests go to a LiteLLM proxy (gateway) at that URL instead,
    #                               and LLM_MODEL is the model alias configured on the proxy.
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5"
    llm_api_key: str = ""
    llm_api_base: str = ""
    agent_max_turns: int = 30

    # "razorpay" = real Razorpay TEST API. "fake" = in-memory stand-in for tests/evals/offline demos.
    payment_gateway: Literal["razorpay", "fake"] = "razorpay"

    database_url: str = "sqlite:///./visala_demo.db"
    public_base_url: str = "http://localhost:8000"

    max_order_amount_inr: int = 2000
    max_daily_amount_inr: int = 5000
    require_user_confirmation: bool = True

    @property
    def litellm_model(self) -> str:
        if self.llm_api_base:
            return self.llm_model if "/" in self.llm_model else f"litellm_proxy/{self.llm_model}"
        return self.llm_model if "/" in self.llm_model else f"{self.llm_provider}/{self.llm_model}"

    def validate_test_mode(self) -> None:
        # Hard stop: this project is a public demo and must never touch real money.
        if not self.razorpay_key_id.startswith("rzp_test_"):
            raise RuntimeError(
                "Refusing to start: RAZORPAY_KEY_ID is not a test-mode key. "
                "This project runs on Razorpay test mode only."
            )


settings = Settings()
settings.validate_test_mode()
