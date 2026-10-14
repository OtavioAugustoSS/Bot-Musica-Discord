"""Avanço de fila: guard anti-corrida do play_next e replace() sem desconectar."""
import asyncio

from apoio import faz_player
from core.player import GuildPlayer
from core.queue import LoopMode, MusicQueue, Track


class FakeVoice:
    """VoiceClient mínimo: distingue stop_playing (só música) de stop (tudo)."""

    def __init__(self, playing: bool) -> None:
        self.playing = playing
        self.stopped_playing = 0
        self.full_stops = 0

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return self.playing

    def is_paused(self) -> bool:
        return False

    def stop_playing(self) -> None:
        self.stopped_playing += 1

    def stop(self) -> None:
        self.full_stops += 1


class FakeGuild:
    id = 1  # skip() loga o guild

    def __init__(self, voice: FakeVoice) -> None:
        self.voice_client = voice


class FakePanel:
    async def ensure_visible(self, channel) -> None:
        pass

    async def refresh(self) -> None:
        pass


def _player(voice: FakeVoice) -> GuildPlayer:
    player = faz_player(guild=FakeGuild(voice), panel=FakePanel())
    return player


def test_play_next_is_noop_while_already_playing():
    # /play que esperou o lock durante a extração de outro start não pode
    # avançar a fila de novo ("Already playing audio" drenava tudo)
    player = _player(FakeVoice(playing=True))
    player.queue.add(Track(title="na fila", video_id="v1"))

    asyncio.run(player.play_next())

    assert player.queue.current is None
    assert len(player.queue.upcoming) == 1  # fila intacta


def test_replace_swaps_queue_without_disconnecting():
    voice = FakeVoice(playing=True)
    player = _player(voice)
    player.queue.current = Track(title="tocando", video_id="v0")
    player.queue.add(Track(title="antiga", video_id="v1"))

    novas = [Track(title="nova1", video_id="n1"), Track(title="nova2", video_id="n2")]
    asyncio.run(player.replace(novas, "canal"))

    assert [t.title for t in player.queue.upcoming] == ["nova1", "nova2"]
    assert voice.stopped_playing == 1  # parou SÓ a música…
    assert voice.full_stops == 0  # …sem desconectar nem matar a escuta


def test_replace_in_queue_loop_does_not_resurrect_old_track():
    player = _player(FakeVoice(playing=True))
    player.queue.loop_mode = LoopMode.QUEUE
    player.queue.current = Track(title="velha", video_id="v0")

    asyncio.run(player.replace([Track(title="nova", video_id="n1")], "canal"))

    # simula o _after_track: o avanço pega a fila nova, sem a velha voltar
    advanced = player.queue.advance(ignore_loop=True)
    assert advanced is not None and advanced.title == "nova"
    assert all(t.title != "velha" for t in player.queue.upcoming)


def test_play_next_circuit_breaker_stops_after_repeated_failures():
    # autoplay + yt-dlp quebrado virava loop eterno: falha → refila → falha…
    player = _player(FakeVoice(playing=False))
    player.autoplay_enabled = True
    for i in range(10):
        player.queue.add(Track(title=f"t{i}", video_id=f"v{i}"))

    async def always_fail(track):
        return False

    player._try_start = always_fail
    asyncio.run(player.play_next())

    assert player.autoplay_enabled is False  # breaker desligou o autoplay
    assert len(player.queue.upcoming) == 5  # consumiu só até o limite (5)
    assert player._consecutive_failures == 0  # pronto para o próximo /play


def test_play_next_failure_counter_resets_on_success():
    player = _player(FakeVoice(playing=False))
    player.autoplay_enabled = True
    for i in range(5):
        player.queue.add(Track(title=f"t{i}", video_id=f"v{i}"))
    attempts = []

    async def fail_four_then_succeed(track):
        attempts.append(track)
        return len(attempts) == 5

    player._try_start = fail_four_then_succeed
    asyncio.run(player.play_next())

    assert len(attempts) == 5  # 4 falhas + 1 sucesso, sem disparar o breaker
    assert player.autoplay_enabled is True
    assert player._consecutive_failures == 0


def test_replace_while_idle_starts_playback():
    player = _player(FakeVoice(playing=False))
    started = []

    async def fake_play_next(**kwargs):
        started.append(True)

    player.play_next = fake_play_next
    asyncio.run(player.replace([Track(title="nova", video_id="n1")], "canal"))

    assert started == [True]
