"""Metadados do Spotify via Client Credentials (sem login de usuário).

O Spotify não fornece o áudio (DRM) — daqui saem só título/artista/capa,
e o áudio equivalente é resolvido no YouTube na hora de tocar.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re

import spotipy
from spotipy.cache_handler import CacheFileHandler, MemoryCacheHandler
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

log = logging.getLogger(__name__)

SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?(track|album|playlist)/([A-Za-z0-9]+)"
)

# Playlists exigem token de USUÁRIO desde 2025 (client credentials → 401/403);
# faixas e álbuns funcionam com client credentials normalmente.
OAUTH_SCOPES = "playlist-read-private playlist-read-collaborative"
OAUTH_REDIRECT_URI = "http://127.0.0.1:8888/callback"


def make_auth_manager(client_id: str, client_secret: str, token_cache: str = ""):
    """Auth de usuário se houver token salvo (spotify_login.py); senão app-only."""
    if token_cache and os.path.exists(token_cache):
        return SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=OAUTH_REDIRECT_URI,
            scope=OAUTH_SCOPES,
            cache_handler=CacheFileHandler(token_cache),
            open_browser=False,
        )
    # cache em memória: o token client-credentials vale 1h e custa 1 request
    # para renovar — o default do spotipy gravava `.cache` no CWD (e esse
    # arquivo já vazou token em commit)
    return SpotifyClientCredentials(
        client_id=client_id, client_secret=client_secret, cache_handler=MemoryCacheHandler()
    )


# Playlists geradas PARA um usuário (Daily Mix, rádio, Discover Weekly).
# A API pública devolve 404 nelas independentemente de estarem públicas —
# não adianta pedir para a pessoa mudar a visibilidade. As editoriais
# (37i9dQZF1DX…, "Today's Top Hits" e afins) funcionam normalmente.
_PREFIXO_ALGORITMICA = "37i9dQZF1E"


def is_algorithmic_playlist(spotify_id: str) -> bool:
    """A playlist é personalizada do Spotify (a API nunca a entrega)?"""
    return bool(spotify_id) and spotify_id.startswith(_PREFIXO_ALGORITMICA)


def parse_spotify_url(text: str) -> tuple[str, str] | None:
    """Retorna (tipo, id) para links do Spotify, ou None se não for um."""
    match = SPOTIFY_URL_RE.search(text)
    return (match.group(1), match.group(2)) if match else None


class SpotifyService:
    def __init__(self, client_id: str, client_secret: str, token_cache: str = "") -> None:
        self._sp = spotipy.Spotify(
            auth_manager=make_auth_manager(client_id, client_secret, token_cache)
        )
        self.has_user_auth = token_cache != "" and os.path.exists(token_cache)

    async def _run(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

    @staticmethod
    def _track_meta(track: dict | None) -> dict | None:
        if not track or track.get("is_local") or not track.get("name"):
            return None
        if track.get("type") not in (None, "track"):  # ignora episódios de podcast
            return None
        album = track.get("album") or {}
        images = album.get("images") or []
        return {
            "title": track["name"],
            "artist": ", ".join(a["name"] for a in track.get("artists", [])),
            "duration": int((track.get("duration_ms") or 0) / 1000),
            "thumbnail": images[0]["url"] if images else "",
        }

    async def fetch(self, kind: str, spotify_id: str) -> tuple[str, list[dict]]:
        """Retorna (nome da coleção, faixas) para track/album/playlist."""
        if kind == "track":
            data = await self._run(self._sp.track, spotify_id)
            meta = self._track_meta(data)
            return data.get("name", "faixa"), [meta] if meta else []

        if kind == "album":
            album = await self._run(self._sp.album, spotify_id)
            images = album.get("images") or []
            tracks = []
            for item in album.get("tracks", {}).get("items", []):
                meta = self._track_meta(dict(item, album={"images": images}))
                if meta:
                    tracks.append(meta)
            return album.get("name", "álbum"), tracks

        if kind == "playlist":
            # O embed público PRIMEIRO. Em 50 dias de journal foram 15
            # importações e 15 falhas na API (404 nas playlists do Spotify,
            # 403 nas de usuário): para apps novos ela nega as faixas de
            # playlist. Tentar antes custava ~1,2s em todo /play de playlist e
            # um WARNING por importação. A API fica de plano B — é a única que
            # enxerga playlist PRIVADA de quem fez o spotify_login.py.
            try:
                name, tracks = await self._scrape_playlist_embed(spotify_id)
            except Exception as erro_do_embed:  # noqa: BLE001
                log.info("playlist %s: embed falhou (%s) — tentando a API", spotify_id, erro_do_embed)
                try:
                    name, tracks = await self._fetch_playlist_api(spotify_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "playlist %s: embed e API falharam — embed: %s | API: %r",
                        spotify_id, erro_do_embed, exc,
                    )
                    # o erro do embed é o que explica ("playlist privada?")
                    raise erro_do_embed from exc
                if not tracks:  # a API às vezes "funciona" com todas as faixas nulas
                    raise erro_do_embed
                log.info("playlist %s pela API: %d faixa(s)", spotify_id, len(tracks))
                return name, tracks
            # o embed entrega no máximo 100 e sem capa: saber por qual caminho
            # veio explica "a playlist tem 300 e entraram 100"
            log.info("playlist %s pelo embed público: %d faixa(s)", spotify_id, len(tracks))
            return name, tracks

        raise ValueError(f"tipo de link não suportado: {kind}")

    async def _fetch_playlist_api(self, spotify_id: str) -> tuple[str, list[dict]]:
        playlist = await self._run(self._sp.playlist, spotify_id, fields="name")
        results = await self._run(
            self._sp.playlist_items, spotify_id, additional_types=("track",)
        )
        tracks = []
        while results:
            for item in results.get("items", []):
                meta = self._track_meta(item.get("track"))
                if meta:
                    tracks.append(meta)
            results = (
                await self._run(self._sp.next, results) if results.get("next") else None
            )
        return playlist.get("name", "playlist"), tracks

    @staticmethod
    async def _scrape_playlist_embed(spotify_id: str) -> tuple[str, list[dict]]:
        """Plano C: a página de embed pública expõe a tracklist (até 100 faixas).

        Desde 2025 o Spotify bloqueia TODOS os endpoints de faixas de playlist
        para apps novos (401/403 até com token de usuário, até na playlist do
        próprio dono) — o embed é o mesmo dado público que o Discord usa para
        pré-visualizar o link. Só funciona para playlists públicas.
        """
        import aiohttp  # dependência do py-cord, já instalada

        url = f"https://open.spotify.com/embed/playlist/{spotify_id}"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                resp.raise_for_status()
                html = await resp.text()
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html,
            re.S,
        )
        if match is None:
            raise LookupError("página de embed sem dados (playlist privada?)")
        entity = json.loads(match.group(1))["props"]["pageProps"]["state"]["data"][
            "entity"
        ]
        tracks = faixas_do_embed(entity)
        if not tracks:
            raise LookupError("embed sem faixas (playlist privada ou vazia)")
        return entity.get("title", "playlist"), tracks


def faixas_do_embed(entity: dict) -> list[dict]:
    """As faixas do JSON do embed público. O embed junta os artistas com espaço
    não-quebrável ("Miguel,<nbsp>J. Cole"): ia parar na busca do YouTube, no
    painel e no journal (visto em 20/09/2026). `split()` sem argumento cobre
    qualquer espaço Unicode."""
    return [
        {
            "title": " ".join(item.get("title", "?").split()),
            "artist": " ".join(item.get("subtitle", "").split()),
            "duration": int((item.get("duration") or 0) / 1000),
            "thumbnail": "",
        }
        for item in entity.get("trackList", [])
        if item.get("title")
    ]
