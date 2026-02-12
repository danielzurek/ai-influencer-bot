import logging
import secrets
from fastapi import APIRouter, Depends, HTTPException, status, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.database.models import User, Message
from app.database.session import get_db, settings #

logger = logging.getLogger(__name__)
router = APIRouter()
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    is_correct_username = secrets.compare_digest(credentials.username, settings.ADMIN_USER)
    is_correct_password = secrets.compare_digest(credentials.password, settings.ADMIN_PASS)
    if not (is_correct_username and is_correct_password):
        logger.warning(f"Unauthorized access attempt: {credentials.username}")
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials.username

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    try:
        total = await db.scalar(select(func.count(User.telegram_id)))
        vip = await db.scalar(select(func.count(User.telegram_id)).where(User.is_vip == True))
        users = (await db.execute(select(User).order_by(desc(User.created_at)).limit(10))).scalars().all()
        return templates.TemplateResponse("dashboard.html", {"request": request, "total_users": total, "vip_users": vip, "recent_users": users, "username": username})
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return HTMLResponse("Error loading dashboard", status_code=500)

@router.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat_viewer(request: Request, user_id: int, db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    user = await db.get(User, user_id)
    msgs = (await db.execute(select(Message).where(Message.user_id == user_id).order_by(Message.timestamp))).scalars().all()
    return templates.TemplateResponse("chat_viewer.html", {"request": request, "chat_user": user, "messages": msgs, "username": username})

@router.post("/users/{user_id}/add_credits")
async def add_credits(user_id: int, amount: int = Form(...), db: AsyncSession = Depends(get_db), username: str = Depends(get_current_username)):
    user = await db.get(User, user_id)
    if user:
        user.credits += amount
        await db.commit()
        logger.info(f"Admin {username} added {amount} credits to {user_id}")
    return HTMLResponse(f"Success. <a href='/admin/'>Back</a>")