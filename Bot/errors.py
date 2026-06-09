import logging

import discord
from discord import app_commands

log = logging.getLogger(__name__)


class ReminderBotError(Exception):
    """Base class for all ReminderBot errors."""


class ReminderNotFound(ReminderBotError):
    """Raised when a reminder ID doesn't exist or doesn't belong to the user."""

    def __init__(self, reminder_id: int) -> None:
        self.reminder_id = reminder_id
        super().__init__(f"Reminder #{reminder_id} not found.")


class ReminderLimitReached(ReminderBotError):
    """Raised when a user tries to create more reminders than the allowed limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"Reminder limit of {limit} reached.")


class InvalidDuration(ReminderBotError):
    """Raised when a duration string can't be parsed."""

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(f"Invalid duration: {value!r}")


class DatabaseError(ReminderBotError):
    """Raised when a database operation fails unexpectedly."""


class DeliveryError(ReminderBotError):
    """Raised when a reminder can't be delivered to the user or any fallback channel."""

    def __init__(self, reminder_id: int, user_id: int) -> None:
        self.reminder_id = reminder_id
        self.user_id = user_id
        super().__init__(
            f"Could not deliver reminder #{reminder_id} to user {user_id}."
        )


_USER_MESSAGES: dict[type, str] = {
    ReminderNotFound: "That reminder doesn't exist or doesn't belong to you.",
    ReminderLimitReached: "You've hit the reminder limit. Delete a few with `/delete` first.",
    InvalidDuration: "Invalid time format — use `<n>m`, `<n>h`, or `<n>d` (e.g. `30m`, `2h`, `1d`).",
    DatabaseError: "Something went wrong on our end. Please try again in a moment.",
    DeliveryError: "Your reminder fired but couldn't be delivered anywhere.",
}


def user_message(error: ReminderBotError) -> str:
    return _USER_MESSAGES.get(type(error), "An unexpected error occurred.")


async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    cause = getattr(error, "__cause__", error)

    if isinstance(cause, ReminderBotError):
        msg = user_message(cause)
        log.warning("Command error for user %d: %s", interaction.user.id, cause)
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to use that command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = "I'm missing permissions to do that. Check my role settings."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down! Try again in {error.retry_after:.0f}s."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "This command can only be used inside a server."
    else:
        msg = "Something went wrong. Please try again."
        log.exception(
            "Unhandled error in /%s for user %d",
            getattr(interaction.command, "name", "unknown"),
            interaction.user.id,
            exc_info=error,
        )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def register(tree: app_commands.CommandTree) -> None:
    tree.on_error = on_app_command_error
