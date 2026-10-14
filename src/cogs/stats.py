"""Ranking do servidor: /top — músicas e DJs mais tocados.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import time

import discord
from discord.ext import commands

_PERIODS = {"semana": 7 * 86400, "mês": 30 * 86400, "sempre": 0}
_MEDALS = ["🥇", "🥈", "🥉"]


def _rank(index: int) -> str:
    return _MEDALS[index] if index < len(_MEDALS) else f"`{index + 1}.`"


class Stats(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(
        name="top", description="Ranking do servidor: músicas e DJs mais tocados"
    )
    async def top(
        self,
        ctx: discord.ApplicationContext,
        periodo: discord.Option(
            str, "Período do ranking", choices=list(_PERIODS), default="semana"
        ),
    ) -> None:
        window = _PERIODS.get(periodo, _PERIODS["semana"])
        since = int(time.time()) - window if window else 0
        tracks = await self.bot.db.get_top_tracks(ctx.guild.id, since, limit=10)
        if not tracks:
            await ctx.respond(
                f"Ainda não tocou nada {'nessa ' + periodo if window else 'por aqui'} 🤷"
                " — manda um `/play`!",
                ephemeral=True,
            )
            return
        requesters = await self.bot.db.get_top_requesters(ctx.guild.id, since, limit=5)

        label = {"semana": "da semana", "mês": "do mês", "sempre": "de todos os tempos"}.get(
            periodo, "da semana"
        )
        embed = discord.Embed(title=f"🏆 Top {label}", color=0x1DB954)
        embed.add_field(
            name="🎵 Músicas mais tocadas",
            value="\n".join(
                f"{_rank(i)} {row['title']}"
                + (f" — {row['artist']}" if row["artist"] else "")
                + f" `{row['plays']}x`"
                for i, row in enumerate(tracks)
            )[:1024],
            inline=False,
        )
        if requesters:
            embed.add_field(
                name="🎧 DJs do servidor",
                value="\n".join(
                    f"{_rank(i)} <@{row['user_id']}> `{row['plays']} pedidos`"
                    for i, row in enumerate(requesters)
                )[:1024],
                inline=False,
            )
        await ctx.respond(embed=embed)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Stats(bot))
