"""Extração em voo é compartilhada, não duplicada.

Com o pool serializado (1 extração por vez numa VM de 2 vCPU), pedir a MESMA
faixa que o prefetch já está extraindo custava o dobro: o foreground entrava
na fila atrás do prefetch e refazia o mesmo trabalho de 8s. O caso real é
`/skip` logo depois de uma faixa começar.
"""
import asyncio

import pytest

from services import youtube


@pytest.fixture(autouse=True)
def _limpa():
    youtube._stream_cache.clear()
    youtube._inflight.clear()
    yield
    youtube._stream_cache.clear()
    youtube._inflight.clear()


def _info(video_id: str) -> dict:
    return {
        "id": video_id, "title": "T", "uploader": "A", "duration": 10,
        "thumbnail": "", "url": f"https://stream/{video_id}",
    }


def test_dois_pedidos_da_mesma_faixa_extraem_uma_vez_so(monkeypatch):
    chamadas = []
    liberar = asyncio.Event()

    def lento(url: str, **kwargs) -> dict:
        chamadas.append(url)
        # segura ate os dois pedidos estarem em voo
        while not liberar.is_set():
            pass
        return _info("vid1")

    monkeypatch.setattr(youtube, "_extract", lento)

    async def run():
        a = asyncio.create_task(youtube.get_stream("vid1", use_cache=False))
        b = asyncio.create_task(youtube.get_stream("vid1", use_cache=False))
        await asyncio.sleep(0)  # deixa as duas registrarem o pedido
        liberar.set()
        return await asyncio.gather(a, b)

    primeiro, segundo = asyncio.run(run())

    assert len(chamadas) == 1, chamadas
    assert primeiro == segundo


def test_faixas_diferentes_nao_se_atrapalham(monkeypatch):
    chamadas = []

    def extrai(url: str, **kwargs) -> dict:
        chamadas.append(url)
        return _info(url.rsplit("=", 1)[-1])

    monkeypatch.setattr(youtube, "_extract", extrai)

    async def run():
        return await asyncio.gather(
            youtube.get_stream("vid1", use_cache=False),
            youtube.get_stream("vid2", use_cache=False),
        )

    a, b = asyncio.run(run())
    assert len(chamadas) == 2
    assert a["video_id"] == "vid1" and b["video_id"] == "vid2"


def test_falha_propaga_para_todos_os_que_esperam(monkeypatch):
    def explode(url: str, **kwargs) -> dict:
        raise RuntimeError("YouTube fora")

    monkeypatch.setattr(youtube, "_extract", explode)

    async def run():
        return await asyncio.gather(
            youtube.get_stream("vid1", use_cache=False),
            youtube.get_stream("vid1", use_cache=False),
            return_exceptions=True,
        )

    a, b = asyncio.run(run())
    assert isinstance(a, RuntimeError) and isinstance(b, RuntimeError)


def test_registro_de_voo_fica_limpo_depois(monkeypatch):
    """Vazar entradas aqui seria pior que o bug: faixa nunca mais extraída."""
    monkeypatch.setattr(youtube, "_extract", lambda url, **kwargs: _info("vid1"))
    asyncio.run(youtube.get_stream("vid1", use_cache=False))
    assert youtube._inflight == {}

    def explode(url, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(youtube, "_extract", explode)
    with pytest.raises(RuntimeError):
        asyncio.run(youtube.get_stream("vid2", use_cache=False))
    assert youtube._inflight == {}


def test_cache_continua_tendo_prioridade(monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        youtube, "_extract", lambda url, **kwargs: chamadas.append(url) or _info("vid1")
    )
    asyncio.run(youtube.get_stream("vid1"))
    asyncio.run(youtube.get_stream("vid1"))
    assert len(chamadas) == 1
