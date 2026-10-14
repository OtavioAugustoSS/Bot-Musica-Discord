"""O player atrás da chave AUDIO_OPUS_DIRETO.

Desligada (o padrão no código) tudo segue pelo PCM de sempre, volume 0,8. Ligada
(só no .env da VM, até o teste de ouvido): Opus 48 kHz estéreo do YouTube é
COPIADO; o resto — itag 18/AAC, vídeo que já mandou pacote fora de 20ms — é
recodificado dentro do FFmpeg. Medido na VM: 118 ms de CPU por segundo de áudio
no PCM, 17 copiando, 43 recodificando.
"""
import asyncio
import logging
from unittest.mock import MagicMock

import discord
import pytest

import config
import core.player as player_mod
from apoio import faz_player
from core import audio
from core.audio import FonteDaFaixa, PacotesProntos, SaidaDoFfmpeg
from core.player import _Tocada
from core.queue import Track

OPUS = {"stream_url": "http://x/a", "acodec": "opus", "asr": 48000, "canais": 2, "itag": "251"}
AAC = {"stream_url": "http://x/a", "acodec": "mp4a.40.2", "asr": 44100, "canais": 2, "itag": "18"}
ANTIGA = {"stream_url": "http://x/a"}  # cache gravado antes de a extração dizer o codec
FAIXA = Track(title="Numb", artist="Linkin Park", video_id="kXYiU_JCYtU", duration=187)


class _Muda(discord.AudioSource):
    def __init__(self, opus: bool) -> None:
        self.opus = opus

    def read(self) -> bytes:
        return b""

    def is_opus(self) -> bool:
        return self.opus


@pytest.fixture
def fabricas(monkeypatch):
    """Anota com que argumentos cada FFmpeg seria aberto, sem abrir nenhum."""
    abertos = []

    def opus(url, **kw):
        abertos.append(("opus", kw))
        return _Muda(True)

    def pcm(url, **kw):
        abertos.append(("pcm", kw))
        return _Muda(False)

    monkeypatch.setattr(player_mod, "FonteOpus", opus)
    monkeypatch.setattr(player_mod.discord, "FFmpegPCMAudio", pcm)
    monkeypatch.setattr(player_mod, "_SEM_COPIA", set())
    return abertos


def _abre(player, info, track=FAIXA, **kw):
    saida = SaidaDoFfmpeg("teste")
    try:
        return player._abre_ffmpeg(info, track, saida, **kw)
    finally:
        saida.comeca()


def test_chave_desligada_e_o_pcm_de_sempre(fabricas, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", False)

    fonte, via = _abre(faz_player(), OPUS)

    assert [a[0] for a in fabricas] == ["pcm"]
    assert via == "pcm" and fonte.is_opus() is False
    assert fonte.volume == pytest.approx(0.8)


def test_opus_do_youtube_e_copiado(fabricas, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", True)

    fonte, via = _abre(faz_player(), OPUS)

    assert via == "direto" and fonte.is_opus() is True
    tipo, kw = fabricas[0]
    assert tipo == "opus" and kw["copia"] is True and kw["inicio_s"] == 0.0


@pytest.mark.parametrize("info", [AAC, ANTIGA])
def test_o_que_nao_e_opus_48k_estereo_recodifica_no_ffmpeg(fabricas, monkeypatch, info):
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", True)

    _, via = _abre(faz_player(), info)

    assert via == "recodificado" and fabricas[0][1]["copia"] is False


def test_seek_passa_o_ponto_de_partida_nos_dois_caminhos(fabricas, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", True)
    _abre(faz_player(), OPUS, desde_s=75.0)
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", False)
    _abre(faz_player(), OPUS, desde_s=75.0)

    assert fabricas[0][1]["inicio_s"] == 75.0
    assert "-ss 75.00" in fabricas[1][1]["before_options"]


def test_video_que_mandou_pacote_fora_de_20ms_nao_e_mais_copiado(fabricas, monkeypatch, caplog):
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", True)
    player = faz_player()
    fonte = MagicMock(frames_entregues=50 * 100, frames_sem_audio=0, primeiro_audio_s=0.5, fora_de_20ms=12)

    with caplog.at_level(logging.INFO):
        player._relata_fim(_Tocada(track=FAIXA, fonte=fonte, saida=None), None)
    _, via = _abre(player, OPUS)

    assert "12 pacote(s) fora de 20ms" in caplog.text
    assert via == "recodificado"


def test_tocar_entrega_ao_pycord_uma_fonte_da_faixa(fabricas, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", True)
    voz = MagicMock()
    voz.is_playing.return_value = False
    player = faz_player(guild=MagicMock(voice_client=voz))

    asyncio.run(player._build_and_play(OPUS, FAIXA))
    fonte = voz.play.call_args.args[0]
    fonte.cleanup()

    assert isinstance(fonte, FonteDaFaixa) and fonte.is_opus() is True
    assert player._source is fonte


# ── o estalo de escuta ──────────────────────────────────────────────────────

ESTALO = bytes(audio.FRAME_BYTES * 30)  # 0,6s


def _parado(monkeypatch, opus: bool):
    monkeypatch.setattr(config, "VOICE_SOM_ESCUTA", True)
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", opus)
    voz = MagicMock()
    voz.is_connected.return_value = True
    voz.is_playing.return_value = False
    voz.is_paused.return_value = False
    return faz_player(guild=MagicMock(voice_client=voz)), voz


def test_estalo_com_o_bot_parado_sai_de_pacotes_prontos(monkeypatch):
    player, voz = _parado(monkeypatch, opus=True)

    player.tocar_efeito(ESTALO)

    assert isinstance(voz.play.call_args.args[0], PacotesProntos)


def test_estalo_com_o_bot_parado_no_pcm_nao_muda(monkeypatch):
    player, voz = _parado(monkeypatch, opus=False)

    player.tocar_efeito(ESTALO)

    assert isinstance(voz.play.call_args.args[0], discord.PCMAudio)


def test_espera_do_estalo_conta_as_bordas_de_silencio_em_opus(monkeypatch):
    import time

    player, voz = _parado(monkeypatch, opus=True)
    antes = time.monotonic()

    player.tocar_efeito(ESTALO)

    assert player._efeito_ate - antes == pytest.approx(0.6 + 0.08, abs=0.02)
