import logging, sys, asyncio, re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import LabeledPrice, PreCheckoutQuery, Message as TGMessage
from redis.asyncio import Redis
from sqlalchemy import select, desc, func
from sqlalchemy.orm.attributes import flag_modified
from openai import AsyncOpenAI

from app.database.models import Base, User, Message
from app.database.session import settings, engine, AsyncSessionLocal, get_db
from app.web.admin_routes import router as admin_router

# --- Logowanie ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("app_main.log")])
logger = logging.getLogger(__name__)

# --- Infrastruktura ---
redis = Redis.from_url(settings.REDIS_URL)

bot = Bot(
    token=settings.BOT_TOKEN, 
    default=DefaultBotProperties(parse_mode="HTML"),
)

dp = Dispatcher(storage=RedisStorage(redis=redis))
ai_client = AsyncOpenAI(api_key=settings.OPENROUTER_KEY, base_url="https://openrouter.ai/api/v1")

# --- INSTRUKCJE PAMIĘCI ---
MEMORY_INSTRUCTIONS = """
\n--- MEMORY EXTRACTION INSTRUCTIONS ---
Your goal is to learn about the user to build a deep connection.
If the user mentions specific details (name, age, city, job, hobbies, kinks, pets, etc.), output a memory tag [MEM: key=value] at the start of your response.

RULES:
1. Format: [MEM: key=value]
2. Use snake_case for keys (e.g. 'pet_name', 'favorite_color').
3. Keep values concise.
4. Example: User says "I live in Miami", you reply: "[MEM: city=Miami] I love Miami! Which part?"
"""

# --- 1. PŁATNOŚCI: Komenda /vip (Faktura) ---
@dp.message(F.text == "/vip")
async def send_vip_invoice(message: TGMessage):
    await message.answer_invoice(
        title="Skye Carter VIP Access 💋",
        description="Unlock unlimited chats and exclusive uncut content!",
        payload="vip_30_days",
        currency="XTR", 
        prices=[LabeledPrice(label="VIP Access", amount=250)],
        provider_token="" 
    )

# --- 2. PŁATNOŚCI: Pre-checkout ---
@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

# --- 3. PŁATNOŚCI: Sukces ---
@dp.message(F.successful_payment)
async def on_successful_payment(message: TGMessage):
    user_id = message.from_user.id
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user:
            user.is_vip = True
            await db.commit()
    
    await message.answer("Oh my god... thank you! 😍 You've just unlocked VIP access. Now we can talk as much as you want! I have no limits for you anymore... 🔥")

# --- 4. GŁÓWNY HANDLER CZATU ---
@dp.message()
async def chat_handler(message: TGMessage):
    if not message.text or message.successful_payment: return
    user_id = message.from_user.id
    
    try:
        async with AsyncSessionLocal() as db:
            # Rozpoznawanie użytkownika
            user = await db.get(User, user_id)
            if not user:
                display_name = message.from_user.username or message.from_user.first_name or f"User_{user_id}"
                user = User(telegram_id=user_id, username=display_name, info={})
                db.add(user); await db.commit()

            # Limit wiadomości dla osób bez VIP-a
            if not user.is_vip:
                msg_count = await db.scalar(select(func.count(Message.id)).where(Message.user_id == user_id))
                if msg_count >= 15:
                    return await message.answer("hey babe... i'm running out of free minutes for today. 🥺 type /vip so we can keep talking without stopping! 💋")

            # 1. Zapisujemy wiadomość usera
            db.add(Message(user_id=user_id, role="user", content=message.text)); await db.commit()

            # 2. BUDOWANIE KONTEKSTU (Pamięć Długotrwała + Historia)
            
            # A. Wstrzykiwanie Profilu (Injection)
            user_info_str = ", ".join([f"{k}: {v}" for k, v in user.info.items()]) if user.info else "Unknown"
            system_content = f"{settings.SYSTEM_PROMPT}{MEMORY_INSTRUCTIONS}\nUSER PROFILE DATA: {user_info_str}"
            
            ai_messages = [{"role": "system", "content": system_content}]

            # B. Pobieranie Historii (Short-Term Memory) - ostatnie 20 wiadomości
            history_stmt = select(Message).where(Message.user_id == user_id).order_by(Message.timestamp.desc()).limit(20)
            result = await db.execute(history_stmt)
            # Odwracamy, aby chronologia była poprawna (najstarsze -> najnowsze)
            for msg in reversed(result.scalars().all()):
                ai_messages.append({"role": msg.role, "content": msg.content})

            # Zapytanie do AI
            await bot.send_chat_action(chat_id=user_id, action="typing")
            res = await ai_client.chat.completions.create(
                model=settings.AI_MODEL, 
                messages=ai_messages
            )
            raw_ai_text = res.choices[0].message.content or ""

            # 3. OBSŁUGA BŁĘDÓW (Fail Tier)
            fail_key = f"fail_tier:{user_id}"
            if not raw_ai_text.strip():
                raw_count = await redis.get(fail_key)
                count = (int(raw_count) if raw_count else 0) + 1
                await redis.set(fail_key, count, ex=3600)
                
                if count == 1: raw_ai_text = "sorry, I got so distracted... can you repeat that? 😅"
                elif count == 2: raw_ai_text = "i'm doing something important right now, give me 10 mins and I'll be back! 💋"
                else: raw_ai_text = "gotta run now, talk to you tomorrow! bye! ❤️"
            else:
                await redis.delete(fail_key)

            # 4. MEMORY EXTRACTION (Zapisywanie nowych faktów)
            final_text = raw_ai_text
            # Szukamy [MEM: key=value]
            matches = re.findall(r"\[MEM:\s*(.*?)=(.*?)\]", raw_ai_text)
            
            if matches:
                current_info = dict(user.info) if user.info else {}
                for key, value in matches:
                    current_info[key.strip().lower()] = value.strip()
                    # Usuwamy tag z tekstu dla użytkownika
                    final_text = final_text.replace(f"[MEM: {key}={value}]", "")
                    final_text = final_text.replace(f"[MEM:{key}={value}]", "")
                
                user.info = current_info
                flag_modified(user, "info") # Ważne dla SQLAlchemy przy typie JSON
                await db.commit()
                logger.info(f"Updated memory for {user_id}: {current_info}")

            # Czyszczenie białych znaków po usunięciu tagów
            final_text = " ".join(final_text.split())

            # Zapisujemy odpowiedź AI i wysyłamy
            db.add(Message(user_id=user_id, role="assistant", content=final_text)); await db.commit()
            await message.answer(final_text)
            
    except Exception as e: 
        logger.error(f"Error: {e}", exc_info=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Czekanie na bazę
    retries = 5
    while retries > 0:
        try:
            async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
            break
        except:
            retries -= 1
            await asyncio.sleep(2)
    await bot.set_webhook(url=f"{settings.WEBHOOK_URL}/webhook")
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)
app.include_router(admin_router, prefix="/admin")

@app.post("/webhook")
async def webhook(request: Request):
    update = types.Update(**await request.json())
    await dp.feed_update(bot=bot, update=update)
    return {"ok": True}