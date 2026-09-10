"""Read and write the book. Decimals in, Decimals out; never a float."""

import hashlib
import json
import threading
from contextlib import contextmanager
import re
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

    tags = load_tags(conn)
    covers = load_covers(conn)
    return [_position(row, tags.get(row["id"], ()), covers.get(row["id"], ()))
            for row in conn.execute(sql, params)]


def load_covers(conn) -> dict[str, tuple[tuple[str, int], ...]]:
    out: dict[str, list] = {}
    for row in conn.execute("SELECT position_id, lot_id, shares FROM position_covers"
                            " ORDER BY position_id, rowid"):
        out.setdefault(row["position_id"], []).append((row["lot_id"], row["shares"]))
    return {pid: tuple(v) for pid, v in out.items()}


def _sync_covers(conn, p: Position) -> None:
    """The covers table and the share_lot_id column, kept to one truth."""
    conn.execute("DELETE FROM position_covers WHERE position_id = ?", (p.id,))
    for lot_id, n in p.covers:
        conn.execute("INSERT INTO position_covers (position_id, lot_id, shares) VALUES (?,?,?)",
                     (p.id, lot_id, n))
    conn.execute("UPDATE positions SET share_lot_id = ? WHERE id = ?",
                 (p.covers[0][0] if p.covers else None, p.id))


def _reconcile_covers(conn, position_id: str) -> None:
    """After a row is restored from the audit log, which knows only the
    share_lot_id column: no lot means no covers; a lot with no matching
    covers row means one cover for the whole call."""
    row = conn.execute("SELECT share_lot_id, quantity, multiplier FROM positions WHERE id = ?",
                       (position_id,)).fetchone()
    if row is None:
        return
    if row["share_lot_id"] is None:
        conn.execute("DELETE FROM position_covers WHERE position_id = ?", (position_id,))
        return
    have = conn.execute("SELECT lot_id FROM position_covers WHERE position_id = ?",
                        (position_id,)).fetchall()
    if not any(h["lot_id"] == row["share_lot_id"] for h in have):
        conn.execute("DELETE FROM position_covers WHERE position_id = ?", (position_id,))
        conn.execute("INSERT INTO position_covers (position_id, lot_id, shares) VALUES (?,?,?)",
                     (position_id, row["share_lot_id"], row["quantity"] * row["multiplier"]))


def _position(row, tags=(), covers=()) -> Position:
    return Position(
        tags=tuple(tags),
        covers=tuple(covers),
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

    with audit_group(), conn:
        # Positions first: a lot may name the trade it came with (a buy-write's
        # call, an assigned put), so the position must exist before the lot.
        for p in result.updated:
            before = _row_as_dict(conn, "positions", p.id)
            _update_position(conn, p)
            _sync_covers(conn, p)
            _audit(conn, "position", p.id, "update", before, _position_dict(p), summary)
        for p in result.created:
            _insert_position(conn, p)
            _audit(conn, "position", p.id, "create", None, _position_dict(p), summary)
            if p.tags:
                set_tags(conn, p.id, p.tags)
        for lot in result.lots:
            _insert_lot(conn, lot)
            _audit(conn, "share_lot", lot.id, "create", None,
                   _row_as_dict(conn, "share_lots", lot.id), summary)
        for d in result.disposals:
            _insert_disposal(conn, d)
            _audit(conn, "share_disposal", d.id, "create", None,
                   _row_as_dict(conn, "share_disposals", d.id), summary)
        for p in result.created:
            _sync_covers(conn, p)       # after the lots it might name exist

    if result.lots or result.disposals:
        return rebuild_allocations(conn)
    return {}


def _position_dict(p: Position) -> dict:
    return dict(zip(_POSITION_COLUMNS, _position_values(p)))


_GROUP = threading.local()


@contextmanager
def audit_group():
    """Everything audited inside belongs to one action, and is undone as one."""
    fresh = getattr(_GROUP, "id", None) is None
    if fresh:
        _GROUP.id = uuid.uuid4().hex
    try:
        yield _GROUP.id
    finally:
        if fresh:
            _GROUP.id = None


def _audit(conn, entity_type, entity_id, action, before, after, note) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, entity_type, entity_id, action, before, after, group_id)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            _now(), entity_type, entity_id, f"{action}: {note}" if note else action,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
            getattr(_GROUP, "id", None),
        ),
    )


