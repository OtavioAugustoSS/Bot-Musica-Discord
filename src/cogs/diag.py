"""/diag — diagnóstico da extração de áudio.

Existe por causa do apagão de agosto/2026: o bot pulava a fila inteira com
"The page needs to be reloaded" e não havia como saber, de dentro do Discord,
QUAL camada tinha caído (versão do yt-dlp, cookies vencidos, client bloqueado,
runtime de JS sumido). Esse comando responde isso em 3 segundos.

NÃO usar `from __future__ import annotations` em cogs (quebra as Options).
"""
import os
import shutil
import time

import discord
import yt_dlp
from discord.ext import commands

import config
from services import youtube


def _age(path: str) -> str:
    try:
        days = (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return "?"
    return f"{days:.0f}d"


# o teste é uma ida real ao YouTube; /diag repetido não pode virar martelo
_PROBE_TTL_S = 60


class Diag(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._probe: tuple[float, bool, str] | None = None  # (quando, ok, texto)

    async def _probe_extraction(self) -> tuple[bool, str]:
        agora = time.monotonic()
        if self._probe and agora - self._probe[0] < _PROBE_TTL_S:
            _, ok, texto = self._probe
            return ok, f"{texto} _(medido há {agora - self._probe[0]:.0f}s)_"
        started = time.monotonic()
        try:
            info = await youtube.get_stream(youtube.PROBE_VIDEO_ID, use_cache=False)
        except Exception as exc:  # noqa: BLE001 — o diagnóstico É o erro
            ok, texto = False, f"❌ {youtube.friendly_error(exc)}"
        else:
            took = time.monotonic() - started
            if info.get("stream_url", "").startswith("http"):
                ok = True
                texto = f"✅ extraiu em {took:.1f}s pelo perfil `{youtube.active_profile()}`"
            else:
                ok, texto = False, "⚠️ veio sem URL de stream"
        self._probe = (time.monotonic(), ok, texto)
        return ok, texto

    @commands.slash_command(
        name="diag", description="Diagnóstico: testa o YouTube de verdade e diz por que o áudio não toca"
    )
    async def diag(
        self,
        ctx: discord.ApplicationContext,
        testar: discord.Option(
            bool, "Faz uma extração de verdade no YouTube agora", default=True
        ),
    ) -> None:
        await ctx.defer(ephemeral=True)

        runtime = youtube.js_runtime_in_use()
        estado_cookies, texto_cookies = youtube.cookies_status()
        if estado_cookies == "ok":
            texto_cookies = f"`{config.YTDLP_COOKIES}` ({_age(config.YTDLP_COOKIES)})"
        elif estado_cookies == "sem-login":
            texto_cookies = f"⚠️ {texto_cookies}"

        nova = await youtube.outdated_release()
        versao = f"`{yt_dlp.version.__version__}`"
        if nova:
            versao += f" ⚠️ saiu a `{nova}` — bump em requirements.txt"

        lines = [
            f"**yt-dlp** {versao}",
            f"**runtime JS** {runtime or '⚠️ nenhum — o áudio cai para o formato 18 (mp4)'}",
            f"**ffmpeg** {'ok' if shutil.which('ffmpeg') else '⚠️ ausente do PATH'}",
            f"**cookies** {texto_cookies}",
            f"**perfis** {' → '.join(youtube.profile_names())}",
            f"**perfil ativo** `{youtube.active_profile()}`",
        ]

        color = 0x1DB954
        if testar:
            ok, texto = await self._probe_extraction()
            lines.append(f"**teste** {texto}")
            if not ok:
                color = 0xE74C3C

        alertas = youtube.alerts()
        if alertas:
            color = 0xE67E22 if color == 0x1DB954 else color
            lines.append("\n**avisos recentes do yt-dlp**")
            lines += [f"• {a}" for a in alertas]

        if not runtime:
            lines.append(
                "\n_Instale um runtime de JS na máquina (`deno` ou `node`) —"
                " sem ele o yt-dlp perde os clients bons._"
            )

        embed = discord.Embed(
            title="🩺 Diagnóstico da extração", description="\n".join(lines), color=color
        )
        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Diag(bot))
