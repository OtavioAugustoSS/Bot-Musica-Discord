"""Os títulos da fila no prompt do Whisper — sem nunca derrubar a transcrição.

Medido na Groq em 20/09/2026: o prompt tem teto (~224 tokens) e, passando dele, a
API não trunca — RECUSA com HTTP 400 (696 caracteres passaram, 1806 falharam). Um
400 por causa do contexto mataria TODOS os comandos de voz enquanto aquela fila
estivesse carregada; título em japonês ou coreano gasta muito mais tokens por
caractere do que um em inglês. Duas travas: o contexto tem orçamento por PESO
(core/listen.py) e, se mesmo assim vier 400, a transcrição é repetida sem contexto
e ele fica desligado por 10 minutos.
"""
import asyncio

import numpy as np
import pytest

from services import speech


class _Recusa(Exception):
    status = 400


@pytest.fixture(autouse=True)
def _groq(monkeypatch):
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "chave")
    monkeypatch.setattr(speech.config, "VOICE_TETO_S", 0.0)
    monkeypatch.setattr(speech, "_sem_contexto_ate", float("-inf"))
    speech._envios.clear()
    yield
    speech._envios.clear()


def _pcm(segundos: float = 1.0) -> bytes:
    return np.zeros(int(48000 * segundos) * 2, dtype=np.int16).tobytes()


def test_contexto_vai_para_a_groq(monkeypatch):
    visto = []

    async def groq(audio, contexto=""):
        visto.append(contexto)
        return "arrodes toca pyramids"

    monkeypatch.setattr(speech, "_transcribe_groq", groq)

    assert asyncio.run(speech.transcribe(_pcm(), "Músicas: Pyramids, Frank Ocean.")) == "arrodes toca pyramids"
    assert visto == ["Músicas: Pyramids, Frank Ocean."]


def test_prompt_recusado_repete_sem_contexto_e_o_desliga_por_10min(monkeypatch, caplog):
    chamadas = []
    relogio = [1000.0]
    monkeypatch.setattr(speech, "_agora", lambda: relogio[0])

    async def groq(audio, contexto=""):
        chamadas.append(contexto)
        if contexto:
            raise _Recusa("400, message='Bad Request'")
        return "arrodes pula"

    monkeypatch.setattr(speech, "_transcribe_groq", groq)

    assert asyncio.run(speech.transcribe(_pcm(), "títulos demais")) == "arrodes pula"  # o comando NÃO se perdeu
    assert chamadas == ["títulos demais", ""]
    assert "400" in caplog.text

    relogio[0] += 60
    asyncio.run(speech.transcribe(_pcm(), "títulos demais"))
    assert chamadas[2:] == [""]  # de castigo: nem tenta

    relogio[0] += 600
    asyncio.run(speech.transcribe(_pcm(), "fila nova"))
    assert chamadas[3] == "fila nova"  # passou: tenta de novo


def test_outro_erro_com_contexto_nao_e_culpa_do_contexto(monkeypatch):
    monkeypatch.setattr(speech, "_local_lento", True)

    class Fora(Exception):
        status = 503

    async def groq(audio, contexto=""):
        raise Fora("503")

    monkeypatch.setattr(speech, "_transcribe_groq", groq)

    assert asyncio.run(speech.transcribe(_pcm(), "Pyramids")) == ""
    assert speech._sem_contexto_ate == float("-inf")


def test_contexto_tem_orcamento_por_peso_e_nao_por_caractere():
    """Não-ASCII pesa 4: 40 títulos em japonês não podem custar o que custam 40 em inglês."""
    from unittest.mock import MagicMock

    from core import listen
    from core.queue import MusicQueue, Track

    def contexto_de(titulos):
        player = MagicMock()
        player.queue = MusicQueue()
        player.queue.add_many([Track(title=t, artist="") for t in titulos])
        return listen.VoiceListener(player, MagicMock())._contexto_da_fila()

    ingles = contexto_de([f"Song Number {i}" for i in range(40)])
    japones = contexto_de(["夜に駆ける" * 3 for _ in range(40)])

    assert len(ingles) <= 260
    assert len(japones) <= 80
    assert contexto_de([]) == ""
