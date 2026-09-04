"""Schema and migrations. stdlib sqlite3, no ORM.

Ten simple tables with straightforward joins do not need SQLAlchemy, and
skipping it keeps the whole engine plus importer dependency-free.

Money is stored as TEXT holding a Decimal's exact digits. Never REAL: a float
column silently corrupts accounting, and SQLite would happily accept one.
Dates are ISO-8601 TEXT.

Only entered values are stored. Cash flows, P/L, chain carry, targets,
break-even and adjusted basis are computed on read, so correcting a rule fixes
every historical row with nothing to migrate.
"""

import sqlite3

SCHEMA_VERSION = 4

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE positions (
            id                TEXT PRIMARY KEY,
            underlying        TEXT NOT NULL,
            expiry            TEXT NOT NULL,
            strike            TEXT NOT NULL,
            -- Not "right": RIGHT became a keyword in SQLite 3.39 (RIGHT JOIN).
            option_right      TEXT NOT NULL CHECK (option_right IN ('CALL','PUT')),
            direction         TEXT NOT NULL CHECK (direction IN ('SHORT','LONG')),
            quantity          INTEGER NOT NULL CHECK (quantity > 0),
            multiplier        INTEGER NOT NULL DEFAULT 100 CHECK (multiplier > 0),
            opened_on         TEXT NOT NULL,
            open_price        TEXT NOT NULL,
            open_fee          TEXT NOT NULL DEFAULT '0',
            closed_on         TEXT,
            close_price       TEXT,
            close_fee         TEXT NOT NULL DEFAULT '0',
            status            TEXT NOT NULL CHECK (
                                  status IN ('OPEN','CLOSED','EXPIRED','ROLLED','ASSIGNED')
                              ),
            rolled_from_id    TEXT REFERENCES positions(id),
            split_from_id     TEXT REFERENCES positions(id),
            share_lot_id      TEXT REFERENCES share_lots(id),
            spread_group_id   TEXT REFERENCES spread_groups(id),
            target_pct        TEXT,
            notes             TEXT NOT NULL DEFAULT '',
            import_batch_id   TEXT REFERENCES import_batches(id),
            -- A position descends from a roll or a split, never both.
            CHECK (rolled_from_id IS NULL OR split_from_id IS NULL),
            CHECK (id <> rolled_from_id AND id <> split_from_id)
        )
        """,
        "CREATE INDEX positions_underlying ON positions(underlying)",
        "CREATE INDEX positions_status ON positions(status)",
        "CREATE INDEX positions_opened_on ON positions(opened_on)",
        "CREATE INDEX positions_rolled_from ON positions(rolled_from_id)",
        """
        CREATE TABLE share_lots (
            id                    TEXT PRIMARY KEY,
            underlying            TEXT NOT NULL,
            quantity              INTEGER NOT NULL CHECK (quantity > 0),
            acquired_on           TEXT NOT NULL,
            cost_per_share        TEXT NOT NULL,
            fee                   TEXT NOT NULL DEFAULT '0',
            source                TEXT NOT NULL CHECK (
                                      source IN ('OUTRIGHT_BUY','BUY_WRITE','PUT_ASSIGNMENT')
                                  ),
            assigning_position_id TEXT REFERENCES positions(id),
            estimated             INTEGER NOT NULL DEFAULT 0,
            notes                 TEXT NOT NULL DEFAULT '',
            import_batch_id       TEXT REFERENCES import_batches(id)
        )
        """,
        "CREATE INDEX share_lots_underlying ON share_lots(underlying, acquired_on)",
        """
        CREATE TABLE share_disposals (
            id                    TEXT PRIMARY KEY,
            underlying            TEXT NOT NULL,
            quantity              INTEGER NOT NULL CHECK (quantity > 0),
            disposed_on           TEXT NOT NULL,
            proceeds_per_share    TEXT NOT NULL,
            fee                   TEXT NOT NULL DEFAULT '0',
            kind                  TEXT NOT NULL CHECK (kind IN ('SOLD','CALLED_AWAY')),
            disposing_position_id TEXT REFERENCES positions(id),
            specific_lot_ids      TEXT NOT NULL DEFAULT '',
            notes                 TEXT NOT NULL DEFAULT '',
            import_batch_id       TEXT REFERENCES import_batches(id)
        )
        """,
        "CREATE INDEX share_disposals_underlying ON share_disposals(underlying, disposed_on)",
        """
        -- Derived from lots and disposals; dropped and rebuilt, never edited.
        CREATE TABLE share_allocations (
            disposal_id TEXT NOT NULL REFERENCES share_disposals(id) ON DELETE CASCADE,
            lot_id      TEXT NOT NULL REFERENCES share_lots(id) ON DELETE CASCADE,
            quantity    INTEGER NOT NULL CHECK (quantity > 0),
            realized_pl TEXT NOT NULL,
            cost_basis  TEXT NOT NULL,
            proceeds    TEXT NOT NULL,
            PRIMARY KEY (disposal_id, lot_id)
        )
        """,
        """
        CREATE TABLE wheel_links (
            id           TEXT PRIMARY KEY,
            share_lot_id TEXT NOT NULL REFERENCES share_lots(id) ON DELETE CASCADE,
            position_id  TEXT NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
            role         TEXT NOT NULL,
            UNIQUE (share_lot_id, position_id)
        )
        """,
        "CREATE TABLE spread_groups (id TEXT PRIMARY KEY, label TEXT NOT NULL DEFAULT '')",
        "CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
        """
        CREATE TABLE position_tags (
            position_id TEXT NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
            tag_id      TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (position_id, tag_id)
        )
        """,
        """
        CREATE TABLE flags (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id   TEXT NOT NULL,
            kind        TEXT NOT NULL,
            detail      TEXT NOT NULL DEFAULT '',
            raised_at   TEXT NOT NULL,
            resolved_at TEXT,
            resolution  TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX flags_open ON flags(resolved_at, kind)",
        """
        CREATE TABLE audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id   TEXT NOT NULL,
            action      TEXT NOT NULL,
            before      TEXT,
            after       TEXT
        )
        """,
        "CREATE INDEX audit_log_entity ON audit_log(entity_type, entity_id)",
        """
        CREATE TABLE saved_views (
            id     TEXT PRIMARY KEY,
            name   TEXT NOT NULL UNIQUE,
            filter TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE import_batches (
            id          TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            file_hash   TEXT NOT NULL,
            row_count   INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL,
            report      TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE UNIQUE INDEX import_batches_hash ON import_batches(file_hash)",
    ),
    # 2 is registered below, once its function is defined.
}

MIGRATION_2_NOTE = (
    "SPLIT status, split_from_quantity, CALL_EXERCISE share source"
)

DEFAULT_SETTINGS = {
    "fee_rate": "0.65",
    "profit_target_pct": "0.50",
    "multiplier": "100",
    "share_matching_rule": "FIFO",
    "strike_increment": "0.50",
}


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas this app depends on."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting version.

    Each migration runs in its own transaction, so a failure leaves the
    database at the last version that fully applied. A migration may be a
    tuple of statements or a callable that manages its own transaction --
    needed for table rebuilds, which have to toggle a pragma.
    """
    version = current_version(conn)
    for target in sorted(MIGRATIONS):
        if target <= version:
            continue
        step = MIGRATIONS[target]
        if callable(step):
            step(conn)
            conn.execute(f"PRAGMA user_version = {target}")
        else:
            with conn:
                for statement in step:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")
        version = target

    _seed_settings(conn)
    return version


def _migrate_2_split_and_exercise(conn: sqlite3.Connection) -> None:
    """Add the SPLIT status, split_from_quantity, and CALL_EXERCISE.

    SQLite cannot alter a CHECK constraint, so both affected tables are
    rebuilt. Foreign keys are disabled for the rebuild -- ``DROP TABLE`` on a
    referenced table would otherwise fail -- and verified again afterwards,
    because a rebuild that silently orphaned a row would be worse than one
    that failed outright.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.executescript(
                """
                CREATE TABLE positions_v2 (
                    id                TEXT PRIMARY KEY,
                    underlying        TEXT NOT NULL,
                    expiry            TEXT NOT NULL,
                    strike            TEXT NOT NULL,
                    option_right      TEXT NOT NULL CHECK (option_right IN ('CALL','PUT')),
                    direction         TEXT NOT NULL CHECK (direction IN ('SHORT','LONG')),
                    quantity          INTEGER NOT NULL CHECK (quantity > 0),
                    multiplier        INTEGER NOT NULL DEFAULT 100 CHECK (multiplier > 0),
                    opened_on         TEXT NOT NULL,
                    open_price        TEXT NOT NULL,
                    open_fee          TEXT NOT NULL DEFAULT '0',
                    closed_on         TEXT,
                    close_price       TEXT,
                    close_fee         TEXT NOT NULL DEFAULT '0',
                    status            TEXT NOT NULL CHECK (
                                          status IN ('OPEN','CLOSED','EXPIRED',
                                                     'ROLLED','ASSIGNED','SPLIT')
                                      ),
                    rolled_from_id    TEXT REFERENCES positions(id),
                    split_from_id     TEXT REFERENCES positions(id),
                    split_from_quantity INTEGER CHECK (
                                          split_from_quantity IS NULL
                                          OR split_from_quantity > 0
                                      ),
                    share_lot_id      TEXT REFERENCES share_lots(id),
                    spread_group_id   TEXT REFERENCES spread_groups(id),
                    target_pct        TEXT,
                    notes             TEXT NOT NULL DEFAULT '',
                    import_batch_id   TEXT REFERENCES import_batches(id),
                    CHECK (id <> rolled_from_id AND id <> split_from_id),
                    -- A split child records both: the sibling group it came
                    -- from, and the roll history it inherited a share of.
                    CHECK ((split_from_id IS NULL) = (split_from_quantity IS NULL))
                );

                INSERT INTO positions_v2 (
                    id, underlying, expiry, strike, option_right, direction,
                    quantity, multiplier, opened_on, open_price, open_fee,
                    closed_on, close_price, close_fee, status,
                    rolled_from_id, split_from_id, split_from_quantity,
                    share_lot_id, spread_group_id, target_pct, notes,
                    import_batch_id
                )
                SELECT
                    id, underlying, expiry, strike, option_right, direction,
                    quantity, multiplier, opened_on, open_price, open_fee,
                    closed_on, close_price, close_fee, status,
                    rolled_from_id, split_from_id, NULL,
                    share_lot_id, spread_group_id, target_pct, notes,
                    import_batch_id
                FROM positions;

                DROP TABLE positions;
                ALTER TABLE positions_v2 RENAME TO positions;

                CREATE INDEX positions_underlying ON positions(underlying);
                CREATE INDEX positions_status ON positions(status);
                CREATE INDEX positions_opened_on ON positions(opened_on);
                CREATE INDEX positions_rolled_from ON positions(rolled_from_id);
                CREATE INDEX positions_expiry ON positions(expiry);

                CREATE TABLE share_lots_v2 (
                    id                    TEXT PRIMARY KEY,
                    underlying            TEXT NOT NULL,
                    quantity              INTEGER NOT NULL CHECK (quantity > 0),
                    acquired_on           TEXT NOT NULL,
                    cost_per_share        TEXT NOT NULL,
                    fee                   TEXT NOT NULL DEFAULT '0',
                    source                TEXT NOT NULL CHECK (
                                              source IN ('OUTRIGHT_BUY','BUY_WRITE',
                                                         'PUT_ASSIGNMENT','CALL_EXERCISE')
                                          ),
                    assigning_position_id TEXT REFERENCES positions(id),
                    estimated             INTEGER NOT NULL DEFAULT 0,
                    notes                 TEXT NOT NULL DEFAULT '',
                    import_batch_id       TEXT REFERENCES import_batches(id)
                );

                INSERT INTO share_lots_v2
                    SELECT id, underlying, quantity, acquired_on,
                           cost_per_share, fee, source, assigning_position_id,
                           estimated, notes, import_batch_id
                    FROM share_lots;

                DROP TABLE share_lots;
                ALTER TABLE share_lots_v2 RENAME TO share_lots;

                CREATE INDEX share_lots_underlying
                    ON share_lots(underlying, acquired_on);
                """
            )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    if orphans:
        raise RuntimeError(
            f"migration 2 left {len(orphans)} orphaned row(s); "
            "the database has not been advanced"
        )


def _seed_settings(conn: sqlite3.Connection) -> None:
    with conn:
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, value),
            )


# Registered here because a callable migration has to be defined first.
MIGRATIONS[2] = _migrate_2_split_and_exercise

# One action, one group: a split writes three audit rows, and undoing one of
# them alone left the other two standing. The group is what undo works on.
MIGRATIONS[3] = (
    "ALTER TABLE audit_log ADD COLUMN group_id TEXT",
    "CREATE INDEX audit_log_group ON audit_log(group_id)",
)

# A call can be covered by shares from more than one lot. The single
# share_lot_id column stays as the first of them; this table holds them all.
MIGRATIONS[4] = (
    """
    CREATE TABLE position_covers (
        position_id TEXT NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
        lot_id      TEXT NOT NULL REFERENCES share_lots(id) ON DELETE CASCADE,
        shares      INTEGER NOT NULL,
        PRIMARY KEY (position_id, lot_id)
    )
    """,
    "INSERT INTO position_covers (position_id, lot_id, shares)"
    " SELECT id, share_lot_id, quantity * multiplier FROM positions"
    " WHERE share_lot_id IS NOT NULL",
)
