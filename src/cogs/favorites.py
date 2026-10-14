"""Favoritos e histórico por usuário: /fav /favs /playfavs /history /replay.

NÃO usar `from __future__ import annotations` neste arquivo: o py-cord
precisa das anotações `discord.Option(...)` como objetos reais — como
strings, o comando quebra na invocação (_raw_type deixa de ser classe).
"""
import random

import discord
from discord.ext import commands

import messages as m
from core.player import GuildPlayer, ensure_voice, get_player, texto_de_adicionada
from core.queue import Track


def _track_from_row(row, requester_id: int) -> Track:
    return Track(
        title=row["title"],
        artist=row["artist"],
        video_id=row["video_id"],
        duration=row["duration"],
        thumbnail=row["thumbnail"],
        requester_id=requester_id,
    )


class FavsView(discord.ui.View):
    """Dropdown dos favoritos: escolher toca (ou remove, no modo remoção)."""

    def __init__(self, bot: discord.Bot, rows: list, owner: discord.Member, removing: bool) -> None:
        super().__init__(timeout=120, disable_on_timeout=True)
        self.bot = bot
        self.rows = rows
        self.owner = owner
        self.removing = removing
        placeholder = m.FAVS_REMOVE_PLACEHOLDER if removing else m.FAVS_PLAY_PLACEHOLDER
        self._select = discord.ui.Select(
            placeholder=placeholder,
            options=[
                discord.SelectOption(
                    label=f"{i + 1}. {row['title']}"[:100],
                    description=(row["artist"] or "")[:100] or None,
                    value=str(i),
                )
                for i, row in enumerate(rows)
            ],
        )
        self._select.callback = self._chosen
        self.add_item(self._select)

    async def _chosen(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(m.FAVS_NOT_YOURS, ephemeral=True)
            return
        row = self.rows[int(self._select.values[0])]

        if self.removing:
            await self.bot.db.remove_favorite(
                self.owner.id, interaction.guild_id, row["video_id"]
            )
            await interaction.response.edit_message(
                content=m.FAV_REMOVED.format(track=row["title"]), view=None
            )
            self.stop()
            return

        member = interaction.user
        if member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(m.JOIN_VOICE_FIRST, ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        if player.is_playing and player.voice.channel != member.voice.channel:
            await interaction.response.send_message(m.OTHER_CHANNEL, ephemeral=True)
            return
        track = _track_from_row(row, member.id)
        self.stop()
        await interaction.response.edit_message(
            content=texto_de_adicionada(player, track), view=None
        )
        await player.connect(member.voice.channel)
        await player.enqueue([track], interaction.channel)


class Favorites(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.slash_command(name="fav", description="Favorita a música que está tocando agora")
    async def fav(self, ctx: discord.ApplicationContext) -> None:
        player = get_player(self.bot, ctx.guild)
        track = player.queue.current
        if track is None or not track.video_id:
            return await ctx.respond(m.NOTHING_PLAYING, ephemeral=True)
        added = await self.bot.db.add_favorite(ctx.author.id, ctx.guild.id, track)
        msg = m.FAV_ADDED.format(track=track.display) if added else m.FAV_ALREADY
        await ctx.respond(msg, ephemeral=True)

    @commands.slash_command(name="favs", description="Seus favoritos — escolhe uma para tocar (ou, com remover, para apagar)")
    async def favs(
        self,
        ctx: discord.ApplicationContext,
        remover: discord.Option(bool, "Modo remoção: escolher apaga em vez de tocar", default=False),
    ) -> None:
        rows = await self.bot.db.get_favorites(ctx.author.id, ctx.guild.id)
        if not rows:
            return await ctx.respond(m.FAVS_EMPTY, ephemeral=True)
        view = FavsView(self.bot, rows, ctx.author, removing=remover)
        await ctx.respond(m.FAVS_HEADER.format(count=len(rows)), view=view, ephemeral=True)

    @commands.slash_command(name="playfavs", description="Toca sua playlist de favoritos inteira")
    async def playfavs(
        self,
        ctx: discord.ApplicationContext,
        modo: discord.Option(
            str,
            "Adicionar à fila ou substituir a fila atual",
            choices=["adicionar", "substituir"],
            default="adicionar",
        ),
        embaralhar: discord.Option(bool, "Embaralhar os favoritos antes de tocar", default=False),
    ) -> None:
        await ctx.defer()  # ensure_voice conecta na voz e pode estourar os 3s
        rows = await self.bot.db.get_favorites(ctx.author.id, ctx.guild.id, limit=100)
        if not rows:
            return await ctx.respond(m.FAVS_EMPTY)
        player = await ensure_voice(ctx)
        if player is None:
            return
        tracks = [_track_from_row(row, ctx.author.id) for row in rows]
        if embaralhar:
            random.shuffle(tracks)
        if modo == "substituir":
            await ctx.respond(m.FAVS_PLAYING_REPLACE.format(count=len(tracks)))
            await player.replace(tracks, ctx.channel)
        else:
            await ctx.respond(m.FAVS_PLAYING.format(count=len(tracks)))
            await player.enqueue(tracks, ctx.channel)

    @commands.slash_command(name="history", description="As últimas 10 músicas que você pediu, e quando")
    async def history(self, ctx: discord.ApplicationContext) -> None:
        rows = await self.bot.db.get_history(ctx.author.id, ctx.guild.id, limit=10)
        if not rows:
            return await ctx.respond(m.HISTORY_EMPTY, ephemeral=True)
        lines = [
            f"`{i + 1}.` {row['title']}" + (f" — {row['artist']}" if row["artist"] else "")
            # <t:…:R> o Discord mostra como "há 2 horas", no fuso de quem lê
            + f" · <t:{int(row['played_at'])}:R>"
            for i, row in enumerate(rows)
        ]
        embed = discord.Embed(
            title=m.HISTORY_TITLE, description="\n".join(lines), color=discord.Color.blurple()
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @commands.slash_command(name="replay", description="Toca de novo a última música do seu histórico")
    async def replay(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer()  # ensure_voice conecta na voz e pode estourar os 3s
        row = await self.bot.db.get_last_played(ctx.author.id, ctx.guild.id)
        if row is None:
            return await ctx.respond(m.HISTORY_EMPTY)
        player: GuildPlayer | None = await ensure_voice(ctx)
        if player is None:
            return
        track = _track_from_row(row, ctx.author.id)
        await ctx.respond(m.REPLAY.format(track=track.display))
        await player.enqueue([track], ctx.channel)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Favorites(bot))
