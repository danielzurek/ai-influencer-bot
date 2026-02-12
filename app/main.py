import logging, sys, asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy import select, desc
from openai import AsyncOpenAI

from app.database.models import Base, User, Message
from app.database.session import settings, engine, AsyncSessionLocal, get_db
from app.web.admin_routes import router as admin_router

# --- Konfiguracja Logowania ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("app_main.log")])
logger = logging.getLogger(__name__)

# --- Infrastruktura ---
redis = Redis.from_url(settings.REDIS_URL)
bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=RedisStorage(redis=redis))
ai_client = AsyncOpenAI(api_key=settings.OPENROUTER_KEY, base_url="https://openrouter.ai/api/v1")

@dp.message()
async def chat_handler(message: types.Message):
    if not message.text: return
    user_id = message.from_user.id
    
    try:
        async with AsyncSessionLocal() as db:
            # 1. Zarządzanie użytkownikiem
            user = await db.get(User, user_id)
            if not user:
                display_name = message.from_user.username or message.from_user.first_name or f"User_{user_id}"
                user = User(telegram_id=user_id, username=display_name)
                db.add(user); await db.commit()

            db.add(Message(user_id=user_id, role="user", content=message.text))
            await db.commit()

            # 2. Przygotowanie kontekstu
            hist = await db.execute(select(Message).where(Message.user_id==user_id).order_by(desc(Message.timestamp)).limit(10))
            payload = [{"role": "system", "content": settings.SYSTEM_PROMPT}]
            for m in hist.scalars().all()[::-1]:
                payload.append({"role": m.role, "content": m.content})

            # 3. Zapytanie do AI
            await bot.send_chat_action(chat_id=user_id, action="typing")
            res = await ai_client.chat.completions.create(
                model=settings.AI_MODEL, 
                messages=payload,
                temperature=settings.AI_TEMPERATURE,
                max_tokens=settings.AI_MAX_TOKENS
            )
            
            ai_text = res.choices[0].message.content or ""

            # --- LOGIKA 3 SCENARIUSZY (FALLBACK) ---
            fail_key = f"fail_tier:{user_id}"
            if not ai_text.strip():
                # Pobieramy obecny poziom błędu z Redis
                raw_count = await redis.get(fail_key)
                count = int(raw_count) if raw_count else 0
                count += 1
                # Zapisujemy na 1 godzinę (3600s)
                await redis.set(fail_key, count, ex=3600) 

                if count == 1:
                    ai_text = "sorki, tak się zamyśliłam... powtórzysz? 😅"
                elif count == 2:
                    ai_text = "robię właśnie coś ważnego, daj mi 10 minut i zaraz wracam! 💋"
                else:
                    ai_text = "muszę uciekać, będę jutro! paaa ❤️"
                
                logger.warning(f"Uruchomiono Fallback Tier {count} dla {user_id}")
            else:
                # Jeśli AI odpowiedziało poprawnie, zerujemy licznik błędów
                await redis.delete(fail_key)

            # 4. Zapis i wysyłka
            db.add(Message(user_id=user_id, role="assistant", content=ai_text))
            await db.commit()
            await message.answer(ai_text)
            
    except Exception as e: 
        logger.error(f"Chat Error: {e}", exc_info=True)
        await message.answer("oops, małe spięcie! spróbuj za chwilę.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Czekanie na bazę danych
    retries = 5
    while retries > 0:
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            break
        except:
            retries -= 1
            await asyncio.sleep(2)

    await bot.set_webhook(url=f"{settings.WEBHOOK_URL}/webhook")
    logger.info("Bot Kasi jest gotowy!")
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)
app.include_router(admin_router, prefix="/admin")

@app.post("/webhook")
async def webhook(request: Request):
    update = types.Update(**await request.json())
    await dp.feed_update(bot=bot, update=update)
    return {"ok": True}