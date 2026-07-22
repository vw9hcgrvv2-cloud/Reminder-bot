import os
import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("TOKEN")

POST_CHANNEL_ID = 1528940157024206899
VOTING_CHANNEL_ID = 1524445184853803069
EVENT_ROLE_NAME = "Event Ping!"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    if not post_message.is_running():
        post_message.start()


@tasks.loop(minutes=5)
async def post_message():
    channel = bot.get_channel(POST_CHANNEL_ID)
    if channel is None:
        return

    guild = channel.guild

    role = discord.utils.get(guild.roles, name=EVENT_ROLE_NAME)
    voting_channel = guild.get_channel(VOTING_CHANNEL_ID)

    if role is None or voting_channel is None:
        return

    await channel.send(
        f"{role.mention} @here\n\n"
        f"Remember to vote in {voting_channel.mention}.\n\n"
        "Once you've finished voting, click the 🔔 emoji."
    )


@post_message.before_loop
async def before_post():
    await bot.wait_until_ready()


bot.run(TOKEN)
