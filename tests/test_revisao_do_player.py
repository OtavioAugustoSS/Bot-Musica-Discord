"""Seis defeitos achados numa revisão do código em 28/09/2026 (nenhum no journal ainda).

1. Painel ou histórico falhando DEPOIS de a música começar faziam `_try_start`
   devolver False: a faixa tocando contava como falha e a fila avançava por cima.
2. "toca a terceira" nos últimos segundos da música: ela acabava sozinha durante
   a extração, a terceira começava — e o corte do fim pulava justamente ela.
3. Duas pré-montagens da próxima faixa ao mesmo tempo: a segunda sobrescrevia a
   primeira e o FFmpeg + thread dela ficavam vivos até o restart.
4. /queue: o menu de remover guardava a POSIÇÃO; uma faixa acabando no meio fazia
   ele remover a música de outra pessoa.
5. /stop durante a extração: sem voz, o erro virava "URL vencida" e o bot extraía
   tudo de novo (~4s de CPU no worker único) para nada.
6. /skip durante a extração fria respondia "pulada" e a faixa tocava assim mesmo.
"""
import asyncio

import discord
import pytest

from apoio import FakeChannel, FakeVoice, faz_player
from core.queue import Track


class _Resp:
    status = 503
    reason = "Service Unavailable"


def _http_erro():
    return discord.HTTPException(_Resp(), "fora")


# ── 1 ───────────────────────────────────────────────────────────────────────


class _PainelQueFalha:
    async def ensure_visible(self, channel):
        pass

    async def refresh(self):
        raise _http_erro()


class _Db:
    async def add_history(self, *a):
        raise RuntimeError("database is locked")


class _Bot:
    db = _Db()


def _prepara_toque(monkeypatch, player):
    async def stream(video_id, **kw):
        return {"duration": 10, "thumbnail": "", "stream_url": "http://x"}

    async def build(info, track=None):
        pass

    async def abre(info):
        return (0.0, 0)

    monkeypatch.setattr("core.player.youtube.get_stream", stream)
    monkeypatch.setattr("core.player.youtube.espera_stream_abrir", abre)
    monkeypatch.setattr(player, "_build_and_play", build)
    monkeypatch.setattr(player, "_schedule_prefetch", lambda: None)


def test_painel_ou_historico_falhando_nao_fazem_a_faixa_tocando_virar_falha(monkeypatch):
    player = faz_player(panel=_PainelQueFalha(), bot=_Bot(), channel=FakeChannel())
    _prepara_toque(monkeypatch, player)
    track = Track(title="Tocando", video_id="v1", requester_id=5)

    assert asyncio.run(player._try_start(track)) is True
    assert player._failures == []


def test_painel_que_nao_consegue_mandar_mensagem_nao_explode(monkeypatch):
    from core import panel as panel_mod

    monkeypatch.setattr(panel_mod, "PlayerView", lambda player: None)
    painel = panel_mod.PlayerPanel.__new__(panel_mod.PlayerPanel)
    painel.player, painel.message, painel._reposted_at = None, None, -1e9

    class Canal:
        async def send(self, **kw):
            raise _http_erro()

    asyncio.run(painel._show_locked(Canal()))
    assert painel.message is None


# ── 2 ───────────────────────────────────────────────────────────────────────


def _player_tocando():
    player = faz_player(voice=FakeVoice(playing=True))
    player._tocada = object()
    pulos = []
    player.skip = lambda *a, **k: pulos.append(1)

    async def nada(*a, **k):
        pass

    player.espera_efeito = nada
    return player, pulos


def test_corte_nao_pula_a_escolhida_que_ja_comecou():
    player, pulos = _player_tocando()
    escolhida = Track(title="Terceira", video_id="t3")

    async def extrai_e_a_atual_acaba(track):
        player._tocada = object()  # a anterior acabou sozinha; a escolhida começou

    player._deixa_pronta = extrai_e_a_atual_acaba
    asyncio.run(player._corta_para_a_proxima(escolhida))
    assert pulos == []


def test_corte_normal_ainda_corta():
    player, pulos = _player_tocando()

    async def extrai(track):
        pass

    player._deixa_pronta = extrai
    asyncio.run(player._corta_para_a_proxima(Track(title="T", video_id="t")))
    assert pulos == [1]


