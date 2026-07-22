import os
import asyncio
import discord

TOKEN = os.getenv("TOKEN")

# Channel where reminders are sent
REMINDER_CHANNEL_ID = 1528940157024206899

# Event voting channel
EVENT_CHANNEL_ID = 1523698932499218492

ROLE_NAME = "Event Ping!"

intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True

client = discord.Client(intents=intents)

async def reminder_loop():
    await client.wait_until_ready()

    while not client.is_closed():
        for guild in client.guilds:
            reminder_channel = guild.get_channel(REMINDER_CHANNEL_ID)

            if reminder_channel is None:
                continue

            role = discord.utils.get(guild.roles, name=ROLE_NAME)

            role_mention = role.mention if role else "@Event Ping!"

            message = (
                f"{role_mention} @here\n\n"
                f"Remember to vote in <#{EVENT_CHANNEL_ID}>.\n\n"
                "Once you've finished voting, click the 🔔 emoji."
            )

            await reminder_channel.send(message)

        await asyncio.sleep(600)  # 10 minutes

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    client.loop.create_task(reminder_loop())

client.run(TOKEN)
