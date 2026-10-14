"""Ciclo de vida da voz: auto-leave com canal vazio, kick forçado, saída de guild.

Sem isso o bot ficava na call sozinho para sempre (banda + STT à toa), uma
desconexão forçada por moderador deixava fila e escuta órfãs, e o
GuildPlayer de um servidor removido vivia na memória eternamente.
"""
import asyncio
import logging
import time

import discord
from discord.ext import commands

import messages as m
from core.player import peek_player, pop_player

log = logging.getLogger(__name__)

# Queda da conexão de voz não é expulsão. Em 21/09/2026 (04:22 UTC) o WebSocket de
# voz fechou com 1006 no meio de um teste do Otavio; o py-cord reconectou sozinho em
# 0,7s — e o AudioPlayer dele espera a reconexão e retoma o áudio. Só que a
# reconexão passa por um voice state "bot fora do canal" (25ms depois, "no canal" de
# novo), e o handler abaixo lia esse instante como kick: música parada, fila limpa,
# escuta desligada, e o bot mudo dentro da call. Agora espera este tempo pela volta.
_QUEDA_ESPERA_S = 8.0
# Em 26/09/2026 (03:48 UTC) o handshake da reconexão levou 30s, com o gateway dizendo
# "bot no canal" desde o primeiro segundo: os 8s acabaram, a escuta foi desligada e o
# bot terminou reconectado, surdo, dentro da call. Enquanto o Discord disser que o
# bot está num canal, a reconexão está em andamento — espera até este teto.
_QUEDA_ESPERA_MAX_S = 60.0
_QUEDA_PASSO_S = 0.25


class Lifecycle(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        player = peek_player(member.guild.id)
        if player is None:
            return
        # o PRÓPRIO bot desconectado à força (kick da call): limpa tudo em
        # vez de deixar fila órfã e escuta rodando contra voz morta. Não
        # reconecta sozinho — se um moderador tirou, foi por algum motivo.
        if member.id == self.bot.user.id and after.channel is None:
            if await self._voltou_sozinha(player):
                return
            await player.stop("o bot foi desconectado da call (kick ou queda de voz)")
            return
        if player.voice is None or not player.voice.is_connected():
            return
        if not player.listeners():
            player.schedule_empty_leave()
        else:
            player.cancel_empty_leave()

    async def _voltou_sozinha(self, player) -> bool:
        """A conexão de voz caiu e o py-cord a refez? (Expulsão de verdade: o
        voice client some, ou a reconexão termina com o bot em canal nenhum.)"""
        inicio = time.monotonic()
        while True:
            passou = time.monotonic() - inicio
            voice = player.voice
            if voice is None:
                return False  # o py-cord desistiu: foi tirado da call
            estado = getattr(player.guild.me, "voice", None)
            no_canal = estado is not None and estado.channel is not None
            if passou >= (_QUEDA_ESPERA_MAX_S if no_canal else _QUEDA_ESPERA_S):
                return False
            if voice.is_connected():
                if not no_canal:
                    return False
                log.warning(
                    "[%s] a conexão de voz caiu e voltou sozinha em %.1fs — a música segue",
                    player.guild.id, time.monotonic() - inicio,
                )
                # se a gravação terminou durante a queda, o reinício automático da
                # escuta desistiu ("sem conexão"): confere agora, já reconectado
                escuta = player.listener
                if escuta is not None and escuta.active:
                    await escuta.confere_a_gravacao()
                return True
            await asyncio.sleep(_QUEDA_PASSO_S)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Diz a que veio ao entrar num servidor novo.

        Sem isto o bot entrava e ficava mudo: ninguém descobria que existe
        `/play` nem o modo de voz. Escolhe o canal de sistema do servidor e,
        se não der para escrever nele, o primeiro que der.
        """
        canais = [guild.system_channel, *guild.text_channels]
        for canal in canais:
            if canal is None:
                continue
            try:
                permissoes = canal.permissions_for(guild.me)
                if not permissoes.send_messages:
                    continue
                await canal.send(m.GUILD_WELCOME)
            except Exception:  # noqa: BLE001 — entrar no servidor não pode falhar por isso
                log.warning("[%s] não consegui dar boas-vindas em #%s", guild.id, canal.name)
                continue
            return
        log.info("[%s] entrei mas não achei canal onde escrever", guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        player = pop_player(guild.id)
        if player is None:
            return
        # sem chamadas à API do Discord: o bot já está fora do servidor
        player.cancel_empty_leave()
        if player.listener is not None:
            player.listener.stop()
            player.listener = None
        if player._prefetch_task is not None:
            player._prefetch_task.cancel()
        player.queue.clear()


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Lifecycle(bot))