def test_pula_por_voz_so_se_a_faixa_ainda_for_a_mesma():
    player, pulos = _player_tocando()
    antes = player.tocada_agora()
    assert player.pula_se_ainda_for(antes) is True
    player._tocada = object()
    assert player.pula_se_ainda_for(antes) is False
    assert pulos == [1]


# ── 3 ───────────────────────────────────────────────────────────────────────


class _Pulmao:
    def __init__(self):
        self.limpo = False

    def cleanup(self):
        self.limpo = True


def test_pre_montagem_em_paralelo_nao_vaza_ffmpeg(monkeypatch):
    player = faz_player(voice=FakeVoice(playing=True))
    tocada = type("T", (), {"pulada": False, "parada": False, "acabou": False})()
    player._tocada = tocada
    proxima = Track(title="Próxima", video_id="p")
    ganhadora = type("P", (), {"track": proxima, "pulmao": _Pulmao()})()
    nosso = _Pulmao()

    async def stream(video_id, **kw):
        return {}

    async def abre(info):
        return (0.0, 0)

    async def monta(info, track):
        player._pronta = ganhadora  # a outra chamada terminou primeiro
        return nosso, None, "via"

    monkeypatch.setattr("core.player.youtube.stream_state", lambda v: "no cache")
    monkeypatch.setattr("core.player.youtube.get_stream", stream)
    monkeypatch.setattr("core.player.youtube.espera_stream_abrir", abre)
    monkeypatch.setattr(player, "_monta", monta)

    asyncio.run(player._prepara_a_proxima(tocada, proxima))
    assert player._pronta is ganhadora
    assert nosso.limpo, "o FFmpeg perdedor ficava vivo até o restart"


# ── 4 ───────────────────────────────────────────────────────────────────────


def test_menu_do_queue_remove_a_faixa_escolhida_mesmo_com_a_fila_andando(monkeypatch):
    from cogs import music

    player = faz_player()
    a, b, c = (Track(title=n, video_id=n, requester_id=1) for n in "abc")
    for t in (a, b, c):
        player.queue.add(t)
    view = music.QueueView.__new__(music.QueueView)
    view.player, view.page = player, 0
    view._remove = type("S", (), {"values": ["1"], "options": [], "disabled": False})()
    view._opcoes = [a, b, c]

    player.queue.upcoming.pop(0)  # "a" começou a tocar: a fila andou

    async def redesenha(interaction):
        pass

    view._redraw = redesenha
    interacao = type("I", (), {"user": type("U", (), {"id": 1})()})()
    asyncio.run(view._on_remove(interacao))
    assert player.queue.upcoming == [c], "antes removia c (a que ficou na posição 1)"


# ── 5 ───────────────────────────────────────────────────────────────────────


def test_sem_voz_nao_extrai_de_novo(monkeypatch):
    player = faz_player(voice=FakeVoice(connected=False))
    extracoes = []

    async def stream(video_id, **kw):
        extracoes.append(kw.get("use_cache", True))
        return {"duration": 10, "thumbnail": "", "stream_url": "http://x"}

    async def build(info, track=None):
        raise AttributeError("'NoneType' object has no attribute 'play'")

    async def abre(info):
        return (0.0, 0)

    monkeypatch.setattr("core.player.youtube.get_stream", stream)
    monkeypatch.setattr("core.player.youtube.espera_stream_abrir", abre)
    monkeypatch.setattr(player, "_build_and_play", build)

    assert asyncio.run(player._try_start(Track(title="X", video_id="x"))) is False
    assert extracoes == [True], "a 2ª extração (use_cache=False) era trabalho jogado fora"


# ── 6 ───────────────────────────────────────────────────────────────────────


def test_skip_durante_a_preparacao_nao_finge_que_pulou():
    player = faz_player()
    faixa = Track(title="Fria", video_id="f", requester_id=1)
    player.queue.current = faixa
    player.preparing = faixa
    pulos = []
    player.skip = lambda *a, **k: pulos.append(1)
    membro = type("M", (), {"id": 1})()

    status = asyncio.run(player.request_skip(membro))
    assert status is not None and "Fria" in status
    assert pulos == []
