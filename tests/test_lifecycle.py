"""Auto-leave por canal vazio: agenda, cancela e só sai se continuar vazio."""
import asyncio
from types import SimpleNamespace

import core.player as player_mod
from apoio import faz_player
from core.player import GuildPlayer
from core.queue import MusicQueue


class FakeChannel:
    def __init__(self) -> None:
        self.members: list = []


class FakeVoice:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.disconnected = 0

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return False

    def is_paused(self) -> bool:
        return False

    def stop(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected += 1


class FakePanel:
    async def refresh(self) -> None:
        pass


def _player(voice: FakeVoice) -> GuildPlayer:
    player = faz_player(guild=SimpleNamespace(id=1, voice_client=voice), panel=FakePanel())
    return player


def test_leaves_when_channel_stays_empty(monkeypatch):
    monkeypatch.setattr(player_mod, "EMPTY_LEAVE_S", 0.01)
    voice = FakeVoice(FakeChannel())

    async def run():
        player = _player(voice)
        player.schedule_empty_leave()
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert voice.disconnected == 1


def test_does_not_leave_if_someone_returns(monkeypatch):
    monkeypatch.setattr(player_mod, "EMPTY_LEAVE_S", 0.01)
    channel = FakeChannel()
    voice = FakeVoice(channel)

    async def run():
        player = _player(voice)
        player.schedule_empty_leave()
        channel.members = [SimpleNamespace(bot=False)]  # alguém voltou a tempo
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert voice.disconnected == 0


def test_cancel_empty_leave_aborts_the_timer(monkeypatch):
    monkeypatch.setattr(player_mod, "EMPTY_LEAVE_S", 0.01)
    voice = FakeVoice(FakeChannel())

    async def run():
        player = _player(voice)
        player.schedule_empty_leave()
        player.cancel_empty_leave()
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert voice.disconnected == 0


def test_manual_stop_cancels_pending_leave(monkeypatch):
    monkeypatch.setattr(player_mod, "EMPTY_LEAVE_S", 0.01)
    voice = FakeVoice(FakeChannel())

    async def run():
        player = _player(voice)
        player.schedule_empty_leave()
        await player.stop()  # /stop manual: o timer não pode disparar depois
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert voice.disconnected == 1  # só o do stop manual, não um segundo do timer


class VozReconectando(FakeVoice):
    """O py-cord no meio de um handshake de reconexão: ainda não conectado."""

    def __init__(self) -> None:
        super().__init__(FakeChannel())
        self.forcas: list[bool] = []

    def is_connected(self) -> bool:
        return False

    async def disconnect(self, *, force: bool = False) -> None:
        self.forcas.append(force)


def test_stop_durante_reconexao_derruba_a_reconexao(caplog):
    """Sem isto, o stop no meio do handshake não desconectava nada: o py-cord
    terminava a reconexão depois e o bot ficava na call sem player nem escuta."""
    voice = VozReconectando()

    async def run():
        player = _player(voice)
        player.listener = SimpleNamespace(stop=lambda: None)
        with caplog.at_level("INFO"):
            await player.stop("queda de voz")

    asyncio.run(run())
    assert voice.forcas == [True]
    assert "parando (queda de voz)" in caplog.text  # escuta ligada é motivo de log
