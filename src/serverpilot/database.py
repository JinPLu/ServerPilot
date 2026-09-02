"""SQLite WAL setup, Alembic migration, readiness and recoverable backup helpers."""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from serverpilot import retention


class Database:
    def __init__(self, url: str, project_root: Path) -> None:
        self.url = url
        self.project_root = project_root
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite":
            raise ValueError("pilot only supports SQLite; migrate to PostgreSQL before multi-writer deployment")
        database = parsed.database
        self.path: Path | None = None
        if database and database != ":memory:":
            self.path = Path(database).expanduser().resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        event.listen(self.engine, "connect", self._configure_sqlite)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    def migrate(self) -> None:
        """Upgrade to the head the running package ships, and only that one.

        The script directory used to be preferred from a source tree found by
        walking up from the working directory, so a daemon started inside any
        checkout stamped that checkout's revisions into the shared database.
        The next start, from a different directory, then could not resolve the
        revision it found and died in a loop. Migrations belong to the code that
        is running, which is this package.
        """

        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parent / "migrations")
        )
        config.set_main_option("sqlalchemy.url", self.url)
        command.upgrade(config, "head")

    def reclaim_space(self) -> None:
        """Return freed pages and the write-ahead log to the filesystem.

        Deleting rows only marks their pages free, and in WAL mode the log keeps
        growing until something checkpoints it, so a database that prunes every
        hour can still take more disk every day. The checkpoint is cheap and
        runs every time; the rewrite is expensive and runs only when enough of
        the file is actually free to be worth it, which in a steady state is
        never.
        """

        # Both statements have to run outside a transaction, so the connection
        # is put in autocommit for the duration rather than wrapped in one.
        with self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            page_size = connection.exec_driver_sql("PRAGMA page_size").scalar() or 0
            page_count = connection.exec_driver_sql("PRAGMA page_count").scalar() or 0
            free_pages = connection.exec_driver_sql("PRAGMA freelist_count").scalar() or 0
            free_bytes = free_pages * page_size
            worth_rewriting = free_bytes >= retention.VACUUM_FREE_BYTES or (
                page_count > 0 and free_pages / page_count >= retention.VACUUM_FREE_FRACTION
            )
            if worth_rewriting:
                connection.exec_driver_sql("VACUUM")

    def session(self) -> Session:
        return self.Session()

    def ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:  # readiness must never leak a DB URL/credential
            return False

    def backup(self, destination: Path) -> Path:
        """Create and atomically publish a consistent online SQLite backup."""

        parsed = make_url(self.url)
        if not parsed.database or parsed.database == ":memory:":
            raise ValueError("cannot back up an in-memory database")
        source = Path(parsed.database).expanduser().resolve()
        destination = destination.expanduser().resolve()
        if destination == source:
            raise ValueError("backup destination must differ from the live database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            # A sqlite3 connection used as a context manager ends the transaction
            # but stays open. Windows refuses to replace or unlink a file that
            # still has a handle, so every connection is closed explicitly.
            with (
                contextlib.closing(
                    sqlite3.connect(f"file:{source}?mode=ro", uri=True)
                ) as source_db,
                contextlib.closing(sqlite3.connect(temporary)) as destination_db,
                source_db,
                destination_db,
            ):
                source_db.backup(destination_db)
                destination_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            with contextlib.closing(
                sqlite3.connect(f"file:{temporary}?mode=ro", uri=True)
            ) as copied_db:
                integrity = copied_db.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ValueError("backup integrity check failed")
            # Windows FlushFileBuffers (os.fsync → CRT _commit) requires
            # GENERIC_WRITE; a read-only handle raises EBADF there.
            descriptor = os.open(temporary, os.O_RDWR)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, destination)
            # Only POSIX can open a directory to fsync the rename itself; on
            # Windows os.replace is the durability boundary available.
            if os.name == "posix":
                directory_descriptor = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def restore_to(source: Path, destination: Path) -> Path:
        """Validate a SQLite backup and restore only to a new explicit target path.

        The method intentionally refuses overwrite; changing a live control-plane
        database is a deployment action, not a routine CLI side effect.
        """

        source = source.expanduser().resolve()
        destination = destination.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"backup does not exist: {source}")
        if destination.exists():
            raise ValueError(f"refusing to overwrite restore target: {destination}")
        with contextlib.closing(
            sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        ) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ValueError("backup integrity check failed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination
