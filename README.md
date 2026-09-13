# Discord Ticket Bot (two-tier staff + appeal jail)

Ticket-first support bot. No auto-fix maze — good triage, private tickets, always pings the right team.

## Roles
- `Support` — handles General Support / Other tickets
- `Senior Support` — handles everything, plus private Report + Appeal tickets
- `Banned` — appeal-jail: sees only `#appeal` (+ own appeal tickets)

## Commands
- `/setup-tickets` (admin): creates `#report-a-problem` panel (Support/Report/Other buttons),
  `#appeal` (private, appeal-only button), `#support-log` + `#senior-log`, `Tickets` category,
  `Support` / `Senior Support` / `Banned` roles. Re-running cleans old panels, no duplicates.
- `/setup-recommend-senior` (admin, optional): `#recommend-senior` where members nominate
  Support mods for Senior — upvote/downvote, seniors Promote/Dismiss.
- `/ticket` + `/report`: button panel anywhere.
- In-ticket buttons: Claim / Transcript / Close (report tickets = senior only).
- `/claim /close /add /transcript` (handling team).
- `/ban @user reason` + `/unban @user` (needs Ban Members): appeal-jail, no kick.
  Ban DMs the user silently (nothing public), hides their non-appeal tickets, restricts
  them to `#appeal`. Unban restores roles + ticket access.

## Routing
- Support / Other tickets → `@Support` ping, opened/transcripts in `#support-log`
- Report User/Mod → `@Senior Support` only (hidden from regular Support), in `#senior-log`
- Appeals → in `#senior-log`; banned users can only open appeals
- `/note` + member watch notes → `#staff-notes` (notes on senior tickets stay in `#senior-log`)
- Bans, unbans, auto-releases, `/staff` rank changes, recommendation results → `#mod-actions`
- Anti-spam: max 3 open tickets each, 45s cooldown, claimed tickets counted

## Setup (5 min)
1. https://discord.com/developers/applications → New Application → Bot →
   **Reset Token** → copy it. Enable **SERVER MEMBERS INTENT** + **MESSAGE CONTENT INTENT**.
2. OAuth2 → URL Generator → scopes `bot` + `applications.commands`, perms:
   Manage Channels, Manage Roles, Ban Members, View Channels, Send Messages, Embed Links,
   Read Message History, Mention Everyone. Invite, then drag the bot role ABOVE
   Support / Senior Support / Banned (Server Settings > Roles).
3. Install + run:
```powershell
cd discord-support-bot
pip install -r requirements.txt
copy .env.example .env
# edit .env, put DISCORD_TOKEN=...
python bot.py
```
4. In Discord (as admin): `/setup-tickets`, then test the buttons.
   Optionally: `/setup-recommend-senior`.

## Customize (.env)
`STAFF_ROLE_NAME`, `SENIOR_STAFF_ROLE_NAME`, `BANNED_ROLE_NAME`, channel names,
`RECOMMEND_CHANNEL_NAME`, optional `GUILD_ID` (instant command sync while testing).

## Files
- `bot.py` — entire bot
- `requirements.txt`, `.env.example`
- Auto-created: `tickets.json` (ticket counter), `banned.json` (jail backups),
  `recommendations.json` (nomination votes)
