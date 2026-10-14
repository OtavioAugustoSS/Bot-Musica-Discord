"""`/queue` mostra a rádio do autoplay — separada, e sem dizer "fila vazia".

Com a rádio numa fila própria, o /queue só lia `upcoming`: com autoplay
ligado e 3 faixas de rádia a caminho, dizia "Fila vazia, manda um /play" —
e a pessoa não tinha como ver o que vinha a seguir.
"""
import asyncio

import messages as m
from cogs.music import QueueView
from apoio import faz_player
from core.player import GuildPlayer
from core.queue import MusicQueue, Track


def _player() -> GuildPlayer:
    p = faz_player(voice=None, autoplay_enabled=True)
    return p


def _embed(p: GuildPlayer):
    async def build():
        return QueueView(p).build_embed()

    return asyncio.run(build())


def test_radio_aparece_num_bloco_proprio():
    p = _player()
    p.queue.add(Track(title="minha", video_id="m1"))
    p.queue.add_radio([Track(title="r1", video_id="r1", from_autoplay=True),
                       Track(title="r2", video_id="r2", from_autoplay=True)])

    embed = _embed(p)

    campos = {f.name: f.value for f in embed.fields}
    radio = next(v for k, v in campos.items() if "🎲" in k)
    assert "r1" in radio and "r2" in radio
    assert "minha" in embed.description and "r1" not in embed.description


def test_so_radio_nao_diz_fila_vazia():
    p = _player()
    p.queue.add_radio([Track(title="r1", video_id="r1", from_autoplay=True)])

    embed = _embed(p)

    assert embed.description != m.QUEUE_EMPTY
    assert any("🎲" in f.name for f in embed.fields)


def test_sem_nada_continua_dizendo_fila_vazia():
    embed = _embed(_player())

    assert embed.description == m.QUEUE_EMPTY
    assert not any("🎲" in f.name for f in embed.fields)