def load_position(conn, position_id: str) -> Position | None:
    row = conn.execute(
        "SELECT * FROM positions WHERE id = ?", (position_id,)
    ).fetchone()
    return _position(row, load_tags(conn).get(position_id, ()), load_covers(conn).get(position_id, ())) if row else None


def open_positions(conn) -> list[Position]:
    return [
        _position(r)
        for r in conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN'"
            " ORDER BY expiry, underlying, strike"
        )
    ]


# --------------------------------------------------------------------------
# tags, notes and saved views


def load_tags(conn) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT pt.position_id AS pid, t.name AS name FROM position_tags pt"
        " JOIN tags t ON t.id = pt.tag_id ORDER BY t.name"
    ):
        out.setdefault(row["pid"], []).append(row["name"])
    return {pid: tuple(names) for pid, names in out.items()}


def all_tags(conn) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM tags ORDER BY name")]


def normalize_tags(raw: str) -> tuple[str, ...]:
    """Comma or space separated, a leading # allowed, case kept as first
    typed, duplicates dropped regardless of case."""
    seen: dict[str, str] = {}
    for part in re.split(r"[,\s]+", raw or ""):
        part = part.strip().lstrip("#")
        if part and part.casefold() not in seen:
            seen[part.casefold()] = part
    return tuple(seen.values())


def set_tags(conn, position_id: str, names) -> None:
    """Replace a position's tags. Runs inside the caller's transaction."""
    conn.execute("DELETE FROM position_tags WHERE position_id = ?", (position_id,))
    for name in names:
        row = conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        tag_id = row["id"] if row else uuid.uuid4().hex
        if row is None:
            conn.execute("INSERT INTO tags (id, name) VALUES (?, ?)", (tag_id, name))
        conn.execute(
            "INSERT INTO position_tags (position_id, tag_id) VALUES (?, ?)",
            (position_id, tag_id),
        )


def update_notes(conn, position_id: str, notes: str, tags, note: str = "notes edited") -> None:
    p = load_position(conn, position_id)
    if p is None:
        raise ValueError("no such position")
    before = dict(_position_dict(p), tags=list(p.tags))
    after = dict(before, notes=notes, tags=list(tags))
    with conn:
        conn.execute("UPDATE positions SET notes = ? WHERE id = ?", (notes, position_id))
        set_tags(conn, position_id, tags)
        _audit(conn, "position", position_id, "update", before, after, note)


def save_view(conn, name: str, filter_: str) -> str:
    """Store a filter under a name; the same name overwrites."""
    name = name.strip()
    if not name:
        raise ValueError("a view needs a name")
    with conn:
        row = conn.execute("SELECT id FROM saved_views WHERE name = ?", (name,)).fetchone()
        view_id = row["id"] if row else uuid.uuid4().hex
        conn.execute(
            "INSERT INTO saved_views (id, name, filter) VALUES (?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET filter = excluded.filter",
            (view_id, name, filter_),
        )
    return view_id


def load_views(conn) -> list[dict]:
    """Most recently saved first: the line shows the newest two."""
    return [dict(r) for r in conn.execute("SELECT id, name, filter FROM saved_views ORDER BY rowid DESC")]


def delete_view(conn, view_id: str) -> None:
    with conn:
        conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))


# --------------------------------------------------------------------------
# repairing positions


