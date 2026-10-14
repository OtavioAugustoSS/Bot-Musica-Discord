"""Extrair não é tocar: a sonda do boot agora abre a URL do stream.

20/09/2026: a sonda do boot dizia "extração de áudio ok" e o /diag também —
enquanto o googlevideo respondia 403 à URL extraída e 4 de 6 faixas não
tocavam. O journal passa a dizer, a cada subida, se o áudio ABRE.
"""
import asyncio

from services import youtube


class _Resposta:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _Sessao:
    def __init__(self, respostas: list) -> None:
        self._respostas = list(respostas)
        self.pedidos: list[dict] = []

    def get(self, url, **kwargs):
        self.pedidos.append({"url": url, **kwargs})
        proxima = self._respostas.pop(0)
        if isinstance(proxima, Exception):
            raise proxima
        return _Resposta(proxima)


def _com_sessao(monkeypatch, respostas: list) -> _Sessao:
    from services import http

    sessao = _Sessao(respostas)
    monkeypatch.setattr(http, "get_session", lambda: sessao)
    monkeypatch.setattr(youtube.asyncio, "sleep", _sem_esperar)
    return sessao


async def _sem_esperar(_segundos: float) -> None:
    return None


def test_url_que_abre_de_primeira_faz_um_pedido_so(monkeypatch):
    sessao = _com_sessao(monkeypatch, [206])

    assert asyncio.run(youtube.sonda_do_stream("http://x")) == [206]
    assert len(sessao.pedidos) == 1
    assert sessao.pedidos[0]["headers"]["Range"] == "bytes=0-1023"  # 1 KB, não a faixa


def test_recusa_na_primeira_e_aceite_na_segunda_e_o_padrao_de_20_09(monkeypatch):
    _com_sessao(monkeypatch, [403, 206])

    assert asyncio.run(youtube.sonda_do_stream("http://x")) == [403, 206]


def test_recusa_em_todas_as_tentativas(monkeypatch):
    sessao = _com_sessao(monkeypatch, [403, 403, 403])

    assert asyncio.run(youtube.sonda_do_stream("http://x")) == [403, 403, 403]
    assert len(sessao.pedidos) == 3


def test_erro_de_rede_vira_o_nome_do_erro_e_nao_excecao(monkeypatch):
    _com_sessao(monkeypatch, [TimeoutError(), 200])

    assert asyncio.run(youtube.sonda_do_stream("http://x")) == ["TimeoutError", 200]


def test_abriu():
    assert youtube.stream_abriu([403, 206]) is True
    assert youtube.stream_abriu([200]) is True
    assert youtube.stream_abriu([403, 403, 403]) is False
    assert youtube.stream_abriu(["TimeoutError"]) is False
