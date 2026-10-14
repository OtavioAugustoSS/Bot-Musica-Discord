"""Histórico é POR SERVIDOR, não por usuário global.

O commit 7a4d6fd escopou os favoritos por guild e esqueceu o histórico:
`add_history` gravava `guild_id`, `/top` filtrava por ele, mas `get_history`
e `get_last_played` liam tudo do usuário em qualquer servidor. Na prática,
`/history` num servidor novo mostrava o que a pessoa tocou no servidor antigo
e `/replay` tocava a última faixa de lá.
"""
import asyncio

from core.queue import Track
from db.database import Database


def _db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def test_history_nao_vaza_entre_servidores(tmp_path):
    async def run():
        db = _db(tmp_path)
        await db.init()
        await db.add_history(1, 10, Track(title="No servidor A", video_id="a1"))
        await db.add_history(1, 20, Track(title="No servidor B", video_id="b1"))

        do_a = await db.get_history(1, guild_id=10)
        do_b = await db.get_history(1, guild_id=20)

        assert [r["title"] for r in do_a] == ["No servidor A"]
        assert [r["title"] for r in do_b] == ["No servidor B"]
        await db.close()

    asyncio.run(run())


def test_replay_pega_a_ultima_DAQUELE_servidor(tmp_path):
    async def run():
        db = _db(tmp_path)
        await db.init()
        await db.add_history(1, 10, Track(title="Antiga do A", video_id="a1"))
        await db.add_history(1, 20, Track(title="Do B", video_id="b1"))
        await db.add_history(1, 10, Track(title="Recente do A", video_id="a2"))

        assert (await db.get_last_played(1, guild_id=20))["title"] == "Do B"
        assert (await db.get_last_played(1, guild_id=10))["title"] == "Recente do A"
        await db.close()

    asyncio.run(run())


def test_servidor_sem_historico_vem_vazio(tmp_path):
    async def run():
        db = _db(tmp_path)
        await db.init()
        await db.add_history(1, 10, Track(title="Só no A", video_id="a1"))

        assert await db.get_history(1, guild_id=999) == []
        assert await db.get_last_played(1, guild_id=999) is None
        await db.close()

    asyncio.run(run())


def test_historico_separa_usuarios_dentro_do_mesmo_servidor(tmp_path):
    async def run():
        db = _db(tmp_path)
        await db.init()
        await db.add_history(1, 10, Track(title="Do usuario 1", video_id="a1"))
        await db.add_history(2, 10, Track(title="Do usuario 2", video_id="a2"))

        assert [r["title"] for r in await db.get_history(1, guild_id=10)] == ["Do usuario 1"]
        await db.close()

    asyncio.run(run())


def test_indice_cobre_a_consulta_de_historico(tmp_path):
    """Sem índice por (user_id, guild_id) o /history vira full scan conforme cresce."""
    async def run():
        db = _db(tmp_path)
        await db.init()
        cur = await db._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM history"
            " WHERE user_id = 1 AND guild_id = 10 ORDER BY played_at DESC LIMIT 10"
        )
        plano = " ".join(str(r["detail"]) for r in await cur.fetchall())
        assert "SCAN" not in plano.upper(), plano
        await db.close()

    asyncio.run(run())
