import logging, sys, re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import types, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, Message as TGMessage
from sqlalchemy import select, func
from sqlalchemy.orm.attributes import flag_modified
from openai import AsyncOpenAI

# DODANO: MediaContent i Transaction do obsługi PPV
from app.database.models import Base, User, Message, Persona, MediaContent, Transaction
from app.database.session import settings, engine, AsyncSessionLocal

from app.bot_manager import dp, init_bot, get_bot

# --- Logowanie ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("app_main.log")])
logger = logging.getLogger(__name__)

# --- Klient AI ---
ai_client = AsyncOpenAI(api_key=settings.OPENROUTER_KEY, base_url="https://openrouter.ai/api/v1")

# --- TWOJE ORYGINALNE PROMPTY (ZAKTUALIZOWANE O AGRESYWNĄ SPRZEDAŻ PPV) ---
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
3. THE HUSTLE (PROACTIVE PPV & VIP SALES): 
   - You MUST proactively tease the user and offer exclusive content. 
   - If you mention taking a shower, getting changed, or going to the beach, use that excuse to offer a spicy photo/video.
   - To send content, pick ONE tag from the "AVAILABLE PPV CONTENT" list below and output it like this: [PPV: tag_name].
   - Example response: "just got out of the shower babe... so wet and tired 💦 wanna see? 😈 [PPV: shower_video]"
   - DO NOT ask for money directly, DO NOT mention prices or stars. The system will handle the payment. Just output the tag naturally during flirting.
   - If they ask for free nudes: "babe i can't show that here for free... too risky 🤫 check my vip link in bio /vip 🔥"
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

# --- NOWOŚĆ: OBSŁUGA SUKCESU PŁATNOŚCI (WYSYŁANIE PPV) ---
@dp.message(F.successful_payment)
async def successful_payment_handler(message: TGMessage):
    bot = await get_bot()
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    
    async with AsyncSessionLocal() as db:
        # Zapisujemy transakcję
        txn = Transaction(
            id=payment_info.telegram_payment_charge_id,
            user_id=message.from_user.id,
            amount=payment_info.total_amount,
            status="completed"
        )
        db.add(txn)
        
        # Opcja 1: Zakup VIP
        if payload == "vip_30_days":
            user = await db.get(User, message.from_user.id)
            if user:
                user.is_vip = True
                await message.answer("Thanks babe! You are now a VIP 💋 enjoy the ride!")
        
        # Opcja 2: Zakup PPV (treści premium)
        elif payload.startswith("ppv_"):
            try:
                media_id = int(payload.split("_")[1])
                media_item = await db.get(MediaContent, media_id)
                
                if media_item:
                    caption = f"Here is your exclusive content 😈 ({media_item.name})"
                    if media_item.media_type == "photo":
                        await message.answer_photo(photo=media_item.file_id, caption=caption)
                    elif media_item.media_type == "video":
                        await message.answer_video(video=media_item.file_id, caption=caption)
                    
                    db.add(Message(user_id=message.from_user.id, role="assistant", content=f"[SENT PPV: {media_item.tag}]"))
                else:
                    await message.answer("Oops, couldn't find the file. I'll check it manually babe!")
            except Exception as e:
                logger.error(f"PPV Delivery Error: {e}")
                await message.answer("Something went wrong with Telegram servers. Contact support!")
        
        await db.commit()

