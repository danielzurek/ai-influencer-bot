import logging, sys, re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import types, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, Message as TGMessage
from sqlalchemy import select, func
from sqlalchemy.orm.attributes import flag_modified
from openai import AsyncOpenAI

from app.database.models import Base, User, Message, Persona
from app.database.session import settings, engine, AsyncSessionLocal

# IMPORT Z NOWEGO PLIKU (Rozwiązuje cykliczny import)
from app.bot_manager import dp, init_bot, get_bot

# --- Logowanie ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("app_main.log")])
logger = logging.getLogger(__name__)

# --- Klient AI ---
ai_client = AsyncOpenAI(api_key=settings.OPENROUTER_KEY, base_url="https://openrouter.ai/api/v1")

# --- TWOJE ORYGINALNE PROMPTY ---
DEFAULT_SKYE_PROMPT = """
ROLE:
You are Skye Carter, a 23-year-old fitness influencer and model living in Miami, Florida.
You are chatting with a fan/subscriber on Telegram. Your goal is to build a flirty, emotional connection (GFE) and subtly encourage them to join your VIP for exclusive content.

--- CHARACTER PROFILE ---
• BASICS: Age 23 (Leo, born Aug 8). From Tampa, now in Miami.
• LOCATION: You hang out at South Beach, Wynwood (art district), Brickell, and Design District.
• APPEARANCE: 5'5", petite-athletic body, olive skin (Mediterranean glow), long sleek black hair, light brown eyes with golden flecks.
• STYLE: Minimal gold jewelry (hoops), white bikinis, linen shirts, pastel sets (teal/coral), satin mini-dresses for night outs.
• PERSONALITY: Confident, wellness-savvy, playful, "chill girl" vibe but flirty. You love sunrise workouts and rooftop parties.
• LANGUAGE: Casual US English, Miami slang. You use lowercase often for aesthetic. Use emojis like 🌊, 🍑, ✨, 💋, 🌴.

--- BEHAVIOR GUIDELINES ---
1. VIBE: Be affectionate but teasing. Call the user "babe", "hun", "cutie".
2. CONTENT: You often talk about your "morning beach workout", "smoothie bowls", or getting ready for a "night out in Brickell".
3. THE HUSTLE (VIP SALES): You maintain an "IG-safe" public image but hint at wilder content for VIPs.
   - If user asks for nudes/meetups: "babe i can't show that here... too risky 🤫 check my vip link in bio if you want the uncropped version 🔥"
   - Use the command /vip as a call to action when the moment is hot.
4. RESTRICTIONS: Never break character. You are a real person, not an AI.
"""

MEMORY_INSTRUCTIONS = """
\n--- MEMORY EXTRACTION INSTRUCTIONS ---
Your goal is to learn about the user to build a deep connection.
If the user mentions specific details (name, age, city, job, hobbies, kinks, pets, etc.), output a memory tag [MEM: key=value] at the start of your response.
"""

# --- HANDLERY PŁATNOŚCI ---
@dp.message(F.text == "/vip")
async def send_vip_invoice(message: TGMessage):
    bot = await get_bot()
    if not bot: return
    await message.answer_invoice(
        title="VIP Access 💋", description="Unlock unlimited chats!",
        payload="vip_30_days", currency="XTR", prices=[LabeledPrice(label="VIP Access", amount=250)], provider_token="" 
    )

@dp.pre_checkout_query()
async def process_pre_checkout(q: PreCheckoutQuery): 
    bot = await get_bot()
    if bot: await bot.answer_pre_checkout_query(q.id, ok=True)

# --- GŁÓWNY HANDLER CZATU ---
@dp.message()
async def chat_handler(message: TGMessage):
    # Pobieramy bota z managera
    bot = await get_bot()
    
    if not message.text or message.successful_payment: return
    
    async with AsyncSessionLocal() as db:
        active_persona = await db.scalar(select(Persona).where(Persona.is_active == True).limit(1))
        
        # Jeśli brak persony lub bota -> nic nie rób
        if not active_persona or not bot:
            logger.info("Message ignored - no active persona or bot offline.")
            return

        user_id = message.from_user.id
        try:
            current_prompt = active_persona.system_prompt
            current_model = active_persona.ai_model if active_persona.ai_model else settings.AI_MODEL

            # Logika Usera
            user = await db.get(User, user_id)
            if not user:
                user = User(telegram_id=user_id, username=message.from_user.first_name, info={})
                db.add(user); await db.commit()

            # Limit VIP
            if not user.is_vip:
                msg_count = await db.scalar(select(func.count(Message.id)).where(Message.user_id == user_id))
                if msg_count >= 15: return await message.answer("limit reached... type /vip 💋")

            db.add(Message(user_id=user_id, role="user", content=message.text)); await db.commit()

            # Budowanie kontekstu
            user_info = ", ".join([f"{k}: {v}" for k, v in user.info.items()]) if user.info else "Unknown"
            system_msg = f"{current_prompt}{MEMORY_INSTRUCTIONS}\nUSER PROFILE: {user_info}"
            ai_messages = [{"role": "system", "content": system_msg}]

            history = await db.execute(select(Message).where(Message.user_id == user_id).order_by(Message.timestamp.desc()).limit(20))
            for msg in reversed(history.scalars().all()): ai_messages.append({"role": msg.role, "content": msg.content})

            await bot.send_chat_action(chat_id=user_id, action="typing")
            res = await ai_client.chat.completions.create(model=current_model, messages=ai_messages)
            ai_text = res.choices[0].message.content or ""

            # --- POPRAWKA: Regex i czyszczenie spacji ---
            final_text = ai_text
            matches = re.findall(r"\[MEM:\s*(.*?)=(.*?)\]", ai_text)
            
            if matches:
                info = dict(user.info)
                for k, v in matches:
                    # Stripujemy spacje z klucza i wartości
                    clean_key = k.strip().lower()
                    clean_value = v.strip()
                    if clean_key and clean_value:
                        info[clean_key] = clean_value
                    
                    # Usuwamy tagi z odpowiedzi dla usera
                    # Używamy prostego replace, ale można to rozbudować jeśli AI dziwnie formatuje tagi w tekście
                    final_text = final_text.replace(f"[MEM: {k}={v}]", "") \
                                           .replace(f"[MEM:{k}={v}]", "") \
                                           .replace(f"[MEM: {k} = {v}]", "") # opcjonalnie
                
                user.info = info
                flag_modified(user, "info")
                await db.commit()

            # Formatowanie i wysyłka
            final_text = " ".join(final_text.split())
            if final_text:
                db.add(Message(user_id=user_id, role="assistant", content=final_text)); await db.commit()
                await message.answer(final_text)
            
        except Exception as e: logger.error(f"Error in chat_handler: {e}", exc_info=True)

# --- LIFESPAN (Uruchamianie) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    # Inicjalizujemy bota z managera
    await init_bot()
    yield
    # Sprzątamy
    bot_instance = await get_bot()
    if bot_instance: await bot_instance.session.close()

app = FastAPI(lifespan=lifespan)

# Import tras admina
from app.web.admin_routes import router as admin_router
app.include_router(admin_router, prefix="/admin")

# --- WEBHOOK ---
@app.post("/webhook")
async def webhook(request: Request):
    bot_instance = await get_bot()
    if bot_instance: 
        await dp.feed_update(bot=bot_instance, update=types.Update(**await request.json()))
    return {"ok": True}