"""
Really-good Ticket Bot (discord.py 2.x)
- Panel with ticket types -> tailored modal -> private channel, always pings staff
- No fake auto-fixes. Fast triage, excellent tickets.
- Claim / Close / Transcript / Add-remove, anti-spam, transcripts DM'd + logged

Run: pip install -r requirements.txt ; python bot.py
Setup in Discord (admin): /setup-tickets
"""

import os
import io
import json
import sys
import time
import asyncio
import datetime
import subprocess

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID", "").strip()
STAFF_ROLE_NAME = os.getenv("STAFF_ROLE_NAME", "Support")
SENIOR_STAFF_ROLE_NAME = os.getenv("SENIOR_STAFF_ROLE_NAME", "Senior Support")
SUPPORT_CATEGORY_NAME = os.getenv("SUPPORT_CATEGORY_NAME", "SUPPORT")
TICKETS_CATEGORY_NAME = os.getenv("TICKETS_CATEGORY_NAME", "Tickets")
PANEL_CHANNEL_NAME = os.getenv("REPORT_CHANNEL_NAME", "report-a-problem")
LOG_CHANNEL_NAME = os.getenv("LOG_CHANNEL_NAME", "support-log")
SENIOR_LOG_CHANNEL_NAME = os.getenv("SENIOR_LOG_CHANNEL_NAME", "senior-log")
BANNED_ROLE_NAME = os.getenv("BANNED_ROLE_NAME", "Banned")
APPEAL_CHANNEL_NAME = os.getenv("APPEAL_CHANNEL_NAME", "appeal")
RECOMMEND_CHANNEL_NAME = os.getenv("RECOMMEND_CHANNEL_NAME", "recommend-senior")
NOTES_LOG_CHANNEL_NAME = os.getenv("NOTES_LOG_CHANNEL_NAME", "staff-notes")
ACTIONS_LOG_CHANNEL_NAME = os.getenv("ACTIONS_LOG_CHANNEL_NAME", "mod-actions")
MESSAGE_LOG_CHANNEL_NAME = os.getenv("MESSAGE_LOG_CHANNEL_NAME", "message-log")
IDLE_WARN_HOURS = float(os.getenv("IDLE_WARN_HOURS", "24"))
IDLE_CLOSE_HOURS = float(os.getenv("IDLE_CLOSE_HOURS", "72"))
IDLE_CHECK_MINUTES = float(os.getenv("IDLE_CHECK_MINUTES", "15"))

# ---------------------------------------------------------------------------
# New feature config
# ---------------------------------------------------------------------------
SLA_WARN_HOURS = float(os.getenv("SLA_WARN_HOURS", "48"))
SLA_CLOSE_HOURS = float(os.getenv("SLA_CLOSE_HOURS", "72"))
WELCOME_DM_ENABLED = os.getenv("WELCOME_DM_ENABLED", "true").lower() == "true"
FAQ_CHANNEL_NAME = os.getenv("FAQ_CHANNEL_NAME", "faq")

MAX_OPEN_PER_USER = 3
CREATE_COOLDOWN_SEC = 45

# ---------------------------------------------------------------------------
# Ticket types: prefix, label, modal fields
# ---------------------------------------------------------------------------
TICKET_TYPES = {
    "support": {
        "label": "General Support", "emoji": "🛟", "prefix": "support",
        "desc": "Bug, crash, can't use something", "level": "standard",
        "fields": [
            ("subject", "Subject", "e.g. Crash on launch", 100, False),
            ("details", "What happened? + error message", "Steps, expected vs actual, error text", 1500, True),
            ("tried", "Screenshot link / extra info", "paste link or 'will upload in ticket'", 400, False),
        ],
    },
    "report": {
        "label": "Report User / Mod", "emoji": "🚨", "prefix": "report",
        "desc": "Report someone", "level": "senior",
        "fields": [
            ("subject", "Who are you reporting? (@name)", "@user", 100, False),
            ("details", "What happened? When? Where?", "what they said/did, channel, time", 1500, True),
            ("tried", "Evidence (message link)", "right-click message > Copy Link, or 'will upload'", 400, False),
        ],
    },
    "appeal": {
        "label": "Appeal", "emoji": "⚖️", "prefix": "appeal",
        "desc": "Ban / mute / warning appeal", "level": "standard",
        "fields": [
            ("subject", "What punishment? (ban/mute/warn)", "e.g. 7d ban", 100, False),
            ("details", "Why should it be lifted?", "be honest, what happened", 1500, True),
            ("tried", "Any evidence of innocence?", "links or 'none'", 400, False),
        ],
    },
    "other": {
        "label": "Other", "emoji": "❓", "prefix": "other",
        "desc": "Anything else", "level": "standard",
        "fields": [
            ("subject", "Subject", "e.g. Question about events", 100, False),
            ("details", "Describe it", "as much detail as you can", 1500, True),
            ("tried", "Extra info / screenshot?", "link or 'will upload'", 400, False),
        ],
    },
}

URGENT_WORDS = ["threat", "dox", "ddos", "hacked", "stolen", "suicide", "self-harm",
                "assault", "ongoing"]

# cooldown + counter persistence
_last_create = {}
COUNTER_FILE = os.path.join(os.path.dirname(__file__), "tickets.json")

def next_counter() -> int:
    try:
        with open(COUNTER_FILE, "r") as f:
            n = json.load(f).get("counter", 0)
    except Exception:
        n = 0
    n += 1
    try:
        with open(COUNTER_FILE, "w") as f:
            json.dump({"counter": n}, f)
    except Exception:
        pass
    return n


def parse_duration(text: str | None) -> float | None:
    """'30m'/'2h'/'7d'/'1w'/seconds -> seconds. None/perm/permanent/0 -> None (no expiry)."""
    if text is None:
        return None
    t = text.strip().lower()
    if t in ("", "perm", "permanent", "forever", "0", "none"):
        return None
    m = __import__("re").fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw])?", t)
    if not m:
        raise ValueError(f"Bad duration '{text}'. Use like 30m, 2h, 7d, 1w or leave empty.")
    val, unit = float(m.group(1)), (m.group(2) or "s")
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "permanent"
    if seconds < 90:
        return f"{seconds:g}s"
    if seconds < 5400:
        return f"{seconds/60:g}m"
    if seconds < 172800:
        return f"{seconds/3600:g}h"
    return f"{seconds/86400:g}d"


def is_staff_member(member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
        return True
    roles = getattr(member, "roles", [])
    staff = staff_role(member.guild)
    senior = senior_role(member.guild)
    return (staff in roles if staff else False) or (senior in roles if senior else False)


def staff_role(guild: discord.Guild):
    return discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)


def senior_role(guild: discord.Guild):
    return discord.utils.get(guild.roles, name=SENIOR_STAFF_ROLE_NAME)


def role_for_type(guild: discord.Guild, type_key: str):
    """Report tickets -> senior only. Everything else -> standard Support."""
    if TICKET_TYPES.get(type_key, {}).get("level") == "senior":
        return senior_role(guild)
    return staff_role(guild)


def log_channel_for_type(guild: discord.Guild, type_key: str):
    """Report + appeal logs go to senior-log (private). Everything else -> support-log."""
    if type_key in ("report", "appeal"):
        ch = discord.utils.get(guild.text_channels, name=SENIOR_LOG_CHANNEL_NAME)
        if ch:
            return ch
    return discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)


def notes_log(guild: discord.Guild):
    """Dedicated notes channel, fallback to support-log."""
    return discord.utils.get(guild.text_channels, name=NOTES_LOG_CHANNEL_NAME) \
        or discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)


def actions_log(guild: discord.Guild):
    """Dedicated mod-actions channel, fallback to support-log."""
    return discord.utils.get(guild.text_channels, name=ACTIONS_LOG_CHANNEL_NAME) \
        or discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)


def message_log(guild: discord.Guild):
    """Tamper-proof chat/picture log. Staff can read, nobody (except the bot) can delete."""
    return discord.utils.get(guild.text_channels, name=MESSAGE_LOG_CHANNEL_NAME)


def _is_staff_log_channel(channel) -> bool:
    """Log channels never get logged themselves (prevents feedback loops)."""
    n = (getattr(channel, "name", "") or "").lower()
    return n in {LOG_CHANNEL_NAME.lower(), SENIOR_LOG_CHANNEL_NAME.lower(),
                 NOTES_LOG_CHANNEL_NAME.lower(), ACTIONS_LOG_CHANNEL_NAME.lower(),
                 MESSAGE_LOG_CHANNEL_NAME.lower()}


def notes_log_for_ticket(guild: discord.Guild, channel: discord.TextChannel):
    """Notes on senior tickets stay in senior-log (private).
    Notes on normal tickets go to staff-notes."""
    n = channel.name.lower()
    if n.startswith(("report-", "claimed-report-", "appeal-", "claimed-appeal-")):
        return discord.utils.get(guild.text_channels, name=SENIOR_LOG_CHANNEL_NAME) \
            or notes_log(guild)
    return notes_log(guild)


