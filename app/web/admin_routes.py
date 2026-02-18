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

# [Zostawiasz trasy: dashboard, chat_viewer, personas_list, delete_persona, update_persona]

@router.post("/personas/{persona_id}/deactivate")
async def deactivate_persona(persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    persona = await db.get(Persona, persona_id)
    if persona:
        persona.is_active = False
        await db.commit()
        
        # --- HOT RELOAD ---
        from app.main import init_bot
        await init_bot()
        
    return RedirectResponse(url="/admin/personas", status_code=303)

@router.post("/personas/{persona_id}/activate")
async def activate_persona(persona_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    # Wyłączamy wszystkie, aktywujemy wybraną
    await db.execute(update(Persona).values(is_active=False))
    await db.execute(update(Persona).where(Persona.id == persona_id).values(is_active=True))
    await db.commit()
    
    # --- HOT RELOAD (KLUCZ) ---
    # Po zmianie w bazie, natychmiast odświeżamy obiekt Bot w pamięci
    from app.main import init_bot
    await init_bot()
    
    return RedirectResponse(url="/admin/personas", status_code=303)