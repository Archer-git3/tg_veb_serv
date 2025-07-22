from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime

Base = declarative_base()

class TelegramAccount(Base):
    __tablename__ = 'telegram_accounts'
    id = Column(Integer, primary_key=True)
    group = Column(String)
    name = Column(String)
    phone = Column(String, unique=True)
    session_string = Column(Text)
    last_updated = Column(DateTime, default=datetime.utcnow)
    is_admin = Column(Boolean, default=False)
    skip_check = Column(Boolean, default=False)
    unread_count = Column(Integer, default=0)
    oldest_unread = Column(DateTime, nullable=True)
    status = Column(String, default='?')


class NotificationChat(Base):
    __tablename__ = 'notification_chats'
    chat_id = Column(String, primary_key=True)
    user_id = Column(Integer)
    username = Column(String)
    groups = Column(JSONB)  # список груп
    is_special = Column(Boolean, default=False)
