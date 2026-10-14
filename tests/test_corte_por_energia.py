"""O trecho de fala fecha quando a PESSOA cala, não só quando o Discord para de mandar pacotes.

Journal de 03–04/10/2026 (deploy 47aed07): o corte do trecho dependia do
cliente do Discord parar de transmitir no silêncio. Quem está com microfone
aberto (ruído de fundo, música baixa) nunca "cala" para o Discord: tudo vira
trecho de 12s, descartado pelo teto de 6s sem ir para a Groq. Um usuário teve
35% dos trechos (176 de 501) jogados fora assim; quem mais comanda o bot teve
16%, e em 04/10 repetiu três comandos 8–15s depois de um trecho longo sumir.

Dois cortes novos, os dois com a mesma régua de energia do gate de fala
(VOICE_MIN_RMS por janela de 20ms):
  - no sink: VOICE_GAP_S sem nenhum pacote ACIMA do limiar fecha o trecho,
    mesmo com pacotes mudos chegando;
  - no worker: um trecho que mesmo assim passa do teto é cortado na última
    pausa interna (≥ 0,3s) que cabe no teto — a 1ª frase vai, o resto não.
"""
import asyncio
import logging
import time

import numpy as np
import pytest

import config
from core import listen
from core.listen import CommandSink

from test_voice_continuacao import _listener, _roda_worker

JANELA = 1920  # amostras int16 por 20ms estéreo a 48 kHz (um pacote do Discord)
ALTO = 2000  # RMS bem acima de VOICE_MIN_RMS (250)
BAIXO = 50  # ruído de microfone aberto: abaixo do limiar, mas o Discord segue mandando


def _pacote(amplitude: int) -> bytes:
    return np.full(JANELA, amplitude, dtype=np.int16).tobytes()


def _janelas(*amplitudes: int) -> bytes:
    return b"".join(_pacote(a) for a in amplitudes)


def _segundos(n: float, amplitude: int) -> bytes:
    return _janelas(*([amplitude] * int(round(n / 0.02))))


class FakePacket:
    ssrc = 7


class FakeData:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm
        self.packet = FakePacket()


class FakeUser:
    id = 42


@pytest.fixture
def relogio(monkeypatch):
    agora = {"t": 1000.0}
    monkeypatch.setattr(listen.time, "monotonic", lambda: agora["t"])
    return agora


def _manda(sink: CommandSink, relogio, segundos: float, amplitude: int) -> None:
    for _ in range(int(round(segundos / 0.02))):
        relogio["t"] += 0.02
        sink.write(FakeData(_pacote(amplitude)), FakeUser())


# ── sink: cala-se por energia ────────────────────────────────────────────────


def test_fala_seguida_de_ruido_baixo_fecha_o_trecho(relogio, monkeypatch):
    monkeypatch.setattr(config, "VOICE_GAP_S", 0.8)
    sink = CommandSink()

    _manda(sink, relogio, 0.5, ALTO)  # "Arrodes, próxima"
    _manda(sink, relogio, 2.0, BAIXO)  # mic aberto: pacotes mudos continuam chegando

    listen.leu_estatisticas()
    trechos = sink.harvest()
    assert len(trechos) == 1
    user_id, pcm, _ = trechos[0]
    assert user_id == 42
    assert abs(len(pcm) / listen.BYTES_PER_SECOND - 2.5) < 0.05
    assert listen.leu_estatisticas()["cortes_por_energia"] == 1


def test_fala_curta_nao_e_partida_pelo_corte_por_energia(relogio, monkeypatch):
    """06/10: com o corte por energia valendo para qualquer trecho, 52 de ~40
    transcrições em 5min fecharam por ele — o fim baixo da frase ("…limpa a
    fila" dito mais baixo) virava outro trecho, sem o nome ou descartado como
    ruído, calado. Os descartes por ruído subiram de 8,3 para 14,3 a cada 100
    transcrições. O corte existe para o microfone aberto que nunca cala, cujo
    trecho cresce: fala curta fecha como antes, quando o Discord para."""
    monkeypatch.setattr(config, "VOICE_GAP_S", 0.8)
    sink = CommandSink()

    colhidos = []
    for segundos, amplitude in ((0.5, ALTO), (0.9, BAIXO), (0.4, ALTO)):  # "Arrodes," … "limpa a fila"
        for _ in range(int(round(segundos / 0.02))):
            relogio["t"] += 0.02
            sink.write(FakeData(_pacote(amplitude)), FakeUser())
            colhidos += sink.harvest()  # o ticker colhe o tempo todo

    assert colhidos == []


def test_microfone_aberto_fecha_pela_energia_no_trecho_comprido(relogio, monkeypatch):
    monkeypatch.setattr(config, "VOICE_GAP_S", 0.8)
    sink = CommandSink()
    colhidos = []
    for segundos, amplitude in ((2.0, ALTO), (1.5, BAIXO)):
        for _ in range(int(round(segundos / 0.02))):
            relogio["t"] += 0.02
            sink.write(FakeData(_pacote(amplitude)), FakeUser())
            colhidos += sink.harvest()

    assert len(colhidos) == 1
    assert len(colhidos[0][1]) / listen.BYTES_PER_SECOND < 3.0


