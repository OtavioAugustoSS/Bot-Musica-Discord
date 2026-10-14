"""Painel fixo do player — Components V2 (container estilo Spotify).

Layout: container com cor de destaque (verde tocando / cinza parado)
contendo seção com capa, prévia da fila e os botões:
  🔀 · ⏸/▶ · ⏭ · 🔁   |   ⭐ Favoritar · 🎲 Autoplay · 🚪 Sair
A cada mudança de estado o painel é reconstruído e editado na mesma
mensagem. Botões desabilitam quando não fazem sentido; toggles ficam
azuis quando ligados.

Sem barra de progresso: o chat do Discord não atualiza em tempo real e a
barra ficava sempre defasada. Em vez disso, um timestamp nativo
(<t:...:R> — "há 2 minutos") que o PRÓPRIO cliente atualiza ao vivo,
sem o bot editar nada.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord

import messages as m
from core.queue import LoopMode

if TYPE_CHECKING:
    from core.player import GuildPlayer

log = logging.getLogger(__name__)

COLOR_PLAYING = 0x1DB954  # verde Spotify
COLOR_IDLE = 0x2B2D31  # cinza discord

_LOOP_LABEL = {LoopMode.OFF: "off", LoopMode.TRACK: "faixa", LoopMode.QUEUE: "fila"}


def format_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def now_playing_text(player: "GuildPlayer") -> str:
    """Bloco principal do painel (markdown), tocando ou parado."""
    track = player.queue.current
    if track is None or not player.is_playing:
        preparando = getattr(player, "preparing", None)
        if preparando is not None:
            # extração fria em andamento (~8s na VM): parecia travamento
            titulo = m.PANEL_PREPARING_TITLE.format(track=preparando.display)
            return f"### {titulo}\n-# {m.PANEL_PREPARING_SUBTITLE}"
        return f"### {m.PANEL_IDLE_TITLE}\n{m.PANEL_IDLE_SUBTITLE}"
    state = m.PANEL_PAUSED if player.is_paused else m.PANEL_PLAYING
    title = f"[{track.title}]({track.url})" if track.url else track.title
    lines = [f"-# {state}", f"### {title}"]
    if track.artist:
        lines.append(f"**{track.artist}**")
    requester = f"<@{track.requester_id}>" if track.requester_id else m.PANEL_AUTOPLAY_TAG
    # <t:...:R> = "há X minutos", atualizado ao vivo pelo cliente do Discord
    started_unix = int(time.time() - player.elapsed)
    started = m.PANEL_STARTED.format(timestamp=f"<t:{started_unix}:R>")
    info = f"-# {m.PANEL_REQUESTED_BY.format(user=requester)}  •  {started}"
    if track.duration:
        info += f"  •  ⏱️ {format_time(track.duration)}"
    lines.append(info)
    return "\n".join(lines)


def queue_preview_text(player: "GuildPlayer") -> str | None:
    """Prévia do que vem — a fila da pessoa e, atrás dela, a rádio do autoplay.

    A rádio sai marcada com 🎲 justamente para não parecer escolha de
    ninguém: era o que confundia quando as duas viviam na mesma lista.
    """
    fila = player.queue.upcoming
    radio = player.queue.radio
    total = len(fila) + len(radio)
    if not total:
        return None
    lines = [f"-# {m.PANEL_UP_NEXT.format(count=total)}"]
    lines += [
        f"`{i}.` {'🎲 ' if t.from_autoplay else ''}{t.display}"[:97]
        for i, t in enumerate(player.queue.proximas(3), 1)
    ]
    if total > 3:
        lines.append(f"-# {m.PANEL_MORE.format(count=total - 3)}")
    return "\n".join(lines)


def footer_text(player: "GuildPlayer") -> str | None:
    flags = []
    if player.queue.loop_mode is not LoopMode.OFF:
        flags.append(m.FOOTER_LOOP.format(mode=_LOOP_LABEL[player.queue.loop_mode]))
    if player.autoplay_enabled:
        flags.append(m.FOOTER_AUTOPLAY)
    return f"-# {'  •  '.join(flags)}" if flags else None


class PlayerView(discord.ui.DesignerView):
    """View V2 (DesignerView) reconstruída a cada mudança de estado do player."""

    def __init__(self, player: "GuildPlayer") -> None:
        super().__init__(timeout=None)
        self.player = player

        def btn(callback, *, emoji=None, label=None, style, custom_id):
            button = discord.ui.Button(
                emoji=emoji, label=label, style=style, custom_id=custom_id
            )
            button.callback = callback
            return button

        secondary = discord.ButtonStyle.secondary
        self.btn_shuffle = btn(self._shuffle, emoji="🔀", style=secondary, custom_id="player:shuffle")
        self.btn_play = btn(self._pause_resume, emoji="⏸️", style=discord.ButtonStyle.primary, custom_id="player:pause")
        self.btn_skip = btn(self._skip, emoji="⏭️", style=secondary, custom_id="player:skip")
        self.btn_loop = btn(self._loop, emoji="🔁", style=secondary, custom_id="player:loop")
        self.btn_fav = btn(self._favorite, emoji="⭐", label=m.BTN_FAV, style=secondary, custom_id="player:fav")
        self.btn_autoplay = btn(self._autoplay, emoji="🎲", label=m.BTN_AUTOPLAY, style=secondary, custom_id="player:autoplay")
        self.btn_leave = btn(self._leave, emoji="🚪", label=m.BTN_LEAVE, style=discord.ButtonStyle.danger, custom_id="player:leave")
        self.sync()

        track = player.queue.current
        playing = player.is_playing and track is not None

        items: list[discord.ui.Item] = []
        header = discord.ui.TextDisplay(now_playing_text(player))
        if playing and track.thumbnail:
            items.append(
                discord.ui.Section(header, accessory=discord.ui.Thumbnail(track.thumbnail))
            )
        else:
            items.append(header)
        preview = queue_preview_text(player)
        if preview:
            items.append(discord.ui.Separator())
            items.append(discord.ui.TextDisplay(preview))
        items.append(discord.ui.Separator())
        items.append(
            discord.ui.ActionRow(self.btn_shuffle, self.btn_play, self.btn_skip, self.btn_loop)
        )
        items.append(discord.ui.ActionRow(self.btn_fav, self.btn_autoplay, self.btn_leave))
        footer = footer_text(player)
        if footer:
            items.append(discord.ui.TextDisplay(footer))

        self.add_item(
            discord.ui.Container(*items, colour=COLOR_PLAYING if playing else COLOR_IDLE)
        )

    def sync(self) -> None:
        """Ajusta ícones, cores e habilitação conforme o estado do player."""
        player = self.player
        playing = player.is_playing
        primary = discord.ButtonStyle.primary
        secondary = discord.ButtonStyle.secondary

        self.btn_play.emoji = "▶️" if player.is_paused else "⏸️"
        self.btn_play.disabled = not playing
        self.btn_skip.disabled = not playing
        self.btn_fav.disabled = not playing
        self.btn_shuffle.disabled = len(player.queue.upcoming) < 2

        mode = player.queue.loop_mode
        self.btn_loop.emoji = "🔂" if mode is LoopMode.TRACK else "🔁"
        self.btn_loop.style = secondary if mode is LoopMode.OFF else primary

        self.btn_autoplay.style = primary if player.autoplay_enabled else secondary
        self.btn_leave.disabled = player.voice is None or not player.voice.is_connected()

    async def _redraw(self, interaction: discord.Interaction) -> None:
        """Responde a interação com um painel recém-construído."""
        view = PlayerView(self.player)
        self.player.panel.view = view
        await interaction.response.edit_message(view=view)

    async def _pause_resume(self, interaction: discord.Interaction) -> None:
        if self.player.is_paused:
            self.player.resume()
        else:
            self.player.pause()
        await self._redraw(interaction)

    async def _skip(self, interaction: discord.Interaction) -> None:
        status = await self.player.request_skip(interaction.user)
        if status is not None:
            await interaction.response.send_message(status, ephemeral=True)
        else:
            await interaction.response.defer()  # painel atualiza na próxima faixa

    async def _shuffle(self, interaction: discord.Interaction) -> None:
        from core.player import control_error  # import tardio: player importa panel

        err = control_error(self.player, interaction.user)
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        self.player.queue.shuffle()
        self.player.queue_changed("botão embaralhar")  # o /shuffle e o de voz já faziam
        await self._redraw(interaction)

    async def _loop(self, interaction: discord.Interaction) -> None:
        self.player.queue.loop_mode = self.player.queue.loop_mode.cycle()
        await self._redraw(interaction)

    async def _autoplay(self, interaction: discord.Interaction) -> None:
        self.player.set_autoplay(not self.player.autoplay_enabled)
        await self._redraw(interaction)

    async def _favorite(self, interaction: discord.Interaction) -> None:
        track = self.player.queue.current
        if track is None or not track.video_id:
            await interaction.response.send_message(m.NOTHING_PLAYING, ephemeral=True)
            return
        added = await self.player.bot.db.add_favorite(
            interaction.user.id, self.player.guild.id, track
        )
        msg = m.FAV_ADDED.format(track=track.display) if added else m.FAV_ALREADY
        await interaction.response.send_message(msg, ephemeral=True)

    async def _leave(self, interaction: discord.Interaction) -> None:
        from core.player import control_error  # import tardio: player importa panel

        err = control_error(self.player, interaction.user)
        if err is not None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer()
        await self.player.stop(f"botão sair de {interaction.user.id}")


class PlayerPanel:
    """Mensagem-player por servidor, reconstruída a cada mudança de estado."""

    REPOST_COOLDOWN_S = 10.0  # delete+send tem bucket apertado (~5/s por canal)

    def __init__(self, player: "GuildPlayer") -> None:
        self.player = player
        self.view = PlayerView(player)
        self.message: discord.Message | None = None
        self._reposted_at = -1e9  # sempre elegível no primeiro show
        # show() apaga e reenvia (dois awaits); um refresh() no meio via
        # `message is None` e mandava um SEGUNDO painel. Acontecia de verdade:
        # enqueue → ensure_visible → show, e o prefetch (que o enqueue
        # dispara) enche a rádio e chama refresh ~0,5s depois.
        self._lock = asyncio.Lock()

    async def show(self, channel: discord.abc.Messageable) -> None:
        """Cria (ou recria) a mensagem do painel no canal."""
        async with self._lock:
            await self._show_locked(channel)

    async def _show_locked(self, channel: discord.abc.Messageable) -> None:
        await self._delete_message()
        self.view = PlayerView(self.player)
        try:
            self.message = await channel.send(view=self.view)
        except discord.HTTPException as exc:
            # sem permissão no canal, 5xx do Discord: fica sem painel até a próxima
            # atualização — e não derruba quem chamou (o início de uma faixa)
            log.warning("painel: não consegui mandar a mensagem (%s %s)", exc.status, exc.text)
            return
        self._reposted_at = time.monotonic()

    async def ensure_visible(self, channel: discord.abc.Messageable) -> None:
        """Garante o painel como ÚLTIMA mensagem do canal.

        Se o chat andou (pedidos novos, conversa), o painel antigo é apagado
        e reenviado embaixo — ninguém precisa rolar para achar o player.
        Reenvio no máximo 1x por cooldown: um burst de /play virava um ciclo
        delete+send por pedido e o rate limit atrasava o canal inteiro.
        """
        async with self._lock:
            stale = (
                self.message is None
                or self.message.channel.id != getattr(channel, "id", None)
                or getattr(channel, "last_message_id", None) != self.message.id
            )
            cooled = (time.monotonic() - self._reposted_at) >= self.REPOST_COOLDOWN_S
            if stale and (cooled or self.message is None):
                await self._show_locked(channel)
            else:
                await self._refresh_locked()

    async def refresh(self) -> None:
        async with self._lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        if self.message is None:
            if self.player.text_channel is not None:
                await self._show_locked(self.player.text_channel)
            return
        self.view = PlayerView(self.player)
        try:
            await self.message.edit(view=self.view)
        except discord.NotFound:
            self.message = None
            if self.player.text_channel is not None:
                await self._show_locked(self.player.text_channel)
        except discord.HTTPException:
            pass  # rate limit ou similar: a próxima atualização corrige

    async def destroy(self) -> None:
        """Remove o painel do canal (shutdown: os botões morreriam no restart)."""
        async with self._lock:
            await self._delete_message()

    async def _delete_message(self) -> None:
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass
            self.message = None
