import secrets
from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, update
from app.database.models import User, Message, Persona
from app.database.session import get_db, settings 

router = APIRouter()
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")

def auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, settings.ADMIN_USER) and 
            secrets.compare_digest(credentials.password, settings.ADMIN_PASS)):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username

@router.get("/", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    users = (await db.execute(select(User).order_by(desc(User.created_at)))).scalars().all()
    vip_users = len([u for u in users if u.is_vip])
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "total_users": len(users), "vip_users": vip_users, "recent_users": users, "username": user
    })

@router.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat_viewer(request: Request, user_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    chat_user = await db.get(User, user_id)
    msgs = (await db.execute(select(Message).where(Message.user_id == user_id).order_by(Message.timestamp))).scalars().all()
    return templates.TemplateResponse("chat_viewer.html", {"request": request, "chat_user": chat_user, "messages": msgs, "username": user})

# --- MODELS Z DODANYMI STATYSTYKAMI ---
@router.get("/personas", response_class=HTMLResponse)
async def personas_list(request: Request, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    result = await db.execute(select(Persona).order_by(Persona.id))
    personas = result.scalars().all()
    
    # Prosta statystyka wiadomości (globalna dla demonstracji)
    msg_count = await db.scalar(select(func.count(Message.id)))
    
    # Dodajemy wirtualne statystyki do obiektów (możesz to później rozbudować o kolumny w DB)
    for p in personas:
        p.stats_msgs = msg_count if p.is_active else 0 # Uproszczenie
        p.stats_cost = round(p.stats_msgs * 0.002, 2) # Szacunkowy koszt 0.002$ za wiadomość
        
    return templates.TemplateResponse("personas.html", {"request": request, "personas": personas, "username": user})
# --- USUWANIE MODELKI ---
@router.post("/personas/{persona_id}/delete")
async def delete_persona(persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    persona = await db.get(Persona, persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    
    # Zabezpieczenie przed usunięciem aktywnej modelki
    if persona.is_active:
        return RedirectResponse(url="/admin/personas?error=active", status_code=303)
        
    await db.delete(persona)
    await db.commit()
    return RedirectResponse(url="/admin/personas", status_code=303)

# --- DEAKTYWACJA MODELKI ---
@router.post("/personas/{persona_id}/deactivate")
async def deactivate_persona(persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    persona = await db.get(Persona, persona_id)
    if persona:
        persona.is_active = False
        await db.commit()
    return RedirectResponse(url="/admin/personas", status_code=303)

@router.post("/personas/create")
async def create_persona(
    name: str = Form(...), 
    system_prompt: str = Form(...), 
    telegram_token: str = Form(None),
    ai_model: str = Form("openrouter/free"),
    db: AsyncSession = Depends(get_db), 
    user=Depends(auth)
):
    token = telegram_token if telegram_token and telegram_token.strip() else None
    db.add(Persona(name=name, system_prompt=system_prompt, telegram_token=token, ai_model=ai_model, is_active=False))
    await db.commit()
    return RedirectResponse(url="/admin/personas", status_code=303)

@router.get("/personas/{persona_id}", response_class=HTMLResponse)
async def edit_persona_page(request: Request, persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    persona = await db.get(Persona, persona_id)
    if not persona: raise HTTPException(status_code=404)
    return templates.TemplateResponse("edit_persona.html", {"request": request, "persona": persona, "username": user})

@router.post("/personas/{persona_id}/update")
async def update_persona(
    persona_id: int, name: str = Form(...), system_prompt: str = Form(...), 
    telegram_token: str = Form(None), ai_model: str = Form(...),
    db: AsyncSession = Depends(get_db), user=Depends(auth)
):
    persona = await db.get(Persona, persona_id)
    if persona:
        persona.name = name
        persona.system_prompt = system_prompt
        persona.ai_model = ai_model
        persona.telegram_token = telegram_token if telegram_token and telegram_token.strip() else None
        await db.commit()
    return RedirectResponse(url="/admin/personas", status_code=303)

@router.post("/personas/{persona_id}/activate")
async def activate_persona(persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    await db.execute(update(Persona).values(is_active=False))
    await db.execute(update(Persona).where(Persona.id == persona_id).values(is_active=True))
    await db.commit()
    return RedirectResponse(url="/admin/personas", status_code=303)