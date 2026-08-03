import discord
from discord import NotFound, Forbidden, HTTPException
from discord.ui import View
import json
import os
from datetime import datetime, timedelta
import shutil
import pytz
import asyncio
import logging
import random
from typing import Optional, Dict, List, Any, Set, Tuple

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LONDON_TZ = pytz.timezone("Europe/London")
EVENT_CHANNEL_ID = 1524445184853803069
REMINDER_CHANNEL_ID = 1528940157024206899
ATTENDEE_ROLE_NAME = "Event Attendee"
PING_ROLE_NAME = "Event Ping!"
STATE_FILE = "bot_state.json"
REMINDER_KEEP_DAYS = 7
HISTORY_LIMIT = 150
ROLE_BATCH_SIZE = 15
ROLE_BATCH_DELAY = 1.2
MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.0
MAX_REMINDER_RETRIES = 8
MAX_BACKOFF = 30.0

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

ALLOWED_MENTIONS_EVERYONE = discord.AllowedMentions(everyone=True, roles=True)
ALLOWED_MENTIONS_ROLES = discord.AllowedMentions(everyone=False, roles=True)

TOMORROW_POLL_TITLE = "Tomorrow's 3PM Tournament Attendance Poll"
TODAY_POLL_TITLE = "Today's Tournament Attendance Poll"


def _to_int_id(value: Any) -> Optional[int]:
    return None if value in (None, "") else int(value) if str(value).strip().isdigit() else None

def _atomic_save_sync(state_dict: Dict[str, Any]) -> None:
    tmp_path = f"{STATE_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state_dict, f, indent=2)
    os.replace(tmp_path, STATE_FILE)

def _backup_corrupted_state() -> str:
    timestamp = datetime.now(LONDON_TZ).strftime("%Y%m%d-%H%M%S")
    backup_name = f"bot_state_corrupted_{timestamp}.json"
    shutil.copy2(STATE_FILE, backup_name)
    logger.error(f"❌ Corrupted state backed up to: {backup_name}")
    return backup_name

def _ensure_state_fields(state_dict: Dict[str, Any]) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "poll_session_date": None, "current_poll_msg_id": None, "voted_users": {},
        "allowed_to_disable": [], "disable_permission_session": None, "opted_out_users": [],
        "last_1400_run_date": None, "last_1400_status": None,
        "last_1500_reset_date": None, "last_1500_reset_status": None,
        "last_1800_poll_date": None, "last_1800_poll_status": None,
        "sent_reminders": {}, "pending_reminders": {},
        "tomorrow_poll_msg_id": None, "tomorrow_poll_created_date": None,
        "tomorrow_poll_target_date": None, "tomorrow_voted_users": {}
    }
    for key, default_val in defaults.items():
        if key not in state_dict:
            state_dict[key] = default_val
    return state_dict

def _validate_state_dict(state_dict: Dict[str, Any]) -> None:
    dict_fields = ["voted_users", "tomorrow_voted_users", "sent_reminders", "pending_reminders"]
    list_fields = ["allowed_to_disable", "opted_out_users"]
    for field in dict_fields:
        if not isinstance(state_dict.get(field), dict):
            state_dict[field] = {}
            logger.warning(f"⚠️ Reset malformed {field} to empty dict")
    for field in list_fields:
        if not isinstance(state_dict.get(field), list):
            state_dict[field] = []
            logger.warning(f"⚠️ Reset malformed {field} to empty list")

def _check_role_assignable(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        return False
    if me.top_role <= role:
        return False
    return True

def _permission_health_check(guild: discord.Guild) -> bool:
    me = guild.me
    if not me:
        return False
    if not me.guild_permissions.manage_roles:
        logger.warning(f"⚠️ Missing Manage Roles permission in guild {guild.name}")
        return False
    ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
    attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)
    if ping_role and me.top_role <= ping_role:
        logger.warning(f"⚠️ Bot role hierarchy too low for {PING_ROLE_NAME} in {guild.name}")
        return False
    if attendee_role and me.top_role <= attendee_role:
        logger.warning(f"⚠️ Bot role hierarchy too low for {ATTENDEE_ROLE_NAME} in {guild.name}")
        return False
    return True