def can_handle_ticket(member: discord.Member, channel: discord.TextChannel) -> bool:
    """Senior can handle everything. Standard Support can handle everything EXCEPT
    report-* and anything escalated to senior (topic flag)."""
    if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
        return True
    roles = getattr(member, "roles", [])
    senior = senior_role(member.guild)
    staff = staff_role(member.guild)
    is_senior = senior in roles if senior else False
    is_staff = staff in roles if staff else False
    n = channel.name.lower()
    topic = (channel.topic or "").lower()
    if n.startswith(("report-", "claimed-report-")) or "escalated" in topic:
        return is_senior
    return is_senior or is_staff


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------
async def build_transcript(channel: discord.TextChannel, limit=200):
    lines, files = [], []
    async for m in channel.history(limit=limit, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M")
        body = m.content or ""
        if m.attachments:
            body += " " + " ".join(a.url for a in m.attachments)
            files.extend(m.attachments)
        if m.embeds and not body:
            body = "[embed] " + (m.embeds[0].title or m.embeds[0].description or "")[:200]
        lines.append(f"[{ts}] {m.author.display_name} ({m.author.id}): {body[:500]}")
    return "\n".join(lines) or "(empty)"


async def send_transcript(channel: discord.TextChannel, requester=None, close_after=False):
    text = await build_transcript(channel)
    buf = io.BytesIO(text.encode("utf-8", errors="replace"))
    file = discord.File(buf, filename=f"transcript-{channel.name}.txt")
    guild = channel.guild
    # Route report-* AND appeal-* transcripts to senior-log (private)
    n = channel.name.lower()
    is_senior_log = n.startswith(("report-", "claimed-report-", "appeal-", "claimed-appeal-"))
    if is_senior_log:
        log_ch = discord.utils.get(guild.text_channels, name=SENIOR_LOG_CHANNEL_NAME)
        if log_ch is None:
            log_ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    else:
        log_ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    embed = discord.Embed(
        title=f"Transcript: #{channel.name}",
        description=f"Topic: {channel.topic or '-'}\nRequested by {requester.mention if requester else 'system'}\n{len(text.splitlines())} messages",
        color=discord.Color.greyple(),
        timestamp=datetime.datetime.now(datetime.timezone.utc))
    if log_ch:
        await log_ch.send(embed=embed, file=file)
    # fresh buffer for DM (discord.File is single-use)
    buf2 = io.BytesIO(text.encode("utf-8", errors="replace"))
    return discord.File(buf2, filename=f"transcript-{channel.name}.txt")


def is_ticket_channel(ch) -> bool:
    n = ch.name.lower()
    prefixes = tuple(v["prefix"] + "-" for v in TICKET_TYPES.values())
    return n.startswith(prefixes) or n.startswith(tuple("claimed-" + p for p in prefixes))


def user_open_tickets(guild: discord.Guild, user_id: int):
    out = []
    for ch in guild.text_channels:
        if is_ticket_channel(ch) and ch.topic and str(user_id) in ch.topic:
            out.append(ch)
    return out

# ---------------------------------------------------------------------------
# Ticket creation (always notifies staff - that's the point now)
# ---------------------------------------------------------------------------
async def create_ticket(guild: discord.Guild, user: discord.Member, type_key: str,
                        answers: dict) -> discord.TextChannel:
    cfg = TICKET_TYPES[type_key]
    level = cfg.get("level", "standard")
    role = role_for_type(guild, type_key)
    staff = staff_role(guild)
    senior = senior_role(guild)
    tickets_cat = discord.utils.get(guild.categories, name=TICKETS_CATEGORY_NAME)
    if tickets_cat is None:
        tickets_cat = await guild.create_category(TICKETS_CATEGORY_NAME)

    num = next_counter()
    safe = "".join(c if c.isalnum() else "-" for c in user.display_name.lower())[:12].strip("-") or "user"
    name = f"{cfg['prefix']}-{safe}-{num:03d}"[:90]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                          read_message_history=True, attach_files=True,
                                          embed_links=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              manage_channels=True, read_message_history=True),
    }
    if level == "senior":
        # Report tickets: senior only. Explicitly hide from regular Support.
        if senior:
            overwrites[senior] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                             read_message_history=True, attach_files=True)
        if staff and (senior is None or staff.id != senior.id):
            overwrites[staff] = discord.PermissionOverwrite(view_channel=False)
    else:
        # Normal tickets: Support + Senior (senior sees everything)
        if staff:
            overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                            read_message_history=True, attach_files=True)
        if senior and (staff is None or senior.id != staff.id):
            overwrites[senior] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                             read_message_history=True, attach_files=True)
    channel = await guild.create_text_channel(
        name, category=tickets_cat, overwrites=overwrites,
        topic=f"{cfg['label']} | {answers.get('subject','')[:60]} | user={user.id} | n={num}")

    blob = (answers.get("subject","") + " " + answers.get("details","")).lower()
    urgent = any(w in blob for w in URGENT_WORDS)
    color = discord.Color.red() if urgent else discord.Color.gold()

    embed = discord.Embed(title=f"{cfg['emoji']} {answers.get('subject','Ticket')[:200]}",
                          color=color,
                          timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.add_field(name="Type", value=cfg["label"], inline=True)
    embed.add_field(name="Customer", value=f"{user.mention} ({user})", inline=True)
    embed.add_field(name="Ticket", value=f"#{num:03d}", inline=True)
    for k, label, _ph, _ml, _par in cfg["fields"]:
        v = answers.get(k, "-") or "-"
        embed.add_field(name=label, value=v[:1000], inline=False)
    embed.set_footer(text="Reply here. Staff: Claim -> help -> Close ticket.")

    ping = role.mention if role else "(create staff roles!)"
    team = SENIOR_STAFF_ROLE_NAME if level == "senior" else STAFF_ROLE_NAME
    header = f"{ping} 🔴 **URGENT {team}**" if urgent else f"{ping} 🎫 **New {cfg['label']} ticket ({team})**"
    await channel.send(f"{header}\n{user.mention} {team} have been notified - reply here, add screenshots.",
                       embed=embed, view=TicketControlView())

    log_ch = log_channel_for_type(guild, type_key)
    if log_ch:
        le = discord.Embed(title=f"Ticket opened: #{name} ({cfg['label']})",
                           description=f"{user.mention}: {answers.get('subject','')[:200]}",
                           color=color, timestamp=datetime.datetime.now(datetime.timezone.utc))
        le.add_field(name="Channel", value=channel.mention)
        await log_ch.send(embed=le)
    # staff-only heads-up: active watch notes on this user (in notes logs, never in the ticket)
    try:
        notes = active_watches(user.id)
        if notes:
            dest = notes_log_for_ticket(guild, channel)
            if dest:
                we = discord.Embed(title=f"Active staff notes on {user.display_name} ({len(notes)})",
                                   color=discord.Color.orange())
                for i, wn in enumerate(notes[:5], 1):
                    left = max(0, wn["expires_at"] - time.time())
                    we.add_field(name=f"#{i} - expires in {format_duration(left)}",
                                 value=wn["text"][:400], inline=False)
                we.add_field(name="Ticket", value=channel.mention, inline=False)
                await dest.send(embed=we)
    except Exception:
        pass
    return channel

# ---------------------------------------------------------------------------
# Panel + modals
# ---------------------------------------------------------------------------
class TicketModal(discord.ui.Modal):
    def __init__(self, type_key: str):
        cfg = TICKET_TYPES[type_key]
        super().__init__(title=f"{cfg['label']}"[:45])
        self.type_key = type_key
        self.inputs = []
        for key, label, ph, ml, par in cfg["fields"]:
            ti = discord.ui.TextInput(label=label[:45], placeholder=ph[:100],
                                      max_length=ml, required=(par or key != "tried"),
                                      style=discord.TextStyle.paragraph if par or ml > 200 else discord.TextStyle.short)
            ti._field_key = key
            self.inputs.append(ti)
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        now = time.time()
        # cooldown
        if now - _last_create.get(uid, 0) < CREATE_COOLDOWN_SEC:
            await interaction.response.send_message(
                f"Slow down - wait {int(CREATE_COOLDOWN_SEC-(now-_last_create[uid]))}s before opening another ticket.",
                ephemeral=True)
            return
        # max open
        opens = user_open_tickets(interaction.guild, uid)
        if len(opens) >= MAX_OPEN_PER_USER:
            await interaction.response.send_message(
                f"You already have {len(opens)} open tickets: " +
                ", ".join(c.mention for c in opens[:5]) +
                "\nClose one or reply there instead of opening a new one.",
                ephemeral=True)
            return
        answers = {ti._field_key: ti.value.strip() for ti in self.inputs}
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
        except Exception:
            member = interaction.user
        channel = await create_ticket(interaction.guild, member, self.type_key, answers)
        _last_create[uid] = time.time()
        await interaction.followup.send(
            f"Done! Your ticket: {channel.mention}\nStaff have been pinged and will reply there.",
            ephemeral=True)


async def open_modal_for_type(interaction: discord.Interaction, type_key: str):
    """Shared: buttons can be re-clicked forever (fixes stuck-select bug)."""
    # Banned users may only open appeals
    try:
        banned = discord.utils.get(interaction.guild.roles, name=BANNED_ROLE_NAME)
        if banned and banned in getattr(interaction.user, "roles", []) and type_key != "appeal":
            await interaction.response.send_message(
                "You are restricted - go to the appeal channel and use the Appeal option.",
                ephemeral=True)
            return
    except Exception:
        pass
    await interaction.response.send_modal(TicketModal(type_key))


class TicketPanelView(discord.ui.View):
    """Buttons, not a dropdown - re-clicking the same option always works.
    NOTE: no Appeal button here on purpose - appeals live only in #appeal."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="General Support", emoji="🛟", style=discord.ButtonStyle.primary,
                       custom_id="ticket_btn_support")
    async def btn_support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_modal_for_type(interaction, "support")

    @discord.ui.button(label="Report User / Mod", emoji="🚨", style=discord.ButtonStyle.danger,
                       custom_id="ticket_btn_report")
    async def btn_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_modal_for_type(interaction, "report")

    @discord.ui.button(label="Other", emoji="❓", style=discord.ButtonStyle.success,
                       custom_id="ticket_btn_other")
    async def btn_other(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_modal_for_type(interaction, "other")


class AppealOnlyView(discord.ui.View):
    """Lives in #appeal - banned users only see this, appeal only."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Appeal Ban", emoji="⚖️", style=discord.ButtonStyle.primary,
                       custom_id="appeal_only_btn")
    async def appeal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal("appeal"))

