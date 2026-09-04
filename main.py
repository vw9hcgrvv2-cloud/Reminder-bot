import os
import re
import json
import logging
import asyncio
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
TIMEZONE = ZoneInfo("Europe/London")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Fixed Channel IDs
DAILY_POLL_CHANNEL_ID = 1545264348295987250
EVENT_CHANNEL_ID_A = 1545264346265952367
EVENT_CHANNEL_ID_B = 1545264350393008169
EVENT_CHANNEL_IDS = [EVENT_CHANNEL_ID_A, EVENT_CHANNEL_ID_B]

# Configurable Settings
VOTING_DURATION_MINUTES = int(os.getenv("VOTING_DURATION_MINUTES", "60"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
DAILY_POLL_HOUR = 10
DAILY_POLL_MINUTE = 0
EVENT_ROLE_NAME = "Event"
REMINDER_INTERVAL_MINUTES = 45
FINAL_REMINDER_MINUTES = 15
MIN_TIME_LEAD_MINUTES = 15
STATE_FILE = "state.json"

# Startup Poll Protection
STARTUP_POLL_CREATED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# DATA STRUCTURES
# -----------------------------------------------------------------------------
@dataclass
class DailyVote:
    user_id: int
    username: str
    vote: Optional[bool] = None
    submitted_time: Optional[str] = None
    time_minutes: Optional[int] = None

@dataclass
class AttendanceVote:
    user_id: int
    username: str
    status: str = "no"  # yes / maybe / no

@dataclass
class DailyPollState:
    active: bool = False
    message_id: Optional[int] = None
    date_str: str = ""
    deadline: Optional[float] = None
    votes: Dict[int, DailyVote] = field(default_factory=dict)

@dataclass
class EventState:
    confirmed: bool = False
    time_minutes: Optional[int] = None
    time_str: Optional[str] = None
    attendance: Dict[int, AttendanceVote] = field(default_factory=dict)
    messages: List[Dict[str, int]] = field(default_factory=list)
    reminders_started: bool = False
    last_reminder_minutes: Optional[int] = None
    final_reminder_sent: bool = False
    started: bool = False

@dataclass
class BotState:
    daily: DailyPollState = field(default_factory=DailyPollState)
    event: EventState = field(default_factory=EventState)
    last_daily_poll_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        def deserialize_daily(d: dict) -> DailyPollState:
            votes = {int(k): DailyVote(**v) for k, v in d.get("votes", {}).items()}
            return DailyPollState(
                active=d.get("active", False),
                message_id=d.get("message_id"),
                date_str=d.get("date_str", ""),
                deadline=d.get("deadline"),
                votes=votes
            )
        def deserialize_event(d: dict) -> EventState:
            att = {int(k): AttendanceVote(**v) for k, v in d.get("attendance", {}).items()}
            return EventState(
                confirmed=d.get("confirmed", False),
                time_minutes=d.get("time_minutes"),
                time_str=d.get("time_str"),
                attendance=att,
                messages=d.get("messages", []),
                reminders_started=d.get("reminders_started", False),
                last_reminder_minutes=d.get("last_reminder_minutes"),
                final_reminder_sent=d.get("final_reminder_sent", False),
                started=d.get("started", False)
            )
        inst = cls()
        inst.daily = deserialize_daily(data.get("daily", {}))
        inst.event = deserialize_event(data.get("event", {}))
        inst.last_daily_poll_date = data.get("last_daily_poll_date", "")
        return inst

# -----------------------------------------------------------------------------
# STATE MANAGER
# -----------------------------------------------------------------------------
class StateManager:
    def __init__(self, path: str):
        self.path = path
        self.state = BotState()
        self.lock = asyncio.Lock()

    async def load(self):
        async with self.lock:
            if not os.path.exists(self.path):
                logger.info("No state file, starting fresh.")
                self.state = BotState()
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self.state = BotState.from_dict(raw)
                logger.info("State loaded successfully.")
            except Exception as e:
                logger.error(f"State load error: {e}")
                self.state = BotState()

    async def save(self):
        async with self.lock:
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self.state.to_dict(), f, indent=2)
            except Exception as e:
                logger.error(f"State save error: {e}")

state_mgr = StateManager(STATE_FILE)

# -----------------------------------------------------------------------------
# TIME UTILITIES
# -----------------------------------------------------------------------------
def now_local() -> datetime:
    return datetime.now(TIMEZONE)

def parse_time(time_str: str) -> Optional[int]:
    s = time_str.strip().lower()
    patterns = [
        r"^(\d{1,2}):(\d{2})\s*(am|pm)$", r"^(\d{1,2})\.(\d{2})\s*(am|pm)$",
        r"^(\d{1,2})\s*(am|pm)$", r"^(\d{1,2}):(\d{2})$", r"^(\d{2}):(\d{2})$"
    ]
    for p in patterns:
        m = re.match(p, s)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g) == 3:
                h, mi, per = int(g[0]), int(g[1]), g[2]
                if per == "pm" and h != 12: h += 12
                if per == "am" and h == 12: h = 0
            elif len(g) == 2:
                h, mi = int(g[0]), int(g[1])
                if not (0 <= h <= 23 and 0 <= mi <= 59): continue
            else:
                continue
            return h * 60 + mi
        except:
            continue
    return None

