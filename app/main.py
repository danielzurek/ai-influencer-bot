import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from pydantic_settings import BaseSettings
from app.database.models import Base
from app.web.admin_routes import router as admin_router

# --- Configuration ---
class Settings(BaseSettings):
    BOT_TOKEN: str
    WEBHOOK_URL: str
    ADMIN_USER: str
    ADMIN_PASS: str
    DATABASE_URL: str  # postgresql+asyncpg://user:pass@db:5432/db_name
    REDIS_URL: str     # redis://redis:6379/0
    OPENROUTER_KEY: str

    class Config:
        env_file = ".env"

settings = Settings()

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Infrastructure Setup ---
# 1. Database
engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# 2. Redis & Aiogram Storage
redis = Redis.from_url(settings.REDIS_URL)
storage = RedisStorage(redis=redis)

# 3. Bot & Dispatcher
bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)

# --- Dependency Injection for Routes ---
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# --- Simple Bot Handler (Placeholder for full bot logic) ---
# In a real app, move these to app/bot/handlers
@dp.message()
async def echo_handler(message: types.Message):
    """
    Temporary handler to prove bot works. 
    In production: Call LLM Service here.
    """
    # Example: Check DB for user, add message to history, call LLM
    await message.answer("Bot is active! Configure LLM service in logic.")

# --- FastAPI Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    # Startup
    logger.info("Initializing Database...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    logger.info(f"Setting Webhook to {settings.WEBHOOK_URL}/webhook")
    await bot.set_webhook(url=f"{settings.WEBHOOK_URL}/webhook")
    
    yield
    
    # Shutdown
    logger.info("Removing Webhook...")
    await bot.delete_webhook()
    await engine.dispose()
    await redis.close()

# --- App Initialization ---
app = FastAPI(lifespan=lifespan, title="AI GFE Bot Admin")

# Mount Admin Router
app.include_router(admin_router, prefix="/admin", tags=["Admin"])

# --- Webhook Endpoint ---
@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Entry point for Telegram updates.
    """
    update_data = await request.json()
    update = types.Update(**update_data)
    await dp.feed_update(bot=bot, update=update)
    return {"status": "ok"}