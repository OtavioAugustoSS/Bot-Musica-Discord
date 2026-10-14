"""Regressão: py-cord precisa de anotações reais (não strings) nas Options.

`from __future__ import annotations` num cog quebra o parsing das opções
(_raw_type vira string → TypeError em issubclass no _invoke) e o comando
morre antes de responder ("O aplicativo não respondeu").

Os comandos são inspecionados após o add_cog — é nesse binding que o
py-cord faz o parse definitivo das opções.
"""
import asyncio

import discord
import pytest

from cogs.diag import Diag
from cogs.favorites import Favorites
from cogs.music import Music


@pytest.fixture(scope="module")
def commands():
    # discord.Bot() pede o "loop atual"; qualquer asyncio.run() de um teste
    # anterior deixa esse loop como None e o construtor explode — o resultado
    # dependia da ORDEM dos arquivos de teste
    asyncio.set_event_loop(asyncio.new_event_loop())
    bot = discord.Bot(intents=discord.Intents.default())
    bot.add_cog(Music(bot))
    bot.add_cog(Favorites(bot))
    bot.add_cog(Diag(bot))
    return list(bot._pending_application_commands)


def test_all_commands_registered(commands):
    names = {cmd.name for cmd in commands}
    assert names >= {
        "play", "pause", "resume", "skip", "stop", "clear", "queue", "shuffle",
        "loop", "remove", "autoplay", "fav", "favs", "playfavs", "history",
        "replay", "diag", "seek", "volume",
    }


def test_option_types_are_real_classes(commands):
    for cmd in commands:
        for op in cmd.options:
            assert isinstance(op._raw_type, type), (
                f"/{cmd.name} opção '{op.name}': _raw_type é {op._raw_type!r},"
                " deveria ser uma classe"
            )


def test_ctx_never_becomes_an_option(commands):
    for cmd in commands:
        names = [op.name for op in cmd.options]
        assert "ctx" not in names, f"/{cmd.name} registrou 'ctx' como opção: {names}"


def test_expected_option_types(commands):
    by_name = {cmd.name: cmd for cmd in commands}
    assert [(o.name, o._raw_type) for o in by_name["play"].options] == [
        ("busca", str),
        ("modo", str),
    ]
    assert [(o.name, o._raw_type) for o in by_name["remove"].options] == [("posicao", int)]
    assert [(o.name, o._raw_type) for o in by_name["seek"].options] == [("posicao", str)]
    assert [(o.name, o._raw_type, o.required) for o in by_name["volume"].options] == [
        ("nivel", int, False)
    ]
    assert [(o.name, o._raw_type) for o in by_name["favs"].options] == [("remover", bool)]
