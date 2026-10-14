"""Enquanto a pessoa escolhe no dropdown, o 1º resultado já vai sendo extraído.

`/play <nome>` → dropdown → escolha → só AÍ começava a extração (~8,3s na
VM) — os "alguns segundos pra começar a tocar" quando o bot acabou de
entrar. O tempo que a pessoa leva escolhendo (2–10s) é de graça: se o pool
estiver ocioso, o 1º resultado (o escolhido na maioria das vezes) é
aquecido em background. Escolheu ele: toca na hora. Escolheu outro: a
extração dele entra atrás da que já roda — por isso só aquecemos com o
pool OCIOSO, para nunca atrasar uma faixa que já está sendo resolvida.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs import music as music_cog
from services import youtube


@pytest.fixture(autouse=True)
def _limpo():
    youtube._stream_cache.clear()
    youtube._inflight.clear()
    yield
    youtube._stream_cache.clear()
    youtube._inflight.clear()


# ── youtube.prewarm ──────────────────────────────────────────────────────────


def test_prewarm_extrai_quando_o_pool_esta_ocioso(monkeypatch):
    monkeypatch.setattr(
        youtube, "_extract", lambda url, **k: {"id": "v1", "title": "T", "url": "http://s"}
    )

    async def run():
        tarefa = youtube.prewarm("v1")
        assert tarefa is not None
        await tarefa

    asyncio.run(run())

    assert youtube.is_cached("v1")


def test_prewarm_nao_disputa_com_extracao_em_voo(monkeypatch):
    """Com o pool ocupado (extração rodando ou na fila), palpite não entra:
    mesmo em prioridade baixa ele ocuparia o worker se a fila esvaziasse
    antes de um pedido real chegar."""
    monkeypatch.setattr(youtube._YTDLP_POOL, "busy", lambda: True)

    async def run():
        assert youtube.prewarm("v1") is None

    asyncio.run(run())


def test_prewarm_nao_refaz_o_que_ja_esta_quente():
    youtube._cache_put("v1", {"video_id": "v1", "stream_url": "quente"})

    async def run():
        assert youtube.prewarm("v1") is None

    asyncio.run(run())


def test_prewarm_que_falha_nao_vaza_excecao(monkeypatch, caplog):
    def explode(url, **k):
        raise RuntimeError("morreu")

    monkeypatch.setattr(youtube, "_extract", explode)

    async def run():
        tarefa = youtube.prewarm("v1")
        assert tarefa is not None
        # espera por condição, não por sleep fixo: com o suite inteiro
        # rodando, 50ms não bastavam para a thread do pool acordar
        for _ in range(200):
            if tarefa.done():
                break
            await asyncio.sleep(0.01)
        assert tarefa.done()

    asyncio.run(run())

    assert "never retrieved" not in caplog.text


# ── /play <nome> aquece o 1º resultado ───────────────────────────────────────


def test_busca_por_nome_aquece_o_primeiro_resultado(monkeypatch):
    aquecidos = []
    monkeypatch.setattr(music_cog.youtube, "prewarm", lambda vid: aquecidos.append(vid))

    async def ensure_voice(ctx):
        return MagicMock()

    async def busca(query, limit=5):
        return [{"video_id": f"v{i}", "title": "T", "artist": "A", "duration": 10,
                 "thumbnail": ""} for i in range(3)]

    monkeypatch.setattr(music_cog, "ensure_voice", ensure_voice)
    monkeypatch.setattr(music_cog.youtube, "search_songs", busca)
    cog = music_cog.Music.__new__(music_cog.Music)
    cog.bot = MagicMock()
    cog.spotify = None
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.respond = AsyncMock()
    ctx.author.id = 1

    asyncio.run(cog.play.callback(cog, ctx, "calcinha preta", "adicionar"))

    assert aquecidos == ["v0"]
    ctx.respond.assert_awaited_once()  # o dropdown saiu normalmente
