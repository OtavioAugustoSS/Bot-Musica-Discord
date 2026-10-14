"""Comandos principais de música: /play, controles e fila.

NÃO usar `from __future__ import annotations` neste arquivo: o py-cord
precisa das anotações `discord.Option(...)` como objetos reais — como
strings, o comando quebra na invocação (_raw_type deixa de ser classe).
"""
import asyncio
import logging
import math

import discord
from discord.ext import commands

import config
import messages as m
from core.panel import format_time
from core.player import GuildPlayer, control_error, ensure_voice, texto_de_adicionada
from core.queue import LoopMode, Track
from services import youtube
from services.spotify_api import SpotifyService, is_algorithmic_playlist, parse_spotify_url

_KIND_LABEL = {"track": "faixa", "album": "álbum", "playlist": "playlist"}
_LOOP_POR_NOME = {"desligado": LoopMode.OFF, "faixa": LoopMode.TRACK, "fila": LoopMode.QUEUE}

log = logging.getLogger(__name__)


class SearchView(discord.ui.View):
    """Dropdown com os resultados da busca — quem buscou escolhe."""

    def __init__(
        self,
        results: list[dict],
        author: discord.Member,
        player: GuildPlayer,
        channel: discord.abc.Messageable,
        replace: bool = False,
    ) -> None:
        super().__init__(timeout=60, disable_on_timeout=True)
        self.results = results
        self.author = author
        self.player = player
        self.channel = channel
        self.replace = replace
        self._select = discord.ui.Select(
            placeholder=m.SEARCH_PLACEHOLDER,
            options=[
                discord.SelectOption(
                    label=r["title"][:100],
                    description=f"{r['artist']} • {format_time(r['duration'])}"[:100],
                    value=str(i),
                )
                for i, r in enumerate(results)
            ],
        )
        self._select.callback = self._chosen
        self.add_item(self._select)

    async def _chosen(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(m.SEARCH_NOT_YOURS, ephemeral=True)
            return
        result = self.results[int(self._select.values[0])]
        track = Track(
            title=result["title"],
            artist=result["artist"],
            video_id=result["video_id"],
            duration=result["duration"],
            thumbnail=result["thumbnail"],
            requester_id=self.author.id,
        )
        self.stop()
        if self.replace:
            await interaction.response.edit_message(
                content=m.REPLACED.format(count=1), view=None
            )
            await self.player.replace([track], self.channel)
        else:
            await interaction.response.edit_message(
                content=texto_de_adicionada(self.player, track), view=None
            )
            await self.player.enqueue([track], self.channel)


class QueueView(discord.ui.View):
    """Fila paginada: setas, menu de páginas (como o do /help) e remoção por dropdown.

    Quase sempre a fila é uma playlist de 50+: o menu leva direto a "41–50"."""

    PER_PAGE = 10
    _MAX_OPCOES = 25  # limite do Discord por menu

    def __init__(self, player: GuildPlayer) -> None:
        super().__init__(timeout=300, disable_on_timeout=True)
        self.player = player
        self.page = 0
        self._paginas = discord.ui.Select(placeholder=m.QUEUE_PAGE_PLACEHOLDER, row=1)
        self._paginas.callback = self._on_page
        self._remove = discord.ui.Select(placeholder=m.QUEUE_REMOVE_PLACEHOLDER, row=2)
        self._remove.callback = self._on_remove
        self.add_item(self._remove)
        self._sync()

    def _pages(self) -> int:
        return max(1, math.ceil(len(self.player.queue.upcoming) / self.PER_PAGE))

    def _sync(self) -> None:
        self.page = min(self.page, self._pages() - 1)
        start = self.page * self.PER_PAGE
        chunk = self.player.queue.upcoming[start : start + self.PER_PAGE]
        # a FAIXA de cada opção, não a posição: a fila anda enquanto o menu está aberto
        self._opcoes = list(chunk)
        self._remove.options = [
            discord.SelectOption(label=f"{start + i + 1}. {t.display}"[:100], value=str(i))
            for i, t in enumerate(chunk)
        ] or [discord.SelectOption(label="(fila vazia)", value="-1")]
        self._remove.disabled = not chunk
        self._sync_paginas()

    def _sync_paginas(self) -> None:
        """Uma opção por página; com mais de 25, a janela de 25 em volta da atual."""
        total, paginas = len(self.player.queue.upcoming), self._pages()
        if paginas <= 1:
            if self._paginas in self.children:
                self.remove_item(self._paginas)
            return
        if self._paginas not in self.children:
            self.add_item(self._paginas)
        inicio = min(max(0, self.page - self._MAX_OPCOES // 2), max(0, paginas - self._MAX_OPCOES))
        opcoes = []
        for pagina in range(inicio, min(paginas, inicio + self._MAX_OPCOES)):
            de = pagina * self.PER_PAGE + 1
            ate = min(total, de + self.PER_PAGE - 1)
            primeira = self.player.queue.upcoming[de - 1].display
            opcoes.append(discord.SelectOption(
                label=f"{de}–{ate}"[:100], description=f"começa com {primeira}"[:100],
                value=str(pagina), default=pagina == self.page,
            ))
        self._paginas.options = opcoes

    def build_embed(self) -> discord.Embed:
        queue = self.player.queue
        embed = discord.Embed(title="📜 Fila", color=discord.Color.blurple())
        if queue.current is not None:
            embed.add_field(
                name="Tocando agora", value=queue.current.display[:1000], inline=False
            )
        upcoming = queue.upcoming
        if upcoming:
            start = self.page * self.PER_PAGE
            embed.description = "\n".join(
                # duração 0 = ainda não extraída (link recém-pedido): não é "0:00"
                f"`{start + i + 1}.` {t.display}"
                f" `[{format_time(t.duration) if t.duration else '?:??'}]`"
                for i, t in enumerate(upcoming[start : start + self.PER_PAGE])
            )
            total = sum(t.duration for t in upcoming)
            embed.set_footer(
                text=f"Página {self.page + 1}/{self._pages()}"
                f" • {len(upcoming)} música(s) • {format_time(total)} no total"
            )
        elif queue.radio:
            embed.description = m.QUEUE_ONLY_RADIO
        else:
            embed.description = m.QUEUE_EMPTY
        # a rádio vive numa fila própria (só toca quando a da pessoa acaba):
        # aparece em bloco separado, para ninguém confundir com pedido
        if queue.radio:
            linhas = [f"`{i}.` {t.display}"[:100] for i, t in enumerate(queue.radio[:3], 1)]
            if len(queue.radio) > 3:  # sem "-#": embed não renderiza subtexto
                linhas.append(m.PANEL_MORE.format(count=len(queue.radio) - 3))
            embed.add_field(
                name=m.QUEUE_RADIO_HEADER.format(count=len(queue.radio)),
                value="\n".join(linhas)[:1024],
                inline=False,
            )
        return embed

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_page(self, interaction: discord.Interaction) -> None:
        self.page = int(self._paginas.values[0])
        await self._redraw(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def prev_page(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._redraw(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def next_page(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.page = min(self._pages() - 1, self.page + 1)
        await self._redraw(interaction)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        index = int(self._remove.values[0])
        target = self._opcoes[index] if 0 <= index < len(self._opcoes) else None
        # já saiu da fila (tocou, alguém tirou): nada a remover, só redesenha
        if target is not None and any(t is target for t in self.player.queue.upcoming):
            # remover música dos OUTROS é destrutivo; a própria, liberado
            if target.requester_id != interaction.user.id:
                err = control_error(self.player, interaction.user)
                if err is not None:
                    await interaction.response.send_message(err, ephemeral=True)
                    return
            self.player.queue.tira([target])
            self.player.queue_changed("remover pelo /queue")
        await self._redraw(interaction)
        await self.player.panel.refresh()


async def _descarta(tarefa) -> None:
    """Cancela trabalho adiantado que não vai ser usado, sem barulho no log.

    Uma tarefa cancelada e nunca aguardada vira "Task exception was never
    retrieved". A extração em si continua (services/youtube protege com
    shield) e o resultado ainda aquece o cache — o /play seguinte aproveita.
    """
    if tarefa is None:
        return
    tarefa.cancel()
    try:
        await tarefa
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — descarte
        pass


def _segue_em_background(tarefa: asyncio.Task) -> None:
    """Deixa a tarefa terminar sozinha e consome a exceção dela, se houver.

    A extração de um link continua depois de o /play já ter respondido (pelo
    oEmbed): é ela que aquece o cache. Se falhar, quem for tocar a faixa
    extrai de novo e avisa na hora certa — aqui só evitamos o "Task
    exception was never retrieved" no journal.
    """
    def _consome(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.debug("extração adiantada falhou: %r", t.exception())

    tarefa.add_done_callback(_consome)


class Music(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.spotify = (
            SpotifyService(
                config.SPOTIFY_CLIENT_ID,
                config.SPOTIFY_CLIENT_SECRET,
                config.SPOTIFY_TOKEN_CACHE,
            )
            if config.spotify_enabled()
            else None
        )

    @commands.slash_command(
        name="play",
        description="Toca uma música: nome, link do YouTube (vídeo/playlist) ou do Spotify (faixa/álbum/playlist)",
    )
    async def play(
        self,
        ctx: discord.ApplicationContext,
        busca: discord.Option(str, "Nome da música, link do YouTube ou link do Spotify"),
        modo: discord.Option(
            str,
            "Adicionar à fila ou substituir a fila inteira (sem sair da call)",
            choices=["adicionar", "substituir"],
            default="adicionar",
        ),
    ) -> None:
        # defer ANTES de tudo: a janela de resposta é 3s e conectar na voz /
        # buscar no YouTube estouram isso ("O aplicativo não respondeu")
        await ctx.defer()
        replace = modo == "substituir"
        spotify_ref = parse_spotify_url(busca)
        url_direta = spotify_ref is None and youtube.is_url(busca)

        if url_direta and not youtube.is_allowed_url(busca):  # anti-SSRF: só YouTube
            await ctx.respond(m.URL_NOT_ALLOWED)
            return

        video_id = playlist_id = None
        if url_direta:
            video_id = youtube.extract_video_id(busca)
            playlist_id = None if video_id else youtube.extract_playlist_id(busca)
            if not video_id and not playlist_id:
                # canal, /results, /feed…: o yt-dlp sairia listando e
                # extraindo tudo — minutos de pool preso e bot mudo
                await ctx.respond(m.URL_NOT_VIDEO)
                return
            if playlist_id and playlist_id.startswith("RD"):
                # mix ("rádio" do YouTube): montado por pessoa, não listável
                await ctx.respond(m.YT_PLAYLIST_MIX)
                return

        # A busca/extração NÃO depende da conexão de voz, e o handshake do
        # Discord leva de 0,4s a 5,9s (negociação DAVE, fora do nosso
        # controle). Em série os tempos somavam — ~5,4s + ~8,4s até o áudio
        # sair. Rodando junto, paga-se só o maior dos dois.
        rapido = None
        if video_id:
            adiantado = asyncio.create_task(youtube.get_stream(video_id))
            # título/canal/capa em ~0,3s: a resposta não espera a extração
            rapido = asyncio.create_task(youtube.quick_info(video_id))
        elif playlist_id:
            adiantado = asyncio.create_task(youtube.playlist_tracks(playlist_id))
        elif spotify_ref is not None:
            adiantado = (
                asyncio.create_task(self.spotify.fetch(*spotify_ref))
                if self.spotify is not None
                else None
            )
        else:
            adiantado = asyncio.create_task(youtube.search_songs(busca, limit=5))

        try:
            player = await ensure_voice(ctx)
        except BaseException:
            # connect que estoura/explode: o trabalho adiantado não pode
            # ficar órfão ("Task exception was never retrieved")
            await _descarta(adiantado)
            await _descarta(rapido)
            raise
        if player is None:
            await _descarta(adiantado)
            await _descarta(rapido)
            return

        if spotify_ref is not None:
            await self._play_spotify(ctx, player, *spotify_ref, replace=replace, fetch=adiantado)
            return
        if video_id:
            await self._play_video(ctx, player, adiantado, rapido, replace=replace)
            return
        if playlist_id:
            await self._play_youtube_playlist(ctx, player, adiantado, replace=replace)
            return

        results = await adiantado
        if not results:
            await ctx.respond(m.SEARCH_NOTHING.format(query=busca))
            return
        # enquanto a pessoa escolhe no dropdown, o 1º resultado (o escolhido
        # na maioria das vezes) já vai sendo extraído — com o pool ocioso
        youtube.prewarm(results[0]["video_id"])
        view = SearchView(results, ctx.author, player, ctx.channel, replace=replace)
        await ctx.respond(m.SEARCH_RESULTS.format(query=busca), view=view)

    async def _play_video(
        self,
        ctx: discord.ApplicationContext,
        player: GuildPlayer,
        extracao: asyncio.Task,
        rapido: asyncio.Task,
        *,
        replace: bool,
    ) -> None:
        """Link de vídeo: responde com os metadados rápidos (oEmbed) e deixa a
        extração terminar em background — ela aquece o cache para a hora de
        tocar. Só espera a extração se o oEmbed não souber do vídeo (privado,
        removido…), porque é ela que sabe dizer o erro certo."""
        info = await rapido
        # substituir é destrutivo (derruba a atual e limpa a fila): só depois
        # de a extração provar que o vídeo toca. O oEmbed responde 200 para
        # vídeo com restrição de idade/país/DRM — só a extração sabe.
        if replace:
            info = None
        if info is None:
            try:
                info = await extracao
            except Exception as exc:  # noqa: BLE001 — DRM/privado/removido/fora do ar
                log.warning("/play URL falhou: %r", exc)
                await ctx.respond(m.URL_FAIL)
                return
        else:
            _segue_em_background(extracao)
        track = Track(
            title=info["title"],
            artist=info["artist"],
            video_id=info["video_id"],
            duration=info["duration"],
            thumbnail=info["thumbnail"],
            requester_id=ctx.author.id,
        )
        if replace:
            await ctx.respond(m.REPLACED.format(count=1))
            await player.replace([track], ctx.channel)
        else:
            await ctx.respond(texto_de_adicionada(player, track))
            await player.enqueue([track], ctx.channel)

    async def _play_youtube_playlist(
        self,
        ctx: discord.ApplicationContext,
        player: GuildPlayer,
        listagem: asyncio.Task,
        *,
        replace: bool,
    ) -> None:
        try:
            name, metas, total = await listagem
        except Exception as exc:  # noqa: BLE001 — privada/inexistente/YouTube fora
            log.warning("/play playlist do YouTube falhou: %r", exc)
            await ctx.respond(m.YT_PLAYLIST_FAIL)
            return
        if not metas:
            await ctx.respond(m.YT_PLAYLIST_EMPTY)
            return
        tracks = [
            Track(
                title=meta["title"],
                artist=meta["artist"],
                video_id=meta["video_id"],
                duration=meta["duration"],
                thumbnail=meta["thumbnail"],
                requester_id=ctx.author.id,
            )
            for meta in metas
        ]
        note = m.YT_PLAYLIST_LIMIT_NOTE if total > youtube.PLAYLIST_MAX else ""
        if replace:
            await ctx.respond(m.REPLACED.format(count=len(tracks)))
            await player.replace(tracks, ctx.channel)
        else:
            await ctx.respond(m.YT_PLAYLIST_ADDED.format(name=name, count=len(tracks), note=note))
            await player.enqueue(tracks, ctx.channel)

    async def _play_spotify(
        self,
        ctx: discord.ApplicationContext,
        player: GuildPlayer,
        kind: str,
        spotify_id: str,
        replace: bool = False,
        fetch: "asyncio.Task | None" = None,
    ) -> None:
        if self.spotify is None:
            await ctx.respond(m.SPOTIFY_OFF)
            return
        try:
            # `fetch` já começou em paralelo com a conexão de voz (play)
            name, metas = await (fetch or self.spotify.fetch(kind, spotify_id))
        except Exception as exc:  # noqa: BLE001 — link inválido/privado/API fora
            log.warning("import Spotify falhou (%s %s): %r", kind, spotify_id, exc)
            if kind == "playlist" and is_algorithmic_playlist(spotify_id):
                await ctx.respond(m.SPOTIFY_ALGORITMICA)
            elif kind == "playlist":
                await ctx.respond(m.SPOTIFY_PLAYLIST_FAIL)
            else:
                await ctx.respond(m.SPOTIFY_LINK_FAIL)
            return
        if not metas:
            await ctx.respond(m.SPOTIFY_EMPTY)
            return
        tracks = [
            Track(
                title=m["title"],
                artist=m["artist"],
                duration=m["duration"],
                thumbnail=m["thumbnail"],
                requester_id=ctx.author.id,
            )
            for m in metas
        ]
        label = _KIND_LABEL[kind]
        note = m.SPOTIFY_LIMIT_NOTE if kind == "playlist" and len(tracks) == 100 else ""
        if replace:
            await ctx.respond(m.REPLACED.format(count=len(tracks)))
            await player.replace(tracks, ctx.channel)
        else:
            await ctx.respond(
                m.SPOTIFY_ADDED.format(
                    kind=label.capitalize(), name=name, count=len(tracks), note=note
                )
            )
            await player.enqueue(tracks, ctx.channel)

    @commands.slash_command(name="pause", description="Pausa a música")
    async def pause(self, ctx: discord.ApplicationContext) -> None:
        player = self._current_player(ctx)
        if player is None:
            return await ctx.respond(m.NOTHING_PLAYING, ephemeral=True)
        if player.is_paused:  # dizia "pausado" de novo, como se tivesse feito algo
            return await ctx.respond(m.ALREADY_PAUSED, ephemeral=True)
        player.pause()
        await ctx.respond(m.PAUSED, ephemeral=True)
        await player.panel.refresh()

    @commands.slash_command(name="resume", description="Retoma a música pausada")
    async def resume(self, ctx: discord.ApplicationContext) -> None:
        player = self._current_player(ctx)
        if player is None:
            return await ctx.respond(m.NOTHING_PLAYING, ephemeral=True)
        if not player.is_paused:
            return await ctx.respond(m.NOT_PAUSED, ephemeral=True)
        player.resume()
        await ctx.respond(m.RESUMED, ephemeral=True)
        await player.panel.refresh()

    @commands.slash_command(name="skip", description="Pula a música atual — quem não pediu ela abre votação")
    async def skip(self, ctx: discord.ApplicationContext) -> None:
        player = self._current_player(ctx)
        if player is None:
            return await ctx.respond(m.NOTHING_PLAYING, ephemeral=True)
        # ANTES de pular: depois, a fila avança na thread de áudio e a "próxima" já é outra
        proxima = player.queue.next_up()
        status = await player.request_skip(ctx.author)
        if status is not None:  # virou votação (ou não havia o que pular)
            return await ctx.respond(status, ephemeral=True)
        if proxima is not None:
            await ctx.respond(m.SKIPPED_NEXT.format(track=proxima.display), ephemeral=True)
        elif player.autoplay_enabled:  # a rádio ainda vai escolher a próxima
            await ctx.respond(m.SKIPPED, ephemeral=True)
        else:
            await ctx.respond(m.SKIPPED_END, ephemeral=True)

    @commands.slash_command(name="stop", description="Para tudo, limpa a fila e sai do canal")
    async def stop(self, ctx: discord.ApplicationContext) -> None:
        player = self._current_player(ctx)
        if player is None:
            return await ctx.respond(m.ALREADY_QUIET, ephemeral=True)
        err = control_error(player, ctx.author)
        if err is not None:
            return await ctx.respond(err, ephemeral=True)
        await ctx.respond(m.STOPPED)
        await player.stop(f"/stop de {ctx.author.id}")

    @commands.slash_command(name="clear", description="Limpa a fila (a música atual continua)")
    async def clear(self, ctx: discord.ApplicationContext) -> None:
        player = self._current_player(ctx)
        if player is None or not player.queue.upcoming:
            return await ctx.respond(m.CLEAR_EMPTY, ephemeral=True)
        err = control_error(player, ctx.author)
        if err is not None:
            return await ctx.respond(err, ephemeral=True)
        player.queue.upcoming.clear()
        player.queue_changed("/clear")  # a próxima agora é a rádio: buscar/adiantar já
        await ctx.respond(m.CLEARED)
        await player.panel.refresh()

    @commands.slash_command(name="queue", description="A fila: o que vem aí, o tempo total e um menu pra tirar música")
    async def queue(self, ctx: discord.ApplicationContext) -> None:
        from core.player import get_player

        player = get_player(self.bot, ctx.guild)
        view = QueueView(player)
        await ctx.respond(embed=view.build_embed(), view=view)

    @commands.slash_command(name="shuffle", description="Embaralha a fila (os pedidos por voz continuam na frente)")
    async def shuffle(self, ctx: discord.ApplicationContext) -> None:
        player = self._current_player(ctx)
        if player is None or not player.queue.upcoming:
            return await ctx.respond(m.SHUFFLE_EMPTY, ephemeral=True)
        err = control_error(player, ctx.author)
        if err is not None:
            return await ctx.respond(err, ephemeral=True)
        player.queue.shuffle()
        player.queue_changed("/shuffle")
        await ctx.respond(m.SHUFFLED, ephemeral=True)
        await player.panel.refresh()

    @commands.slash_command(name="loop", description="Loop: desligado, faixa ou fila — sem escolher, alterna entre os três")
    async def loop(
        self,
        ctx: discord.ApplicationContext,
        modo: discord.Option(
            str,
            "faixa repete a música atual; fila faz a fila girar (sem escolher, alterna)",
            choices=list(_LOOP_POR_NOME),
            required=False,
        ),
    ) -> None:
        player = self._current_player(ctx)
        if player is None:
            return await ctx.respond(m.NOTHING_PLAYING, ephemeral=True)
        # só alternava às cegas (e respondia em inglês: "Loop: track")
        player.queue.loop_mode = _LOOP_POR_NOME.get(modo) or player.queue.loop_mode.cycle()
        await ctx.respond(
            m.LOOP_CHANGED.format(mode=m.LOOP_NOMES[player.queue.loop_mode.value]), ephemeral=True
        )
        await player.panel.refresh()

    @commands.slash_command(name="remove", description="Tira uma música da fila pelo número que aparece no /queue")
    async def remove(
        self,
        ctx: discord.ApplicationContext,
        posicao: discord.Option(int, "Posição na fila (veja /queue)", min_value=1),
    ) -> None:
        player = self._current_player(ctx)
        if player is None:
            return await ctx.respond(m.NOTHING_PLAYING, ephemeral=True)
        upcoming = player.queue.upcoming
        target = upcoming[posicao - 1] if posicao <= len(upcoming) else None
        # remover música dos OUTROS é destrutivo; a própria, liberado
        if target is not None and target.requester_id != ctx.author.id:
            err = control_error(player, ctx.author)
            if err is not None:
                return await ctx.respond(err, ephemeral=True)
        removed = player.queue.remove(posicao)
        player.queue_changed("/remove")
        if removed is None:
            return await ctx.respond(m.REMOVE_INVALID.format(pos=posicao), ephemeral=True)
        await ctx.respond(m.REMOVED.format(track=removed.display), ephemeral=True)
        await player.panel.refresh()

    @commands.slash_command(name="seek", description="Vai para um ponto da música: 1:30 · 90 · +30 · -10")
    async def seek(
        self,
        ctx: discord.ApplicationContext,
        posicao: discord.Option(str, "Tempo: 1:30 · 90 (segundos) · +30 (avança) · -10 (volta)"),
    ) -> None:
        player = self._current_player(ctx)
        if player is None:
            return await ctx.respond(m.NOTHING_PLAYING, ephemeral=True)
        err = self._fora_do_canal(player, ctx.author)
        if err is not None:
            return await ctx.respond(err, ephemeral=True)
        # URL fora do cache = extração de novo (~5s na VM): passa dos 3s do Discord
        await ctx.defer(ephemeral=True)
        estado, destino = await player.pede_seek(posicao)
        atual = player.queue.current
        if estado == "ok":
            duracao = format_time(atual.duration) if atual and atual.duration else "?:??"
            await ctx.respond(m.SEEK_DONE.format(pos=format_time(destino), dur=duracao), ephemeral=True)
        elif estado == "falhou":
            await ctx.respond(m.SEEK_FAILED.format(pos=format_time(destino)), ephemeral=True)
        elif estado == "nada":
            await ctx.respond(m.NOTHING_PLAYING, ephemeral=True)
        elif estado == "alem":
            await ctx.respond(m.SEEK_BEYOND.format(dur=format_time(destino)), ephemeral=True)
        else:
            await ctx.respond(m.SEEK_INVALID, ephemeral=True)

    @commands.slash_command(
        name="volume", description="Volume do bot neste servidor (0 a 150; sem número mostra o atual)"
    )
    async def volume(
        self,
        ctx: discord.ApplicationContext,
        nivel: discord.Option(
            int, "De 0 a 150 (100 = original da música)", min_value=0, max_value=150, required=False
        ),
    ) -> None:
        from core.player import get_player

        player = get_player(self.bot, ctx.guild)
        await player._carrega_ajustes()
        if nivel is None:
            return await ctx.respond(m.VOLUME_NOW.format(volume=player.volume), ephemeral=True)
        err = self._fora_do_canal(player, ctx.author)
        if err is not None:
            return await ctx.respond(err, ephemeral=True)
        await ctx.defer()
        ficou = await player.set_volume(nivel)
        await ctx.respond(m.VOLUME_SET.format(volume=ficou))

    @staticmethod
    def _fora_do_canal(player: GuildPlayer, member: discord.Member) -> str | None:
        """Seek e volume mexem no que TODOS ouvem: só quem está na call do bot.
        (Sem a regra de DJ do /stop: é como o /pause, qualquer um da call pode.)"""
        if player.voice is None or not player.voice.is_connected():
            return None
        voice = getattr(member, "voice", None)
        if voice is None or voice.channel != player.voice.channel:
            return m.CTRL_SAME_CHANNEL
        return None

    @commands.slash_command(
        name="autoplay", description="Liga/desliga a rádio: quando a fila acaba, sigo com músicas parecidas"
    )
    async def autoplay(self, ctx: discord.ApplicationContext) -> None:
        from core.player import get_player

        player = get_player(self.bot, ctx.guild)
        player.set_autoplay(not player.autoplay_enabled)
        await ctx.respond(m.AUTOPLAY_ON if player.autoplay_enabled else m.AUTOPLAY_OFF)
        await player.panel.refresh()

    def _current_player(self, ctx: discord.ApplicationContext) -> GuildPlayer | None:
        """Player do servidor, apenas se o bot estiver conectado em voz."""
        from core.player import get_player

        player = get_player(self.bot, ctx.guild)
        if player.voice is None or not player.voice.is_connected():
            return None
        return player


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Music(bot))
