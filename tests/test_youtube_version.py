"""Aviso de yt-dlp desatualizado.

O apagão de agosto/2026 foi, no fundo, uma versão pinada que envelheceu sem
ninguém perceber: o requirements.txt segurava 2026.7.4 desde julho e o YouTube
quebrou os clients dela em agosto. O bot agora avisa quando ficou para trás —
sem atualizar sozinho (bump é ato deliberado, ver requirements.txt).
"""
import asyncio

import pytest

from services import youtube


@pytest.fixture(autouse=True)
def _reset():
    youtube._release_cache = None
    yield
    youtube._release_cache = None


def _fake_fetch(tag, chamadas=None):
    async def fetch():
        if chamadas is not None:
            chamadas.append(1)
        return tag

    return fetch


def test_versao_nova_disponivel_e_reportada(monkeypatch):
    monkeypatch.setattr(youtube, "installed_version", lambda: "2026.07.04")
    monkeypatch.setattr(youtube, "_fetch_latest_release", _fake_fetch("2026.08.19"))

    assert asyncio.run(youtube.outdated_release()) == "2026.08.19"


def test_na_ultima_versao_nao_reporta_nada(monkeypatch):
    monkeypatch.setattr(youtube, "installed_version", lambda: "2026.08.19")
    monkeypatch.setattr(youtube, "_fetch_latest_release", _fake_fetch("2026.08.19"))

    assert asyncio.run(youtube.outdated_release()) is None


def test_versao_mais_nova_que_a_publicada_nao_reporta(monkeypatch):
    """Canal nightly/master instalado: estar à frente não é estar atrasado."""
    monkeypatch.setattr(youtube, "installed_version", lambda: "2026.09.01.123456")
    monkeypatch.setattr(youtube, "_fetch_latest_release", _fake_fetch("2026.08.19"))

    assert asyncio.run(youtube.outdated_release()) is None


def test_resultado_fica_em_cache(monkeypatch):
    chamadas = []
    monkeypatch.setattr(youtube, "installed_version", lambda: "2026.07.04")
    monkeypatch.setattr(youtube, "_fetch_latest_release", _fake_fetch("2026.08.19", chamadas))

    async def duas_vezes():
        await youtube.outdated_release()
        await youtube.outdated_release()

    asyncio.run(duas_vezes())
    assert len(chamadas) == 1  # a segunda veio do cache


def test_cache_expira(monkeypatch):
    chamadas = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])
    monkeypatch.setattr(youtube, "installed_version", lambda: "2026.07.04")
    monkeypatch.setattr(youtube, "_fetch_latest_release", _fake_fetch("2026.08.19", chamadas))

    asyncio.run(youtube.outdated_release())
    clock["t"] += youtube._RELEASE_TTL_S + 1
    asyncio.run(youtube.outdated_release())

    assert len(chamadas) == 2


def test_github_fora_do_ar_nao_derruba_nada(monkeypatch):
    async def boom():
        raise RuntimeError("sem rede")

    monkeypatch.setattr(youtube, "installed_version", lambda: "2026.07.04")
    monkeypatch.setattr(youtube, "_fetch_latest_release", boom)

    assert asyncio.run(youtube.outdated_release()) is None


def test_tag_lixo_e_ignorada(monkeypatch):
    monkeypatch.setattr(youtube, "installed_version", lambda: "2026.07.04")
    monkeypatch.setattr(youtube, "_fetch_latest_release", _fake_fetch("nightly"))

    assert asyncio.run(youtube.outdated_release()) is None


@pytest.mark.parametrize(
    "instalada,publicada,atrasado",
    [
        ("2026.07.04", "2026.08.19", True),
        ("2026.8.19", "2026.08.19", False),  # pip normaliza o zero à esquerda
        ("2026.08.19", "2026.08.19.010203", True),  # nightly do mesmo dia é mais nova
        ("2026.12.01", "2026.9.30", False),  # 12 > 9: comparação numérica, não textual
    ],
)
def test_comparacao_de_versao(instalada, publicada, atrasado):
    assert youtube._is_outdated(instalada, publicada) is atrasado
