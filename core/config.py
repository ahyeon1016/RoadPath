import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    tmap_api_key: str | None
    gemini_api_key: str | None
    gemini_model: str

    @property
    def ready(self) -> bool:
        return bool(self.tmap_api_key and self.gemini_api_key)


def get_settings() -> Settings:
    gemini_api_key = os.getenv("GEMINI_API_KEY")

    # langchain-google-genai reads GOOGLE_API_KEY by default.
    # Reuse the existing GEMINI_API_KEY without exposing it to the browser.
    if gemini_api_key and not os.getenv("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = gemini_api_key

    return Settings(
        tmap_api_key=os.getenv("TMAP_API_KEY"),
        gemini_api_key=gemini_api_key,
        gemini_model="gemini-3.5-flash-lite",
    )
