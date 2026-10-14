"""Busca com plano B: ytmusicapi cai, o yt-dlp assume.

Toda importação do Spotify e todo comando de voz passam por search_one(). Com
o ytmusicapi como única fonte, uma quebra dele derrubava a playlist inteira
com "não achei essa música no YouTube" — sem nem tentar o YouTube.
"""
import asyncio

import pytest

from services import youtube


@pytest.fixture(autouse=True)
def _reset():
    youtube._ytmusic = None
    yield
    youtube._ytmusic = None


class FakeYTM:
    def __init__(self, results=None, boom: Exception | None = None) -> None:
        self.results = results or []
        self.boom = boom
        self.calls = 0

    def search(self, query, filter=None, limit=5):
        self.calls += 1
        if self.boom:
            raise self.boom
        return self.results[:limit]


def _ytm_item(vid: str, title: str = "T") -> dict:
    return {
        "videoId": vid,
        "title": title,
        "artists": [{"name": "A"}],
        "duration": "3:00",
        "thumbnails": [{"url": "http://t/1"}],
    }


def test_busca_normal_usa_o_ytmusic(monkeypatch):
    ytm = FakeYTM([_ytm_item("v1"), _ytm_item("v2")])
    monkeypatch.setattr(youtube, "_ytm", lambda: ytm)

    got = asyncio.run(youtube.search_songs("qualquer", limit=2))

    assert [g["video_id"] for g in got] == ["v1", "v2"]


def test_ytmusic_quebrado_cai_para_o_ytdlp(monkeypatch):
    monkeypatch.setattr(youtube, "_ytm", lambda: FakeYTM(boom=RuntimeError("API fora")))
    monkeypatch.setattr(
        youtube,
        "_ytdlp_search",
        lambda query, limit: [
            {"video_id": "fb1", "title": "de fallback", "artist": "A", "duration": 10,
             "thumbnail": ""}
        ],
    )

    got = asyncio.run(youtube.search_songs("qualquer", limit=3))

    assert [g["video_id"] for g in got] == ["fb1"]


def test_ytmusic_sem_resultado_tambem_tenta_o_ytdlp(monkeypatch):
    """Faixa que o YouTube Music não indexa (remix, ao vivo obscuro) existe no YouTube."""
    monkeypatch.setattr(youtube, "_ytm", lambda: FakeYTM([]))
    monkeypatch.setattr(
        youtube,
        "_ytdlp_search",
        lambda query, limit: [
            {"video_id": "fb1", "title": "achou", "artist": "", "duration": 0, "thumbnail": ""}
        ],
    )

    assert asyncio.run(youtube.search_one("obscura"))["video_id"] == "fb1"


def test_as_duas_fontes_falham_devolve_vazio(monkeypatch):
    monkeypatch.setattr(youtube, "_ytm", lambda: FakeYTM(boom=RuntimeError("API fora")))

    def boom(query, limit):
        raise RuntimeError("yt-dlp fora tambem")

    monkeypatch.setattr(youtube, "_ytdlp_search", boom)

    assert asyncio.run(youtube.search_songs("x")) == []
    assert asyncio.run(youtube.search_one("x")) is None


def test_falha_do_ytmusic_descarta_a_instancia(monkeypatch):
    """Sessão HTTP podre ficava para sempre: o singleton nunca era recriado."""
    youtube._ytmusic = object()
    monkeypatch.setattr(youtube, "_ytm", lambda: FakeYTM(boom=RuntimeError("API fora")))
    monkeypatch.setattr(youtube, "_ytdlp_search", lambda query, limit: [])

    asyncio.run(youtube.search_songs("x"))

    assert youtube._ytmusic is None