def format_time(total_min: int) -> str:
    h, m = divmod(total_min, 60)
    if h == 0: return f"12:{m:02d} AM" if m else "12 AM"
    if h < 12: return f"{h}:{m:02d} AM" if m else f"{h} AM"
    if h == 12: return f"12:{m:02d} PM" if m else "12 PM"
    return f"{h-12}:{m:02d} PM" if m else f"{h-12} PM"

def average_circular(times: List[int]) -> int:
    if not times: return 0
    rad = [t * (2 * math.pi / 1440) for t in times]
    avg_sin = sum(math.sin(x) for x in rad) / len(rad)
    avg_cos = sum(math.cos(x) for x in rad) / len(rad)
    avg_rad = math.atan2(avg_sin, avg_cos)
    if avg_rad < 0: avg_rad += 2 * math.pi
    return round((avg_rad * 1440) / (2 * math.pi))

def is_valid_time(total_min: int) -> bool:
    current_min = now_local().hour * 60 + now_local().minute
    return total_min >= current_min + MIN_TIME_LEAD_MINUTES and total_min < 1440

# -----------------------------------------------------------------------------
# ROLE MANAGEMENT
# -----------------------------------------------------------------------------
async def get_event_role(guild: discord.Guild) -> Optional[discord.Role]:
    return discord.utils.get(guild.roles, name=EVENT_ROLE_NAME)

async def set_event_role(member: discord.Member, enable: bool):
    role = await get_event_role(member.guild)
    if not role:
        logger.warning(f"Role '{EVENT_ROLE_NAME}' not found.")
        return
    try:
        if enable and role not in member.roles:
            await member.add_roles(role, reason="Event Attendance")
        elif not enable and role in member.roles:
            await member.remove_roles(role, reason="Event Attendance")
    except discord.Forbidden:
        logger.error(f"Missing permissions to manage role for {member}")
    except Exception as e:
        logger.error(f"Role error: {e}")

async def reset_all_event_roles(guild: discord.Guild):
    role = await get_event_role(guild)
    if not role: return
    count = 0
    for member in guild.members:
        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Daily Reset")
                count += 1
                if count % 15 == 0: await asyncio.sleep(0.5)
            except: pass
    logger.info(f"Removed role from {count} members")

# -----------------------------------------------------------------------------
# EMBED BUILDERS
# -----------------------------------------------------------------------------
def build_daily_poll_embed() -> discord.Embed:
    state = state_mgr.state
    yes_valid = [v for v in state.daily.votes.values() if v.vote is True and v.time_minutes is not None]
    yes_pending = [v for v in state.daily.votes.values() if v.vote is True and v.time_minutes is None]
    no_votes = [v for v in state.daily.votes.values() if v.vote is False]
    embed = discord.Embed(title="🏆 TOURNAMENT / EVENT TODAY?", description="Would you like an event later today?", color=0x3498db)
    embed.add_field(name=f"✅ YES — {len(yes_valid)}", value="\n".join(f"• {v.username}" for v in yes_valid) or "—", inline=True)
    if yes_pending:
        embed.add_field(name="⏳ PENDING TIME", value="\n".join(f"• {v.username}" for v in yes_pending), inline=True)
    embed.add_field(name=f"❌ NO — {len(no_votes)}", value="\n".join(f"• {v.username}" for v in no_votes) or "—", inline=True)
    if state.daily.deadline:
        closes = datetime.fromtimestamp(state.daily.deadline, TIMEZONE)
        embed.set_footer(text=f"Closes: {closes.strftime('%H:%M %Z')}")
    return embed

