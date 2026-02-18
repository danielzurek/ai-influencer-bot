from datetime import datetime
from typing import Optional, List
from sqlalchemy import BigInteger, String, Boolean, DateTime, ForeignKey, Text, Float, JSON, Table, Column
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs

class Base(AsyncAttrs, DeclarativeBase):
    pass

# --- TABELA ŁĄCZĄCA (Many-to-Many) ---
user_groups = Table(
    "user_groups",
    Base.metadata,
    Column("user_id", ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)

class User(Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    credits: Mapped[int] = mapped_column(default=10)
    
    # Pamięć długotrwała (JSON)
    info: Mapped[dict] = mapped_column(JSON, default={})
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    
    # Relacje
    messages: Mapped[List["Message"]] = relationship("Message", back_populates="user")
    transactions: Mapped[List["Transaction"]] = relationship("Transaction", back_populates="user")
    
    # NOWE: Relacja do grup
    groups: Mapped[List["Group"]] = relationship("Group", secondary=user_groups, back_populates="users")

class Group(Base):
    __tablename__ = "groups"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    
    # Relacja do userów
    users: Mapped[List["User"]] = relationship("User", secondary=user_groups, back_populates="groups")

class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    role: Mapped[str] = mapped_column(String(20)) # user/assistant
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    user: Mapped["User"] = relationship("User", back_populates="messages")

class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20))
    user: Mapped["User"] = relationship("User", back_populates="transactions")

class Persona(Base):
    __tablename__ = "personas"
    
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    system_prompt: Mapped[str] = mapped_column(Text)
    
    telegram_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ai_model: Mapped[str] = mapped_column(String(100), default="openrouter/free")
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)