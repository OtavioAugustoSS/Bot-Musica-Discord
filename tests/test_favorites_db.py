"""Favoritos por servidor: escopo por guild e migração v0→v1 do schema."""
import asyncio
import os
import sqlite3

from core.queue import Track
from db.database import Database

_OLD_SCHEMA = """
CREATE TABLE favorites (
    user_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    thumbnail TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, video_id)
);
"""


def _make_old_db(path: str, rows: list[tuple]) -> None:
    """DB no shape ANTIGO (sem guild_id), como o de produção pré-migração."""
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO favorites (user_id, video_id, title, added_at) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_favorites_are_scoped_per_guild(tmp_path):
    async def run():
        db = Database(str(tmp_path / "t.db"))
        await db.init()
        track = Track(title="Hit", artist="A", video_id="v1")
        assert await db.add_favorite(user_id=1, guild_id=10, track=track)

        mine = await db.get_favorites(user_id=1, guild_id=10)
        assert [r["video_id"] for r in mine] == ["v1"]
        assert await db.get_favorites(user_id=1, guild_id=99) == []  # outro servidor
        assert await db.get_favorites(user_id=2, guild_id=10) == []  # outro usuário

        # remover no guild errado não afeta o certo
        assert not await db.remove_favorite(user_id=1, guild_id=99, video_id="v1")
        assert await db.remove_favorite(user_id=1, guild_id=10, video_id="v1")
        assert await db.get_favorites(user_id=1, guild_id=10) == []
        await db.close()

    asyncio.run(run())


def test_same_track_in_two_guilds_but_dedup_within_one(tmp_path):
    async def run():
        db = Database(str(tmp_path / "t.db"))
        await db.init()
        track = Track(title="Hit", video_id="v1")
        assert await db.add_favorite(1, 10, track)
        assert await db.add_favorite(1, 99, track)  # mesmo (user, video), outro guild
        assert not await db.add_favorite(1, 10, track)  # repetida no mesmo guild
        await db.close()

    asyncio.run(run())


def test_migration_assigns_rows_to_legacy_guild(tmp_path):
    path = str(tmp_path / "old.db")
    _make_old_db(path, [(1, "v1", "Uma", 100), (1, "v2", "Duas", 200), (2, "v1", "Uma", 300)])

    async def run():
        db = Database(path)
        await db.init(legacy_guild_id=10)
        rows = await db.get_favorites(user_id=1, guild_id=10)
        assert [r["video_id"] for r in rows] == ["v2", "v1"]  # added_at DESC
        assert await db.get_favorites(user_id=2, guild_id=10) != []
        assert await db.get_favorites(user_id=1, guild_id=99) == []
        cur = await db._conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == 1
        await db.close()

    asyncio.run(run())
    assert os.path.exists(path + ".pre-v1")  # backup automático antes de mexer


def test_migration_without_legacy_preserves_rows_in_guild_zero(tmp_path):
    path = str(tmp_path / "old.db")
    _make_old_db(path, [(1, "v1", "Uma", 100)])

    async def run():
        db = Database(path)
        await db.init(legacy_guild_id=None)
        # dados preservados, recuperáveis via UPDATE ... WHERE guild_id = 0
        assert [r["video_id"] for r in await db.get_favorites(1, 0)] == ["v1"]
        await db.close()

    asyncio.run(run())


def test_wal_mode_and_backup(tmp_path):
    async def run():
        db = Database(str(tmp_path / "t.db"))
        await db.init()
        await db.add_favorite(1, 10, Track(title="Hit", video_id="v1"))
        cur = await db._conn.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0] == "wal"

        backup_path = str(tmp_path / "backup.db")
        await db.backup_to(backup_path)
        await db.close()

        restored = Database(backup_path)
        await restored.init()
        assert [r["video_id"] for r in await restored.get_favorites(1, 10)] == ["v1"]
        await restored.close()

    asyncio.run(run())


def test_init_is_idempotent_after_migration(tmp_path):
    path = str(tmp_path / "old.db")
    _make_old_db(path, [(1, "v1", "Uma", 100)])

    async def run():
        db = Database(path)
        await db.init(legacy_guild_id=10)
        await db.close()
        db2 = Database(path)
        await db2.init(legacy_guild_id=10)  # segunda abertura: no-op de migração
        assert [r["video_id"] for r in await db2.get_favorites(1, 10)] == ["v1"]
        await db2.close()

    asyncio.run(run())