def position_links(conn, position_id: str) -> list[str]:
    """What still refers to a position: the reasons it cannot be removed."""
    reasons = []
    n = conn.execute("SELECT COUNT(*) AS n FROM positions WHERE rolled_from_id = ?",
                     (position_id,)).fetchone()["n"]
    if n:
        reasons.append(f"{n} position(s) rolled from it")
    n = conn.execute("SELECT COUNT(*) AS n FROM positions WHERE split_from_id = ?",
                     (position_id,)).fetchone()["n"]
    if n:
        reasons.append(f"{n} position(s) split from it")
    n = conn.execute("SELECT COUNT(*) AS n FROM share_lots WHERE assigning_position_id = ?",
                     (position_id,)).fetchone()["n"]
    if n:
        reasons.append(f"{n} share lot(s) came from its assignment")
    n = conn.execute("SELECT COUNT(*) AS n FROM share_disposals WHERE disposing_position_id = ?",
                     (position_id,)).fetchone()["n"]
    if n:
        reasons.append(f"{n} share sale(s) came from it")
    return reasons


def convert_to_share_trade(conn, position_id: str, note: str = "") -> str:
    """A row that is really a stock trade stops being an option.

    The importer already derived the shares it moved: a lot from an
    "assigned put", or a sale from a "called-away call". Those records stay,
    relabelled as an outright buy or sale with no option behind them. If
    nothing was derived, the shares are created from the row's own terms.
    Then the row itself goes. Every step is audited on its own record."""
    p = load_position(conn, position_id)
    if p is None:
        raise ValueError("no such position")
    if position_links(conn, position_id):
        # Only share records may hang off it; option links mean it is a real leg.
        others = [r_ for r_ in position_links(conn, position_id)
                  if "rolled" in r_ or "split" in r_]
        if others:
            raise InUseError("; ".join(others) + " - a real option leg is not a share trade")
    when = _iso(p.closed_on or p.opened_on)
    moved = []
    with audit_group(), conn:
        for row in conn.execute("SELECT * FROM share_lots WHERE assigning_position_id = ?",
                                (position_id,)).fetchall():
            before = dict(row)
            conn.execute("UPDATE share_lots SET source = ?, assigning_position_id = NULL WHERE id = ?",
                         (ShareSource.OUTRIGHT_BUY.value, row["id"]))
            _audit(conn, "share_lot", row["id"], "update", before,
                   _row_as_dict(conn, "share_lots", row["id"]), note)
            moved.append(f"lot of {row['quantity']}")
        for row in conn.execute("SELECT * FROM share_disposals WHERE disposing_position_id = ?",
                                (position_id,)).fetchall():
            before = dict(row)
            conn.execute("UPDATE share_disposals SET kind = ?, disposing_position_id = NULL"
                         " WHERE id = ?", (DisposalKind.SOLD.value, row["id"]))
            _audit(conn, "share_disposal", row["id"], "update", before,
                   _row_as_dict(conn, "share_disposals", row["id"]), note)
            moved.append(f"sale of {row['quantity']}")
        if not moved:
            acquiring = (p.direction is Direction.SHORT) == (p.right is Right.PUT)
            if acquiring:
                lot = ShareLot(id=uuid.uuid4().hex, underlying=p.underlying, quantity=p.shares,
                               acquired_on=p.closed_on or p.opened_on, cost_per_share=p.strike,
                               fee=p.open_fee + p.close_fee, notes=f"converted from row {p.id[:8]}")
                _insert_lot(conn, lot)
                _audit(conn, "share_lot", lot.id, "create", None,
                       _row_as_dict(conn, "share_lots", lot.id), note)
                moved.append(f"lot of {p.shares}")
            else:
                d = ShareDisposal(id=uuid.uuid4().hex, underlying=p.underlying, quantity=p.shares,
                                  disposed_on=p.closed_on or p.opened_on, proceeds_per_share=p.strike,
                                  fee=p.open_fee + p.close_fee, notes=f"converted from row {p.id[:8]}")
                _insert_disposal(conn, d)
                _audit(conn, "share_disposal", d.id, "create", None,
                       _row_as_dict(conn, "share_disposals", d.id), note)
                moved.append(f"sale of {p.shares}")
    with audit_group():
        delete_position(conn, position_id, note)
    rebuild_allocations(conn)
    return ", ".join(moved) + f" on {when}"