def _is_retryable_exception(e: Exception) -> bool:
    if isinstance(e, (Forbidden, NotFound)):
        return False
    if isinstance(e, HTTPException):
        if e.status in (403, 404):
            return False
        if 400 <= e.status < 500 and e.status != 429:
            return False
    return True

def _calculate_backoff(attempt: int) -> float:
    delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0.1, 0.5)
    return min(delay, MAX_BACKOFF)

async def _retry_operation(op, *args, max_retries: int = MAX_RETRIES, **kwargs):
    last_exception: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await op(*args, **kwargs), True
        except HTTPException as e:
            last_exception = e
            if not _is_retryable_exception(e):
                logger.warning(f"⏭️ Non-retryable error (status {e.status}), failing fast")
                break
            if attempt < max_retries - 1:
                delay = _calculate_backoff(attempt)
                if e.status == 429:
                    try:
                        retry_after = e.response.headers.get("Retry-After")
                        if retry_after:
                            delay = float(retry_after) + 0.5
                    except Exception:
                        pass
                logger.warning(f"⚠️ API error, retry {attempt+1}/{max_retries} in {delay:.1f}s")
                await asyncio.sleep(delay)
        except Exception as e:
            last_exception = e
            if not _is_retryable_exception(e):
                break
            delay = _calculate_backoff(attempt)
            logger.warning(f"⚠️ Operation error, retry {attempt+1}/{max_retries}: {e}")
            await asyncio.sleep(delay)
    logger.error(f"❌ Operation failed after {attempt+1} attempts: {last_exception}")
    return None, False