# ---------------------------------------------------------------------------
# In-ticket controls
# ---------------------------------------------------------------------------
class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary,
                       custom_id="ticket_claim", emoji="🙋")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not can_handle_ticket(interaction.user, interaction.channel):
            await interaction.response.send_message(
                "You can't claim this one - report tickets are senior staff only.", ephemeral=True)
            return
        # rename to show claimed + set topic
        try:
            if not interaction.channel.name.startswith("claimed-"):
                await interaction.channel.edit(name="claimed-" + interaction.channel.name[:80])
        except Exception:
            pass
        await interaction.response.send_message(
            f"{interaction.user.mention} claimed this ticket - {interaction.channel.topic or ''}")

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary,
                       custom_id="ticket_transcript", emoji="📝")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        f = await send_transcript(interaction.channel, requester=interaction.user)
        await interaction.followup.send("Transcript saved to log channel. Copy:", file=f, ephemeral=True)

    @discord.ui.button(label="Escalate", style=discord.ButtonStyle.primary,
                       custom_id="ticket_escalate", emoji="⏫")
    async def escalate(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Staff-only handoff: Support -> Senior. Members can never escalate themselves."""
        if not can_handle_ticket(interaction.user, interaction.channel):
            await interaction.response.send_message(
                "Only the current handling team can escalate this.", ephemeral=True)
            return
        ch = interaction.channel
        topic = (ch.topic or "")
        n = ch.name.lower()
        if n.startswith(("report-", "claimed-report-")) or "escalated" in topic.lower():
            await interaction.response.send_message("Already with Senior staff.", ephemeral=True)
            return
        guild = interaction.guild
        senior = senior_role(guild)
        staff = staff_role(guild)
        if senior is None:
            await interaction.response.send_message("No Senior role exists yet.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            if staff and staff.id != senior.id:
                await ch.set_permissions(staff, view_channel=False, reason=f"Escalated by {interaction.user}")
            await ch.set_permissions(senior, view_channel=True, send_messages=True,
                                     read_message_history=True, attach_files=True)
            if "escalated" not in topic.lower():
                try:
                    await ch.edit(topic=(topic + " | escalated=true")[:250] if topic else "escalated=true")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except discord.Forbidden:
            await interaction.followup.send(
                "I lack Manage Channels or my role is below the staff roles.", ephemeral=False)
            return
        await ch.send(f"{senior.mention} ⏫ **Escalated by {interaction.user.mention}** - "
                      "Senior team now owns this. Support can no longer see it.")
        senior_log = discord.utils.get(guild.text_channels, name=SENIOR_LOG_CHANNEL_NAME)
        if senior_log:
            e = discord.Embed(title=f"Ticket escalated: #{ch.name}",
                              description=f"By {interaction.user.mention} | {topic[:200]}",
                              color=discord.Color.purple(),
                              timestamp=datetime.datetime.now(datetime.timezone.utc))
            e.add_field(name="Channel", value=ch.mention)
            await senior_log.send(embed=e)
        await interaction.followup.send("Escalated to Senior - they have been pinged.", ephemeral=False)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger,
                       custom_id="ticket_close", emoji="🔒")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_staff = can_handle_ticket(interaction.user, interaction.channel) or \
                   interaction.user.guild_permissions.manage_channels
        if not is_staff:
            await interaction.response.send_message(
                "Only the handling staff team can close this. Report tickets = senior only.",
                ephemeral=True)
            return
        await interaction.response.send_message("Closing in 5s - transcript will be DM'd + logged.")
        try:
            text = await build_transcript(interaction.channel)
            # DM the ticket owner
            try:
                uid = int((interaction.channel.topic or "").split("user=")[1].split()[0].strip("|, "))
                owner = interaction.guild.get_member(uid)
                if owner:
                    buf = io.BytesIO(f"Ticket #{interaction.channel.name}\n\n".encode() +
                                     text.encode("utf-8", errors="replace"))
                    await owner.send(f"Your ticket `{interaction.channel.name}` was closed. Transcript attached.",
                                     file=discord.File(buf, filename=f"transcript-{interaction.channel.name}.txt"))
            except Exception:
                pass
            await send_transcript(interaction.channel, requester=interaction.user)
        except Exception:
            pass
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Closed by {interaction.user}")
        except discord.HTTPException:
            pass

# ---------------------------------------------------------------------------
# Appeal-jail: /ban restricts to #appeal only (no kick), /unban restores
# ---------------------------------------------------------------------------
BANNED_FILE = os.path.join(os.path.dirname(__file__), "banned.json")


def load_banned():
    try:
        with open(BANNED_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_banned(data):
    try:
        with open(BANNED_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


async def ensure_appeal_channel(guild: discord.Guild):
    """#appeal: ONLY visible to Banned users + handling staff. Hidden from everyone else."""
    banned = discord.utils.get(guild.roles, name=BANNED_ROLE_NAME)
    if banned is None:
        banned = await guild.create_role(name=BANNED_ROLE_NAME, reason="appeal-jail role")
    support_cat = discord.utils.get(guild.categories, name=SUPPORT_CATEGORY_NAME)
    if support_cat is None:
        support_cat = await guild.create_category(SUPPORT_CATEGORY_NAME)
    staff = staff_role(guild)
    senior = senior_role(guild)
    appeal_ch = discord.utils.get(guild.text_channels, name=APPEAL_CHANNEL_NAME)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        banned: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                            read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if staff:
        overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                        read_message_history=True)
    if senior:
        overwrites[senior] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                         read_message_history=True)
    if appeal_ch is None:
        appeal_ch = await guild.create_text_channel(APPEAL_CHANNEL_NAME, category=support_cat,
                                                    overwrites=overwrites,
                                                    topic="Restricted? Appeal here with the button below.")
    else:
        # migrate old public appeal channel to private (remove @everyone view)
        try:
            await appeal_ch.edit(overwrites=overwrites,
                                 topic="Restricted? Appeal here with the button below.")
        except (discord.Forbidden, discord.HTTPException):
            pass
    return banned, appeal_ch


async def post_appeal_panel(guild: discord.Guild, clean_first: bool = False):
    _, appeal_ch = await ensure_appeal_channel(guild)
    if clean_first:
        await clean_old_panels(appeal_ch, "Restricted? Appeal here")
    embed = discord.Embed(
        title="Restricted? Appeal here",
        description="Click **Appeal Ban** below to open a private appeal ticket (staff are pinged).",
        color=discord.Color.orange())
    await appeal_ch.send(embed=embed, view=AppealOnlyView())
    return appeal_ch


# ---------------------------------------------------------------------------
# Recommend-for-senior: members nominate Support mods, vote, seniors promote
# ---------------------------------------------------------------------------
REC_FILE = os.path.join(os.path.dirname(__file__), "recommendations.json")


