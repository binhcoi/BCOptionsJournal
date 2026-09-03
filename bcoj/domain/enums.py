"""Enumerations, with parsers for the legacy sheet's spellings."""

from enum import Enum


class Direction(Enum):
    """Which side of the contract we are on.

    The sheet encodes this in the sign of Quantity: positive is SHORT (sold to
    open), negative is LONG. Internally we store direction plus a positive
    quantity so no arithmetic depends on a sign convention.
    """

    SHORT = "SHORT"
    LONG = "LONG"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.SHORT else -1

    @classmethod
    def from_quantity(cls, signed_quantity: int) -> "Direction":
        if signed_quantity == 0:
            raise ValueError("quantity of zero has no direction")
        return cls.SHORT if signed_quantity > 0 else cls.LONG


class Right(Enum):
    CALL = "CALL"
    PUT = "PUT"

    @classmethod
    def parse(cls, raw: str) -> "Right":
        text = (raw or "").strip().upper()
        if text in {"C", "CALL"}:
            return cls.CALL
        if text in {"P", "PUT"}:
            return cls.PUT
        raise ValueError(f"unknown option type {raw!r}")


class Status(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    ROLLED = "ROLLED"
    ASSIGNED = "ASSIGNED"
    # A position divided into two children. Kept as a record of the event --
    # deleting a position that existed has no place in a journal -- but it
    # contributes nothing to any total: its premium and its history now belong
    # to the children.
    SPLIT = "SPLIT"

    @property
    def is_open(self) -> bool:
        return self is Status.OPEN

    @property
    def is_superseded(self) -> bool:
        """True when the children carry this position's premium and history.

        Every aggregation must skip these or it double-counts.
        """
        return self is Status.SPLIT

    @classmethod
    def parse(cls, raw: str) -> "Status":
        text = (raw or "").strip().upper()
        try:
            return cls[text]
        except KeyError:
            raise ValueError(f"unknown status {raw!r}") from None


class ShareSource(Enum):
    """How a share lot came into existence.

    OUTRIGHT_BUY is the case the legacy sheet had no room for, and the reason
    a ticker's share count there can go negative.
    """

    OUTRIGHT_BUY = "OUTRIGHT_BUY"
    BUY_WRITE = "BUY_WRITE"
    PUT_ASSIGNMENT = "PUT_ASSIGNMENT"
    CALL_EXERCISE = "CALL_EXERCISE"


class DisposalKind(Enum):
    SOLD = "SOLD"
    CALLED_AWAY = "CALLED_AWAY"


class MatchingRule(Enum):
    FIFO = "FIFO"
    LIFO = "LIFO"
    SPECIFIC = "SPECIFIC"