class StateManager:
    def __init__(self) -> None:
        self.state: Dict[str, Any] = {}
        self.write_lock = asyncio.Lock()
        self.load()

    def load(self) -> None:
        loaded: Dict[str, Any] = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except json.JSONDecodeError as e:
                logger.error(f"❌ Corrupted JSON detected: {e}")
                _backup_corrupted_state()
                loaded = {}
            except Exception as e:
                logger.error(f"❌ State load error: {e}")
                loaded = {}
        self.state = _ensure_state_fields(loaded)
        _validate_state_dict(self.state)
        self.state["current_poll_msg_id"] = _to_int_id(self.state.get("current_poll_msg_id"))
        self.state["tomorrow_poll_msg_id"] = _to_int_id(self.state.get("tomorrow_poll_msg_id"))

    async def save(self) -> None:
        async with self.write_lock:
            try:
                safe_copy = json.loads(json.dumps(self.state))
                await asyncio.to_thread(_atomic_save_sync, safe_copy)
            except Exception as e:
                logger.error(f"❌ State save failed: {e}")

    def get_today_key(self) -> str:
        return datetime.now(LONDON_TZ).strftime("%Y-%m-%d")
    def get_tomorrow_key(self) -> str:
        return (datetime.now(LONDON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    def get_reminder_key(self, dt: datetime) -> str:
        return f"{dt.strftime('%Y-%m-%d')}-{dt.hour:02d}-{dt.minute:02d}"

    async def cleanup_old_reminders(self) -> None:
        now = datetime.now(LONDON_TZ)
        cutoff_dt = (now.date() - timedelta(days=REMINDER_KEEP_DAYS))
        changed = False
        cleaned_sent: Dict[str, bool] = {}
        removed = 0
        failed_removed = 0
        for key, sent_flag in self.state["sent_reminders"].items():
            try:
                date_part = "-".join(key.split("-")[:3])
                key_date = datetime.strptime(date_part, "%Y-%m-%d").date()
                if key_date >= cutoff_dt:
                    cleaned_sent[key] = sent_flag
                else:
                    removed += 1
                    changed = True
            except (ValueError, IndexError):
                cleaned_sent[key] = sent_flag
        pending_items = list(self.state["pending_reminders"].items())
        for key, info in pending_items:
            if isinstance(info, dict) and info.get("failures", 0) >= MAX_REMINDER_RETRIES:
                failed_removed += 1
                self.state["pending_reminders"].pop(key, None)
                changed = True
                logger.warning(f"🗑️ Deleted permanently failed reminder: {key}")
        if changed:
            logger.info(f"✅ Cleaned {removed} old sent reminders, removed {failed_removed} permanently-failed pending")
            self.state["sent_reminders"] = cleaned_sent
            await self.save()


state = StateManager()


async def _chunk_guild_safe(guild: discord.Guild) -> None:
    try:
        if not guild.chunked:
            logger.info(f"🔄 Chunking member list for guild: {guild.name}")
            await guild.chunk()
            logger.info(f"✅ Chunked {len(guild.members)} members for {guild.name}")
    except Exception as e:
        logger.warning(f"⚠️ Could not chunk guild {guild.name}: {e}, will fetch members individually")

async def _fetch_member_safe(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    try:
        if guild.get_member(user_id):
            return guild.get_member(user_id)
        return await guild.fetch_member(user_id)
    except Exception as e:
        logger.debug(f"Could not fetch member {user_id}: {e}")
        return None

async def _update_role_safe(member: discord.Member, role: discord.Role, add: bool) -> bool:
    async def _op():
        if add:
            await member.add_roles(role)
        else:
            await member.remove_roles(role)
    _, ok = await _retry_operation(_op)
    return ok


async def _remove_attendee_role_bulk(guild: discord.Guild) -> int:
    attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)
    if not attendee_role:
        logger.warning(f"❌ {ATTENDEE_ROLE_NAME} role not found during bulk reset")
        return 0
    if not _permission_health_check(guild):
        logger.error(f"❌ Cannot remove {ATTENDEE_ROLE_NAME}: permissions/hierarchy check failed")
        return 0

    await _chunk_guild_safe(guild)
    candidates = [m for m in guild.members if not m.bot and attendee_role in m.roles]
    total = len(candidates)
    success = 0
    failed_count = 0
    logger.info(f"🔄 Removing {ATTENDEE_ROLE_NAME} from {total} members...")

    for i in range(0, total, ROLE_BATCH_SIZE):
        if not _permission_health_check(guild):
            logger.error(f"⚠️ Permissions lost mid-reset; pausing after {success} successes")
            break
        batch = candidates[i:i + ROLE_BATCH_SIZE]
        results = await asyncio.gather(
            *[_update_role_safe(m, attendee_role, add=False) for m in batch],
            return_exceptions=True
        )
        for ok in results:
            if ok is True:
                success += 1
            else:
                failed_count += 1
        if i + ROLE_BATCH_SIZE < total:
            await asyncio.sleep(ROLE_BATCH_DELAY)

    if failed_count:
        logger.warning(f"❌ Failed to remove role from {failed_count} members")
    logger.info(f"✅ Removed {ATTENDEE_ROLE_NAME} from {success}/{total} members")
    return success


async def _batch_assign_roles(members: List[discord.Member], role: discord.Role, add: bool) -> int:
    if not members:
        return 0
    success = 0
    for i in range(0, len(members), ROLE_BATCH_SIZE):
        batch = members[i:i + ROLE_BATCH_SIZE]
        results = await asyncio.gather(
            *[_update_role_safe(m, role, add) for m in batch],
            return_exceptions=True
        )
        success += sum(1 for r in results if r is True)
        if i + ROLE_BATCH_SIZE < len(members):
            await asyncio.sleep(ROLE_BATCH_DELAY)
    return success


async def _fetch_channel_safe(client: discord.Client, channel_id: int, name_hint: str = "channel") -> Optional[discord.TextChannel]:
    try:
        return client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    except Forbidden:
        logger.error(f"❌ Forbidden: cannot access {name_hint} {channel_id}")
    except NotFound:
        logger.error(f"❌ Not found: {name_hint} {channel_id}")
    except Exception as e:
        logger.error(f"❌ Failed to fetch {name_hint} {channel_id}: {e}")
    return None


class DisablePingView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Disable Event Pings", style=discord.ButtonStyle.gray, custom_id="disable_pings")
    async def disable_pings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        deferred = False
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
                deferred = True

            user_id = str(interaction.user.id)
            today = state.get_today_key()
            session_date = state.state.get("poll_session_date")
            permission_session = state.state.get("disable_permission_session")
            voted_users = state.state.get("voted_users", {})
            allowed_list = state.state.get("allowed_to_disable", [])

            if session_date != today or permission_session != today or user_id not in voted_users or user_id not in allowed_list:
                if deferred:
                    await interaction.followup.send("You must vote first.", ephemeral=True)
                return

            ping_role = discord.utils.get(interaction.guild.roles, name=PING_ROLE_NAME) if interaction.guild else None
            if not ping_role:
                if deferred:
                    await interaction.followup.send("Role not found.", ephemeral=True)
                return
            if ping_role not in interaction.user.roles:
                if deferred:
                    await interaction.followup.send("No role to remove.", ephemeral=True)
                return
            if not _check_role_assignable(interaction.guild, ping_role):
                if deferred:
                    await interaction.followup.send("Cannot remove role: permission or hierarchy issue.", ephemeral=True)
                return

            ok = await _update_role_safe(interaction.user, ping_role, add=False)
            if not ok:
                if deferred:
                    await interaction.followup.send("❌ Failed to remove role.", ephemeral=True)
                return

            if user_id not in state.state["opted_out_users"]:
                state.state["opted_out_users"].append(user_id)
                await state.save()

            if deferred:
                await interaction.followup.send("✅ Pings disabled.", ephemeral=True)
        except Exception as e:
            logger.error(f"❌ Error in disable_pings button: {e}")
            try:
                if deferred:
                    await interaction.followup.send("An error occurred. Please try again later.", ephemeral=True)
            except Exception:
                pass


class BasePollView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _handle_vote(self, interaction: discord.Interaction, vote_type: str, prefix: str) -> None:
        deferred = False
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
                deferred = True

            user_id = str(interaction.user.id)
            today = state.get_today_key()
            guild = interaction.guild
            if not guild:
                if deferred:
                    await interaction.followup.send("Guild not found.", ephemeral=True)
                return

            if prefix:
                target_date = state.state.get("tomorrow_poll_target_date")
                session_date = state.state.get("tomorrow_poll_created_date")
            else:
                target_date = state.get_today_key()
                session_date = state.state.get("poll_session_date")

            now = datetime.now(LONDON_TZ)
            end_of_target = None
            if target_date:
                try:
                    target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
                    end_of_target = datetime.combine(target_dt, datetime.max.time(), tzinfo=LONDON_TZ)
                except ValueError:
                    pass

            if session_date != target_date or (end_of_target and now > end_of_target):
                logger.info("⏭️ Vote rejected: poll expired or session mismatch")
                if deferred:
                    await interaction.followup.send("Poll expired.", ephemeral=True)
                return

            stored_msg_id = state.state.get(f"{prefix}poll_msg_id" if prefix else "current_poll_msg_id")
            if interaction.message and stored_msg_id != interaction.message.id:
                logger.info("⏭️ Vote rejected: message ID mismatch")
                if deferred:
                    await interaction.followup.send("Wrong poll.", ephemeral=True)
                return

            voted_users = state.state[f"{prefix}voted_users"]
            if vote_type in ("attending", "maybe"):
                voted_users[user_id] = vote_type
                if not prefix:
                    if user_id not in state.state["allowed_to_disable"]:
                        state.state["allowed_to_disable"].append(user_id)
                    state.state["disable_permission_session"] = today
                    if user_id in state.state["opted_out_users"]:
                        state.state["opted_out_users"].remove(user_id)
            else:
                voted_users.pop(user_id, None)
                if not prefix:
                    state.state["allowed_to_disable"] = [u for u in state.state["allowed_to_disable"] if u != user_id]
                    if not state.state["allowed_to_disable"]:
                        state.state["disable_permission_session"] = None

            await state.save()
            if deferred:
                await interaction.followup.send(f"✅ Vote: {vote_type.replace('_',' ').title()}", ephemeral=True)

            attendee_role = discord.utils.get(guild.roles, name=ATTENDEE_ROLE_NAME)
            if attendee_role and _check_role_assignable(guild, attendee_role):
                should_have = vote_type in ("attending", "maybe")
                has_now = attendee_role in interaction.user.roles
                if should_have != has_now:
                    asyncio.create_task(_update_role_safe(interaction.user, attendee_role, add=should_have))
        except Exception as e:
            logger.error(f"❌ Error processing vote: {e}")
            try:
                if deferred:
                    await interaction.followup.send("Failed to record vote. Please try again.", ephemeral=True)
            except Exception:
                pass


class PollView(BasePollView):
    @discord.ui.button(label="✅ Attending", style=discord.ButtonStyle.green, custom_id="poll:attending")
    async def attending(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_vote(interaction, "attending", prefix="")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.secondary, custom_id="poll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_vote(interaction, "maybe", prefix="")

    @discord.ui.button(label="❌ Not attending", style=discord.ButtonStyle.red, custom_id="poll:not_attending")
    async def not_attending(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_vote(interaction, "not_attending", prefix="")


class TomorrowPollView(BasePollView):
    @discord.ui.button(label="✅ Attending", style=discord.ButtonStyle.green, custom_id="tomorrow_poll:attending")
    async def attending(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_vote(interaction, "attending", prefix="tomorrow_")

    @discord.ui.button(label="❓ Maybe", style=discord.ButtonStyle.secondary, custom_id="tomorrow_poll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_vote(interaction, "maybe", prefix="tomorrow_")

    @discord.ui.button(label="❌ Not attending", style=discord.ButtonStyle.red, custom_id="tomorrow_poll:not_attending")
    async def not_attending(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_vote(interaction, "not_attending", prefix="tomorrow_")


class TournamentBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.poll_view: PollView
        self.tomorrow_poll_view: TomorrowPollView
        self.disable_view: DisablePingView
        self._scheduler_started = False
        self._first_reminder_started = False
        self.reminder_send_lock = asyncio.Lock()
        self.task_locks: Dict[str, asyncio.Lock] = {
            "reminder": asyncio.Lock(),
            "1400": asyncio.Lock(),
            "1500": asyncio.Lock(),
            "1800": asyncio.Lock(),
            "tomorrow_poll": asyncio.Lock(),
        }
        self._seen_boundaries: Set[str] = set()
        self._background_tasks: Set[asyncio.Task] = set()
        self._health_check_hour = -1

    def _track_task(self, coro, name: str):
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        def _done(t):
            self._background_tasks.discard(t)
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error(f"❌ Background task '{name}' failed: {exc}")
        task.add_done_callback(_done)
        return task

    async def _retry_pending_reminders(self) -> None:
        for rem_key in list(state.state.get("pending_reminders", {}).keys()):
            info = state.state["pending_reminders"][rem_key]
            if isinstance(info, dict) and info.get("failures", 0) >= MAX_REMINDER_RETRIES:
                logger.warning(f"⏭️ Skipping permanently failed reminder: {rem_key}")
                continue
            if rem_key not in state.state["sent_reminders"]:
                logger.info(f"🔄 Retrying pending reminder: {rem_key}")
                await self._send_reminder_message(rem_key)

    async def setup_hook(self) -> None:
        self.poll_view = PollView()
        self.tomorrow_poll_view = TomorrowPollView()
        self.disable_view = DisablePingView()
        self.add_view(self.poll_view)
        self.add_view(self.tomorrow_poll_view)
        self.add_view(self.disable_view)
        logger.info("✅ Persistent views registered")
        logger.info(f"🕐 Timezone set to: {LONDON_TZ}")
        await self._retry_pending_reminders()

    async def on_ready(self) -> None:
        logger.info(f"✅ Bot ready: {self.user}")
        if not self._scheduler_started:
            await self.wait_until_ready()
            self._scheduler_started = True
            self._track_task(self._startup_poll(), "startup_poll")
            self._track_task(self._first_reminder_worker(), "first_reminder")
            self._track_task(self._scheduler_loop(), "scheduler")

    async def _send_reminder_message(self, rem_key: str) -> bool:
        async with self.reminder_send_lock:
            now = datetime.now(LONDON_TZ).isoformat()
            if rem_key in state.state["sent_reminders"]:
                logger.debug(f"⏭️ {rem_key} already sent")
                state.state["pending_reminders"].pop(rem_key, None)
                await state.save()
                return True

            pending_info = state.state["pending_reminders"].get(rem_key, {})
            if isinstance(pending_info, dict):
                failures = pending_info.get("failures", 0)
                first_attempt = pending_info.get("first_attempt_at", now)
            else:
                failures = 0
                first_attempt = now

            if failures >= MAX_REMINDER_RETRIES:
                logger.warning(f"🗑️ Reminder {rem_key} failed {failures} times — deleting")
                state.state["pending_reminders"].pop(rem_key, None)
                await state.save()
                return False

            state.state["pending_reminders"][rem_key] = {
                "failures": failures,
                "first_attempt_at": first_attempt,
                "last_attempt_at": now
            }
            await state.save()

            ch = await _fetch_channel_safe(self, REMINDER_CHANNEL_ID, "reminder channel")
            if not ch:
                state.state["pending_reminders"][rem_key]["failures"] = failures + 1
                await state.save()
                logger.warning(f"⏭️ {rem_key} reminder deferred: channel unavailable (attempt {failures+1})")
                return False
            ping_role = discord.utils.get(ch.guild.roles, name=PING_ROLE_NAME)
            if not ping_role:
                state.state["pending_reminders"][rem_key]["failures"] = failures + 1
                await state.save()
                logger.warning(f"⏭️ {rem_key} reminder deferred: Event Ping! role not found")
                return False
            content = f"{ping_role.mention} Don't forget to vote in the upcoming tournament poll! Head over to <#{EVENT_CHANNEL_ID}> now."
            try:
                await ch.send(content, view=self.disable_view, allowed_mentions=ALLOWED_MENTIONS_ROLES)
                state.state["sent_reminders"][rem_key] = True
                state.state["pending_reminders"].pop(rem_key, None)
                self._seen_boundaries.add(rem_key)
                await state.save()
                logger.info(f"✅ Sent {rem_key} reminder")
                return True
            except Exception as e:
                state.state["pending_reminders"][rem_key]["failures"] = failures + 1
                state.state["pending_reminders"][rem_key]["last_attempt_at"] = now
                await state.save()
                logger.error(f"❌ Failed {rem_key} reminder (attempt {failures+1}): {e}")
                return False

    async def _first_reminder_worker(self) -> None:
        if self._first_reminder_started:
            return
        self._first_reminder_started = True
        try:
            now = datetime.now(LONDON_TZ)
            remainder = now.minute % 15
            minutes_next = 15 - remainder if remainder else 0
            next_min = now.minute + minutes_next
            next_hour = now.hour + (next_min // 60)
            next_boundary = now.replace(hour=next_hour % 24, minute=next_min % 60, second=0, microsecond=0)
            wait = (next_boundary - now).total_seconds()
            if wait > 0:
                logger.info(f"⏳ First reminder at {next_boundary.strftime('%H:%M')} ({int(wait)}s)")
                await asyncio.sleep(wait)

            rem_key = state.get_reminder_key(next_boundary)
            async with self.task_locks["reminder"]:
                if rem_key not in state.state["sent_reminders"]:
                    await self._send_reminder_message(rem_key)
                else:
                    logger.info(f"⏭️ {rem_key} already sent, skipping")
        except Exception as e:
            logger.error(f"❌ First reminder task failed: {e}")

    async def _1400_announcement(self) -> None:
        async with self.task_locks["1400"]:
            ch = await _fetch_channel_safe(self, EVENT_CHANNEL_ID, "event channel")
            if not ch:
                return
            try:
                await ch.send("@everyone Tournament starts in 1 hour!", allowed_mentions=ALLOWED_MENTIONS_EVERYONE)
                logger.info("✅ Sent 14:00 announcement")
            except Exception as e:
                logger.error(f"❌ 14:00 announcement failed: {e}")

    async def _1500_daily_reset(self) -> None:
        async with self.task_locks["1500"]:
            state.state["allowed_to_disable"] = []
            state.state["disable_permission_session"] = None
            await state.save()
            for guild in self.guilds:
                self._track_task(_remove_attendee_role_bulk(guild), f"reset_roles_{guild.id}")
            logger.info("✅ 15:00 daily reset initiated")

    async def _assign_ping_roles_all(self, guild: discord.Guild) -> None:
        ping_role = discord.utils.get(guild.roles, name=PING_ROLE_NAME)
        if not ping_role or not guild.me or not guild.me.guild_permissions.manage_roles:
            logger.warning("❌ Cannot assign Ping role: missing role or permission")
            return
        if not _check_role_assignable(guild, ping_role):
            logger.warning("❌ Cannot assign Ping role: hierarchy too low")
            return
        await _chunk_guild_safe(guild)
        opted = set(state.state.get("opted_out_users", []))
        candidates = [m for m in guild.members if not m.bot and str(m.id) not in opted and ping_role not in m.roles]
        if candidates:
            count = await _batch_assign_roles(candidates, ping_role, add=True)
            logger.info(f"✅ Assigned Ping role to {count} members")

    async def _1800_create_poll(self) -> None:
        async with self.task_locks["1800"]:
            today = state.get_today_key()
            ch = await _fetch_channel_safe(self, EVENT_CHANNEL_ID, "event channel")
            if not ch:
                return
            state.state["voted_users"] = {}
            state.state["allowed_to_disable"] = []
            state.state["disable_permission_session"] = None
            state.state["opted_out_users"] = []
            state.state["poll_session_date"] = today
            try:
                embed = discord.Embed(title=TODAY_POLL_TITLE, color=discord.Color.blue())
                msg = await ch.send(content="@everyone", embed=embed, view=self.poll_view,
                                    allowed_mentions=ALLOWED_MENTIONS_EVERYONE)
                state.state["current_poll_msg_id"] = msg.id
                await state.save()
                logger.info("✅ Created today's poll")
                for g in self.guilds:
                    self._track_task(self._assign_ping_roles_all(g), f"ping_roles_{g.id}")
            except Exception as e:
                logger.error(f"❌ Failed to create today's poll: {e}")

    async def _startup_poll(self) -> None:
        async with self.task_locks["tomorrow_poll"]:
            tomorrow = state.get_tomorrow_key()
            ch = await _fetch_channel_safe(self, EVENT_CHANNEL_ID, "event channel")
            if not ch:
                return

            stored_id = state.state.get("tomorrow_poll_msg_id")
            stored_target = state.state.get("tomorrow_poll_target_date")
            valid = False

            if stored_id and stored_target == tomorrow:
                try:
                    await ch.fetch_message(stored_id)
                    valid = True
                    logger.info("✅ Found existing tomorrow poll via stored ID")
                except NotFound:
                    state.state["tomorrow_poll_msg_id"] = None
                    state.state["tomorrow_poll_created_date"] = None
                    state.state["tomorrow_poll_target_date"] = None
                    await state.save()
                except Exception as e:
                    logger.warning(f"⚠️ Error verifying stored tomorrow poll: {e}")

            if not valid:
                async for msg in ch.history(limit=HISTORY_LIMIT):
                    if msg.author == self.user and msg.embeds:
                        if getattr(msg.embeds[0], "title", None) == TOMORROW_POLL_TITLE:
                            try:
                                msg_dt = msg.created_at.astimezone(LONDON_TZ)
                                msg_date = msg_dt.strftime("%Y-%m-%d")
                            except Exception:
                                continue
                            if msg_date == tomorrow:
                                valid = True
                                state.state["tomorrow_poll_msg_id"] = msg.id
                                state.state["tomorrow_poll_created_date"] = tomorrow
                                state.state["tomorrow_poll_target_date"] = tomorrow
                                await state.save()
                                logger.info("✅ Found existing tomorrow poll via history")
                                break

            if valid:
                return

            try:
                embed = discord.Embed(title=TOMORROW_POLL_TITLE, color=discord.Color.gold())
                msg = await ch.send(content="@everyone", embed=embed, view=self.tomorrow_poll_view,
                                    allowed_mentions=ALLOWED_MENTIONS_EVERYONE)
                state.state["tomorrow_poll_msg_id"] = msg.id
                state.state["tomorrow_poll_created_date"] = tomorrow
                state.state["tomorrow_poll_target_date"] = tomorrow
                state.state["tomorrow_voted_users"] = {}
                await state.save()
                logger.info("✅ Created tomorrow poll")
            except Exception as e:
                logger.error(f"❌ Failed to create tomorrow poll: {e}")

    async def _scheduler_loop(self) -> None:
        logger.info("✅ Scheduler running")
        while not self.is_closed():
            try:
                now = datetime.now(LONDON_TZ)
                today = now.strftime("%Y-%m-%d")

                if now.hour != self._health_check_hour:
                    self._health_check_hour = now.hour
                    for guild in self.guilds:
                        _permission_health_check(guild)

                await state.cleanup_old_reminders()

                remainder = now.minute % 15
                if remainder == 0 and 5 <= now.second <= 35:
                    rem_key = state.get_reminder_key(now.replace(second=0, microsecond=0))
                    async with self.task_locks["reminder"]:
                        if rem_key not in state.state["sent_reminders"]:
                            await self._send_reminder_message(rem_key)
                        else:
                            logger.debug(f"⏭️ {rem_key} already sent")

                t14 = now.replace(hour=14, minute=0, second=0, microsecond=0)
                if now >= t14 and state.state["last_1400_run_date"] != today:
                    state.state["last_1400_run_date"] = today
                    await self._1400_announcement()
                    state.state["last_1400_status"] = "completed"
                    await state.save()

                t15 = now.replace(hour=15, minute=0, second=0, microsecond=0)
                if now >= t15 and state.state["last_1500_reset_date"] != today:
                    state.state["last_1500_reset_date"] = today
                    await self._1500_daily_reset()
                    state.state["last_1500_reset_status"] = "completed"
                    await state.save()

                t18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
                if now >= t18 and state.state["last_1800_poll_date"] != today:
                    state.state["last_1800_poll_date"] = today
                    await self._1800_create_poll()
                    state.state["last_1800_poll_status"] = "completed"
                    await state.save()

                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"❌ Scheduler loop error: {e}")
                await asyncio.sleep(60)


if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        logger.error("❌ DISCORD_BOT_TOKEN environment variable not set")
        exit(1)
    bot = TournamentBot()
    bot.run(TOKEN)
