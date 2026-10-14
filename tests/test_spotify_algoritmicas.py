"""Playlists algorítmicas do Spotify merecem a mensagem certa.

No journal de produção, 12 erros do spotipy — a maioria em IDs `37i9dQZF1E8…`
(Daily Mix, rádio, mixes personalizados). Essas são geradas por usuário e a
API pública NUNCA as entrega, esteja a playlist pública ou não. O bot
respondia "se ela for privada, deixa pública que eu consigo" — conselho que
não resolve e faz a pessoa tentar de novo à toa.
"""
import pytest

from services.spotify_api import is_algorithmic_playlist


@pytest.mark.parametrize("spotify_id", [
    "37i9dQZF1E8zLMgLirolMz",  # os três que falharam em produção
    "37i9dQZF1E8O25XWktDdR4",
    "37i9dQZF1E8LqOmNxYgnYP",
    "37i9dQZF1E4somethingelse",
])
def test_reconhece_as_personalizadas(spotify_id):
    assert is_algorithmic_playlist(spotify_id)


@pytest.mark.parametrize("spotify_id", [
    "37i9dQZF1DXcBWIGoYBM5M",  # editorial (Today's Top Hits) — essa a API entrega
    "7fwIHqGdaEiSwTVJQPCalI",  # playlist comum de usuário
    "",
    "37i9dQZF1D",              # prefixo curto demais
])
def test_nao_marca_as_que_a_api_entrega(spotify_id):
    assert not is_algorithmic_playlist(spotify_id)


def test_mensagem_especifica_existe_e_nao_manda_deixar_publica():
    import messages as m

    assert "SPOTIFY_ALGORITMICA" in dir(m)
    texto = m.SPOTIFY_ALGORITMICA
    assert "pública" not in texto.lower()  # o conselho errado não pode voltar
    assert len(texto) > 40