def build_attendance_embed() -> discord.Embed:
    state = state_mgr.state
    yes = [v.username for v in state.event.attendance.values() if v.status == "yes"]
    maybe = [v.username for v in state.event.attendance.values() if v.status == "maybe"]
    no = [v.username for v in state.event.attendance.values() if v.status == "no"]
    embed = discord.Embed(title="🏆 EVENT CONFIRMED!", color=0x2ecc71)
    embed.add_field(name="🕐 Event Time", value=state.event.time_str or "—", inline=False)
    embed.add_field(name=f"✅ YES — {len(yes)}", value="\n".join(yes) or "—", inline=True)
    embed.add_field(name=f"🤔 MAYBE — {len(maybe)}", value="\n".join(maybe) or "—", inline=True)
    embed.add_field(name=f"❌ NO — {len(no)}", value="\n".join(no) or "—", inline=True)
    return embed

# -----------------------------------------------------------------------------
# SYNCHRONIZED UPDATES
# -----------------------------------------------------------------------------
async def update_attendance_messages():
    state = state_mgr.state
    if not state.event.messages: return
    embed = build_attendance_embed()
    guild = bot.get_guild(GUILD_ID)
    if not guild: return
    valid = []
    for entry in state.event.messages:
        ch = guild.get_channel(entry["channel_id"])
        if not ch: continue
        try:
            msg = await ch.fetch_message(entry["message_id"])
            view = AttendanceView() if not state.event.started else None
            await msg.edit(embed=embed, view=view)
            valid.append(entry)
        except discord.NotFound:
            logger.warning(f"Message {entry['message_id']} missing")
        except Exception as e:
            logger.error(f"Update error: {e}")
    state.event.messages = valid
    await state_mgr.save()

async def update_daily_poll():
    state = state_mgr.state
    if not state.daily.message_id: return
    guild = bot.get_guild(GUILD_ID)
    ch = guild.get_channel(DAILY_POLL_CHANNEL_ID) if guild else None
    if not ch: return
    try:
        msg = await ch.fetch_message(state.daily.message_id)
        await msg.edit(embed=build_daily_poll_embed())
    except Exception as e:
        logger.error(f"Daily poll update: {e}")

# -----------------------------------------------------------------------------
# VIEWS & MODALS
# -----------------------------------------------------------------------------
class TimeInputModal(ui.Modal, title="Submit Event Time"):
    time_str = ui.TextInput(label="Event Time", placeholder="e.g. 3:00 PM / 15:00", required=True)

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        state = state_mgr.state
        if not state.daily.active:
            await interaction.response.send_message("❌ Poll closed.", ephemeral=True)
            return
        minutes = parse_time(self.time_str.value)
        if not minutes:
            await interaction.response.send_message("❌ Invalid format.", ephemeral=True)
            return
        if not is_valid_time(minutes):
            await interaction.response.send_message("❌ Time is past or too soon.", ephemeral=True)
            return
        vote = state.daily.votes.get(self.user_id)
        if not vote or vote.vote is not True:
            await interaction.response.send_message("❌ Select YES first.", ephemeral=True)
            return
        vote.submitted_time = self.time_str.value
        vote.time_minutes = minutes
        await state_mgr.save()
        await interaction.response.send_message("✅ Time saved.", ephemeral=True)
        await update_daily_poll()

