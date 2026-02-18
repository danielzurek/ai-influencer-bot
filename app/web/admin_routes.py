import secrets
import asyncio
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, update
from sqlalchemy.orm import selectinload

from app.database.models import User, Message, Persona, Group, Broadcast, BroadcastLog
from app.database.session import get_db, settings, AsyncSessionLocal # WAŻNE: AsyncSessionLocal do tasków w tle
from app.bot_manager import init_bot, get_bot

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")

def auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, settings.ADMIN_USER) and 
            secrets.compare_digest(credentials.password, settings.ADMIN_PASS)):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# ... (DASHBOARD, CHAT, PERSONAS - BEZ ZMIAN - SKOPIUJ JE ZE STAREGO PLIKU JEŚLI POTRZEBA) ...
# Poniżej wklejam tylko fragmenty dashboardu dla kontekstu, reszta bez zmian.

@router.get("/", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    users = (await db.execute(select(User).order_by(desc(User.created_at)))).scalars().all()
    vip_users = len([u for u in users if u.is_vip])
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "total_users": len(users), "vip_users": vip_users, "recent_users": users[:15], "username": user
    })

@router.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat_viewer(request: Request, user_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    chat_user = await db.get(User, user_id)
    if not chat_user: raise HTTPException(status_code=404)
    msgs = (await db.execute(select(Message).where(Message.user_id == user_id).order_by(Message.timestamp))).scalars().all()
    return templates.TemplateResponse("chat_viewer.html", {"request": request, "chat_user": chat_user, "messages": msgs, "username": user})

# ... (PERSONAS, GROUPS CRUD - BEZ ZMIAN) ...
# Wklejam sekcję Groups dla pewności, ale logika się nie zmieniła.

@router.get("/groups", response_class=HTMLResponse)
async def list_groups(request: Request, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    groups = (await db.execute(select(Group).options(selectinload(Group.users)))).scalars().all()
    return templates.TemplateResponse("groups.html", {"request": request, "groups": groups, "username": user})

@router.post("/groups/create")
async def create_group(name: str = Form(...), description: str = Form(None), db: AsyncSession = Depends(get_db), user=Depends(auth)):
    db.add(Group(name=name, description=description))
    await db.commit()
    return RedirectResponse(url="/admin/groups", status_code=303)

@router.get("/groups/{group_id}", response_class=HTMLResponse)
async def edit_group(request: Request, group_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    group = await db.get(Group, group_id, options=[selectinload(Group.users)])
    if not group: raise HTTPException(status_code=404)
    all_users = (await db.execute(select(User).order_by(User.username))).scalars().all()
    return templates.TemplateResponse("group_edit.html", {
        "request": request, "group": group, "all_users": all_users, "username": user
    })

@router.post("/groups/{group_id}/update")
async def update_group(group_id: int, name: str = Form(...), description: str = Form(None), db: AsyncSession = Depends(get_db), user=Depends(auth)):
    group = await db.get(Group, group_id)
    if group:
        group.name = name
        group.description = description
        await db.commit()
    return RedirectResponse(url="/admin/groups", status_code=303)

@router.post("/groups/{group_id}/delete")
async def delete_group(group_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    group = await db.get(Group, group_id)
    if group:
        await db.delete(group)
        await db.commit()
    return RedirectResponse(url="/admin/groups", status_code=303)

@router.post("/groups/{group_id}/add_user")
async def add_user_to_group(group_id: int, user_id: int = Form(...), db: AsyncSession = Depends(get_db), user=Depends(auth)):
    group = await db.get(Group, group_id, options=[selectinload(Group.users)])
    user_obj = await db.get(User, user_id)
    if group and user_obj and user_obj not in group.users:
        group.users.append(user_obj)
        await db.commit()
    return RedirectResponse(url=f"/admin/groups/{group_id}", status_code=303)

@router.post("/groups/{group_id}/remove_user")
async def remove_user_from_group(group_id: int, user_id: int = Form(...), db: AsyncSession = Depends(get_db), user=Depends(auth)):
    group = await db.get(Group, group_id, options=[selectinload(Group.users)])
    user_obj = await db.get(User, user_id)
    if group and user_obj and user_obj in group.users:
        group.users.remove(user_obj)
        await db.commit()
    return RedirectResponse(url=f"/admin/groups/{group_id}", status_code=303)

# ... (PERSONAS LOGIC - PROSZĘ ZOSTAWIĆ JAK BYŁO) ...
# Poniżej placeholder dla Personas, aby kod był kompletny przy copy-paste jeśli potrzebujesz, 
# ale najlepiej po prostu zostaw starą sekcję Personas.
@router.get("/personas", response_class=HTMLResponse)
async def personas_list(request: Request, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    result = await db.execute(select(Persona).order_by(Persona.id))
    personas = result.scalars().all()
    msg_count = await db.scalar(select(func.count(Message.id)))
    for p in personas:
        p.stats_msgs = msg_count if p.is_active else 0
        p.stats_cost = round(p.stats_msgs * 0.002, 2)
    return templates.TemplateResponse("personas.html", {"request": request, "personas": personas, "username": user})
# (Reszta endpointów personas create/edit/delete/activate bez zmian...)
@router.post("/personas/create")
async def create_persona(name: str = Form(...), system_prompt: str = Form(...), telegram_token: str = Form(None), ai_model: str = Form("openrouter/free"), db: AsyncSession = Depends(get_db), user=Depends(auth)):
    token = telegram_token.strip() if telegram_token and telegram_token.strip() else None
    db.add(Persona(name=name, system_prompt=system_prompt, telegram_token=token, ai_model=ai_model, is_active=False))
    await db.commit()
    return RedirectResponse(url="/admin/personas", status_code=303)

@router.get("/personas/{persona_id}", response_class=HTMLResponse)
async def edit_persona_page(request: Request, persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    persona = await db.get(Persona, persona_id)
    if not persona: raise HTTPException(status_code=404)
    return templates.TemplateResponse("edit_persona.html", {"request": request, "persona": persona, "username": user})

@router.post("/personas/{persona_id}/update")
async def update_persona(persona_id: int, name: str = Form(...), system_prompt: str = Form(...), telegram_token: str = Form(None), ai_model: str = Form(...), db: AsyncSession = Depends(get_db), user=Depends(auth)):
    persona = await db.get(Persona, persona_id)
    if persona:
        persona.name = name
        persona.system_prompt = system_prompt
        persona.ai_model = ai_model
        persona.telegram_token = telegram_token.strip() if telegram_token and telegram_token.strip() else None
        await db.commit()
        if persona.is_active: 
            from app.bot_manager import init_bot
            await init_bot()
    return RedirectResponse(url="/admin/personas", status_code=303)

@router.post("/personas/{persona_id}/activate")
async def activate_persona(persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    await db.execute(update(Persona).values(is_active=False))
    await db.execute(update(Persona).where(Persona.id == persona_id).values(is_active=True))
    await db.commit()
    from app.bot_manager import init_bot
    await init_bot()
    return RedirectResponse(url="/admin/personas", status_code=303)

@router.post("/personas/{persona_id}/deactivate")
async def deactivate_persona(persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    persona = await db.get(Persona, persona_id)
    if persona:
        persona.is_active = False
        await db.commit()
        from app.bot_manager import init_bot
        await init_bot()
    return RedirectResponse(url="/admin/personas", status_code=303)

@router.post("/personas/{persona_id}/delete")
async def delete_persona(persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    persona = await db.get(Persona, persona_id)
    if persona:
        if persona.is_active:
            persona.is_active = False; await db.commit()
            from app.bot_manager import init_bot
            await init_bot()
        await db.delete(persona)
        await db.commit()
    return RedirectResponse(url="/admin/personas", status_code=303)


# --- NOWY SYSTEM BROADCAST (Z HISTORIĄ I LOGAMI) ---

@router.get("/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    groups = (await db.execute(select(Group))).scalars().all()
    # Pobierz historię ostatnich 10 broadcastów
    history = (await db.execute(select(Broadcast).order_by(desc(Broadcast.created_at)).limit(10))).scalars().all()
    return templates.TemplateResponse("broadcast.html", {
        "request": request, "groups": groups, "history": history, "username": user
    })

@router.get("/broadcast/{broadcast_id}", response_class=HTMLResponse)
async def broadcast_details(request: Request, broadcast_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    # Pobierz szczegóły broadcastu
    broadcast = await db.get(Broadcast, broadcast_id)
    if not broadcast: raise HTTPException(status_code=404)
    
    # Pobierz logi (kto dostał, kto nie)
    logs = (await db.execute(
        select(BroadcastLog).options(selectinload(BroadcastLog.user))
        .where(BroadcastLog.broadcast_id == broadcast_id)
        .order_by(BroadcastLog.status) # Failed first usually interesting
    )).scalars().all()
    
    return templates.TemplateResponse("broadcast_details.html", {
        "request": request, "broadcast": broadcast, "logs": logs, "username": user
    })

async def background_send_task(broadcast_id: int, user_ids: List[int]):
    """
    Logika działająca w tle:
    1. Otwiera nową sesję DB.
    2. Wysyła wiadomości.
    3. Zapisuje wynik (Success/Fail) dla każdego usera w BroadcastLog.
    4. Aktualizuje liczniki w tabeli Broadcast.
    """
    bot = await get_bot()
    if not bot:
        logger.error("Broadcast failed: Bot not active.")
        return

    # Otwieramy nową sesję, bo ta z requesta już wygasła
    async with AsyncSessionLocal() as db:
        broadcast = await db.get(Broadcast, broadcast_id)
        if not broadcast: return
        
        success_count = 0
        fail_count = 0
        
        for uid in user_ids:
            try:
                # Próba wysyłki
                await bot.send_message(chat_id=uid, text=broadcast.message_content)
                
                # Sukces
                success_count += 1
                db.add(BroadcastLog(broadcast_id=broadcast.id, user_id=uid, status="sent"))
                
                await asyncio.sleep(0.05) # Rate limiting
            except Exception as e:
                # Błąd (np. bot zablokowany)
                fail_count += 1
                error_msg = str(e)[:250]
                db.add(BroadcastLog(broadcast_id=broadcast.id, user_id=uid, status="failed", error_message=error_msg))
        
        # Aktualizacja statusu końcowego kampanii
        broadcast.status = "completed"
        broadcast.success_count = success_count
        broadcast.fail_count = fail_count
        await db.commit()
        logger.info(f"Broadcast {broadcast_id} finished. Success: {success_count}, Fail: {fail_count}")

@router.post("/broadcast/send")
async def send_broadcast(
    request: Request,
    background_tasks: BackgroundTasks,
    target_type: str = Form(...),
    group_ids: List[int] = Form(default=[]),
    message_text: str = Form(...),
    db: AsyncSession = Depends(get_db), 
    user=Depends(auth)
):
    # 1. Ustalanie listy odbiorców
    target_users_ids = set()
    if target_type == "all":
        result = await db.execute(select(User.telegram_id))
        target_users_ids = set(result.scalars().all())
    elif target_type == "groups" and group_ids:
        for gid in group_ids:
            group = await db.get(Group, gid, options=[selectinload(Group.users)])
            if group:
                for u in group.users:
                    target_users_ids.add(u.telegram_id)
    
    if not target_users_ids:
        return HTMLResponse("No users selected for broadcast.", status_code=400)

    # 2. Tworzenie rekordu Broadcast w bazie (Status: Processing)
    new_broadcast = Broadcast(
        message_content=message_text,
        target_type=target_type,
        total_recipients=len(target_users_ids),
        status="processing"
    )
    db.add(new_broadcast)
    await db.commit()
    await db.refresh(new_broadcast)
    
    # 3. Uruchomienie zadania w tle (przekazujemy tylko ID, nie obiekty)
    background_tasks.add_task(background_send_task, new_broadcast.id, list(target_users_ids))
    
    # 4. Przekierowanie do szczegółów kampanii
    return RedirectResponse(url=f"/admin/broadcast/{new_broadcast.id}", status_code=303)