def delete_position(conn, position_id: str, note: str = "") -> None:
    """Remove a position outright. Refused while anything refers to it; the
    full row is kept in the audit log, so History can put it back."""
    p = load_position(conn, position_id)
    if p is None:
        raise ValueError("no such position")
    reasons = position_links(conn, position_id)
    if reasons:
        raise InUseError("; ".join(reasons) + " - remove or relink those first")
    before = dict(_position_dict(p), tags=list(p.tags))
    with conn:
        conn.execute("DELETE FROM position_tags WHERE position_id = ?", (position_id,))
        conn.execute("UPDATE flags SET resolved_at = ?, resolution = ? WHERE entity_id = ?"
                     " AND resolved_at IS NULL", (_now(), "record removed", position_id))
        conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        _audit(conn, "position", position_id, "delete", before, None, note)


EDITABLE = ("underlying", "expiry", "strike", "right", "direction", "quantity", "multiplier",
            "opened_on", "open_price", "open_fee", "closed_on", "close_price", "close_fee")


def edit_position(conn, position_id: str, changes: dict, note: str = "") -> None:
    """Correct entered values on a record. Status and chain links are not
    edited here: those change through the actions, which keep the chain and
    the shares consistent. Audited as one update."""
    import dataclasses
    p = load_position(conn, position_id)
    if p is None:
        raise ValueError("no such position")
    bad = set(changes) - set(EDITABLE)
    if bad:
        raise ValueError(f"cannot edit {', '.join(sorted(bad))} here")
    before = _position_dict(p)
    new = dataclasses.replace(p, **changes)      # Position validates quantity and multiplier
    if new.expiry < new.opened_on:
        raise ValueError("expiry cannot be before the position was opened")
    if new.closed_on is not None and new.closed_on < new.opened_on:
        raise ValueError("close cannot be before the position was opened")
    if new.open_price < 0 or (new.close_price is not None and new.close_price < 0):
        raise ValueError("prices cannot be negative")
    if new.open_fee < 0 or new.close_fee < 0:
        raise ValueError("fees cannot be negative")
    with conn:
        _update_position(conn, new)
        _sync_covers(conn, new)
        _audit(conn, "position", position_id, "update", before, _position_dict(new), note)


def reopen_position(conn, position_id: str, note: str = "") -> None:
    """Undo a close or expiry recorded by mistake. A roll, a split or an
    assignment created other records and is not reopened here."""
    p = load_position(conn, position_id)
    if p is None:
        raise ValueError("no such position")
    if p.status not in (Status.CLOSED, Status.EXPIRED):
        raise ValueError(f"a {p.status.value.lower()} position is not reopened here; "
                         "it has records that depend on it")
    before = _position_dict(p)
    p.status = Status.OPEN
    p.closed_on = None
    p.close_price = None
    with conn:
        _update_position(conn, p)
        _audit(conn, "position", position_id, "update", before, _position_dict(p), note)


def redate_position(conn, position_id: str, *, opened_on=None, expiry=None, closed_on=None,
                    note: str = "") -> None:
    """Move a position's dates. Only what is given changes; audited as one update."""
    p = load_position(conn, position_id)
    if p is None:
        raise ValueError("no such position")
    before = _position_dict(p)
    if opened_on is not None:
        p.opened_on = opened_on
    if expiry is not None:
        p.expiry = expiry
    if closed_on is not None:
        p.closed_on = closed_on
    if p.expiry < p.opened_on:
        raise ValueError("expiry cannot be before the position was opened")
    if p.closed_on is not None and p.closed_on < p.opened_on:
        raise ValueError("close cannot be before the position was opened")
    with conn:
        _update_position(conn, p)
        _audit(conn, "position", position_id, "update", before, _position_dict(p), note)


# --------------------------------------------------------------------------
# snapshots
#
# A snapshot is a consistent copy of the whole journal made through SQLite's
# backup API, next to the journal in a backups/ folder. Restoring copies a
# snapshot back over the live journal the same way -- after taking a snapshot
# of what is being replaced, so a restore is itself undoable.


def snapshots_dir(db_path: str):
    import pathlib
    return pathlib.Path(db_path).resolve().parent / "backups"


