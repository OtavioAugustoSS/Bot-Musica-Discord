"""Player por servidor: conexão de voz, playback (yt-dlp → FFmpeg) e avanço de fila."""
from __future__ import annotations

import asyncio
import io
import logging
import math
import time
from dataclasses import dataclass, field

import discord

import config
import messages as m
from core.audio import (
    BufferedAudioSource,
    FonteDaFaixa,
    FonteOpus,
    NumpyVolumeTransformer,
    PacotesProntos,
    SaidaDoFfmpeg,
    duracao_do_estalo_s,
    efeito_em_opus,
)
from core.autoplay import fetch_autoplay_tracks
from core.intents import VOLUME_MAX, parse_posicao
from core.panel import PlayerPanel, format_time
from core.queue import LoopMode, MusicQueue, Track
from services import youtube

log = logging.getLogger(__name__)

_RECENT_LIMIT = 50  # memória anti-repetição do autoplay
_FAILURE_LIMIT = 5  # falhas seguidas de playback até desistir (circuit breaker)
# quantas faixas adiantar. Extrair custa ~8,3s na VM e o pool serializa,
# então 2 cobre dois /skip seguidos sem virar fila longa de trabalho.
_PREFETCH_DEPTH = 2
# a fila PEDIDA vai mais fundo: com os comandos de voz por posição, "toca a
# terceira", "pula três" e "pula pra quinta" caíam todas fora das 2 adiantadas —
# 5,4 a 8,6s até trocar no teste de 21/09, contra 1,1 a 1,5s com a faixa no
# cache. A extração hoje leva ~4,0s (eram 8,3s quando o 2 foi escolhido), então
# 4 faixas ocupam o worker o mesmo tempo que 2 ocupavam. Em regime nada muda:
# a janela anda uma faixa por música. A rádio segue em 2 — ninguém pede "a
# quarta" dela, e ela é refeita a cada pedido.
_ADIANTA_PEDIDAS = 4
EMPTY_LEAVE_S = 120  # segundos sozinho na call até sair (cogs/lifecycle.py)
# tocou menos que isto e acabou sozinha: o FFmpeg morreu na largada (em 20/09,
# 403 do YouTube ao abrir a URL) — não é uma faixa que terminou
_MORTE_NA_LARGADA_S = 5.0
# acabou faltando mais que isto para o fim: o stream caiu no meio e o FFmpeg
# desistiu (rw_timeout, reconexões esgotadas). Margem larga porque a duração
# do YouTube Music e a do arquivo divergem em alguns segundos.
_CORTE_TOLERADO_S = 15.0
# o normal é ~1,3s (FFmpeg abrir o stream + 0,6s de pré-buffer); acima disto
# a demora entra na linha de fim de faixa
_PRIMEIRO_AUDIO_LENTO_S = 2.0
# o volume de sempre no caminho PCM. No Opus direto o áudio é COPIADO e sai no
# volume original do YouTube (decisão do Otavio, 20/09/2026).
_VOLUME_PCM = 0.8
# troca de volume no meio da faixa: o FFmpeg novo abre ESTE tanto à frente da
# posição atual e entra quando a música chega lá (abrir leva ~0,3s na VM)
_FOLGA_DA_TROCA_S = 3.0
_ESPERA_DA_TROCA_S = 8.0  # teto para o FFmpeg novo (seek/volume) dar áudio
_FIM_PROXIMO_S = 8.0  # faltando menos que isto, o volume novo fica para a próxima faixa
# vídeos que já mandaram pacote Opus sem os 20ms que o py-cord assume: da
# próxima vez vão recodificados. Em 33 streams medidos na VM nenhum mandou — é
# cinto de segurança, e por isso só em memória.
_SEM_COPIA: set[str] = set()
# fonte da PRÓXIMA faixa montada antes de a atual acabar: vale por este tempo.
# Música pausada por meia hora nos últimos segundos deixa o FFmpeg da próxima
# com a conexão parada esse tempo todo — melhor abrir outro.
_PRONTA_VALE_S = 600.0
# quanto o corte manual espera a fonte pré-montada encher (ver prepara_o_corte)
_ESPERA_DO_CORTE_S = 1.5
_ESPERA_DA_ESCOLHIDA_S = 4.0


@dataclass
class _Tocada:
    """O que o fim da faixa precisa saber sobre a faixa que ACABOU.

    Vai amarrado ao callback `after` na hora do play: ler `self._source` no
    fim pegava a fonte da faixa SEGUINTE quando um /play chegava entre o fim
    da thread de áudio e o callback.
    """

    track: Track | None
    fonte: FonteDaFaixa
    saida: SaidaDoFfmpeg | None
    # marcadas por skip()/stop() NESTA faixa. Eram flags do player lidas na hora
    # do callback: um /play que chegasse entre o skip e o `after` (thread de
    # áudio, até 50ms depois) zerava a flag ao montar a faixa nova, e a pulada
    # virava "MORREU NA LARGADA" — WARNING falso e um ponto no disjuntor.
    pulada: bool = False
    parada: bool = False
    acabou: bool = False  # o `after` do py-cord já disparou


@dataclass
class _Pronta:
    """A fonte da PRÓXIMA faixa, montada enquanto a atual tocava os últimos segundos."""

    track: Track
    info: dict
    pulmao: BufferedAudioSource
    saida: SaidaDoFfmpeg | None
    via: str
    feita_em: float = field(default_factory=time.monotonic)


