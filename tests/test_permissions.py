"""Permissões das ações destrutivas (/stop, /clear, botão Sair): control_error."""
from types import SimpleNamespace

import messages as m
from apoio import faz_player
from core.player import GuildPlayer, control_error


class FakeChannel:
    def __init__(self, members: list) -> None:
        self.members = members


class FakeVoice:
    def __init__(self, channel: FakeChannel, connected: bool = True) -> None:
        self.channel = channel
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


def _player(voice: FakeVoice | None) -> GuildPlayer:
    player = faz_player(guild=SimpleNamespace(id=1, voice_client=voice))
    return player


def _member(channel: FakeChannel | None, manage_guild: bool = False, member_id: int = 1):
    return SimpleNamespace(
        id=member_id,
        bot=False,
        voice=SimpleNamespace(channel=channel) if channel is not None else None,
        guild_permissions=SimpleNamespace(manage_guild=manage_guild),
    )


def test_bot_out_of_call_has_nothing_to_protect():
    assert control_error(_player(None), _member(None)) is None
    disconnected = FakeVoice(FakeChannel([]), connected=False)
    assert control_error(_player(disconnected), _member(None)) is None


def test_member_outside_the_bots_channel_is_refused():
    channel = FakeChannel([])
    player = _player(FakeVoice(channel))
    assert control_error(player, _member(None)) == m.CTRL_SAME_CHANNEL
    assert control_error(player, _member(FakeChannel([]))) == m.CTRL_SAME_CHANNEL


def test_alone_with_the_bot_is_allowed():
    member = _member(None)
    channel = FakeChannel([member])
    member.voice = SimpleNamespace(channel=channel)
    assert control_error(_player(FakeVoice(channel)), member) is None


def test_accompanied_without_permission_is_refused():
    member = _member(None)
    other = _member(None, member_id=2)
    channel = FakeChannel([member, other])
    member.voice = SimpleNamespace(channel=channel)
    assert control_error(_player(FakeVoice(channel)), member) == m.CTRL_NO_PERMISSION


def test_accompanied_with_manage_guild_is_allowed():
    member = _member(None, manage_guild=True)
    other = _member(None, member_id=2)
    channel = FakeChannel([member, other])
    member.voice = SimpleNamespace(channel=channel)
    assert control_error(_player(FakeVoice(channel)), member) is None
