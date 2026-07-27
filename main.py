import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button
import asyncio
import json
from datetime import datetime, date
import pytz

# Configuration (adjust as needed)
EVENT_CHANNEL_ID = 1524445184853803069  # given by user
EVENT_CHANNEL_NAME = "1——events-📆——1"     # fallback name if needed
PING_ROLE_NAME = "Event Ping!"            # role to restore daily at 5:00 PM London time
VOTING_ROLE_NAME = "Event role"            # role added/removed by poll votes
PERSISTENCE_FILE = "votes.json"
TESTER_PERSISTENCE_FILE = "tester_polls.json"

# Timezone
LONDON_TZ = pytz.timezone("Europe/London")

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Helpers
def load_votes():
    try:
        with open(PERSISTENCE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_votes(data):
    with open(PERSISTENCE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_tester_polls():
    try:
        with open(TESTER_PERSISTENCE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_tester_polls(data):
    with open(TESTER_PERSISTENCE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_event_channel(guild: discord.Guild):
    # Prefer ID when available
    ch = guild.get_channel(EVENT_CHANNEL_ID)
    if ch and isinstance(ch, discord.TextChannel):
        return ch
    # Fallback to name lookup per guild
    for c in guild.text_channels:
        if c.name == EVENT_CHANNEL_NAME:
            return c
    return None

async def resolve_resources(guild: discord.Guild):
    # Resolve ping role by name
    ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
    voting_role = discord.utils.get(guild.roles, name=VOTING_ROLE_NAME)

    # Resolve event channel
    event_channel = get_event_channel(guild)

    return {
        "ping_role": ping_role,
        "voting_role": voting_role,
        "event_channel": event_channel
    }

# Views
class PollView(View):
    def __init__(self, guild_id: int, channel: discord.TextChannel, voting_role: discord.Role, persisted_votes: dict):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel = channel
        self.voting_role = voting_role
        self.votes = persisted_votes  # dict key: user_id -> "Yes"/"Maybe"/"No"

        self.yes_button = Button(label="Yes", style=discord.ButtonStyle.primary, custom_id="poll_yes")
        self.maybe_button = Button(label="Maybe", style=discord.ButtonStyle.secondary, custom_id="poll_maybe")
        self.no_button = Button(label="No", style=discord.ButtonStyle.danger, custom_id="poll_no")

        self.yes_button.callback = self.vote_callback
        self.maybe_button.callback = self.vote_callback
        self.no_button.callback = self.vote_callback

        self.add_item(self.yes_button)
        self.add_item(self.maybe_button)
        self.add_item(self.no_button)

        # Load existing votes for this guild/poll if needed
        self.votes_cache = {}

    async def vote_callback(self, interaction: discord.Interaction):
        if interaction.user.bot:
            await interaction.response.defer()
            return

        # Determine which button was pressed
        choice = None
        if interaction.data["custom_id"] == "poll_yes":
            choice = "Yes"
        elif interaction.data["custom_id"] == "poll_maybe":
            choice = "Maybe"
        elif interaction.data["custom_id"] == "poll_no":
            choice = "No"

        user_id = str(interaction.user.id)
        # Persist vote
        self.votes[user_id] = choice
        save_votes(self.votes)

        # Apply voting role changes immediately for this user
        member = interaction.user
        if isinstance(member, discord.Member):
            try:
                if choice in ["Yes", "Maybe"]:
                    if self.voting_role and self.voting_role not in member.roles:
                        await member.add_roles(self.voting_role, reason="Poll vote Yes/Maybe")
                else:  # "No"
                    if self.voting_role and self.voting_role in member.roles:
                        await member.remove_roles(self.voting_role, reason="Poll vote No")
            except Exception:
                pass  # swallow to avoid interrupting user flow

        # Acknowledge
        await interaction.response.defer()

# Core tasks
async def restore_event_ping_daily():
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    london_now = now_utc.astimezone(LONDON_TZ)
    if london_now.hour != 17:
        return  # only run at 17:00 London time

    # Iterate guilds
    for guild in bot.guilds:
        resources = await resolve_resources(guild)
        ping_role = resources.get("ping_role")
        if not ping_role:
            continue

        # Restore to all non-bot members
        restored = 0
        for member in guild.members:
            if member.bot:
                continue
            if ping_role in member.roles:
                continue
            try:
                await member.add_roles(ping_role, reason="Daily Event Ping! restoration")
                restored += 1
            except Exception:
                continue

        print(f"[restore_event_ping_daily] Guild: {guild.name} Restored: {restored} members")

async def post_daily_poll():
    # This posts at 18:00 London time
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    london_now = now_utc.astimezone(LONDON_TZ)
    if london_now.hour != 18:
        return

    votes_data = load_votes()

    for guild in bot.guilds:
        resources = await resolve_resources(guild)
        channel = resources.get("event_channel")
        voting_role = resources.get("voting_role")

        if channel is None:
            continue

        poll_message = (
            "Poll: Who wants to participate in the 1v1 tournament? "
            "Please vote: Yes, Maybe, No. Your choice persists."
        )

        view = PollView(guild_id=guild.id, channel=channel, voting_role=voting_role, persisted_votes=votes_data.get(str(guild.id), {}))
        try:
            await channel.send(poll_message, view=view)
        except Exception as e:
            print(f"[post_daily_poll] Failed to post in {channel.name if channel else 'Unknown'}: {e}")

async def post_tester_poll():
    # This runs daily at 19:45 London time, once per day per guild
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    london_now = now_utc.astimezone(LONDON_TZ)
    if london_now.hour != 19 or london_now.minute != 45:
        return

    today_str = london_now.date().isoformat()

    tester_polls = load_tester_polls()

    for guild in bot.guilds:
        resources = await resolve_resources(guild)
        channel = resources.get("event_channel")
        voting_role = resources.get("voting_role")

        if channel is None:
            continue

        last_sent_date = tester_polls.get(str(guild.id), "")
        if last_sent_date == today_str:
            continue  # already sent today

        tester_message = (
            "Tester poll: This poll will run tomorrow's main poll behavior. "
            "This is to verify the poll flow works before the 6:00 PM poll. "
            "Please vote: Yes, Maybe, No. This tester vote does not affect real scheduling."
        )

        try:
            # Use the same voting mechanism for consistency, but this is a one-off test poll
            view = PollView(guild_id=guild.id, channel=channel, voting_role=voting_role, persisted_votes={})
            await channel.send(tester_message, view=view)
            tester_polls[str(guild.id)] = today_str
        except Exception as e:
            print(f"[post_tester_poll] Failed to post tester poll in {channel.name if channel else 'Unknown'}: {e}")

    save_tester_polls(tester_polls)

# Tasks loop
@tasks.loop(minutes=1)
async def scheduler():
    await asyncio.sleep(1)
    await bot.wait_until_ready()
    # 17:00 London restoration
    await restore_event_ping_daily()
    # 18:00 London poll post
    await post_daily_poll()
    # 19:45 London tester poll (one-time per day)
    await post_tester_poll()

# Events
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not scheduler.is_running():
        scheduler.start()

# Run
def main():
    # Place for your token (ensure secure handling in production)
    TOKEN = "YOUR_BOT_TOKEN_HERE"
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