def snapshot(conn, db_path: str, label: str = "") -> str:
    """Write a snapshot; returns its file name."""
    folder = snapshots_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{_slug(label)}" if label else ""
    name = f"journal-{stamp}{suffix}.db"
    n = 2
    while (folder / name).exists():          # two in one second must not overwrite
        name = f"journal-{stamp}{suffix}-{n}.db"
        n += 1
    target = sqlite3.connect(str(folder / name))
    try:
        conn.backup(target)
    finally:
        target.close()
    return name


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:40]


def list_snapshots(db_path: str) -> list[dict]:
    folder = snapshots_dir(db_path)
    if not folder.exists():
        return []
    out = []
    for f in sorted(folder.glob("journal-*.db"), reverse=True):
        st = f.stat()
        out.append({"name": f.name, "bytes": st.st_size,
                    "at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


def restore(conn, db_path: str, name: str) -> str:
    """Replace the live journal with a snapshot. The journal as it was is
    snapshotted first, and that name is returned so the step can be undone."""
    if name not in {s["name"] for s in list_snapshots(db_path)}:
        raise ValueError("no such snapshot")
    source = sqlite3.connect(str(snapshots_dir(db_path) / name))
    try:
        if schema.current_version(source) > schema.SCHEMA_VERSION:
            raise ValueError("that snapshot was written by a newer version of the app")
        kept = snapshot(conn, db_path, "before-restore")
        source.backup(conn)
    finally:
        source.close()
    schema.migrate(conn)   # an older snapshot may predate a migration
    rebuild_allocations(conn)
    return kept


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


def audit_entries(conn, limit: int = 200, entity_id: str | None = None,
                  since: str = "", until: str = ""):
    """Newest first. ``since`` and ``until`` are ISO dates, inclusive; stamps
    are UTC, so a day boundary is a UTC one."""
    where, args = [], []
    if entity_id:
        where.append("entity_id = ?"); args.append(entity_id)
    if since:
        where.append("at >= ?"); args.append(since)
    if until:
        where.append("at < ?"); args.append(until + "T99")   # anything dated that day
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return list(conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)))


