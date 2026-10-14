"""`/play <link>`: responde na hora, aceita playlist do YouTube, rejeita o resto.

Antes, um link de vídeo só era respondido depois da extração completa
(~8,3s na VM, e atrás do prefetch no pool serializado — até ~16s); links
seguidos viravam respostas a 8, 16, 24s. Um link de playlist do YouTube
fazia o yt-dlp extrair vídeo por vídeo (minutos com o bot mudo), e um link
de canal idem. Agora:

  - vídeo: título/canal/capa pelo oEmbed (~0,3s) e a extração segue em
    background aquecendo o cache; se o oEmbed falhar, espera a extração
    (que sabe dizer "privado", "removido"...);
  - playlist: a lista em uma ida (flat), enfileirada inteira;
  - canal/outros: recusa com uma explicação em vez de sair listando tudo.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import messages as m
from cogs import music as music_cog

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PLAYLIST = "https://www.youtube.com/playlist?list=PLabc"


class Relogio:
    def __init__(self) -> None:
        self.eventos: list[str] = []

    def marca(self, nome: str) -> None:
        self.eventos.append(nome)

    def antes(self, a: str, b: str) -> bool:
        return self.eventos.index(a) < self.eventos.index(b)


@pytest.fixture
def cenario(monkeypatch):
    relogio = Relogio()
    player = MagicMock()
    player.enqueue = AsyncMock()
    player.replace = AsyncMock()
    relogio.player = player

    async def ensure_voice_lento(ctx):
        relogio.marca("voz:inicio")
        await asyncio.sleep(0.02)
        relogio.marca("voz:fim")
        return player

    async def stream(video_id, **kwargs):
        relogio.marca("extracao:inicio")
        await asyncio.sleep(0.05)  # a extração é LENTA (8s na vida real)
        relogio.marca("extracao:fim")
        return {"video_id": video_id, "title": "Extraído", "artist": "A", "duration": 212,
                "thumbnail": "http://t", "stream_url": "http://x"}

    async def rapido(video_id):
        relogio.marca("oembed")
        return {"video_id": video_id, "title": "Rápido", "artist": "A", "duration": 0,
                "thumbnail": "http://t"}

    async def playlist(list_id):
        relogio.marca("playlist")
        return "Minha lista", [
            {"video_id": f"v{i}", "title": f"M{i}", "artist": "A", "duration": 10,
             "thumbnail": ""} for i in range(3)
        ], 3

    monkeypatch.setattr(music_cog, "ensure_voice", ensure_voice_lento)
    monkeypatch.setattr(music_cog.youtube, "get_stream", stream)
    monkeypatch.setattr(music_cog.youtube, "quick_info", rapido)
    monkeypatch.setattr(music_cog.youtube, "playlist_tracks", playlist)
    return relogio


def _ctx():
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.respond = AsyncMock()
    ctx.author.id = 1
    ctx.channel = MagicMock()
    return ctx


def _cog():
    cog = music_cog.Music.__new__(music_cog.Music)
    cog.bot = MagicMock()
    cog.spotify = None
    return cog


async def _play(ctx, busca, modo="adicionar"):
    cog = _cog()
    await cog.play.callback(cog, ctx, busca, modo)
    await asyncio.sleep(0.1)  # deixa a extração em background terminar


# ── vídeo ────────────────────────────────────────────────────────────────────


def test_link_de_video_responde_pelo_oembed_sem_esperar_a_extracao(cenario):
    ctx = _ctx()
    asyncio.run(_play(ctx, VIDEO))

    assert cenario.antes("oembed", "extracao:fim")
    ctx.respond.assert_awaited_once_with(m.ADDED.format(track="Rápido — A"))
    tracks, _canal = cenario.player.enqueue.await_args.args
    assert tracks[0].video_id == "dQw4w9WgXcQ" and tracks[0].requester_id == 1


def test_a_extracao_do_link_continua_em_background(cenario):
    """É ela que aquece o cache para a hora de tocar."""
    asyncio.run(_play(_ctx(), VIDEO))

    assert "extracao:fim" in cenario.eventos


def test_sem_oembed_espera_a_extracao_completa(monkeypatch, cenario):
    async def nada(video_id):
        return None

    monkeypatch.setattr(music_cog.youtube, "quick_info", nada)
    ctx = _ctx()
    asyncio.run(_play(ctx, VIDEO))

    ctx.respond.assert_awaited_once_with(m.ADDED.format(track="Extraído — A"))
    tracks, _ = cenario.player.enqueue.await_args.args
    assert tracks[0].duration == 212


def test_video_impossivel_avisa_com_o_erro_da_extracao(monkeypatch, cenario):
    async def nada(video_id):
        return None

    async def explode(video_id, **kwargs):
        raise RuntimeError("Private video")

    monkeypatch.setattr(music_cog.youtube, "quick_info", nada)
    monkeypatch.setattr(music_cog.youtube, "get_stream", explode)
    ctx = _ctx()
    asyncio.run(_play(ctx, VIDEO))

    ctx.respond.assert_awaited_once_with(m.URL_FAIL)
    cenario.player.enqueue.assert_not_awaited()


def test_extracao_em_background_que_falha_nao_vaza_excecao(monkeypatch, cenario, caplog):
    """oEmbed OK mas a extração morre depois: nada de "Task exception was
    never retrieved" no journal — quem toca avisa na hora certa."""
    async def explode(video_id, **kwargs):
        raise RuntimeError("morreu depois")

    monkeypatch.setattr(music_cog.youtube, "get_stream", explode)
    ctx = _ctx()

    async def run():
        await _play(ctx, VIDEO)

    asyncio.run(run())  # asyncio.run fecha o loop e reportaria a exceção órfã

    assert "never retrieved" not in caplog.text
    ctx.respond.assert_awaited_once_with(m.ADDED.format(track="Rápido — A"))


