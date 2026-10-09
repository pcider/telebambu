from dataclasses import dataclass


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    description: str
    owner_only: bool = False


COMMANDS = (
    CommandDefinition("help", "Show available commands"),
    CommandDefinition("camera", "View printer camera"),
    CommandDefinition("notify", "Set a one-time notification"),
    CommandDefinition("notify_every", "Set recurring camera notifications"),
    CommandDefinition("info", "Show current print info"),
    CommandDefinition("unclaim", "Release a claimed print"),
    CommandDefinition("restart", "Restart a printer connection", owner_only=True),
    CommandDefinition("light", "Toggle printer light", owner_only=True),
)


def get_commands(owner_commands: bool = False) -> tuple[CommandDefinition, ...]:
    """Return commands available to the requested audience."""
    return tuple(command for command in COMMANDS if owner_commands or not command.owner_only)
