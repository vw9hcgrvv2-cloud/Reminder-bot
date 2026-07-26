import discord
from discord.ext import commands, tasks
import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Config
TOKEN = "YOUR_BOT_TOKEN_HERE"

# Timezone
LONDON_TZ = ZoneInfo("Europe/London")

# Persistence
PERSISTENCE_PATH = "poll_votes.json"

# Channel and role identifiers by name (names used for resolution; ID used only for reliability when possible)
EVENT_CHANNEL_ID = 1524445184853803069
EVENT_PING_ROLE_NAME = "Event Ping!"
EVENT_VOTE_ROLE_NAME = "Event role"  # voting role name

# Client setup with required intents
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Ensure persistence file exists
import os
if not os.path.exists(PERSISTENCE_PATH):
    with open(PERSISTENCE_PATH, "w") as f:
        json.dump({}, f)

# Helper to resolve per-guild resources by name (and ID where appropriate)
async def resolve_resources(bot):
    per_guild = []
    for g in bot.guilds:
        channel = bot.get_channel(EVENT_CHANNEL_ID)
        if channel is None or not isinstance(channel, discord.TextChannel) or channel.guild.id != g.id:
            channel = discord.utils.get(g.text_channels, name=channel_name_for_guild(g))
        ping_role = discord.utils.get(g.roles, name=EVENT_PING_ROLE_NAME)
        voting_role = discord.utils.get(g.roles, name=EVENT_VOTE_ROLE_NAME)
        per_guild.append((g, channel, ping_role, voting_role))
    return per_guild

def channel_name_for_guild(guild):
    # If you want to prefer a name-based channel lookup, set it here.
    # Otherwise return a placeholder that will fail gracefully.
    return "event-channel"  # replace if you have a specific name per guild

# Daily restore of Event Ping! role (5:00 PM London)
async def restore_event_ping_daily():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(tz=timezone.utc)
        london_now = now.astimezone(LONDON_TZ)
        target = london_now.replace(hour=17, minute=0, second=0, microsecond=0)
        if london_now >= target:
            target += timedelta(days=1)
        wait = (target - london_now).total_seconds()
        await asyncio.sleep(max(0, wait))

        for g, channel, ping_role, _ in await resolve_resources(bot):
            if channel is None or ping_role is None:
                continue
            for member in g.members:
                if member.bot:
                    continue
                if ping_role not in member.roles:
                    try:
                        await member.add_roles(ping_role, reason="Daily Event Ping role restoration")
                    except Exception:
                        pass

# Poll view and voting logic
class PollView(discord.ui.View):
    def __init__(self, voting_role):
        super().__init__(timeout=None)
        self.voting_role = voting_role

    @discord.ui.button(label="✅ Yes", style=discord.ButtonStyle.success, custom_id="poll_yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_vote(interaction, "yes")

    @discord.ui.button(label="🤔 Maybe", style=discord.ButtonStyle.primary, custom_id="poll_maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_vote(interaction, "maybe")

    @discord.ui.button(label="❌ No", style=discord.ButtonStyle.danger, custom_id="poll_no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_vote(interaction, "no")

    async def process_vote(self, interaction: discord.Interaction, vote: str):
        user_id = str(interaction.user.id)

        # Load votes
        try:
            with open(PERSISTENCE_PATH, "r") as f:
                votes = json.load(f)
        except Exception:
            votes = {}

        votes[user_id] = vote
        with open(PERSISTENCE_PATH, "w") as f:
            json.dump(votes, f)

        member = interaction.user
        voting_role = self.voting_role
        if voting_role is not None and isinstance(member, discord.Member):
            if vote in ("yes", "maybe"):
                if voting_role not in member.roles:
                    try:
                        await member.add_roles(voting_role, reason="Poll vote: yes/maybe")
                    except Exception:
                        pass
            else:
                if voting_role in member.roles:
                    try:
                        await member.remove_roles(voting_role, reason="Poll vote: no")
                    except Exception:
                        pass

        if interaction.response is None:
            await interaction.response.defer()
        await interaction.followup.send(
            f"Your vote '{vote}' has been recorded.",
            ephemeral=True
        )

async def post_daily_poll():
    await bot.wait_until_ready()
    while True:
        resolved = await resolve_resources(bot)
        for g, channel, ping_role, voting_role in resolved:
            if channel is None or voting_role is None:
                continue
            poll_embed = discord.Embed(
                title="1v1 Tournament",
                description="Who will join tomorrow's 1v1 tournament?"
            )
            view = PollView(voting_role)
            try:
                await channel.send(embed=poll_embed, view=view)
            except Exception:
                pass

        # Schedule for 6:00 PM London
        now = datetime.now(tz=timezone.utc)
        london_now = now.astimezone(LONDON_TZ)
        target = london_now.replace(hour=18, minute=0, second=0, microsecond=0)
        if london_now >= target:
            target += timedelta(days=1)
        wait = (target - london_now).total_seconds()
        await asyncio.sleep(max(0, wait))

# Setup startup tasks
async def setup_background_tasks():
    bot.loop.create_task(restore_event_ping_daily())
    bot.loop.create_task(post_daily_poll())

# Bot events and run
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.command(name="ping")
async def ping_cmd(ctx):
    await ctx.send("Pong!")

# Initialize
@bot.event
async def on_connect():
    pass

# Start background tasks on startup
@bot.event
async def on_guild_channel_update(before, after):
    pass

@bot.event
async def on_ready():
    await setup_background_tasks()

if __name__ == "__main__":
    bot.run(TOKEN)
