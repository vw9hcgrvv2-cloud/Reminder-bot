import os
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Button, View
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
from typing import Dict, Set

TOKEN = os.getenv("TOKEN")

POST_CHANNEL_ID = 1528940157024206899
VOTING_CHANNEL_ID = 1524445184853803069
EVENT_ROLE_NAME = "Event Ping!"

TZ = ZoneInfo("Europe/London")

INTENTS = discord.Intents.default()
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# Simple persistence (optional)
PERSISTENCE_FILE = "attendance_state.json"

def load_state():
    if not os.path.exists(PERSISTENCE_FILE):
        return {"coming": [], "maybe": [], "not_coming": []}
    try:
        with open(PERSISTENCE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"coming": [], "maybe": [], "not_coming": []}

def save_state(state):
    try:
        with open(PERSISTENCE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

attendance_state = load_state()  # dict with lists of user IDs as strings

# Persistent Event Ping toggle button
class EventPingButton(View):
    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)
        self.add_item(ToggleEventPingButton())

    async def on_timeout(self):
        # Optional: auto-remove if not persistent
        pass

class ToggleEventPingButton(Button):
    def __init__(self):
        super().__init__(label="🔔", style=discord.ButtonStyle.primary, custom_id="toggle_event_ping")

    async def callback(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            member = interaction.user

            role = discord.utils.get(guild.roles, name=EVENT_ROLE_NAME)
            if role is None:
                await interaction.response.send_message("Event Ping! role not found.", ephemeral=True)
                return

            if role in member.roles:
                await member.remove_roles(role)
                await interaction.response.send_message("Event Ping! role removed. You will no longer receive reminders.", ephemeral=True)
            else:
                await member.add_roles(role)
                await interaction.response.send_message("Event Ping! role added. You will start receiving reminders again.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Error handling button: {e}", ephemeral=True)

# Attendance widgets
class GoingButton(Button):
    def __init__(self):
        super().__init__(label="✅ I’ll Be There", style=discord.ButtonStyle.success, custom_id="going")

    async def callback(self, interaction: discord.Interaction):
        await handle_attendance(interaction, "going")

class MaybeButton(Button):
    def __init__(self):
        super().__init__(label="🤷 Maybe", style=discord.ButtonStyle.secondary, custom_id="maybe")

    async def callback(self, interaction: discord.Interaction):
        await handle_attendance(interaction, "maybe")

class CantGoButton(Button):
    def __init__(self):
        super().__init__(label="❌ I Can’t Do It", style=discord.ButtonStyle.danger, custom_id="cant")

    async def callback(self, interaction: discord.Interaction):
        await handle_attendance(interaction, "cant")

class AttendanceView(View):
    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)
        self.add_item(GoingButton())
        self.add_item(MaybeButton())
        self.add_item(CantGoButton())

async def handle_attendance(interaction: discord.Interaction, choice: str):
    try:
        member = interaction.user
        user_id = str(member.id)

        # Normalize: remove from all lists
        for key in ["coming", "maybe", "not_coming"]:
            if user_id in attendance_state.get(key, []):
                attendance_state[key].remove(user_id)

        # Add to chosen list
        if choice == "going":
            attendance_state.setdefault("coming", []).append(user_id)
        elif choice == "maybe":
            attendance_state.setdefault("maybe", []).append(user_id)
        else:
            attendance_state.setdefault("not_coming", []).append(user_id)

        # Persist
        save_state(attendance_state)

        # Update embed on poll message if present
        if interaction.message:
            embed = build_attendance_embed(interaction.guild)
            await interaction.message.edit(embed=embed)
        await interaction.response.defer()
        # Update role logic: emit in DM to user about role status
        role = interaction.guild and discord.utils.get(interaction.guild.roles, name=EVENT_ROLE_NAME)
        if role:
            if choice in ("going", "maybe"):
                if role not in member.roles:
                    await member.add_roles(role)
            else:
                if role in member.roles:
                    await member.remove_roles(role)
        # Optional: acknowledge
    except Exception as e:
        await interaction.response.send_message(f"Error updating attendance: {e}", ephemeral=True)