def revert_audit_entry(conn, entry_id: int) -> str:
    """Undo the action an audit entry belongs to -- every record it touched,
    in reverse order, in one transaction -- or nothing.

    A create is undone by deleting; an update by writing the previous values
    back; a delete by putting the row back. Refused when a later action
    depends on a record this one created (a half of a split that has since
    been rolled): undo that later action first. The reversal is itself
    logged, as one group.
    """
    entry = conn.execute("SELECT * FROM audit_log WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        raise ValueError(f"no audit entry {entry_id}")
    if entry["action"].startswith(("revert", "redo")):
        raise ValueError("undo and redo apply to the action itself, not to its bookkeeping")
    entries = action_entries(conn, entry)
    if action_state(conn, entries) == "undone":
        raise ValueError("that action is already undone; Redo puts it back")

    created_here = {e["entity_id"] for e in entries
                    if e["entity_type"] == "position" and e["action"].startswith("create")}
    for pid in created_here:
        outside = [r_ for r_ in position_links(conn, pid)]
        # Links from records this same action created do not count.
        dependants = conn.execute(
            "SELECT id FROM positions WHERE (rolled_from_id = ? OR split_from_id = ?)",
            (pid, pid)).fetchall()
        if any(d["id"] not in created_here for d in dependants) or any(
                "share" in r_ for r_ in outside):
            raise ValueError(f"a later action depends on {pid[:8]}: undo that one first")

    outcomes = []
    touched_shares = False
    with audit_group(), conn:
        for e in entries:
            outcomes.append(_revert_one(conn, e))
            touched_shares = touched_shares or e["entity_type"] != "position"
    if touched_shares:
        rebuild_allocations(conn)
    what = _note_of(entries[-1])
    return (f"{what}: " if what else "") + "; ".join(outcomes)


def action_state(conn, entries) -> str:
    """'in_effect' or 'undone': whichever of the action's undos and redos came
    last decides. An action never undone is in effect."""
    marks = [f"revert: undo of audit #{e['id']}" for e in entries] + \
            [f"redo: redo of audit #{e['id']}" for e in entries]
    last = conn.execute(
        "SELECT action FROM audit_log WHERE " + " OR ".join("action = ?" for _ in marks)
        + " ORDER BY id DESC LIMIT 1", tuple(marks)).fetchone()
    if last is None:
        return "in_effect"
    return "undone" if last["action"].startswith("revert") else "in_effect"


def action_state_at(conn, entries) -> str | None:
    """When the action's state last changed, for the History row."""
    marks = [f"revert: undo of audit #{e['id']}" for e in entries] + \
            [f"redo: redo of audit #{e['id']}" for e in entries]
    last = conn.execute(
        "SELECT at FROM audit_log WHERE " + " OR ".join("action = ?" for _ in marks)
        + " ORDER BY id DESC LIMIT 1", tuple(marks)).fetchone()
    return last["at"] if last else None


def _note_of(entry) -> str:
    """The action's own words, e.g. 'split 20 into 2 and 18'."""
    return entry["action"].split(": ", 1)[1] if ": " in entry["action"] else ""


def action_entries(conn, entry) -> list:
    """Every audit entry of the action ``entry`` belongs to, newest first.

    Grouped entries carry the id. Entries from before grouping existed are
    matched by what the action itself wrote: the same second and the same
    note, which is how one action's rows always looked."""
    if entry["group_id"]:
        return list(conn.execute(
            "SELECT * FROM audit_log WHERE group_id = ? ORDER BY id DESC", (entry["group_id"],)))
    note = _note_of(entry)
    if not note:
        return [entry]
    return list(conn.execute(
        "SELECT * FROM audit_log WHERE group_id IS NULL AND at = ? AND action LIKE ?"
        " ORDER BY id DESC", (entry["at"], f"%: {note}")))


def redo_audit_entry(conn, revert_id: int) -> str:
    """Do again what an undo took back: the whole action, in its original
    order, from the states its own entries recorded."""
    entry = conn.execute("SELECT * FROM audit_log WHERE id = ?", (revert_id,)).fetchone()
    if entry is None:
        raise ValueError(f"no audit entry {revert_id}")
    if entry["action"].startswith("revert"):      # an undo row still points at its action
        original_id = int(entry["action"].rsplit("#", 1)[1])
        entry = conn.execute("SELECT * FROM audit_log WHERE id = ?", (original_id,)).fetchone()
        if entry is None:
            raise ValueError("the undone action is no longer in the log")
    if entry["action"].startswith("redo"):
        raise ValueError("undo and redo apply to the action itself, not to its bookkeeping")
    entries = sorted(action_entries(conn, entry), key=lambda e: e["id"])
    if action_state(conn, entries) != "undone":
        raise ValueError("that action is in effect; there is nothing to redo")
    for e in entries:
        if e["action"].startswith("create") and e["entity_type"] != "position":
            after = json.loads(e["after"]) if e["after"] else {}
            if "id" not in after:
                raise ValueError("this action's share records were logged before they could be "
                                 "recreated; record them again by hand")
    outcomes = []
    touched_shares = False
    with audit_group(), conn:
        for e in entries:
            outcomes.append(_redo_one(conn, e))
            touched_shares = touched_shares or e["entity_type"] != "position"
    if touched_shares:
        rebuild_allocations(conn)
    what = _note_of(entries[0])
    return (f"{what}: " if what else "") + "; ".join(outcomes)


def _redo_one(conn, entry) -> str:
    after = json.loads(entry["after"]) if entry["after"] else None
    before = json.loads(entry["before"]) if entry["before"] else None
    kind = entry["entity_type"]
    table = {"position": "positions", "share_lot": "share_lots",
             "share_disposal": "share_disposals"}.get(kind)
    if table is None:
        raise ValueError("that change cannot be redone automatically")
    label = _describe(kind, after or before, entry["entity_id"][:8])
    current = _row_as_dict(conn, table, entry["entity_id"])
    if entry["action"].startswith("create"):
        if current is None:
            row = {k: v for k, v in after.items()
                   if kind != "position" or k in _POSITION_COLUMNS}
            _reinsert(conn, table, row)
        outcome = f"recreated {label}"
    elif entry["action"].startswith("delete"):
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (entry["entity_id"],))
        outcome = f"removed {label} again"
    else:
        cols = [c for c in after if c != "id" and (kind != "position" or c in _POSITION_COLUMNS)]
        conn.execute(f"UPDATE {table} SET {','.join(c + ' = ?' for c in cols)} WHERE id = ?",
                     tuple(after[c] for c in cols) + (entry["entity_id"],))
        outcome = f"restored {label}"
    if kind == "position":
        _reconcile_covers(conn, entry["entity_id"])
    _audit(conn, kind, entry["entity_id"], "redo", current, after, f"redo of audit #{entry['id']}")
    return outcome


