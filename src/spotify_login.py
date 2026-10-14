"""Autorização única do Spotify — libera a importação de playlists.

Rode da raiz do projeto: python src/spotify_login.py
Abre o navegador para você clicar em "Concordo"; o token fica salvo em
data/spotify-token.json (gitignored) e renova sozinho para sempre.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import spotipy  # noqa: E402
from spotipy.cache_handler import CacheFileHandler  # noqa: E402
from spotipy.oauth2 import SpotifyOAuth  # noqa: E402

import config  # noqa: E402
from services.spotify_api import OAUTH_REDIRECT_URI, OAUTH_SCOPES  # noqa: E402


def main() -> None:
    if not config.spotify_enabled():
        sys.exit("❌ Preencha SPOTIFY_CLIENT_ID/SECRET no .env primeiro.")
    os.makedirs(os.path.dirname(config.SPOTIFY_TOKEN_CACHE) or ".", exist_ok=True)
    auth = SpotifyOAuth(
        client_id=config.SPOTIFY_CLIENT_ID,
        client_secret=config.SPOTIFY_CLIENT_SECRET,
        redirect_uri=OAUTH_REDIRECT_URI,
        scope=OAUTH_SCOPES,
        cache_handler=CacheFileHandler(config.SPOTIFY_TOKEN_CACHE),
        open_browser=True,
    )
    user = spotipy.Spotify(auth_manager=auth).current_user()
    print(f"✅ Autorizado como {user.get('display_name') or user['id']}!")
    print("   Reinicie o bot e as playlists estarão liberadas. 🎉")


if __name__ == "__main__":
    main()
