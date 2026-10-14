"""Entrypoint do bot. Rode da raiz do projeto: python src/main.py"""
import asyncio
import logging
import os
import shutil
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord  # noqa: E402

import config  # noqa: E402
import messages as m  # noqa: E402
from core.pycord_voice_patch import apply as apply_voice_patch  # noqa: E402
from db.database import Database  # noqa: E402
from services import alerts, observabilidade  # noqa: E402

apply_voice_patch()  # recepção de voz do py-cord 2.8.1 (ver o módulo)

log = logging.getLogger("main")

_shutting_down = False


async def _shutdown(bot: discord.Bot) -> None:
    """Desligamento gracioso no SIGTERM do systemd.

    Sem isso o processo ignorava o sinal e o systemd esperava ~90s até o
    SIGKILL — restart sujo: painéis órfãos no chat, DB e sessão HTTP abertos.
    """
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    log.info("🛑 SIGTERM — desligando...")
    from core.player import all_players
    from services import http

    try:
        for player in all_players():
            try:
                await asyncio.wait_for(player.stop("desligamento do bot"), timeout=3)
                await asyncio.wait_for(player.panel.destroy(), timeout=3)
            except Exception:  # noqa: BLE001 — desligar nunca pode travar
                pass
        from services import youtube

        salvas = youtube.dump_cache(config.STREAM_CACHE_PATH)
        log.info("cache de streams salvo: %d faixa(s)", salvas)
        await bot.db.close()
        await http.close()
    finally:
        bot.run_state.mark_clean_exit()  # sem isto o próximo boot acusa morte
        await bot.close()


async def _alert_unclean_restart(bot: discord.Bot, anterior: dict) -> None:
    """DM ao dono: o processo anterior morreu sem desligamento limpo."""
    inicio = anterior.get("started_at")
    quando = f"<t:{int(inicio)}:R>" if inicio else "?"
    erros = alerts.tail_errors(config.RECENT_ERRORS_PATH) or m.ALERT_NO_RECENT_ERRORS
    await bot.alerts.send(
        "reinicio-sujo",
        m.ALERT_UNCLEAN_RESTART.format(inicio=quando, erros=erros[-1500:]),
        ttl_s=10 * 60,  # em loop de crash, um aviso a cada 10min, não a cada boot
    )


async def _check_extraction(bot: discord.Bot) -> None:
    """Autoteste da extração de áudio no boot — o journal responde sozinho.

    Em agosto/2026 o bot subiu "saudável" por dias enquanto TODA faixa era
    pulada: nada no log dizia que o yt-dlp não conseguia mais extrair. Uma
    sonda de ~2s na subida elimina esse ponto cego — e o que ela achar de
    errado (extração morta, cookies sem login, yt-dlp velho) vai por DM ao
    dono, porque journal ninguém lê.
    """
    import yt_dlp

    from services import youtube

    recuperadas = youtube.load_cache(config.STREAM_CACHE_PATH)
    if recuperadas:
        log.info("cache de streams recuperado: %d faixa(s) ainda válidas", recuperadas)

    runtime = youtube.js_runtime_in_use()
    log.info(
        "yt-dlp %s | runtime JS: %s (cache do player %s) | %d extração(ões)"
        " simultânea(s) em %d vCPU | perfis: %s",
        yt_dlp.version.__version__,
        runtime or "NENHUM",
        "ligado" if youtube.CACHE_DE_PLAYER else "DESLIGADO",
        youtube._YTDLP_POOL._max_workers,
        os.cpu_count() or 0,
        ", ".join(youtube.profile_names()),
    )
    if not runtime:
        log.warning(
            "sem runtime de JS (deno/node): o yt-dlp perde os melhores clients"
            " e o áudio cai para o formato 18 (mp4 com vídeo junto)"
        )
    sonda = None
    try:
        sonda = await youtube.get_stream(youtube.PROBE_VIDEO_ID, use_cache=False)
    except Exception as exc:  # noqa: BLE001 — o autoteste não pode derrubar o boot
        motivo = youtube.friendly_error(exc)
        log.error(
            "extração de áudio NÃO está funcionando (%s) — rode `python src/doctor.py`",
            motivo,
        )
        await bot.alerts.send(
            "extracao", m.ALERT_EXTRACTION_BROKEN.format(error=motivo), ttl_s=6 * 3600
        )
    else:
        log.info("extração de áudio ok pelo perfil %s", youtube.active_profile())

    if sonda is not None:
        # extrair não é tocar: em 20/09 a extração passava aqui e no /diag
        # enquanto o googlevideo recusava a URL (4 de 6 faixas mudas)
        vistos = await youtube.sonda_do_stream(sonda["stream_url"])
        sequencia = " → ".join(str(v) for v in vistos)
        if not youtube.stream_abriu(vistos):
            log.error("a URL do stream NÃO abre (%s) — as faixas não vão tocar", sequencia)
            await bot.alerts.send(
                "stream", m.ALERT_STREAM_REFUSED.format(status=sequencia), ttl_s=6 * 3600
            )
        elif len(vistos) > 1:
            log.warning(
                "a URL do stream só abriu na %dª tentativa (%s) — o FFmpeg re-tenta"
                " sozinho, mas o início das faixas frias atrasa 1–4s",
                len(vistos), sequencia,
            )
        else:
            log.info("a URL do stream abre de primeira (%s)", sequencia)

    estado_cookies, texto_cookies = youtube.cookies_status()
    idade = youtube.cookies_age_days()
    log.info(
        "cookies do YouTube: %s — %s%s", estado_cookies, texto_cookies,
        f" ({idade:.0f} dias)" if idade is not None else "",
    )
    if estado_cookies == "sem-login":
        await bot.alerts.send("cookies", m.ALERT_COOKIES_DEAD, ttl_s=24 * 3600)

    nova = await youtube.outdated_release()
    if nova:
        log.warning(
            "yt-dlp %s está atrás da %s — o apagão de ago/2026 foi versão velha."
            " Bump em requirements.txt + testar /play",
            yt_dlp.version.__version__,
            nova,
        )
        await bot.alerts.send(
            f"ytdlp-{nova}",
            m.ALERT_YTDLP_OUTDATED.format(nova=nova, instalada=yt_dlp.version.__version__),
            ttl_s=7 * 86400,
        )