def test_link_de_video_substituir(cenario):
    ctx = _ctx()
    asyncio.run(_play(ctx, VIDEO, "substituir"))

    ctx.respond.assert_awaited_once_with(m.REPLACED.format(count=1))
    cenario.player.replace.assert_awaited_once()


# ── playlist do YouTube ──────────────────────────────────────────────────────


def test_link_de_playlist_enfileira_a_lista_inteira(cenario):
    ctx = _ctx()
    asyncio.run(_play(ctx, PLAYLIST))

    assert cenario.antes("playlist", "voz:fim")  # também roda durante o handshake
    assert "extracao:inicio" not in cenario.eventos  # NADA de extrair vídeo por vídeo
    ctx.respond.assert_awaited_once_with(
        m.YT_PLAYLIST_ADDED.format(name="Minha lista", count=3, note="")
    )
    tracks, _ = cenario.player.enqueue.await_args.args
    assert [t.video_id for t in tracks] == ["v0", "v1", "v2"]
    assert all(t.requester_id == 1 for t in tracks)


def test_playlist_no_teto_avisa_que_cortou(monkeypatch, cenario):
    async def grande(list_id):
        return "Grande", [
            {"video_id": f"v{i}", "title": "M", "artist": "", "duration": 1, "thumbnail": ""}
            for i in range(music_cog.youtube.PLAYLIST_MAX)
        ], 250

    monkeypatch.setattr(music_cog.youtube, "playlist_tracks", grande)
    ctx = _ctx()
    asyncio.run(_play(ctx, PLAYLIST))

    ctx.respond.assert_awaited_once_with(m.YT_PLAYLIST_ADDED.format(
        name="Grande", count=music_cog.youtube.PLAYLIST_MAX, note=m.YT_PLAYLIST_LIMIT_NOTE
    ))


def test_playlist_privada_ou_inexistente(monkeypatch, cenario):
    async def explode(list_id):
        raise RuntimeError("The playlist does not exist")

    monkeypatch.setattr(music_cog.youtube, "playlist_tracks", explode)
    ctx = _ctx()
    asyncio.run(_play(ctx, PLAYLIST))

    ctx.respond.assert_awaited_once_with(m.YT_PLAYLIST_FAIL)


