"""Relato de falhas de playback: um resumo em vez de uma mensagem por faixa.

Antes: playlist de 50 do Spotify com o YouTube bloqueado = uma mensagem
"⚠️ Falha ao tocar ..." por faixa, com o erro cru do yt-dlp no chat.
"""
import asyncio

import pytest

import messages as m
from apoio import faz_player
from core.player import GuildPlayer
from core.queue import MusicQueue, Track


class FakeVoice:
    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return False

    def is_paused(self) -> bool:
        return False


class FakeGuild:
    id = 1

    def __init__(self, voice: FakeVoice) -> None:
        self.voice_client = voice


class FakePanel:
    async def ensure_visible(self, channel) -> None:
        pass

    async def refresh(self) -> None:
        pass


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


def _player(channel: FakeChannel | None = None) -> GuildPlayer:
    player = faz_player(guild=FakeGuild(FakeVoice()), channel=channel, panel=FakePanel())
    return player


def _fill(player: GuildPlayer, n: int) -> None:
    for i in range(n):
        player.queue.add(Track(title=f"Musica {i}", artist="A", video_id=f"v{i}"))


def _always_fail(player: GuildPlayer) -> None:
    async def fail(track):
        player._record_failure(track, RuntimeError("boom"))
        return False

    player._try_start = fail


def test_falha_unica_mantem_a_mensagem_detalhada():
    channel = FakeChannel()
    player = _player(channel)
    _fill(player, 1)
    _always_fail(player)

    asyncio.run(player.play_next())

    assert len(channel.sent) == 1
    assert "Musica 0" in channel.sent[0]


def test_varias_falhas_seguidas_viram_um_resumo_so():
    channel = FakeChannel()
    player = _player(channel)
    _fill(player, 4)
    _always_fail(player)

    asyncio.run(player.play_next())

    assert len(channel.sent) == 1, channel.sent
    assert "4" in channel.sent[0]


def test_resumo_sai_junto_com_o_aviso_do_breaker():
    channel = FakeChannel()
    player = _player(channel)
    player.autoplay_enabled = True
    _fill(player, 20)
    _always_fail(player)

    asyncio.run(player.play_next())

    # resumo das puladas + aviso do breaker; nada de 20 mensagens
    assert len(channel.sent) == 2, channel.sent
    assert channel.sent[-1] == m.PLAYBACK_BROKEN


def test_faixa_que_toca_fecha_o_resumo_e_nao_vaza_para_a_proxima():
    channel = FakeChannel()
    player = _player(channel)
    _fill(player, 5)
    calls = {"n": 0}

    async def two_fails_then_ok(track):
        calls["n"] += 1
        if calls["n"] <= 2:
            player._record_failure(track, RuntimeError("boom"))
            return False
        return True

    player._try_start = two_fails_then_ok
    asyncio.run(player.play_next())

    assert len(channel.sent) == 1
    assert "2" in channel.sent[0]  # as duas puladas antes da que tocou

    channel.sent.clear()
    player._try_start = lambda track: _true()
    asyncio.run(player.play_next(ignore_loop=True))
    assert channel.sent == []  # acumulado não sobrou da rodada anterior


async def _true() -> bool:
    return True


def test_sem_canal_de_texto_nao_explode():
    player = _player(None)
    _fill(player, 3)
    _always_fail(player)

    asyncio.run(player.play_next())  # não pode levantar


@pytest.mark.parametrize("erro,esperado", [
    ("ERROR: [youtube] x: The page needs to be reloaded.", "YouTube recusou"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "cookies"),
])
def test_resumo_usa_o_motivo_traduzido(monkeypatch, erro, esperado):
    channel = FakeChannel()
    player = _player(channel)
    _fill(player, 3)

    async def fail(video_id, **kwargs):
        raise RuntimeError(erro)

    monkeypatch.setattr("core.player.youtube.get_stream", fail)
    asyncio.run(player.play_next())

    assert esperado in channel.sent[0]
    assert "ERROR: [youtube]" not in channel.sent[0]  # erro cru não vaza pro chat


def test_falha_ao_publicar_o_resumo_nao_derruba_o_avanco():
    """O resumo roda no finally de play_next — não pode mascarar erro real."""
    class CanalQuebrado:
        async def send(self, content=None, **kwargs):
            raise RuntimeError("Discord fora do ar")

    player = _player(CanalQuebrado())
    _fill(player, 3)
    _always_fail(player)

    asyncio.run(player.play_next())  # não pode levantar
    assert player._failures == []  # e o acumulado foi drenado mesmo assim


