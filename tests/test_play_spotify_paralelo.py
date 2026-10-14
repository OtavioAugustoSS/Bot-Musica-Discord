"""O Spotify também busca durante o handshake de voz, não depois.

O /play com link do YouTube e a busca por nome já rodavam em paralelo com o
ensure_voice (0,4–5,9s de DAVE). O caminho do Spotify ficou de fora: só
depois de conectar é que ia à API do Spotify — os tempos somavam.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import messages as m
from cogs import music as music_cog

LINK = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


def test_spotify_busca_durante_o_handshake(monkeypatch):
    eventos = []
    player = MagicMock()
    player.enqueue = AsyncMock()

    async def ensure_voice_lento(ctx):
        eventos.append("voz:inicio")
        await asyncio.sleep(0.03)
        eventos.append("voz:fim")
        return player

    async def fetch(kind, spotify_id):
        eventos.append("spotify:inicio")
        await asyncio.sleep(0.01)
        eventos.append("spotify:fim")
        return "Faixa", [{"title": "T", "artist": "A", "duration": 10, "thumbnail": ""}]

    monkeypatch.setattr(music_cog, "ensure_voice", ensure_voice_lento)
    cog = music_cog.Music.__new__(music_cog.Music)
    cog.bot = MagicMock()
    cog.spotify = MagicMock()
    cog.spotify.fetch = fetch
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.respond = AsyncMock()
    ctx.author.id = 1

    asyncio.run(cog.play.callback(cog, ctx, LINK, "adicionar"))

    assert eventos.index("spotify:inicio") < eventos.index("voz:fim"), eventos
    ctx.respond.assert_awaited_once_with(
        m.SPOTIFY_ADDED.format(kind="Faixa", name="Faixa", count=1, note="")
    )
    player.enqueue.assert_awaited_once()


def test_sem_spotify_configurado_nao_tenta_buscar(monkeypatch):
    async def sem_voz(ctx):
        return None

    monkeypatch.setattr(music_cog, "ensure_voice", sem_voz)
    cog = music_cog.Music.__new__(music_cog.Music)
    cog.bot = MagicMock()
    cog.spotify = None
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.respond = AsyncMock()

    asyncio.run(cog.play.callback(cog, ctx, LINK, "adicionar"))  # não pode explodir
