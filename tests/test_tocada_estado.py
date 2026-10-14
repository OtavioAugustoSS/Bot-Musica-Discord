"""O que aconteceu com a faixa fica NA faixa — e o tempo é o do áudio entregue.

Dois defeitos que o seek tornaria visíveis:

1. `_after_track` lia `self._skip_requested` na hora do callback. Um /play que
   chegasse entre o skip e o callback (o `after` roda na thread de áudio, até
   50ms depois) zerava a flag ao montar a faixa nova: a pulada virava "MORREU NA
   LARGADA" (se tinha tocado <5s) ou "CORTADA" — WARNING falso para o dono e um
   ponto no disjuntor.
2. `elapsed` era relógio de parede desde o `voice.play()`: contava a abertura do
   FFmpeg, o silêncio de pulmão vazio e o estalo de escuta. Com seek ele nem
   teria como estar certo. Agora é posição = destino do seek + áudio entregue.
"""
import logging
from unittest.mock import MagicMock

from apoio import faz_player
from core.player import _Tocada
from core.queue import Track

FAIXA = Track(title="Mind Over Matter", artist="Young the Giant", video_id="LpRby_Osyjc", duration=183)


class Fonte:
    """Os contadores de uma FonteDaFaixa, sem áudio."""

    def __init__(self, tocou_s: float, posicao_s: float | None = None) -> None:
        self.frames_entregues = int(tocou_s * 50)
        self.frames_sem_audio = 0
        self.falhas_de_buffer = 0
        self.fora_de_20ms = 0
        self.primeiro_audio_s = 0.5
        self.posicao_s = tocou_s if posicao_s is None else posicao_s
        self.interrompida = False

    def interrompe(self) -> None:
        self.interrompida = True


def _tocando():
    voz = MagicMock()
    voz.is_connected.return_value = True
    voz.is_playing.return_value = True
    voz.is_paused.return_value = False
    player = faz_player(guild=MagicMock(voice_client=voz))
    player.guild.id = 1
    return player, voz


def test_skip_marca_a_faixa_que_tocava_e_nao_uma_flag_do_player(caplog):
    player, voz = _tocando()
    antiga = _Tocada(track=FAIXA, fonte=Fonte(2.0), saida=None)
    player._tocada, player._source = antiga, antiga.fonte

    player.skip()
    # o /play que chega antes do callback: a faixa NOVA já é a atual do player
    player._tocada = _Tocada(track=FAIXA, fonte=Fonte(0.0), saida=None)
    with caplog.at_level(logging.INFO, logger="core.player"):
        morreu = player._relata_fim(antiga, None)

    assert antiga.pulada is True and antiga.fonte.interrompida
    assert morreu is False
    assert "pulada" in caplog.text and "MORREU" not in caplog.text


def test_stop_marca_a_faixa_como_parada(caplog):
    import asyncio

    from unittest.mock import AsyncMock

    player, voz = _tocando()
    voz.disconnect = AsyncMock()
    tocada = _Tocada(track=FAIXA, fonte=Fonte(40.0), saida=None)
    player._tocada, player._source = tocada, tocada.fonte

    asyncio.run(player.stop("teste"))
    with caplog.at_level(logging.INFO, logger="core.player"):
        player._relata_fim(tocada, None)

    assert tocada.parada is True
    assert "(parada)" in caplog.text


def test_elapsed_e_a_posicao_da_musica_e_nao_o_relogio_de_parede():
    player, _ = _tocando()
    player._started_at = 1.0  # "há faixa": começou há uma eternidade no relógio de parede
    player._source = Fonte(tocou_s=12.0, posicao_s=95.4)  # seek para 1:23 + 12s tocados

    assert player.elapsed == 95


def test_sem_faixa_o_elapsed_e_zero():
    player, _ = _tocando()
    player._started_at = 0.0
    player._source = Fonte(30.0)

    assert player.elapsed == 0


def test_seek_para_perto_do_fim_nao_vira_morte_na_largada(caplog):
    """Seek para 3:00 de uma faixa de 3:03, logo no começo: tocou 4s ao todo e
    acabou — do jeito certo. Pelo tempo tocado seria "morreu na largada", com
    direito a segunda tentativa e ponto no disjuntor."""
    player, _ = _tocando()
    tocada = _Tocada(track=FAIXA, fonte=Fonte(tocou_s=4.0, posicao_s=183.0), saida=None)

    with caplog.at_level(logging.INFO, logger="core.player"):
        morreu = player._relata_fim(tocada, None)

    assert morreu is False
    assert "(acabou)" in caplog.text
    assert "posição 3:03" in caplog.text  # tocou 0:04, mas estava no fim


def test_ffmpeg_do_seek_que_morre_e_corte_e_nao_largada(caplog):
    """A fonte nova (depois do seek para 1:40) morreu em 2s: a faixa foi CORTADA
    em 1:42. Não é largada — tentar "de novo do zero" devolveria a música ao
    começo sem ninguém pedir."""
    player, _ = _tocando()
    tocada = _Tocada(track=FAIXA, fonte=Fonte(tocou_s=3.0, posicao_s=102.0), saida=None)

    with caplog.at_level(logging.INFO, logger="core.player"):
        morreu = player._relata_fim(tocada, None)

    assert morreu is False
    assert "CORTADA" in caplog.text


def test_sem_seek_a_morte_na_largada_continua_valendo(caplog):
    player, _ = _tocando()
    tocada = _Tocada(track=FAIXA, fonte=Fonte(tocou_s=0.4), saida=None)

    with caplog.at_level(logging.INFO, logger="core.player"):
        morreu = player._relata_fim(tocada, None)

    assert morreu is True
    assert "MORREU NA LARGADA" in caplog.text
