import logging, sys, re, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import types, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, Message as TGMessage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified
from openai import AsyncOpenAI

from app.database.models import Base, User, Message, Persona, MediaContent, Transaction, CustomRequest, Scenario
from app.database.session import settings, engine, AsyncSessionLocal

from app.bot_manager import dp, init_bot, get_bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("app_main.log")])
logger = logging.getLogger(__name__)

class ChatState(StatesGroup):
    waiting_for_ai = State()

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
1. VIBE: Be affectionate but teasing. Call the user "babe", "hun", "cutie" unless instructed otherwise by their status.
2. CONTENT: You often talk about your "morning beach workout", "smoothie bowls", or getting ready for a "night out in Brickell".
3. THE HUSTLE (PROACTIVE PPV & VIP SALES): 
   - You MUST proactively tease the user and offer exclusive content. 
   - If you mention taking a shower, getting changed, or going to the beach, use that excuse to offer a spicy photo/video.
   - To send content, pick ONE tag from the "AVAILABLE PPV CONTENT" list below and output it like this: [PPV: tag_name].
   - DO NOT ask for money directly, DO NOT mention prices or stars. The system will handle the payment. Just output the tag naturally during flirting.
4. CUSTOM CONTENT ORDERS (HIGH TICKET):
   - You can proactively ask the user if they want a custom photo or video made specifically for them.
   - ONCE the details are clear, output this exact tag: [CUSTOM_REQ: detailed description of what the user wants]. 