def load_recs():
    try:
        with open(REC_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_recs(data):
    try:
        with open(REC_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def rec_embed(nominee: discord.Member, recommender: discord.Member, reason: str,
              up: int = 0, down: int = 0, status: str = "open"):
    colors = {"open": discord.Color.blurple(), "promoted": discord.Color.green(),
              "dismissed": discord.Color.greyple()}
    e = discord.Embed(title=f"Nomination: {nominee.display_name}",
                      color=colors.get(status, discord.Color.blurple()))
    e.add_field(name="Nominee", value=f"{nominee.mention} ({nominee})", inline=True)
    e.add_field(name="Recommended by", value=recommender.mention, inline=True)
    e.add_field(name="Status", value=status.upper(), inline=True)
    e.add_field(name="Why they deserve Senior", value=reason[:1000] or "-", inline=False)
    e.add_field(name="Votes", value=f"👍 {up}   |   👎 {down}", inline=False)
    e.set_footer(text="One vote each - click again to switch. Seniors: Promote/Dismiss.")
    return e


def resolve_member(guild: discord.Guild, text: str):
    text = (text or "").strip()
    if not text:
        return None
    # mention <@123> / <@!123> or raw ID
    digits = "".join(c for c in text if c.isdigit())
    if digits and len(digits) >= 15:
        try:
            m = guild.get_member(int(digits))
        except (ValueError, OverflowError):
            m = None
        if m:
            return m
    # name / display name / name#discrim
    tl = text.lower().lstrip("@")
    for m in guild.members:
        if m.display_name.lower() == tl or str(m).lower() == tl or m.name.lower() == tl:
            return m
    return None


class RecommendModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Recommend for Senior")
        self.who = discord.ui.TextInput(label="Who? (@name or user ID)", max_length=100, required=True,
                                        placeholder="@SomeMod")
        self.why = discord.ui.TextInput(label="Why do they deserve Senior?", style=discord.TextStyle.paragraph,
                                        max_length=800, required=True,
                                        placeholder="Helpful, fair, active... give an example")
        self.add_item(self.who)
        self.add_item(self.why)

    async def on_submit(self, interaction: discord.Interaction):
        nominee = resolve_member(interaction.guild, self.who.value)
        if nominee is None:
            await interaction.response.send_message(
                "Couldn't find that member. Use @mention or their user ID.", ephemeral=True)
            return
        if nominee.bot:
            await interaction.response.send_message("You can't nominate a bot.", ephemeral=True)
            return
        staff = staff_role(interaction.guild)
        if staff and staff not in getattr(nominee, "roles", []):
            await interaction.response.send_message(
                f"{nominee.display_name} doesn't have the {STAFF_ROLE_NAME} role - "
                "only Support mods can be recommended for Senior.", ephemeral=True)
            return
        senior = senior_role(interaction.guild)
        if senior and senior in getattr(nominee, "roles", []):
            await interaction.response.send_message("They're already Senior.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = rec_embed(nominee, interaction.user, self.why.value.strip())
        msg = await interaction.channel.send(embed=embed, view=RecVoteView())
        data = load_recs()
        data[str(msg.id)] = {"nominee_id": nominee.id, "reason": self.why.value.strip(),
                             "by": interaction.user.id, "up": [], "down": [], "status": "open"}
        save_recs(data)
        await interaction.followup.send(f"Nominated {nominee.mention}!", ephemeral=True)


class RecommendView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Recommend for Senior", emoji="⭐", style=discord.ButtonStyle.primary,
                       custom_id="rec_open")
    async def open_rec(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RecommendModal())


class RecVoteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _vote(self, interaction: discord.Interaction, kind: str):
        data = load_recs()
        rec = data.get(str(interaction.message.id))
        if rec is None or rec.get("status") != "open":
            await interaction.response.send_message("Voting closed on this one.", ephemeral=True)
            return
        uid = interaction.user.id
        up, down = rec.get("up", []), rec.get("down", [])
        if kind == "up":
            if uid in up:
                up.remove(uid)
            else:
                up.append(uid)
                if uid in down:
                    down.remove(uid)
        else:
            if uid in down:
                down.remove(uid)
            else:
                down.append(uid)
                if uid in up:
                    up.remove(uid)
        rec["up"], rec["down"] = up, down
        data[str(interaction.message.id)] = rec
        save_recs(data)
        guild = interaction.guild
        nominee = guild.get_member(rec["nominee_id"])
        recommender = guild.get_member(rec["by"])
        try:
            await interaction.message.edit(
                embed=rec_embed(nominee or interaction.user, recommender or interaction.user,
                                rec.get("reason", "-"), len(up), len(down), "open"))
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass
        await interaction.response.send_message(
            f"Vote counted: 👍 {len(up)} / 👎 {len(down)}", ephemeral=True)

    @discord.ui.button(label="Upvote", emoji="👍", style=discord.ButtonStyle.success, custom_id="rec_up")
    async def upvote(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "up")

    @discord.ui.button(label="Downvote", emoji="👎", style=discord.ButtonStyle.secondary, custom_id="rec_down")
    async def downvote(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "down")

    def _is_senior_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True
        senior = senior_role(member.guild)
        return senior in getattr(member, "roles", []) if senior else False

    @discord.ui.button(label="Promote", emoji="🎖️", style=discord.ButtonStyle.primary, custom_id="rec_promote")
    async def promote(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_senior_staff(interaction.user):
            await interaction.response.send_message("Senior staff only.", ephemeral=True)
            return
        data = load_recs()
        rec = data.get(str(interaction.message.id))
        if not rec:
            await interaction.response.send_message("Recommendation not found.", ephemeral=True)
            return
        guild = interaction.guild
        nominee = guild.get_member(rec["nominee_id"])
        senior = senior_role(guild)
        if nominee is None or senior is None:
            await interaction.response.send_message("Nominee or Senior role missing.", ephemeral=True)
            return
        if senior.position >= guild.me.top_role.position:
            await interaction.response.send_message(
                f"Move my role above `{SENIOR_STAFF_ROLE_NAME}` first.", ephemeral=True)
            return
        await nominee.add_roles(senior, reason=f"Promoted by {interaction.user} via recommendation")
        rec["status"] = "promoted"
        data[str(interaction.message.id)] = rec
        save_recs(data)
        act = actions_log(guild)
        if act:
            try:
                await act.send(f"🎖️ {interaction.user.mention} promoted {nominee.mention} to Senior "
                               f"(👍 {len(rec.get('up', []))} / 👎 {len(rec.get('down', []))}).")
            except (discord.Forbidden, discord.HTTPException):
                pass
        recommender = guild.get_member(rec["by"])
        try:
            await interaction.message.edit(
                embed=rec_embed(nominee, recommender or interaction.user, rec.get("reason", "-"),
                                len(rec.get("up", [])), len(rec.get("down", [])), "promoted"),
                view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass
        await interaction.response.send_message(f"Promoted {nominee.mention} to {senior.mention}!")

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.danger, custom_id="rec_dismiss")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_senior_staff(interaction.user):
            await interaction.response.send_message("Senior staff only.", ephemeral=True)
            return
        data = load_recs()
        rec = data.get(str(interaction.message.id))
        if not rec:
            await interaction.response.send_message("Recommendation not found.", ephemeral=True)
            return
        rec["status"] = "dismissed"
        data[str(interaction.message.id)] = rec
        save_recs(data)
        act = actions_log(interaction.guild)
        if act:
            try:
                nominee = interaction.guild.get_member(rec.get("nominee_id", 0))
                await act.send(f"🗑️ {interaction.user.mention} dismissed the nomination for "
                               f"{nominee.mention if nominee else rec.get('nominee_id')}.")
            except (discord.Forbidden, discord.HTTPException):
                pass
        try:
            await interaction.message.edit(view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass
        await interaction.response.send_message("Nomination dismissed.", ephemeral=True)


async def clean_old_panels(channel: discord.TextChannel, title_part: str, limit: int = 25):
    """Delete the bot's own previous panel embeds so /setup-* doesn't stack duplicates."""
    try:
        async for m in channel.history(limit=limit):
            if m.author != channel.guild.me or not m.embeds:
                continue
            if title_part.lower() in (m.embeds[0].title or "").lower():
                try:
                    await m.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
    except (discord.Forbidden, discord.HTTPException):
        pass


async def ensure_system(guild: discord.Guild):
    role = staff_role(guild)
    if role is None:
        role = await guild.create_role(name=STAFF_ROLE_NAME, mentionable=True)
    senior = senior_role(guild)
    if senior is None:
        senior = await guild.create_role(name=SENIOR_STAFF_ROLE_NAME, mentionable=True)
    support_cat = discord.utils.get(guild.categories, name=SUPPORT_CATEGORY_NAME)
    if support_cat is None:
        support_cat = await guild.create_category(SUPPORT_CATEGORY_NAME)
    tickets_cat = discord.utils.get(guild.categories, name=TICKETS_CATEGORY_NAME)
    if tickets_cat is None:
        tickets_cat = await guild.create_category(TICKETS_CATEGORY_NAME)
    panel_ch = discord.utils.get(guild.text_channels, name=PANEL_CHANNEL_NAME)
    if panel_ch is None:
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
        panel_ch = await guild.create_text_channel(PANEL_CHANNEL_NAME, category=support_cat, overwrites=overwrites)
    log_ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_ch is None:
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                      senior: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        log_ch = await guild.create_text_channel(LOG_CHANNEL_NAME, category=support_cat, overwrites=overwrites)
    else:
        # make sure senior can also see the normal log
        try:
            await log_ch.set_permissions(senior, view_channel=True, send_messages=True, read_message_history=True)
        except Exception:
            pass
    senior_log = discord.utils.get(guild.text_channels, name=SENIOR_LOG_CHANNEL_NAME)
    if senior_log is None:
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      senior: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        # explicitly hide from regular Support
        if role and role.id != senior.id:
            overwrites[role] = discord.PermissionOverwrite(view_channel=False)
        senior_log = await guild.create_text_channel(SENIOR_LOG_CHANNEL_NAME, category=support_cat, overwrites=overwrites)
    staff_both = [r for r in (role, senior) if r]
    notes_ch = discord.utils.get(guild.text_channels, name=NOTES_LOG_CHANNEL_NAME)
    if notes_ch is None:
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        for r in staff_both:
            overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                        read_message_history=True)
        notes_ch = await guild.create_text_channel(NOTES_LOG_CHANNEL_NAME, category=support_cat,
                                                   overwrites=overwrites,
                                                   topic="Staff-only: /note + member watch notes.")
    actions_ch = discord.utils.get(guild.text_channels, name=ACTIONS_LOG_CHANNEL_NAME)
    if actions_ch is None:
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
        for r in staff_both:
            overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                        read_message_history=True)
        actions_ch = await guild.create_text_channel(ACTIONS_LOG_CHANNEL_NAME, category=support_cat,
                                                      overwrites=overwrites,
                                                      topic="Staff-only: bans, rank changes, promotions.")
    # tamper-proof message/picture log: BOTH staff tiers can read, NEITHER can
    # send or delete (no Manage Messages for staff - only the bot writes here).
    msg_log = discord.utils.get(guild.text_channels, name=MESSAGE_LOG_CHANNEL_NAME)
    ro_overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                     guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           manage_messages=True, read_message_history=True)}
    for r in staff_both:
        ro_overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                       add_reactions=False, read_message_history=True)
    if msg_log is None:
        msg_log = await guild.create_text_channel(MESSAGE_LOG_CHANNEL_NAME, category=support_cat,
                                                  overwrites=ro_overwrites,
                                                  topic="Tamper-proof: deleted/edited chats + pictures. Staff read-only.")
    else:
        # enforce read-only on re-runs (repairs channels where staff could delete)
        try:
            await msg_log.edit(overwrites=ro_overwrites,
                               topic="Tamper-proof: deleted/edited chats + pictures. Staff read-only.")
        except (discord.Forbidden, discord.HTTPException):
            pass
    # appeal jail channel (banned users only see this)
    try:
        await ensure_appeal_channel(guild)
    except Exception as e:
        print(f"appeal channel setup failed: {e}")
    return role, senior, support_cat, tickets_cat, panel_ch, log_ch, senior_log, notes_ch, actions_ch, msg_log


@app_commands.checks.has_permissions(manage_guild=True)
@bot.tree.command(name="setup-tickets", description="Create the ticket panel + categories + log (admin).")
async def setup_tickets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        role, senior, _sc, tickets_cat, panel_ch, log_ch, senior_log, notes_ch, actions_ch, msg_log = await ensure_system(interaction.guild)
        main_types = {k: v for k, v in TICKET_TYPES.items() if k != "appeal"}
        lines = "\n".join(
            f"{v['emoji']} **{v['label']}** - {v['desc']}"
            + (" *(goes to senior staff privately)*" if v.get("level") == "senior" else "")
            for v in main_types.values())
        embed = discord.Embed(
            title="Need help? Open a ticket",
            description=(f"Click a button below. A private channel opens and staff are pinged.\n\n{lines}\n\n"
                         f"Please don't open duplicates - max {MAX_OPEN_PER_USER} open each.\n"
                         f"Normal: {role.mention} | Reports: {senior.mention} (private)"),
            color=discord.Color.blurple())
        await clean_old_panels(panel_ch, "Need help? Open a ticket")
        await panel_ch.send(embed=embed, view=TicketPanelView())
        appeal_ch = await post_appeal_panel(interaction.guild, clean_first=True)
        await log_ch.send(f"Ticket system ready. Staff: {role.mention} Senior: {senior.mention}\n"
                          f"Logs: tickets -> {log_ch.mention}, senior tickets -> {senior_log.mention}, "
                          f"notes -> {notes_ch.mention}, actions -> {actions_ch.mention}, "
                          f"deleted/edited chats+pics -> {msg_log.mention} (staff read-only)")
        await interaction.followup.send(
            f"Done! Panel in {panel_ch.mention}, appeal-only in {appeal_ch.mention}, "
            f"tickets in {tickets_cat.name}.\nLogs: {log_ch.mention} (tickets), "
            f"{senior_log.mention} (senior), {notes_ch.mention} (notes), {actions_ch.mention} (actions), "
            f"{msg_log.mention} (deleted/edited evidence, staff can't delete).",
            ephemeral=True)
    except discord.Forbidden as e:
        print(f"setup-tickets Forbidden: {e}")
        await interaction.followup.send(
            "I don't have permission for that. Give me Manage Channels + Manage Roles, "
            "and drag my role ABOVE Support / Senior Support / Banned (Server Settings > Roles). Then retry.",
            ephemeral=True)
    except Exception as e:
        print(f"setup-tickets failed: {type(e).__name__}: {e}")
        await interaction.followup.send(f"Setup failed: `{type(e).__name__}: {e}`", ephemeral=True)


