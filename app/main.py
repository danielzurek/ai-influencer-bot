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
from sqlalchemy import select, desc
from openai import AsyncOpenAI

from pydantic_settings import BaseSettings
from app.database.models import Base, User, Message
from app.web.admin_routes import router as admin_router

# --- Configuration Load ---
class Settings(BaseSettings):
    BOT_TOKEN: str
    WEBHOOK_URL: str
    ADMIN_USER: str
    ADMIN_PASS: str
    DATABASE_URL: str
    REDIS_URL: str
    OPENROUTER_KEY: str
    
    # AI Parameters from .env
    AI_MODEL: str = "gryphe/mythomax-l2-13b"
    AI_TEMPERATURE: float = 0.8
    AI_MAX_TOKENS: int = 250
    SYSTEM_PROMPT: str = "You are a casual influencer. Use lowercase only."

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("app_main.log")
    ]
)
logger = logging.getLogger(__name__)

# --- Infrastructure ---
engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
redis = Redis.from_url(settings.REDIS_URL)
storage = RedisStorage(redis=redis)

bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)

ai_client = AsyncOpenAI(
    api_key=settings.OPENROUTER_KEY,
    base_url="https://openrouter.ai/api/v1"
)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# --- Bot Handlers ---
@dp.message()
async def chat_handler(message: types.Message):
    user_id = message.from_user.id
    text = message.text
    if not text: return

    try:
        async with AsyncSessionLocal() as db:
            # User registration/check
            user = await db.get(User, user_id)
            if not user:
                user = User(telegram_id=user_id, username=message.from_user.username)
                db.add(user)
                await db.commit()

            # Save incoming message
            db.add(Message(user_id=user_id, role="user", content=text))
            await db.commit()

            # Context building (last 10 msgs)
            hist = await db.execute(select(Message).where(Message.user_id==user_id).order_by(desc(Message.timestamp)).limit(10))
            msgs = [{"role": "system", "content": settings.SYSTEM_PROMPT}]
            for m in hist.scalars().all()[::-1]:
                msgs.append({"role": m.role, "content": m.content})

            # AI Generation
            await bot.send_chat_action(chat_id=user_id, action="typing")
            res = await ai_client.chat.completions.create(
                model=settings.AI_MODEL,
                messages=msgs,
                temperature=settings.AI_TEMPERATURE,
                max_tokens=settings.AI_MAX_TOKENS
            )
            ai_text = res.choices[0].message.content

            # Save and send
            db.add(Message(user_id=user_id, role="assistant", content=ai_text))
            await db.commit()
            await message.answer(ai_text)

    except Exception as e:
        logger.error(f"Chat error for {user_id}: {e}", exc_info=True)
        await message.answer("I'm a bit dizzy... can you repeat?")

# --- FastAPI Setup ---
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    logger.info("Application booting up...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await bot.set_webhook(url=f"{settings.WEBHOOK_URL}/webhook")
    yield
    await bot.delete_webhook()
    await engine.dispose()
    await redis.close()

app = FastAPI(lifespan=lifespan)
app.include_router(admin_router, prefix="/admin")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global crash: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": "Internal Server Error"})

@app.post("/webhook")
async def webhook(request: Request):
    update = types.Update(**await request.json())
    await dp.feed_update(bot=bot, update=update)
    return {"ok": True}