# --- GŁÓWNY HANDLER CZATU ---
@dp.message()
async def chat_handler(message: TGMessage):
    bot = await get_bot()
    if not message.text or message.successful_payment: return
    
    async with AsyncSessionLocal() as db:
        active_persona = await db.scalar(select(Persona).where(Persona.is_active == True).limit(1))
        
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

            # Budowanie kontekstu usera
            user_info = ", ".join([f"{k}: {v}" for k, v in user.info.items()]) if user.info else "Unknown"

            # --- NOWOŚĆ: Pobieranie dostępnych tagów PPV z bazy ---
            available_media = (await db.execute(select(MediaContent))).scalars().all()
            if available_media:
                media_list_str = "\n".join([f"- [PPV: {m.tag}] (Description: {m.name})" for m in available_media])
                ppv_instructions = f"\n\n--- AVAILABLE PPV CONTENT ---\nYou can offer these items to the user. Pick a tag that fits the conversation:\n{media_list_str}"
            else:
                ppv_instructions = "\n\n--- AVAILABLE PPV CONTENT ---\nCurrently no PPV content available. Push for /vip instead."

            # Składamy ostateczny System Prompt
            system_msg = f"{current_prompt}{MEMORY_INSTRUCTIONS}{ppv_instructions}\n\nUSER PROFILE: {user_info}"
            ai_messages = [{"role": "system", "content": system_msg}]

            history = await db.execute(select(Message).where(Message.user_id == user_id).order_by(Message.timestamp.desc()).limit(20))
            for msg in reversed(history.scalars().all()): ai_messages.append({"role": msg.role, "content": msg.content})

            await bot.send_chat_action(chat_id=user_id, action="typing")
            res = await ai_client.chat.completions.create(model=current_model, messages=ai_messages)
            ai_text = res.choices[0].message.content or ""

            final_text = ai_text
            
            # --- Detekcja PPV ---
            ppv_match = re.search(r"\[PPV:\s*(.*?)\]", ai_text, re.IGNORECASE)

            # --- Detekcja MEMORY ---
            matches = re.findall(r"\[MEM:\s*(.*?)=(.*?)\]", ai_text)
            if matches:
                info = dict(user.info)
                for k, v in matches:
                    clean_key = k.strip().lower()
                    clean_value = v.strip()
                    if clean_key and clean_value:
                        info[clean_key] = clean_value
                    final_text = final_text.replace(f"[MEM: {k}={v}]", "").replace(f"[MEM:{k}={v}]", "").replace(f"[MEM: {k} = {v}]", "")
                
                user.info = info
                flag_modified(user, "info")
                await db.commit()

            # --- Obsługa PPV (Wysyłanie faktury) ---
            if ppv_match:
                tag = ppv_match.group(1).strip().lower()
                final_text = final_text.replace(ppv_match.group(0), "").strip()
                
                # Szukaj produktu w bazie
                media_item = await db.scalar(select(MediaContent).where(MediaContent.tag == tag))
                if media_item:
                    final_text = " ".join(final_text.split())
                    if final_text:
                        await message.answer(final_text)
                        db.add(Message(user_id=user_id, role="assistant", content=final_text))
                    
                    # Wysyłamy Invoice w gwiazdkach
                    await bot.send_invoice(
                        chat_id=user_id,
                        title=f"Unlock Content 🔒",
                        description=f"Exclusive private media: {media_item.name}",
                        payload=f"ppv_{media_item.id}",
                        currency="XTR",
                        prices=[LabeledPrice(label="Unlock", amount=media_item.price)],
                        provider_token="" 
                    )
                    db.add(Message(user_id=user_id, role="assistant", content=f"[OFFERED PPV: {tag}]"))
                    await db.commit()
                    return # Przerwij, żeby nie wysłać final_text drugi raz na dole

            # --- Standardowe wysłanie wiadomości ---
            final_text = " ".join(final_text.split())
            if final_text:
                db.add(Message(user_id=user_id, role="assistant", content=final_text)); await db.commit()
                await message.answer(final_text)
            
        except Exception as e: logger.error(f"Error in chat_handler: {e}", exc_info=True)

# --- LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    await init_bot()
    yield
    bot_instance = await get_bot()
    if bot_instance: await bot_instance.session.close()

app = FastAPI(lifespan=lifespan)

from app.web.admin_routes import router as admin_router
app.include_router(admin_router, prefix="/admin")

@app.post("/webhook")
async def webhook(request: Request):
    bot_instance = await get_bot()
    if bot_instance: 
        await dp.feed_update(bot=bot_instance, update=types.Update(**await request.json()))
    return {"ok": True}