@bot.tree.command(name="ticket", description="Open a ticket (buttons).")
async def ticket_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Click a ticket type:", view=TicketPanelView(), ephemeral=True)


@bot.tree.command(name="report", description="Alias for /ticket.")
async def report_alias(interaction: discord.Interaction):
    await ticket_cmd.callback(interaction)


@app_commands.checks.has_permissions(manage_guild=True)
@bot.tree.command(name="setup-recommend-senior",
                  description="Optional: channel to recommend Support mods for Senior (admin).")
async def setup_recommend_senior(interaction: discord.Interaction):
    """Optional setup: #recommend-senior where anyone can nominate Support mods."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        guild = interaction.guild
        support_cat = discord.utils.get(guild.categories, name=SUPPORT_CATEGORY_NAME)
        if support_cat is None:
            support_cat = await guild.create_category(SUPPORT_CATEGORY_NAME)
        rec_ch = discord.utils.get(guild.text_channels, name=RECOMMEND_CHANNEL_NAME)
        if rec_ch is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                                read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            staff = staff_role(guild)
            senior = senior_role(guild)
            if staff:
                overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                read_message_history=True)
            if senior:
                overwrites[senior] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                 read_message_history=True)
            rec_ch = await guild.create_text_channel(RECOMMEND_CHANNEL_NAME, category=support_cat,
                                                     overwrites=overwrites,
                                                     topic="Recommend Support mods for Senior.")
        await clean_old_panels(rec_ch, "Recommend a future Senior")
        embed = discord.Embed(
            title="⭐ Recommend a future Senior",
            description=(f"Know a {STAFF_ROLE_NAME} mod who deserves **{SENIOR_STAFF_ROLE_NAME}**?\n"
                         "Click below, name them + why. Others upvote/downvote.\n"
                         "Seniors review top nominations with Promote/Dismiss."),
            color=discord.Color.gold())
        await rec_ch.send(embed=embed, view=RecommendView())
        await interaction.followup.send(f"Done! Recommendations in {rec_ch.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Missing Manage Channels permission.", ephemeral=True)
    except Exception as e:
        print(f"setup-recommend-senior failed: {type(e).__name__}: {e}")
        await interaction.followup.send(f"Setup failed: `{type(e).__name__}: {e}`", ephemeral=True)


@bot.tree.command(name="claim", description="Claim this ticket (handling team).")
async def claim_cmd(interaction: discord.Interaction):
    if not can_handle_ticket(interaction.user, interaction.channel):
        await interaction.response.send_message("You can't claim this - report tickets are senior only.", ephemeral=True)
        return
    await interaction.response.send_message(f"{interaction.user.mention} claimed this ticket.")


@bot.tree.command(name="close", description="Close this ticket (handling team).")
async def close_cmd(interaction: discord.Interaction,
                    reason: str = "resolved"):
    if not can_handle_ticket(interaction.user, interaction.channel) and \
       not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("Only the handling team can close this.", ephemeral=True)
        return
    await interaction.response.send_message(f"Closing ({reason}) in 5s - logging transcript.")
    try:
        await send_transcript(interaction.channel, requester=interaction.user)
    except Exception:
        pass
    await asyncio.sleep(5)
    try:
        await interaction.channel.delete(reason=f"Closed by {interaction.user}: {reason}")
    except discord.HTTPException:
        pass


@bot.tree.command(name="add", description="Add a user to this ticket (handling team).")
async def add_cmd(interaction: discord.Interaction, member: discord.Member):
    if not can_handle_ticket(interaction.user, interaction.channel) and \
       not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("Only the handling team can add people.", ephemeral=True)
        return
    await interaction.channel.set_permissions(member, view_channel=True, send_messages=True,
                                              read_message_history=True, attach_files=True)
    await interaction.response.send_message(f"Added {member.mention}.")


@bot.tree.command(name="transcript", description="Save transcript of this ticket.")
async def transcript_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    f = await send_transcript(interaction.channel, requester=interaction.user)
    await interaction.followup.send("Saved to log channel:", file=f, ephemeral=True)


@app_commands.checks.has_permissions(ban_members=True)
@bot.tree.command(name="ban", description="Restrict user to #appeal only, optionally timed (no kick). Staff.")
@app_commands.describe(member="Who to restrict", reason="Logged reason",
                       duration="Optional: 30m, 2h, 7d, 1w. Empty = permanent.")
async def ban_cmd(interaction: discord.Interaction, member: discord.Member,
                  reason: str = "No reason given", duration: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        try:
            dur_sec = parse_duration(duration)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.followup.send("You can't ban yourself.", ephemeral=True)
            return
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            await interaction.followup.send("Can't jail admins/managers.", ephemeral=True)
            return
        guild = interaction.guild
        banned, appeal_ch = await ensure_appeal_channel(guild)
        me = guild.me
        if banned.position >= me.top_role.position:
            await interaction.followup.send(
                f"Move my role above `{BANNED_ROLE_NAME}` (Server Settings > Roles), then retry.",
                ephemeral=True)
            return
        # save current roles for restore (+ optional expiry)
        data = load_banned()
        entry = {"roles": [r.id for r in member.roles if r != guild.default_role and r != banned],
                 "reason": reason, "by": interaction.user.id, "at": time.time()}
        if dur_sec is not None:
            entry["expires_at"] = time.time() + dur_sec
        data[str(member.id)] = entry
        save_banned(data)
        # strip roles, add Banned
        try:
            await member.remove_roles(*[r for r in member.roles
                                        if r != guild.default_role and r != banned and r.position < me.top_role.position],
                                      reason=f"Appeal-jail by {interaction.user}: {reason}")
        except discord.Forbidden:
            pass
        await member.add_roles(banned, reason=f"Appeal-jail by {interaction.user}: {reason}")
        # hide EVERYTHING except #appeal at role level (including old ticket channels)
        for ch in guild.channels:
            if ch.id == appeal_ch.id:
                continue
            try:
                await ch.set_permissions(banned, view_channel=False, reason="appeal-jail lockdown")
            except (discord.Forbidden, discord.HTTPException):
                continue
        # member-level: hide their non-appeal tickets (member allow beats role deny,
        # so role deny alone is NOT enough - must remove their explicit access)
        for ch in user_open_tickets(guild, member.id):
            n = ch.name.lower()
            if n.startswith("appeal-") or n.startswith("claimed-appeal-"):
                try:
                    await ch.set_permissions(member, view_channel=True, send_messages=True,
                                             read_message_history=True, attach_files=True)
                except (discord.Forbidden, discord.HTTPException):
                    pass
                continue
            try:
                await ch.set_permissions(member, view_channel=False, send_messages=False,
                                         read_message_history=False, reason="appeal-jail: hide non-appeal tickets")
                await ch.send(f"🔒 {member.display_name}'s access removed (appeal-jailed by {interaction.user}).")
            except (discord.Forbidden, discord.HTTPException):
                continue
        # make sure appeal stays visible (no public ping - panel is already there)
        await appeal_ch.set_permissions(banned, view_channel=True, send_messages=False,
                                        read_message_history=True)
        # silent notify: DM only, nothing public with their name/reason
        length_txt = f" for {format_duration(dur_sec)}" if dur_sec else " permanently"
        try:
            await member.send(f"You have been restricted in **{guild.name}**{length_txt}.\n"
                              f"Reason: {reason}\n"
                              f"You can only see the appeal channel. Use the **Appeal Ban** button there.")
            dm_ok = True
        except (discord.Forbidden, discord.HTTPException):
            dm_ok = False
        log_ch = actions_log(guild)
        if log_ch:
            await log_ch.send(f"🔒 {interaction.user.mention} jailed {member.mention} ({member.id}){length_txt}. "
                              f"Reason: {reason} {'(DM sent)' if dm_ok else '(DMs closed - tell them to check #appeal)'}")
        await interaction.followup.send(
            f"Jailed {member.mention}{length_txt} - they now see only {appeal_ch.mention} (+ their appeal tickets). "
            f"{'DM sent.' if dm_ok else 'DMs closed - they will see it when they check appeal.'}", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            "Missing permissions. I need Manage Roles + Manage Channels and my role above Banned.",
            ephemeral=True)
    except Exception as e:
        print(f"/ban failed: {type(e).__name__}: {e}")
        await interaction.followup.send(f"Ban failed: `{type(e).__name__}: {e}`", ephemeral=True)


async def do_unjail(guild: discord.Guild, member: discord.Member, actor: str) -> int:
    """Remove Banned, restore saved roles + ticket access. Returns roles restored."""
    banned = discord.utils.get(guild.roles, name=BANNED_ROLE_NAME)
    try:
        if banned and banned in getattr(member, "roles", []):
            await member.remove_roles(banned, reason=f"Unjailed by {actor}")
    except (discord.Forbidden, discord.HTTPException):
        pass
    data = load_banned()
    saved = data.pop(str(member.id), None)
    save_banned(data)
    restored = 0
    if saved:
        me = guild.me
        for rid in saved.get("roles", []):
            r = guild.get_role(rid)
            if r and r.position < me.top_role.position and (banned is None or r != banned):
                try:
                    await member.add_roles(r, reason=f"Unjail restore by {actor}")
                    restored += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
    for ch in guild.text_channels:
        try:
            if ch.topic and str(member.id) in ch.topic and is_ticket_channel(ch):
                await ch.set_permissions(member, overwrite=None, reason="unjail restore")
        except (discord.Forbidden, discord.HTTPException):
            continue
    return restored


@app_commands.checks.has_permissions(ban_members=True)
@bot.tree.command(name="unban", description="Release user from #appeal-only jail. Staff.")
async def unban_cmd(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        restored = await do_unjail(interaction.guild, member, str(interaction.user))
        await interaction.followup.send(
            f"Released {member.mention} ({restored} roles restored).", ephemeral=True)
        log_ch = actions_log(interaction.guild)
        if log_ch:
            await log_ch.send(f"🔓 {interaction.user.mention} released {member.mention}.")
    except Exception as e:
        print(f"/unban failed: {type(e).__name__}: {e}")
        await interaction.followup.send(f"Unban failed: `{type(e).__name__}: {e}`", ephemeral=True)


async def tempjail_sweep_loop():
    """Auto-release timed bans when expires_at passes. Also prunes expired watch notes."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = time.time()
            data = load_banned()
            for guild in bot.guilds:
                for uid, entry in list(data.items()):
                    exp = entry.get("expires_at")
                    if exp and now >= exp:
                        m = guild.get_member(int(uid))
                        if m is None:
                            try:
                                m = await guild.fetch_member(int(uid))
                            except (discord.NotFound, discord.HTTPException):
                                data.pop(uid, None)
                                continue
                        restored = await do_unjail(guild, m, "auto-release (timer)")
                        data = load_banned()
                        log_ch = actions_log(guild)
                        if log_ch:
                            try:
                                await log_ch.send(
                                    f"⏰ Auto-released {m.mention} (timed jail expired, {restored} roles restored).")
                            except (discord.Forbidden, discord.HTTPException):
                                pass
                        try:
                            await m.send(f"You have been released in **{guild.name}** - your timed restriction expired.")
                        except (discord.Forbidden, discord.HTTPException):
                            pass
            save_banned(data)
            prune_watches()
        except Exception as e:
            print(f"tempjail sweep failed: {type(e).__name__}: {e}")
        await asyncio.sleep(300)


