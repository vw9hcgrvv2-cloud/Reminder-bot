import os
import asyncio
from datetime import datetime, time, timedelta
import pytz

import discord
from discord.ext import commands, tasks

# Configuration (override with environment vars if you prefer)
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN")

GUILD_ID = int(os.environ.get("GUILD_ID", "0"))
POLL_CHANNEL_ID = int(os.environ.get("POLL_CHANNEL_ID", "1524445184853803069"))
POLL_IMAGE_URL = os.environ.get("POLL_IMAGE_URL", "https://example.com/poll-image.png")

REMINDER_CHANNEL_ID = int(os.environ.get("REMINDER_CHANNEL_ID", "1528940157024206899"))
REMINDER_IMAGE_URL = os.environ.get("REMINDER_IMAGE_URL", "https://example.com/reminder-image.png")

EVENT_ROLE_NAME = "Event role"
PING_ROLE_NAME = "Event Ping! Role"

YES_EMOJI = "✅"
MAYBE_EMOJI = "❔"
NO_EMOJI = "❌"

# Timezone for scheduling
TZ = pytz.timezone("Europe/London")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

def get_role_by_name(guild, name):
    for r in guild.roles:
        if r.name == name:
            return r
    return None

async def ensure_role_exists(guild, role_name):
    r = get_role_by_name(guild, role_name)
    if r is None:
        r = await guild.create_role(name=role_name)
    return r

async def post_poll_message(guild):
    channel = guild.get_channel(POLL_CHANNEL_ID)
    if channel is None:
        return None
    content = "@everyone Poll time! Please vote:"
    embed = discord.Embed(title="Event Poll", description="React with Yes / Maybe / No to indicate attendance.")
    embed.set_image(url=POLL_IMAGE_URL)
    msg = await channel.send(content=content, embed=embed)
    await msg.add_reaction(YES_EMOJI)
    await msg.add_reaction(MAYBE_EMOJI)
    await msg.add_reaction(NO_EMOJI)
    return msg

async def build_voter_lists(msg):
    yes_users = []
    maybe_users = []
    no_users = []
    for reaction in msg.reactions:
        if str(reaction.emoji) == YES_EMOJI:
            users = await reaction.users().flatten()
            yes_users = [u.display_name for u in users if not u.bot]
        elif str(reaction.emoji) == MAYBE_EMOJI:
            users = await reaction.users().flatten()
            maybe_users = [u.display_name for u in users if not u.bot]
        elif str(reaction.emoji) == NO_EMOJI:
            users = await reaction.users().flatten()
            no_users = [u.display_name for u in users if not u.bot]
    return yes_users, maybe_users, no_users

async def update_poll_display(msg):
    yes_users, maybe_users, no_users = await build_voter_lists(msg)
    embed = msg.embeds[0]
    description = (
        "Yes:\n" + (", ".join(yes_users) if yes_users else "Nobody yet") + "\n\n" +
        "Maybe:\n" + (", ".join(maybe_users) if maybe_users else "Nobody yet") + "\n\n" +
        "No:\n" + (", ".join(no_users) if no_users else "Nobody yet")
    )
    new_embed = discord.Embed(title=embed.title, description=description)
    new_embed.set_image(url=POLL_IMAGE_URL)
    await msg.edit(embed=new_embed)

async def update_all_polls_in_guild(guild):
    channel = guild.get_channel(POLL_CHANNEL_ID)
    if channel is None:
        return
    # Get recent messages and try to refresh the latest poll (assumes last poll is the latest bot message)
    msgs = []
    async for m in channel.history(limit=5):
        msgs.append(m)
    poll_msg = None
    for m in msgs:
        if m.author.bot:
            poll_msg = m
            break
    if poll_msg:
        await update_poll_display(poll_msg)

async def remind_event_ping():
    for g in bot.guilds:
        channel = g.get_channel(REMINDER_CHANNEL_ID)
        if channel is None:
            continue
        try:
            ping_role = get_role_by_name(g, PING_ROLE_NAME)
            if ping_role is None:
                ping_role = await ensure_role_exists(g, PING_ROLE_NAME)
            # Craft the reminder content
            content = f"Reminder: Event is upcoming. {ping_role.mention}"
            embed = discord.Embed(title="Event Reminder", url=None)
            embed.set_image(url=REMINDER_IMAGE_URL)
            msg = await channel.send(content=content, embed=embed)
            # Attach a bell reaction to opt-out
            await msg.add_reaction("🔔")
        except Exception:
            pass

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Schedule first poll at 18:00 Europe/London
    London = TZ
    now = datetime.now(London)
    target = London.localize(datetime.combine(now.date(), time(18, 0)))
    if now > target:
        target = target + timedelta(days=1)
    delay = (target - now).total_seconds()
    bot.loop.call_later(delay, lambda: bot.loop.create_task(run_daily_poll()))
    # Start reminder loop (every 15 minutes)
    reminder_loop.start()

async def run_daily_poll():
    for g in bot.guilds:
        poll_msg = await post_poll_message(g)
        # Optionally store poll_msg.id if you want to reference later
    # Schedule next day's poll
    London = TZ
    next_run = London.localize(datetime.combine(datetime.now(London).date(), time(18, 0))) + timedelta(days=1)
    delay = (next_run - datetime.now(London)).total_seconds()
    bot.loop.call_later(delay, lambda: bot.loop.create_task(run_daily_poll()))

@tasks.loop(minutes=15)
async def reminder_loop():
    await remind_event_ping()

@bot.event
async def on_raw_reaction_add(payload):
    # Distinguish between poll reactions and bell opt-out
    if payload.guild_id is None:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    emoji = payload.emoji.name

    # Bell reaction on reminder message to remove Event Ping! Role
    if emoji == "🔔":
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        role = get_role_by_name(guild, PING_ROLE_NAME)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Bell reaction removed Event Ping! Role")
            except Exception:
                pass
        return

    # Poll reactions
    if payload.channel_id != POLL_CHANNEL_ID:
        return

    member = guild.get_member(payload.user_id)
    if member is None or member.bot:
        return

    role = get_role_by_name(guild, EVENT_ROLE_NAME)
    if role is None:
        role = await ensure_role_exists(guild, EVENT_ROLE_NAME)

    emoji_name = emoji
    try:
        if emoji_name in {YES_EMOJI, MAYBE_EMOJI}:
            if role not in member.roles:
                await member.add_roles(role, reason="Vote adds Event role")
        elif emoji_name == NO_EMOJI:
            if role in member.roles:
                await member.remove_roles(role, reason="Vote removes Event role")

        # Update poll display with current voters
        channel = guild.get_channel(POLL_CHANNEL_ID)
        if channel:
            # Try to fetch latest poll message authored by bot
            msgs = []
            async for m in channel.history(limit=5):
                msgs.append(m)
            if msgs:
                poll_msg = msgs[0]
                if poll_msg.author.bot:
                    await update_poll_display(poll_msg)
    except Exception:
        pass

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id is None:
        return
    if payload.channel_id != POLL_CHANNEL_ID:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    emoji = payload.emoji.name
    channel = guild.get_channel(POLL_CHANNEL_ID)
    if channel is None:
        return
    # Recompute poll state after removal
    if emoji in {YES_EMOJI, MAYBE_EMOJI, NO_EMOJI}:
        try:
            msgs = []
            async for m in channel.history(limit=5):
                msgs.append(m)
            if msgs:
                poll_msg = msgs[0]
                if poll_msg.author.bot:
                    await update_poll_display(poll_msg)
        except Exception:
            pass

if __name__ == "__main__":
    bot.run(TOKEN)
