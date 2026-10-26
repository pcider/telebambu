from dataclasses import dataclass

from data import Role


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    description: str
    min_role: Role = Role.USER


COMMANDS = (
    CommandDefinition("help", "Show available commands"),
    CommandDefinition("info", "Show your prints or a print's details"),
    CommandDefinition("claim", "Claim an unclaimed print"),
    CommandDefinition("unclaim", "Release a claimed print"),
    CommandDefinition("anonymous", "Hide your name on your prints"),
    CommandDefinition("notify", "Set a one-time notification"),
    CommandDefinition("notify_every", "Set recurring camera notifications"),
    CommandDefinition("notify_user", "Notify other users about a print"),
    CommandDefinition("stop_at", "Stop a print at a layer"),
    CommandDefinition("pause", "Pause a print"),
    CommandDefinition("resume", "Resume a paused print"),
    CommandDefinition("speed", "Change a print's speed"),
    CommandDefinition("skip", "Skip objects on the plate"),
    CommandDefinition("camera", "View printer camera"),
    CommandDefinition("light", "Toggle printer light"),
    CommandDefinition("queue", "Get a DM when a printer is free"),
    CommandDefinition("unqueue", "Cancel a /queue reminder"),
    CommandDefinition("staff", "See the owner, admins and mods"),
    CommandDefinition("reportbug", "Report a bug to the maintainers"),
    CommandDefinition("suggestfeature", "Suggest a feature to the maintainers"),
    CommandDefinition("forcereclaim", "Give a print to someone else", Role.ADMIN),
    CommandDefinition("forceunclaim", "Unclaim someone else's print", Role.ADMIN),
    CommandDefinition("restart", "Reboot printers", Role.ADMIN),
    CommandDefinition("restartall", "Reboot every printer", Role.ADMIN),
    CommandDefinition("home", "Home the axes of idle printers", Role.ADMIN),
    CommandDefinition("print", "Print a sliced .3mf (testing)", Role.ADMIN),
    CommandDefinition("printer", "List, add, edit or remove printers", Role.ADMIN),
    CommandDefinition("config", "Show or change bot settings", Role.OWNER),
)


def get_commands(role: Role = Role.USER, disabled: frozenset[str] = frozenset()) -> tuple[CommandDefinition, ...]:
    """Return the commands available to a role, leaving out disabled ones."""
    return tuple(command for command in COMMANDS if role >= command.min_role and command.name not in disabled)