# ---------------------------------------------------------------------------
# Staff-only member notes: /watch adds a note (default 7 days), /watches views,
# /unwatch removes. Surfaced privately in logs when they open tickets.
# ---------------------------------------------------------------------------
WATCH_FILE = os.path.join(os.path.dirname(__file__), "watches.json")
DEFAULT_WATCH_SECONDS = 7 * 86400


def load_watches():
    try:
        with open(WATCH_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_watches(data):
    try:
        with open(WATCH_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def prune_watches() -> bool:
    now = time.time()
    data = load_watches()
    changed = False
    for uid in list(data.keys()):
        notes = [n for n in data[uid] if n.get("expires_at", 0) > now]
        if notes:
            if len(notes) != len(data[uid]):
                data[uid] = notes
                changed = True
        else:
            data.pop(uid, None)
            changed = True
    if changed:
        save_watches(data)
    return changed


def active_watches(user_id: int):
    now = time.time()
    return [n for n in load_watches().get(str(user_id), []) if n.get("expires_at", 0) > now]


@app_commands.checks.has_permissions(manage_roles=True)
@bot.tree.command(name="watch", description="Staff-only note on a member, auto-expires (default 7d).")
@app_commands.describe(member="Who", note="Note text (staff-only)", duration="Optional: 1d, 7d, 30d. Default 7d.")
async def watch_cmd(interaction: discord.Interaction, member: discord.Member,
                    note: str, duration: str | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not is_staff_member(interaction.user):
        await interaction.followup.send("Support / Senior staff only.", ephemeral=True)
        return
    try:
        dur = parse_duration(duration) if duration else DEFAULT_WATCH_SECONDS
    except ValueError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return
    if dur is None:
        dur = DEFAULT_WATCH_SECONDS
    data = load_watches()
    notes = data.get(str(member.id), [])
    notes.append({"text": note.strip()[:500], "by": interaction.user.id,
                  "at": time.time(), "expires_at": time.time() + dur})
    data[str(member.id)] = notes
    save_watches(data)
    prune_watches()
    log_ch = notes_log(interaction.guild)
    if log_ch:
        e = discord.Embed(title=f"Staff note on {member.display_name}",
                          description=note.strip()[:1000], color=discord.Color.orange(),
                          timestamp=datetime.datetime.now(datetime.timezone.utc))
        e.add_field(name="Member", value=f"{member.mention} ({member.id})", inline=True)
        e.add_field(name="By", value=interaction.user.mention, inline=True)
        e.add_field(name="Expires", value=f"in {format_duration(dur)}", inline=True)
        await log_ch.send(embed=e)
    await interaction.followup.send(
        f"Noted on {member.mention}, expires in {format_duration(dur)}. View with `/watches`.",
        ephemeral=True)


@app_commands.checks.has_permissions(manage_roles=True)
@bot.tree.command(name="watches", description="View active staff-only notes on a member.")
async def watches_cmd(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not is_staff_member(interaction.user):
        await interaction.followup.send("Support / Senior staff only.", ephemeral=True)
        return
    prune_watches()
    notes = active_watches(member.id)
    if not notes:
        await interaction.followup.send(f"No active notes on {member.mention}.", ephemeral=True)
        return
    e = discord.Embed(title=f"Active notes: {member.display_name} ({len(notes)})",
                      color=discord.Color.orange())
    for i, n in enumerate(notes, 1):
        left = max(0, n["expires_at"] - time.time())
        e.add_field(name=f"#{i} by <@{n['by']}> - expires in {format_duration(left)}",
                    value=n["text"][:500], inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


@app_commands.checks.has_permissions(manage_roles=True)
@bot.tree.command(name="unwatch", description="Remove staff notes on a member (one # or all).")
@app_commands.describe(member="Who", index="Optional: note number from /watches. Empty = remove all.")
async def unwatch_cmd(interaction: discord.Interaction, member: discord.Member, index: int | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not is_staff_member(interaction.user):
        await interaction.followup.send("Support / Senior staff only.", ephemeral=True)
        return
    data = load_watches()
    notes = [n for n in data.get(str(member.id), []) if n.get("expires_at", 0) > time.time()]
    if not notes:
        await interaction.followup.send(f"No active notes on {member.mention}.", ephemeral=True)
        return
    if index is None:
        data.pop(str(member.id), None)
        msg = f"Cleared all {len(notes)} notes on {member.mention}."
    else:
        if index < 1 or index > len(notes):
            await interaction.followup.send(f"Bad index - they have {len(notes)} notes.", ephemeral=True)
            return
        notes.pop(index - 1)
        if notes:
            data[str(member.id)] = notes
        else:
            data.pop(str(member.id), None)
        msg = f"Removed note #{index} on {member.mention}."
    save_watches(data)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="note", description="Private staff note on THIS ticket (logged, customer can't see).")
@app_commands.describe(text="Note text - goes to staff logs only")
async def note_cmd(interaction: discord.Interaction, text: str):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel) or not is_ticket_channel(ch):
        await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)
        return
    if not can_handle_ticket(interaction.user, ch):
        await interaction.response.send_message("Only the handling team can add notes.", ephemeral=True)
        return
    n = ch.name.lower()
    senior_ticket = n.startswith(("report-", "claimed-report-", "appeal-", "claimed-appeal-"))
    log_ch = notes_log_for_ticket(ch.guild, ch)
    if log_ch:
        e = discord.Embed(title=f"Note on #{ch.name}",
                          description=text.strip()[:1500], color=discord.Color.teal(),
                          timestamp=datetime.datetime.now(datetime.timezone.utc))
        e.add_field(name="By", value=interaction.user.mention, inline=True)
        e.add_field(name="Ticket", value=ch.mention, inline=True)
        e.add_field(name="Topic", value=(ch.topic or "-")[:200], inline=False)
        await log_ch.send(embed=e)
    await interaction.response.send_message("Noted privately - customer can't see this.", ephemeral=True)


@app_commands.checks.has_permissions(manage_roles=True)
@bot.tree.command(name="staff", description="Manage Support / Senior Support ranks (needs Manage Roles).")
@app_commands.describe(action="What to do", member="Who", reason="Logged reason")
@app_commands.choices(action=[
    app_commands.Choice(name="Give Support", value="support"),
    app_commands.Choice(name="Give Senior Support", value="senior"),
    app_commands.Choice(name="Downgrade to Support", value="downgrade"),
    app_commands.Choice(name="Remove staff roles", value="remove"),
])
async def staff_cmd(interaction: discord.Interaction, action: str, member: discord.Member,
                    reason: str = "No reason given"):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        guild = interaction.guild
        me = guild.me
        staff = staff_role(guild)
        senior = senior_role(guild)
        if staff is None or senior is None:
            await interaction.followup.send("Run `/setup-tickets` first - staff roles don't exist yet.",
                                            ephemeral=True)
            return
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            if interaction.user.id != guild.owner_id:
                await interaction.followup.send("Can't change ranks of the owner/admins.", ephemeral=True)
                return
        if member.bot:
            await interaction.followup.send("Bots can't hold staff ranks.", ephemeral=True)
            return
        for r in (staff, senior):
            if r.position >= me.top_role.position:
                await interaction.followup.send(
                    f"Move my role above `{r.name}` (Server Settings > Roles), then retry.", ephemeral=True)
                return
        roles = getattr(member, "roles", [])
        if action == "support":
            if staff in roles:
                await interaction.followup.send(f"{member.mention} already has {staff.mention}.", ephemeral=True)
                return
            await member.add_roles(staff, reason=f"{interaction.user}: {reason}")
            msg = f"Gave {member.mention} {staff.mention}."
            log_to = actions_log(guild)
        elif action == "senior":
            added = []
            if staff not in roles:
                await member.add_roles(staff, reason=f"{interaction.user}: senior bundle: {reason}")
                added.append(staff.mention)
            if senior in roles:
                await interaction.followup.send(f"{member.mention} is already Senior.", ephemeral=True)
                return
            await member.add_roles(senior, reason=f"{interaction.user}: {reason}")
            added.append(senior.mention)
            msg = f"Promoted {member.mention} to {' + '.join(added)}."
            log_to = actions_log(guild)
        elif action == "downgrade":
            if senior not in roles:
                await interaction.followup.send(f"{member.mention} isn't Senior.", ephemeral=True)
                return
            await member.remove_roles(senior, reason=f"{interaction.user}: {reason}")
            if staff not in roles:
                await member.add_roles(staff, reason=f"{interaction.user}: downgrade keep Support")
            msg = f"Downgraded {member.mention} to {staff.mention}."
            log_to = actions_log(guild)
        else:  # remove
            gone = [r.mention for r in (staff, senior) if r in roles]
            if not gone:
                await interaction.followup.send(f"{member.mention} holds no staff roles.", ephemeral=True)
                return
            await member.remove_roles(*[r for r in (staff, senior) if r in roles],
                                      reason=f"{interaction.user}: {reason}")
            msg = f"Removed {member.mention} from {' + '.join(gone)}."
            log_to = actions_log(guild)
        if log_to:
            await log_ch_send_safe(log_to, f"🎖️ {interaction.user.mention} {msg} Reason: {reason}")
        await interaction.followup.send(msg, ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            "Missing permissions - I need Manage Roles and my role above the staff roles.", ephemeral=True)
    except Exception as e:
        print(f"/staff failed: {type(e).__name__}: {e}")
        await interaction.followup.send(f"Failed: `{type(e).__name__}: {e}`", ephemeral=True)


async def log_ch_send_safe(ch: discord.TextChannel, text: str):
    try:
        await ch.send(text)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ---------------------------------------------------------------------------
# Idle tickets: warn after IDLE_WARN_HOURS, auto-close after IDLE_CLOSE_HOURS
# ---------------------------------------------------------------------------
IDLE_FILE = os.path.join(os.path.dirname(__file__), "idle.json")

# ---------------------------------------------------------------------------
# FAQ / Knowledge base
# ---------------------------------------------------------------------------
FAQ_FILE = os.path.join(os.path.dirname(__file__), "faq.json")


def load_idle():
    try:
        with open(IDLE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_idle(data):
    try:
        with open(IDLE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FAQ / Knowledge base
# ---------------------------------------------------------------------------
def load_faq():
    try:
        with open(FAQ_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_faq(data):
    try:
        with open(FAQ_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


async def last_human_activity(channel: discord.TextChannel):
    """Newest non-bot message, or None if the ticket has no human messages yet."""
    try:
        async for m in channel.history(limit=20):
            if not m.author.bot:
                return m
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


async def close_ticket_silently(channel: discord.TextChannel, reason: str):
    try:
        text = await build_transcript(channel)
        try:
            uid = int((channel.topic or "").split("user=")[1].split()[0].strip("|, "))
            owner = channel.guild.get_member(uid)
            if owner:
                buf = io.BytesIO(f"Ticket #{channel.name} ({reason})\n\n".encode() +
                                 text.encode("utf-8", errors="replace"))
                await owner.send(f"Your ticket `{channel.name}` was closed ({reason}). Transcript attached.",
                                 file=discord.File(buf, filename=f"transcript-{channel.name}.txt"))
        except Exception:
            pass
        await send_transcript(channel, requester=None)
    except Exception:
        pass
    try:
        await channel.delete(reason=reason)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def idle_sweep_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await idle_sweep_once()
        except Exception as e:
            print(f"idle sweep failed: {type(e).__name__}: {e}")
        await asyncio.sleep(max(5, IDLE_CHECK_MINUTES * 60))


async def idle_sweep_once():
    now = datetime.datetime.now(datetime.timezone.utc)
    state = load_idle()
    changed = False
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if not is_ticket_channel(ch):
                continue
            last = await last_human_activity(ch)
            if last is None:
                continue
            idle_h = (now - last.created_at).total_seconds() / 3600
            entry = state.get(str(ch.id), {})
            if idle_h >= IDLE_CLOSE_HOURS and entry.get("warned"):
                await ch.send("🔒 Closing for inactivity - transcript will be logged. "
                              "Reopen with `/ticket` if you still need help.")
                await close_ticket_silently(ch, reason="inactive")
                state.pop(str(ch.id), None)
                changed = True
            elif idle_h >= IDLE_WARN_HOURS and not entry.get("warned"):
                try:
                    # mention owner if we know them, else just warn the channel
                    mention = ""
                    try:
                        uid = int((ch.topic or "").split("user=")[1].split()[0].strip("|, "))
                        mention = f"<@{uid}> "
                    except Exception:
                        pass
                    await ch.send(f"{mention}⏳ Still need help? This ticket is idle and will "
                                  f"auto-close after {IDLE_CLOSE_HOURS:g}h of silence. Reply to keep it open.")
                    entry["warned"] = True
                    entry["warned_at"] = now.isoformat()
                    state[str(ch.id)] = entry
                    changed = True
                except (discord.Forbidden, discord.HTTPException):
                    continue
            elif idle_h < IDLE_WARN_HOURS and entry.get("warned"):
                # human came back - forgive the warning
                state.pop(str(ch.id), None)
                changed = True
    # drop entries for deleted channels
    known = {str(ch.id) for g in bot.guilds for ch in g.text_channels}
    for cid in list(state.keys()):
        if cid not in known:
            state.pop(cid, None)
            changed = True
    if changed:
        save_idle(state)


# ---------------------------------------------------------------------------
# SLA sweep: warn at SLA_WARN_HOURS, close at SLA_CLOSE_HOURS (staff no-response)
# ---------------------------------------------------------------------------
async def sla_sweep_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await sla_sweep_once()
        except Exception as e:
            print(f"SLA sweep failed: {type(e).__name__}: {e}")
        await asyncio.sleep(3600)  # hourly


async def sla_sweep_once():
    now = datetime.datetime.now(datetime.timezone.utc)
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if not is_ticket_channel(ch):
                continue
            # Check last staff message vs now
            last_staff_msg = None
            async for m in ch.history(limit=50):
                if not m.author.bot and is_staff_member(m.author):
                    last_staff_msg = m
                    break
            if last_staff_msg is None:
                # No staff response yet - check ticket creation time
                created = ch.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=datetime.timezone.utc)
                age_h = (now - created).total_seconds() / 3600
                if age_h >= SLA_CLOSE_HOURS:
                    await ch.send("🔒 Closing - no staff response within SLA. Transcript logged.")
                    await close_ticket_silently(ch, reason="SLA breach (no staff response)")
                elif age_h >= SLA_WARN_HOURS:
                    try:
                        await ch.send(f"⚠️ SLA warning: No staff response in {SLA_WARN_HOURS:g}h. "
                                      f"Will auto-close at {SLA_CLOSE_HOURS:g}h.")
                    except Exception:
                        pass


@app_commands.checks.has_permissions(manage_guild=True)
@bot.tree.command(name="restart", description="Restart the bot to pick up updates (admin).")
async def restart_cmd(interaction: discord.Interaction):
    """In-Discord update: /restart re-runs bot.py in place. ~5s downtime."""
    await interaction.response.send_message("Restarting to pick up updates...", ephemeral=True)
    await asyncio.sleep(1)
    try:
        await bot.close()
    except Exception:
        pass
    # Re-exec same interpreter + same file (works for .py and .exe)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Pong! {round(bot.latency*1000)}ms", ephemeral=True)


# ---------------------------------------------------------------------------
# /profile - User profile card
# ---------------------------------------------------------------------------
@bot.tree.command(name="profile", description="View user profile: tickets, notes, bans, join date.")
@app_commands.describe(member="User to view (defaults to you)")
async def profile_cmd(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    await interaction.response.defer(ephemeral=True, thinking=True)
    
    guild = interaction.guild
    opens = user_open_tickets(guild, target.id)
    prune_watches()
    notes = active_watches(target.id)
    banned_data = load_banned()
    banned_info = banned_data.get(str(target.id))
    
    e = discord.Embed(title=f"Profile: {target.display_name}",
                      color=discord.Color.blurple(),
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="User", value=f"{target.mention} ({target.id})", inline=True)
    e.add_field(name="Joined", value=f"<t:{int(target.joined_at.timestamp())}:R>" if target.joined_at else "Unknown", inline=True)
    e.add_field(name="Created", value=f"<t:{int(target.created_at.timestamp())}:R>", inline=True)
    e.add_field(name="Open Tickets", value=str(len(opens)), inline=True)
    e.add_field(name="Active Notes", value=str(len(notes)), inline=True)
    e.add_field(name="Banned/Jailed", value="Yes" if banned_info else "No", inline=True)
    if banned_info:
        e.add_field(name="Ban Reason", value=banned_info.get("reason", "Unknown")[:500], inline=False)
        if banned_info.get("expires_at"):
            e.add_field(name="Expires", value=f"<t:{int(banned_info['expires_at'])}:R>", inline=True)
    if notes:
        e.add_field(name="Recent Notes", value="\n".join(f"• {n['text'][:100]}" for n in notes[:3]), inline=False)
    if opens:
        e.add_field(name="Open Ticket Channels", value=", ".join(c.mention for c in opens[:5]), inline=False)
    
    await interaction.followup.send(embed=e, ephemeral=True)


# ---------------------------------------------------------------------------
# /audit - Audit log viewer
# ---------------------------------------------------------------------------
@app_commands.checks.has_permissions(view_audit_log=True)
@bot.tree.command(name="audit", description="View recent audit log entries for a user (needs View Audit Log).")
@app_commands.describe(member="User to audit", limit="How many entries (max 25)")
async def audit_cmd(interaction: discord.Interaction, member: discord.Member, limit: int = 10):
    await interaction.response.defer(ephemeral=True, thinking=True)
    limit = min(max(1, limit), 25)
    entries = []
    try:
        async for entry in interaction.guild.audit_logs(limit=100, user=member):
            if len(entries) >= limit:
                break
            ts = entry.created_at.strftime("%Y-%m-%d %H:%M")
            target = entry.target.mention if hasattr(entry.target, "mention") else str(entry.target)
            action = str(entry.action).split(".")[-1].replace("_", " ").title()
            entries.append(f"`{ts}` **{action}** → {target} ({entry.reason or 'No reason'})")
    except discord.Forbidden:
        await interaction.followup.send("Missing View Audit Log permission.", ephemeral=True)
        return
    except Exception as ex:
        await interaction.followup.send(f"Error: `{type(ex).__name__}: {ex}`", ephemeral=True)
        return
    
    if not entries:
        await interaction.followup.send(f"No audit log entries for {member.mention}.", ephemeral=True)
        return
    
    e = discord.Embed(title=f"Audit Log: {member.display_name}",
                      description="\n".join(entries),
                      color=discord.Color.gold(),
                      timestamp=datetime.datetime.now(datetime.timezone.utc))
    await interaction.followup.send(embed=e, ephemeral=True)


# ---------------------------------------------------------------------------
# FAQ / Knowledge base
# ---------------------------------------------------------------------------
class FAQModal(discord.ui.Modal):
    def __init__(self, edit_key: str | None = None):
        super().__init__(title="Add FAQ Entry" if edit_key is None else f"Edit FAQ: {edit_key}")
        self.edit_key = edit_key
        existing = load_faq().get(edit_key, {}) if edit_key else {}
        self.question = discord.ui.TextInput(label="Question / Keyword", default=existing.get("q", ""), max_length=100, required=True)
        self.answer = discord.ui.TextInput(label="Answer", style=discord.TextStyle.paragraph, default=existing.get("a", ""), max_length=2000, required=True)
        self.category = discord.ui.TextInput(label="Category (optional)", default=existing.get("cat", ""), max_length=50, required=False)
        self.add_item(self.question)
        self.add_item(self.answer)
        self.add_item(self.category)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        data = load_faq()
        key = self.question.value.strip().lower()
        data[key] = {
            "q": self.question.value.strip(),
            "a": self.answer.value.strip(),
            "cat": self.category.value.strip() or "General",
            "by": interaction.user.id,
            "at": time.time()
        }
        save_faq(data)
        await interaction.response.send_message(f"FAQ entry **{key}** saved.", ephemeral=True)


class FAQView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Search FAQ", style=discord.ButtonStyle.primary, custom_id="faq_search")
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FAQSearchModal())

    @discord.ui.button(label="Add Entry", style=discord.ButtonStyle.success, custom_id="faq_add")
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_modal(FAQModal())

    @discord.ui.button(label="Delete Entry", style=discord.ButtonStyle.danger, custom_id="faq_delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_modal(FAQDeleteModal())

    @discord.ui.button(label="List All", style=discord.ButtonStyle.secondary, custom_id="faq_list")
    async def list_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_faq()
        if not data:
            await interaction.response.send_message("No FAQ entries yet.", ephemeral=True)
            return
        cats = {}
        for k, v in data.items():
            cat = v.get("cat", "General")
            cats.setdefault(cat, []).append((k, v))
        
        e = discord.Embed(title="FAQ / Knowledge Base", color=discord.Color.blurple())
        for cat, items in sorted(cats.items()):
            val = "\n".join(f"`{v['q']}`" for k, v in items[:10])
            if len(items) > 10:
                val += f"\n... and {len(items) - 10} more"
            e.add_field(name=cat, value=val, inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


class FAQDeleteModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Delete FAQ Entry")
        self.key = discord.ui.TextInput(label="Entry Keyword/Question (exact)", max_length=100, required=True,
                                        placeholder="Type the exact question/keyword to delete")
        self.confirm = discord.ui.TextInput(label="Type DELETE to confirm", max_length=6, required=True)
        self.add_item(self.key)
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_staff_member(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        if self.confirm.value.strip().upper() != "DELETE":
            await interaction.response.send_message("Confirmation failed - type DELETE exactly.", ephemeral=True)
            return
        data = load_faq()
        k = self.key.value.strip().lower()
        if k not in data:
            await interaction.response.send_message(f"No entry found for `{self.key.value}`.", ephemeral=True)
            return
        del data[k]
        save_faq(data)
        await interaction.response.send_message(f"Deleted FAQ entry **{k}**.", ephemeral=True)


class FAQSearchModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Search FAQ")
        self.query = discord.ui.TextInput(label="Search term", max_length=100, required=True)
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        q = self.query.value.strip().lower()
        data = load_faq()
        results = [(k, v) for k, v in data.items() if q in k or q in v.get("q", "").lower() or q in v.get("a", "").lower()]
        if not results:
            await interaction.response.send_message("No matches found.", ephemeral=True)
            return
        e = discord.Embed(title=f"FAQ Results for '{self.query.value}'", color=discord.Color.green())
        for k, v in results[:10]:
            e.add_field(name=v['q'], value=v['a'][:500], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="faq", description="Search or browse the FAQ (staff can add/edit).")
async def faq_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("FAQ / Knowledge Base", view=FAQView(), ephemeral=True)


# ---------------------------------------------------------------------------
# Welcome DM (no verification role)
# ---------------------------------------------------------------------------
async def send_welcome_dm(member: discord.Member):
    if not WELCOME_DM_ENABLED:
        return
    try:
        e = discord.Embed(title=f"Welcome to {member.guild.name}!",
                          description=f"Hi {member.mention}! Welcome to **{member.guild.name}**.\n\nEnjoy your stay!",
                          color=discord.Color.green())
        await member.send(embed=e)
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_member_join(member: discord.Member):
    await send_welcome_dm(member)


# ---------------------------------------------------------------------------
# attachments re-uploaded, so deleting "evidence" doesn't destroy it.
# Both staff tiers can READ #message-log; neither can send or delete there.
# ---------------------------------------------------------------------------
async def _save_attachments(files_in) -> list:
    """Download attachments and return discord.File copies. Skips oversized ones."""
    out = []
    for a in files_in or []:
        try:
            data = await a.read()
        except Exception:
            continue
        if not data or len(data) > 8_000_000:
            continue
        try:
            out.append(discord.File(io.BytesIO(data), filename=a.filename or "attachment"))
        except Exception:
            continue
    return out


@bot.event
async def on_message_delete(message: discord.Message):
    try:
        if message.guild is None:
            return
        if getattr(message.author, "bot", False):
            return
        if _is_staff_log_channel(message.channel):
            return
        log_ch = message_log(message.guild)
        if log_ch is None or message.channel.id == log_ch.id:
            return
        files = await _save_attachments(getattr(message, "attachments", []))
        e = discord.Embed(title=f"Message deleted in #{message.channel.name}",
                          color=discord.Color.red(),
                          timestamp=datetime.datetime.now(datetime.timezone.utc))
        e.add_field(name="Author", value=f"{message.author.mention} ({message.author} | {message.author.id})", inline=False)
        e.add_field(name="Content", value=(message.content or "(no text)")[:1000], inline=False)
        if getattr(message, "attachments", []):
            names = ", ".join(f"{a.filename} ({a.size // 1024}KB)" for a in message.attachments)[:500]
            kept = f"{len(files)}/{len(message.attachments)} re-uploaded below"
            big = " (too large to keep - see original link before it expires)" if len(files) < len(message.attachments) else ""
            e.add_field(name="Attachments", value=f"{names}\n{kept}{big}", inline=False)
        e.set_footer(text=f"msg_id={message.id} | {message.created_at.strftime('%Y-%m-%d %H:%M') if message.created_at else ''}")
        await log_ch.send(embed=e, files=files or None)
    except Exception as ex:
        print(f"message-log delete failed: {type(ex).__name__}: {ex}")


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    try:
        if after.guild is None:
            return
        if getattr(after.author, "bot", False):
            return
        if _is_staff_log_channel(after.channel):
            return
        if (before.content or "") == (after.content or ""):
            return  # embed-only update, not a real edit
        log_ch = message_log(after.guild)
        if log_ch is None or after.channel.id == log_ch.id:
            return
        e = discord.Embed(title=f"Message edited in #{after.channel.name}",
                          color=discord.Color.orange(),
                          timestamp=datetime.datetime.now(datetime.timezone.utc))
        e.add_field(name="Author", value=f"{after.author.mention} ({after.author} | {after.author.id})", inline=False)
        e.add_field(name="Before", value=(before.content or "(no text)")[:1000], inline=False)
        e.add_field(name="After", value=(after.content or "(no text)")[:1000], inline=False)
        e.add_field(name="Jump", value=f"[Go to message]({after.jump_url})", inline=False)
        e.set_footer(text=f"msg_id={after.id}")
        await log_ch.send(embed=e)
    except Exception as ex:
        print(f"message-log edit failed: {type(ex).__name__}: {ex}")


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    try:
        guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        log_ch = message_log(guild)
        if log_ch is None or payload.channel_id == log_ch.id:
            return
        ch = guild.get_channel(payload.channel_id)
        if ch is not None and _is_staff_log_channel(ch):
            return
        where = f"#{ch.name}" if ch else f"channel {payload.channel_id}"
        e = discord.Embed(title=f"{len(payload.message_ids)} messages bulk-deleted in {where}",
                          description="Bulk deletes (purge/clean) don't include content - "
                                      "individual deletes above are the detailed record.",
                          color=discord.Color.dark_red(),
                          timestamp=datetime.datetime.now(datetime.timezone.utc))
        e.set_footer(text="ids: " + ", ".join(str(i) for i in list(payload.message_ids)[:10]))
        await log_ch.send(embed=e)
    except Exception as ex:
        print(f"message-log bulk failed: {type(ex).__name__}: {ex}")


@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    bot.add_view(AppealOnlyView())
    bot.add_view(TicketControlView())
    bot.add_view(RecommendView())
    bot.add_view(RecVoteView())
    if not getattr(bot, "_idle_task_started", False):
        bot._idle_task_started = True
        bot.loop.create_task(idle_sweep_loop())
        bot.loop.create_task(tempjail_sweep_loop())
        bot.loop.create_task(sla_sweep_loop())
    try:
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=g)
            await bot.tree.sync(guild=g)
        else:
            await bot.tree.sync()
        print(f"Logged in as {bot.user} - commands synced.")
    except Exception as e:
        print(f"Command sync failed: {e}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set.")
    # Optional keep-alive web server for cloud hosts that require a PORT
    # (Render/Railway/Fly health checks). No new dependencies.
    _port = os.getenv("PORT", "").strip()
    if _port.isdigit():
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _Ping(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        threading.Thread(
            target=HTTPServer(("0.0.0.0", int(_port)), _Ping).serve_forever,
            daemon=True).start()
        print(f"Keep-alive HTTP on PORT {_port}")
    bot.run(TOKEN)
