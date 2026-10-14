"""A próxima faixa montada ANTES de a atual acabar.

Entre "fim de X" e o som de Y passavam ~1,3s mesmo com Y no cache: abrir o
FFmpeg (TLS + HTTP com o googlevideo) e encher o pré-buffer do pulmão. Só que o
FFmpeg da faixa atual termina de baixar ~8s ANTES de a música acabar (é o que o
pulmão tem adiantado) — tempo de sobra para montar a fonte da próxima.

Regras que seguram isto no lugar:
  - só monta se a extração da próxima JÁ está no cache (nunca dispara os ~5s de
    CPU de uma extração por causa disto — é o que o prefetch faz, com prioridade);
  - quem decide se a fonte pronta serve é o CONSUMO: tem de ser a mesma faixa
    que a fila entregou, estar viva e ser recente. Fila mexida, skip, rádio
    desligada, loop — nada disso precisa avisar ninguém; o que não serve é
    descartado e a faixa começa pelo caminho normal, sem contar como falha.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import core.player as player_mod
from apoio import faz_player
from core.audio import BufferedAudioSource, FonteDaFaixa
from core.player import _Pronta, _Tocada
from core.queue import LoopMode, Track

ATUAL = Track(title="Atual", artist="A", video_id="aaaaaaaaaaa", duration=180)
PROXIMA = Track(title="Proxima", artist="B", video_id="bbbbbbbbbbb", duration=200)
INFO = {"stream_url": "http://x/b", "duration": 200, "thumbnail": "", "acodec": "opus", "asr": 48000,
        "canais": 2, "itag": "251"}


# ── o aviso do pulmão ───────────────────────────────────────────────────────


class _Curta(discord.AudioSource):
    def __init__(self, quantos: int, trava: bool = False) -> None:
        self.quantos, self.trava, self.solta = quantos, trava, False

    def read(self) -> bytes:
        while self.trava and not self.solta:
            time.sleep(0.005)
        if self.quantos <= 0:
            return b""
        self.quantos -= 1
        return b"\xf8\x01"

    def is_opus(self) -> bool:
        return True

    def cleanup(self) -> None:
        self.solta = True


def _espera(condicao, timeout: float = 3.0) -> None:
    fim = time.monotonic() + timeout
    while not condicao() and time.monotonic() < fim:
        time.sleep(0.005)


def test_pulmao_avisa_uma_vez_quando_o_ffmpeg_termina_com_audio_ainda_na_fila():
    avisos = []
    pulmao = BufferedAudioSource(_Curta(20), segundos=2.0, prebuffer_s=0.02)
    pulmao.quando_acabar(lambda: avisos.append(pulmao._fila.qsize()))
    try:
        _espera(lambda: avisos)
        time.sleep(0.05)
    finally:
        pulmao.cleanup()

    assert len(avisos) == 1


def test_pulmao_interrompido_nao_avisa():
    """Skip/stop/seek matam a fonte: isso não é "a faixa está acabando"."""
    avisos = []
    pulmao = BufferedAudioSource(_Curta(20, trava=True), segundos=2.0, prebuffer_s=0.02)
    pulmao.quando_acabar(lambda: avisos.append(1))

    pulmao.cleanup()
    time.sleep(0.05)

    assert avisos == []


# ── o player ────────────────────────────────────────────────────────────────


class Pulmao:
    def __init__(self, *, viva: bool = True) -> None:
        self.frames_entregues = self.frames_sem_audio = self.falhas_de_buffer = 0
        self.primeiro_audio_s = None
        self.original = SimpleNamespace(fora_de_20ms=0)
        self.viva = viva
        self.limpou = self.marcou = False

    def quando_acabar(self, aviso) -> None:
        self.aviso = aviso

    def is_opus(self) -> bool:
        return True

    def encheu(self) -> bool:
        return self.viva

    def acabou_vazia(self) -> bool:
        return not self.viva

    def marca_inicio(self) -> None:
        self.marcou = True

    def read(self) -> bytes:
        return b""

    def interrompe(self) -> None:
        pass

    def cleanup(self) -> None:
        self.limpou = True


@pytest.fixture
def cena(monkeypatch):
    voz = MagicMock()
    voz.is_connected.return_value = True
    voz.is_playing.return_value = True
    voz.is_paused.return_value = False
    voz.disconnect = AsyncMock()
    bot = MagicMock()
    bot.db.add_history = AsyncMock()
    bot.db.get_volume = AsyncMock(return_value=100)
    player = faz_player(guild=MagicMock(voice_client=voz), bot=bot)
    player.guild.id = 1
    player.queue.current = ATUAL
    player.queue.upcoming.append(PROXIMA)
    player._tocada = _Tocada(track=ATUAL, fonte=FonteDaFaixa(Pulmao()), saida=None)
    player._source = player._tocada.fonte
    player._started_at = 1.0
    montadas: list[Pulmao] = []
    estado = {"cache": "no cache", "extracoes": 0}

    async def monta(info, track, desde_s=0.0):
        montadas.append(Pulmao())
        return montadas[-1], None, "direto"

    async def stream(video_id, **kw):
        estado["extracoes"] += 1
        return INFO

    async def abre(info):
        return (0.0, 0)

    monkeypatch.setattr(player, "_monta", monta)
    monkeypatch.setattr(player, "_schedule_prefetch", lambda: None)
    monkeypatch.setattr(player_mod.youtube, "get_stream", stream)
    monkeypatch.setattr(player_mod.youtube, "stream_state", lambda vid: estado["cache"])
    monkeypatch.setattr(player_mod.youtube, "espera_stream_abrir", abre)
    return SimpleNamespace(player=player, voz=voz, montadas=montadas, estado=estado)


def test_monta_a_proxima_quando_ela_ja_esta_no_cache(cena):
    p = cena.player
    asyncio.run(p._prepara_a_proxima(p._tocada))

    assert p._pronta is not None and p._pronta.track is PROXIMA
    assert len(cena.montadas) == 1


def test_proxima_fora_do_cache_nao_dispara_extracao(cena):
    cena.estado["cache"] = "fria"

    asyncio.run(cena.player._prepara_a_proxima(cena.player._tocada))

    assert cena.player._pronta is None
    assert cena.montadas == [] and cena.estado["extracoes"] == 0


def test_faixa_pulada_antes_de_montar_nao_monta(cena):
    cena.player._tocada.pulada = True

    asyncio.run(cena.player._prepara_a_proxima(cena.player._tocada))

    assert cena.montadas == []


def test_faixa_que_acabou_enquanto_montava_descarta_a_fonte(cena, monkeypatch):
    p = cena.player
    tocada = p._tocada

    async def monta(info, track, desde_s=0.0):
        tocada.acabou = True  # o `after` chegou antes de o FFmpeg abrir
        cena.montadas.append(Pulmao())
        return cena.montadas[-1], None, "direto"

    monkeypatch.setattr(p, "_monta", monta)
    asyncio.run(p._prepara_a_proxima(tocada))

    assert p._pronta is None and cena.montadas[0].limpou


def test_loop_de_faixa_monta_a_mesma_de_novo(cena):
    p = cena.player
    p.queue.loop_mode = LoopMode.TRACK

    asyncio.run(p._prepara_a_proxima(p._tocada))

    assert p._pronta.track is ATUAL


def test_nao_monta_duas_vezes_a_mesma(cena):
    p = cena.player
    asyncio.run(p._prepara_a_proxima(p._tocada))
    asyncio.run(p._prepara_a_proxima(p._tocada))  # seek para trás no fim: outro EOF

    assert len(cena.montadas) == 1


def test_comeco_da_faixa_usa_a_fonte_pronta_sem_abrir_outro_ffmpeg(cena, caplog):
    import logging

    p = cena.player
    asyncio.run(p._prepara_a_proxima(p._tocada))
    pronta = cena.montadas[0]
    cena.voz.is_playing.return_value = False
    p.queue.advance()

    with caplog.at_level(logging.INFO, logger="core.player"):
        assert asyncio.run(p._try_start(PROXIMA)) is True

    assert len(cena.montadas) == 1  # nenhum FFmpeg novo
    tocando = cena.voz.play.call_args.args[0]
    assert tocando._fonte is pronta and pronta.marcou
    assert p._pronta is None and p._tocada.track is PROXIMA
    assert "fonte já pronta" in caplog.text


def test_fonte_pronta_de_outra_faixa_e_descartada_e_o_caminho_normal_segue(cena):
    p = cena.player
    asyncio.run(p._prepara_a_proxima(p._tocada))
    outra = Track(title="Outra", artist="C", video_id="ccccccccccc", duration=100)
    cena.voz.is_playing.return_value = False

    assert asyncio.run(p._try_start(outra)) is True

    assert cena.montadas[0].limpou and len(cena.montadas) == 2
    assert cena.voz.play.call_args.args[0]._fonte is cena.montadas[1]


def test_fonte_pronta_que_morreu_nao_conta_como_falha(cena):
    p = cena.player
    asyncio.run(p._prepara_a_proxima(p._tocada))
    cena.montadas[0].viva = False
    cena.voz.is_playing.return_value = False

    assert asyncio.run(p._try_start(PROXIMA)) is True

    assert cena.montadas[0].limpou and len(cena.montadas) == 2
    assert p._failures == [] and p._mortes_seguidas == 0


def test_fonte_pronta_velha_nao_e_usada(cena):
    """Música pausada por meia hora nos últimos segundos: o FFmpeg da próxima
    ficou esse tempo todo com a conexão parada — melhor abrir outro."""
    p = cena.player
    asyncio.run(p._prepara_a_proxima(p._tocada))
    p._pronta.feita_em -= 3600
    cena.voz.is_playing.return_value = False

    asyncio.run(p._try_start(PROXIMA))

    assert cena.montadas[0].limpou and len(cena.montadas) == 2


def test_stop_nao_deixa_ffmpeg_orfao(cena):
    p = cena.player
    asyncio.run(p._prepara_a_proxima(p._tocada))

    asyncio.run(p.stop("teste"))

    assert cena.montadas[0].limpou and p._pronta is None


def test_pronta_e_um_registro_simples():
    pronta = _Pronta(track=PROXIMA, info=INFO, pulmao=Pulmao(), saida=None, via="direto")
    assert time.monotonic() - pronta.feita_em < 1


# ── URL recém-extraída na pré-montagem (fumaça na VM, 21/09 03:20 UTC) ──────
# "toca in the end agora": extração fresca → pré-montagem → corte. O FFmpeg abriu
# a URL nos primeiros ~1s de vida dela (403), caiu no backoff dele (0s → 1s → 3s)
# e o corte veio com a fonte ainda vazia: "1º áudio só 2.8s depois do play", um
# buraco de 2805ms entre pacotes. O início normal da faixa já sondava a URL
# fresca antes de subir o FFmpeg; a pré-montagem não.


def test_pre_montagem_sonda_a_url_fresca_antes_de_abrir_o_ffmpeg(cena, monkeypatch):
    p = cena.player
    ordem = []

    async def abre(info):
        ordem.append("sondou")
        return (1.1, 4)

    async def monta(info, track, desde_s=0.0):
        ordem.append("abriu o ffmpeg")
        return Pulmao(), None, "direto"

    monkeypatch.setattr(player_mod.youtube, "espera_stream_abrir", abre)
    monkeypatch.setattr(p, "_monta", monta)

    asyncio.run(p._prepara_a_proxima(p._tocada))

    assert ordem == ["sondou", "abriu o ffmpeg"]
    assert p._pronta is not None


def test_faixa_pulada_durante_a_sonda_nao_abre_ffmpeg(cena, monkeypatch):
    p = cena.player

    async def abre(info):
        p._tocada.pulada = True
        return (1.1, 4)

    monkeypatch.setattr(player_mod.youtube, "espera_stream_abrir", abre)

    asyncio.run(p._prepara_a_proxima(p._tocada))

    assert p._pronta is None and cena.montadas == []


def test_corte_para_uma_faixa_escolhida_espera_mais_pela_fonte(cena, monkeypatch):
    """Com a música tocando enquanto espera, vale esperar a fonte encher: cortar
    com ela vazia é silêncio. O "pula" simples segue com a espera curta."""
    p = cena.player
    esperas = []

    class Lento(Pulmao):
        def encheu(self) -> bool:
            esperas.append(1)
            return len(esperas) > 100  # ~2s de voltas de 20ms

    async def monta(info, track, desde_s=0.0):
        return Lento(), None, "direto"

    monkeypatch.setattr(p, "_monta", monta)

    asyncio.run(p.prepara_o_corte(PROXIMA))

    assert len(esperas) > 100  # não desistiu aos 1,5s