def _describe(kind: str, row: dict | None, fallback: str) -> str:
    """A record in trade terms, for the undo message."""
    if not row:
        return fallback
    try:
        if kind == "position":
            right = str(row.get("option_right", ""))[:1]
            return (f"{row['quantity']} {row['underlying']} {row['expiry']} "
                    f"{Decimal(str(row['strike'])).normalize():f}{right}")
        return f"{row['underlying']} {'lot' if kind == 'share_lot' else 'sale'} of {row['quantity']}"
    except (KeyError, TypeError, ValueError):
        return fallback


def _revert_one(conn, entry) -> str:
    before = json.loads(entry["before"]) if entry["before"] else None
    after = json.loads(entry["after"]) if entry["after"] else None
    kind = entry["entity_type"]
    short = _describe(kind, before or after, entry["entity_id"][:8])
    if kind in ("share_lot", "share_disposal"):
        table = "share_lots" if kind == "share_lot" else "share_disposals"
        if entry["action"].startswith("create"):
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (entry["entity_id"],))
            outcome = f"removed {short}"
        elif entry["action"].startswith("delete") and before:
            _reinsert(conn, table, before)
            outcome = f"restored {short}"
        elif entry["action"].startswith("update") and before:
            cols = [c for c in before if c != "id"]
            conn.execute(
                f"UPDATE {table} SET {','.join(c + ' = ?' for c in cols)} WHERE id = ?",
                tuple(before[c] for c in cols) + (entry["entity_id"],),
            )
            outcome = f"restored {short}"
        else:
            raise ValueError("that share change cannot be reverted automatically")
    elif kind == "position":
        if before is None:
            conn.execute("DELETE FROM position_tags WHERE position_id = ?", (entry["entity_id"],))
            conn.execute("DELETE FROM positions WHERE id = ?", (entry["entity_id"],))
            outcome = f"deleted {short}"
        elif entry["action"].startswith("delete"):
            row = {k: v for k, v in before.items() if k in _POSITION_COLUMNS}
            _reinsert(conn, "positions", row)
            outcome = f"restored {short}"
        else:
            columns = [c for c in _POSITION_COLUMNS if c != "id"]
            assignments = ",".join(f"{c} = ?" for c in columns)
            conn.execute(
                f"UPDATE positions SET {assignments} WHERE id = ?",
                tuple(before.get(c) for c in columns) + (entry["entity_id"],),
            )
            outcome = f"restored {short}"
        _reconcile_covers(conn, entry["entity_id"])
    else:
        raise ValueError("that change cannot be reverted automatically")
    _audit(conn, kind, entry["entity_id"], "revert", None, before, f"undo of audit #{entry['id']}")
    return outcome


# --------------------------------------------------------------------------
# removing share records
#
# A mis-entered sale or purchase has to be removable: a sale pinned to the
# wrong lot blocks a ticker's matching until it goes. Removal is audited with
# the row's full contents, so it is itself reversible.


class InUseError(ValueError):
    """The record is referenced by something that must be dealt with first."""


def delete_disposal(conn, disposal_id: str, note: str = "") -> None:
    before = _row_as_dict(conn, "share_disposals", disposal_id)
    if before is None:
        raise ValueError("no such disposal")
    with conn:
        conn.execute("DELETE FROM share_disposals WHERE id = ?", (disposal_id,))
        _audit(conn, "share_disposal", disposal_id, "delete", before, None, note)
    rebuild_allocations(conn)


