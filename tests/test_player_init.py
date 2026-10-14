"""Regressão: GuildPlayer real precisa construir por inteiro.

O PlayerPanel/PlayerView lê o estado do player no sync() já na construção —
se o painel for criado antes dos atributos de estado, explode AttributeError
(foi o caso de autoplay_enabled) e todo comando morre em "não respondeu".
"""
import asyncio

from core.player import GuildPlayer


class FakeGuild:
    id = 1
    voice_client = None


class FakeBot:
    loop = None
    db = None


def test_guild_player_constructs_completely():
    async def build():
        return GuildPlayer(FakeBot(), FakeGuild())

    player = asyncio.run(build())
    assert player.autoplay_enabled is False
    assert player.panel.view.btn_autoplay is not None
    assert player.queue.current is None
