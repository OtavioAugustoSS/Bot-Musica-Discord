"""Diagnóstico da extração de áudio. Rode na VM: `.venv/bin/python src/doctor.py`

Responde, em ordem, as perguntas que o apagão de agosto/2026 deixou sem
resposta por dias: a versão do yt-dlp está velha? tem runtime de JS? os
cookies estão sendo usados (e é por causa deles que o client mudou)? QUAL
perfil ainda funciona neste IP?

Código de saída (é por ele que scripts/canario_ytdlp.sh decide): 0 = extrai e
toca; 1 = nenhum perfil extrai; 2 = extrai, mas o FFmpeg não tira áudio da URL
(o 403 de 20/09/2026 era isso).
"""
import asyncio
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# console do Windows abre em cp1252 e engasga no primeiro acento do relatório
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover — stdout redirecionado
    pass

import yt_dlp  # noqa: E402

import config  # noqa: E402
from services import youtube  # noqa: E402

OK, WARN, BAD = " ok  ", "AVISO", "FALHA"


def _line(status: str, label: str, value: str) -> None:
    print(f"[{status}] {label:<16} {value}")


async def ambiente() -> None:
    print("── ambiente ─────────────────────────────────────────────")
    _line(OK, "python", sys.version.split()[0])
    nucleos = os.cpu_count() or 0
    simultaneas = youtube._YTDLP_POOL._max_workers
    _line(
        WARN if simultaneas > max(1, nucleos // 2) else OK,
        "extração",
        f"{simultaneas} simultânea(s) em {nucleos} vCPU — extrair é CPU-bound"
        " (desafio JS do player); passar dos núcleos derruba latência E vazão",
    )

    nova = await youtube.outdated_release()
    _line(
        WARN if nova else OK,
        "yt-dlp",
        f"{yt_dlp.version.__version__}"
        + (
            f" — saiu a {nova}. Atualize: .venv/bin/pip install -U 'yt-dlp[default]'"
            " e fixe a nova em requirements.txt"
            if nova
            else " (última)"
        ),
    )

    ffmpeg = shutil.which("ffmpeg")
    _line(OK if ffmpeg else BAD, "ffmpeg", ffmpeg or "AUSENTE do PATH — nada toca sem ele")

    runtime = youtube.js_runtime_in_use()
    _line(
        OK if runtime else WARN,
        "runtime JS",
        runtime or f"nenhum de [{config.YTDLP_JS_RUNTIMES}] — instale deno ou node",
    )

    estado, texto = youtube.cookies_status()
    if estado == "ok":
        idade = (time.time() - os.path.getmtime(config.YTDLP_COOKIES)) / 86400
        texto += (
            f" ({idade:.0f} dias). ATENÇÃO: a mera presença deste arquivo faz o"
            " yt-dlp trocar os clients padrão pelos autenticados."
        )
    _line({"ok": OK, "ausente": OK}.get(estado, WARN), "cookies", texto)


async def tocar_de_verdade(profile) -> bool:
    """Passa a URL extraída pelo FFmpeg — extrair não é o mesmo que tocar.

    A URL pode vir válida e o CDN do googlevideo recusar a conexão do IP da
    VM (403), ou o formato pode ser um que o FFmpeg não decodifica. Esse
    teste cobre o pedaço do caminho que o resto do doctor não vê.
    """
    print("\n── playback pelo FFmpeg (o teste que importa) ────────────")
    if shutil.which("ffmpeg") is None:
        _line(BAD, "ffmpeg", "ausente — impossível testar playback")
        return False
    try:
        info = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: youtube._extract_once(
                f"https://www.youtube.com/watch?v={youtube.PROBE_VIDEO_ID}", profile
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _line(BAD, "extração", youtube.friendly_error(exc))
        return False

    # mesmos parâmetros do bot (services/youtube.py), 3s de áudio para stdout
    comando = [
        "ffmpeg", *youtube.FFMPEG_BEFORE.split(), "-t", "3", "-i", info["url"],
        *youtube.FFMPEG_OPTIONS.split(), "-f", "s16le", "-ar", "48000", "-ac", "2", "-",
    ]
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *comando, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        saida, erro = await asyncio.wait_for(proc.communicate(), timeout=40)
    except asyncio.TimeoutError:
        proc.kill()
        _line(BAD, profile.name, "FFmpeg travou (>40s) — CDN pendurado")
        return False
    took = time.monotonic() - started

    # 3s de PCM 48kHz estéreo 16bit = 576000 bytes; aceito metade por margem
    if len(saida) > 288000:
        _line(OK, profile.name, f"{len(saida) / 1024:.0f} KB de PCM em {took:.1f}s — TOCA")
        return True
    else:
        ultima = (erro or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        _line(
            BAD,
            profile.name,
            f"só {len(saida)} bytes de áudio em {took:.1f}s"
            + (f" — {ultima[0][:110]}" if ultima else ""),
        )
        return False


async def perfis() -> int:
    print("\n── perfis de extração (um por um, neste IP) ──────────────")
    chain = youtube.profiles()
    if not chain:
        _line(BAD, "cadeia", "vazia — confira YTDLP_PROFILES no .env")
        return 1

    vencedores = []
    for profile in chain:
        started = time.monotonic()
        try:
            info = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda p=profile: youtube._extract_once(
                    f"https://www.youtube.com/watch?v={youtube.PROBE_VIDEO_ID}", p
                ),
            )
        except Exception as exc:  # noqa: BLE001 — catalogar a falha é o objetivo
            _line(BAD, profile.name, youtube.friendly_error(exc))
            continue
        took = time.monotonic() - started
        if not info.get("url"):
            _line(BAD, profile.name, f"{took:.1f}s — formato sem URL (SABR)")
            continue
        vcodec = info.get("vcodec")
        so_audio = vcodec in (None, "none")
        _line(
            OK if so_audio else WARN,
            profile.name,
            f"{took:.1f}s — formato {info.get('format_id')}"
            f" {'(audio-only, ' + str(info.get('acodec')) + ')' if so_audio else '(COM VÍDEO: mais banda e pior áudio)'}",
        )
        vencedores.append(profile.name)

    toca = True
    if vencedores:
        toca = await tocar_de_verdade(chain[[p.name for p in chain].index(vencedores[0])])

    alertas = youtube.alerts()
    if alertas:
        print("\n── avisos que o yt-dlp deu durante o teste ──────────────")
        for texto in alertas:
            _line(WARN, "yt-dlp", texto)

    print("\n── veredito ─────────────────────────────────────────────")
    if not vencedores:
        print(
            "NENHUM perfil extraiu áudio neste IP.\n"
            "  1. atualize o yt-dlp:  .venv/bin/pip install -U 'yt-dlp[default]'\n"
            "  2. instale um runtime de JS (deno ou node) se o aviso acima apareceu\n"
            "  3. renove data/cookies.txt (conta descartável) OU remova o arquivo\n"
            "     para o bot voltar ao modo anônimo — os dois cenários mudam os\n"
            "     clients que o yt-dlp escolhe."
        )
        return 1
    if not toca:
        print(f"{vencedores[0]} EXTRAI mas o FFmpeg não tira áudio da URL — o bot fica mudo.")
        return 2
    print(f"funcionam: {', '.join(vencedores)}")
    if vencedores[0] != chain[0].name:
        print(
            f"dica: ponha YTDLP_PROFILES={','.join(vencedores)} no .env para o bot"
            " não gastar a primeira tentativa num perfil morto após cada restart."
        )
    return 0


async def _run() -> int:
    await ambiente()
    codigo = await perfis()
    from services import http

    await http.close()  # a checagem de versão abre uma sessão aiohttp
    return codigo


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
