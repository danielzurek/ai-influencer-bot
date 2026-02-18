import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy import select
from app.database.session import settings, AsyncSessionLocal
from app.database.models import Persona

# --- Konfiguracja Logera ---
logger = logging.getLogger(__name__)

# --- Infrastruktura Wspólna ---
redis = Redis.from_url(settings.REDIS_URL)
dp = Dispatcher(storage=RedisStorage(redis=redis))

# Globalna instancja bota
bot: Bot = None

async def init_bot():
    """
    Zarządza cyklem życia instancji bota (Hot Reload).
    Zamyka starą sesję, czyści webhooki i uruchamia nową konfigurację.
    """
    global bot
    async with AsyncSessionLocal() as db:
        active_persona = await db.scalar(select(Persona).where(Persona.is_active == True).limit(1))
        
        # 1. Sprzątanie po poprzednim bocie
        if bot is not None:
            logger.info("Closing previous bot session...")
            try:
                # WAŻNE: Usuwamy webhook starego bota, żeby Telegram nie słał update'ów
                # do starego tokena (błąd Conflict).
                await bot.delete_webhook(drop_pending_updates=True)
                await bot.session.close()
            except Exception as e:
                logger.error(f"Error closing bot: {e}")
            finally:
                bot = None

        # 2. Inicjalizacja nowego bota
        if active_persona:
            token = active_persona.telegram_token if active_persona.telegram_token else settings.BOT_TOKEN
            
            try:
                # Tworzymy nową instancję
                new_bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
                
                # Ustawiamy webhook dla nowego tokena
                webhook_url = f"{settings.WEBHOOK_URL}/webhook"
                await new_bot.set_webhook(url=webhook_url, drop_pending_updates=True)
                
                bot = new_bot
                logger.info(f"--- BOT ACTIVE: {active_persona.name} (Model: {active_persona.ai_model}) ---")
            except Exception as e:
                logger.error(f"Failed to initialize bot for {active_persona.name}: {e}")
        else:
            logger.info("--- ALL MODELS INACTIVE: BOT OFFLINE ---")

async def get_bot():
    """Pomocnicza funkcja do bezpiecznego pobierania bota w handlerach."""
    return bot