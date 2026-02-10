import secrets
from fastapi import APIRouter, Depends, HTTPException, status, Form
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.database.models import User, Message, Transaction
# We need to import get_db and settings. 
# In a larger app, these would be in a shared config/dependency file.
# For this structure, we assume they are importable or passed cleanly.
# To avoid circular imports, we assume `main` imports this, but dependencies 
# usually live in a `app.dependencies` module. 
# *Patching imports for context of this file generation*:
from app.database.models import Base 

router = APIRouter()
security = HTTPBasic()
templates = Jinja2Templates(directory="app/templates")

# --- Auth ---
# Re-import settings mechanism or use os.getenv for simplicity in this snippet
import os
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- Dependency Hack for this snippet ---
# In production: from app.dependencies import get_db
from app.main import get_db

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, 
    username: str = Depends(get_current_username),
    db: AsyncSession = Depends(get_db)
):
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

@router.get("/users", response_class=HTMLResponse)
async def user_list(
    request: Request, 
    username: str = Depends(get_current_username),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).order_by(desc(User.created_at)).limit(100))
    users = result.scalars().all()
    return templates.TemplateResponse("users.html", {"request": request, "users": users})

@router.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat_viewer(
    request: Request, 
    user_id: int,
    username: str = Depends(get_current_username),
    db: AsyncSession = Depends(get_db)
):
    user = await db.get(User, user_id)
    result = await db.execute(
        select(Message).where(Message.user_id == user_id).order_by(Message.timestamp)
    )
    messages = result.scalars().all()
    
    return templates.TemplateResponse("chat_viewer.html", {
        "request": request, 
        "chat_user": user, 
        "messages": messages
    })

# --- Simple Action: Add Credits ---
from fastapi import Request
@router.post("/users/{user_id}/add_credits")
async def add_credits(
    user_id: int,
    amount: int = Form(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username)
):
    user = await db.get(User, user_id)
    if user:
        user.credits += amount
        await db.commit()
    # Redirect back to user list or chat
    return HTMLResponse(content=f"Added {amount} credits to {user_id}. <a href='/admin/'>Back</a>")