class DailyPollView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        state = state_mgr.state
        if not state.daily.active or (state.daily.deadline and i.created_at.timestamp() > state.daily.deadline):
            await i.response.send_message("🔒 Voting closed.", ephemeral=True)
            return False
        return True

    @ui.button(label="✅ YES", style=discord.ButtonStyle.Green, custom_id="poll:yes")
    async def yes_btn(self, i: discord.Interaction, b: ui.Button):
        state = state_mgr.state
        uid = i.user.id
        state.daily.votes[uid] = DailyVote(user_id=uid, username=i.user.display_name, vote=True)
        await state_mgr.save()
        await i.response.send_message("✅ YES selected. Submit time below.", ephemeral=True, view=TimeSubmitView(uid))
        await update_daily_poll()

    @ui.button(label="❌ NO", style=discord.ButtonStyle.Red, custom_id="poll:no")
    async def no_btn(self, i: discord.Interaction, b: ui.Button):
        state = state_mgr.state
        uid = i.user.id
        state.daily.votes[uid] = DailyVote(user_id=uid, username=i.user.display_name, vote=False)
        await state_mgr.save()
        await i.response.send_message("❌ NO recorded.", ephemeral=True)
        await update_daily_poll()

class TimeSubmitView(ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=None)
        self.uid = uid

    @ui.button(label="🕐 Submit Time", style=discord.ButtonStyle.Primary, custom_id="time:submit")
    async def submit_btn(self, i: discord.Interaction, b: ui.Button):
        if i.user.id != self.uid:
            await i.response.send_message("❌ Not your button.", ephemeral=True)
            return
        await i.response.send_modal(TimeInputModal(self.uid))

class AttendanceView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        state = state_mgr.state
        if not state.event.confirmed or state.event.started:
            await i.response.send_message("❌ Event unavailable.", ephemeral=True)
            return False
        return True

    @ui.button(label="✅ YES", style=discord.ButtonStyle.Green, custom_id="att:yes")
    async def yes_btn(self, i: discord.Interaction, b: ui.Button):
        state = state_mgr.state
        uid = i.user.id
        state.event.attendance[uid] = AttendanceVote(user_id=uid, username=i.user.display_name, status="yes")
        await set_event_role(i.user, True)
        await state_mgr.save()
        await i.response.send_message("✅ Attending.", ephemeral=True)
        await update_attendance_messages()

    @ui.button(label="🤔 MAYBE", style=discord.ButtonStyle.Blurple, custom_id="att:maybe")
    async def maybe_btn(self, i: discord.Interaction, b: ui.Button):
        state = state_mgr.state
        uid = i.user.id
        state.event.attendance[uid] = AttendanceVote(user_id=uid, username=i.user.display_name, status="maybe")
        await set_event_role(i.user, True)
        await state_mgr.save()
        await i.response.send_message("🤔 Maybe attending.", ephemeral=True)
        await update_attendance_messages()

    @ui.button(label="❌ NO", style=discord.ButtonStyle.Red, custom_id="att:no")
    async def no_btn(self, i: discord.Interaction, b: ui.Button):
        state = state_mgr.state
        uid = i.user.id
        state.event.attendance[uid] = AttendanceVote(user_id=uid, username=i.user.display_name, status="no")
        await set_event_role(i.user, False)
        await state_mgr.save()
        await i.response.send_message("❌ Not attending.", ephemeral=True)
        await update_attendance_messages()

# -----------------------------------------------------------------------------
# POLL LOGIC
# -----------------------------------------------------------------------------
async def create_daily_poll(guild: discord.Guild):
    state = state_mgr.state
    channel = guild.get_channel(DAILY_POLL_CHANNEL_ID)
    if not channel:
        logger.error("Daily poll channel missing")
        return
    state.daily = DailyPollState(active=True, date_str=now_local().strftime("%Y-%m-%d"),
                                  deadline=(now_local() + timedelta(minutes=VOTING_DURATION_MINUTES)).timestamp())
    embed = build_daily_poll_embed()
    msg = await channel.send("@everyone", embed=embed, view=DailyPollView())
    state.daily.message_id = msg.id
    state.last_daily_poll_date = state.daily.date_str
    await state_mgr.save()
    logger.info(f"Daily poll created: {msg.id}")