async def _caixa_de_avisos(bot: discord.Bot) -> None:
    """Entrega ao dono o que o canário do yt-dlp (fora do bot) deixou em data/avisos."""
    while True:
        try:
            entregues = await alerts.entrega_avisos(bot.alerts, config.AVISOS_DIR)
            if entregues:
                log.info("caixa de avisos: %d entregue(s) ao dono", entregues)
        except Exception:  # noqa: BLE001 — aviso não pode derrubar o bot
            log.exception("caixa de avisos falhou")
        await asyncio.sleep(600)


# Journal de 25/09 a 06/10: depois de >20h parado, a 1ª extração levou 11–18s
# (normal ~4s) — o YouTube troca o player JS e quem pedia a música pagava o
# processamento do novo ("cache de players pré-processados: 1 antigo(s) apagado(s)")
_AQUECE_A_CADA_S = 2 * 3600.0


async def _aquece(bot: discord.Bot) -> float | None:
    """Ocioso: extrai a sonda para o próximo /play achar tudo quente. Segundos gastos,
    ou None (em call — não disputa a CPU com a música — ou falhou)."""
    if bot.voice_clients:
        return None
    from services import youtube

    inicio = time.monotonic()
    try:
        await youtube.get_stream(youtube.PROBE_VIDEO_ID, use_cache=False)
    except Exception as exc:  # noqa: BLE001 — aquecimento é cortesia
        log.info("aquecimento ocioso: extração falhou (%s)", type(exc).__name__)
        return None
    gasto = time.monotonic() - inicio
    log.info("aquecimento ocioso: extração em %.1fs", gasto)
    return gasto


async def _mantem_aquecido(bot: discord.Bot) -> None:
    while True:
        await asyncio.sleep(_AQUECE_A_CADA_S)
        await _aquece(bot)


async def _daily_backup(bot: discord.Bot) -> None:
    """Backup diário do SQLite (bot.db.daily) — perder o disco não perde tudo.

    Primeiro backup 1h após o boot (restart não dispara um a cada subida),
    depois a cada 24h, via API de backup do SQLite (consistente com WAL).
    """
    delay = 3600.0
    while True:
        await asyncio.sleep(delay)
        delay = 24 * 3600.0
        try:
            await bot.db.backup_to(config.DB_PATH + ".daily")
            log.info("backup diário do banco OK")
        except Exception:  # noqa: BLE001 — backup não pode derrubar o bot
            log.exception("backup diário do banco falhou")


