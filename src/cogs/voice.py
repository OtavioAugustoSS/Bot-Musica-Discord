"""Modo de voz ("Arrodes"): /ouvir liga a escuta de comandos falados na call.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import logging

import discord
from discord.ext import commands

import messages as m
from core.listen import VoiceListener
from core.player import ensure_voice, get_player


log = logging.getLogger(__name__)


class Voice(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(
        name="ouvir",
        description='Liga/desliga o modo de voz: fale "Arrodes, toca…" na call (a cola tá no /help)',
    )
    async def ouvir(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()
        player = get_player(self.bot, ctx.guild)

        if player.listener is not None and player.listener.active:
            player.listener.stop()
            player.listener = None
            await ctx.respond(m.VOICE_STOPPED)
            return

        player = await ensure_voice(ctx)
        if player is None:
            return
        if player.voice is None or not player.voice.is_connected():
            await ctx.respond(m.VOICE_CONN_DROPPED)
            return
        listener = VoiceListener(player, ctx.channel)
        try:
            await listener.start()
        except Exception as exc:  # noqa: BLE001 — melhor que "pensando…" eterno
            # a pessoa vê o erro no chat; o journal não via nada
            log.warning("[%s] não consegui ligar a escuta: %r", ctx.guild.id, exc, exc_info=exc)
            listener.stop()
            await ctx.respond(m.VOICE_START_FAILED.format(error=exc))
            return
        player.listener = listener
        await ctx.respond(m.VOICE_LISTENING)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Voice(bot))
