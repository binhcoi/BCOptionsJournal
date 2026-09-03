"""Read and write the book. Decimals in, Decimals out; never a float."""

import hashlib
import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from ..domain.enums import (
    Direction,
    DisposalKind,
    MatchingRule,
    Right,
    ShareSource,
    Status,
)
from ..domain.types import Position, ShareDisposal, ShareLot
from ..engine.shares import InsufficientSharesError, match
from . import schema


def _d(text) -> Decimal | None:
    return None if text is None or text == "" else Decimal(text)


def _s(value) -> str | None:
    return None if value is None else str(value)


def _date(text) -> date | None:
    return None if not text else date.fromisoformat(text)


def _iso(value) -> str | None:
    return None if value is None else value.isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: str) -> sqlite3.Connection:
    """Open (creating if needed) and bring the schema up to date."""
    conn = schema.connect(path)
    schema.migrate(conn)
    return conn


# --------------------------------------------------------------------------
# settings


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value) -> None:
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def matching_rule(conn) -> MatchingRule:
    return MatchingRule(get_setting(conn, "share_matching_rule", "FIFO"))


# --------------------------------------------------------------------------
# positions


def save_positions(conn, positions, batch_id: str | None = None) -> int:
    """Insert positions. Chain links are set in a second pass, so a roll may
    legally reference a row that appears later in the file."""
    rows = list(positions)
    with conn:
        conn.executemany(
            """
            INSERT INTO positions (
                id, underlying, expiry, strike, option_right, direction,
                quantity, multiplier, opened_on, open_price, open_fee,
                closed_on, close_price, close_fee, status,
                target_pct, notes, import_batch_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    p.id,
                    p.underlying,
                    _iso(p.expiry),
                    _s(p.strike),
                    p.right.value,
                    p.direction.value,
                    p.quantity,
                    p.multiplier,
                    _iso(p.opened_on),
                    _s(p.open_price),
                    _s(p.open_fee),
                    _iso(p.closed_on),
                    _s(p.close_price),
                    _s(p.close_fee),
                    p.status.value,
                    _s(p.target_pct),
                    p.notes,
                    batch_id,
                )
                for p in rows
            ],
        )

        known = {p.id for p in rows}
        links = [
            (p.rolled_from_id, p.split_from_id, p.id)
            for p in rows
            if (p.rolled_from_id in known) or (p.split_from_id in known)
        ]
        if links:
            conn.executemany(
                "UPDATE positions SET rolled_from_id = ?, split_from_id = ? "
                "WHERE id = ?",
                links,
            )

    return len(rows)


def load_positions(conn, underlying: str | None = None) -> list[Position]:
    sql = "SELECT * FROM positions"
    params: tuple = ()
    if underlying:
        sql += " WHERE underlying = ?"
        params = (underlying.upper(),)
    sql += " ORDER BY opened_on, id"

    return [_position(row) for row in conn.execute(sql, params)]


def _position(row) -> Position:
    return Position(
        id=row["id"],
        underlying=row["underlying"],
        expiry=_date(row["expiry"]),
        strike=_d(row["strike"]),
        right=Right(row["option_right"]),
        direction=Direction(row["direction"]),
        quantity=row["quantity"],
        multiplier=row["multiplier"],
        opened_on=_date(row["opened_on"]),
        open_price=_d(row["open_price"]),
        open_fee=_d(row["open_fee"]) or Decimal("0"),
        closed_on=_date(row["closed_on"]),
        close_price=_d(row["close_price"]),
        close_fee=_d(row["close_fee"]) or Decimal("0"),
        status=Status(row["status"]),
        rolled_from_id=row["rolled_from_id"],
        split_from_id=row["split_from_id"],
        # Without this the split ratio is lost on reload and both halves
        # silently inherit the whole chain history.
        split_from_quantity=row["split_from_quantity"],
        share_lot_id=row["share_lot_id"],
        spread_group_id=row["spread_group_id"],
        target_pct=_d(row["target_pct"]),
        notes=row["notes"] or "",
    )


# --------------------------------------------------------------------------
# shares


def save_shares(conn, lots, disposals, batch_id: str | None = None) -> None:
    with conn:
        conn.executemany(
            """
            INSERT INTO share_lots (
                id, underlying, quantity, acquired_on, cost_per_share, fee,
                source, assigning_position_id, estimated, notes, import_batch_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    l.id,
                    l.underlying,
                    l.quantity,
                    _iso(l.acquired_on),
                    _s(l.cost_per_share),
                    _s(l.fee),
                    l.source.value,
                    l.assigning_position_id,
                    1 if l.estimated else 0,
                    l.notes,
                    batch_id,
                )
                for l in lots
            ],
        )
        conn.executemany(
            """
            INSERT INTO share_disposals (
                id, underlying, quantity, disposed_on, proceeds_per_share, fee,
                kind, disposing_position_id, specific_lot_ids, notes,
                import_batch_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    d.id,
                    d.underlying,
                    d.quantity,
                    _iso(d.disposed_on),
                    _s(d.proceeds_per_share),
                    _s(d.fee),
                    d.kind.value,
                    d.disposing_position_id,
                    "\n".join(d.specific_lot_ids),
                    d.notes,
                    batch_id,
                )
                for d in disposals
            ],
        )


def load_lots(conn, underlying: str | None = None) -> list[ShareLot]:
    sql = "SELECT * FROM share_lots"
    params: tuple = ()
    if underlying:
        sql += " WHERE underlying = ?"
        params = (underlying.upper(),)
    sql += " ORDER BY acquired_on, id"
    return [
        ShareLot(
            id=r["id"],
            underlying=r["underlying"],
            quantity=r["quantity"],
            acquired_on=_date(r["acquired_on"]),
            cost_per_share=_d(r["cost_per_share"]),
            fee=_d(r["fee"]) or Decimal("0"),
            source=ShareSource(r["source"]),
            assigning_position_id=r["assigning_position_id"],
            estimated=bool(r["estimated"]),
            notes=r["notes"] or "",
        )
        for r in conn.execute(sql, params)
    ]


def load_disposals(conn, underlying: str | None = None) -> list[ShareDisposal]:
    sql = "SELECT * FROM share_disposals"
    params: tuple = ()
    if underlying:
        sql += " WHERE underlying = ?"
        params = (underlying.upper(),)
    sql += " ORDER BY disposed_on, id"
    return [
        ShareDisposal(
            id=r["id"],
            underlying=r["underlying"],
            quantity=r["quantity"],
            disposed_on=_date(r["disposed_on"]),
            proceeds_per_share=_d(r["proceeds_per_share"]),
            fee=_d(r["fee"]) or Decimal("0"),
            kind=DisposalKind(r["kind"]),
            disposing_position_id=r["disposing_position_id"],
            specific_lot_ids=tuple(
                filter(None, (r["specific_lot_ids"] or "").split("\n"))
            ),
            notes=r["notes"] or "",
        )
        for r in conn.execute(sql, params)
    ]


def rebuild_allocations(conn, rule: MatchingRule | None = None) -> dict[str, str]:
    """Drop and replay every share allocation.

    Allocations are derived, so this is the whole point of storing lots and
    disposals rather than net figures: changing the matching rule is a rebuild,
    not a migration. Returns any per-ticker errors (a missing acquisition).
    """
    rule = rule or matching_rule(conn)
    errors: dict[str, str] = {}

    lots = load_lots(conn)
    disposals = load_disposals(conn)
    underlyings = sorted({l.underlying for l in lots} | {d.underlying for d in disposals})

    with conn:
        conn.execute("DELETE FROM share_allocations")
        for underlying in underlyings:
            subset_lots = [l for l in lots if l.underlying == underlying]
            subset_disposals = [d for d in disposals if d.underlying == underlying]
            try:
                result = match(subset_lots, subset_disposals, rule)
            except InsufficientSharesError as exc:
                errors[underlying] = str(exc)
                continue

            conn.executemany(
                "INSERT INTO share_allocations "
                "(disposal_id, lot_id, quantity, realized_pl, cost_basis, proceeds) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (
                        a.disposal_id,
                        a.lot_id,
                        a.quantity,
                        _s(a.realized_pl),
                        _s(a.cost_basis),
                        _s(a.proceeds),
                    )
                    for a in result.allocations
                ],
            )

    return errors


# --------------------------------------------------------------------------
# flags, batches, audit


def save_flags(conn, flags, entity_type: str = "position") -> int:
    rows = list(flags)  # may be a generator; needed twice
    raised = _now()
    with conn:
        conn.executemany(
            "INSERT INTO flags (entity_type, entity_id, kind, detail, raised_at) "
            "VALUES (?,?,?,?,?)",
            [(entity_type, f.entity_id, f.kind, f.detail, raised) for f in rows],
        )
    return len(rows)


def open_flags(conn) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM flags WHERE resolved_at IS NULL ORDER BY kind, entity_id"
        )
    )


def resolve_flag(conn, flag_id: int, resolution: str = "") -> None:
    with conn:
        conn.execute(
            "UPDATE flags SET resolved_at = ?, resolution = ? WHERE id = ?",
            (_now(), resolution, flag_id),
        )


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


class AlreadyImportedError(RuntimeError):
    """This exact file has been imported before.

    Re-importing an overlapping export must be a no-op, not a duplicate.
    """


def record_batch(conn, path: str, row_count: int, report: str = "") -> str:
    digest = file_hash(path)
    existing = conn.execute(
        "SELECT id FROM import_batches WHERE file_hash = ?", (digest,)
    ).fetchone()
    if existing:
        raise AlreadyImportedError(
            f"{path} was already imported as batch {existing['id']}"
        )

    batch_id = uuid.uuid4().hex
    with conn:
        conn.execute(
            "INSERT INTO import_batches "
            "(id, filename, file_hash, row_count, imported_at, report) "
            "VALUES (?,?,?,?,?,?)",
            (batch_id, path, digest, row_count, _now(), report),
        )
    return batch_id


def revert_batch(conn, batch_id: str) -> None:
    """Remove everything an import wrote. A batch is revertible whole."""
    with conn:
        conn.execute(
            "UPDATE positions SET rolled_from_id = NULL, split_from_id = NULL "
            "WHERE import_batch_id = ?",
            (batch_id,),
        )
        for table in (
            "share_allocations",
            "share_disposals",
            "share_lots",
            "positions",
        ):
            if table == "share_allocations":
                conn.execute(
                    "DELETE FROM share_allocations WHERE disposal_id IN "
                    "(SELECT id FROM share_disposals WHERE import_batch_id = ?)",
                    (batch_id,),
                )
            else:
                conn.execute(
                    f"DELETE FROM {table} WHERE import_batch_id = ?", (batch_id,)
                )
        conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))


def log(conn, entity_type, entity_id, action, before=None, after=None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (at, entity_type, entity_id, action, before, after) "
            "VALUES (?,?,?,?,?,?)",
            (
                _now(),
                entity_type,
                entity_id,
                action,
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
            ),
        )


# --------------------------------------------------------------------------
# single-record writes, with an audit trail
#
# Every mutation goes through apply(), so nothing changes without a log entry.
# The data is hand-entered and irreplaceable; a typo needs to be findable and
# reversible six months later.

_POSITION_COLUMNS = (
    "id", "underlying", "expiry", "strike", "option_right", "direction",
    "quantity", "multiplier", "opened_on", "open_price", "open_fee",
    "closed_on", "close_price", "close_fee", "status", "rolled_from_id",
    "split_from_id", "split_from_quantity", "share_lot_id", "spread_group_id",
    "target_pct", "notes",
)


def _position_values(p: Position) -> tuple:
    return (
        p.id, p.underlying, _iso(p.expiry), _s(p.strike), p.right.value,
        p.direction.value, p.quantity, p.multiplier, _iso(p.opened_on),
        _s(p.open_price), _s(p.open_fee), _iso(p.closed_on),
        _s(p.close_price), _s(p.close_fee), p.status.value, p.rolled_from_id,
        p.split_from_id, p.split_from_quantity, p.share_lot_id,
        p.spread_group_id, _s(p.target_pct), p.notes,
    )


def _row_as_dict(conn, table: str, key: str):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (key,)).fetchone()
    return dict(row) if row else None


def _insert_position(conn, p: Position, batch_id=None) -> None:
    placeholders = ",".join("?" * (len(_POSITION_COLUMNS) + 1))
    conn.execute(
        f"INSERT INTO positions ({','.join(_POSITION_COLUMNS)}, import_batch_id)"
        f" VALUES ({placeholders})",
        _position_values(p) + (batch_id,),
    )


def _update_position(conn, p: Position) -> None:
    assignments = ",".join(f"{c} = ?" for c in _POSITION_COLUMNS if c != "id")
    values = tuple(
        v for c, v in zip(_POSITION_COLUMNS, _position_values(p)) if c != "id"
    )
    conn.execute(f"UPDATE positions SET {assignments} WHERE id = ?", values + (p.id,))


def _insert_lot(conn, lot: ShareLot, batch_id=None) -> None:
    conn.execute(
        "INSERT INTO share_lots (id, underlying, quantity, acquired_on,"
        " cost_per_share, fee, source, assigning_position_id, estimated,"
        " notes, import_batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            lot.id, lot.underlying, lot.quantity, _iso(lot.acquired_on),
            _s(lot.cost_per_share), _s(lot.fee), lot.source.value,
            lot.assigning_position_id, 1 if lot.estimated else 0, lot.notes,
            batch_id,
        ),
    )


def _insert_disposal(conn, d: ShareDisposal, batch_id=None) -> None:
    conn.execute(
        "INSERT INTO share_disposals (id, underlying, quantity, disposed_on,"
        " proceeds_per_share, fee, kind, disposing_position_id,"
        " specific_lot_ids, notes, import_batch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            d.id, d.underlying, d.quantity, _iso(d.disposed_on),
            _s(d.proceeds_per_share), _s(d.fee), d.kind.value,
            d.disposing_position_id, "\n".join(d.specific_lot_ids), d.notes,
            batch_id,
        ),
    )


def apply(conn, result, note: str = "") -> dict[str, str]:
    """Write everything an action produced, atomically, with an audit entry each.

    Returns any per-ticker share-matching errors. Allocations are rebuilt after
    the commit rather than inside it: they are derived, so a failure there
    leaves them stale rather than leaving the positions half-written, and the
    next rebuild fixes it.
    """
    summary = note or getattr(result, "summary", "")

    with conn:
        for p in result.updated:
            before = _row_as_dict(conn, "positions", p.id)
            _update_position(conn, p)
            _audit(conn, "position", p.id, "update", before, _position_dict(p), summary)
        for p in result.created:
            _insert_position(conn, p)
            _audit(conn, "position", p.id, "create", None, _position_dict(p), summary)
        for lot in result.lots:
            _insert_lot(conn, lot)
            _audit(conn, "share_lot", lot.id, "create", None,
                   {"underlying": lot.underlying, "quantity": lot.quantity,
                    "cost_per_share": str(lot.cost_per_share),
                    "source": lot.source.value}, summary)
        for d in result.disposals:
            _insert_disposal(conn, d)
            _audit(conn, "share_disposal", d.id, "create", None,
                   {"underlying": d.underlying, "quantity": d.quantity,
                    "proceeds_per_share": str(d.proceeds_per_share),
                    "kind": d.kind.value}, summary)

    if result.lots or result.disposals:
        return rebuild_allocations(conn)
    return {}


def _position_dict(p: Position) -> dict:
    return dict(zip(_POSITION_COLUMNS, _position_values(p)))


def _audit(conn, entity_type, entity_id, action, before, after, note) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, entity_type, entity_id, action, before, after)"
        " VALUES (?,?,?,?,?,?)",
        (
            _now(), entity_type, entity_id, f"{action}: {note}" if note else action,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
        ),
    )


def load_position(conn, position_id: str) -> Position | None:
    row = conn.execute(
        "SELECT * FROM positions WHERE id = ?", (position_id,)
    ).fetchone()
    return _position(row) if row else None


def open_positions(conn) -> list[Position]:
    return [
        _position(r)
        for r in conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN'"
            " ORDER BY expiry, underlying, strike"
        )
    ]


def recent_underlyings(conn, limit: int = 40) -> list[str]:
    """Tickers by most recent use -- the autocomplete order that actually helps."""
    return [
        r["underlying"]
        for r in conn.execute(
            "SELECT underlying, MAX(opened_on) AS last FROM positions"
            " GROUP BY underlying ORDER BY last DESC LIMIT ?",
            (limit,),
        )
    ]


def audit_entries(conn, limit: int = 200, entity_id: str | None = None):
    if entity_id:
        return list(
            conn.execute(
                "SELECT * FROM audit_log WHERE entity_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (entity_id, limit),
            )
        )
    return list(
        conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    )


def revert_audit_entry(conn, entry_id: int) -> str:
    """Undo one logged change by restoring its ``before`` state.

    A create is undone by deleting; an update by writing the previous values
    back. The reversal is itself logged, so undo is auditable too.
    """
    entry = conn.execute(
        "SELECT * FROM audit_log WHERE id = ?", (entry_id,)
    ).fetchone()
    if entry is None:
        raise ValueError(f"no audit entry {entry_id}")
    if entry["entity_type"] != "position":
        raise ValueError("only position changes can be reverted automatically")

    before = json.loads(entry["before"]) if entry["before"] else None
    with conn:
        if before is None:
            conn.execute("DELETE FROM positions WHERE id = ?", (entry["entity_id"],))
            outcome = f"deleted {entry['entity_id'][:8]}"
        else:
            columns = [c for c in _POSITION_COLUMNS if c != "id"]
            assignments = ",".join(f"{c} = ?" for c in columns)
            conn.execute(
                f"UPDATE positions SET {assignments} WHERE id = ?",
                tuple(before.get(c) for c in columns) + (entry["entity_id"],),
            )
            outcome = f"restored {entry['entity_id'][:8]}"
        _audit(conn, "position", entry["entity_id"], "revert", None, before,
               f"undo of audit #{entry_id}")
    return outcome
