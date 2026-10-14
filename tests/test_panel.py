"""Estados da UI do painel V2: botões e blocos de texto conforme o player."""
import asyncio

import discord

import messages as m
from core.panel import (
    PlayerPanel,
    footer_text,
    format_time,
    now_playing_text,
    queue_preview_text,
)
from core.queue import LoopMode, MusicQueue, Track


class StubVoice:
    def is_connected(self):
        return True


class StubPlayer:
    def __init__(self):
        self.queue = MusicQueue()
        self.autoplay_enabled = False
        self.voice = None
        self.playing = False
        self.paused = False
        self.text_channel = None
        self.elapsed = 30
        self.bot = None
        self.panel = None

    @property
    def is_playing(self):
        return self.playing

    @property
    def is_paused(self):
        return self.paused


def make_panel(**kwargs):
    player = StubPlayer()
    for key, value in kwargs.items():
        setattr(player, key, value)

    async def build():  # py-cord exige event loop ativo para criar Views
        return PlayerPanel(player)

    panel = asyncio.run(build())
    player.panel = panel
    return player, panel


def test_idle_disables_transport_buttons():
    _, panel = make_panel()
    view = panel.view
    assert view.btn_play.disabled
    assert view.btn_skip.disabled
    assert view.btn_fav.disabled
    assert view.btn_shuffle.disabled  # fila < 2
    assert view.btn_leave.disabled  # sem voz


def test_playing_enables_buttons_and_shows_pause_icon():
    player, panel = make_panel(playing=True, voice=StubVoice())
    player.queue.current = Track(title="x", video_id="v")
    panel.view.sync()
    assert not panel.view.btn_play.disabled
    assert str(panel.view.btn_play.emoji) == "⏸️"
    assert not panel.view.btn_leave.disabled


def test_paused_shows_play_icon():
    player, panel = make_panel(playing=True, paused=True, voice=StubVoice())
    panel.view.sync()
    assert str(panel.view.btn_play.emoji) == "▶️"


def test_loop_button_reflects_mode():
    player, panel = make_panel()
    view = panel.view
    assert view.btn_loop.style is discord.ButtonStyle.secondary

    player.queue.loop_mode = LoopMode.TRACK
    view.sync()
    assert str(view.btn_loop.emoji) == "🔂"
    assert view.btn_loop.style is discord.ButtonStyle.primary

    player.queue.loop_mode = LoopMode.QUEUE
    view.sync()
    assert str(view.btn_loop.emoji) == "🔁"
    assert view.btn_loop.style is discord.ButtonStyle.primary


def test_autoplay_button_reflects_state():
    player, panel = make_panel(autoplay_enabled=True)
    panel.view.sync()
    assert panel.view.btn_autoplay.style is discord.ButtonStyle.primary


def test_shuffle_enabled_with_two_or_more_upcoming():
    player, panel = make_panel()
    player.queue.add_many([Track(title="a"), Track(title="b")])
    panel.view.sync()
    assert not panel.view.btn_shuffle.disabled


def test_now_playing_text_idle():
    player, _ = make_panel()
    text = now_playing_text(player)
    assert m.PANEL_IDLE_TITLE in text
    assert m.PANEL_IDLE_SUBTITLE in text


def test_now_playing_text_with_track():
    player, _ = make_panel(playing=True, voice=StubVoice())
    player.queue.current = Track(
        title="Time", artist="Pink Floyd", video_id="v", duration=421, requester_id=42
    )
    text = now_playing_text(player)
    assert m.PANEL_PLAYING in text
    assert "Time" in text
    assert "Pink Floyd" in text
    assert "<@42>" in text
    assert "<t:" in text  # timestamp nativo que o cliente atualiza sozinho
    assert "7:01" in text  # duração da faixa

    player.paused = True
    assert m.PANEL_PAUSED in now_playing_text(player)


def test_queue_preview_text():
    player, _ = make_panel()
    assert queue_preview_text(player) is None
    player.queue.add_many([Track(title=f"t{i}") for i in range(5)])
    preview = queue_preview_text(player)
    assert m.PANEL_UP_NEXT.format(count=5) in preview
    assert "t0" in preview
    assert m.PANEL_MORE.format(count=2) in preview


def test_footer_text_only_shows_active_flags():
    player, _ = make_panel()
    assert footer_text(player) is None
    player.queue.loop_mode = LoopMode.TRACK
    player.autoplay_enabled = True
    footer = footer_text(player)
    assert m.FOOTER_LOOP.format(mode="faixa") in footer
    assert m.FOOTER_AUTOPLAY in footer


def test_format_time():
    assert format_time(75) == "1:15"
    assert format_time(3723) == "1:02:03"


def test_refresh_durante_o_show_nao_cria_dois_paineis():
    """`enqueue` → `ensure_visible` → `show()` apaga a mensagem e manda outra;
    no meio, o prefetch enche a rádio e chama `refresh()`, que via
    `message is None` e mandava um SEGUNDO painel. Serializado agora."""
    from unittest.mock import AsyncMock, MagicMock

    player, panel = make_panel()
    envios = []

    class Canal:
        id = 1
        last_message_id = None

        async def send(self, view=None):
            await asyncio.sleep(0.02)
            msg = MagicMock()
            msg.id = len(envios) + 1
            msg.channel = self
            msg.edit = AsyncMock()
            msg.delete = AsyncMock()
            envios.append(msg)
            return msg

    canal = Canal()
    player.text_channel = canal

    async def run():
        await asyncio.gather(panel.show(canal), panel.refresh())

    asyncio.run(run())

    assert len(envios) == 1
    assert panel.message is envios[0]