def test_quem_ainda_fala_nao_e_cortado(relogio, monkeypatch):
    monkeypatch.setattr(config, "VOICE_GAP_S", 0.8)
    sink = CommandSink()

    _manda(sink, relogio, 1.5, ALTO)

    assert sink.harvest() == []


def test_so_ruido_baixo_nao_vira_trecho_antes_do_maximo(relogio, monkeypatch):
    monkeypatch.setattr(config, "VOICE_GAP_S", 0.8)
    sink = CommandSink()

    _manda(sink, relogio, 3.0, BAIXO)

    assert sink.harvest() == []


def test_pausa_curta_no_meio_da_frase_nao_corta(relogio, monkeypatch):
    monkeypatch.setattr(config, "VOICE_GAP_S", 0.8)
    sink = CommandSink()

    _manda(sink, relogio, 0.6, ALTO)
    _manda(sink, relogio, 0.5, BAIXO)  # respirou
    _manda(sink, relogio, 0.6, ALTO)

    assert sink.harvest() == []


def test_o_corte_por_pacotes_ausentes_continua_valendo(relogio, monkeypatch):
    monkeypatch.setattr(config, "VOICE_GAP_S", 0.8)
    sink = CommandSink()

    _manda(sink, relogio, 0.5, ALTO)
    relogio["t"] += 0.9  # o Discord parou de mandar

    listen.leu_estatisticas()
    assert len(sink.harvest()) == 1
    assert listen.leu_estatisticas()["cortes_por_energia"] == 0  # esse corte é o de sempre


# ── worker: a 1ª frase de um trecho longo vai para a Groq ───────────────────


def test_trecho_acima_do_teto_e_cortado_na_ultima_pausa_que_cabe():
    pcm = _segundos(2.0, ALTO) + _segundos(0.4, BAIXO) + _segundos(5.6, ALTO)

    cortado = listen.corta_na_pausa(pcm, 250.0, teto_s=6.0)

    assert len(cortado) == int(round(2.0 / 0.02)) * JANELA * 2


def test_com_varias_pausas_fica_com_a_ultima_antes_do_teto():
    pcm = (_segundos(1.0, ALTO) + _segundos(0.4, BAIXO) + _segundos(2.0, ALTO)
           + _segundos(0.4, BAIXO) + _segundos(5.0, ALTO))

    cortado = listen.corta_na_pausa(pcm, 250.0, teto_s=6.0)

    assert len(cortado) == int(round(3.4 / 0.02)) * JANELA * 2


def test_sem_pausa_nenhuma_o_trecho_volta_inteiro():
    pcm = _segundos(8.0, ALTO)

    assert listen.corta_na_pausa(pcm, 250.0, teto_s=6.0) == pcm


def test_pausa_so_depois_do_teto_nao_ajuda():
    pcm = _segundos(7.0, ALTO) + _segundos(0.5, BAIXO) + _segundos(1.0, ALTO)

    assert listen.corta_na_pausa(pcm, 250.0, teto_s=6.0) == pcm


def test_trecho_que_cabe_no_teto_nao_e_tocado():
    pcm = _segundos(2.0, ALTO) + _segundos(0.4, BAIXO) + _segundos(2.0, ALTO)

    assert listen.corta_na_pausa(pcm, 250.0, teto_s=6.0) == pcm


def test_respiro_curto_nao_e_pausa():
    pcm = _segundos(2.0, ALTO) + _segundos(0.2, BAIXO) + _segundos(5.8, ALTO)

    assert listen.corta_na_pausa(pcm, 250.0, teto_s=6.0) == pcm


def test_worker_manda_so_a_primeira_frase_e_avisa_no_log(monkeypatch, caplog):
    monkeypatch.setattr(config, "VOICE_TETO_S", 6.0)
    monkeypatch.setattr(config, "VOICE_MIN_RMS", 250.0)
    enviados = []

    async def transcreve(pcm, contexto=""):
        enviados.append(pcm)
        return "Arrodes, próxima."

    async def executa(user_id, heard, action, arg, **kw):
        pass

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    l = _listener()
    l._contexto_da_fila = lambda: ""
    l._execute = executa
    pcm = _segundos(2.0, ALTO) + _segundos(0.4, BAIXO) + _segundos(5.6, ALTO)

    listen.leu_estatisticas()
    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, pcm, time.monotonic())]))

    assert len(enviados) == 1
    assert len(enviados[0]) / listen.BYTES_PER_SECOND <= 6.0
    assert "cortado" in caplog.text
    assert listen.leu_estatisticas()["cortes_na_pausa"] == 1