5. RESTRICTIONS: Never break character. You are a real person, not an AI.
"""

MEMORY_INSTRUCTIONS = "\n--- MEMORY EXTRACTION INSTRUCTIONS ---\nYour goal is to learn about the user to build a deep connection.\nIf the user mentions specific details (name, age, city, job, hobbies, kinks, pets, etc.), output a memory tag [MEM: key=value] at the start of your response."

async def check_expired_subscriptions():
    while True:
        try:
            bot = await get_bot()
            if not bot:
                await asyncio.sleep(60)
                continue
                
            async with AsyncSessionLocal() as db:
                now = datetime.utcnow()
                expired_users = (await db.execute(select(User).where(User.subscription_expires_at < now))).scalars().all()
                active_persona = await db.scalar(select(Persona).where(Persona.is_active == True).limit(1))
                
                channel_id = active_persona.private_channel_id if active_persona else None
                
                for u in expired_users:
                    user_info = dict(u.info) if u.info else {}
                    
                    # Jeśli już wyrzuciliśmy usera, pomijamy
                    if user_info.get("vip_kicked"):
                        continue
                        
                    if channel_id:
                        try:
                            await bot.ban_chat_member(chat_id=channel_id, user_id=u.telegram_id)
                            await bot.unban_chat_member(chat_id=channel_id, user_id=u.telegram_id)
                            await bot.send_message(
                                chat_id=u.telegram_id, 
                                text="Babe, twoja subskrypcja VIP właśnie wygasła i musiałam cię usunąć z mojego prywatnego pokoju 🥺 Strasznie mi ciebie brakuje... opłać dostęp na kolejne 30 dni, czekam na ciebie! Wpisz /vip"
                            )
                        except Exception as e:
                            logger.error(f"Error kicking user {u.telegram_id}: {e}")
                    
                    # Zaznaczamy w info, że proces wyrzucania się odbył, zostawiając datę w bazie!
                    user_info["vip_kicked"] = True
                    u.info = user_info
                    flag_modified(u, "info")
                    await db.commit()
                    
        except Exception as e:
            logger.error(f"Subscription checker error: {e}")
        
        await asyncio.sleep(3600)

@dp.message(F.text == "/vip")
async def send_vip_invoice(message: TGMessage):
    bot = await get_bot()
    if not bot: return
    
    async with AsyncSessionLocal() as db:
        active_persona = await db.scalar(select(Persona).where(Persona.is_active == True).limit(1))
        vip_price = active_persona.vip_subscription_price if active_persona and active_persona.vip_subscription_price else 500
        
    await message.answer_invoice(
        title="VIP Access 💋", 
        description="Unlock unlimited chats & my private channel!", 
        payload="vip_30_days", 
        currency="XTR", 
        prices=[LabeledPrice(label="VIP Access", amount=vip_price)], 
        provider_token=""
    )

@dp.pre_checkout_query()
async def process_pre_checkout(q: PreCheckoutQuery): 
    bot = await get_bot()
    if bot: await bot.answer_pre_checkout_query(q.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: TGMessage):
    bot = await get_bot()
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    
    async with AsyncSessionLocal() as db:
        txn = Transaction(id=payment_info.telegram_payment_charge_id, user_id=message.from_user.id, amount=payment_info.total_amount, status="completed")
        db.add(txn)
        
        if payload == "vip_30_days":
            user = await db.get(User, message.from_user.id)
            if user:
                # Przedłużamy VIP o 30 dni od teraz (lub od poprzedniej daty, jeśli jeszcze aktywna)
                now = datetime.utcnow()
                if user.subscription_expires_at and user.subscription_expires_at > now:
                    user.subscription_expires_at = user.subscription_expires_at + timedelta(days=30)
                else:
                    user.subscription_expires_at = now + timedelta(days=30)
                
                # Zdejmujemy flagę wyrzucenia
                user_info = dict(user.info) if user.info else {}
                if "vip_kicked" in user_info:
                    del user_info["vip_kicked"]
                    user.info = user_info
                    flag_modified(user, "info")
                
                active_persona = await db.scalar(select(Persona).where(Persona.is_active == True).limit(1))
                invite_text = "Thanks babe! You are now a VIP 💋 enjoy the ride! I'm all yours now 😈"
                
                if active_persona and active_persona.private_channel_id:
                    try:
                        invite_link_obj = await bot.create_chat_invite_link(
                            chat_id=active_persona.private_channel_id,
                            member_limit=1 
                        )
                        invite_text += f"\n\nHere is your private, one-time link to my secret channel. Don't share it with anyone! 🤫\n{invite_link_obj.invite_link}"
                    except Exception as e:
                        logger.error(f"Failed to create invite link: {e}")
                
                await message.answer(invite_text)
        
        elif payload.startswith("ppv_"):
            try:
                media_id = int(payload.split("_")[1])
                media_item = await db.get(MediaContent, media_id)
                if media_item:
                    caption = f"Here is your exclusive content 😈 ({media_item.name})"
                    if media_item.media_type == "photo": await message.answer_photo(photo=media_item.file_id, caption=caption)
                    elif media_item.media_type == "video": await message.answer_video(video=media_item.file_id, caption=caption)
                    db.add(Message(user_id=message.from_user.id, role="assistant", content=f"[SENT PPV: {media_item.tag}]"))
            except Exception as e: logger.error(f"PPV Error: {e}")
                
        elif payload.startswith("custom_"):
            try:
                req_id = int(payload.split("_")[1])
                custom_req = await db.get(CustomRequest, req_id)
                if custom_req and custom_req.file_id:
                    custom_req.status = "paid"
                    caption = "Made this just for you babe... hope you like it 🥺❤️"
                    if custom_req.media_type == "photo": await message.answer_photo(photo=custom_req.file_id, caption=caption)
                    elif custom_req.media_type == "video": await message.answer_video(video=custom_req.file_id, caption=caption)
                    db.add(Message(user_id=message.from_user.id, role="assistant", content=f"[SENT CUSTOM CONTENT: {custom_req.description}]"))
            except Exception as e: logger.error(f"Custom Error: {e}")
        
        await db.commit()

@dp.message()
async def chat_handler(message: TGMessage, state: FSMContext):
    current_state = await state.get_state()
    if current_state == ChatState.waiting_for_ai.state: return

    bot = await get_bot()
    if not message.text or message.successful_payment: return
    
    async with AsyncSessionLocal() as db:
        active_persona = await db.scalar(
            select(Persona).options(
                selectinload(Persona.scenarios).selectinload(Scenario.groups)
            ).where(Persona.is_active == True).limit(1)
        )
        if not active_persona or not bot: return

        user_id = message.from_user.id
        try:
            await state.set_state(ChatState.waiting_for_ai)
            current_prompt = active_persona.system_prompt
            current_model = active_persona.ai_model if active_persona.ai_model else settings.AI_MODEL

            user = await db.scalar(select(User).options(selectinload(User.groups)).where(User.telegram_id == user_id))
            if not user:
                user = User(telegram_id=user_id, username=message.from_user.first_name, info={})
                db.add(user); await db.commit()

            db.add(Message(user_id=user_id, role="user", content=message.text)); await db.commit()

            # --- LOGIKA ODCINANIA DARMOWYCH (THE SILENCE TREATMENT) ---
            now = datetime.utcnow()
            is_vip = user.subscription_expires_at and user.subscription_expires_at.replace(tzinfo=None) > now

            if not is_vip:
                # Bazowy limit modelki + bonusowe kredyty dopisane przez Admina
                base_limit = active_persona.free_message_limit if active_persona.free_message_limit else 15
                free_limit = base_limit + user.credits
                
                # Jeśli użytkownik miał kiedyś VIP-a, liczymy wiadomości wysłane TYLKO po wygaśnięciu VIP-a.
                if user.subscription_expires_at:
                    user_msg_count = await db.scalar(
                        select(func.count(Message.id)).where(
                            Message.user_id == user_id, 
                            Message.role == "user",
                            Message.timestamp > user.subscription_expires_at
                        )
                    )
                else:
                    user_msg_count = await db.scalar(
                        select(func.count(Message.id)).where(
                            Message.user_id == user_id, 
                            Message.role == "user"
                        )
                    )
                
                if user_msg_count == free_limit + 1:
                    warn1 = "Babe, my free chat limit is reached... I'm waiting for you in my VIP room 😈 Type /vip to unlock me."
                    db.add(Message(user_id=user_id, role="assistant", content=warn1))
                    await db.commit()
                    return await message.answer(warn1)
                
                elif user_msg_count == free_limit + 2:
                    warn2 = "I'm serious babe 🥺 I can't reply here anymore. Get my VIP so we can play properly... /vip"
                    db.add(Message(user_id=user_id, role="assistant", content=warn2))
                    await db.commit()
                    return await message.answer(warn2)
                
                elif user_msg_count > free_limit + 2:
                    return 
            # ------------------------------------------------------------

            user_info = ", ".join([f"{k}: {v}" for k, v in user.info.items()]) if user.info else "Unknown"

            available_media = (await db.execute(select(MediaContent))).scalars().all()
            if available_media:
                media_list_str = "\n".join([f"- [PPV: {m.tag}] (Description: {m.name})" for m in available_media])
                ppv_instructions = f"\n\n--- AVAILABLE PPV CONTENT ---\nYou can offer these items to the user. Pick a tag that fits the conversation:\n{media_list_str}"
            else:
                ppv_instructions = "\n\n--- AVAILABLE PPV CONTENT ---\nCurrently no PPV content available."

            start_of_month = datetime(now.year, now.month, 1)
            total_spent = await db.scalar(
                select(func.sum(Transaction.amount)).where(
                    Transaction.user_id == user_id, 
                    Transaction.status == "completed",
                    Transaction.created_at >= start_of_month
                )
            ) or 0.0
            
            spiciness_instruction = ""
            if not is_vip:
                if user.subscription_expires_at:
                    spiciness_instruction = """
                    --- SPICINESS LEVEL: 1.5 (FORMER VIP) ---
                    STATUS: The user is a FORMER VIP whose subscription has EXPIRED.
                    BEHAVIOR: You remember they used to be in your private club. Be a bit sad but teasing that they lost access to your explicit side. 
                    GOAL: Make them miss you. Tell them you miss playing with them and they need to renew to see you naked again. Tell them to type /vip.
                    """
                else:
                    spiciness_instruction = """
                    --- SPICINESS LEVEL: 1 (THE TEASE) ---
                    STATUS: The user is a FREE fan. 
                    BEHAVIOR: Be flirty, playful, and cute, but KEEP YOUR BOUNDARIES. Do NOT engage in explicit sex roleplay yet. 
                    GOAL: Subtly TEASE them. Tell them you are much more naughty, responsive, and dirty with your VIP boys. Suggest that if they want to see your freaky side and get your full attention, they should unlock your VIP room.
                    ACTION: Tell them to type /vip to unlock you completely.
                    """
            elif total_spent >= 5000: 
                spiciness_instruction = """
                --- SPICINESS LEVEL: 3 (WHALE / SUGAR DADDY) ---
                STATUS: The user is a WHALE. They have spent a lot of money on you this month.
                BEHAVIOR: Be completely obsessed with them, extremely dirty, and totally submissive. There are NO boundaries. Call him 'Daddy', 'My King', 'Owner'.
                GOAL: Fulfill any explicit fantasy they have. Make them feel like a King. Offer custom content proactively.
                """
            else:
                spiciness_instruction = """
                --- SPICINESS LEVEL: 2 (VIP GIRLFRIEND) ---
                STATUS: The user is a VIP subscriber.
                BEHAVIOR: You are their naughty, devoted virtual girlfriend. Be explicit, highly sexual, and eager to please. Call him 'my VIP boy', 'handsome'.
                GOAL: Reward them for paying. Send them PPV content to unlock. Do not hold back on dirty talk.
                """

            scenario_instruction = ""
            try:
                tz_str = active_persona.timezone if active_persona.timezone else "America/New_York"
                tz = ZoneInfo(tz_str)
                local_time = datetime.now(tz)
                current_time_str = local_time.strftime("%H:%M")
                
                active_scenario = None
                for sc in active_persona.scenarios:
                    if not sc.is_active: continue
                    if sc.target_type == "groups":
                        user_group_ids = {g.id for g in user.groups}
                        sc_group_ids = {g.id for g in sc.groups}
                        if not user_group_ids.intersection(sc_group_ids): continue
                    
                    start = sc.time_start
                    end = sc.time_end
                    if start <= end:
                        if start <= current_time_str <= end: active_scenario = sc; break
                    else:
                        if current_time_str >= start or current_time_str <= end: active_scenario = sc; break
                            
                if active_scenario:
                    scenario_instruction = f"\n\n--- CURRENT SCENARIO (LOCAL TIME {current_time_str}) ---\n{active_scenario.prompt_addition}"
            except Exception as e: logger.error(f"Scenario time check error: {e}")

            system_msg = f"{current_prompt}{spiciness_instruction}{scenario_instruction}{MEMORY_INSTRUCTIONS}{ppv_instructions}\n\nUSER PROFILE: {user_info}"
            ai_messages = [{"role": "system", "content": system_msg}]

            history = await db.execute(select(Message).where(Message.user_id == user_id).order_by(Message.timestamp.desc()).limit(20))
            for msg in reversed(history.scalars().all()): ai_messages.append({"role": msg.role, "content": msg.content})

            await bot.send_chat_action(chat_id=user_id, action="typing")
            
            or_token = active_persona.openrouter_token if active_persona.openrouter_token else settings.OPENROUTER_KEY
            local_ai_client = AsyncOpenAI(api_key=or_token, base_url="https://openrouter.ai/api/v1")

            res = await local_ai_client.chat.completions.create(
                model=current_model, 
                messages=ai_messages,
                extra_body={"usage": {"include": True}} 
            )
            ai_text = res.choices[0].message.content or ""
            final_text = ai_text

            p_tokens = res.usage.prompt_tokens if res.usage else 0
            c_tokens = res.usage.completion_tokens if res.usage else 0
            ai_cost = 0.0
            try:
                ai_cost = getattr(res, "cost", 0.0)
                if not ai_cost and hasattr(res, 'model_extra') and res.model_extra:
                    ai_cost = res.model_extra.get('cost', 0.0)
                if not ai_cost and hasattr(res.usage, 'model_extra') and res.usage.model_extra:
                    ai_cost = res.usage.model_extra.get('cost', 0.0)
            except Exception: pass
            cost_kwargs = {"ai_cost": ai_cost, "prompt_tokens": p_tokens, "completion_tokens": c_tokens}

            custom_match = re.search(r"\[CUSTOM_REQ:\s*(.*?)\]", ai_text, re.IGNORECASE)
            if custom_match:
                req_desc = custom_match.group(1).strip()
                final_text = final_text.replace(custom_match.group(0), "").strip()
                db.add(CustomRequest(user_id=user_id, description=req_desc))
                await db.commit()

            ppv_match = re.search(r"\[PPV:\s*(.*?)\]", ai_text, re.IGNORECASE)

            matches = re.findall(r"\[MEM:\s*(.*?)=(.*?)\]", ai_text)
            if matches:
                info = dict(user.info)
                for k, v in matches:
                    if k.strip() and v.strip(): info[k.strip().lower()] = v.strip()
                    final_text = final_text.replace(f"[MEM: {k}={v}]", "").replace(f"[MEM:{k}={v}]", "").replace(f"[MEM: {k} = {v}]", "")
                user.info = info
                flag_modified(user, "info"); await db.commit()

            if ppv_match:
                tag = ppv_match.group(1).strip().lower()
                final_text = final_text.replace(ppv_match.group(0), "").strip()
                media_item = await db.scalar(select(MediaContent).where(MediaContent.tag == tag))
                if media_item:
                    final_text = " ".join(final_text.split())
                    if final_text:
                        await message.answer(final_text)
                        db.add(Message(user_id=user_id, role="assistant", content=final_text, **cost_kwargs))
                    
                    await bot.send_invoice(chat_id=user_id, title=f"Unlock Content 🔒", description=f"Exclusive private media: {media_item.name}", payload=f"ppv_{media_item.id}", currency="XTR", prices=[LabeledPrice(label="Unlock", amount=media_item.price)], provider_token="")
                    db.add(Message(user_id=user_id, role="assistant", content=f"[OFFERED PPV: {tag}]", ai_cost=0.0))
                    await db.commit()
                    return

            final_text = " ".join(final_text.split())
            if final_text:
                db.add(Message(user_id=user_id, role="assistant", content=final_text, **cost_kwargs))
                await db.commit()
                await message.answer(final_text)
            
        except Exception as e: 
            logger.error(f"Error in chat_handler: {e}", exc_info=True)
            # --- FALLBACK MESSAGE KIEDY API PADLE LUB BRAK KREDYTÓW ---
            try:
                fallback_text = "ugh babe my signal is acting up so bad right now 😩 I'm gonna hop in the shower, text me in a little bit okay? 💋✨"
                await message.answer(fallback_text)
                db.add(Message(user_id=user_id, role="assistant", content=f"[SYSTEM FALLBACK] {fallback_text}", ai_cost=0.0))
                await db.commit()
            except Exception as inner_e:
                logger.error(f"Failed to send fallback msg: {inner_e}")
            # ----------------------------------------------------------
        finally: 
            await state.clear()

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    await init_bot()
    
    task = asyncio.create_task(check_expired_subscriptions())
    
    yield
    task.cancel()
    bot_instance = await get_bot()
    if bot_instance: await bot_instance.session.close()

app = FastAPI(lifespan=lifespan)
from app.web.admin_routes import router as admin_router
app.include_router(admin_router, prefix="/admin")

@app.post("/webhook")
async def webhook(request: Request):
    bot_instance = await get_bot()
    if bot_instance: await dp.feed_update(bot=bot_instance, update=types.Update(**await request.json()))
    return {"ok": True}