def test_erro_real_do_avanco_nao_e_engolido_pelo_resumo():
    channel = FakeChannel()
    player = _player(channel)
    _fill(player, 3)

    async def explode(track):
        player._record_failure(track, RuntimeError("boom"))
        raise ValueError("erro real do avanço")

    player._try_start = explode

    with pytest.raises(ValueError, match="erro real"):
        asyncio.run(player.play_next())
    assert channel.sent  # o resumo saiu, mas a exceção original venceu


def test_faixa_que_toca_registra_o_tempo_de_cada_fase(monkeypatch, caplog):
    """Sem isto, "o bot demora pra tocar" era irreprodutível: nada media na VM."""
    player = _player(FakeChannel())
    _fill(player, 1)

    async def fake_stream(video_id, **kwargs):
        return {"duration": 10, "thumbnail": "", "stream_url": "http://x"}

    async def fake_build(info, track=None):
        pass

    monkeypatch.setattr("core.player.youtube.get_stream", fake_stream)
    monkeypatch.setattr(player, "_build_and_play", fake_build)
    monkeypatch.setattr(player, "_schedule_prefetch", lambda: None)

    with caplog.at_level("INFO", logger="core.player"):
        asyncio.run(player.play_next())

    assert "extração" in caplog.text and "ffmpeg" in caplog.text
    assert "Musica 0" in caplog.text


def test_cache_quente_aparece_no_log_como_tal(monkeypatch, caplog):
    """Distinguir extração fria de cache é o que prova se o prefetch funciona."""
    player = _player(FakeChannel())
    _fill(player, 1)

    async def instantaneo(video_id, **kwargs):
        return {"duration": 10, "thumbnail": "", "stream_url": "http://x"}

    monkeypatch.setattr("core.player.youtube.get_stream", instantaneo)
    monkeypatch.setattr(player, "_build_and_play", lambda info, track=None: _true())
    monkeypatch.setattr(player, "_schedule_prefetch", lambda: None)
    monkeypatch.setattr("core.player.youtube.stream_state", lambda vid: "no cache")

    with caplog.at_level("INFO", logger="core.player"):
        asyncio.run(player.play_next())

    assert "do cache" in caplog.text


# ── casamento Spotify → YouTube: primeiro medir ──────────────────────────────


def test_faixa_do_spotify_loga_a_diferenca_de_duracao(monkeypatch, caplog):
    """O bot toca o 1º resultado da busca. Se ele é um ao vivo de 7min para uma
    faixa de 3min, só quem estava na call sabia. O Δ no journal diz com que
    frequência isso acontece — antes de gastar esforço num casador melhor."""
    player = _player(FakeChannel())
    player.queue.add(Track(title="Evidências", artist="Chitãozinho & Xororó", duration=283))

    async def busca(query):
        return {"video_id": "abc", "title": "Evidências (Ao Vivo)", "artist": "Chitãozinho & Xororó",
                "duration": 421, "thumbnail": ""}

    async def stream(video_id, **kwargs):
        return {"duration": 421, "thumbnail": "", "stream_url": "http://x"}

    monkeypatch.setattr("core.player.youtube.search_one", busca)
    monkeypatch.setattr("core.player.youtube.get_stream", stream)
    monkeypatch.setattr(player, "_build_and_play", lambda info, track=None: _true())
    monkeypatch.setattr(player, "_schedule_prefetch", lambda: None)

    with caplog.at_level("INFO", logger="core.player"):
        asyncio.run(player.play_next())

    linha = next(r.getMessage() for r in caplog.records if "casamento" in r.getMessage())
    assert "Evidências (Ao Vivo)" in linha and "+138s" in linha


def test_duracao_que_vale_e_a_do_video_que_vai_tocar(monkeypatch):
    """A do Spotify (283s) ficava no track e o fim de faixa acusaria "CORTADA"
    ou deixaria de acusar, comparando com a duração de OUTRA gravação."""
    player = _player(FakeChannel())
    faixa = Track(title="Evidências", artist="X", duration=283)
    player.queue.add(faixa)

    async def busca(query):
        return {"video_id": "abc", "title": "Evidências", "artist": "X", "duration": 280, "thumbnail": ""}

    async def stream(video_id, **kwargs):
        return {"duration": 279, "thumbnail": "", "stream_url": "http://x"}

    monkeypatch.setattr("core.player.youtube.search_one", busca)
    monkeypatch.setattr("core.player.youtube.get_stream", stream)
    monkeypatch.setattr(player, "_build_and_play", lambda info, track=None: _true())
    monkeypatch.setattr(player, "_schedule_prefetch", lambda: None)

    asyncio.run(player.play_next())

    assert faixa.duration == 279
