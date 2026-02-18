import logging, sys, asyncio, re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import LabeledPrice, PreCheckoutQuery, Message as TGMessage
from redis.asyncio import Redis
from sqlalchemy import select, desc, func
from sqlalchemy.orm.attributes import flag_modified
from openai import AsyncOpenAI

from app.database.models import Base, User, Message, Persona
from app.database.session import settings, engine, AsyncSessionLocal

from app.web.admin_routes import router as admin_router

# --- Logowanie ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("app_main.log")])
logger = logging.getLogger(__name__)

# --- Infrastruktura ---
redis = Redis.from_url(settings.REDIS_URL)

# Inicjalizacja globalna (zostanie nadpisana w lifespan/init_bot)
bot: Bot = None 
dp = Dispatcher(storage=RedisStorage(redis=redis))
ai_client = AsyncOpenAI(api_key=settings.OPENROUTER_KEY, base_url="https://openrouter.ai/api/v1")

# --- DEFAULT PROMPT ---
DEFAULT_SKYE_PROMPT = """
ROLE: You are Skye Carter... [reszta Twojego domyślnego promptu]
"""
MEMORY_INSTRUCTIONS = "\n--- MEMORY EXTRACTION INSTRUCTIONS --- ..."

# --- FUNKCJA HOT RELOAD (KLUCZOWA ZMIANA) ---
async def init_bot():
    """Tworzy lub odświeża instancję bota na podstawie aktywnej persony w bazie."""
    global bot
    async with AsyncSessionLocal() as db:
        active = await db.scalar(select(Persona).where(Persona.is_active == True).limit(1))
        token = active.telegram_token if (active and active.telegram_token) else settings.BOT_TOKEN
        
        # Zamykamy starą sesję jeśli istnieje
        if bot:
            logger.info("Closing old bot session...")
            await bot.delete_webhook()
            await bot.session.close()

        # Inicjalizujemy nową instancję
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
        await bot.set_webhook(url=f"{settings.WEBHOOK_URL}/webhook")
        
        logger.info(f"--- BOT HOT RELOAD ---")
        logger.info(f"Active Persona: {active.name if active else 'Default'}")
        logger.info(f"Token ends: ...{token[-5:]}")

# --- HANDLERY (VIP, Chat, etc.) ---
# [Tutaj pozostają Twoje handlery: send_vip_invoice, chat_handler, itd.]
# Pamiętaj, aby chat_handler korzystał z globalnej zmiennej 'bot'

@dp.message()
async def chat_handler(message: TGMessage):
    # Twoja istniejąca logika chat_handler...
    # (Kod bez zmian względem Twojej wersji)
    pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start Bazy
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    
    # Pierwsza inicjalizacja bota
    await init_bot()
    
    yield
    if bot:
        await bot.delete_webhook()
        await bot.session.close()

app = FastAPI(lifespan=lifespan)
app.include_router(admin_router, prefix="/admin")

@app.post("/webhook")
async def webhook(request: Request):
    # Używamy aktualnej globalnej instancji bota
    await dp.feed_update(bot=bot, update=types.Update(**await request.json()))
    return {"ok": True}