class GuildPlayer:
    """Um player por servidor (guild)."""

    def __init__(self, bot: discord.Bot, guild: discord.Guild, *, panel=None) -> None:
        """`panel`: só os testes passam (tests/apoio.py::faz_player). Era por
        causa do PlayerPanel — que monta uma View do Discord — que 14 arquivos
        de teste criavam o player com __new__ e repetiam esta lista de
        atributos à mão; cada atributo novo eram 14 edições."""
        self.bot = bot
        self.guild = guild
        self.queue = MusicQueue()
        self.autoplay_enabled = False
        self.skip_votes: set[int] = set()
        self.text_channel: discord.abc.Messageable | None = None
        self._started_at: float = 0.0
        self._paused_at: float | None = None
        self._paused_total: float = 0.0
        self._tocada: _Tocada | None = None  # a faixa que o py-cord está tocando
        # /volume do servidor, em % (guild_settings no banco; lido ao conectar)
        self.volume = 100
        self._ajustes_lidos = False
        self._troca_vez = 0  # seek/volume em andamento: só a ÚLTIMA troca de fonte vale
        self._pronta: _Pronta | None = None  # a próxima faixa já montada (ver _prepara_a_proxima)
        self._pronta_task: asyncio.Task | None = None
        self._stopping = False
        self._consecutive_failures = 0
        self._advance_lock = asyncio.Lock()
        self._prefetch_task: asyncio.Task | None = None
        self._empty_task: asyncio.Task | None = None  # timer do auto-leave
        self._autoplay_task: asyncio.Task | None = None  # play_next ao ligar a rádio parado
        # faixa cuja extração fria está rodando agora — o painel mostra
        # "preparando…" em vez de parecer travado por ~8s
        self.preparing: Track | None = None
        self._recent_ids: list[str] = []
        # a rádio sai da ÚLTIMA MÚSICA PEDIDA, não da última tocada (core/autoplay)
        self._semente_radio: str | None = None
        # faixas puladas na rodada atual de play_next — viram UMA mensagem no
        # fim, não uma por faixa (playlist de 50 com o YouTube fora do ar
        # enchia o chat de 50 avisos com o erro cru do yt-dlp)
        self._failures: list[tuple[str, str]] = []
        self.listener = None  # VoiceListener do modo de voz (cogs/voice.py)
        # som de escuta (wake word)
        self._source = None  # a FonteDaFaixa que está tocando
        self._via = ""  # por onde o áudio dela vai: direto | recodificado | pcm
        self._so_efeito = False  # o py-cord está tocando SÓ o estalo (bot parado na call)
        self._efeito_vez = 0  # qual estalo é o atual — o fim de um antigo não mexe no novo
        self._efeito_ate = 0.0  # monotonic em que o estalo atual termina
        # faixas que morreram na largada
        self._mortes_seguidas = 0  # disjuntor: só zera quando uma faixa toca de verdade
        self._retentada: Track | None = None  # a que já teve a segunda chance
        # por último: o painel lê o estado do player (sync) já na construção
        self.panel = panel if panel is not None else PlayerPanel(self)

    # --- estado ---

    @property
    def voice(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def is_playing(self) -> bool:
        # o estalo tocando sozinho não é música: enqueue/play_next precisam
        # começar a faixa mesmo assim
        if self._so_efeito:
            return False
        return bool(self.voice and (self.voice.is_playing() or self.voice.is_paused()))

    @property
    def is_paused(self) -> bool:
        return bool(self.voice and self.voice.is_paused())

    @property
    def elapsed(self) -> int:
        """Em que ponto da música estamos: destino do último seek + áudio entregue.

        Era relógio de parede desde o `voice.play()` — contava a abertura do
        FFmpeg, o silêncio de pulmão vazio e o estalo de escuta, e com seek nem
        teria como estar certo. O relógio fica de reserva para fonte sem posição.
        """
        if not self._started_at:
            return 0
        posicao = getattr(self._source, "posicao_s", None)
        if isinstance(posicao, (int, float)):
            return int(posicao)
        end = self._paused_at or time.monotonic()
        return int(end - self._started_at - self._paused_total)

    def listeners(self) -> list[int]:
        """Ids de quem está ouvindo (só as contas importam: votação, canal vazio)."""
        if not self.voice or not self.voice.channel:
            return []
        return ouvintes(self.voice.channel)

    def votes_needed(self) -> int:
        return max(1, math.ceil(len(self.listeners()) / 2))

    # --- conexão ---

    async def _carrega_ajustes(self) -> None:
        """O /volume do servidor, uma vez por processo. Falha de banco não
        impede ninguém de ouvir música: fica o padrão."""
        if self._ajustes_lidos:
            return
        self._ajustes_lidos = True
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        try:
            self.volume = await db.get_volume(self.guild.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] não li o volume do servidor (%r) — fica 100%%", self.guild.id, exc)

    async def connect(self, channel: discord.VoiceChannel) -> None:
        await self._carrega_ajustes()
        if self.voice and self.voice.is_connected():
            if self.voice.channel != channel:
                log.info("[%s] mudando de canal de voz: %s → %s", self.guild.id, self.voice.channel, channel)
                await self.voice.move_to(channel)
            return
        if self.voice is not None:  # voice client zumbi (ex.: pós-desconexão) trava o connect
            log.warning("[%s] voice client zumbi — derrubando antes de reconectar", self.guild.id)
            await self.voice.disconnect(force=True)
        inicio = time.monotonic()
        try:
            await channel.connect()
        except Exception as exc:
            # o handshake de voz é a parte que o bot não controla (DAVE, região):
            # quando falha, o /play morre e só sobrava o traceback genérico
            log.warning(
                "[%s] não consegui entrar em %s depois de %.1fs: %r",
                self.guild.id, channel, time.monotonic() - inicio, exc,
            )
            raise
        log.info(
            "[%s] entrei em %s em %.1fs (%d pessoa(s) no canal)",
            self.guild.id, channel, time.monotonic() - inicio, len(self.listeners()),
        )

    # --- controle ---

    async def enqueue(
        self, tracks: list[Track], text_channel: discord.abc.Messageable, *, na_frente: bool = False,
    ) -> None:
        """`na_frente`: pedido por voz — entra como a próxima, atrás só dos outros
        pedidos por voz que ainda não tocaram (MusicQueue.poe_na_frente)."""
        if na_frente:
            for track in tracks:
                posicao = self.queue.poe_na_frente(track, quem=track.requester_id)
                # sem isto o journal não dizia O QUE a busca por voz tinha achado
                # nem onde pôs (teste de 21/09: só "fila mudou (pedido): próxima …")
                log.info(
                    "[%s] pedido por voz: %s entra na %dª posição de %d",
                    self.guild.id, track.display, posicao, len(self.queue.todas()),
                )
        else:
            self.queue.add_many(tracks)
        self.text_channel = text_channel
        # com a fila vazia (ou só rádio) a pedida vira a PRÓXIMA — e o
        # adiantamento já rodou no início da faixa atual. Sem reprogramar, a
        # transição pagava os ~8,3s de extração em silêncio.
        self.queue_changed("pedido")
        # painel sempre desce para o fim do chat quando chega pedido novo — em
        # PARALELO com o começo da faixa: reenviar o painel são ~0,5s de API
        # do Discord (delete + send) que não dependem em nada do áudio, e o
        # play_next ficava esperando por eles (journal de 20/09: 0,47s entre
        # "fila mudou" e o início da busca). O lock do painel já serializa o
        # refresh que o _try_start dispara no meio.
        painel = asyncio.create_task(self.panel.ensure_visible(text_channel))
        try:
            if not self.is_playing:
                await self.play_next()
        finally:
            await painel  # erro do painel continua chegando a quem chamou

    async def replace(self, tracks: list[Track], text_channel: discord.abc.Messageable) -> None:
        """Troca a fila inteira SEM desconectar (nem derrubar a escuta).

        É o caminho do "/play modo:substituir" — antes disso a única forma
        de trocar de playlist era /stop (que tirava o bot da call).
        """
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
            self._prefetch_task = None
        self.queue.upcoming.clear()
        self.queue.radio.clear()  # substituir é substituir tudo, rádio inclusa
        self.queue.add_many(tracks)
        self.text_channel = text_channel
        if self.is_playing:
            # a atual não pode voltar nem no loop de fila — é substituição
            self.queue.current = None
            self.skip()  # stop_playing → _after_track → play_next pega a fila nova
            await self.panel.ensure_visible(text_channel)
            return
        # parado: painel e começo da faixa juntos (ver enqueue)
        painel = asyncio.create_task(self.panel.ensure_visible(text_channel))
        try:
            await self.play_next()
        finally:
            await painel

    async def play_next(
        self,
        *,
        ignore_loop: bool = False,
        de_novo: Track | None = None,
        morta: Track | None = None,
    ) -> None:
        """Avança a fila até conseguir tocar uma faixa (pula as que falharem).

        `de_novo`: a faixa atual morreu na largada e ganha UMA segunda chance
        antes de a fila andar. `morta`: a que morreu de novo — entra no resumo
        de falhas desta rodada para a pessoa saber por que a música não veio.
        """
        async with self._advance_lock:
            # um /play que esperou o lock durante a extração (~2s) de outro
            # start chegaria aqui com a faixa já tocando — e o voice.play()
            # duplicado ("Already playing audio") drenaria a fila inteira
            if self.is_playing:
                return
            self._failures.clear()
            if morta is not None:
                self._failures.append((morta.display, m.TRACK_DIED_EARLY))
            try:
                await self._advance_until_playing(ignore_loop, de_novo)
            finally:
                await self._report_failures()

    async def _advance_until_playing(self, ignore_loop: bool, de_novo: Track | None = None) -> None:
        while True:
            if not self.voice or not self.voice.is_connected():
                self._started_at = 0.0
                return
            if de_novo is not None and de_novo is self.queue.current:
                track = de_novo  # a mesma faixa outra vez: a fila não anda
            else:
                track = self.queue.advance(ignore_loop=ignore_loop)
            de_novo = None
            ignore_loop = True  # tentativas seguintes sempre avançam
            if track is not None and track.from_autoplay and not self.autoplay_enabled:
                # rádio desligada no meio do caminho: faixa de rádio não toca
                # — inclusive a atual voltando pelo loop de fila, que senão
                # repetia para sempre
                self.queue.radio.clear()
                self.queue.current = None
                track = None
            if track is None and self.autoplay_enabled:
                novas = await fetch_autoplay_tracks(self._recent_ids, semente=self._semente_radio)
                if self.autoplay_enabled:  # pode ter sido desligada durante o await
                    self.queue.add_radio(novas)
                    track = self.queue.advance(ignore_loop=True)
            if track is None:
                self._started_at = 0.0
                await self._descarta_a_pronta()  # a fila acabou: ninguém vai consumir
                await self.panel.refresh()
                return
            if await self._try_start(track):
                self._consecutive_failures = 0
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= _FAILURE_LIMIT:
                # sem isso, autoplay + yt-dlp quebrado = loop eterno
                # martelando o YouTube e spammando o canal a cada falha
                await self._desiste("falhas seguidas de extração")
                return

    async def _desiste(self, motivo: str) -> None:
        """Disjuntor: para de tentar, desliga a rádio e avisa o canal."""
        log.error(
            "[%s] disjuntor: %d %s — parei de tentar tocar (rádio desligada)",
            self.guild.id, _FAILURE_LIMIT, motivo,
        )
        self._consecutive_failures = 0
        self._mortes_seguidas = 0
        await self._descarta_a_pronta()
        self.set_autoplay(False)  # e leva junto o que a rádio enfileirou
        self._started_at = 0.0
        await self._report_failures()
        if self.text_channel is not None:
            try:
                await self.text_channel.send(m.PLAYBACK_BROKEN)
            except discord.HTTPException:
                pass
        await self.panel.refresh()

    def _record_failure(self, track: Track, exc: BaseException) -> None:
        """Guarda a falha para o resumo (o traceback vai para o log na hora)."""
        log.warning("[%s] falha ao tocar %s: %r", self.guild.id, track.display, exc)
        self._failures.append((track.display, youtube.friendly_error(exc)))
        self._sintoma("extracao", f"{track.display}: {youtube.friendly_error(exc)}")

    def _sintoma(self, nome: str, detalhe: str) -> None:
        """Um sinal de que o YouTube mudou (services/alerts.Sintomas): passou do
        limiar na última hora, o dono é avisado e o canário do yt-dlp é pedido."""
        vigia = getattr(self.bot, "sintomas", None)
        if vigia is None or not vigia.conta(nome):
            return
        tarefa = asyncio.create_task(vigia.avisa(nome, detalhe))
        tarefa.add_done_callback(self._loga_falha_de_task)

    def _confere_o_formato(self, track: Track, info: dict) -> None:
        """Áudio que não é Opus (itag 18: mp4 com vídeo, AAC) toca — recodificado,
        com mais banda e pior som — mas é o aviso de que o client bom caiu."""
        acodec = info.get("acodec")
        if acodec and acodec != "opus":
            self._sintoma("formato", f"{track.display}: {youtube.descreve_audio(info)}")

    async def _report_failures(self) -> None:
        """Publica UMA mensagem sobre as faixas puladas nesta rodada."""
        failures, self._failures = self._failures, []
        if not failures or self.text_channel is None:
            return
        if len(failures) == 1:
            track, reason = failures[0]
            text = m.TRACK_FAILED.format(track=track, error=reason)
        else:
            # o motivo mais comum explica a rodada inteira; os nomes de duas
            # faixas bastam para a pessoa saber de onde veio
            reasons = [r for _, r in failures]
            reason = max(set(reasons), key=reasons.count)
            examples = ", ".join(t for t, _ in failures[:2])
            text = m.TRACKS_FAILED.format(
                count=len(failures), error=reason, examples=examples
            )
        try:
            await self.text_channel.send(text)
        except Exception:  # noqa: BLE001 — roda no finally de play_next
            # aviso cosmético NUNCA pode mascarar o erro real de playback nem
            # abortar o avanço da fila: qualquer falha aqui morre no log
            log.debug("[%s] não consegui publicar o resumo de falhas", self.guild.id)

    async def _try_start(self, track: Track) -> bool:
        # cronometragem por fase: "o bot demora pra tocar" era irreproduzível
        # porque nada media nada na VM. Sai uma linha por faixa no journal.
        marcos = [time.monotonic()]
        try:
            if track.video_id is None:  # importação Spotify: resolve o vídeo agora
                if not await self._acha_o_video(track):
                    raise LookupError("não achei essa música no YouTube")
            marcos.append(time.monotonic())

            estado = youtube.stream_state(track.video_id)
            na_fila = youtube._YTDLP_POOL.pending()  # quantos jobs esperavam o worker
            # a fonte desta faixa pode já estar montada (a anterior avisou que
            # estava acabando): serve se for ESTA faixa, viva e recente
            pronta = await self._pega_a_pronta(track)
            if pronta is None and estado != "no cache":
                # vai levar segundos: o painel avisa o que está acontecendo
                self.preparing = track
                await self._refresca_o_painel()
            info = pronta.info if pronta is not None else await youtube.get_stream(track.video_id)
            # a duração que vale é a do vídeo que VAI TOCAR: a do Spotify é de
            # outra gravação, e é com ela que o fim de faixa decide "CORTADA"
            track.duration = info["duration"] or track.duration
            track.thumbnail = track.thumbnail or info["thumbnail"]
            self._confere_o_formato(track, info)
            marcos.append(time.monotonic())

            fresca = (0.0, 0)
            if pronta is not None:
                self._via = pronta.via
                pronta.pulmao.marca_inicio()  # a espera pela vez não é demora do áudio
                self._toca(pronta.pulmao, pronta.saida, track)
            else:
                fresca = await youtube.espera_stream_abrir(info)
            try:
                if pronta is None:
                    await self._build_and_play(info, track)
            except discord.ClientException:
                raise  # "Already playing"/desconectado: URL nova não resolve
            except Exception as exc:
                if not self.voice or not self.voice.is_connected():
                    raise  # /stop ou expulsão no meio: não é URL vencida
                # info veio do cache e a URL pode ter vencido: UMA fresca
                log.warning(
                    "[%s] não consegui montar o áudio de %s (%r) — extraindo de novo",
                    self.guild.id, track.display, exc,
                )
                youtube.invalidate(track.video_id)
                info = await youtube.get_stream(track.video_id, use_cache=False)
                fresca = await youtube.espera_stream_abrir(info)
                await self._build_and_play(info, track)

            marcos.append(time.monotonic())
            self.preparing = None  # tocando: o painel volta ao normal
            busca, extracao, ffmpeg = (b - a for a, b in zip(marcos, marcos[1:]))
            log.info(
                "[%s] tocando %s%s em %.1fs (busca %.1fs, extração %.1fs %s%s,"
                " ffmpeg %.1fs, perfil %s, %s)%s",
                self.guild.id, track.display,
                " [autoplay]" if track.from_autoplay else "",
                marcos[-1] - marcos[0],
                busca, extracao,
                "fonte já pronta" if pronta is not None
                else "do cache" if estado == "no cache" else estado,
                f" com {na_fila} na fila do pool" if na_fila and estado != "no cache" else "",
                ffmpeg, youtube.active_profile(),
                f"{youtube.descreve_audio(info)} {self._via}".rstrip(),
                # a fase "ffmpeg" inclui esta espera: o googlevideo recusa a
                # URL no primeiro ~1s de vida dela
                f" — URL fresca só abriu depois de {fresca[0]:.1f}s ({fresca[1]}x 403)"
                if fresca[1] else "",
            )

            self._remember(track.video_id)
            self._ancora_radio(track)
            self._schedule_prefetch()
            await self._depois_de_comecar(track)
            return True
        except Exception as exc:  # noqa: BLE001 — qualquer falha: avisa e tenta a próxima
            # registra SEMPRE: sem text_channel (ex.: autoplay pós-restart) o
            # erro desaparecia por completo
            self.preparing = None
            self._record_failure(track, exc)
            return False

    async def _depois_de_comecar(self, track: Track) -> None:
        """Histórico e painel: a música JÁ está tocando. Falha aqui (Discord fora,
        sem permissão no canal, banco travado) contava como falha da faixa — a
        fila avançava por cima dela e, na 5ª, o disjuntor desligava a rádio."""
        try:
            if track.requester_id:
                await self.bot.db.add_history(track.requester_id, self.guild.id, track)
        except Exception:  # noqa: BLE001
            log.warning("[%s] não consegui gravar o histórico de %s", self.guild.id, track.display, exc_info=True)
        await self._refresca_o_painel()

    async def _refresca_o_painel(self) -> None:
        """O painel é cortesia: Discord fora nunca pode derrubar uma faixa."""
        try:
            await self.panel.refresh()
        except Exception:  # noqa: BLE001
            log.warning("[%s] não consegui atualizar o painel", self.guild.id, exc_info=True)

    async def _acha_o_video(self, track: Track) -> bool:
        """Faixa importada do Spotify (sem vídeo): busca no YouTube e anota o casamento.

        O bot fica com o 1º resultado. Se ele for um ao vivo de 7min para uma
        faixa de 3min, só quem estava na call sabia: o Δ de duração no journal
        diz com que frequência isso acontece ANTES de se gastar esforço num
        casador melhor (pontuar 5 resultados por duração e por live/remix/cover).
        """
        found = await youtube.search_one(track.search_query)
        if found is None:
            log.warning("[%s] casamento: nada no YouTube para %r", self.guild.id, track.search_query)
            return False
        do_spotify = track.duration
        track.video_id = found["video_id"]
        track.duration = found["duration"] or track.duration
        track.thumbnail = track.thumbnail or found["thumbnail"]
        if do_spotify and found["duration"]:
            log.info(
                "[%s] casamento: %r → %s — %s (Δduração %+ds)",
                self.guild.id, track.search_query, found["title"], found["artist"],
                found["duration"] - do_spotify,
            )
        return True

    def _abre_ffmpeg(
        self, info: dict, track: Track | None, saida: SaidaDoFfmpeg, desde_s: float = 0.0
    ) -> tuple[discord.AudioSource, str]:
        """O FFmpeg da faixa a partir de `desde_s`, e por onde o áudio vai.

        Bloqueante (Popen): chamar por `asyncio.to_thread`. Medido na VM, em ms
        de CPU por segundo de áudio: "pcm" 118, "recodificado" 43, "direto" 17.
        """
        if not config.AUDIO_OPUS_DIRETO:
            antes = youtube.FFMPEG_BEFORE + (f" -ss {desde_s:.2f}" if desde_s > 0 else "")
            ffmpeg = discord.FFmpegPCMAudio(
                info["stream_url"],
                before_options=antes,
                options=youtube.FFMPEG_OPTIONS,
                stderr=saida.escrita,
            )
            return NumpyVolumeTransformer(ffmpeg, volume=_VOLUME_PCM * self.volume / 100), "pcm"
        # copiar só o que o Discord aceita como está: Opus 48 kHz estéreo em
        # pacotes de 20ms, no volume original. itag 18/AAC, extração antiga sem
        # o dado e /volume diferente de 100% recodificam.
        copia = (
            self.volume == 100
            and youtube.serve_para_opus_direto(info)
            and (track is None or track.video_id not in _SEM_COPIA)
        )
        fonte = FonteOpus(
            info["stream_url"],
            before_options=youtube.FFMPEG_BEFORE,
            copia=copia,
            volume=self.volume / 100,
            inicio_s=desde_s,
            stderr=saida.escrita,
        )
        return fonte, "direto" if copia else "recodificado"

    async def _monta(
        self, info: dict, track: Track | None, desde_s: float = 0.0
    ) -> tuple[BufferedAudioSource, SaidaDoFfmpeg, str]:
        """FFmpeg + pulmão da faixa a partir de `desde_s` — o começo, ou um seek."""
        saida = SaidaDoFfmpeg(f"{self.guild.id} {track.display if track else '?'}"[:80])
        try:
            # abrir o FFmpeg é um subprocess.Popen — bloqueante no Windows
            ffmpeg, via = await asyncio.to_thread(self._abre_ffmpeg, info, track, saida, desde_s)
        finally:
            saida.comeca()  # com ou sem FFmpeg: solta o pipe (o leitor sai no EOF)
        return BufferedAudioSource(ffmpeg, segundos=config.AUDIO_BUFFER_S), saida, via

    async def _build_and_play(self, info: dict, track: Track | None = None) -> None:
        """Monta a fonte (FFmpeg fora do event loop) e começa a tocar.

        O buffer é a camada de FORA: a thread dele também paga o numpy do
        volume, tirando esse trabalho da thread de áudio, que na VM de um
        núcleo disputa CPU com a extração e com a escuta.
        """
        pulmao, saida, self._via = await self._monta(info, track)
        self._toca(pulmao, saida, track)

    def _toca(self, pulmao: BufferedAudioSource, saida: SaidaDoFfmpeg | None, track: Track | None) -> None:
        """Entrega a fonte (recém-montada, ou a que já estava pronta) ao py-cord."""
        source = FonteDaFaixa(pulmao)
        tocada = _Tocada(track=track, fonte=source, saida=saida)
        pulmao.quando_acabar(lambda: self._fim_proximo(tocada))
        self._source = source
        self._tocada = tocada
        self.skip_votes.clear()
        self._started_at = time.monotonic()
        self._paused_at = None
        self._paused_total = 0.0
        if self._so_efeito:
            # "toca X" com o bot parado: a música não espera o estalo acabar
            self._so_efeito = False
            self.voice.stop_playing()
        try:
            self.voice.play(
                source, after=lambda erro: self._after_track(erro, tocada), signal_type="music"
            )
        except Exception:
            # "Already playing"/desconectou no meio: sem isto o FFmpeg e a
            # thread do pulmão ficavam vivos, baixando uma faixa que ninguém ouve
            source.cleanup()
            raise

    # --- a próxima faixa montada antes da hora ---

    def _fim_proximo(self, tocada: _Tocada) -> None:
        """Da thread do pulmão: o FFmpeg da faixa terminou de baixar e ainda há
        até AUDIO_BUFFER_S de música na fila — hora de montar a próxima."""
        loop = getattr(self.bot, "loop", None)
        if loop is None or loop.is_closed():
            return

        def agenda() -> None:
            self._pronta_task = asyncio.create_task(self._prepara_a_proxima(tocada))
            self._pronta_task.add_done_callback(self._loga_falha_de_task)

        loop.call_soon_threadsafe(agenda)

    def _a_que_vem(self) -> Track | None:
        """Palpite de qual faixa toca quando a atual acabar SOZINHA. Pode errar
        (fila mexida depois, rádio desligada): quem valida é o consumo."""
        fila = self.queue
        if fila.loop_mode is LoopMode.TRACK:
            return fila.current
        return fila.next_up()

    async def prepara_o_corte(self, track: Track | None = None, espera_s: float | None = None) -> None:
        """Monta a fonte da faixa que vai entrar ANTES de cortar a atual, e espera
        ela ter áudio — o corte manual deixa de ter silêncio.

        Na fumaça da VM (21/09/2026), todo skip/"toca a terceira" tinha ~0,4s de
        buraco até o FFmpeg novo dar áudio; a transição natural, que já vinha
        pré-montada, tinha 40ms. `track` None = a próxima da fila (o skip simples).
        Só monta o que já está no cache; o resto segue pelo caminho normal.

        A espera pela fonte encher: curta no "pula" (quem pula quer que a atual
        PARE), mais longa no corte para uma faixa escolhida — a música segue
        tocando, e cortar com a fonte vazia é silêncio (2,8s na fumaça de 21/09).
        """
        if espera_s is None:
            espera_s = _ESPERA_DO_CORTE_S if track is None else _ESPERA_DA_ESCOLHIDA_S
        tocada = self._tocada
        if tocada is None or not self.is_playing:
            return
        try:
            await self._prepara_a_proxima(tocada, track or self.queue.next_up())
        except Exception as exc:  # noqa: BLE001 — é cortesia: sem ela o corte segue normal
            log.warning("[%s] não consegui pré-montar antes do corte: %r", self.guild.id, exc)
            return
        limite = time.monotonic() + espera_s
        while self._pronta is not None and time.monotonic() < limite:
            pulmao = self._pronta.pulmao
            if pulmao.encheu() or pulmao.acabou_vazia():
                return
            await asyncio.sleep(0.02)

    async def _prepara_a_proxima(self, tocada: _Tocada, proxima: Track | None = None) -> None:
        """Monta FFmpeg + pulmão da próxima faixa — só se a extração JÁ está no
        cache: isto nunca dispara os ~5s de CPU de uma extração (é o papel do
        prefetch, que tem prioridade e fila próprias).

        Entre "fim de X" e o som de Y eram ~1,3s mesmo com Y no cache: abrir o
        FFmpeg (TLS + HTTP com o googlevideo) e encher o pré-buffer.
        """
        def vale() -> bool:
            return self._tocada is tocada and not (tocada.pulada or tocada.parada or tocada.acabou)

        proxima = proxima or self._a_que_vem()
        if not vale() or proxima is None or not proxima.video_id:
            return
        if self._pronta is not None and self._pronta.track is proxima:
            return  # seek para trás nos últimos segundos: a fonte acabou de novo
        if youtube.stream_state(proxima.video_id) != "no cache":
            return
        await self._descarta_a_pronta()
        info = await youtube.get_stream(proxima.video_id)
        # URL com menos de 3s de vida (extraída agora para um "toca X agora"): o
        # googlevideo a recusa com 403 por ~1s, e o FFmpeg cai no backoff dele
        # (0s → 1s → 3s) — na fumaça de 21/09 o corte veio com a fonte vazia e o
        # 1º áudio só 2,8s depois. A URL do adiantamento já envelheceu: não espera.
        esperou_s, recusas = await youtube.espera_stream_abrir(info)
        if recusas:
            log.info(
                "[%s] pré-montagem de %s: URL fresca só abriu depois de %.1fs (%dx 403)",
                self.guild.id, proxima.display, esperou_s, recusas,
            )
        if not vale():
            return
        pulmao, saida, via = await self._monta(info, proxima)
        if not vale() or self._pronta is not None:
            # a faixa acabou antes de o FFmpeg abrir — ou outra pré-montagem (o
            # "pula" e o fim natural ao mesmo tempo) chegou primeiro: sem isto a
            # dela era sobrescrita e o FFmpeg + a thread ficavam vivos até o restart
            await asyncio.to_thread(pulmao.cleanup)
            return
        self._pronta = _Pronta(track=proxima, info=info, pulmao=pulmao, saida=saida, via=via)
        log.debug("[%s] fonte de %s pronta antes da hora (%s)", self.guild.id, proxima.display, via)

    async def _pega_a_pronta(self, track: Track) -> _Pronta | None:
        """A fonte pronta, se servir para `track`; senão descarta (sem contar falha)."""
        pronta, self._pronta = self._pronta, None
        if pronta is None:
            return None
        if (
            pronta.track is track
            and not pronta.pulmao.acabou_vazia()
            and time.monotonic() - pronta.feita_em < _PRONTA_VALE_S
        ):
            return pronta
        await asyncio.to_thread(pronta.pulmao.cleanup)
        return None

    async def _descarta_a_pronta(self) -> None:
        if self._pronta_task is not None and not self._pronta_task.done():
            if self._pronta_task is not asyncio.current_task():
                self._pronta_task.cancel()
        pronta, self._pronta = self._pronta, None
        if pronta is not None:
            await asyncio.to_thread(pronta.pulmao.cleanup)

    # --- seek e volume ---

    async def seek(self, destino_s: float) -> tuple[int, int] | None:
        """Leva a faixa atual para `destino_s`. Devolve (de, para) em segundos, ou
        None se não havia o que mover ou o FFmpeg novo não abriu (a música segue)."""
        return await self._reconstroi(float(destino_s))

    async def pede_seek(self, texto: str) -> tuple[str, int]:
        """O /seek e o seek por voz: ("ok" | "nada" | "invalido" | "falhou" | "alem",
        destino em segundos; no "alem", a duração da faixa). `texto` é "1:30", "90", "+30", "-10"… (intents.parse_posicao)."""
        track = self.queue.current
        if not self.is_playing or track is None:
            return "nada", 0
        destino = parse_posicao(texto, atual_s=self.elapsed, duracao_s=track.duration or 0)
        if destino is None:
            return "invalido", 0
        # ponto ABSOLUTO que a música não tem é engano de quem falou ou de quem
        # transcreveu: no teste de 21/09, "minuto trinta… não, segundo trinta" virou
        # 30:00 numa faixa de 4:19, o teto levou para 4:14 e a música ACABOU 5s
        # depois. O relativo ("+30" perto do fim) segue indo até o teto.
        if track.duration and texto.strip()[:1] not in ("+", "-"):
            pedido = parse_posicao(texto, atual_s=0, duracao_s=0)
            if pedido is not None and pedido >= track.duration:
                return "alem", int(track.duration)
        feito = await self.seek(destino)
        return ("ok", feito[1]) if feito else ("falhou", destino)

    async def set_volume(self, percent: int) -> int:
        """Volume do servidor em % (0–150), guardado no banco. Devolve o que ficou.

        Acima de 150 o filtro `volume` do FFmpeg já clipa a maioria das músicas,
        e um limitador custaria a CPU que o Opus direto acabou de devolver.
        """
        percent = max(0, min(VOLUME_MAX, int(percent)))
        anterior, self.volume = self.volume, percent
        if percent == anterior:
            return percent
        log.info("[%s] volume %d%% → %d%%", self.guild.id, anterior, percent)
        db = getattr(self.bot, "db", None)
        if db is not None:
            try:
                await db.set_volume(self.guild.id, percent)
            except Exception as exc:  # noqa: BLE001 — vale até o restart, e avisa
                log.warning("[%s] não gravei o volume no banco: %r", self.guild.id, exc)
        await self._reconstroi(None)
        return percent

    async def _reconstroi(self, destino_s: float | None) -> tuple[int, int] | None:
        """FFmpeg novo a partir de um ponto da faixa, trocado POR DENTRO da fonte.

        `destino_s` None = troca de volume: o ponto é "daqui a 3s" e a troca
        espera a música chegar lá — o FFmpeg leva ~0,3s para abrir (medido na
        VM); trocando no ponto certo, nada repete nem some. Para o py-cord é a
        mesma fonte: sem `after`, sem avançar a fila, pausa intacta.
        """
        tocada, track = self._tocada, self.queue.current
        if not self.is_playing or tocada is None or track is None or not track.video_id:
            return None
        if not isinstance(tocada.fonte, FonteDaFaixa):
            return None
        self._troca_vez += 1
        vez = self._troca_vez
        de = tocada.fonte.posicao_s
        no_ponto = destino_s is None
        if no_ponto:
            destino_s = de if self.is_paused else de + _FOLGA_DA_TROCA_S
            if track.duration and destino_s > track.duration - _FIM_PROXIMO_S:
                return None  # a faixa está acabando: o volume novo vale na próxima
        inicio = time.monotonic()
        try:
            info = await youtube.get_stream(track.video_id)
            pulmao, saida, via = await self._monta(info, track, destino_s)
        except Exception as exc:  # noqa: BLE001 — a música segue tocando como estava
            log.warning("[%s] não consegui reabrir %s em %s: %r",
                        self.guild.id, track.display, format_time(int(destino_s)), exc)
            return None

        def vale() -> bool:
            return (
                vez == self._troca_vez
                and self._tocada is tocada
                and not (tocada.pulada or tocada.parada)
            )

        limite = time.monotonic() + _ESPERA_DA_TROCA_S
        while vale() and not pulmao.encheu() and not pulmao.acabou_vazia() and time.monotonic() < limite:
            await asyncio.sleep(0.02)
        # volume: segura a fonte nova até a música chegar ao ponto dela
        while no_ponto and vale() and pulmao.encheu() and not self.is_paused:
            if tocada.fonte.posicao_s >= destino_s - 0.02 or time.monotonic() > limite:
                break
            await asyncio.sleep(0.01)
        if not vale() or not pulmao.encheu():
            if vale():
                log.warning(
                    "[%s] %s: o FFmpeg novo não abriu em %s — a música segue de onde estava",
                    self.guild.id, "volume" if no_ponto else "seek", format_time(int(destino_s)),
                )
            await asyncio.to_thread(pulmao.cleanup)
            return None
        atraso = tocada.fonte.posicao_s - destino_s
        if no_ponto and atraso > 0.04:
            # a fonte nova ficou pronta DEPOIS de a música passar do ponto dela:
            # no teste de 20/09 o FFmpeg recodificando levou 3,7s para os 3s de
            # folga, e trocar assim repetiria 0,7s de música. O começo dela já
            # tocou pela fonte antiga — fora.
            destino_s += pulmao.descarta(round(atraso * 50)) * 0.02
        pulmao.quando_acabar(lambda: self._fim_proximo(tocada))
        antiga = tocada.fonte.troca(pulmao, destino_s)
        if saida is not None and tocada.saida is not None:
            # o fim de faixa relata a faixa inteira, não só o último FFmpeg
            saida.reconexoes += tocada.saida.reconexoes
            saida.recusas += tocada.saida.recusas
        tocada.saida = saida if saida is not None else tocada.saida
        self._via = via
        log.info(
            "[%s] %s %s → %s em %.1fs (%s)",
            self.guild.id, f"volume {self.volume}%: troca" if no_ponto else "seek",
            format_time(int(de)), format_time(int(destino_s)), time.monotonic() - inicio, via,
        )
        await asyncio.to_thread(antiga.cleanup)  # mata o FFmpeg antigo; o join fica fora do loop
        if not no_ponto:
            await self.panel.refresh()  # o "começou há…" do painel mudou
        return int(de), int(destino_s)

    # --- som de escuta ---

    def tocar_efeito(self, pcm: bytes) -> None:
        """Toca o estalo de "estou ouvindo" sem atrapalhar a música."""
        voice = self.voice
        if not config.VOICE_SOM_ESCUTA or not pcm or voice is None or not voice.is_connected():
            return
        if voice.is_paused():
            return  # o py-cord não lê nada enquanto pausado
        if voice.is_playing() and not self._so_efeito:
            # PCM: misturado por cima da música. Opus: pacotes próprios, com a
            # música retida enquanto ele toca (quem decide é a FonteDaFaixa)
            self._source.sobrepor(pcm)
            em_opus = self._source.is_opus() is True
        else:
            if voice.is_playing():
                voice.stop_playing()  # outro estalo sozinho ainda tocando: recomeça
            self._efeito_vez += 1
            vez = self._efeito_vez
            self._so_efeito = True
            em_opus = config.AUDIO_OPUS_DIRETO
            # em Opus sai dos pacotes já codificados: o py-cord não cria um
            # encoder só para o estalo
            fonte = PacotesProntos(efeito_em_opus(pcm)) if em_opus else discord.PCMAudio(io.BytesIO(pcm))
            voice.play(fonte, after=lambda _erro: self._fim_do_efeito(vez))
        self._efeito_ate = time.monotonic() + duracao_do_estalo_s(pcm, em_opus)

    def _fim_do_efeito(self, vez: int) -> None:
        """Callback do py-cord (thread de áudio) quando o estalo sozinho acaba."""
        if vez == self._efeito_vez:
            self._so_efeito = False

    async def espera_efeito(self) -> None:
        """Dá tempo de o estalo terminar antes de um comando que cortaria o som."""
        resta = self._efeito_ate - time.monotonic()
        if resta > 0:
            await asyncio.sleep(resta)

    def set_autoplay(self, enabled: bool) -> None:
        """Liga/desliga a rádio — o ÚNICO caminho para mexer em autoplay_enabled.

        Botão do painel, /autoplay e comando de voz só trocavam a flag. Com a
        rádio numa fila própria isso deixava faixas de rádio tocando depois de
        "desligado", e ligar no meio de uma música não enchia a rádio (o
        prefetch só roda no início de cada faixa): a primeira transição pagava
        busca + ~8s de extração em silêncio.
        """
        if enabled == self.autoplay_enabled:
            return
        self.autoplay_enabled = enabled
        log.info("[%s] rádio (autoplay) %s", self.guild.id, "ligada" if enabled else "desligada")
        if not enabled:
            # o prefetch pode estar em `await fetch_autoplay_tracks` (0,5–2s):
            # sem cancelar, a busca voltava e enchia a rádio DEPOIS de
            # desligada — e gastava o worker único extraindo faixas que não
            # deviam tocar
            if self._prefetch_task is not None:
                self._prefetch_task.cancel()
                self._prefetch_task = None
            self.queue.radio.clear()  # o que a rádio já tinha enfileirado sai junto
            if self.is_playing:
                self._schedule_prefetch()  # a fila da pessoa segue adiantada
            return
        if self.is_playing:
            self._schedule_prefetch()  # enche a rádio agora, não no fim da faixa
        elif self.voice and self.voice.is_connected() and self._recent_ids:
            # bot parado na call com a fila vazia: ligar a rádio é pedir
            # música — e a semente é a última faixa que tocou. A referência
            # fica guardada: task sem dono pode ser coletada no meio.
            self._autoplay_task = asyncio.create_task(self.play_next())
            self._autoplay_task.add_done_callback(self._loga_falha_de_task)

    def _loga_falha_de_task(self, task: asyncio.Task) -> None:
        """Task em background que morreu vira log, não "never retrieved"."""
        if not task.cancelled() and task.exception() is not None:
            log.warning("[%s] tarefa em background falhou: %r", self.guild.id, task.exception())

    def _schedule_prefetch(self) -> None:
        """Adianta as próximas faixas em background (aquece o cache).

        Best-effort: se a fila mudar (shuffle, remove), o pior caso é ter
        aquecido um vídeo que tocará mais tarde — o cache continua valendo.
        """
        if self._prefetch_task is not None and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = asyncio.create_task(self._prefetch_ahead())

    def queue_changed(self, origem: str = "fila") -> None:
        """A fila foi reordenada/mexida — reprograma o adiantamento.

        `shuffle` e `remove` trocam quem é a próxima e invalidam o que estava
        adiantado. Sem isto, o `/skip` seguinte pagava a extração inteira: nos
        logs de 12/09, 14,4s numa faixa logo depois de um shuffle.
        """
        proxima = self.queue.next_up()
        # uma linha por mudança: sem isto o journal só tinha "tocando X em
        # 21,4s" e não dava para saber o que aconteceu ANTES (embaralhou? pulou?)
        log.info(
            "[%s] fila mudou (%s): próxima %s — %s",
            self.guild.id, origem,
            proxima.display if proxima else "nenhuma",
            youtube.stream_state(proxima.video_id) if proxima else "-",
        )
        if self.is_playing:
            self._schedule_prefetch()

    async def _prefetch_ahead(self) -> None:
        """Enche a fila (autoplay) e adianta as próximas: _ADIANTA_PEDIDAS da fila
        pedida, e a rádio só até completar _PREFETCH_DEPTH."""
        try:
            # Com autoplay ligado a fila fica VAZIA durante a música, então não
            # havia o que adiantar: a próxima só era buscada quando a atual
            # terminava — ~9s de silêncio entre faixas. Enchendo a RÁDIO aqui
            # (não a fila da pessoa), ela entra no caminho de adiantamento sem
            # furar o que foi pedido.
            # só quando a fila da pessoa está acabando: com 50 músicas pedidas,
            # buscar rádio agora seria trabalho jogado fora (e ela só tocaria
            # daqui a horas)
            precisa_radio = (
                self.autoplay_enabled
                and not self.queue.radio
                and len(self.queue.upcoming) < _PREFETCH_DEPTH
            )
            if precisa_radio:
                try:
                    novas = await fetch_autoplay_tracks(self._recent_ids, semente=self._semente_radio)
                except Exception:  # noqa: BLE001 — rádio fora não pode matar o prefetch
                    log.warning("[%s] autoplay antecipado falhou", self.guild.id)
                    novas = []
                # a pessoa pode ter desligado a rádio durante a busca
                if novas and self.autoplay_enabled:
                    self.queue.add_radio(novas)
                    await self.panel.refresh()

            pedidas = self.queue.upcoming[:_ADIANTA_PEDIDAS]
            da_radio = self.queue.radio[: max(0, _PREFETCH_DEPTH - len(pedidas))]
            for track in pedidas + da_radio:
                await self._prefetch(track)
        except asyncio.CancelledError:
            raise

    async def _prefetch(self, track: Track, *, agora: bool = False) -> None:
        """`agora`: alguém pediu ESTA faixa para já ("toca a terceira") — extrai com
        a prioridade de quem vai tocar, furando os adiantamentos pendentes."""
        try:
            if track.video_id is None:  # faixa do Spotify: resolve o vídeo já
                if not await self._acha_o_video(track):
                    return
            if agora:
                info = await youtube.get_stream(track.video_id)
                track.duration = track.duration or info["duration"]
                track.thumbnail = track.thumbnail or info["thumbnail"]
                return
            # 1 slot do pool no máximo: prefetch não pode atrasar o início
            # de faixa (foreground) de nenhum servidor
            async with youtube.PREFETCH_SEM:
                estado = youtube.stream_state(track.video_id)
                if estado == "fria":
                    log.info("[%s] adiantando %s", self.guild.id, track.display)
                # PRIO_PREFETCH: entra atrás de quem vai tocar agora e, se a
                # fila mudar antes de começar, some sem gastar o worker
                info = await youtube.get_stream(track.video_id, priority=youtube.PRIO_PREFETCH)
            # faixa vinda do oEmbed (/play <link>) chega sem duração/capa
            track.duration = track.duration or info["duration"]
            track.thumbnail = track.thumbnail or info["thumbnail"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — quem toca paga a extração de novo
            # era um `pass`: adiantamento que falha significa que a PRÓXIMA
            # transição vai pagar a extração fria (ou falhar) — e nada avisava
            log.warning(
                "[%s] adiantamento de %s falhou: %s",
                self.guild.id, track.display, youtube.friendly_error(exc),
            )

    def _after_track(self, error: Exception | None, tocada: _Tocada | None = None) -> None:
        """Callback do fim da faixa (roda na thread de áudio)."""
        if error is not None:  # antes o parâmetro era descartado: erro de
            # FFmpeg/opus no fim da faixa sumia sem rastro
            log.warning("[%s] erro no fim da faixa: %r", self.guild.id, error)
        if tocada is None:  # chamado sem contexto: o que dá para saber é a fonte
            fonte = getattr(self, "_source", None)
            tocada = _Tocada(None, fonte, None) if fonte is not None else None
        if tocada is not None and tocada.fonte.frames_sem_audio:
            # é a medida direta do "cortou um pedaço da música": sem ela só
            # dava para saber pelo relato de quem estava na call
            log.warning(
                "[%s] faltou áudio na faixa: %.1fs em %d interrupção(ões)"
                " — buffer de %.0fs não deu conta",
                self.guild.id,
                tocada.fonte.frames_sem_audio * 0.02,
                tocada.fonte.falhas_de_buffer,
                config.AUDIO_BUFFER_S,
            )
        if tocada is not None:
            tocada.acabou = True
        pulada = tocada.pulada if tocada is not None else False
        morreu = self._relata_fim(tocada, error) if tocada is not None else False
        if self._stopping:
            return
        asyncio.run_coroutine_threadsafe(
            self._segue_depois_do_fim(tocada.track if tocada else None, morreu=morreu, pulada=pulada),
            self.bot.loop,
        )

    def _relata_fim(self, tocada: _Tocada, error: Exception | None) -> bool:
        """Uma linha por faixa que acaba: quanto tocou e por quê. True = morreu na largada.

        Era o ponto cego de 20/09: quatro faixas "tocaram" 0 segundos e o
        journal só tinha o "tocando X" de cada uma. Quem lê o log agora vê a
        faixa que não tocou — e, como é WARNING, o dono também (services/alerts).
        """
        track = tocada.track
        tocou = tocada.fonte.frames_entregues * 0.02
        # com seek, quanto TOCOU e onde a música ESTAVA são coisas diferentes:
        # seek para 3:00 de 3:03 toca 3s e acaba — do jeito certo
        posicao = getattr(tocada.fonte, "posicao_s", tocou)
        if not isinstance(posicao, (int, float)):
            posicao = tocou
        teve_seek = posicao > tocou + 1.0
        esperado = track.duration if track else 0
        morreu = cortada = False
        if tocada.parada or self._stopping:
            motivo = "parada"
        elif tocada.pulada:
            motivo = "pulada"
        elif not teve_seek and tocou < _MORTE_NA_LARGADA_S and (not esperado or tocou < esperado * 0.5):
            motivo, morreu = "MORREU NA LARGADA", True
        elif esperado and posicao < esperado - _CORTE_TOLERADO_S:
            # inclui o FFmpeg de um seek que morre: tentar "de novo do zero"
            # devolveria a música ao começo sem ninguém pedir
            motivo, cortada = "CORTADA antes do fim", True
        else:
            motivo = "acabou"
        extras = []
        if teve_seek:
            extras.append(f"posição {format_time(int(posicao))}")
        saida = tocada.saida
        if saida is not None:
            if motivo not in ("pulada", "parada"):
                # fim natural: o FFmpeg já saiu e as últimas linhas do stderr
                # (o motivo de uma morte na largada) ainda estão chegando.
                # Em skip/stop NÃO: o `after` do py-cord roda ANTES do
                # cleanup(), o FFmpeg está vivo e o stderr não fecha — a
                # espera estourava os 0,5s em todo skip (journal de 20/09:
                # "pulando" 18:19:59,501 → "fim de … (pulada)" 18:20:00,007).
                saida.espera()
            if saida.reconexoes:
                extras.append(f"CDN reconectou {saida.reconexoes}x")
            if saida.recusas:
                extras.append(f"{saida.recusas} recusa(s) 403/5xx re-tentada(s)")
            if (morreu or cortada) and saida.erros:
                extras.append(f"ffmpeg: {saida.erros[-1]}")
        primeiro = getattr(tocada.fonte, "primeiro_audio_s", None)
        if primeiro is not None and primeiro > _PRIMEIRO_AUDIO_LENTO_S:
            # "tocando X em 5,7s" é até o FFmpeg SUBIR; o som sai depois
            extras.append(f"1º áudio só {primeiro:.1f}s depois do play")
        if tocada.fonte.frames_sem_audio:
            extras.append(f"faltou {tocada.fonte.frames_sem_audio * 0.02:.1f}s de áudio")
        fora = getattr(tocada.fonte, "fora_de_20ms", 0)
        if isinstance(fora, int) and fora:
            # o py-cord avança 20ms por pacote sem olhar: esses tocaram no tempo
            # errado. Este vídeo não é mais copiado — vai recodificado.
            extras.append(f"{fora} pacote(s) fora de 20ms")
            if track is not None and track.video_id:
                _SEM_COPIA.add(track.video_id)
        if error is not None:
            extras.append(f"erro: {error!r}")
        log.log(
            logging.WARNING if morreu or cortada else logging.INFO,
            "[%s] fim de %s: tocou %s de %s (%s)%s",
            self.guild.id,
            track.display if track else "?",
            format_time(int(tocou)),
            format_time(esperado) if esperado else "?:??",
            motivo,
            " — " + ", ".join(extras) if extras else "",
        )
        return morreu

    async def _segue_depois_do_fim(self, track: Track | None, *, morreu: bool, pulada: bool) -> None:
        """O que toca depois: a próxima — ou a MESMA, se ela morreu na largada."""
        if not morreu:
            self._mortes_seguidas = 0
            await self.play_next(ignore_loop=pulada)
            return
        self._mortes_seguidas += 1
        self._sintoma("largada", track.display if track is not None else "?")
        if self._mortes_seguidas >= _FAILURE_LIMIT:
            # rádio ligada + YouTube recusando tudo: cada tentativa é uma
            # extração (~5s de CPU na VM). Melhor parar e avisar.
            if track is not None:
                self._failures.append((track.display, m.TRACK_DIED_EARLY))
            await self._desiste("faixas seguidas que morreram na largada")
            return
        ainda_e_a_atual = track is not None and track is self.queue.current
        if ainda_e_a_atual and track is not self._retentada:
            self._retentada = track
            if track.video_id:
                youtube.invalidate(track.video_id)  # a URL em cache pode ser a que morreu
            log.warning("[%s] %s morreu na largada — tentando de novo com extração fresca",
                        self.guild.id, track.display)
            await self.play_next(de_novo=track)
            return
        # segunda morte da mesma faixa (ou a fila foi substituída): segue em
        # frente, e ignore_loop para o loop de faixa não devolver a morta
        await self.play_next(ignore_loop=True, morta=track if ainda_e_a_atual else None)

    def pause(self) -> None:
        if self.voice and self.voice.is_playing() and not self._so_efeito:
            self.voice.pause()
            self._paused_at = time.monotonic()
            log.info("[%s] pausado aos %s", self.guild.id, format_time(self.elapsed))

    def resume(self) -> None:
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            if self._paused_at is not None:
                log.info(
                    "[%s] retomado depois de %.0fs em pausa",
                    self.guild.id, time.monotonic() - self._paused_at,
                )
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None

    def skip(self, quantas: int = 1) -> Track | None:
        """Pula a atual — e, com `quantas` > 1, as próximas `quantas - 1` também
        ("pula 3"). Devolve a faixa que vem."""
        puladas = self.queue.descarta_da_frente(quantas - 1) if quantas > 1 else []
        if self._tocada is not None:
            self._tocada.pulada = True
        proxima = self.queue.next_up()
        log.info(
            "[%s] pulando %s%s → próxima %s — %s",
            self.guild.id,
            self.queue.current.display if self.queue.current else "nada",
            f" e mais {len(puladas)} da fila" if puladas else "",
            proxima.display if proxima else "nenhuma",
            youtube.stream_state(proxima.video_id) if proxima else "-",
        )
        if self.voice and not self._so_efeito and (self.voice.is_playing() or self.voice.is_paused()):
            # stop_playing (pycord_voice_patch): para só a música e dispara
            # _after_track; voice.stop() derrubaria também a escuta do /ouvir
            self._solta_a_fonte()
            self.voice.stop_playing()
        return proxima

    def tocada_agora(self) -> object | None:
        """Marca da faixa que está tocando — para `pula_se_ainda_for` depois de um await."""
        return self._tocada

    def pula_se_ainda_for(self, marca: object | None) -> bool:
        """Pula só se ainda toca a mesma faixa de quando o pedido chegou."""
        if self._tocada is not marca:
            log.info("[%s] o pulo chegou depois de a faixa acabar sozinha — nada a pular", self.guild.id)
            return False
        self.skip()
        return True

    # --- a fila por voz: escolher qual toca, qual é a próxima, tirar, voltar ---

    async def _corta_para_a_proxima(self, track: Track | None = None) -> None:
        """Passa JÁ para a frente da fila — sem silêncio. Se `track` ainda não foi
        extraída, extrai ANTES de cortar (~5s na VM: melhor passá-los ouvindo a
        música atual), com a prioridade de quem vai tocar AGORA; depois monta a
        fonte dela, e só então corta."""
        antes = self._tocada
        if track is not None and self.is_playing:
            # o estalo de "estou ouvindo" só não pode ser CORTADO — e o corte vem
            # segundos depois. Esperar por ele antes de começar a extrair custava
            # 1,3s entre "toca a terceira" e a extração (teste de 21/09).
            await asyncio.gather(self.espera_efeito(), self._deixa_pronta(track))
        else:
            await self.espera_efeito()
        if self.is_playing:
            # a atual pode ter acabado sozinha durante a espera (~5s de extração):
            # aí quem toca já é a escolhida, e cortar agora pulava justamente ela
            self.pula_se_ainda_for(antes)
        else:
            await self.play_next()

    async def _deixa_pronta(self, track: Track) -> None:
        if track.video_id is None or youtube.stream_state(track.video_id) != "no cache":
            # na VM o pedido entrou ATRÁS de um adiantamento no pool de um
            # worker só e "toca a última" levou 7,9s: os adiantamentos
            # pendentes saem da frente (o que já está rodando, termina)
            if self._prefetch_task is not None and not self._prefetch_task.done():
                self._prefetch_task.cancel()
            await self._prefetch(track, agora=True)
        await self.prepara_o_corte(track)

    async def toca_da_fila(self, track: Track, *, pulando: bool = False, quem: int = 0) -> list[Track]:
        """Toca AGORA uma faixa que já está na fila.

        "toca a quinta" puxa a quinta para a frente e ninguém se perde; "pula pra
        quinta" (`pulando`) descarta as quatro que vinham antes — devolvidas aqui.
        """
        puladas: list[Track] = []
        if pulando:
            puladas = self.queue.descarta_ate(track)
        else:
            self.queue.puxa_para_a_frente(track, quem=quem)
        # sem queue_changed(): ele reprogramaria o adiantamento DESTA faixa em
        # prioridade baixa, na frente do pedido de verdade. O início da faixa já
        # reprograma o adiantamento das seguintes.
        log.info(
            "[%s] fila mudou (%s): próxima %s — %s%s",
            self.guild.id, "pular para uma da fila" if pulando else "tocar uma da fila",
            track.display, youtube.stream_state(track.video_id) if track.video_id else "sem vídeo",
            f" — {len(puladas)} pulada(s) sem tocar" if puladas else "",
        )
        await self._corta_para_a_proxima(track)
        return puladas

    def proxima_sera(self, track: Track, *, quem: int = 0) -> None:
        """`track` (da fila, ou nova) vira a próxima, sem cortar a que está tocando."""
        if not self.queue.puxa_para_a_frente(track, quem=quem):
            self.queue.upcoming.insert(0, track)
        self.queue_changed("como próxima")

    def tira_da_fila(self, tracks: list[Track]) -> list[Track]:
        tiradas = self.queue.tira(tracks)
        if tiradas:
            self.queue_changed("tirar da fila")
        return tiradas

    async def anterior(self, quantas: int = 1) -> Track | None:
        """Volta para a música que tocou antes; a atual fica logo atrás dela na fila."""
        track = self.queue.volta_para_a_anterior(quantas)
        if track is None:
            return None
        log.info("[%s] fila mudou (música anterior): próxima %s", self.guild.id, track.display)
        await self._corta_para_a_proxima(track)
        return track

    def _solta_a_fonte(self) -> None:
        """Antes de parar o player: o read() do pulmão devolve fim de faixa já,
        em vez de segurar a thread do py-cord até o FFmpeg entregar um frame."""
        fonte = getattr(self, "_source", None)
        if fonte is not None and hasattr(fonte, "interrompe"):
            fonte.interrompe()

    async def request_skip(self, member: discord.Member) -> str | None:
        """Skip direto (dono da música/DJ/sozinho) ou registra voto.

        Retorna None quando pulou; senão, mensagem de status da votação.
        """
        track = self.queue.current
        if track is None:
            return m.NOTHING_PLAYING
        if self.preparing is not None and self.preparing is track:
            # extração fria em curso: não há o que parar, e "pulada" era mentira —
            # a faixa começava ~5s depois assim mesmo
            return m.SKIP_PREPARANDO.format(track=track.display)
        can_force = (
            member.id == track.requester_id
            or track.from_autoplay
            or member.guild_permissions.manage_guild
            or len(self.listeners()) <= 1
        )
        if can_force:
            self.skip()
            return None
        self.skip_votes.add(member.id)
        needed = self.votes_needed()
        if len(self.skip_votes) >= needed:
            self.skip()
            return None
        return m.VOTE_REGISTERED.format(votes=len(self.skip_votes), needed=needed)

    def schedule_empty_leave(self) -> None:
        """Agenda a saída por canal vazio (idempotente enquanto pendente)."""
        if self._empty_task is not None and not self._empty_task.done():
            return
        log.info("[%s] fiquei sozinho na call — saio em %ds se ninguém voltar", self.guild.id, EMPTY_LEAVE_S)
        self._empty_task = asyncio.create_task(self._leave_when_still_empty())

    def cancel_empty_leave(self) -> None:
        if self._empty_task is not None:
            if not self._empty_task.done():
                log.info("[%s] alguém voltou para a call — fico", self.guild.id)
            self._empty_task.cancel()
            self._empty_task = None

    async def _leave_when_still_empty(self) -> None:
        await asyncio.sleep(EMPTY_LEAVE_S)
        # zera ANTES do stop(): ele cancela _empty_task, e esta task É ela
        self._empty_task = None
        if not self.voice or not self.voice.is_connected() or self.listeners():
            return
        channel = self.text_channel
        await self.stop("canal vazio")
        if channel is not None:
            try:
                await channel.send(m.AUTO_LEFT_EMPTY)
            except discord.HTTPException:
                pass

    async def stop(self, motivo: str = "") -> None:
        """Para tudo, limpa a fila e desconecta.

        `motivo` só vai para o log: são seis caminhos até aqui (/stop, botão,
        voz, canal vazio, kick, desligamento) e "o bot saiu do nada" era
        impossível de explicar depois.
        """
        conectado = bool(self.voice and self.voice.is_connected())
        if conectado or self.queue.current is not None or len(self.queue) or self.listener is not None:
            log.info(
                "[%s] parando (%s): tocava %s, %d na fila, escuta %s",
                self.guild.id, motivo or "sem motivo informado",
                self.queue.current.display if self.queue.current else "nada",
                len(self.queue), "ligada" if self.listener is not None else "desligada",
            )
        # o timer de canal vazio é cancelado calado: quem chama o stop por
        # canal vazio É o timer
        if self._empty_task is not None:
            self._empty_task.cancel()
            self._empty_task = None
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
            self._prefetch_task = None
        await self._descarta_a_pronta()
        self._stopping = True
        if self._tocada is not None:
            self._tocada.parada = True
        try:
            self.queue.clear()
            if self.voice:
                if self.voice.is_playing() or self.voice.is_paused():
                    self._solta_a_fonte()
                    self.voice.stop()
                if self.voice.is_connected():
                    await self.voice.disconnect()
                else:
                    # no meio de um handshake de reconexão (26/09, 03:48): sem o
                    # force, o py-cord terminava a reconexão DEPOIS deste stop e o
                    # bot ficava na call sem fila nem escuta
                    await self.voice.disconnect(force=True)
        finally:
            self._stopping = False
        self._started_at = 0.0
        await self.panel.refresh()

    def _ancora_radio(self, track: Track) -> None:
        """Música pedida começou: ela vira a semente da rádio. O que a rádio já
        tinha enfileirado era da semente anterior e sai — senão, depois da pedida,
        voltavam as sugestões da música de antes."""
        if track.from_autoplay or track.video_id == self._semente_radio:
            return
        self._semente_radio = track.video_id
        if self.queue.radio:
            log.info(
                "[%s] rádio: nova semente %s — %d sugestão(ões) da anterior descartada(s)",
                self.guild.id, track.display, len(self.queue.radio),
            )
            self.queue.radio.clear()

    def _remember(self, video_id: str) -> None:
        self._recent_ids.append(video_id)
        del self._recent_ids[:-_RECENT_LIMIT]


def ouvintes(canal) -> list[int]:
    """Ids das pessoas no canal de voz, pelos ESTADOS DE VOZ do servidor.

    O bot não tem o intent de membros, e `VoiceChannel.members` do py-cord só
    devolve quem está no cache: depois de um reinício, quem já estava na call
    ficava invisível. Em 28/09 o bot contou 1 com três na call e, quando um saiu,
    deixou a call com DUAS pessoas ouvindo. Quem não está no cache conta como
    gente — só fica de fora o próprio bot e bot conhecido.
    """
    guild = getattr(canal, "guild", None)
    if not hasattr(guild, "_voice_states"):  # canal sem os estados (dublês de teste)
        return [getattr(m, "id", m) for m in getattr(canal, "members", []) if not m.bot]
    eu = getattr(getattr(guild, "_state", None), "self_id", None)
    ids = []
    for user_id, estado in guild._voice_states.items():
        if estado.channel is None or estado.channel.id != canal.id or user_id == eu:
            continue
        membro = guild.get_member(user_id)
        if membro is not None and membro.bot:
            continue
        ids.append(user_id)
    return ids


_players: dict[int, GuildPlayer] = {}


def get_player(bot: discord.Bot, guild: discord.Guild) -> GuildPlayer:
    player = _players.get(guild.id)
    if player is None:
        player = GuildPlayer(bot, guild)
        _players[guild.id] = player
    return player


def all_players() -> list[GuildPlayer]:
    return list(_players.values())


def peek_player(guild_id: int) -> GuildPlayer | None:
    """Player existente do guild, SEM criar um novo (eventos de lifecycle)."""
    return _players.get(guild_id)


def pop_player(guild_id: int) -> GuildPlayer | None:
    return _players.pop(guild_id, None)


def _tempo_aproximado(segundos: float) -> str:
    if segundos < 60:
        return "menos de 1 min"
    minutos = round(segundos / 60)
    return f"{minutos} min" if minutos < 60 else f"{minutos // 60}h{minutos % 60:02d}"


def texto_de_adicionada(player: GuildPlayer, track: Track) -> str:
    """A resposta do /play para UMA música — chamar ANTES do enqueue.

    Era só "na fila!": com 30 músicas na frente, a pessoa não sabia se tinha
    entrado em 2º ou em 31º, nem quando ia tocar. Sem saber a duração de alguma
    da frente (faixa do Spotify ainda sem vídeo, link recém-pedido) ou com a
    faixa atual em loop, diz só a posição — não inventa hora.
    """
    atual = player.queue.current
    if not player.is_playing or atual is None:
        return m.ADDED.format(track=track.display)
    try:
        na_frente = list(player.queue.upcoming)
        posicao = len(na_frente) + 1
        sabe = (
            player.queue.loop_mode is not LoopMode.TRACK
            and atual.duration > 0
            and all(t.duration > 0 for t in na_frente)
        )
        if not sabe:
            return m.ADDED_POS_NO_ETA.format(track=track.display, pos=posicao)
        falta = max(0, atual.duration - player.elapsed) + sum(t.duration for t in na_frente)
        return m.ADDED_POS.format(track=track.display, pos=posicao, eta=_tempo_aproximado(falta))
    except Exception:  # noqa: BLE001 — é cortesia: o /play não morre por causa de uma frase
        return m.ADDED.format(track=track.display)


def control_error(player: GuildPlayer, member: discord.Member) -> str | None:
    """None se o membro pode executar ações destrutivas (stop/clear/…); senão a recusa.

    Regra: estar no canal de voz do bot E (estar sozinho com o bot OU ter
    manage_guild). O /skip fica fora — tem votação própria (request_skip).
    """
    if player.voice is None or not player.voice.is_connected():
        return None  # bot fora da call: nada a proteger
    voice = getattr(member, "voice", None)
    if voice is None or voice.channel != player.voice.channel:
        return m.CTRL_SAME_CHANNEL
    if len(player.listeners()) <= 1 or member.guild_permissions.manage_guild:
        return None
    return m.CTRL_NO_PERMISSION


async def ensure_voice(ctx: discord.ApplicationContext) -> GuildPlayer | None:
    """Confere que o autor está num canal de voz e conecta o bot lá.

    Responde com o erro apropriado e retorna None quando não dá.
    """
    # sem ephemeral: os chamadores dão defer público antes (followup ephemeral
    # deixaria o "pensando…" pendurado para sempre)
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.respond(m.JOIN_VOICE_FIRST)
        return None
    player = get_player(ctx.bot, ctx.guild)
    if player.is_playing and player.voice.channel != ctx.author.voice.channel:
        await ctx.respond(m.OTHER_CHANNEL)
        return None
    await player.connect(ctx.author.voice.channel)
    return player
