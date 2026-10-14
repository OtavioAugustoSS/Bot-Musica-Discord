"""A busca/extração começa junto com a conexão de voz, não depois.

Medido nos logs de produção, o handshake de voz leva 0,4s a 5,9s (7 de 10
conexões ficam em ~5,4s — é a negociação DAVE do Discord, fora do nosso
controle). Hoje o `/play` faz `ensure_voice` e SÓ ENTÃO busca/extrai, então
os tempos somam:

    handshake 5,4s + extração 8,4s = 13,8s até o áudio sair

Como nada da busca depende da conexão, dá para rodar em paralelo e pagar só
o maior dos dois. É o "quando ele entra demora alguns segundos pra tocar".
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs import music as music_cog


class Relogio:
    """Registra em que ordem as coisas começaram e terminaram."""

    def __init__(self) -> None:
        self.eventos: list[str] = []

    def marca(self, nome: str) -> None:
        self.eventos.append(nome)

    def antes(self, a: str, b: str) -> bool:
        return self.eventos.index(a) < self.eventos.index(b)


@pytest.fixture
def cenario(monkeypatch):
    relogio = Relogio()

    async def ensure_voice_lento(ctx):
        relogio.marca("voz:inicio")
        await asyncio.sleep(0.05)  # imita o handshake DAVE
        relogio.marca("voz:fim")
        player = MagicMock()
        player.enqueue = AsyncMock()
        player.replace = AsyncMock()
        return player

    async def busca(query, limit=5):
        relogio.marca("busca:inicio")
        await asyncio.sleep(0.01)
        relogio.marca("busca:fim")
        return [{"video_id": "v1", "title": "T", "artist": "A", "duration": 10,
                 "thumbnail": ""}]

    async def stream(url, **kwargs):
        relogio.marca("extracao:inicio")
        await asyncio.sleep(0.01)
        relogio.marca("extracao:fim")
        return {"video_id": "v1", "title": "T", "artist": "A", "duration": 10,
                "thumbnail": "", "stream_url": "http://x"}

    async def sem_oembed(video_id):
        return None  # sem o atalho: o caminho medido aqui e o da extracao

    monkeypatch.setattr(music_cog, "ensure_voice", ensure_voice_lento)
    monkeypatch.setattr(music_cog.youtube, "search_songs", busca)
    monkeypatch.setattr(music_cog.youtube, "get_stream", stream)
    monkeypatch.setattr(music_cog.youtube, "quick_info", sem_oembed)
    return relogio


def _ctx():
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.respond = AsyncMock()
    ctx.author.id = 1
    ctx.channel = MagicMock()
    return ctx


def _cog():
    cog = music_cog.Music.__new__(music_cog.Music)
    cog.bot = MagicMock()
    cog.spotify = None
    return cog


def test_link_do_youtube_extrai_durante_o_handshake(cenario):
    ctx = _ctx()
    asyncio.run(_cog().play.callback(
        _cog(), ctx, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "adicionar"
    ))

    assert cenario.antes("extracao:inicio", "voz:fim"), cenario.eventos


def test_busca_por_nome_roda_durante_o_handshake(cenario):
    ctx = _ctx()
    asyncio.run(_cog().play.callback(_cog(), ctx, "calcinha preta", "adicionar"))

    assert cenario.antes("busca:inicio", "voz:fim"), cenario.eventos


def test_sem_canal_de_voz_descarta_o_trabalho_adiantado(monkeypatch, cenario):
    """Sem call, o trabalho iniciado tem de ser encerrado — senão o log reclama
    de "Task exception was never retrieved" a cada /play fora de canal."""
    async def sem_voz(ctx):
        return None

    monkeypatch.setattr(music_cog, "ensure_voice", sem_voz)
    descartadas = []
    original = music_cog._descarta

    async def espia(tarefa):
        descartadas.append(tarefa)
        await original(tarefa)

    monkeypatch.setattr(music_cog, "_descarta", espia)

    asyncio.run(_cog().play.callback(_cog(), ctx := _ctx(), "calcinha preta", "adicionar"))

    tarefas = [t for t in descartadas if t is not None]  # o oEmbed so existe p/ link
    assert len(tarefas) == 1
    assert tarefas[0].done()
    assert ctx.respond.await_count == 0  # quem responde é o ensure_voice


def test_url_de_fora_do_youtube_nem_comeca_a_extrair(cenario):
    """A allowlist anti-SSRF continua valendo ANTES de qualquer rede."""
    ctx = _ctx()
    asyncio.run(_cog().play.callback(_cog(), ctx, "https://evil.com/x", "adicionar"))

    assert "extracao:inicio" not in cenario.eventos
    ctx.respond.assert_awaited()
