from enum import IntEnum


class Role(IntEnum):
    """Staff roles, ordered by rank. MOD is cosmetic for now; ADMIN has full printer control."""
    USER = 0
    MOD = 1
    ADMIN = 2
    OWNER = 3

    @property
    def label(self) -> str:
        return self.name.capitalize()

    @classmethod
    def parse(cls, text: str) -> 'Role | None':
        text = text.lower()
        if text in ('user', 'none', 'remove'):
            return cls.USER
        return cls.__members__.get(text.upper())


def can_assign(actor: Role, current: Role, new: Role) -> bool:
    """Whether ``actor`` may move someone from ``current`` to ``new``.

    Staff can only add or remove roles strictly below their own, so the owner
    manages admins and mods while admins manage mods. Ownership changes hands
    through a transfer, which only the owner can start.
    """
    if new == Role.OWNER:
        return actor == Role.OWNER and current != Role.OWNER
    return actor > max(current, new)
