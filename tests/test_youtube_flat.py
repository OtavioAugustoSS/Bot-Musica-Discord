"""Busca e playlist NÃO podem resolver formato de cada vídeo.

Medido no dev (12/09), instância quente, perfil padrão:

    ytsearch3 cheio: 3,3s      ytsearch3 flat: 1,1s
    playlist de 183 vídeos, cheia (só 3 itens): 4,6s   flat (183): 1,4s

"Cheio" resolve formato (desafio JS) de CADA entrada — no dev ~1s por
vídeo, na VM ~8s. Ou seja, o plano B da busca custava 5 × 8s e um
`/play <link de playlist>` de 183 vídeos prendia o pool serializado por
~25 minutos com o bot mudo em todos os servidores. Em modo flat as entradas
já trazem id, título, duração, canal e thumbnails — o formato só é
resolvido na hora de tocar (prefetch/_try_start), que é onde já era.
"""
import asyncio

import pytest

from services import youtube


@pytest.fixture(autouse=True)
def _reset():
    youtube._stream_cache.clear()
    youtube._failures.clear()
    youtube.reset_profiles()
    yield
    youtube._stream_cache.clear()
    youtube._failures.clear()
    youtube.reset_profiles()


# ── instância flat por perfil ────────────────────────────────────────────────


def test_opts_flat_nao_resolve_formato_das_entradas():
    perfil = youtube.profiles()[0]
    assert "extract_flat" not in perfil.opts("")
    flat = perfil.opts("", flat=True)
    assert flat["extract_flat"] == "in_playlist"
    assert flat["playlistend"] == youtube.PLAYLIST_MAX


def test_instancia_flat_e_separada_e_reaproveitada():
    perfil = youtube.profiles()[0]
    cheia = youtube._get_ydl(perfil)
    flat = youtube._get_ydl(perfil, flat=True)

    assert flat is not cheia
    assert youtube._get_ydl(perfil, flat=True) is flat  # cache por thread vale
    assert youtube._get_ydl(perfil) is cheia


def test_extracao_de_playlist_usa_a_instancia_flat(monkeypatch):
    usados = []

    class FakeYdl:
        def __init__(self, flat):
            self.flat = flat

        def extract_info(self, url, download=False):
            usados.append(self.flat)
            return {"entries": [{"id": "a"}]}

    monkeypatch.setattr(youtube, "_get_ydl", lambda perfil, flat=False: FakeYdl(flat))
    perfil = youtube.profiles()[0]

    youtube._extract_once("ytsearch3:x", perfil, playlist=True)
    youtube._extract_once("https://www.youtube.com/watch?v=a", perfil)

    assert usados == [True, False]


def test_busca_pelo_ytdlp_le_thumbnail_da_lista():
    """Em modo flat `thumbnail` vem None; a capa está em `thumbnails`."""
    entradas = {"entries": [
        {"id": "a1", "title": "T", "channel": "C", "duration": 200, "thumbnail": None,
         "thumbnails": [{"url": "http://t/p"}, {"url": "http://t/g"}]},
    ]}
    got = youtube._normalize_flat(entradas["entries"][0])

    assert got["thumbnail"] == "http://t/g"
    assert got["artist"] == "C"


# ── ids a partir de URL ──────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc&index=3",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=abc123",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share",
])
def test_extrai_o_id_do_video(url):
    assert youtube.extract_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/playlist?list=PLabc",
    "https://www.youtube.com/@canal",
    "https://www.youtube.com/watch?v=curto",  # id inválido
    "nome de música",
    "",
])
def test_sem_id_de_video(url):
    assert youtube.extract_video_id(url) is None


def test_extrai_o_id_da_playlist_so_quando_nao_ha_video():
    """Link de vídeo "dentro de" playlist toca o vídeo (é o que a pessoa
    compartilhou); só o link de playlist pura importa a playlist."""
    assert youtube.extract_playlist_id("https://www.youtube.com/playlist?list=PLabc_-9") == "PLabc_-9"
    assert youtube.extract_playlist_id("https://music.youtube.com/playlist?list=OLAK5uy_x") == "OLAK5uy_x"
    assert youtube.extract_playlist_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc") is None
    assert youtube.extract_playlist_id("https://www.youtube.com/@canal") is None
    assert youtube.extract_playlist_id("playlist") is None


# ── playlist do YouTube ──────────────────────────────────────────────────────


def test_playlist_tracks_normaliza_e_descarta_videos_mortos(monkeypatch):
    def fake_extract(url, **kwargs):
        assert kwargs.get("playlist") is True
        assert url == "https://www.youtube.com/playlist?list=PLx"
        return {"title": "Minha lista", "entries": [
            {"id": "a1", "title": "Viva", "channel": "C", "duration": 100,
             "thumbnails": [{"url": "http://t/a"}]},
            {"id": "a2", "title": "[Private video]", "channel": None, "duration": None},
            {"id": "a3", "title": "[Deleted video]"},
            None,
            {"id": "a4", "title": "Outra", "uploader": "U", "duration": 50},
        ]}

    monkeypatch.setattr(youtube, "_extract", fake_extract)

    nome, faixas, total = asyncio.run(youtube.playlist_tracks("PLx"))

    assert nome == "Minha lista"
    assert total == 2  # sem playlist_count no info: o que sobrou
    assert [f["video_id"] for f in faixas] == ["a1", "a4"]
    assert faixas[0] == {"video_id": "a1", "title": "Viva", "artist": "C", "duration": 100,
                         "thumbnail": "http://t/a"}
    assert faixas[1]["artist"] == "U" and faixas[1]["thumbnail"] == ""


