import logging
import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

import database
import errors

load_dotenv()
log = logging.getLogger(__name__)

DURATION_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)
UNITS = {"m": "minutes", "h": "hours", "d": "days"}

MAX_REMINDERS_PER_USER = 25
SNOOZE_OPTIONS = [("5 min", "5m"), ("15 min", "15m"), ("1 hour", "1h"), ("Tomorrow", "1d")]


def parse_duration(value: str) -> timedelta | None:
    m = DURATION_RE.match(value.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return timedelta(**{UNITS[unit]: n})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_tz(user_id: int) -> timezone | zoneinfo.ZoneInfo:
    name = database.get_user_timezone(user_id)
    if name:
        try:
            return zoneinfo.ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def build_target_mentions(targets_str: str | None) -> str:
    if not targets_str:
        return ""
    parts = []
    for entry in targets_str.split(","):
        entry = entry.strip()
        if entry.startswith("role:"):
            parts.append(f"<@&{entry[5:]}>")
        elif entry.startswith("user:"):
            parts.append(f"<@{entry[5:]}>")
    return " ".join(parts)


def reminder_embed(rows: list, title: str = "Your Reminders") -> discord.Embed:
    embed = discord.Embed(title=title, color=0x5865F2)
    for r in rows:
        due = datetime.fromisoformat(r["due_at"])
        ts = discord.utils.format_dt(due, style="R")
        label = f"{r['id']} — {ts}"
        if r["snooze_count"]:
            label += f" *(snoozed {r['snooze_count']}×)*"
        if r["recur"]:
            label += f" 🔁 every {r['recur']}"
        body = r["message"]
        if r["targets"]:
            body += f"\n*Also notifying: {build_target_mentions(r['targets'])}*"
        embed.add_field(name=label, value=body, inline=False)
    embed.set_footer(text=f"{len(rows)} active reminder{'s' if len(rows) != 1 else ''}")
    return embed


class SnoozeView(discord.ui.View):
    def __init__(self, reminder_id: int, user_id: int) -> None:
        super().__init__(timeout=300)
        self.reminder_id = reminder_id
        self.user_id = user_id

        for label, code in SNOOZE_OPTIONS:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            btn.callback = self._make_callback(code)
            self.add_item(btn)

        dismiss = discord.ui.Button(label="Dismiss", style=discord.ButtonStyle.danger)
        dismiss.callback = self._dismiss
        self.add_item(dismiss)

    def _make_callback(self, code: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("Not your reminder.", ephemeral=True)
                return
            delta = parse_duration(code)
            new_due = utcnow() + delta
            if database.snooze_reminder(self.reminder_id, self.user_id, new_due):
                ts = discord.utils.format_dt(new_due, style="R")
                await interaction.response.edit_message(
                    content=f"⏰ Snoozed! I'll remind you again {ts}.",
                    view=None,
                )
            else:
                await interaction.response.send_message(
                    "Couldn't find that reminder — it may have been deleted.",
                    ephemeral=True,
                )
            self.stop()
        return callback

    async def _dismiss(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your reminder.", ephemeral=True)
            return
        database.delete_reminder(self.reminder_id, self.user_id)
        await interaction.response.edit_message(content="✅ Reminder dismissed.", view=None)
        self.stop()


class ReminderBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        database.init_db()
        errors.register(self.tree)
        self._register_commands()
        await self.tree.sync()
        self._check_reminders.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%d)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="for /remind",
        ))

    def _register_commands(self) -> None:

        @self.tree.command(description="Configure the bot for this server (admin only)")
        @app_commands.describe(fallback_channel="Channel used when a reminder DM can't be sent")
        @app_commands.default_permissions(administrator=True)
        @app_commands.guild_only()
        async def setup(
            interaction: discord.Interaction,
            fallback_channel: discord.TextChannel,
        ) -> None:
            database.set_fallback_channel(interaction.guild_id, fallback_channel.id)
            await interaction.response.send_message(
                f"Done. Failed DMs will fall back to {fallback_channel.mention}.",
                ephemeral=True,
            )

        @self.tree.command(description="Set your timezone for accurate reminder times")
        @app_commands.describe(tz="IANA timezone name, e.g. America/New_York or Europe/London")
        async def timezone(interaction: discord.Interaction, tz: str) -> None:
            try:
                zoneinfo.ZoneInfo(tz)
            except (zoneinfo.ZoneInfoNotFoundError, KeyError):
                await interaction.response.send_message(
                    f"Unknown timezone `{tz}`. Use an IANA name like `America/New_York`, "
                    "`Europe/London`, or `Asia/Tokyo`.",
                    ephemeral=True,
                )
                return
            database.set_user_timezone(interaction.user.id, tz)
            await interaction.response.send_message(
                f"Got it — your timezone is now set to **{tz}**. "
                "Future reminders will show times in your local zone.",
                ephemeral=True,
            )

        @self.tree.command(description="Set a reminder (e.g. /remind 10m Take a break)")
        @app_commands.describe(
            when="How long from now: 10m, 2h, 1d",
            message="What to remind you about",
            repeat="Repeat interval: 1h, 1d, 7d, etc. Leave blank for a one-time reminder.",
            mention="Extra @user or @role to ping alongside you (optional)",
        )
        async def remind(
            interaction: discord.Interaction,
            when: str,
            message: str,
            repeat: str | None = None,
            mention: str | None = None,
        ) -> None:
            delta = parse_duration(when)
            if not delta:
                await interaction.response.send_message(
                    "Invalid time — use `<n>m`, `<n>h`, or `<n>d` (e.g. `30m`, `2h`, `1d`).",
                    ephemeral=True,
                )
                return

            if repeat and not parse_duration(repeat):
                await interaction.response.send_message(
                    "Invalid repeat interval — same format as `when` (e.g. `1d`, `12h`).",
                    ephemeral=True,
                )
                return

            existing = database.get_user_reminders(interaction.user.id)
            if len(existing) >= MAX_REMINDERS_PER_USER:
                await interaction.response.send_message(
                    f"You're at the limit of {MAX_REMINDERS_PER_USER} active reminders. "
                    "Delete some with `/delete` first.",
                    ephemeral=True,
                )
                return

            targets: list[str] = []
            if mention:
                for role_id in re.findall(r"<@&(\d+)>", mention):
                    targets.append(f"role:{role_id}")
                for user_id_str in re.findall(r"<@!?(\d+)>", mention):
                    targets.append(f"user:{user_id_str}")

            due_at = utcnow() + delta
            reminder_id = database.add_reminder(
                user_id=interaction.user.id,
                guild_id=getattr(interaction.guild, "id", None),
                channel_id=interaction.channel_id,
                message=message,
                due_at=due_at,
                recur=repeat,
                targets=targets or None,
            )

            ts = discord.utils.format_dt(due_at, style="R")
            desc = f"⏰ I'll ping you {ts}.\n\n> {message}"
            if repeat:
                desc += f"\n\n🔁 Repeats every **{repeat}**"
            if targets:
                mentions_preview = build_target_mentions(",".join(targets))
                desc += f"\n👥 Also notifying: {mentions_preview}"

            embed = discord.Embed(description=desc, color=0x57F287)
            embed.set_footer(text=f"Reminder {reminder_id} • cancel anytime with /delete {reminder_id}")
            await interaction.response.send_message(embed=embed)

        @self.tree.command(name="remindgroup", description="Remind a role or set of users in a channel")
        @app_commands.describe(
            when="How long from now: 10m, 2h, 1d",
            message="What to remind everyone about",
            role="Role to ping (optional)",
            extra_user="An additional user to mention (optional)",
        )
        @app_commands.guild_only()
        async def remindgroup(
            interaction: discord.Interaction,
            when: str,
            message: str,
            role: discord.Role | None = None,
            extra_user: discord.Member | None = None,
        ) -> None:
            delta = parse_duration(when)
            if not delta:
                await interaction.response.send_message(
                    "Invalid time — use `<n>m`, `<n>h`, or `<n>d`.",
                    ephemeral=True,
                )
                return

            existing = database.get_user_reminders(interaction.user.id)
            if len(existing) >= MAX_REMINDERS_PER_USER:
                await interaction.response.send_message(
                    f"You're at the limit of {MAX_REMINDERS_PER_USER} reminders.",
                    ephemeral=True,
                )
                return

            targets: list[str] = []
            if role:
                targets.append(f"role:{role.id}")
            if extra_user:
                targets.append(f"user:{extra_user.id}")

            due_at = utcnow() + delta
            reminder_id = database.add_reminder(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                message=message,
                due_at=due_at,
                targets=targets or None,
            )

            ts = discord.utils.format_dt(due_at, style="R")
            mentions_preview = build_target_mentions(",".join(targets)) if targets else "no extra mentions"
            embed = discord.Embed(
                description=(
                    f"⏰ Group reminder set for {ts}.\n\n"
                    f"> {message}\n\n"
                    f"👥 Will ping: you + {mentions_preview}"
                ),
                color=0x5865F2,
            )
            embed.set_footer(text=f"Reminder {reminder_id}")
            await interaction.response.send_message(embed=embed)

        @self.tree.command(description="List your active reminders")
        async def reminders(interaction: discord.Interaction) -> None:
            rows = database.get_user_reminders(interaction.user.id)
            if not rows:
                await interaction.response.send_message("No active reminders.", ephemeral=True)
                return
            await interaction.response.send_message(embed=reminder_embed(rows), ephemeral=True)

        @self.tree.command(description="Delete a reminder by ID")
        @app_commands.describe(id="Reminder ID shown in /reminders")
        async def delete(interaction: discord.Interaction, id: int) -> None:
            if database.delete_reminder(id, interaction.user.id):
                await interaction.response.send_message(f"Reminder {id} deleted.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"No reminder {id} found (or it's not yours).",
                    ephemeral=True,
                )

        @self.tree.command(name="clear", description="Delete ALL of your reminders at once")
        async def clear(interaction: discord.Interaction) -> None:
            count = database.clear_user_reminders(interaction.user.id)
            if count:
                await interaction.response.send_message(
                    f"Cleared {count} reminder{'s' if count != 1 else ''}.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("No active reminders to clear.", ephemeral=True)

    @tasks.loop(seconds=30)
    async def _check_reminders(self) -> None:
        for row in database.get_due_reminders(utcnow()):
            await self._deliver(row)
            if row["recur"]:
                delta = parse_duration(row["recur"])
                if delta:
                    new_due = datetime.fromisoformat(row["due_at"]).replace(tzinfo=timezone.utc) + delta
                    database.reschedule_recurring(row["id"], new_due)
                    continue
            database.delete_reminder(row["id"], row["user_id"])

    @_check_reminders.before_loop
    async def _before_check(self) -> None:
        await self.wait_until_ready()

    @_check_reminders.error
    async def _check_error(self, error: Exception) -> None:
        log.exception("Error in reminder loop: %s", error)

    async def _deliver(self, row: dict) -> None:
        owner_mention = f"<@{row['user_id']}>"
        extra_mentions = build_target_mentions(row["targets"])
        all_mentions = f"{owner_mention} {extra_mentions}".strip()

        content = f"⏰ {all_mentions} **Reminder:** {row['message']}"
        if row["recur"]:
            content += f"\n*(repeats every {row['recur']})*"

        view = SnoozeView(row["id"], row["user_id"])

        user = self.get_user(row["user_id"]) or await self.fetch_user(row["user_id"])
        try:
            await user.send(content, view=view)
            return
        except (discord.Forbidden, discord.HTTPException):
            pass

        channel = self.get_channel(row["channel_id"])

        if guild_id := row["guild_id"]:
            if guild := self.get_guild(guild_id):
                fallback_id = database.get_fallback_channel(guild.id)
                if fallback_id:
                    channel = self.get_channel(fallback_id) or channel

        if channel:
            try:
                await channel.send(content, view=view)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass

        log.warning("Could not deliver reminder %d to user %d", row["id"], row["user_id"])


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    ReminderBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
