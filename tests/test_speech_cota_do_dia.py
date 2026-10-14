"""Cota DIÁRIA da Groq: quando o turbo acaba, o large-v3 assume; sem nenhum, nem tenta.

Journal de 26–27/09/2026: a conversa da call gastou as 2000 requisições do dia do
whisper-large-v3-turbo em ~4h (3.031 envios, 120 comandos). Às 01:10 a Groq passou
a responder 429 com `x-ratelimit-remaining-requests=0` e `retry-after=44` — o balde
do dia enche 2000/24h, uma requisição a cada 43s — e a escuta ficou surda até a
pessoa mandar o bot sair: 86 "comando perdido" em 22 minutos, cada um um 429.

Medido na VM em 27/09: a cota é POR MODELO. Com o turbo em 644 restantes, o
whisper-large-v3 respondeu com 1999 restantes de 2000. Com os dois, o dia dobra.
"""
import asyncio

import numpy as np
import pytest

from services import speech

TURBO, LARGE = speech._MODELOS


class _Resposta:
    def __init__(self, status: int, restantes: int | None = None, retry_after: str | None = None,
                 texto: str = "arrodes pula") -> None:
        self.status = status
        self.headers = {}
        if restantes is not None:
            self.headers["x-ratelimit-remaining-requests"] = str(restantes)
            self.headers["x-ratelimit-limit-requests"] = "2000"
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after
        self._texto = texto

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return {"text": self._texto}


def _modelo_do_form(form) -> str:
    for opcoes, _cabecalhos, valor in form._fields:
        if opcoes.get("name") == "model":
            return valor
    raise AssertionError("pedido sem modelo")


class _Sessao:
    def __init__(self, respostas: list) -> None:
        self.respostas = list(respostas)
        self.modelos: list[str] = []

    def post(self, *args, data, **kwargs):
        self.modelos.append(_modelo_do_form(data))
        return self.respostas.pop(0)


@pytest.fixture
def groq(monkeypatch):
    monkeypatch.setattr(speech.config, "GROQ_API_KEY", "chave-de-teste")
    monkeypatch.setattr(speech.config, "GROQ_STT_RPM", 20)
    monkeypatch.setattr(speech.config, "VOICE_TETO_S", 6.0)
    monkeypatch.setattr(speech.config, "VOICE_WHISPER_LOCAL", False)
    monkeypatch.setattr(speech.config, "VOICE_STT_PROMPT", "")
    speech._envios.clear()
    speech._cota.clear()
    speech.leu_estatisticas()
    relogio = [1000.0]
    monkeypatch.setattr(speech, "_agora", lambda: relogio[0])

    async def dorme(segundos: float) -> None:
        relogio[0] += segundos

    monkeypatch.setattr(speech.asyncio, "sleep", dorme)

    def arma(respostas: list) -> _Sessao:
        sessao = _Sessao(respostas)
        monkeypatch.setattr(speech.http, "get_session", lambda: sessao)
        return sessao

    arma.relogio = relogio
    yield arma
    speech._envios.clear()
    speech._cota.clear()
    speech.leu_estatisticas()


def _pcm(segundos: float) -> bytes:
    return np.full(int(48000 * segundos) * 2, 1000, dtype=np.int16).tobytes()


def test_turbo_sem_cota_do_dia_o_mesmo_trecho_vai_para_o_large(groq):
    sessao = groq([_Resposta(429, restantes=0, retry_after="44"), _Resposta(200, restantes=1999)])

    assert asyncio.run(speech.transcribe(_pcm(1.2))) == "arrodes pula"
    assert sessao.modelos == [TURBO, LARGE], "sem esperar 44s nem perder o comando"


def test_depois_de_esgotar_o_turbo_nem_tenta_nele(groq):
    sessao = groq([
        _Resposta(429, restantes=0, retry_after="44"), _Resposta(200, restantes=1999),
        _Resposta(200, restantes=1998),
    ])

    asyncio.run(speech.transcribe(_pcm(1.2)))
    asyncio.run(speech.transcribe(_pcm(1.2)))
    assert sessao.modelos == [TURBO, LARGE, LARGE]


def test_turbo_quase_no_fim_passa_para_o_large_antes_do_429(groq):
    sessao = groq([_Resposta(200, restantes=3), _Resposta(200, restantes=1999)])

    asyncio.run(speech.transcribe(_pcm(1.2)))
    asyncio.run(speech.transcribe(_pcm(1.2)))
    assert sessao.modelos == [TURBO, LARGE]


def test_o_turbo_volta_quando_o_balde_enche_de_novo(groq):
    """2000 por dia = uma requisição a cada 43,2s: 1h depois são ~83 de novo."""
    sessao = groq([_Resposta(200, restantes=0), _Resposta(200, restantes=82)])

    asyncio.run(speech.transcribe(_pcm(1.2)))
    groq.relogio[0] += 3600
    asyncio.run(speech.transcribe(_pcm(1.2)))
    assert sessao.modelos == [TURBO, TURBO]


def test_sem_cota_em_nenhum_nem_envia(groq, caplog):
    sessao = groq([_Resposta(429, restantes=0, retry_after="44"), _Resposta(429, restantes=0, retry_after="44")])

    assert asyncio.run(speech.transcribe(_pcm(1.2))) == ""
    assert sessao.modelos == [TURBO, LARGE]
    caplog.clear()
    for _ in range(5):  # a call segue falando
        assert asyncio.run(speech.transcribe(_pcm(1.2))) == ""
    assert sessao.modelos == [TURBO, LARGE], "antes eram 86 requisições 429 em 22min"
    avisos = [r for r in caplog.records if "cota do dia" in r.getMessage()]
    assert len(avisos) <= 1, "um aviso, não um por frase"
    assert speech.leu_estatisticas()["perdidos"] == 6

