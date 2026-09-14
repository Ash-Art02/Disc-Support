"""
Dashboard web server: FastAPI + Discord OAuth2 + PostgreSQL/SQLite shared DB
Run: pip install -r requirements.txt && python dashboard.py
OAuth2: Discord Developer Portal -> OAuth2 -> Redirects -> add http://localhost:8080/callback (dev) or https://your-app.fly.dev/callback (prod)
"""
import os
import json
import time
import secrets
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

# ─── DB Layer ───
from db import (
    init_db, close_db,
    get_guild_settings, set_guild_settings,
    set_restart_flag, set_idle_flag,
)

# ─── Config ───
CLIENT_ID = os.getenv("DASHBOARD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DASHBOARD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DASHBOARD_REDIRECT_URI", "http://localhost:8080/callback")
SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY", secrets.token_urlsafe(32))
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID", "").strip()

# ─── Discord API helpers ───
API_BASE = "https://discord.com/api/v10"

async def discord_get(path: str, token: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{API_BASE}{path}", headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()

async def exchange_code(code: str) -> dict:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/oauth2/token", data=data, headers=headers)
        r.raise_for_status()
        return r.json()

async def refresh_token(refresh: str) -> dict:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{API_BASE}/oauth2/token", data=data, headers=headers)
        r.raise_for_status()
        return r.json()

async def bot_get(path: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{API_BASE}{path}", headers={"Authorization": f"Bot {BOT_TOKEN}"})
        r.raise_for_status()
        return r.json()

# ─── FastAPI app ───
app = FastAPI(title="Support Bot Dashboard", on_startup=[init_db], on_shutdown=[close_db])
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False)

templates = Jinja2Templates(directory="templates")

# ─── Session / Auth ───
def get_user(request: Request) -> Optional[dict]:
    return request.session.get("user")

def require_user(request: Request) -> dict:
    user = get_user(request)
    if not user:
        raise HTTPException(401, "Not logged in")
    return user

# ─── Routes ───
@app.get("/")
async def home(request: Request):
    user = get_user(request)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})

@app.get("/login")
async def login(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    scope = "identify guilds"
    url = (f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}"
           f"&redirect_uri={REDIRECT_URI}&response_type=code&scope={scope}&state={state}")
    return RedirectResponse(url)

@app.get("/callback")
async def callback(request: Request, code: str, state: str):
    if state != request.session.pop("oauth_state", None):
        raise HTTPException(400, "Invalid state")
    tokens = await exchange_code(code)
    user = await discord_get("/users/@me", tokens["access_token"])
    guilds = await discord_get("/users/@me/guilds", tokens["access_token"])
    managed = [g for g in guilds if g.get("permissions", 0) & 0x20]
    request.session["user"] = {
        "id": user["id"],
        "username": user["username"],
        "avatar": user.get("avatar"),
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": time.time() + tokens.get("expires_in", 604800),
        "guilds": managed,
    }
    return RedirectResponse("/")

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")

# ─── Guild settings API ───
class GuildSettings(BaseModel):
    staff_role: Optional[str] = "Support"
    senior_role: Optional[str] = "Senior Support"
    banned_role: Optional[str] = "Banned"
    ticket_category: Optional[str] = "Tickets"
    support_category: Optional[str] = "SUPPORT"
    panel_channel: Optional[str] = "report-a-problem"
    log_channel: Optional[str] = "support-log"
    senior_log_channel: Optional[str] = "senior-log"
    appeal_channel: Optional[str] = "appeal"
    notes_log_channel: Optional[str] = "staff-notes"
    actions_log_channel: Optional[str] = "mod-actions"
    message_log_channel: Optional[str] = "message-log"
    recommend_channel: Optional[str] = "recommend-senior"
    idle_warn_hours: float = 24
    idle_close_hours: float = 72
    idle_check_minutes: float = 15
    max_open_per_user: int = 3
    create_cooldown_sec: int = 45

@app.get("/api/guilds")
async def list_guilds(user: dict = Depends(require_user)):
    bot_guilds = await bot_get("/users/@me/guilds")
    bot_guild_ids = {g["id"] for g in bot_guilds}
    return [
        {
            "id": g["id"],
            "name": g["name"],
            "icon": g.get("icon"),
            "bot_here": g["id"] in bot_guild_ids,
            "managed": True,
        }
        for g in user["guilds"]
    ]

@app.get("/api/guild/{guild_id}/settings")
async def get_settings(guild_id: str, user: dict = Depends(require_user)):
    if not any(g["id"] == guild_id for g in user["guilds"]):
        raise HTTPException(403, "Not your guild")
    settings = await get_guild_settings(int(guild_id))
    return GuildSettings(**settings)

@app.post("/api/guild/{guild_id}/settings")
async def save_settings(guild_id: str, data: GuildSettings, user: dict = Depends(require_user)):
    if not any(g["id"] == guild_id for g in user["guilds"]):
        raise HTTPException(403, "Not your guild")
    await set_guild_settings(int(guild_id), data.model_dump())
    return {"ok": True}

# ─── Bot stats (read-only) ───
@app.get("/api/guild/{guild_id}/stats")
async def get_stats(guild_id: str, user: dict = Depends(require_user)):
    if not any(g["id"] == guild_id for g in user["guilds"]):
        raise HTTPException(403, "Not your guild")
    try:
        channels = await bot_get(f"/guilds/{guild_id}/channels")
        tickets = [c for c in channels if c["type"] == 0 and any(c["name"].startswith(p + "-") for p in ("support", "report", "appeal", "other", "claimed-support", "claimed-report", "claimed-appeal", "claimed-other"))]
        return {
            "open_tickets": len(tickets),
            "ticket_channels": [{"id": c["id"], "name": c["name"]} for c in tickets[:10]],
        }
    except Exception as e:
        return {"error": str(e)}

# ─── Actions (write flags to DB for bot to pick up) ───
class RestartRequest(BaseModel):
    confirm: bool = True

@app.post("/api/guild/{guild_id}/restart")
async def trigger_restart(guild_id: str, req: RestartRequest, user: dict = Depends(require_user)):
    if not any(g["id"] == guild_id for g in user["guilds"]):
        raise HTTPException(403, "Not your guild")
    if not req.confirm:
        raise HTTPException(400, "Confirm required")
    await set_restart_flag(int(guild_id))
    return {"ok": True, "message": "Restart requested - bot will pick up within 5s"}

@app.post("/api/guild/{guild_id}/idle-check")
async def trigger_idle(guild_id: str, user: dict = Depends(require_user)):
    if not any(g["id"] == guild_id for g in user["guilds"]):
        raise HTTPException(403, "Not your guild")
    await set_idle_flag(int(guild_id))
    return {"ok": True, "message": "Idle check requested"}

# ─── Health ───
@app.get("/health")
async def health():
    return {"status": "ok"}

# ─── Entrypoint ───
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)