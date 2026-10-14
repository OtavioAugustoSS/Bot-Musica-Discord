"""Cache de extração do yt-dlp: TTL, evicção, invalidate e uso no get_stream."""
import asyncio

from services import youtube


def _info(video_id: str) -> dict:
    return {"video_id": video_id, "stream_url": f"https://stream/{video_id}"}


def test_cache_hit_and_ttl_expiry(monkeypatch):
    youtube._stream_cache.clear()
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])

    youtube._cache_put("abc", _info("abc"))
    assert youtube._cache_get("abc") == _info("abc")

    clock["t"] += youtube._STREAM_TTL_S + 1
    assert youtube._cache_get("abc") is None  # venceu e foi removida


def test_eviction_keeps_newest(monkeypatch):
    youtube._stream_cache.clear()
    clock = {"t": 0.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])

    total = youtube._STREAM_CACHE_MAX + 5
    for i in range(total):
        clock["t"] = float(i)
        youtube._cache_put(f"v{i}", _info(f"v{i}"))

    assert len(youtube._stream_cache) == youtube._STREAM_CACHE_MAX
    assert youtube._cache_get("v0") is None  # mais velhas saíram
    assert youtube._cache_get(f"v{total - 1}") is not None


def test_invalidate():
    youtube._stream_cache.clear()
    youtube._cache_put("abc", _info("abc"))
    youtube.invalidate("abc")
    assert youtube._cache_get("abc") is None
    youtube.invalidate("nao-existe")  # não explode


def test_get_stream_uses_cache_and_use_cache_false_bypasses(monkeypatch):
    youtube._stream_cache.clear()
    calls = []

    def fake_extract(url: str) -> dict:
        calls.append(url)
        return {
            "id": "vid1",
            "title": "T",
            "uploader": "A",
            "duration": 10,
            "thumbnail": "",
            "url": "https://stream/vid1",
        }

    monkeypatch.setattr(youtube, "_extract", fake_extract)

    first = asyncio.run(youtube.get_stream("vid1"))
    second = asyncio.run(youtube.get_stream("vid1"))
    assert first["stream_url"] == "https://stream/vid1"
    assert second == first
    assert len(calls) == 1  # segunda veio do cache

    asyncio.run(youtube.get_stream("vid1", use_cache=False))
    assert len(calls) == 2  # bypass força extração fresca