def delete_lot(conn, lot_id: str, note: str = "", unlink_open: bool = False) -> None:
    before = _row_as_dict(conn, "share_lots", lot_id)
    if before is None:
        raise ValueError("no such lot")
    pinned = conn.execute(
        "SELECT COUNT(*) AS n FROM share_disposals WHERE specific_lot_ids LIKE ?",
        (f"%{lot_id}%",),
    ).fetchone()["n"]
    if pinned:
        raise InUseError(f"{pinned} sale(s) are pinned to this lot; remove or repin them first")
    with audit_group(), conn:
        conn.execute("DELETE FROM share_lots WHERE id = ?", (lot_id,))
        _audit(conn, "share_lot", lot_id, "delete", before, None, note)
    rebuild_allocations(conn)


LOT_EDITABLE = ("acquired_on", "quantity", "cost_per_share", "fee", "estimated", "notes")
DISPOSAL_EDITABLE = ("disposed_on", "quantity", "proceeds_per_share", "fee", "notes")


def _edit_share_row(conn, table: str, key: str, changes: dict, allowed, note: str) -> None:
    before = _row_as_dict(conn, table, key)
    if before is None:
        raise ValueError(f"no such {table[:-1].replace('_', ' ')}")
    bad = set(changes) - set(allowed)
    if bad:
        raise ValueError(f"cannot edit {', '.join(sorted(bad))} here")
    if "quantity" in changes and int(changes["quantity"]) <= 0:
        raise ValueError("quantity must be positive")
    for money_key in ("cost_per_share", "proceeds_per_share", "fee"):
        if money_key in changes and Decimal(str(changes[money_key])) < 0:
            raise ValueError(f"{money_key.replace('_', ' ')} cannot be negative")
    values = {}
    for k, v in changes.items():
        if isinstance(v, date):
            v = _iso(v)
        elif isinstance(v, Decimal):
            v = _s(v)
        elif isinstance(v, bool):
            v = 1 if v else 0
        values[k] = v
    with conn:
        conn.execute(f"UPDATE {table} SET {', '.join(k + ' = ?' for k in values)} WHERE id = ?",
                     tuple(values.values()) + (key,))
        _audit(conn, "share_lot" if table == "share_lots" else "share_disposal", key, "update",
               before, _row_as_dict(conn, table, key), note)
    rebuild_allocations(conn)


def edit_lot(conn, lot_id: str, changes: dict, note: str = "") -> None:
    """Correct a lot as entered. Confirming one against a statement is the
    change that turns an estimate into a record: ``estimated`` False."""
    _edit_share_row(conn, "share_lots", lot_id, changes, LOT_EDITABLE, note)


def edit_disposal(conn, disposal_id: str, changes: dict, note: str = "") -> None:
    _edit_share_row(conn, "share_disposals", disposal_id, changes, DISPOSAL_EDITABLE, note)


def redate_lot(conn, lot_id: str, on: date, note: str = "") -> None:
    """Move a lot's acquisition date, and the assignment that produced it.

    Assignments used to default to the expiry, which is still in the future
    when a put is assigned early -- and a sale dated today then finds a lot
    that does not exist yet. The lot and its assigning position move together
    so the two never disagree about when the shares arrived.
    """
    before = _row_as_dict(conn, "share_lots", lot_id)
    if before is None:
        raise ValueError("no such lot")
    with audit_group(), conn:
        conn.execute("UPDATE share_lots SET acquired_on = ? WHERE id = ?", (_iso(on), lot_id))
        _audit(conn, "share_lot", lot_id, "update", before,
               _row_as_dict(conn, "share_lots", lot_id), note)
        pid = before.get("assigning_position_id")
        p = load_position(conn, pid) if pid else None
        if p is not None and p.closed_on is not None:
            pbefore = _position_dict(p)
            p.closed_on = on
            _update_position(conn, p)
            _audit(conn, "position", p.id, "update", pbefore, _position_dict(p), note)
    rebuild_allocations(conn)


def _reinsert(conn, table: str, row: dict) -> None:
    columns = ",".join(row.keys())
    marks = ",".join("?" * len(row))
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))
