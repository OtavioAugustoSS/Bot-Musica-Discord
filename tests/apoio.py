"""Dublês e montagem do player compartilhados pelos testes.

Até 20/09/2026, 14 arquivos criavam o player com `GuildPlayer.__new__` e
repetiam à mão a lista de ~17 atributos do `__init__` (que não podia rodar:
o PlayerPanel monta uma View do Discord). Cada atributo novo no player eram 14
edições — e o que faltava num arquivo virava AttributeError só naquele teste.

`faz_player` roda o `__init__` DE VERDADE com um painel de mentira. Os arquivos
que precisam de um dublê mais específico (voz que conta desconexões, painel que
anota a ordem das chamadas…) continuam com o seu e só o passam para cá.
"""
from __future__ import annotations

from core.player import GuildPlayer


class FakeVoice:
    """Voz conectada e parada — o ponto de partida da maioria dos testes."""

    def __init__(self, *, playing: bool = False, paused: bool = False, connected: bool = True,
                 channel=None) -> None:
        self.playing = playing
        self.paused = paused
        self.connected = connected
        self.channel = channel

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing

    def is_paused(self) -> bool:
        return self.paused


class FakeGuild:
    def __init__(self, voice=None, guild_id: int = 1) -> None:
        self.id = guild_id
        self.voice_client = voice


class FakePanel:
    async def ensure_visible(self, channel) -> None:
        pass

    async def refresh(self) -> None:
        pass

    async def destroy(self) -> None:
        pass


class FakeChannel:
    """Canal de texto que guarda o que o bot mandou."""

    def __init__(self) -> None:
        self.sent: list = []
        self.embeds: list = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        if kwargs.get("embed") is not None:
            self.embeds.append(kwargs["embed"])


_SEM_VOZ = object()


def faz_player(*, guild=None, voice=_SEM_VOZ, channel=None, panel=None, bot=None, **atributos) -> GuildPlayer:
    """Um GuildPlayer de verdade, sem Discord.

    `guild` OU `voice` (com `voice`, monta um FakeGuild em volta); `channel` é o
    canal de texto; o resto (`_stopping=True`, `autoplay_enabled=True`…) é
    aplicado por cima do `__init__`.
    """
    if guild is None:
        guild = FakeGuild(FakeVoice() if voice is _SEM_VOZ else voice)
    player = GuildPlayer(bot, guild, panel=panel if panel is not None else FakePanel())
    player.text_channel = channel
    for nome, valor in atributos.items():
        setattr(player, nome, valor)
    return player
