"""Metadados rápidos (oEmbed) para responder ao /play <link> sem esperar a extração.

A resposta "➕ na fila!" de um link só saía depois da extração completa
(~8,3s na VM — e atrás de qualquer prefetch em andamento no pool
serializado, até ~16s). Vários links seguidos viravam respostas a 8, 16,
24s: "bugado ao receber vários comandos". O oEmbed do YouTube devolve
título/canal/capa em ~0,3s (medido do dev e da VM em 12/09), sem desafio
JS; a extração continua em background e aquece o cache para a hora de tocar.
Sem duração — ela é preenchida quando a faixa é adiantada ou toca.
"""
import asyncio

import pytest

from services import http, youtube


class _Resposta:
    def __init__(self, status: int, payload: dict | None = None, boom: Exception | None = None):
        self.status = status
        self._payload = payload or {}
        self._boom = boom

    async def __aenter__(self):
        if self._boom:
            raise self._boom
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, **kwargs):
        return self._payload


class _Sessao:
    def __init__(self, resposta: _Resposta):
        self.resposta = resposta
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.resposta


@pytest.fixture
def sessao(monkeypatch):
    def instala(resposta: _Resposta) -> _Sessao:
        s = _Sessao(resposta)
        monkeypatch.setattr(http, "get_session", lambda: s)
        return s

    return instala


def test_oembed_vira_o_mesmo_formato_da_extracao(sessao):
    s = sessao(_Resposta(200, {"title": "Me at the zoo", "author_name": "jawed",
                               "thumbnail_url": "http://i/hq.jpg"}))

    info = asyncio.run(youtube.quick_info("jNQXAC9IVRw"))

    assert info == {"video_id": "jNQXAC9IVRw", "title": "Me at the zoo", "artist": "jawed",
                    "duration": 0, "thumbnail": "http://i/hq.jpg"}
    assert "jNQXAC9IVRw" in s.urls[0] and "oembed" in s.urls[0]


def test_video_privado_ou_inexistente_devolve_none(sessao):
    """4xx do oEmbed: deixa a extração completa dizer o erro certo."""
    sessao(_Resposta(401))
    assert asyncio.run(youtube.quick_info("privado00000")) is None


def test_rede_fora_devolve_none_sem_levantar(sessao):
    sessao(_Resposta(200, boom=OSError("sem rede")))
    assert asyncio.run(youtube.quick_info("jNQXAC9IVRw")) is None


def test_resposta_sem_titulo_devolve_none(sessao):
    sessao(_Resposta(200, {"author_name": "x"}))
    assert asyncio.run(youtube.quick_info("jNQXAC9IVRw")) is None