def build_attendance_embed(guild: discord.Guild) -> discord.Embed:
    coming_ids = attendance_state.get("coming", [])
    maybe_ids = attendance_state.get("maybe", [])
    not_ids = attendance_state.get("not_coming", [])

    def names_from(ids):
        names = []
        for uid in ids:
            member = guild.get_member(int(uid))
            if member:
                names.append(str(member))
            else:
                names.append(f"User#{uid}")
        return "\n".join(names) if names else "Nobody"

    embed = discord.Embed(title="Tournament Attendance", color=0x00ff00)
    embed.add_field(name="✅ Coming", value=names_from(coming_ids), inline=False)
    embed.add_field(name="🤷 Maybe", value=names_from(maybe_ids), inline=False)
    embed.add_field(name="❌ Not Coming", value=names_from(not_ids), inline=False)
    return embed

# Global views cache for persistence
def get_attendance_view():
    return AttendanceView(timeout=None)

# Post a daily poll
poll_message_id: int | None = None

async def post_daily_poll():
    channel = bot.get_channel(POST_CHANNEL_ID)
    if channel is None:
        return
    guild = channel.guild
    embed = discord.Embed(title="🏆 Tournament Tomorrow - 3:00 PM", color=0xFFD700)
    embed.description = "1v1 Tournament\nCompetitive Rules\nFT3 (First to 3)"
    embed.set_footer(text="Please indicate your attendance:")

    view = get_attendance_view()

    try:
        # Mention Everyone explicitly in content
        msg = await channel.send(content="@everyone Tournament Tomorrow details:", embed=embed, view=view)
        global poll_message_id
        poll_message_id = msg.id
        # Persist state when poll is created
        save_state(attendance_state)
    except Exception as e:
        print(f"Failed to post poll: {e}")

def attendance_message_exists(guild: discord.Guild) -> bool:
    if poll_message_id is None:
        return False
    # Optional: verify message exists
    return True

# Handlers and scheduled tasks
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    # Register persistent view for the toggle button
    bot.add_view(EventPingButton())

    # Ensure attendance view is persistent by recreating on poll send
    # Start loops if not running
    if not post_message.is_running():
        post_message.start()

    if not daily_poll.is_running():
        daily_poll.start()

    if not reset_ping.is_running():
        reset_ping.start()

# 15-minute Event Ping reminder
@tasks.loop(minutes=15)
async def post_message():
    channel = bot.get_channel(POST_CHANNEL_ID)
    if channel is None:
        return
    guild = channel.guild
    role = discord.utils.get(guild.roles, name=EVENT_ROLE_NAME)
    voting_channel = guild.get_channel(VOTING_CHANNEL_ID)
    if role is None or voting_channel is None:
        return

    embed = discord.Embed(title="Event Reminder", description=f"Remember to vote in {voting_channel.mention}.")
    embed.set_footer(text="Click the 🔔 to receive or stop Event Ping! reminders.")

    try:
        msg = await channel.send(content=f"{role.mention} @here", embed=embed, view=EventPingButton())
        bot.add_view(EventPingButton())  # ensure persistence
    except Exception:
        pass

@post_message.before_loop
async def before_post():
    await bot.wait_until_ready()

# Daily poll at 18:00 Europe/London
@tasks.loop(minutes=1)
async def daily_poll():
    now = datetime.now(tz=TZ)
    target = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    wait = (target - now).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    await post_daily_poll()

@daily_poll.before_loop
async def before_daily_poll():
    await bot.wait_until_ready()

# Reset Event Ping role at 17:00 Europe/London
@tasks.loop(minutes=60)
async def reset_ping():
    now = datetime.now(tz=TZ)
    if now.hour == 17 and now.minute == 0:
        await reset_event_ping_role()

@reset_ping.before_loop
async def before_reset_ping():
    await bot.wait_until_ready()

async def reset_event_ping_role():
    channel = bot.get_channel(POST_CHANNEL_ID)
    if channel is None:
        return
    guild = channel.guild
    role = discord.utils.get(guild.roles, name=EVENT_ROLE_NAME)
    if role is None:
        return
    for member in guild.members:
        if member.bot:
            continue
        try:
            if role not in member.roles:
                await member.add_roles(role)
        except Exception:
            pass

# Run the bot
bot.run(TOKEN)