async def close_daily_poll():
    state = state_mgr.state
    if not state.daily.active: return
    logger.info("Closing daily poll")
    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(DAILY_POLL_CHANNEL_ID) if guild else None
    if channel and state.daily.message_id:
        try:
            msg = await channel.fetch_message(state.daily.message_id)
            await msg.edit(content="🔒 @everyone\nPoll closed.", view=None)
        except: pass
    yes = [v for v in state.daily.votes.values() if v.vote is True and v.time_minutes is not None]
    no = [v for v in state.daily.votes.values() if v.vote is False]
    state.daily.active = False
    await state_mgr.save()
    if len(yes) > len(no):
        if not yes:
            if channel: await channel.send("❌ No valid times submitted.")
            return
        avg_min = average_circular([v.time_minutes for v in yes])
        state.event.confirmed = True
        state.event.time_minutes = avg_min
        state.event.time_str = format_time(avg_min)
        await state_mgr.save()
        await create_attendance_polls(guild)
    else:
        if channel: await channel.send("❌ No event scheduled today.")

async def create_attendance_polls(guild: discord.Guild):
    state = state_mgr.state
    state.event.messages.clear()
    embed = build_attendance_embed()
    for ch_id in EVENT_CHANNEL_IDS:
        ch = guild.get_channel(ch_id)
        if not ch: continue
        msg = await ch.send("@everyone", embed=embed, view=AttendanceView())
        state.event.messages.append({"channel_id": ch.id, "message_id": msg.id})
    state.event.reminders_started = True
    await state_mgr.save()

async def process_event_logic():
    state = state_mgr.state
    if not state.event.confirmed or state.event.started: return
    now = now_local()
    now_min = now.hour * 60 + now.minute
    event_min = state.event.time_minutes
    diff = event_min - now_min
    guild = bot.get_guild(GUILD_ID)
    role = await get_event_role(guild)
    mention = f"<@&{role.id}>" if role else "@Event"
    if diff <= 0:
        state.event.started = True
        await state_mgr.save()
        for entry in state.event.messages:
            ch = guild.get_channel(entry["channel_id"])
            if ch: await ch.send(f"{mention}\n🏆 EVENT STARTING NOW! Good luck! 🔥")
        await update_attendance_messages()
        return
    if diff <= FINAL_REMINDER_MINUTES and not state.event.final_reminder_sent:
        state.event.final_reminder_sent = True
        await state_mgr.save()
        for entry in state.event.messages:
            ch = guild.get_channel(entry["channel_id"])
            if ch: await ch.send(f"{mention}\n🚨 FINAL REMINDER! Starts in {diff} mins.")
        return
    if state.event.last_reminder_minutes is None or state.event.last_reminder_minutes - diff >= REMINDER_INTERVAL_MINUTES:
        state.event.last_reminder_minutes = diff
        await state_mgr.save()
        for entry in state.event.messages:
            ch = guild.get_channel(entry["channel_id"])
            if ch: await ch.send(f"{mention}\n⏰ REMINDER: Event at {state.event.time_str}")

# -----------------------------------------------------------------------------
# SCHEDULER
# -----------------------------------------------------------------------------
@tasks.loop(minutes=1)
async def scheduler():
    now = now_local()
    guild = bot.get_guild(GUILD_ID)
    if not guild: return
    if now.hour == DAILY_POLL_HOUR and now.minute == DAILY_POLL_MINUTE:
        today_str = now.strftime("%Y-%m-%d")
        if state_mgr.state.last_daily_poll_date != today_str:
            await reset_all_event_roles(guild)
            await create_daily_poll(guild)
    if state_mgr.state.daily.deadline and now.timestamp() > state_mgr.state.daily.deadline and state_mgr.state.daily.active:
        await close_daily_poll()
    await process_event_logic()

@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()

# -----------------------------------------------------------------------------
# BOT SETUP
# -----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Register Persistent Views
bot.add_view(DailyPollView())
bot.add_view(AttendanceView())

# -----------------------------------------------------------------------------
# COMMON
# -----------------------------------------------------------------------------
def is_admin(user: discord.User | discord.Member) -> bool:
    if not user.guild: return False
    if user.guild_permissions.administrator or user.guild_permissions.manage_guild:
        return True
    if ADMIN_ROLE_ID:
        return any(r.id == ADMIN_ROLE_ID for r in user.roles)
    return False

async def admin_check(i: discord.Interaction) -> bool:
    if not is_admin(i.user):
        await i.response.send_message("❌ Admin only.", ephemeral=True)
        return False
    return True