def main() -> None:
    # níveis, segredos mascarados, tracebacks de thread e warnings DENTRO do
    # logging (services/observabilidade.py explica cada um)
    observabilidade.configura_logging()
    # espelho dos últimos WARNING/ERROR: é o que vai por DM ao dono se o
    # processo morrer sem desligamento limpo (o bot não lê o journal)
    alerts.install_error_log(config.RECENT_ERRORS_PATH)

    if not config.DISCORD_TOKEN:
        sys.exit(
            "❌ DISCORD_TOKEN não configurado.\n"
            "   Copie .env.example para .env e preencha o token do bot."
        )
    if shutil.which("ffmpeg") is None:
        sys.exit(
            "❌ FFmpeg não encontrado no PATH.\n"
            "   Instale com: winget install Gyan.FFmpeg (e reabra o terminal)."
        )

    intents = discord.Intents.default()
    debug_guilds = [int(config.GUILD_ID)] if config.GUILD_ID else None
    bot = discord.Bot(intents=intents, debug_guilds=debug_guilds)
    bot.db = Database(config.DB_PATH)
    bot.run_state = alerts.RunState(config.RUN_STATE_PATH)
    bot.alerts = alerts.OwnerAlerts(bot, bot.run_state)
    bot.sintomas = alerts.Sintomas(bot.alerts, pedido_path=config.CANARIO_PEDIDO_PATH)
    execucao_anterior = bot.run_state.begin()  # não-None = morreu sem SIGTERM

    @bot.event
    async def on_ready() -> None:
        # guild "legado" da migração v0→v1 dos favoritos: GUILD_ID do .env
        # (caso da VM) ou, sem ele, o único servidor do bot — as linhas
        # antigas foram todas criadas ali
        legacy = (
            int(config.GUILD_ID)
            if config.GUILD_ID
            else (bot.guilds[0].id if len(bot.guilds) == 1 else None)
        )
        await bot.db.init(legacy_guild_id=legacy)
        log.info("✅ Online como %s — em %d servidor(es)", bot.user, len(bot.guilds))
        if not hasattr(bot, "_backup_task"):  # on_ready dispara de novo em reconexões
            bot._backup_task = asyncio.create_task(_daily_backup(bot))
            bot._extraction_check = asyncio.create_task(_check_extraction(bot))
            bot._avisos_task = asyncio.create_task(_caixa_de_avisos(bot))
            bot._aquecimento = asyncio.create_task(_mantem_aquecido(bot))
            if execucao_anterior is not None:
                bot._unclean_alert = asyncio.create_task(
                    _alert_unclean_restart(bot, execucao_anterior)
                )
        if sys.platform != "win32":  # dev Windows: Ctrl+C já é tratado no bot.run()
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGTERM, lambda: asyncio.create_task(_shutdown(bot))
            )
        # aquece o Whisper local em background — só quando ele é o motor
        # principal (sem Groq); com Groq ativo, o local é fallback raro e
        # carregá-lo desperdiçaria RAM (importante em servidores pequenos)
        if not config.GROQ_API_KEY:
            from services import speech

            asyncio.get_running_loop().run_in_executor(None, speech.preload)
        if not config.spotify_enabled():
            log.info(
                "Spotify desativado — defina SPOTIFY_CLIENT_ID/SECRET"
                " no .env para importar playlists"
            )

    @bot.event
    async def on_error(event: str, *args, **kwargs) -> None:
        # o padrão do py-cord imprime "Ignoring exception in on_x" + traceback
        # cru no stderr: sem horário, sem nível e fora do espelho de erros
        log.exception("erro no evento %s", event)

    @bot.event
    async def on_view_error(error: Exception, item, interaction: discord.Interaction) -> None:
        # botão/menu do painel: mesmo destino cru no stderr. O caso conhecido é
        # o 10062 (Unknown interaction, 3x no journal): a janela de 3s para
        # responder venceu — event loop ocupado ou VM sem CPU, não bug do botão.
        onde = getattr(item, "custom_id", None) or type(item).__name__
        if isinstance(error, discord.NotFound) and error.code == 10062:
            log.warning(
                "[%s] clique em %s expirou antes da resposta (janela de 3s do Discord)",
                interaction.guild_id, onde,
            )
            return
        log.error("[%s] erro no clique em %s: %r", interaction.guild_id, onde, error, exc_info=error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(m.COMMAND_ERROR, ephemeral=True)
        except discord.HTTPException:
            pass

    @bot.event
    async def on_application_command_error(
        ctx: discord.ApplicationContext, error: discord.DiscordException
    ) -> None:
        # sem isso, exceção em slash command virava "O aplicativo não
        # respondeu" para o usuário e sumia sem rastro no log
        original = getattr(error, "original", error)
        log.error(
            "erro no /%s: %r",
            getattr(ctx.command, "qualified_name", "?"),
            original,
            exc_info=original,
        )
        try:
            await ctx.respond(m.COMMAND_ERROR, ephemeral=True)
        except discord.HTTPException:
            pass

    for extension in (
        "cogs.music",
        "cogs.favorites",
        "cogs.help",
        "cogs.voice",
        "cogs.stats",
        "cogs.diag",
        "cogs.lifecycle",
        "cogs.registro",
    ):
        bot.load_extension(extension)

    # vigia do loop + batimento do watchdog do systemd + retratos de saúde.
    # ANTES do run(), presos ao loop do bot: nascem com o event loop e não
    # dependem do on_ready — com o Discord fora do ar o batimento continua, e
    # o systemd não fica reiniciando o bot por uma queda que não é dele.
    bot._observabilidade = observabilidade.inicia(bot, bot.loop)

    try:
        bot.run(config.DISCORD_TOKEN)
    except RuntimeError as exc:
        # py-cord fecha a própria sessão HTTP dentro de close(), mas o loop de
        # reconexão do gateway ainda está de pé: a tentativa seguinte levanta
        # "Session is closed" e o processo sai com status 1 num desligamento
        # que deu certo (systemd registra 'Failed'). Só engolimos isso quando
        # o SIGTERM foi nosso — qualquer outro RuntimeError continua subindo.
        if not (_shutting_down and "Session is closed" in str(exc)):
            raise
        log.info("desligado")


if __name__ == "__main__":
    main()
