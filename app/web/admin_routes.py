import logging
import secrets
import os
from fastapi import APIRouter, Depends, HTTPException, status, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.database.models import User, Message, Transaction
# Zakładamy, że importy działają (w Dockerze będą ok)
from app.database.models import Base 
# Dependency Hack (w produkcji lepiej mieć oddzielny plik dependencies.py)
from app.main import get_db 

# --- 🛠️ SETUP LOGGERA ---
# Pobieramy loggera skonfigurowanego w main.py
logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")

# --- Auth Configuration ---
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "secret")

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    current_username_bytes = credentials.username.encode("utf8")
    correct_username_bytes = ADMIN_USER.encode("utf8")
    is_correct_username = secrets.compare_digest(current_username_bytes, correct_username_bytes)

    current_password_bytes = credentials.password.encode("utf8")
    correct_password_bytes = ADMIN_PASS.encode("utf8")
    is_correct_password = secrets.compare_digest(current_password_bytes, correct_password_bytes)

    if not (is_correct_username and is_correct_password):
        # 🚨 SECURITY LOG
        logger.warning(f"⛔ Failed Admin Login Attempt: {credentials.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, 
    username: str = Depends(get_current_username),
    db: AsyncSession = Depends(get_db)
):
    try:
        # KPIs
        total_users = await db.scalar(select(func.count(User.telegram_id)))
        vip_users = await db.scalar(select(func.count(User.telegram_id)).where(User.is_vip == True))
        
        # Recent Users
        result = await db.execute(select(User).order_by(desc(User.created_at)).limit(10))
        recent_users = result.scalars().all()

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "username": username,
            "total_users": total_users,
            "vip_users": vip_users,
            "recent_users": recent_users
        })
    except Exception as e:
        # 🐛 ERROR LOG
        logger.error(f"Error rendering Dashboard: {e}", exc_info=True)
        return HTMLResponse("<h1>500 Internal Server Error</h1><p>Check logs.</p>", status_code=500)

@router.get("/users", response_class=HTMLResponse)
async def user_list(
    request: Request, 
    username: str = Depends(get_current_username),
    db: AsyncSession = Depends(get_db)
):
    try:
        result = await db.execute(select(User).order_by(desc(User.created_at)).limit(100))
        users = result.scalars().all()
        return templates.TemplateResponse("users.html", {"request": request, "users": users})
    except Exception as e:
        logger.error(f"Error rendering User List: {e}", exc_info=True)
        return HTMLResponse("Error loading users", status_code=500)

@router.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat_viewer(
    request: Request, 
    user_id: int,
    username: str = Depends(get_current_username),
    db: AsyncSession = Depends(get_db)
):
    try:
        user = await db.get(User, user_id)
        if not user:
            return HTMLResponse("User not found", status_code=404)
            
        result = await db.execute(
            select(Message).where(Message.user_id == user_id).order_by(Message.timestamp)
        )
        messages = result.scalars().all()
        
        return templates.TemplateResponse("chat_viewer.html", {
            "request": request, 
            "chat_user": user, 
            "messages": messages
        })
    except Exception as e:
        logger.error(f"Error fetching chat for {user_id}: {e}", exc_info=True)
        return HTMLResponse("Error loading chat", status_code=500)

@router.post("/users/{user_id}/add_credits")
async def add_credits(
    user_id: int,
    amount: int = Form(...),
    db: AsyncSession = Depends(get_db),
    username: str = Depends(get_current_username)
):
    try:
        user = await db.get(User, user_id)
        if user:
            old_credits = user.credits
            user.credits += amount
            await db.commit()
            
            # ✅ AUDIT LOG (Kluczowe dla biznesu!)
            logger.info(f"💰 ADMIN ACTION: {username} added {amount} credits to User {user_id}. (Was: {old_credits}, Now: {user.credits})")
            
        return HTMLResponse(content=f"Added {amount} credits to {user_id}. <a href='/admin/'>Back</a>")
    except Exception as e:
        logger.error(f"Error adding credits to {user_id}: {e}", exc_info=True)
        return HTMLResponse("Transaction failed", status_code=500)