def test_ytdlp_search_normaliza_o_resultado(monkeypatch):
    entries = {
        "entries": [
            {"id": "a1", "title": "Titulo", "uploader": "Canal", "duration": 200,
             "thumbnail": "http://t"},
            {"id": None, "title": "sem id"},  # descartada
        ]
    }
    monkeypatch.setattr(youtube, "_extract", lambda url, **kwargs: entries)

    got = youtube._ytdlp_search("consulta", 5)

    assert got == [
        {"video_id": "a1", "title": "Titulo", "artist": "Canal", "duration": 200,
         "thumbnail": "http://t"}
    ]


# ── teste do Otavio de 21/09: "Arrodes, toca Indepol" ──────────────────────
# O Whisper escreveu "in the pool" como "Indepol". O YouTube Music não tem música
# com esse nome → plano B (busca comum do YouTube) → o 1º vídeo era um COVER de
# piano: "Chainsaw Man: Reze Arc OST - in the pool (Piano) | Kensuke Ushio".
# Medido na VM: buscar no YouTube Music pelo TÍTULO desse vídeo devolve, em 1º, a
# gravação original ("in the pool — kensuke ushio").


class YTMPorConsulta:
    def __init__(self, por_consulta: dict) -> None:
        self.por_consulta = por_consulta
        self.consultas: list[str] = []

    def search(self, query, filter=None, limit=5):
        self.consultas.append(query)
        return self.por_consulta.get(query, [])[:limit]


def _musica(vid: str, titulo: str, artista: str) -> dict:
    return {"videoId": vid, "title": titulo, "artists": [{"name": artista}], "duration": "4:06",
            "thumbnails": [{"url": "http://t/1"}]}


VIDEO_DO_COVER = {
    "video_id": "cover", "title": "Chainsaw Man: Reze Arc OST - in the pool (Piano) | Kensuke Ushio",
    "artist": "PianoDeuss", "duration": 259, "thumbnail": "",
}


def test_video_do_plano_b_vira_a_gravacao_original_quando_o_titulo_dele_a_nomeia(monkeypatch):
    ytm = YTMPorConsulta({VIDEO_DO_COVER["title"]: [_musica("original", "in the pool", "kensuke ushio")]})
    monkeypatch.setattr(youtube, "_ytm", lambda: ytm)
    monkeypatch.setattr(youtube, "_ytdlp_search", lambda query, limit: [dict(VIDEO_DO_COVER)])

    achado = asyncio.run(youtube.search_one("indepol"))

    assert achado["video_id"] == "original" and achado["artist"] == "kensuke ushio"
    assert ytm.consultas == ["indepol", VIDEO_DO_COVER["title"]]


def test_video_do_plano_b_fica_quando_a_musica_achada_nao_e_a_do_titulo(monkeypatch):
    """Remix/ao vivo obscuro que o YouTube Music não indexa é o motivo de o plano B
    existir: uma música qualquer com outro nome não pode tomar o lugar dele."""
    video = {"video_id": "obscuro", "title": "Banda X - Ao Vivo no Bar do Zé 2009", "artist": "canal do zé",
             "duration": 300, "thumbnail": ""}
    ytm = YTMPorConsulta({video["title"]: [_musica("outra", "Evidências", "Chitãozinho & Xororó")]})
    monkeypatch.setattr(youtube, "_ytm", lambda: ytm)
    monkeypatch.setattr(youtube, "_ytdlp_search", lambda query, limit: [dict(video)])

    assert asyncio.run(youtube.search_one("banda x ao vivo no bar do ze"))["video_id"] == "obscuro"


def test_segunda_busca_que_falha_nao_derruba_o_plano_b(monkeypatch):
    class Quebra(YTMPorConsulta):
        def search(self, query, filter=None, limit=5):
            if self.consultas:
                raise RuntimeError("API fora")
            return super().search(query, filter, limit)

    monkeypatch.setattr(youtube, "_ytm", lambda: Quebra({}))
    monkeypatch.setattr(youtube, "_ytdlp_search", lambda query, limit: [dict(VIDEO_DO_COVER)])

    assert asyncio.run(youtube.search_one("indepol"))["video_id"] == "cover"
