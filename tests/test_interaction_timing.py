"""Regressão: /play precisa dar defer ANTES de qualquer trabalho lento.

A janela de resposta de uma interação é de 3s; conectar na voz estoura isso.
Se o defer não vier primeiro, o Discord mostra "O aplicativo não respondeu"
(NotFound 10062 no defer tardio).
"""
import asyncio

from cogs.music import Music


class FakeVoicelessAuthor:
    voice = None


class FakeCtx:
    def __init__(self):
        self.calls = []
        self.author = FakeVoicelessAuthor()
        self.bot = None
        self.guild = None

    async def defer(self):
        self.calls.append("defer")

    async def respond(self, *args, **kwargs):
        self.calls.append("respond")


def test_play_defers_before_any_slow_work():
    cog = Music.__new__(Music)  # sem __init__: não precisamos de bot/spotify
    cog.spotify = None
    ctx = FakeCtx()

    asyncio.run(Music.play.callback(cog, ctx, "uma musica qualquer", "adicionar"))

    assert ctx.calls[0] == "defer", (
        f"primeira chamada foi {ctx.calls[:1]} — o defer precisa vir antes de tudo"
    )
