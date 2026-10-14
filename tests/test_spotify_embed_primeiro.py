"""Playlist do Spotify: o embed público vem PRIMEIRO; a API é o plano B.

Journal de 50 dias da VM: 15 importações de playlist, 15 falhas na API — 404
nas playlists do próprio Spotify (37i9…), 403 nas de usuário — e todas
resolvidas pelo embed. Desde 2025 o Spotify nega as faixas de playlist a apps
novos. Tentar a API antes era ~1,2s de espera garantida em todo /play de
playlist (18:19 de 20/09: falhou às 01,28s, o embed respondeu em 0,75s) e um
WARNING no espelho de erros a cada importação.
"""
import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from services.spotify_api import SpotifyService

FAIXAS = [{"title": "T", "artist": "A", "duration": 100, "thumbnail": ""}]


def _servico(embed, api) -> SpotifyService:
    servico = SpotifyService.__new__(SpotifyService)
    servico._scrape_playlist_embed = embed
    servico._fetch_playlist_api = api
    return servico


def test_embed_que_responde_dispensa_a_api():
    embed = AsyncMock(return_value=("Daily Mix", FAIXAS))
    api = AsyncMock(side_effect=AssertionError("a API não devia ser chamada"))

    nome, faixas = asyncio.run(_servico(embed, api).fetch("playlist", "37i9dQZF1E8O5QN8WvjaUX"))

    assert (nome, faixas) == ("Daily Mix", FAIXAS)
    api.assert_not_awaited()


def test_sucesso_pelo_embed_nao_gera_warning(caplog):
    embed = AsyncMock(return_value=("Daily Mix", FAIXAS))
    with caplog.at_level(logging.WARNING, logger="services.spotify_api"):
        asyncio.run(_servico(embed, AsyncMock()).fetch("playlist", "abc"))

    assert caplog.text == ""


def test_embed_que_falha_cai_para_a_api():
    """Playlist privada com login de usuário (spotify_login.py): só a API vê."""
    embed = AsyncMock(side_effect=LookupError("página de embed sem dados (playlist privada?)"))
    api = AsyncMock(return_value=("Minha privada", FAIXAS))

    nome, faixas = asyncio.run(_servico(embed, api).fetch("playlist", "abc"))

    assert nome == "Minha privada" and faixas == FAIXAS


def test_os_dois_falhando_sobe_o_erro_do_embed(caplog):
    """É o erro que explica ("playlist privada?"); o da API é sempre 403/404."""
    embed = AsyncMock(side_effect=LookupError("embed sem faixas (playlist privada ou vazia)"))
    api = AsyncMock(side_effect=RuntimeError("403 Forbidden"))

    with caplog.at_level(logging.WARNING, logger="services.spotify_api"):
        with pytest.raises(LookupError, match="privada ou vazia"):
            asyncio.run(_servico(embed, api).fetch("playlist", "abc"))

    assert "403 Forbidden" in caplog.text  # o da API fica no log


def test_api_que_devolve_zero_faixas_nao_conta_como_sucesso():
    embed = AsyncMock(side_effect=LookupError("embed sem faixas"))
    api = AsyncMock(return_value=("Vazia", []))

    with pytest.raises(LookupError):
        asyncio.run(_servico(embed, api).fetch("playlist", "abc"))
