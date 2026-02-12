import logging
import secrets
import os
from fastapi import APIRouter, Depends, HTTPException, status, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from app.database.models import User, Message

logger = logging.getLogger(__name__)
router = APIRouter()
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")

# We import get_db here to avoid circular imports during startup
from app.main import get_db, settings

def auth(credentials: HTTPBasicCredentials = Depends(security)):
    is_user = secrets.compare_digest(credentials.username, settings.ADMIN_USER)
    is_pass = secrets.compare_digest(credentials.password, settings.ADMIN_PASS)
    if not (is_user and is_pass):
        logger.warning(f"Unauthorized access attempt by: {credentials.username}")
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    try:
        total = await db.scalar(select(func.count(User.telegram_id)))
        users = (await db.execute(select(User).order_by(desc(User.created_at)).limit(10))).scalars().all()
        return templates.TemplateResponse("dashboard.html", {"request": request, "total_users": total, "recent_users": users, "username": user})
    except Exception as e:
        logger.error(f"Dashboard error: {e}", exc_info=True)
        return HTMLResponse("Error", status_code=500)

@router.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat_view(request: Request, user_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    c_user = await db.get(User, user_id)
    msgs = (await db.execute(select(Message).where(Message.user_id==user_id).order_by(Message.timestamp))).scalars().all()
    return templates.TemplateResponse("chat_viewer.html", {"request": request, "chat_user": c_user, "messages": msgs, "username": user})

@router.post("/users/{user_id}/add_credits")
async def add_credits(user_id: int, amount: int = Form(...), db: AsyncSession = Depends(get_db), user=Depends(auth)):
    u = await db.get(User, user_id)
    if u:
        u.credits += amount
        await db.commit()
        logger.info(f"Admin {user} added {amount} credits to {user_id}")
    return HTMLResponse("Success. <a href='/admin/'>Back</a>")