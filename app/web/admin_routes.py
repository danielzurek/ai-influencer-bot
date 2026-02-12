import secrets
from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from app.database.models import User, Message
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
@router.get("/users", response_class=HTMLResponse) # DODANO TĘ LINIĘ DLA TWOJEGO BŁĘDU!
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    total = await db.scalar(select(func.count(User.telegram_id)))
    users = (await db.execute(select(User).order_by(desc(User.created_at)))).scalars().all()
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "total_users": total, "recent_users": users, "username": user
    })

@router.get("/chat/{user_id}", response_class=HTMLResponse)
async def chat_viewer(request: Request, user_id: int, db: AsyncSession = Depends(get_db), user=Depends(auth)):
    chat_user = await db.get(User, user_id)
    msgs = (await db.execute(select(Message).where(Message.user_id == user_id).order_by(Message.timestamp))).scalars().all()
    return templates.TemplateResponse("chat_viewer.html", {"request": request, "chat_user": chat_user, "messages": msgs, "username": user})