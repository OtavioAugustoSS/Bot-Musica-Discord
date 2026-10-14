"""O primeiro /play do dia não paga a extração fria.

Journal de 25/09 a 06/10: depois de mais de 20h parado, a 1ª extração levou
11–18s (normal: ~4s). Em 06/10 a 1ª música levou 13s para tocar e, no meio
dela, "cache de players pré-processados: 1 antigo(s) apagado(s)": o YouTube
trocou o player JS e quem pediu a música pagou o processamento do novo.
Ocioso, o bot extrai a sonda de tempos em tempos e chega pronto.
"""
import asyncio
from types import SimpleNamespace

import main
from services import youtube


def _bot(em_call: bool):
    return SimpleNamespace(voice_clients=[object()] if em_call else [])


def test_ocioso_extrai_a_sonda(monkeypatch):
    pedidos = []

    async def extrai(video, **kw):
        pedidos.append((video, kw.get("use_cache")))
        return {}

    monkeypatch.setattr(youtube, "get_stream", extrai)

    assert asyncio.run(main._aquece(_bot(em_call=False))) is not None
    assert pedidos == [(youtube.PROBE_VIDEO_ID, False)]


def test_em_call_nao_disputa_a_cpu_com_a_musica(monkeypatch):
    async def extrai(video, **kw):
        raise AssertionError("não deveria extrair com gente ouvindo")

    monkeypatch.setattr(youtube, "get_stream", extrai)

    assert asyncio.run(main._aquece(_bot(em_call=True))) is None


def test_falha_no_aquecimento_nao_derruba_o_laco(monkeypatch):
    async def extrai(video, **kw):
        raise RuntimeError("bot-check")

    monkeypatch.setattr(youtube, "get_stream", extrai)

    assert asyncio.run(main._aquece(_bot(em_call=False))) is None
