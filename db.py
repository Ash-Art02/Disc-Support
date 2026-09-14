"""
Shared database layer: PostgreSQL (prod) + SQLite (local dev)
Tables: guild_settings, restart_flags, idle_flags
"""
import os
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any

from sqlalchemy import (
    Column, String, BigInteger, Text, DateTime, select, delete, create_engine
)
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, async_sessionmaker
)
from sqlalchemy.orm import DeclarativeBase

# ─── Config ───
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")
if IS_POSTGRES:
    # SQLAlchemy asyncpg dialect
    ASYNC_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    # Local SQLite file
    DATA_DIR = Path(__file__).parent / "dashboard_data"
    DATA_DIR.mkdir(exist_ok=True)
    ASYNC_URL = f"sqlite+aiosqlite:///{DATA_DIR / 'dashboard.db'}"

# ─── Engine / Session ───
engine = create_async_engine(ASYNC_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)

# ─── Models ───
class Base(DeclarativeBase):
    pass

class GuildSettings(Base):
    __tablename__ = "guild_settings"
    guild_id = Column(BigInteger, primary_key=True)
    settings_json = Column(Text, default="{}")
    updated_at = Column(DateTime, default=lambda: time.time())

class RestartFlag(Base):
    __tablename__ = "restart_flags"
    guild_id = Column(BigInteger, primary_key=True)
    requested_at = Column(DateTime, default=lambda: time.time())

class IdleFlag(Base):
    __tablename__ = "idle_flags"
    guild_id = Column(BigInteger, primary_key=True)
    requested_at = Column(DateTime, default=lambda: time.time())

# ─── Init ───
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ─── Settings API ───
async def get_guild_settings(guild_id: int) -> Dict[str, Any]:
    async with async_session() as session:
        row = await session.get(GuildSettings, guild_id)
        if row:
            return json.loads(row.settings_json)
        return {}

async def set_guild_settings(guild_id: int, settings: Dict[str, Any]):
    async with async_session() as session:
        row = await session.get(GuildSettings, guild_id)
        if row:
            row.settings_json = json.dumps(settings)
            row.updated_at = time.time()
        else:
            row = GuildSettings(guild_id=guild_id, settings_json=json.dumps(settings))
            session.add(row)
        await session.commit()

# ─── Flag API (bot polls these) ───
async def set_restart_flag(guild_id: int):
    async with async_session() as session:
        row = await session.get(RestartFlag, guild_id)
        if not row:
            row = RestartFlag(guild_id=guild_id)
            session.add(row)
        row.requested_at = time.time()
        await session.commit()

async def check_and_clear_restart_flag(guild_id: int) -> bool:
    async with async_session() as session:
        row = await session.get(RestartFlag, guild_id)
        if row:
            await session.delete(row)
            await session.commit()
            return True
        return False

async def set_idle_flag(guild_id: int):
    async with async_session() as session:
        row = await session.get(IdleFlag, guild_id)
        if not row:
            row = IdleFlag(guild_id=guild_id)
            session.add(row)
        row.requested_at = time.time()
        await session.commit()

async def check_and_clear_idle_flag(guild_id: int) -> bool:
    async with async_session() as session:
        row = await session.get(IdleFlag, guild_id)
        if row:
            await session.delete(row)
            await session.commit()
            return True
        return False

# ─── Sync helpers for local-only code paths ───
def get_guild_settings_sync(guild_id: int) -> Dict[str, Any]:
    """Blocking version for run.py / local scripts."""
    if IS_POSTGRES:
        # Use sync engine for one-off scripts
        from sqlalchemy import create_engine as sync_create_engine
        sync_engine = create_engine(DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1))
    else:
        sync_engine = create_engine(f"sqlite:///{DATA_DIR / 'dashboard.db'}")
    from sqlalchemy.orm import sessionmaker
    SyncSession = sessionmaker(bind=sync_engine)
    with SyncSession() as session:
        row = session.get(GuildSettings, guild_id)
        return json.loads(row.settings_json) if row else {}

# ─── Cleanup ───
async def close_db():
    await engine.dispose()