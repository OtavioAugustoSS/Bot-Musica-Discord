"""O caminho do erro do Spotify no COG — não só o helper.

`test_spotify_algoritmicas` provava que `is_algorithmic_playlist` reconhece
os IDs certos, mas ninguém exercitava o `_play_spotify` que a USA. O nome
nunca foi importado em cogs/music.py: toda playlist que falhava (a situação
que a mensagem existe para explicar) morria em NameError e a pessoa via o
"deu ruim aqui do meu lado" genérico.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import messages as m
from cogs import music as music_cog


def _ctx():
    ctx = MagicMock()
    ctx.respond = AsyncMock()
    ctx.author.id = 1
    return ctx


def _cog(boom: Exception) -> music_cog.Music:
    cog = music_cog.Music.__new__(music_cog.Music)
    cog.bot = MagicMock()
    cog.spotify = MagicMock()
    cog.spotify.fetch = AsyncMock(side_effect=boom)
    return cog


def test_playlist_algoritmica_que_falha_recebe_a_explicacao_certa():
    ctx = _ctx()
    asyncio.run(_cog(RuntimeError("404")).
                _play_spotify(ctx, MagicMock(), "playlist", "37i9dQZF1E8zLMgLirolMz"))

    ctx.respond.assert_awaited_once_with(m.SPOTIFY_ALGORITMICA)


def test_playlist_comum_que_falha_recebe_a_dica_de_deixar_publica():
    ctx = _ctx()
    asyncio.run(_cog(RuntimeError("403")).
                _play_spotify(ctx, MagicMock(), "playlist", "7fwIHqGdaEiSwTVJQPCalI"))

    ctx.respond.assert_awaited_once_with(m.SPOTIFY_PLAYLIST_FAIL)


def test_faixa_que_falha_recebe_a_mensagem_de_link():
    ctx = _ctx()
    asyncio.run(_cog(RuntimeError("x")).
                _play_spotify(ctx, MagicMock(), "track", "abc"))

    ctx.respond.assert_awaited_once_with(m.SPOTIFY_LINK_FAIL)
