from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, String, Boolean, DateTime, ForeignKey, Text, Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs

class Base(AsyncAttrs, DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    credits: Mapped[int] = mapped_column(default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    messages: Mapped[list["Message"]] = relationship("Message", back_populates="user")
    transactions: Mapped[list["Transaction"]] = relationship("Transaction", back_populates="user")

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