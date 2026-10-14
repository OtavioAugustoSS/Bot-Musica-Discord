"""O painel não pode segurar o começo da música.

Journal do teste de 20/09 (18:19): entre "fila mudou (pedido)" e o início da
busca/extração passaram 0,47s — o `enqueue` esperava o painel ser reenviado ao
canal (delete + send na API do Discord) ANTES de chamar o play_next. Meio
segundo de silêncio a mais em todo /play com o bot parado, por uma mensagem
que não depende em nada do áudio.
"""
import asyncio

import pytest

from apoio import faz_player
from core.player import GuildPlayer
from core.queue import MusicQueue, Track


class FakeVoice:
    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return False

    def is_paused(self) -> bool:
        return False


class FakeGuild:
    id = 1

    def __init__(self) -> None:
        self.voice_client = FakeVoice()


class PainelLento:
    """Painel cuja ida ao Discord leva `atraso` — e anota a ordem dos eventos."""

    def __init__(self, ordem: list, atraso: float = 0.2, erro: Exception | None = None) -> None:
        self.ordem = ordem
        self.atraso = atraso
        self.erro = erro

    async def ensure_visible(self, channel) -> None:
        self.ordem.append("painel começou")
        await asyncio.sleep(self.atraso)
        if self.erro is not None:
            raise self.erro
        self.ordem.append("painel terminou")

    async def refresh(self) -> None:
        pass


def _player(ordem: list, **painel) -> GuildPlayer:
    player = faz_player(guild=FakeGuild(), panel=PainelLento(ordem, **painel))

    async def toca(track):
        ordem.append("extração começou")
        return True

    player._try_start = toca
    return player


FAIXA = Track(title="Musica", artist="A", video_id="v1")


def test_a_extracao_nao_espera_o_painel():
    ordem: list = []
    player = _player(ordem)

    asyncio.run(player.enqueue([FAIXA], object()))

    assert ordem.index("extração começou") < ordem.index("painel terminou")


def test_enqueue_so_volta_com_o_painel_no_lugar():
    """Quem chama (o /play) conta com o painel já publicado ao retornar."""
    ordem: list = []
    player = _player(ordem)

    asyncio.run(player.enqueue([FAIXA], object()))

    assert ordem[-1] == "painel terminou"


def test_erro_no_painel_continua_chegando_a_quem_chamou():
    ordem: list = []
    player = _player(ordem, erro=RuntimeError("canal sem permissão"))

    with pytest.raises(RuntimeError, match="sem permissão"):
        asyncio.run(player.enqueue([FAIXA], object()))

    assert "extração começou" in ordem  # e a música não ficou refém do painel


def test_substituir_a_fila_tambem_nao_espera_o_painel():
    ordem: list = []
    player = _player(ordem)

    asyncio.run(player.replace([FAIXA], object()))

    assert ordem.index("extração começou") < ordem.index("painel terminou")