def test_playlist_vazia(monkeypatch, cenario):
    async def vazia(list_id):
        return "Vazia", [], 0

    monkeypatch.setattr(music_cog.youtube, "playlist_tracks", vazia)
    ctx = _ctx()
    asyncio.run(_play(ctx, PLAYLIST))

    ctx.respond.assert_awaited_once_with(m.YT_PLAYLIST_EMPTY)


def test_playlist_substituir(cenario):
    ctx = _ctx()
    asyncio.run(_play(ctx, PLAYLIST, "substituir"))

    ctx.respond.assert_awaited_once_with(m.REPLACED.format(count=3))
    cenario.player.replace.assert_awaited_once()


# ── o resto do YouTube ───────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/@algumcanal",
    "https://www.youtube.com/results?search_query=x",
    "https://www.youtube.com/feed/trending",
])
def test_link_que_nao_e_video_nem_playlist_e_recusado_sem_rede(cenario, url):
    ctx = _ctx()
    asyncio.run(_play(ctx, url))

    ctx.respond.assert_awaited_once_with(m.URL_NOT_VIDEO)
    assert cenario.eventos == []  # nem conectou na voz, nem tocou na rede


# ── achados da revisão de 12/09 ──────────────────────────────────────────────


def test_substituir_com_link_so_troca_a_fila_depois_da_extracao_dar_certo(monkeypatch, cenario):
    """oEmbed responde 200 para vídeo com restrição de idade/país/DRM; só a
    extração sabe que não toca. Substituir pelo oEmbed derrubava a atual,
    limpava a fila e ficava o silêncio. Antes: URL_FAIL e a atual seguia."""
    async def explode(video_id, **kwargs):
        raise RuntimeError("Sign in to confirm your age")

    monkeypatch.setattr(music_cog.youtube, "get_stream", explode)
    ctx = _ctx()
    asyncio.run(_play(ctx, VIDEO, "substituir"))

    ctx.respond.assert_awaited_once_with(m.URL_FAIL)
    cenario.player.replace.assert_not_awaited()


def test_substituir_com_link_usa_os_metadados_completos(cenario):
    ctx = _ctx()
    asyncio.run(_play(ctx, VIDEO, "substituir"))

    tracks, _ = cenario.player.replace.await_args.args
    assert tracks[0].duration == 212  # veio da extração, não do oEmbed


def test_ensure_voice_que_explode_nao_deixa_tarefa_orfa(monkeypatch, cenario):
    async def explode(ctx):
        raise RuntimeError("timeout no connect")

    monkeypatch.setattr(music_cog, "ensure_voice", explode)
    descartadas = []
    original = music_cog._descarta

    async def espia(tarefa):
        descartadas.append(tarefa)
        await original(tarefa)

    monkeypatch.setattr(music_cog, "_descarta", espia)
    ctx = _ctx()

    with pytest.raises(RuntimeError):
        asyncio.run(_play(ctx, PLAYLIST))

    tarefas = [t for t in descartadas if t is not None]
    assert tarefas and all(t.done() for t in tarefas)


def test_mix_do_youtube_nao_e_playlist_importavel(cenario):
    """`list=RD…` é a rádio pessoal do YouTube: não dá para listar."""
    ctx = _ctx()
    asyncio.run(_play(ctx, "https://www.youtube.com/playlist?list=RDdQw4w9WgXcQ"))

    ctx.respond.assert_awaited_once_with(m.YT_PLAYLIST_MIX)
    assert "playlist" not in cenario.eventos


def test_nota_de_corte_usa_o_total_da_playlist(monkeypatch, cenario):
    """playlistend corta ANTES do filtro de vídeos mortos: 100 itens com 3
    privados viram 97 faixas — a nota tem de olhar o total da playlist."""
    async def grande(list_id):
        return "Grande", [
            {"video_id": f"v{i}", "title": "M", "artist": "", "duration": 1, "thumbnail": ""}
            for i in range(97)
        ], 150

    monkeypatch.setattr(music_cog.youtube, "playlist_tracks", grande)
    ctx = _ctx()
    asyncio.run(_play(ctx, PLAYLIST))

    ctx.respond.assert_awaited_once_with(m.YT_PLAYLIST_ADDED.format(
        name="Grande", count=97, note=m.YT_PLAYLIST_LIMIT_NOTE
    ))
