"""O que os comandos de barra RESPONDEM — revisão de 21/09/2026, pedida pelo Otavio
junto com o /help novo ("veja o que pode melhorar no conteúdo dos comandos").

Nada aqui é recurso novo: é o bot dizendo o que já sabe. O /play respondia só "na
fila!" (em que posição? toca quando?), o /skip não dizia o que vinha, o /loop
respondia em inglês ("Loop: track") e só alternava às cegas, o /pause dizia
"pausado" mesmo já estando, o /history não dizia quando, e um título de anime com 4
dubladoras no campo do artista ocupava 3 linhas em toda mensagem.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import messages as m
from apoio import faz_player
from cogs.favorites import Favorites
from cogs.music import Music, texto_de_adicionada
from core.queue import LoopMode, Track


def _player(tocando: bool = True, pausado: bool = False, posicao: int = 60):
    voz = MagicMock()
    voz.is_connected.return_value = True
    voz.is_playing.return_value = tocando and not pausado
    voz.is_paused.return_value = pausado
    player = faz_player(guild=MagicMock(voice_client=voz))
    player.guild.id = 1
    if tocando:
        player.queue.current = Track(title="Atual", artist="A", video_id="atual", duration=180)
        player._started_at = 1.0
        player._source = MagicMock(posicao_s=float(posicao))
    player.panel = MagicMock()
    player.panel.refresh = AsyncMock()
    return player, voz


def _ctx():
    ctx = MagicMock()
    ctx.respond = AsyncMock()
    ctx.author.id = 7
    return ctx


def _cog(player) -> Music:
    cog = Music(MagicMock())
    cog._current_player = lambda ctx: player
    return cog


NOVA = Track(title="Evidências", artist="Chitãozinho & Xororó", video_id="ev", duration=279)


# ── /play: em que posição entrou e em quanto tempo toca ─────────────────────


def test_play_com_o_bot_parado_so_diz_que_entrou():
    player, _ = _player(tocando=False)

    assert texto_de_adicionada(player, NOVA) == m.ADDED.format(track=NOVA.display)


def test_play_com_fila_diz_a_posicao_e_em_quanto_tempo_toca():
    player, _ = _player(posicao=60)  # faltam 2:00 da atual
    player.queue.add_many([Track(title="B", video_id="b", duration=200), Track(title="C", video_id="c", duration=100)])

    assert texto_de_adicionada(player, NOVA) == m.ADDED_POS.format(track=NOVA.display, pos=3, eta="7 min")


def test_play_com_a_fila_vazia_e_a_proxima():
    player, _ = _player(posicao=150)  # faltam 30s

    assert texto_de_adicionada(player, NOVA) == m.ADDED_POS.format(
        track=NOVA.display, pos=1, eta="menos de 1 min")


def test_play_sem_saber_a_duracao_de_alguma_nao_inventa_tempo():
    player, _ = _player()
    player.queue.add(Track(title="do Spotify, ainda sem vídeo", duration=0))

    assert texto_de_adicionada(player, NOVA) == m.ADDED_POS_NO_ETA.format(track=NOVA.display, pos=2)


def test_play_com_a_faixa_em_loop_nao_promete_hora():
    player, _ = _player()
    player.queue.loop_mode = LoopMode.TRACK

    assert texto_de_adicionada(player, NOVA) == m.ADDED_POS_NO_ETA.format(track=NOVA.display, pos=1)


def test_fila_longa_mostra_horas():
    player, _ = _player(posicao=0)
    player.queue.add_many([Track(title=f"M{i}", video_id=f"m{i}", duration=240) for i in range(20)])

    assert "1h23" in texto_de_adicionada(player, NOVA)  # 3min + 80min


# ── /skip diz o que vem ─────────────────────────────────────────────────────


def test_skip_diz_qual_e_a_proxima():
    player, _ = _player()
    player.queue.add(NOVA)
    player.request_skip = AsyncMock(return_value=None)
    ctx = _ctx()

    asyncio.run(_cog(player).skip.callback(_cog(player), ctx))

    ctx.respond.assert_awaited_once_with(m.SKIPPED_NEXT.format(track=NOVA.display), ephemeral=True)


def test_skip_com_a_fila_vazia_e_sem_radio_avisa_que_acabou():
    player, _ = _player()
    player.autoplay_enabled = False
    player.request_skip = AsyncMock(return_value=None)
    ctx = _ctx()

    asyncio.run(_cog(player).skip.callback(_cog(player), ctx))

    ctx.respond.assert_awaited_once_with(m.SKIPPED_END, ephemeral=True)


def test_skip_que_virou_votacao_mostra_a_votacao():
    player, _ = _player()
    player.queue.add(NOVA)
    player.request_skip = AsyncMock(return_value="🗳️ 1/2")
    ctx = _ctx()

    asyncio.run(_cog(player).skip.callback(_cog(player), ctx))

    ctx.respond.assert_awaited_once_with("🗳️ 1/2", ephemeral=True)


# ── /pause e /resume dizem a verdade ────────────────────────────────────────


def test_pause_ja_pausado_nao_finge_que_pausou():
    player, _ = _player(pausado=True)
    player.pause = MagicMock()
    ctx = _ctx()

    asyncio.run(_cog(player).pause.callback(_cog(player), ctx))

    ctx.respond.assert_awaited_once_with(m.ALREADY_PAUSED, ephemeral=True)
    player.pause.assert_not_called()


def test_resume_sem_estar_pausado():
    player, _ = _player()
    player.resume = MagicMock()
    ctx = _ctx()

    asyncio.run(_cog(player).resume.callback(_cog(player), ctx))

    ctx.respond.assert_awaited_once_with(m.NOT_PAUSED, ephemeral=True)
    player.resume.assert_not_called()


# ── /loop: escolher o modo, e responder em português ────────────────────────


def test_loop_com_modo_escolhido_vai_direto_nele():
    player, _ = _player()
    ctx = _ctx()

    asyncio.run(_cog(player).loop.callback(_cog(player), ctx, "fila"))

    assert player.queue.loop_mode is LoopMode.QUEUE
    ctx.respond.assert_awaited_once_with(m.LOOP_CHANGED.format(mode=m.LOOP_NOMES["queue"]), ephemeral=True)


def test_loop_sem_modo_continua_alternando():
    player, _ = _player()
    ctx = _ctx()

    asyncio.run(_cog(player).loop.callback(_cog(player), ctx, None))

    assert player.queue.loop_mode is LoopMode.TRACK
    assert "faixa" in ctx.respond.await_args.args[0] and "track" not in ctx.respond.await_args.args[0]


# ── /history diz quando ─────────────────────────────────────────────────────


def test_history_diz_quando_cada_uma_tocou():
    bot = MagicMock()
    bot.db.get_history = AsyncMock(return_value=[
        {"title": "Evidências", "artist": "Chitãozinho & Xororó", "played_at": 1790000000},
        {"title": "Sem artista", "artist": "", "played_at": 1789990000},
    ])
    ctx = _ctx()

    asyncio.run(Favorites(bot).history.callback(Favorites(bot), ctx))

    descricao = ctx.respond.await_args.kwargs["embed"].description
    assert descricao.splitlines() == [
        "`1.` Evidências — Chitãozinho & Xororó · <t:1790000000:R>",
        "`2.` Sem artista · <t:1789990000:R>",
    ]


# ── artista com meia dúzia de nomes ─────────────────────────────────────────


def test_display_nao_despeja_todos_os_artistas():
    """Journal de 21/09: '不可思議のカルテ — Mai Sakurajima(CV:Asami Seto), Tomoe Koga(CV:Nao
    Toyama), Rio Futaba(CV:Atsumi Tanezaki), Nodoka Toyohama(CV:Maaya Uchida)' em
    TODA mensagem, log e linha do painel."""
    faixa = Track(
        title="Fukashigi no KARTE",
        artist="Mai Sakurajima(CV:Asami Seto), Tomoe Koga(CV:Nao Toyama), Rio Futaba(CV:Atsumi Tanezaki),"
        " Nodoka Toyohama(CV:Maaya Uchida)",
    )

    assert faixa.display == "Fukashigi no KARTE — Mai Sakurajima(CV:Asami Seto), Tomoe Koga(CV:Nao Toyama) e mais 2"
    assert Track(title="Meu Coração Deu PT", artist="Wesley Safadão, Matheus e Kauan").display == (
        "Meu Coração Deu PT — Wesley Safadão, Matheus e Kauan")
    assert Track(title="Só título").display == "Só título"
    assert "Nodoka" in faixa.search_query  # a busca continua com todos


# ── a cola de voz que o bot manda no chat ───────────────────────────────────


def test_colas_de_voz_cabem_numa_mensagem_e_apontam_para_o_help():
    for texto in (m.VOICE_HELLO, m.VOICE_LISTENING):
        assert len(texto) <= 1500
        assert "/help" in texto
    for novidade in ("agora", "pula 3", "toca a terceira", "volume", "segundo"):
        assert novidade in m.VOICE_HELLO
