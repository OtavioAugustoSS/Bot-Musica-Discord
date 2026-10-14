"""Persistência SQLite: favoritos e histórico por usuário."""
from __future__ import annotations

import logging
import os
import shutil
import time

import aiosqlite

from core.queue import Track

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1  # v1: favorites por servidor (guild_id na PK)

_FAVORITES_SQL = """
CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    thumbnail TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, guild_id, video_id)
);
"""

_SCHEMA = _FAVORITES_SQL + """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    thumbnail TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    played_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_guild_time ON history (guild_id, played_at);
CREATE INDEX IF NOT EXISTS idx_history_user_time ON history (user_id, played_at);
-- ajustes por servidor (o /volume). Aditivo de propósito: IF NOT EXISTS no
-- schema cria a tabela em banco antigo sem subir _SCHEMA_VERSION nem migrar.
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    volume INTEGER NOT NULL DEFAULT 100
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def init(self, legacy_guild_id: int | None = None) -> None:
        """Abre o banco, migra se preciso e garante o schema.

        legacy_guild_id: dono das linhas de favoritos criadas ANTES do
        guild_id existir (migração v0→v1) — na prática, o único servidor
        em que o bot rodava até então.
        """
        if self._conn is not None:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # WAL: commit sem fsync do journal inteiro + leitura durante escrita;
        # busy_timeout cobre um segundo processo acidental no mesmo arquivo
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._migrate(legacy_guild_id)
        await self._conn.executescript(_SCHEMA)
        await self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        await self._conn.commit()

    async def _migrate(self, legacy_guild_id: int | None) -> None:
        """v0→v1: favorites ganha guild_id na PK; linhas antigas vão para o guild legado.

        Detecção pela COLUNA (não pelo user_version): um DB novo e um DB
        velho estão ambos em user_version 0 — só o shape da tabela distingue.
        """
        cur = await self._conn.execute("PRAGMA table_info(favorites)")
        cols = [row["name"] for row in await cur.fetchall()]
        if not cols or "guild_id" in cols:
            return  # DB novo (sem tabela) ou já migrado
        # a migração roda sozinha no boot: backup do arquivo antes de mexer
        await self._conn.commit()
        shutil.copy2(self.path, self.path + ".pre-v1")
        guild = legacy_guild_id if legacy_guild_id is not None else 0
        if legacy_guild_id is None:
            log.warning(
                "favoritos migrados sem GUILD_ID — atribuídos ao guild 0"
                " (recuperáveis com UPDATE favorites SET guild_id = <id>"
                " WHERE guild_id = 0)"
            )
        await self._conn.executescript(
            "BEGIN;\n"
            + _FAVORITES_SQL.replace("favorites", "favorites_new")
            + f"""
            INSERT INTO favorites_new
                SELECT user_id, {guild}, video_id, title, artist,
                       thumbnail, duration, added_at
                FROM favorites;
            DROP TABLE favorites;
            ALTER TABLE favorites_new RENAME TO favorites;
            COMMIT;
            """
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def backup_to(self, path: str) -> None:
        """Cópia consistente do banco (API de backup do SQLite — segura com WAL)."""
        async with aiosqlite.connect(path) as dest:
            await self._conn.backup(dest)

    # --- ajustes do servidor ---

    async def get_volume(self, guild_id: int) -> int:
        """Volume do servidor em % (100 = o original da música)."""
        cursor = await self._conn.execute(
            "SELECT volume FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        row = await cursor.fetchone()
        return int(row["volume"]) if row is not None else 100

    async def set_volume(self, guild_id: int, volume: int) -> None:
        await self._conn.execute(
            "INSERT INTO guild_settings (guild_id, volume) VALUES (?, ?)"
            " ON CONFLICT(guild_id) DO UPDATE SET volume = excluded.volume",
            (guild_id, volume),
        )
        await self._conn.commit()

    # --- favoritos ---

    async def add_favorite(self, user_id: int, guild_id: int, track: Track) -> bool:
        """True se adicionou; False se já era favorita (neste servidor)."""
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO favorites"
            " (user_id, guild_id, video_id, title, artist, thumbnail, duration, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, guild_id, track.video_id, track.title, track.artist,
             track.thumbnail, track.duration, int(time.time())),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def remove_favorite(self, user_id: int, guild_id: int, video_id: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM favorites WHERE user_id = ? AND guild_id = ? AND video_id = ?",
            (user_id, guild_id, video_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_favorites(
        self, user_id: int, guild_id: int, limit: int = 25
    ) -> list[aiosqlite.Row]:
        cur = await self._conn.execute(
            "SELECT * FROM favorites WHERE user_id = ? AND guild_id = ?"
            " ORDER BY added_at DESC LIMIT ?",
            (user_id, guild_id, limit),
        )
        return list(await cur.fetchall())

    # --- histórico ---

    async def add_history(self, user_id: int, guild_id: int, track: Track) -> None:
        await self._conn.execute(
            "INSERT INTO history"
            " (user_id, guild_id, video_id, title, artist, thumbnail, duration, played_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, guild_id, track.video_id, track.title, track.artist,
             track.thumbnail, track.duration, int(time.time())),
        )
        await self._conn.commit()

    async def get_history(
        self, user_id: int, guild_id: int, limit: int = 10
    ) -> list[aiosqlite.Row]:
        """Histórico do usuário NESTE servidor.

        guild_id é obrigatório de propósito: sem ele, /history num servidor
        novo mostrava o que a pessoa tocou em outro (o guild_id já era gravado,
        só não era lido — sobra da migração v0→v1, que escopou os favoritos e
        esqueceu o histórico).
        """
        cur = await self._conn.execute(
            "SELECT * FROM history WHERE user_id = ? AND guild_id = ?"
            " ORDER BY played_at DESC LIMIT ?",
            (user_id, guild_id, limit),
        )
        return list(await cur.fetchall())

    async def get_last_played(self, user_id: int, guild_id: int) -> aiosqlite.Row | None:
        rows = await self.get_history(user_id, guild_id, limit=1)
        return rows[0] if rows else None

    # --- ranking do servidor ---

    async def get_top_tracks(
        self, guild_id: int, since: int = 0, limit: int = 10
    ) -> list[aiosqlite.Row]:
        cur = await self._conn.execute(
            "SELECT title, artist, COUNT(*) AS plays FROM history"
            " WHERE guild_id = ? AND played_at >= ?"
            " GROUP BY video_id ORDER BY plays DESC, MAX(played_at) DESC LIMIT ?",
            (guild_id, since, limit),
        )
        return list(await cur.fetchall())

    async def get_top_requesters(
        self, guild_id: int, since: int = 0, limit: int = 5
    ) -> list[aiosqlite.Row]:
        cur = await self._conn.execute(
            "SELECT user_id, COUNT(*) AS plays FROM history"
            " WHERE guild_id = ? AND played_at >= ?"
            " GROUP BY user_id ORDER BY plays DESC LIMIT ?",
            (guild_id, since, limit),
        )
        return list(await cur.fetchall())