# -----------------------------------------------------------------------------
# SLASH COMMANDS
# -----------------------------------------------------------------------------
@bot.tree.command(name="eventstatus", description="Show event status")
async def cmd_status(i: discord.Interaction):
    s = state_mgr.state
    txt = f"📅 Poll Date: {s.daily.date_str or '—'}\n🗳️ Poll Active: {s.daily.active}\n✅ Confirmed: {s.event.confirmed}\n"
    if s.event.confirmed:
        txt += f"🕐 Time: {s.event.time_str}\n🔴 Started: {s.event.started}"
    await i.response.send_message(txt, ephemeral=True)

@bot.tree.command(name="eventreset", description="Reset daily poll (Admin)")
@app_commands.check(admin_check)
async def cmd_reset(i: discord.Interaction):
    state_mgr.state.daily = DailyPollState()
    await state_mgr.save()
    await i.response.send_message("✅ Poll reset.", ephemeral=True)

@bot.tree.command(name="eventstart", description="Start new poll (Admin)")
@app_commands.check(admin_check)
async def cmd_start(i: discord.Interaction):
    await create_daily_poll(i.guild)
    await i.response.send_message("✅ Poll created.", ephemeral=True)

@bot.tree.command(name="eventcancel", description="Cancel event (Admin)")
@app_commands.check(admin_check)
async def cmd_cancel(i: discord.Interaction):
    state_mgr.state.event.confirmed = False
    state_mgr.state.event.started = True
    await state_mgr.save()
    await i.response.send_message("✅ Event cancelled.", ephemeral=True)

@bot.tree.command(name="eventremind", description="Send reminder (Admin)")
@app_commands.check(admin_check)
async def cmd_remind(i: discord.Interaction):
    s = state_mgr.state
    if not s.event.confirmed or not s.event.time_str:
        await i.response.send_message("❌ No active event.", ephemeral=True)
        return
    role = await get_event_role(i.guild)
    mention = f"<@&{role.id}>" if role else "@Event"
    for entry in s.event.messages:
        ch = i.guild.get_channel(entry["channel_id"])
        if ch: await ch.send(f"{mention}\n⏰ Reminder: Event at {s.event.time_str}")
    await i.response.send_message("✅ Reminder sent.", ephemeral=True)

@bot.tree.command(name="eventrole", description="Check Event role")
async def cmd_role(i: discord.Interaction):
    role = await get_event_role(i.guild)
    if role:
        await i.response.send_message(f"✅ Found: {role.name} ({role.id})", ephemeral=True)
    else:
        await i.response.send_message(f"❌ Not found: '{EVENT_ROLE_NAME}'", ephemeral=True)

@bot.tree.command(name="botstatus", description="Bot status")
async def cmd_botstatus(i: discord.Interaction):
    txt = f"✅ Online\n🕐 London: {now_local().strftime('%Y-%m-%d %H:%M:%S')}\n🏰 Guild: {i.guild.name if i.guild else '—'}\n🗳️ Channel: {DAILY_POLL_CHANNEL_ID}"
    await i.response.send_message(txt, ephemeral=True)

@bot.tree.error
async def cmd_error(i: discord.Interaction, e: Exception):
    if isinstance(e, app_commands.MissingPermissions):
        await i.response.send_message("❌ No permission.", ephemeral=True)
    elif not i.response.is_done:
        await i.response.send_message("❌ Error.", ephemeral=True)

# -----------------------------------------------------------------------------
# STARTUP
# -----------------------------------------------------------------------------
@bot.event
async def on_ready():
    global STARTUP_POLL_CREATED
    await state_mgr.load()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        logger.error("Guild not found")
        return
    try:
        await bot.tree.sync()
        logger.info("Commands synced")
    except Exception as e:
        logger.error(f"Sync error: {e}")
    logger.info(f"Ready as {bot.user} | {TIMEZONE}")
    if not STARTUP_POLL_CREATED:
        STARTUP_POLL_CREATED = True
        logger.info("Startup poll creating...")
        await create_daily_poll(guild)
    if not scheduler.is_running():
        scheduler.start()

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    if not DISCORD_TOKEN or not GUILD_ID:
        logger.error("Missing DISCORD_TOKEN/GUILD_ID")
        return
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid token")
    except Exception as e:
        logger.error(f"Fatal: {e}")

if __name__ == "__main__":
    main()
