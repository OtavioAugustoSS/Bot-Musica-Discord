"""Enquanto a extração fria roda (~8s na VM), o painel diz o que está fazendo.

Sem isto o painel ficava em "Tá tocando nada" (ou na faixa anterior) por 8s
depois de um /play ou de um pular, e a pessoa via como travamento. Só vale
para extração fria/em voo: faixa no cache toca em 0,0s e um edit extra do
painel seria só ruído.
"""
import asyncio
from unittest.mock import AsyncMock

import messages as m
from core.panel import now_playing_text
from apoio import faz_player
from core.player import GuildPlayer
from core.queue import MusicQueue, Track


class StubPlayer:
    def __init__(self):
        self.queue = MusicQueue()
        self.is_playing = False
        self.is_paused = False
        self.elapsed = 0
        self.preparing = None


def test_texto_do_painel_mostra_a_faixa_em_preparo():
    p = StubPlayer()
    p.preparing = Track(title="Vidro Fumê", artist="Bruno & Marrone", video_id="v")

    texto = now_playing_text(p)

    assert "Preparando" in texto and "Vidro Fumê" in texto


def test_sem_preparo_continua_o_texto_de_parado():
    texto = now_playing_text(StubPlayer())

    assert m.PANEL_IDLE_TITLE in texto


# ── o player marca e desmarca ────────────────────────────────────────────────


class FakeVoice:
    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    def play(self, *a, **k):
        pass


class FakeGuild:
    id = 1

    def __init__(self):
        self.voice_client = FakeVoice()


def _player(monkeypatch, estado: str) -> tuple[GuildPlayer, list]:
    import core.player as player_mod

    p = faz_player(guild=FakeGuild())
    vistos = []  # o que o painel viu em `preparing` a cada refresh

    class Panel:
        async def refresh(self):
            vistos.append(p.preparing.title if p.preparing else None)

        async def ensure_visible(self, channel):
            pass

    p.panel = Panel()

    async def stream(video_id, **kwargs):
        assert p.preparing is not None if estado != "no cache" else True
        return {"duration": 10, "thumbnail": "", "stream_url": "http://x"}

    monkeypatch.setattr(player_mod.youtube, "stream_state", lambda vid: estado)
    monkeypatch.setattr(player_mod.youtube, "get_stream", stream)
    monkeypatch.setattr(player_mod.youtube._YTDLP_POOL, "pending", lambda: 0)
    p._build_and_play = AsyncMock()
    p._schedule_prefetch = lambda: None
    return p, vistos


def test_extracao_fria_mostra_preparando_e_depois_limpa(monkeypatch):
    p, vistos = _player(monkeypatch, "fria")
    track = Track(title="Fria", video_id="v1")

    assert asyncio.run(p._try_start(track)) is True

    assert vistos[0] == "Fria"  # refresh com "preparando" antes de extrair
    assert vistos[-1] is None  # e o painel final já sem o aviso
    assert p.preparing is None


def test_faixa_no_cache_nao_gasta_um_edit_extra(monkeypatch):
    p, vistos = _player(monkeypatch, "no cache")

    asyncio.run(p._try_start(Track(title="Quente", video_id="v1")))

    assert vistos == [None]  # só o refresh final


def test_falha_na_extracao_tambem_limpa_o_preparo(monkeypatch):
    import core.player as player_mod

    p, _ = _player(monkeypatch, "fria")

    async def explode(video_id, **kwargs):
        raise RuntimeError("morreu")

    monkeypatch.setattr(player_mod.youtube, "get_stream", explode)

    assert asyncio.run(p._try_start(Track(title="X", video_id="v1"))) is False
    assert p.preparing is None
