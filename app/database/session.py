import os
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

class Settings(BaseSettings):
    BOT_TOKEN: str
    WEBHOOK_URL: str
    ADMIN_USER: str
    ADMIN_PASS: str
    DATABASE_URL: str
    REDIS_URL: str
    OPENROUTER_KEY: str
    
    # Te dane są wczytywane z Twojego .env
    AI_MODEL: str = "google/gemini-2.0-flash-exp:free"
    AI_TEMPERATURE: float = 0.8
    AI_MAX_TOKENS: int = 250
    SYSTEM_PROMPT: str = "Jesteś Kasia, pisz luźno."

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session