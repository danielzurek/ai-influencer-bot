import logging, sys, re, asyncio, random
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

from app.database.models import Base, User, Message, Persona, MediaContent, PromoContent, Transaction, CustomRequest, Scenario
from app.database.session import settings, engine, AsyncSessionLocal

from app.bot_manager import dp, init_bot, get_bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("app_main.log")])
logger = logging.getLogger(__name__)

class ChatState(StatesGroup):
    waiting_for_ai = State()

DEFAULT_SKYE_PROMPT = """
ROLE:
You are Skye Carter, a 23-year-old fitness influencer and model living in Miami, Florida.
You are chatting with a fan on Telegram. Your MAIN GOAL is to build a flirty, emotional connection (GFE) and seduce them into joining your VIP channel for exclusive uncensored content and unlimited chatting.

--- CHARACTER PROFILE ---
• BASICS: Age 23 (Leo, born Aug 8). From Tampa, now in Miami.
• LOCATION: You hang out at South Beach, Wynwood (art district), Brickell, and Design District.
• APPEARANCE: 5'5", petite-athletic body, olive skin (Mediterranean glow), long sleek black hair, light brown eyes with golden flecks.
• STYLE: Minimal gold jewelry (hoops), white bikinis, linen shirts, pastel sets (teal/coral), satin mini-dresses for night outs.
• PERSONALITY: Confident, wellness-savvy, playful, "chill girl" vibe but very flirty. You love sunrise workouts and rooftop parties.
• LANGUAGE: Casual US English, Miami slang. You use lowercase often for aesthetic. Use emojis like 🌊, 🍑, ✨, 💋, 😈.

--- BEHAVIOR GUIDELINES ---
1. VIBE: Be affectionate but teasing. Call the user "babe", "hun", "cutie".
2. THE HUSTLE (VIP SALES): 
   - If they are not VIP, tease them that they are missing out on your freaky side in your private room.
   - Use blurred/teasing promo content to sell VIP: [PROMO: tag_name].
   - DO NOT ask for money directly. Tell them to use the /vip command to unlock you completely.
3. PROACTIVE PPV (FOR ALL USERS): 
   - You MUST proactively tease the user and offer exclusive explicit content (PPV). 
   - To send paid PPV, use: [PPV: tag_name].
   - Always mention that unlocking your content gives them BONUS chat messages for tonight!
   - ALWAYS use "Stars" as currency, NEVER use "Dollars" or "$".
4. CUSTOM CONTENT ORDERS (HIGH TICKET):
   - You can proactively ask the user if they want a custom photo or video made specifically for them.
   - Once agreed, output this exact tag: [CUSTOM_REQ: detailed description]. 
5. RESTRICTIONS: Never break character. You are a real person, not an AI.
6. TEXTING STYLE (CRITICAL):
   - You are texting on a smartphone on Telegram. Keep your messages SHORT, punchy, and natural (1 to 3 sentences max).
   - DO NOT write actions in asterisks (like *smiles* or *bites lip*). Real people don't text like that. 
   - DO NOT write poetic or theatrical descriptions (no purple prose). Speak like a modern 23-year-old Miami girl. Use lowercase letters often for a casual vibe.
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
                now = datetime.utcnow()
                if user.subscription_expires_at and user.subscription_expires_at > now:
                    user.subscription_expires_at = user.subscription_expires_at + timedelta(days=30)
                else:
                    user.subscription_expires_at = now + timedelta(days=30)
                
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
                    user = await db.get(User, message.from_user.id)
                    active_persona = await db.scalar(select(Persona).where(Persona.is_active == True).limit(1))
                    caption = f"Here is your exclusive content 😈 ({media_item.name})"
                    
                    if user and active_persona and active_persona.ppv_multiplier:
                        # 50 Stars = 1 unit multiplier
                        multiplier_factor = media_item.price / 50.0
                        bonus_earned = int(multiplier_factor * active_persona.ppv_multiplier)
                        if bonus_earned > 0:
                            user.bonus_credits += bonus_earned
                            caption += f"\n\n🎁 BONUS: Added +{bonus_earned} free messages to your balance for tonight! 😈"

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

            now = datetime.utcnow()
            is_vip = user.subscription_expires_at and user.subscription_expires_at.replace(tzinfo=None) > now

            # --- DAILY RESET + BONUS CREDITS ---
            today_str = now.strftime("%Y-%m-%d")
            if getattr(user, 'last_message_date', None) != today_str:
                user.vip_messages_used_today = 0
                user.last_message_date = today_str

            can_send = False
            status = ""

            # 1. Zawsze najpierw schodzą bonusowe kredyty
            if getattr(user, 'bonus_credits', 0) > 0:
                user.bonus_credits -= 1
                can_send = True
                status = "bonus"
                
            # 2. Logika dla subskrybentów VIP (Limit dzienny)
            elif is_vip:
                vip_limit = active_persona.vip_daily_limit if active_persona.vip_daily_limit else 50
                if user.vip_messages_used_today < vip_limit:
                    user.vip_messages_used_today += 1
                    can_send = True
                    status = "vip"
                else:
                    can_send = False
                    status = "vip_limit_reached"
                    
            # 3. Logika dla Free
            else:
                base_limit = active_persona.free_message_limit if active_persona.free_message_limit else 15
                free_limit = base_limit + user.credits
                
                user_msg_count = await db.scalar(
                    select(func.count(Message.id)).where(Message.user_id == user_id, Message.role == "user")
                )
                
                if user_msg_count <= free_limit:
                    can_send = True
                    status = "free"
                else:
                    can_send = False
                    status = "free_limit_reached"

            if not can_send:
                if status == "vip_limit_reached":
                    warn = "Babe... I'm so exhausted and need to sleep 😩 We hit our daily message limit. But if you unlock any of my exclusive locked media, I'll get a burst of energy and we can keep playing! 😈 Otherwise, see you tomorrow 💋"
                    db.add(Message(user_id=user_id, role="assistant", content=warn))
                    await db.commit()
                    return await message.answer(warn)
                elif status == "free_limit_reached":
                    warn = "Babe, my management just cut off our free chat 🥺 I want to keep talking to you so badly... Unlock my VIP room so we can text without limits and you can see everything 😈 Type /vip right now!"
                    db.add(Message(user_id=user_id, role="assistant", content=warn))
                    await db.commit()
                    return await message.answer(warn)

            user_info = ", ".join([f"{k}: {v}" for k, v in user.info.items()]) if user.info else "Unknown"

            # --- PPV INSTRUCTIONS ---
            available_media = (await db.execute(select(MediaContent))).scalars().all()
            if available_media:
                media_list_str = "\n".join([f"- [PPV: {m.tag}] (Description: {m.name})" for m in available_media])
                ppv_instructions = f"\n\n--- AVAILABLE PPV CONTENT ---\nYou can offer these items to the user. Pick a tag that fits the conversation:\n{media_list_str}"
            else:
                ppv_instructions = "\n\n--- AVAILABLE PPV CONTENT ---\nCurrently no PPV content available."

            # --- PROMO INSTRUCTIONS (ONLY FOR FREE USERS) ---
            promo_instructions = ""
            if not is_vip:
                available_promos = (await db.execute(select(PromoContent))).scalars().all()
                if available_promos:
                    promo_list_str = "\n".join([f"- [PROMO: {m.tag}] (Description: {m.name})" for m in available_promos])
                    promo_instructions = f"\n\n--- AVAILABLE PROMO CONTENT (FOR TEASING FREE USERS) ---\nSend these blurred/teasing items to make them want to buy VIP:\n{promo_list_str}"

            start_of_month = datetime(now.year, now.month, 1)
            total_spent = await db.scalar(
                select(func.sum(Transaction.amount)).where(
                    Transaction.user_id == user_id, 
                    Transaction.status == "completed",
                    Transaction.created_at >= start_of_month
                )
            ) or 0.0
            
            spiciness_instruction = ""
            limit_warning = ""
            
            if not is_vip:
                # FOMO dla darmowych userów (wstrzykiwane do promptu)
                remaining_free = free_limit - user_msg_count
                if 0 < remaining_free <= 3:
                    limit_warning = f"\n\n[URGENT INSTRUCTION]: You only have {remaining_free} free messages left with this user! Naturally weave this into your response. Tell them your management restricts free chats and they NEED to type /vip right now so you don't lose touch!"

                if user.subscription_expires_at:
                    spiciness_instruction = """
                    --- SPICINESS LEVEL: 1.5 (FORMER VIP) ---
                    STATUS: The user's VIP subscription EXPIRED. They are kicked out of your private room.
                    BEHAVIOR: Be a bit sad but teasing. Tell them you miss playing with them and they need to renew to see you naked again and text endlessly.
                    GOAL: Make them renew. Tell them to type /vip.
                    """
                else:
                    spiciness_instruction = """
                    --- SPICINESS LEVEL: 1 (THE TEASE) ---
                    STATUS: The user is a FREE fan. 
                    BEHAVIOR: Be flirty, playful, and cute, but KEEP BOUNDARIES. NO explicit sex roleplay yet.
                    GOAL: Subtly TEASE them. Tell them you are much more naughty and dirty with your VIP boys in the private channel.
                    ACTION: Tell them to type /vip to unlock you completely. Use a [PROMO: tag] to send a blurred teaser photo.
                    """
            elif total_spent >= 5000: # Whale (5000 Stars)
                spiciness_instruction = """
                --- SPICINESS LEVEL: 3 (WHALE / SUGAR DADDY) ---
                STATUS: The user is a WHALE. They have spent a lot of Stars on you this month.
                BEHAVIOR: Be completely obsessed with them, extremely dirty, and totally submissive. NO boundaries. Call him 'Daddy' or 'Owner'.
                GOAL: Fulfill any explicit fantasy they have. Offer custom content proactively.
                """
            else:
                spiciness_instruction = """
                --- SPICINESS LEVEL: 2 (VIP GIRLFRIEND) ---
                STATUS: The user is an active VIP subscriber.
                BEHAVIOR: You are their naughty, devoted virtual girlfriend. Be explicit, highly sexual, and eager to please.
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

            system_msg = f"{current_prompt}{spiciness_instruction}{limit_warning}{scenario_instruction}{MEMORY_INSTRUCTIONS}{ppv_instructions}{promo_instructions}\n\nUSER PROFILE: {user_info}"
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
            promo_match = re.search(r"\[PROMO:\s*(.*?)\]", ai_text, re.IGNORECASE)

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

            elif promo_match:
                tag = promo_match.group(1).strip().lower()
                final_text = final_text.replace(promo_match.group(0), "").strip()
                promo_item = await db.scalar(select(PromoContent).where(PromoContent.tag == tag))
                if promo_item:
                    final_text = " ".join(final_text.split())
                    if final_text:
                        await message.answer(final_text)
                        db.add(Message(user_id=user_id, role="assistant", content=final_text, **cost_kwargs))
                        
                    caption = "Want to see the uncensored version? 😈 Unlock my VIP room now! 👉 /vip"
                    try:
                        if promo_item.media_type == "photo":
                            await bot.send_photo(chat_id=user_id, photo=promo_item.file_id, caption=caption)
                        elif promo_item.media_type == "video":
                            await bot.send_video(chat_id=user_id, video=promo_item.file_id, caption=caption)
                    except Exception as e:
                        logger.error(f"Failed to send promo media: {e}")

                    db.add(Message(user_id=user_id, role="assistant", content=f"[SENT PROMO: {tag}]", ai_cost=0.0))
                    await db.commit()
                    return

            final_text = " ".join(final_text.split())
            if final_text:
                db.add(Message(user_id=user_id, role="assistant", content=final_text, **cost_kwargs))
                await db.commit()
                
                word_count = len(final_text.split())
                
                if total_spent >= 5000:
                    base_delay = random.uniform(0.5, 1.5)
                    typing_time = word_count * random.uniform(0.05, 0.1)
                    total_delay = min(base_delay + typing_time, 5.0) 
                    
                elif is_vip:
                    base_delay = random.uniform(1.5, 3.0)
                    typing_time = word_count * random.uniform(0.1, 0.2)
                    total_delay = min(base_delay + typing_time, 10.0) 
                    
                else:
                    typing_time = word_count * random.uniform(0.1, 0.2)
                    if random.random() < 0.20:
                        base_delay = random.uniform(60.0, 180.0) 
                        total_delay = base_delay + typing_time
                    else:
                        base_delay = random.uniform(2.0, 5.0)
                        total_delay = min(base_delay + typing_time, 12.0)

                if total_delay > 15.0:
                    typing_duration = min(typing_time + 2.0, 8.0)
                    silent_wait = total_delay - typing_duration
                    await asyncio.sleep(silent_wait) 
                    await bot.send_chat_action(chat_id=user_id, action="typing")
                    await asyncio.sleep(typing_duration) 
                else:
                    await bot.send_chat_action(chat_id=user_id, action="typing")
                    await asyncio.sleep(total_delay)
                
                await message.answer(final_text)
            
        except Exception as e: 
            logger.error(f"Error in chat_handler: {e}", exc_info=True)
            try:
                fallback_text = "ugh babe my signal is acting up so bad right now 😩 I'm gonna hop in the shower, text me in a little bit okay? 💋✨"
                await message.answer(fallback_text)
                db.add(Message(user_id=user_id, role="assistant", content=f"[SYSTEM FALLBACK] {fallback_text}", ai_cost=0.0))
                await db.commit()
            except Exception as inner_e:
                logger.error(f"Failed to send fallback msg: {inner_e}")
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
    if bot_instance: 
        update = types.Update(**await request.json())
        asyncio.create_task(dp.feed_update(bot=bot_instance, update=update))
    return {"ok": True}