# ── get_stream com URL usa o cache pelo id ───────────────────────────────────


def test_get_stream_com_url_acha_o_cache_pelo_id(monkeypatch):
    """`/play <link>` de uma faixa quente pagava a extração inteira: a busca
    no cache só acontecia quando o argumento era o id."""
    youtube._cache_put("dQw4w9WgXcQ", {"video_id": "dQw4w9WgXcQ", "stream_url": "quente"})

    def nao_extrai(url, **kwargs):
        raise AssertionError("não devia extrair")

    monkeypatch.setattr(youtube, "_extract", nao_extrai)

    info = asyncio.run(youtube.get_stream("https://youtu.be/dQw4w9WgXcQ?si=zz"))

    assert info["stream_url"] == "quente"


def test_get_stream_com_url_de_video_compartilha_o_voo_com_o_id(monkeypatch):
    """O cog adianta com a URL e o player pede pelo id: tem de ser UMA extração."""
    extracoes = []

    def lenta(url, **kwargs):
        extracoes.append(url)
        return {"id": "dQw4w9WgXcQ", "title": "T", "url": "http://s"}

    monkeypatch.setattr(youtube, "_extract", lenta)

    async def run():
        return await asyncio.gather(
            youtube.get_stream("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1"),
            youtube.get_stream("dQw4w9WgXcQ"),
        )

    a, b = asyncio.run(run())

    assert a["stream_url"] == b["stream_url"] == "http://s"
    assert len(extracoes) == 1


# ── achados da revisão de 12/09 ──────────────────────────────────────────────


def test_watch_com_list_e_sem_v_e_playlist():
    """`youtube.com/watch?list=PL…` sem `v=`: o yt-dlp trata como playlist."""
    assert youtube.extract_playlist_id("https://www.youtube.com/watch?list=PLabc") == "PLabc"


def test_playlist_tracks_devolve_o_total_real(monkeypatch):
    """A nota "primeiras 100" precisa do total da playlist, não do que sobrou
    depois de cortar em 100 e filtrar vídeos mortos."""
    def fake_extract(url, **kwargs):
        return {"title": "L", "playlist_count": 183, "entries": [{"id": "a1", "title": "x"}]}

    monkeypatch.setattr(youtube, "_extract", fake_extract)

    nome, faixas, total = asyncio.run(youtube.playlist_tracks("PLx"))

    assert (nome, len(faixas), total) == ("L", 1, 183)


@pytest.mark.parametrize("erro", [
    "ERROR: [youtube:tab] PLx: The playlist does not exist.",
    "ERROR: [youtube:tab] PLx: This playlist is private",
    "ERROR: [youtube:tab] RDx: This playlist type is unviewable",
])
def test_playlist_morta_nao_percorre_a_cadeia_de_perfis(monkeypatch, erro):
    tentativas = []

    def fake_once(url, profile, **kwargs):
        tentativas.append(profile.name)
        raise RuntimeError(erro)

    monkeypatch.setattr(youtube, "_extract_once", fake_once)

    with pytest.raises(RuntimeError):
        asyncio.run(youtube.playlist_tracks("PLx"))
    assert len(tentativas) == 1


def test_falha_recente_nao_repaga_a_cadeia_inteira(monkeypatch):
    """/play <link> pelo oEmbed enfileira a faixa; a extração em background
    falha (idade/país/DRM). Na hora de tocar, _try_start pedia de novo e
    pagava a cadeia inteira (até 25s no worker único) para falhar igual."""
    chamadas = []

    def falha(url, **kwargs):
        chamadas.append(url)
        raise RuntimeError("Sign in to confirm your age")

    monkeypatch.setattr(youtube, "_extract", falha)

    async def run():
        with pytest.raises(RuntimeError):
            await youtube.get_stream("dQw4w9WgXcQ")
        with pytest.raises(RuntimeError, match="confirm your age"):
            await youtube.get_stream("dQw4w9WgXcQ")

    asyncio.run(run())

    assert len(chamadas) == 1


def test_falha_recente_expira_e_use_cache_false_ignora(monkeypatch):
    relogio = [1000.0]
    monkeypatch.setattr(youtube, "_now", lambda: relogio[0])
    chamadas = []

    def falha(url, **kwargs):
        chamadas.append(url)
        raise RuntimeError("x")

    monkeypatch.setattr(youtube, "_extract", falha)

    async def run():
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await youtube.get_stream("dQw4w9WgXcQ")
        with pytest.raises(RuntimeError):
            await youtube.get_stream("dQw4w9WgXcQ", use_cache=False)  # retentativa explícita
        relogio[0] += youtube._FAILURE_TTL_S + 1
        with pytest.raises(RuntimeError):
            await youtube.get_stream("dQw4w9WgXcQ")

    asyncio.run(run())

    assert len(chamadas) == 3
