import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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
    DATABASE_URL: str
    REDIS_URL: str
    OPENROUTER_KEY: str

    class Config:
        env_file = ".env"
        # Ignoruj nadmiarowe zmienne w .env (np. komentarze)
        extra = "ignore" 

settings = Settings()

# --- 🛠️ LOGGING SETUP (FIX) ---
# To tworzy master.log i jednocześnie wysyła na konsolę Dockera
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),      # Konsola (Docker logs)
        logging.FileHandler("app_main.log")     # Plik na dysku (Master Log)
    ]
)
logger = logging.getLogger(__name__)

# --- Infrastructure Setup ---
engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

redis = Redis.from_url(settings.REDIS_URL)
storage = RedisStorage(redis=redis)

bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# --- FastAPI Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    logger.info("🚀 Starting up application...")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        
        logger.info(f"Setting Webhook to {settings.WEBHOOK_URL}/webhook")
        await bot.set_webhook(url=f"{settings.WEBHOOK_URL}/webhook")
        yield
    except Exception as e:
        logger.error(f"❌ Critical Startup Error: {e}", exc_info=True)
        raise
    finally:
        logger.info("🛑 Shutting down...")
        await bot.delete_webhook()
        await engine.dispose()
        await redis.close()

app = FastAPI(lifespan=lifespan, title="AI GFE Bot Admin")

# --- 🛡️ GLOBAL EXCEPTION HANDLER (FIX) ---
# To łapie każdy błąd, który nie został obsłużony ręcznie
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"🔥 UNHANDLED EXCEPTION: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error. Support has been notified."}
    )

app.include_router(admin_router, prefix="/admin", tags=["Admin"])

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update_data = await request.json()
        update = types.Update(**update_data)
        await dp.feed_update(bot=bot, update=update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"⚠️ Webhook Error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}