from spotipy.cache_handler import MemoryCacheHandler

from services.spotify_api import make_auth_manager, parse_spotify_url


def test_client_credentials_uses_memory_cache():
    # o CacheFileHandler default gravava `.cache` no CWD — e já vazou token em commit
    mgr = make_auth_manager("id", "secret", token_cache="")
    assert isinstance(mgr.cache_handler, MemoryCacheHandler)


def test_parses_track_link():
    kind, sid = parse_spotify_url("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")
    assert kind == "track"
    assert sid == "4uLU6hMCjMI75M1A2tKUQC"


def test_parses_album_and_playlist_links():
    assert parse_spotify_url("https://open.spotify.com/album/abc123DEF")[0] == "album"
    assert parse_spotify_url("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")[0] == "playlist"


def test_parses_intl_links():
    kind, sid = parse_spotify_url("https://open.spotify.com/intl-pt/track/4uLU6hMCjMI75M1A2tKUQC")
    assert kind == "track"
    assert sid == "4uLU6hMCjMI75M1A2tKUQC"


def test_parses_link_with_query_params():
    kind, sid = parse_spotify_url(
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123&pt=xyz"
    )
    assert kind == "playlist"
    assert sid == "37i9dQZF1DXcBWIGoYBM5M"


def test_rejects_non_spotify_input():
    assert parse_spotify_url("lofi hip hop") is None
    assert parse_spotify_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert parse_spotify_url("https://open.spotify.com/artist/xyz") is None
