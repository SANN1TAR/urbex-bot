import os
import sys
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    telegram_token: str
    tavily_api_key: str
    database_url: str

def get_config() -> Config:
    missing = []
    token = os.getenv("TELEGRAM_TOKEN", "")
    tavily = os.getenv("TAVILY_API_KEY", "")
    db_url = os.getenv("DATABASE_URL", "")

    if not token:
        missing.append("TELEGRAM_TOKEN")
    if not tavily:
        missing.append("TAVILY_API_KEY")
    if not db_url:
        missing.append("DATABASE_URL")

    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        print("Set them in .env file or environment before starting the bot.")
        sys.exit(1)

    return Config(
        telegram_token=token,
        tavily_api_key=tavily,
        database_url=db_url,
    )
