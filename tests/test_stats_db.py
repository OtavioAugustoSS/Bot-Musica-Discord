"""Consultas de ranking (/top) no SQLite."""
import asyncio

from core.queue import Track
from db.database import Database


def test_top_tracks_and_requesters(tmp_path):
    async def run():
        db = Database(str(tmp_path / "t.db"))
        await db.init()
        hit = Track(title="Hit", artist="A", video_id="v1")
        other = Track(title="Outra", artist="B", video_id="v2")
        for _ in range(3):
            await db.add_history(user_id=1, guild_id=10, track=hit)
        await db.add_history(user_id=2, guild_id=10, track=other)
        await db.add_history(user_id=9, guild_id=99, track=hit)  # outro servidor

        top = await db.get_top_tracks(guild_id=10, since=0)
        assert [(r["title"], r["plays"]) for r in top] == [("Hit", 3), ("Outra", 1)]

        requesters = await db.get_top_requesters(guild_id=10, since=0)
        assert requesters[0]["user_id"] == 1
        assert requesters[0]["plays"] == 3

        # filtro de período: nada tão recente quanto o futuro
        assert await db.get_top_tracks(guild_id=10, since=9_999_999_999) == []
        await db.close()

    asyncio.run(run())
