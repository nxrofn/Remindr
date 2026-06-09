import logging
import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import database

load_dotenv()
log = logging.getLogger(__name__)

DURATION_RE = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)
UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: str) -> timedelta | None:
    m = DURATION_RE.match(value.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return timedelta(**{UNITS[unit]: n})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReminderBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        database.init_db()
        self._register_commands()
        await self.tree.sync()
        self._check_reminders.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%d)", self.user, self.user.id)

    def _register_commands(self) -> None:
        @self.tree.command(description="Set a reminder (e.g. /remind 10m Take a break)")
        @app_commands.describe(when="Duration: 10m, 2h, 1d", message="What to remind you about")
        async def remind(interaction: discord.Interaction, when: str, message: str) -> None:
            duration = parse_duration(when)
            if not duration:
                await interaction.response.send_message(
                    "Invalid time — use `<n>m`, `<n>h`, or `<n>d` (e.g. `30m`, `2h`, `1d`).",
                    ephemeral=True,
                )
                return

            due_at = utcnow() + duration
            reminder_id = database.add_reminder(
                user_id=interaction.user.id,
                channel_id=interaction.channel_id,
                message=message,
                due_at=due_at,
            )
            ts = discord.utils.format_dt(due_at, style="R")
            await interaction.response.send_message(
                f"Reminder #{reminder_id} set — I'll ping you {ts}.\n> {message}"
            )

        @self.tree.command(description="List your active reminders")
        async def reminders(interaction: discord.Interaction) -> None:
            rows = database.get_user_reminders(interaction.user.id)
            if not rows:
                await interaction.response.send_message("No active reminders.", ephemeral=True)
                return

            lines = [
                f"**#{r['id']}** {discord.utils.format_dt(datetime.fromisoformat(r['due_at']), style='R')} — {r['message']}"
                for r in rows
            ]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(description="Delete a reminder by ID")
        @app_commands.describe(id="Reminder ID from /reminders")
        async def delete(interaction: discord.Interaction, id: int) -> None:
            if database.delete_reminder(id, interaction.user.id):
                await interaction.response.send_message(f"Reminder #{id} deleted.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"No reminder #{id} found (or it's not yours).", ephemeral=True
                )

    @tasks.loop(seconds=30)
    async def _check_reminders(self) -> None:
        for row in database.get_due_reminders(utcnow()):
            await self._deliver(row)
            database.delete_reminder(row["id"], row["user_id"])

    @_check_reminders.before_loop
    async def _before_check(self) -> None:
        await self.wait_until_ready()

    async def _deliver(self, row: dict) -> None:
        content = f"⏰ Reminder: {row['message']}"
        user = self.get_user(row["user_id"]) or await self.fetch_user(row["user_id"])

        try:
            await user.send(content)
            return
        except (discord.Forbidden, discord.HTTPException):
            pass

        channel = self.get_channel(row["channel_id"])
        if channel:
            try:
                await channel.send(f"<@{row['user_id']}> {content}")
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
