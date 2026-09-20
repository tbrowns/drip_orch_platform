#!/usr/bin/env python
"""
scripts/sync_schema.py -- bring the live Postgres schema in line with db/models.py.

Background
----------
The live `public.users` table had drifted away from the `User` model: it carried
a quoted camelCase "FullName" and a plaintext `password` column, and was missing
`full_name`, `password_hash`, `created_at` and `updated_at`. Every other model
table already matched. Two tables (`dividend_events`, `user_portfolio`) exist in
the database with no model behind them at all.

What this script does
---------------------
  1. Loads DATABASE_URL from .env (explicit path -- see note below).
  2. SAFETY GATE: refuses to touch `users` if it has rows, unless --force.
  3. Re-confirms the drift against information_schema and prints the diff.
  4. DROP TABLE public.users CASCADE, then Base.metadata.create_all().
  5. Re-adds the foreign keys the CASCADE silently took with it.
  6. Drops the two model-less orphan tables, but only if they are empty.
  7. Verifies everything and exits non-zero if any check fails.

It is idempotent: a second run finds nothing to do and reports "already in sync".

The connection string is a secret. It is never printed, logged or written to
disk by this script -- only the host is shown, and only in redacted form.

NOTE ON load_dotenv(): it must be called with an explicit path here. With no
argument it walks the caller's stack frame to locate the .env, which asserts and
crashes when the script is fed to python via stdin. Always pass the path.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from db.models import Base

# Baseline row counts for the two tables that hold real scraped NSE data.
# Falling BELOW these means the migration destroyed data -> hard failure.
EXPECTED_ROWS = {"dividend_announcements": 76, "stock_quotes": 67}

# Tables present in the database with no model behind them. Dropped only when
# empty and only when genuinely absent from Base.metadata.
ORPHAN_TABLES = ["dividend_events", "user_portfolio"]

SCHEMA = "public"


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #

_failures: list[str] = []


def step(msg: str) -> None:
    print("\n=== {} ===".format(msg))


def info(msg: str) -> None:
    print("    {}".format(msg))


def ok(msg: str) -> None:
    print("    [OK]   {}".format(msg))


def warn(msg: str) -> None:
    print("    [WARN] {}".format(msg))


def fail(msg: str) -> None:
    _failures.append(msg)
    print("    [FAIL] {}".format(msg))


def redact(url: str) -> str:
    """Show enough of the URL to prove which database we hit, and nothing more."""
    try:
        after_scheme = url.split("://", 1)[1]
        hostpart = after_scheme.split("@", 1)[1] if "@" in after_scheme else after_scheme
        host = hostpart.split("/", 1)[0].split("?", 1)[0]
        db = hostpart.split("/", 1)[1].split("?", 1)[0] if "/" in hostpart else "?"
        shown = host if len(host) < 24 else host[:20] + "..."
        return "postgresql://<redacted>@{}/{}".format(shown, db)
    except Exception:
        return "postgresql://<redacted>"


# --------------------------------------------------------------------------- #
# introspection
# --------------------------------------------------------------------------- #

def db_tables(conn) -> set:
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
        ),
        {"s": SCHEMA},
    )
    return {r[0] for r in rows}


def db_columns(conn, table: str) -> dict:
    rows = conn.execute(
        text(
            "SELECT column_name, data_type, character_maximum_length, "
            "       numeric_precision, numeric_scale, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t "
            "ORDER BY ordinal_position"
        ),
        {"s": SCHEMA, "t": table},
    )
    out = {}
    for name, dtype, charlen, nprec, nscale, nullable, default in rows:
        rendered = dtype
        if charlen:
            rendered = "{}({})".format(dtype, charlen)
        elif dtype == "numeric" and nprec:
            rendered = "numeric({},{})".format(nprec, nscale)
        out[name] = {
            "type": rendered,
            "nullable": nullable == "YES",
            "default": default,
        }
    return out


def model_columns(table_name: str) -> list:
    return [c.name for c in Base.metadata.tables[table_name].columns]


def row_count(conn, table: str) -> int:
    return conn.execute(
        text('SELECT count(*) FROM {}."{}"'.format(SCHEMA, table))
    ).scalar_one()


def db_foreign_keys(conn) -> dict:
    """Map (child_table, child_column) -> fk details, for single-column FKs."""
    rows = conn.execute(
        text(
            """
            SELECT con.conname,
                   cl.relname   AS child_table,
                   att.attname  AS child_col,
                   rcl.relname  AS parent_table,
                   ratt.attname AS parent_col,
                   con.confdeltype
            FROM pg_constraint con
            JOIN pg_class cl      ON cl.oid = con.conrelid
            JOIN pg_namespace ns  ON ns.oid = cl.relnamespace
            JOIN unnest(con.conkey)  WITH ORDINALITY AS ck(attnum, ord) ON true
            JOIN unnest(con.confkey) WITH ORDINALITY AS fk(attnum, ord) ON fk.ord = ck.ord
            JOIN pg_attribute att  ON att.attrelid  = con.conrelid AND att.attnum  = ck.attnum
            JOIN pg_class rcl      ON rcl.oid = con.confrelid
            JOIN pg_attribute ratt ON ratt.attrelid = con.confrelid AND ratt.attnum = fk.attnum
            WHERE con.contype = 'f' AND ns.nspname = :s
            """
        ),
        {"s": SCHEMA},
    )
    deltypes = {"c": "CASCADE", "a": "NO ACTION", "r": "RESTRICT",
                "n": "SET NULL", "d": "SET DEFAULT"}
    out = {}
    for name, child_t, child_c, parent_t, parent_c, deltype in rows:
        out[(child_t, child_c)] = {
            "name": name,
            "parent_table": parent_t,
            "parent_col": parent_c,
            "on_delete": deltypes.get(deltype, deltype),
        }
    return out


def model_foreign_keys() -> dict:
    """The FKs db/models.py expects, keyed the same way as db_foreign_keys()."""
    out = {}
    for table in Base.metadata.sorted_tables:
        for fkc in table.foreign_key_constraints:
            cols = [c.name for c in fkc.columns]
            elements = list(fkc.elements)
            if len(cols) != 1:
                continue
            child_c = cols[0]
            parent_t = elements[0].column.table.name
            parent_c = elements[0].column.name
            # SQLAlchemy does not name these, so Postgres assigns its default.
            name = fkc.name or "{}_{}_fkey".format(table.name, child_c)
            out[(table.name, child_c)] = {
                "name": name,
                "parent_table": parent_t,
                "parent_col": parent_c,
                "on_delete": (fkc.ondelete or "NO ACTION").upper(),
            }
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sync the live Postgres schema with db/models.py."
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="rebuild public.users even if it contains rows (DESTRUCTIVE)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="connect and report the diff, but change nothing",
    )
    args = ap.parse_args()

    # ---- 1. config -------------------------------------------------------- #
    step("1. Loading configuration")
    if not ENV_PATH.exists():
        print("    [FAIL] no .env at {}".format(ENV_PATH))
        return 2
    load_dotenv(str(ENV_PATH))  # explicit path: see module docstring
    url = os.getenv("DATABASE_URL")
    if not url:
        print("    [FAIL] DATABASE_URL is not set in .env -- refusing to run.")
        return 2
    info("env file  : {}".format(ENV_PATH))
    info("target    : {}".format(redact(url)))
    info("mode      : {}".format("DRY RUN (no changes)" if args.dry_run else "LIVE"))

    engine = create_engine(url, pool_pre_ping=True, future=True)
    model_tables = sorted(Base.metadata.tables)
    info("models    : {} tables in Base.metadata".format(len(model_tables)))

    with engine.connect() as conn:
        present = db_tables(conn)
        info("database  : {} tables in schema '{}'".format(len(present), SCHEMA))

        # ---- 2. safety gate ----------------------------------------------- #
        step("2. Safety gate: public.users must be empty")
        if "users" not in present:
            users_rows = 0
            info("users does not exist yet -- nothing to protect, create_all will make it.")
        else:
            users_rows = row_count(conn, "users")
            info("public.users currently holds {} row(s)".format(users_rows))
        if users_rows > 0 and not args.force:
            print()
            print("    [ABORT] public.users is NOT empty.")
            print("    [ABORT] It holds {} row(s), and this script rebuilds the".format(users_rows))
            print("    [ABORT] table from scratch -- those accounts would be destroyed.")
            print("    [ABORT] Back the table up first, then re-run with --force if you")
            print("    [ABORT] are certain you want to discard them.")
            return 3
        if users_rows > 0 and args.force:
            warn("--force given: proceeding to DESTROY {} user row(s)".format(users_rows))
        else:
            ok("users is empty -- safe to rebuild")

        # ---- 3. re-confirm the drift -------------------------------------- #
        step("3. Re-confirming drift against db/models.py (before any change)")
        users_in_sync = False
        if "users" not in present:
            info("users: MISSING from the database entirely")
        else:
            live = db_columns(conn, "users")
            wanted = model_columns("users")
            missing = [c for c in wanted if c not in live]
            unexpected = [c for c in live if c not in wanted]
            info("users in database : {}".format(list(live)))
            info("users in model    : {}".format(wanted))
            if missing:
                info("MISSING (model has, database lacks)   : {}".format(missing))
            if unexpected:
                info("UNEXPECTED (database has, model lacks): {}".format(unexpected))
            users_in_sync = not missing and not unexpected
            if users_in_sync:
                ok("users already matches the model")
            else:
                info("=> users has DRIFTED and will be rebuilt")

        orphans_present = [
            t for t in ORPHAN_TABLES if t in present and t not in Base.metadata.tables
        ]
        missing_tables = [t for t in model_tables if t not in present]
        if missing_tables:
            info("model tables absent from database: {}".format(missing_tables))
        info("orphan tables still present: {}".format(orphans_present or "none"))

        nothing_to_do = users_in_sync and not orphans_present and not missing_tables
        if nothing_to_do:
            ok("already in sync -- no changes needed")
        if args.dry_run:
            step("DRY RUN: stopping before any change")
            return 0

    # ---- 4/5. rebuild users, restore FKs ---------------------------------- #
    if not nothing_to_do:
        step("4. Rebuilding public.users")
        with engine.begin() as conn:
            if "users" in present:
                info("DROP TABLE public.users CASCADE")
                info("  (CASCADE also drops the FK constraints on user_kyc,")
                info("   payment_methods, cds_accounts and user_portfolios)")
                conn.execute(text("DROP TABLE {}.users CASCADE".format(SCHEMA)))
                ok("users dropped")
            else:
                info("users absent -- nothing to drop")

        info("Base.metadata.create_all(engine)  (creates only what is missing)")
        Base.metadata.create_all(engine)
        ok("create_all finished")

        # create_all only creates missing TABLES. The child tables survived the
        # CASCADE, so create_all will NOT put their dropped FK constraints back.
        # They have to be re-added by hand or the schema stays subtly broken.
        step("5. Restoring foreign keys removed by the CASCADE")
        with engine.begin() as conn:
            existing = db_foreign_keys(conn)
            wanted_fks = model_foreign_keys()
            restored = 0
            for key in sorted(wanted_fks):
                spec = wanted_fks[key]
                child_t, child_c = key
                if key in existing:
                    ok("{}.{} -> {}.{}  [{}] already present".format(
                        child_t, child_c, spec["parent_table"], spec["parent_col"],
                        existing[key]["name"]))
                    continue
                ddl = (
                    'ALTER TABLE {schema}."{child_t}" '
                    'ADD CONSTRAINT "{name}" '
                    'FOREIGN KEY ("{child_c}") '
                    'REFERENCES {schema}."{parent_t}" ("{parent_c}")'
                ).format(
                    schema=SCHEMA, child_t=child_t, name=spec["name"],
                    child_c=child_c, parent_t=spec["parent_table"],
                    parent_c=spec["parent_col"],
                )
                if spec["on_delete"] != "NO ACTION":
                    ddl += " ON DELETE {}".format(spec["on_delete"])
                info("re-adding {}: {}.{} -> {}.{} ON DELETE {}".format(
                    spec["name"], child_t, child_c, spec["parent_table"],
                    spec["parent_col"], spec["on_delete"]))
                conn.execute(text(ddl))
                restored += 1
            if restored:
                ok("{} foreign key(s) restored".format(restored))
            else:
                info("no foreign keys needed restoring")

        # ---- 6. orphan tables --------------------------------------------- #
        step("6. Handling model-less orphan tables")
        with engine.begin() as conn:
            present_now = db_tables(conn)
            for t in ORPHAN_TABLES:
                if t not in present_now:
                    ok("{}: already absent".format(t))
                    continue
                if t in Base.metadata.tables:
                    warn("{}: IS in Base.metadata after all -- keeping it".format(t))
                    continue
                n = row_count(conn, t)
                if n != 0:
                    warn("{}: holds {} row(s) -- NOT empty, leaving it alone".format(t, n))
                    warn("{}: review it by hand; this script will not drop data".format(t))
                    continue
                info("{}: 0 rows, not in Base.metadata -> DROP TABLE".format(t))
                conn.execute(text('DROP TABLE {}."{}" CASCADE'.format(SCHEMA, t)))
                ok("{}: dropped".format(t))
    else:
        step("4-6. Skipped: nothing to change")

    # ---- 7. verification -------------------------------------------------- #
    step("7. Verification")
    with engine.connect() as conn:
        present_now = db_tables(conn)

        info("-- every model table exists --")
        for t in model_tables:
            if t in present_now:
                ok("table {} exists".format(t))
            else:
                fail("table {} is MISSING".format(t))

        info("-- column sets match the model exactly --")
        for t in model_tables:
            if t not in present_now:
                continue
            live = set(db_columns(conn, t))
            wanted = set(model_columns(t))
            if live == wanted:
                ok("{}: {} columns match".format(t, len(wanted)))
            else:
                fail("{}: missing={} unexpected={}".format(
                    t, sorted(wanted - live), sorted(live - wanted)))

        info("-- foreign keys present --")
        existing = db_foreign_keys(conn)
        wanted_fks = model_foreign_keys()
        for key in sorted(wanted_fks):
            spec = wanted_fks[key]
            child_t, child_c = key
            got = existing.get(key)
            if not got:
                fail("FK {}.{} -> {}.{} MISSING".format(
                    child_t, child_c, spec["parent_table"], spec["parent_col"]))
            elif got["parent_table"] != spec["parent_table"]:
                fail("FK {}.{} points at {}, expected {}".format(
                    child_t, child_c, got["parent_table"], spec["parent_table"]))
            else:
                ok("FK {}: {}.{} -> {}.{} ON DELETE {}".format(
                    got["name"], child_t, child_c, got["parent_table"],
                    got["parent_col"], got["on_delete"]))

        info("-- real scraped data still intact --")
        for t in sorted(EXPECTED_ROWS):
            expected = EXPECTED_ROWS[t]
            if t not in present_now:
                fail("{}: table missing, expected {} rows".format(t, expected))
                continue
            n = row_count(conn, t)
            if n == expected:
                ok("{}: {} rows (expected {})".format(t, n, expected))
            elif n > expected:
                warn("{}: {} rows, more than the {} baseline "
                     "(new scrapes, not data loss)".format(t, n, expected))
            else:
                fail("{}: {} rows, FEWER than the {} baseline -- DATA LOST".format(
                    t, n, expected))

        info("-- orphan tables gone --")
        for t in ORPHAN_TABLES:
            if t in present_now:
                n = row_count(conn, t)
                if n:
                    warn("{}: still present with {} row(s) -- deliberately kept".format(t, n))
                else:
                    fail("{}: still present and empty -- should have been dropped".format(t))
            else:
                ok("{}: dropped".format(t))

        info("-- final public.users shape --")
        for name, meta in db_columns(conn, "users").items():
            null = "NULL" if meta["nullable"] else "NOT NULL"
            dflt = "  DEFAULT {}".format(meta["default"]) if meta["default"] else ""
            print("           {:<16} {:<28} {}{}".format(name, meta["type"], null, dflt))

    step("RESULT")
    if _failures:
        print("    FAILED -- {} check(s) did not pass:".format(len(_failures)))
        for f in _failures:
            print("      - {}".format(f))
        return 1
    if nothing_to_do:
        print("    already in sync -- nothing was changed, all checks passed.")
    else:
        print("    schema now matches db/models.py -- all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
