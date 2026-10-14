"""Busca e extração de áudio: ytmusicapi para busca musical, yt-dlp para o stream."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import re
import unicodedata
from urllib.parse import parse_qs, urlsplit

import aiohttp
import yt_dlp
from ytmusicapi import YTMusic

import config
from services import jsc_cache

log = logging.getLogger(__name__)

# sem isto cada extração com o perfil de cookies refazia o parse do player JS
# inteiro no deno — 11 a 18s de CPU cheia na VM a cada faixa (ver jsc_cache)
CACHE_DE_PLAYER = jsc_cache.ativa()

_YDL_OPTS = {
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    # sem socket_timeout, um CDN que trava sem fechar a conexão prendia a
    # thread do pool para sempre (e o pool tem 4 slots para o bot inteiro)
    "socket_timeout": 10,
    "retries": 2,
    # o retry interno do extractor multiplica pelo número de perfis da cadeia
    # (_PROFILES): 3 tentativas × 4 perfis = 12 idas ao YouTube por faixa morta
    "extractor_retries": 1,
}


# Avisos do yt-dlp que explicam uma quebra futura e não podem se perder no
# meio do ruído: são promovidos a WARNING no log e ficam visíveis no /diag.
_AVISOS_IMPORTANTES = (
    ("no longer valid", "os cookies do YouTube venceram (rotacionados pela conta)"),
    ("only images are available", "o YouTube só ofereceu imagens — client bloqueado"),
    ("sabr streaming", "o YouTube forçou SABR neste client (formatos sem URL)"),
    ("no supported javascript runtime", "sem runtime de JS: o yt-dlp perde os clients bons"),
)

_alertas: dict[str, float] = {}  # texto -> quando apareceu pela última vez
_ALERTA_TTL_S = 6 * 3600


def alerts() -> list[str]:
    """Avisos recentes do yt-dlp que explicam degradação (para o /diag)."""
    limite = _now() - _ALERTA_TTL_S
    for texto, visto in list(_alertas.items()):
        if visto < limite:
            del _alertas[texto]
    return list(_alertas)


class _YdlLogger:
    """Manda a saída do yt-dlp para o logging do bot em vez do stderr.

    Sem isso a cadeia de perfis polui o journal: a falha do primeiro perfil é
    ESPERADA (é o mecanismo funcionando) mas o yt-dlp imprime "ERROR: ..."
    direto no stderr mesmo com quiet=True.
    """

    def debug(self, msg: str) -> None:
        log.debug("%s", msg)

    def info(self, msg: str) -> None:
        log.debug("%s", msg)

    def warning(self, msg: str) -> None:
        low = str(msg).lower()
        for marker, texto in _AVISOS_IMPORTANTES:
            if marker in low:
                # só uma vez por janela: o aviso repete a cada extração
                if _now() - _alertas.get(texto, -_ALERTA_TTL_S) > 300:
                    log.warning("yt-dlp: %s", texto)
                _alertas[texto] = _now()
                return
        log.debug("%s", msg)

    def error(self, msg: str) -> None:
        # quem decide se isto vira problema é _extract (pode ter próximo perfil)
        log.debug("%s", msg)


def _js_runtimes() -> dict:
    """Runtimes de JS habilitados (config.YTDLP_JS_RUNTIMES → opção do yt-dlp).

    Sem runtime nenhum o yt-dlp cai no conjunto "jsless" — UM client só, sem
    alternativa — e o áudio despenca para o formato 18 (mp4 com vídeo junto).
    Só `deno` vem ligado de fábrica; habilitar os outros é de graça (o yt-dlp
    testa disponibilidade e usa o de maior prioridade que existir).
    """
    runtimes: dict[str, dict] = {}
    for item in config.YTDLP_JS_RUNTIMES.split(","):
        name, _, path = item.strip().partition(":")
        if name:
            runtimes[name] = {"path": path} if path else {}
    return runtimes


def js_runtime_in_use() -> str:
    """Nome do runtime de JS que o yt-dlp usaria agora ('' = nenhum). Para o /diag."""
    try:
        from yt_dlp.globals import supported_js_runtimes
    except ImportError:  # versão de yt-dlp sem EJS
        return ""
    enabled = _js_runtimes()
    for name, cls in supported_js_runtimes.value.items():
        if name not in enabled:
            continue
        try:
            info = cls(enabled[name].get("path")).info
        except Exception:  # noqa: BLE001 — diagnóstico não pode derrubar nada
            continue
        if info is not None and getattr(info, "supported", False):
            return f"{name} {info.version}"
    return ""

# -nostdin: ffmpeg não pode disputar o stdin do processo; probesize e
# analyzeduration mínimos porque o formato do stream já é conhecido
# (bestaudio) — corta algumas centenas de ms do início de cada faixa
FFMPEG_BEFORE = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    # o googlevideo recusa com 403 o PRIMEIRO acesso a uma URL recém-extraída
    # e aceita o mesmo acesso 1s depois. Sem isto o FFmpeg morria na abertura:
    # em 20/09, 4 de 6 faixas pedidas na hora não tocaram (as do prefetch,
    # abertas 30s+ depois da extração, tocavam). Medido na VM, 6 aberturas de
    # URL fresca: 5 mortes sem a opção, 0 com ela (custa 1,3–4,4s de espera).
    # `-reconnect 1` não cobre: só vale para queda no MEIO do stream.
    " -reconnect_on_http_error 403,5xx -reconnect_on_network_error 1"
    # rw_timeout (15s, em µs): CDN travado sem fechar a conexão deixava o
    # ffmpeg pendurado para sempre — o after nunca disparava e o bot ficava
    # mudo na call até alguém reiniciar
    " -rw_timeout 15000000"
    " -probesize 65536 -analyzeduration 200000"
)
FFMPEG_OPTIONS = "-vn"

# Executor próprio do yt-dlp: (a) não disputa o executor default com o
# whisper local e o spotipy; (b) permite reusar UMA instância YoutubeDL por
# thread (o cache de assinatura/nsig e a sessão HTTP dela valem ouro — criar
# uma nova por chamada pagava player JS + TLS a cada faixa).


def _pool_size() -> int:
    """Quantas extrações simultâneas — casado com a CPU, não um número fixo.

    Extrair é CPU-bound: o yt-dlp roda o desafio JS do player do YouTube num
    runtime externo. Medido na VM de produção (2 vCPU) extraindo de verdade:

        simultâneas   por faixa   vazão
                  1        8,3s   0,12/s
                  2       21,9s   0,09/s
                  4       66,3s   0,05/s

    Ou seja, concorrência acima dos núcleos piora latência E vazão — e ainda
    rouba CPU do FFmpeg e da thread de voz. Com os 4 workers fixos que havia
    aqui, quatro comandos seguidos faziam cada extração levar 66s e o bot
    ficava "travado sem tocar". Metade dos núcleos deixa folga para o áudio.
    """
    bruto = config.YTDLP_WORKERS.strip()
    if bruto:
        try:
            pedido = int(bruto)
            if pedido > 0:
                return pedido
        except ValueError:
            log.warning("YTDLP_WORKERS=%r não é um número — usando o automático", bruto)
    return max(1, min(4, (os.cpu_count() or 2) // 2))


PRIO_FOREGROUND = 0  # alguém está esperando para tocar ou para responder
PRIO_PREFETCH = 1  # adiantamento e prewarm: só quando não atrasa ninguém

# Extrair é CPU-bound e pode esperar; mandar áudio precisa de pouquíssima CPU
# mas precisa dela A CADA 20ms. Sem isto, com a música tocando, a extração
# levava de 12,7s (mediana) a 48,3s num núcleo só — e o áudio cortava. No
# Linux o nice vale por thread e é herdado pelos subprocessos (o deno, que é
# quem realmente queima CPU no desafio JS do YouTube).
_NICE_EXTRACAO = 10


def _baixa_prioridade_de_cpu() -> None:
    """Leva a thread atual ao nice _NICE_EXTRACAO. Silencioso onde não dá
    (Windows, sem permissão).

    `os.nice(n)` SOMA n ao nível atual — e a unit do systemd põe o processo em
    Nice=-5 para o áudio ganhar: somando 10 a extração pararia em +5. Aqui o
    alvo é absoluto, e nunca se tenta SUBIR a prioridade (exige privilégio).
    """
    try:
        falta = _NICE_EXTRACAO - os.nice(0)  # nice(0) = o nível atual, sem mexer
        if falta > 0:
            os.nice(falta)
    except (AttributeError, OSError, ValueError) as exc:
        log.debug("não consegui baixar a prioridade da thread de extração: %r", exc)


class _Job:
    __slots__ = ("fn", "priority", "seq", "future", "loop", "started", "label", "queued_at")

    def __init__(
        self, fn, priority: int, seq: int, loop: asyncio.AbstractEventLoop, label: str = ""
    ) -> None:
        self.fn = fn
        self.priority = priority
        self.seq = seq
        self.loop = loop
        self.future: asyncio.Future = loop.create_future()
        self.started = False
        self.label = label
        self.queued_at = time.monotonic()


def _entrega(future: asyncio.Future, valor=None, erro: BaseException | None = None) -> None:
    if future.done():
        return
    if erro is not None:
        future.set_exception(erro)
    else:
        future.set_result(valor)


class _Scheduler:
    """Pool de threads com fila por PRIORIDADE — a do ThreadPoolExecutor é FIFO.

    Log de produção (12/09, 15:32): embaralhar e pular deixou a faixa
    seguinte 21,4s "extraindo" — na verdade parada na fila FIFO, atrás de duas
    extrações de prefetch que o embaralhar tinha tornado inúteis. Com 1 worker
    na VM, a ordem da fila É a latência. Aqui quem vai tocar agora
    (PRIO_FOREGROUND) passa na frente de qualquer prefetch ainda não
    iniciado; um job na fila pode subir de prioridade (`bump`) quando o
    foreground pede a mesma faixa; e um job que ninguém espera mais pode ser
    tirado da fila (`cancel`) antes de gastar o worker.
    """

    def __init__(self, workers: int, name: str = "ytdlp") -> None:
        self._max_workers = workers  # lido por main.py e doctor.py
        self._cond = threading.Condition()
        self._pending: list[_Job] = []
        self._running = 0
        self._seq = 0
        for i in range(workers):
            threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True).start()

    def submit(self, fn, *, priority: int = PRIO_FOREGROUND, label: str = "") -> _Job:
        job = _Job(fn, priority, self._seq, asyncio.get_running_loop(), label)
        with self._cond:
            self._seq += 1
            self._pending.append(job)
            self._cond.notify()
        return job

    def bump(self, job: _Job, priority: int) -> None:
        with self._cond:
            if not job.started and priority < job.priority:
                job.priority = priority

    def cancel(self, job: _Job) -> bool:
        """Tira o job da fila se ainda não começou. False se já está rodando."""
        with self._cond:
            if job.started or job not in self._pending:
                return False
            self._pending.remove(job)
            return True

    def pending(self) -> int:
        with self._cond:
            return len(self._pending)

    def busy(self) -> bool:
        with self._cond:
            return self._running > 0 or bool(self._pending)

    def _run(self) -> None:
        _baixa_prioridade_de_cpu()
        while True:
            with self._cond:
                while not self._pending:
                    self._cond.wait()
                job = min(self._pending, key=lambda j: (j.priority, j.seq))
                self._pending.remove(job)
                job.started = True
                self._running += 1
            inicio = time.monotonic()
            try:
                resultado, erro = job.fn(), None
            except BaseException as exc:  # noqa: BLE001 — vai para quem espera
                resultado, erro = None, exc
            finally:
                with self._cond:
                    self._running -= 1
            # espera × execução: é o que separa "o pool estava cheio" de "a
            # máquina está lenta" (12/09: 21,1s "em voo" sem saber qual dos dois)
            log.info(
                "[pool] %s: esperou %.1fs na fila, rodou %.1fs (prio %d)%s",
                job.label or "job", inicio - job.queued_at, time.monotonic() - inicio,
                job.priority, " — FALHOU" if erro is not None else "",
            )
            try:
                job.loop.call_soon_threadsafe(_entrega, job.future, resultado, erro)
            except RuntimeError:
                pass  # loop já fechado (testes): ninguém espera esse resultado


_YTDLP_POOL = _Scheduler(_pool_size())
_thread_ydl = threading.local()

# Prefetch ocupa no máximo 1 dos 4 workers: com vários servidores, os
# prefetches de uns roubavam os 2 slots antigos e o início de faixa dos
# outros (foreground) esperava — gap de silêncio audível entre músicas.
PREFETCH_SEM = asyncio.Semaphore(1)


# vídeo de sonda ("Me at the zoo", o mais antigo do YouTube): público, sem
# restrição de idade/região e não vai sair do ar
PROBE_VIDEO_ID = "jNQXAC9IVRw"

# teto de faixas importadas de uma playlist do YouTube — mesmo limite da
# prévia do Spotify. Cada página da listagem são ~100 itens numa ida ao
# YouTube; e a fila tem teto próprio (core/queue.MAX_QUEUE).
PLAYLIST_MAX = 100


# ── Frescor do yt-dlp ────────────────────────────────────────────────────────
# O apagão de agosto/2026 foi, no fundo, uma versão pinada que envelheceu sem
# ninguém perceber. O bot NÃO se atualiza sozinho (o pin é deliberado, ver
# requirements.txt) — ele só avisa, no log e no /diag, que ficou para trás.
_RELEASE_URL = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
_RELEASE_TTL_S = 12 * 3600
_release_cache: tuple[float, str | None] | None = None  # (quando, versão nova ou None)


def installed_version() -> str:
    return yt_dlp.version.__version__


def _version_tuple(text: str) -> tuple[int, ...] | None:
    parts = text.strip().lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None  # tag fora do padrão data (ex.: "nightly")


def _is_outdated(instalada: str, publicada: str) -> bool:
    a, b = _version_tuple(instalada), _version_tuple(publicada)
    return bool(a and b and a < b)


async def _fetch_latest_release() -> str:
    from services import http

    async with http.get_session().get(
        _RELEASE_URL,
        headers={"Accept": "application/vnd.github+json"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        return ((await resp.json()).get("tag_name") or "").strip()


async def outdated_release() -> str | None:
    """Versão publicada do yt-dlp, se for mais nova que a instalada. Senão None."""
    global _release_cache
    if _release_cache is not None and _now() - _release_cache[0] < _RELEASE_TTL_S:
        return _release_cache[1]
    try:
        publicada = await _fetch_latest_release()
    except Exception as exc:  # noqa: BLE001 — checar versão nunca pode quebrar o bot
        log.debug("não consegui checar a versão do yt-dlp: %r", exc)
        return None
    nova = publicada if _is_outdated(installed_version(), publicada) else None
    _release_cache = (_now(), nova)
    return nova


def _cookie_state() -> tuple[str, float]:
    """(path, mtime) do cookies.txt — muda quando o arquivo é trocado."""
    path = config.YTDLP_COOKIES
    if path and os.path.exists(path):
        return (path, os.path.getmtime(path))
    return ("", 0.0)


# ── Perfis de extração ───────────────────────────────────────────────────────
# O YouTube derruba clients do yt-dlp o tempo todo, e a escolha do client é
# implícita: BASTA existir cookies.txt para o yt-dlp trocar o conjunto padrão
# pelo "autenticado". Foi exatamente isso que quebrou o bot em agosto/2026 —
# os cookies (postos para vencer o bot-check do IP de datacenter) passaram a
# selecionar o client `tv_downgraded`, que responde
# "The page needs to be reloaded." em TODO vídeo. Sem alternativa configurada,
# cada faixa da fila morria com esse erro.
#
# Em vez de fixar um client (que o YouTube quebra de novo mês que vem), o bot
# mantém uma cadeia: tenta o perfil que funcionou por último e, se falhar,
# percorre os outros. O primeiro que devolver uma URL vira o preferido.


@dataclass(frozen=True)
class Profile:
    name: str
    use_cookies: bool
    # vazio = deixa o yt-dlp escolher (ele sabe mais que a gente sobre o que
    # está de pé hoje); preenchido = força a lista.
    player_client: tuple[str, ...] = field(default=())

    def opts(self, cookiefile: str, *, flat: bool = False) -> dict:
        opts = dict(_YDL_OPTS)
        opts["js_runtimes"] = _js_runtimes()
        opts["logger"] = _YdlLogger()
        if self.use_cookies and cookiefile:
            opts["cookiefile"] = cookiefile
        if self.player_client:
            opts["extractor_args"] = {"youtube": {"player_client": list(self.player_client)}}
        if flat:
            # busca e playlist: SÓ a lista (id, título, duração, canal, capa).
            # Sem isto o yt-dlp resolvia formato (desafio JS, ~8s na VM) de
            # CADA entrada: 5 resultados de busca = 40s; uma playlist de 183
            # vídeos = ~25min com o pool serializado preso e o bot mudo.
            opts["extract_flat"] = "in_playlist"
            opts["playlistend"] = PLAYLIST_MAX
        return opts


_ALL_PROFILES: tuple[Profile, ...] = (
    # anônimo: em 2026.08.19 os defaults são (visionos, web) — os que ainda
    # entregam opus 251 (audio-only) de verdade
    Profile("padrao", use_cookies=False),
    Profile("cookies", use_cookies=True),
    # workaround publicado pelo mantenedor no yt-dlp#17389: tira o
    # tv_downgraded da frente quando ele está respondendo UNPLAYABLE
    Profile("cookies_embedded", use_cookies=True, player_client=("default", "web_embedded")),
    # último recurso: clients móveis. Costuma cair no formato 18 (mp4 com
    # vídeo, áudio AAC) — pior qualidade e mais banda, mas toca.
    Profile("movel", use_cookies=False, player_client=("tv_simply", "mweb", "android_vr")),
)

_profiles: list[Profile] = []
_profiles_state: tuple | None = None  # (cookie_state, env) que gerou a lista
_active = 0  # índice do perfil preferido (sticky)
_active_since = 0.0  # quando o sticky saiu do topo — base da reavaliação
# de tempos em tempos vale reconferir o topo da cadeia: o perfil de baixo pode
# estar tocando em formato pior (mp4 18) muito depois de o bom ter voltado
_PROFILE_RECHECK_S = 30 * 60
# -inf e não 0.0: no Linux time.monotonic() começa perto de zero, então
# 0.0 adiaria a primeira varredura em 30min depois de cada boot
_quality_probe_at = float("-inf")  # última varredura por formato audio-only


def _build_profiles() -> list[Profile]:
    has_cookies = bool(_cookie_state()[0])
    usable = [p for p in _ALL_PROFILES if has_cookies or not p.use_cookies]
    wanted = [n.strip() for n in (config.YTDLP_PROFILES or "").split(",") if n.strip()]
    if wanted:
        by_name = {p.name: p for p in usable}
        chosen = [by_name[n] for n in wanted if n in by_name]
        if chosen:
            return chosen
        log.warning("YTDLP_PROFILES=%r não casa com nenhum perfil — usando a ordem padrão", wanted)
    return usable


def reset_profiles() -> None:
    """Recalcula a cadeia (cookies apareceram/sumiram, env mudou) e zera o sticky."""
    global _profiles, _profiles_state, _active, _active_since, _quality_probe_at
    _profiles = _build_profiles()
    _profiles_state = (_cookie_state(), config.YTDLP_PROFILES)
    _active = 0
    _active_since = 0.0
    _quality_probe_at = float("-inf")


def profiles() -> list[Profile]:
    if _profiles_state != (_cookie_state(), config.YTDLP_PROFILES):
        reset_profiles()
    return _profiles


def profile_names() -> list[str]:
    return [p.name for p in profiles()]


def active_profile() -> str:
    chain = profiles()
    return chain[_active % len(chain)].name if chain else ""


def set_active_profile(name: str) -> None:
    global _active, _active_since
    names = profile_names()
    if name in names:
        _active = names.index(name)
        _active_since = _now() if _active else 0.0


# O yt-dlp REESCREVE o cookiefile ao fim de cada execução com o que o servidor
# devolveu. Quando a conta burner é invalidada, ele apaga os cookies de login
# do arquivo e o bot vira anônimo em silêncio — o aviso "no longer valid" sai
# uma única vez e some no journal. Estas funções tornam esse estado legível.
_COOKIES_DE_LOGIN = ("LOGIN_INFO", "SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")


def cookies_status() -> tuple[str, str]:
    """(estado, explicação) do arquivo de cookies. Estado: ausente|sem-login|ok."""
    path = config.YTDLP_COOKIES
    if not path:
        return "ausente", "YTDLP_COOKIES vazio — modo anônimo"
    if not os.path.exists(path):
        return "ausente", f"sem arquivo em {path} — modo anônimo"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            nomes = {
                line.split("\t")[5]
                for line in fh
                if not line.startswith("#") and line.count("\t") >= 6
            }
    except OSError as exc:
        return "sem-login", f"{path} ilegível ({exc})"
    if "LOGIN_INFO" in nomes and nomes.intersection(_COOKIES_DE_LOGIN[1:]):
        return "ok", f"{path} com cookies de login"
    return (
        "sem-login",
        f"{path} existe mas PERDEU os cookies de login — a conta foi invalidada e"
        " o yt-dlp reescreveu o arquivo. Renove ou apague o arquivo.",
    )


def cookies_age_days() -> float | None:
    """Idade do arquivo de cookies em dias (None se não há arquivo)."""
    path, mtime = _cookie_state()
    return (time.time() - mtime) / 86400 if path else None


def _get_ydl(profile: Profile, *, flat: bool = False) -> yt_dlp.YoutubeDL:
    """YoutubeDL da thread atual para este perfil (recriada se os cookies mudarem).

    Uma instância por (thread, perfil, flat): o cache de assinatura/nsig e a
    sessão HTTP dela valem ouro — criar uma nova por chamada pagava player JS
    + TLS a cada faixa. A variante flat (busca/playlist) é outra instância
    porque as opções são fixadas na construção.
    """
    state = _cookie_state()
    cache = getattr(_thread_ydl, "cache", None)
    if cache is None or _thread_ydl.state != state:
        cache = _thread_ydl.cache = {}
        _thread_ydl.state = state
    chave = (profile.name, flat)
    ydl = cache.get(chave)
    if ydl is None:
        ydl = cache[chave] = yt_dlp.YoutubeDL(profile.opts(state[0], flat=flat))
    return ydl


# Cache de extração — é o que torna o prefetch da próxima faixa barato e o que
# poupa a extração (4–5s, deno a toda) quando a mesma música volta na noite.
# A validade de cada entrada vem do `expire=` da própria URL (~6h); eram 3h
# fixas, metade da vida útil jogada fora. _STREAM_TTL_S segue valendo para URL
# sem `expire` (outros sites, formato inesperado).
_STREAM_TTL_S = 3 * 3600
_STREAM_TTL_MAX_S = 6 * 3600  # teto: expire absurdo não vira cache eterno
# a URL tem de valer até a música ACABAR: o CDN derruba a conexão a cada ~31s e
# cada reconexão do FFmpeg revalida o expire
_MARGEM_EXPIRE_S = 30 * 60
_FOLGA_DEPOIS_DA_FAIXA_S = 5 * 60
_STREAM_CACHE_MAX = 50
# video_id -> (nasceu_em, info, validade em segundos a partir do nascimento)
_stream_cache: dict[str, tuple[float, dict, float]] = {}
_now = time.monotonic  # indireção para os testes controlarem o relógio
_EXPIRE_RE = re.compile(r"[?&]expire=(\d{9,11})(?:&|$)")


def _relogio_real() -> float:
    """Relógio de parede (o `expire` da URL é epoch). Função, e não alias, para
    os testes que trocam `youtube.time.time` continuarem valendo."""
    return time.time()


def _validade_s(info: dict) -> float:
    """Por quantos segundos, a partir de agora, esta extração ainda serve."""
    achado = _EXPIRE_RE.search(str(info.get("stream_url") or ""))
    if achado is None:
        return _STREAM_TTL_S
    folga = max(_MARGEM_EXPIRE_S, int(info.get("duration") or 0) + _FOLGA_DEPOIS_DA_FAIXA_S)
    return min(_STREAM_TTL_MAX_S, int(achado.group(1)) - _relogio_real() - folga)


class _Voo:
    """Uma extração em andamento: o job no pool, a task que normaliza o
    resultado e quantos estão esperando por ela."""

    __slots__ = ("url", "job", "task", "waiters")

    def __init__(self, url: str) -> None:
        self.url = url
        self.job: _Job | None = None
        self.task: asyncio.Task | None = None
        self.waiters = 0


# extrações em voo, por URL — dedupe de pedidos simultâneos da mesma faixa
_inflight: dict[str, _Voo] = {}


def stream_state(video_id: str | None) -> str:
    """Para o log: "no cache", "em voo" (já pedida) ou "fria"."""
    if not video_id:
        return "sem vídeo"
    if _cache_get(video_id) is not None:
        return "no cache"
    if f"https://www.youtube.com/watch?v={video_id}" in _inflight:
        return "em voo"
    return "fria"


def _cache_get(video_id: str) -> dict | None:
    entry = _stream_cache.get(video_id)
    if entry is None:
        return None
    born, info, validade = entry
    if _now() - born > validade:
        del _stream_cache[video_id]
        return None
    return info


def _cache_put(video_id: str, info: dict) -> None:
    validade = _validade_s(info)
    if validade <= 0:
        return  # já nasce perto demais do expire: não vale guardar
    _stream_cache[video_id] = (_now(), info, validade)
    while len(_stream_cache) > _STREAM_CACHE_MAX:
        oldest = min(_stream_cache, key=lambda k: _stream_cache[k][0])
        del _stream_cache[oldest]


def dump_cache(path: str) -> int:
    """Salva o cache em disco para sobreviver a um restart. Devolve quantas salvou.

    O cache marca idade com `time.monotonic()`, que ZERA a cada processo — por
    isso a validade vai para o arquivo como relógio de parede. Sem isto, todo
    deploy jogava as URLs quentes fora e a próxima faixa pagava os ~8,3s de
    extração de novo (com o pool serializado, isso é silêncio no canal).
    """
    agora_mono, agora_real = _now(), _relogio_real()
    itens = {
        video_id: {"expira_em": agora_real + (validade - (agora_mono - nasceu)),
                   "info": info}
        for video_id, (nasceu, info, validade) in _stream_cache.items()
        if agora_mono - nasceu < validade
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(itens, fh)
    except OSError as exc:
        log.warning("não consegui salvar o cache de streams: %s", exc)
        return 0
    return len(itens)


def load_cache(path: str) -> int:
    """Recarrega o cache salvo, descartando o que já venceu. Devolve quantas voltaram."""
    try:
        with open(path, encoding="utf-8") as fh:
            itens = json.load(fh)
    except (OSError, ValueError) as exc:
        log.debug("sem cache de streams para recarregar: %s", exc)
        return 0
    agora_mono, agora_real = _now(), _relogio_real()
    recuperadas = 0
    try:
        for video_id, entrada in itens.items():
            restante = float(entrada["expira_em"]) - agora_real
            if restante <= 0:
                continue
            # nasce agora, no monotonic DESTE processo, valendo só o que restava
            _stream_cache[video_id] = (agora_mono, entrada["info"], restante)
            recuperadas += 1
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        log.warning("cache de streams em formato inesperado, ignorando: %s", exc)
        return recuperadas
    return recuperadas


def is_cached(video_id: str) -> bool:
    """A próxima get_stream desta faixa sai do cache? (mede se o prefetch pegou)"""
    return _cache_get(video_id) is not None


def invalidate(video_id: str) -> None:
    """Descarta a entrada do cache (ex.: URL vencida antes do TTL)."""
    _stream_cache.pop(video_id, None)
    _failures.pop(video_id, None)


# Cache NEGATIVO, curto: a faixa que acabou de falhar na cadeia inteira não
# vai funcionar 5s depois. Sem isto, /play <link> pelo oEmbed enfileirava
# uma faixa cuja extração em background já tinha morrido (idade/país/DRM) e,
# na hora de tocar, _try_start pagava a cadeia de novo (até 25s no worker
# único) para falhar igual. use_cache=False continua ignorando (retentativa
# deliberada, sonda do boot).
_FAILURE_TTL_S = 60.0
# Erro que é do VÍDEO não passa em 60s: em 27/09 uma faixa com restrição de
# idade pagou a extração inteira de novo (~6s no worker único) 94s depois da
# primeira falha. Esses ficam lembrados por horas.
_FAILURE_DO_VIDEO_TTL_S = 6 * 3600.0
# "video unavailable" fica de fora: às vezes depende do client (ver _PERMANENT)
_DO_VIDEO = (
    "age-restricted", "confirm your age", "inappropriate for some users", "private video",
    "removed by the uploader", "has been terminated", "who has blocked it in your country",
)
_FAILURES_MAX = 500
_failures: dict[str, tuple[float, BaseException]] = {}


def _falha_do_video(exc: BaseException) -> bool:
    low = str(exc).lower()
    return any(marca in low for marca in _DO_VIDEO)


def _lembra_falha(video_id: str, exc: BaseException) -> None:
    if len(_failures) >= _FAILURES_MAX:
        _failures.pop(next(iter(_failures)))
    _failures[video_id] = (_now(), exc)


def _recent_failure(video_id: str) -> BaseException | None:
    entrada = _failures.get(video_id)
    if entrada is None:
        return None
    quando, exc = entrada
    if _now() - quando > (_FAILURE_DO_VIDEO_TTL_S if _falha_do_video(exc) else _FAILURE_TTL_S):
        del _failures[video_id]
        return None
    return exc

_ytmusic: YTMusic | None = None


def _ytm() -> YTMusic:
    global _ytmusic
    if _ytmusic is None:
        _ytmusic = YTMusic()
    return _ytmusic


def parse_duration(text: str | None) -> int:
    """'3:45' -> 225; '1:02:03' -> 3723; lixo -> 0."""
    if not text:
        return 0
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


# Barreira anti-SSRF do /play: sem allowlist, qualquer URL ia crua para o
# yt-dlp e o bot buscava recursos internos da VM (ex.: metadata da nuvem).
# Hostname EXATO via urlsplit — derrota "youtube.com.evil.com",
# "youtube.com@evil.com" e IP literal.
_ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def is_allowed_url(text: str) -> bool:
    """URL http(s) de um domínio confiável (YouTube)."""
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in _ALLOWED_HOSTS


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,}$")


def extract_video_id(text: str) -> str | None:
    """Id do vídeo numa URL do YouTube (watch, youtu.be, shorts, embed, live).

    Trabalhar pelo id e não pela URL crua é o que faz `/play <link>` achar
    o cache e dividir a extração em voo com o player (que pede pelo id).
    """
    if not is_allowed_url(text):
        return None
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    candidato = ""
    if host == "youtu.be":
        candidato = parts.path.strip("/").split("/")[0]
    else:
        segmentos = [s for s in parts.path.split("/") if s]
        if segmentos and segmentos[0] in ("shorts", "embed", "live", "v") and len(segmentos) > 1:
            candidato = segmentos[1]
        else:
            candidato = parse_qs(parts.query).get("v", [""])[0]
    return candidato if _VIDEO_ID_RE.match(candidato) else None


def extract_playlist_id(text: str) -> str | None:
    """Id da playlist numa URL de playlist PURA (`/playlist?list=`).

    Link de vídeo com `&list=` continua sendo o vídeo — é o que a pessoa
    compartilhou; a playlist inteira só quando é o link dela.
    """
    if not is_allowed_url(text) or extract_video_id(text) is not None:
        return None
    parts = urlsplit(text)
    # `/watch?list=…` SEM `v=` também é playlist (é como o yt-dlp trata)
    if parts.path.rstrip("/") not in ("/playlist", "/watch"):
        return None
    candidato = parse_qs(parts.query).get("list", [""])[0]
    return candidato if _LIST_ID_RE.match(candidato) else None


async def _run(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))


def _normalize_ytm(item: dict) -> dict:
    artists = item.get("artists") or []
    thumbs = item.get("thumbnails") or item.get("thumbnail") or []
    return {
        "video_id": item.get("videoId"),
        "title": item.get("title", "?"),
        "artist": ", ".join(a["name"] for a in artists if a.get("name")),
        "duration": item.get("duration_seconds")
        or parse_duration(item.get("duration") or item.get("length")),
        "thumbnail": thumbs[-1]["url"] if thumbs else "",
    }


_VIDEOS_MORTOS = ("[private video]", "[deleted video]", "[unavailable video]")


def _normalize_flat(entry: dict) -> dict:
    """Entrada flat do yt-dlp (busca/playlist) no formato que o bot consome.

    Em modo flat `thumbnail` vem None — a capa está na lista `thumbnails`.
    """
    thumbs = entry.get("thumbnails") or []
    return {
        "video_id": entry["id"],
        "title": entry.get("title") or "?",
        "artist": entry.get("channel") or entry.get("uploader") or "",
        "duration": int(entry.get("duration") or 0),
        "thumbnail": entry.get("thumbnail") or (thumbs[-1].get("url", "") if thumbs else ""),
    }


def _flat_entries(info: dict) -> list[dict]:
    found = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        if (entry.get("title") or "").lower() in _VIDEOS_MORTOS:
            continue  # playlist com vídeo privado/apagado: pular já, sem tentar tocar
        found.append(_normalize_flat(entry))
    return found


def _ytdlp_search(query: str, limit: int) -> list[dict]:
    """Busca pelo próprio yt-dlp (`ytsearchN:`) — o plano B da busca."""
    info = _extract(f"ytsearch{limit}:{query}", playlist=True)
    return _flat_entries(info)[:limit]


def _ytdlp_playlist(list_id: str) -> tuple[str, list[dict], int]:
    info = _extract(f"https://www.youtube.com/playlist?list={list_id}", playlist=True)
    faixas = _flat_entries(info)
    # o total REAL da playlist: playlistend corta antes do filtro de vídeos
    # mortos, então len(faixas) não diz se houve corte
    total = int(info.get("playlist_count") or 0) or len(faixas)
    return (info.get("title") or "Playlist"), faixas, total


async def playlist_tracks(list_id: str) -> tuple[str, list[dict], int]:
    """(nome, faixas, total na playlist) de uma playlist do YouTube — só a
    lista, em uma ida. Levanta se não existir/for privada (o chamador avisa).
    """
    # foreground: alguém está esperando a resposta do /play
    return await _YTDLP_POOL.submit(
        functools.partial(_ytdlp_playlist, list_id), label=f"playlist {list_id}"
    ).future


async def search_songs(query: str, limit: int = 5) -> list[dict]:
    """Busca no YouTube Music; se ele falhar ou não achar nada, tenta o yt-dlp.

    O ytmusicapi era fonte ÚNICA: uma quebra dele (ou uma faixa que o YT Music
    não indexa — remix, ao vivo obscuro) derrubava a importação inteira do
    Spotify com "não achei essa música no YouTube", sem nem tentar o YouTube.
    """
    global _ytmusic
    try:
        results = await _run(_ytm().search, query, filter="songs", limit=limit)
        normalized = [_normalize_ytm(r) for r in results[:limit]]
        found = [r for r in normalized if r["video_id"]]
    except Exception as exc:  # noqa: BLE001 — o plano B existe justamente para isto
        log.warning("busca no YouTube Music falhou (%s) — caindo para o yt-dlp", exc)
        _ytmusic = None  # sessão HTTP podre ficava para sempre no singleton
        found = []
        ytm_de_pe = False
    else:
        ytm_de_pe = True
    if found:
        return found
    try:
        videos = await _YTDLP_POOL.submit(
            functools.partial(_ytdlp_search, query, limit), label="busca"
        ).future
    except Exception as exc:  # noqa: BLE001 — sem fonte nenhuma: o chamador avisa
        log.warning("busca pelo yt-dlp também falhou: %s", friendly_error(exc))
        return []
    return await _prefere_a_gravacao(query, videos) if ytm_de_pe else videos


def _palavras_da_busca(texto: str) -> set[str]:
    sem_acento = "".join(
        ch for ch in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(ch) != "Mn"
    )
    return set(re.findall(r"[a-z0-9]+", sem_acento))


async def _prefere_a_gravacao(query: str, videos: list[dict]) -> list[dict]:
    """O plano B achou um VÍDEO; se o título dele nomeia uma música que o YouTube
    Music tem, fica a gravação.

    Teste de 21/09: o Whisper escreveu "in the pool" como "Indepol"; o YouTube
    Music não tem música com esse nome, e o 1º vídeo da busca comum era um COVER
    de piano ("Chainsaw Man: Reze Arc OST - in the pool (Piano) | Kensuke Ushio").
    Medido na VM: buscar músicas pelo TÍTULO desse vídeo devolve, em 1º, a
    original ("in the pool — kensuke ushio"). O vídeo fica quando a música achada
    não é a que o título nomeia, ou quando a pessoa pediu algo que só o vídeo tem
    ("remix", "ao vivo no bar do zé") — é para isso que o plano B existe.
    """
    if not videos:
        return videos
    video = videos[0]
    try:
        results = await _run(_ytm().search, video["title"], filter="songs", limit=3)
    except Exception as exc:  # noqa: BLE001 — é cortesia: o vídeo já serve
        log.debug("segunda busca (pelo título do vídeo) falhou: %r", exc)
        return videos
    do_video = _palavras_da_busca(video["title"]) | _palavras_da_busca(video.get("artist", ""))
    pedidas = _palavras_da_busca(query)
    for item in results[:3]:
        musica = _normalize_ytm(item)
        titulo, artista = _palavras_da_busca(musica["title"]), _palavras_da_busca(musica["artist"])
        if not musica["video_id"] or not titulo or not titulo <= do_video or not artista & do_video:
            continue
        if (pedidas & do_video) - titulo - artista:
            continue  # a pessoa pediu algo que só o vídeo tem
        log.info(
            "busca: %r só achou o vídeo %r — o título dele nomeia a gravação %s — %s: fico com ela",
            query, video["title"], musica["title"], musica["artist"],
        )
        return [musica] + videos[1:]
    return videos


async def search_one(query: str) -> dict | None:
    results = await search_songs(query, limit=1)
    return results[0] if results else None


_OEMBED_URL = "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={id}&format=json"


async def quick_info(video_id: str) -> dict | None:
    """Título/canal/capa de um vídeo em ~0,3s (oEmbed), SEM extrair o áudio.

    É o que deixa o `/play <link>` responder na hora enquanto a extração
    (~8,3s na VM, e atrás do prefetch no pool serializado) segue em
    background aquecendo o cache. Sem duração: é preenchida quando a faixa
    for adiantada ou tocar. None em qualquer falha — aí o chamador espera a
    extração completa, que sabe dizer o erro certo (privado, removido...).
    """
    from services import http

    try:
        async with http.get_session().get(
            _OEMBED_URL.format(id=video_id), timeout=aiohttp.ClientTimeout(total=3)
        ) as resp:
            if resp.status != 200:
                return None
            dados = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 — atalho opcional: nunca derruba o /play
        log.debug("oembed de %s falhou: %r", video_id, exc)
        return None
    titulo = (dados or {}).get("title")
    if not titulo:
        return None
    return {
        "video_id": video_id,
        "title": titulo,
        "artist": dados.get("author_name") or "",
        "duration": 0,
        "thumbnail": dados.get("thumbnail_url") or "",
    }


async def related_tracks(video_id: str, limit: int = 5) -> list[dict]:
    """Faixas relacionadas (rádio do YouTube Music) — base do autoplay."""
    data = await _run(_ytm().get_watch_playlist, videoId=video_id, radio=True)
    tracks = [_normalize_ytm(t) for t in data.get("tracks", [])]
    return [t for t in tracks if t["video_id"] and t["video_id"] != video_id][:limit]


# Teto de tempo para percorrer a cadeia inteira em UMA faixa. Sem ele, 4
# perfis × socket_timeout × retries prendem os 4 slots do pool por minutos e o
# bot fica mudo em todos os servidores. Vale mais pular a faixa.
_EXTRACT_BUDGET_S = 25.0

# Erros que são do VÍDEO, não do client: trocar de perfil só queima tempo
# (4 perfis × ~2s em cada faixa morta de uma playlist de 50). "video
# unavailable" é ocasionalmente dependente do client — aceitamos perder essas
# raras em troca de não pagar a cadeia inteira em toda faixa morta.
_PERMANENT = (
    "video unavailable",
    "private video",
    "members-only",
    "this video is not available",
    "removed by the uploader",
    "account associated with this video has been terminated",
    "who has blocked it in your country",
    "is not a valid url",
    "unsupported url",
    # playlist morta/privada/mix: trocar de client não muda nada, e cada
    # tentativa é uma ida ao YouTube no worker único
    "playlist does not exist",
    "playlist is private",
    "unviewable",
)

# Tradução dos erros do yt-dlp para algo que o dono do bot entenda no chat.
_FRIENDLY = (
    ("the page needs to be reloaded", "o YouTube recusou a extração (client bloqueado)"),
    ("sign in to confirm", "o YouTube pediu login — os cookies venceram"),
    ("age-restricted", "vídeo com restrição de idade"),
    ("private video", "vídeo privado"),
    ("members-only", "vídeo só para membros do canal"),
    ("video unavailable", "vídeo indisponível"),
    ("requested format is not available", "sem formato de áudio disponível"),
    ("blocked it in your country", "vídeo bloqueado no país do servidor"),
    ("http error 429", "o YouTube limitou a taxa (429) — muitas buscas seguidas"),
    ("timed out", "o YouTube demorou demais para responder"),
)


def friendly_error(exc: BaseException) -> str:
    """Mensagem curta em PT-BR para o chat (o traceback vai para o log)."""
    raw = str(exc).replace("\n", " ").strip()
    low = raw.lower()
    for marker, texto in _FRIENDLY:
        if marker in low:
            return texto
    return raw[:157] + "…" if len(raw) > 160 else raw


def _is_permanent(exc: BaseException) -> bool:
    low = str(exc).lower()
    return any(marker in low for marker in _PERMANENT)


def _extract_once(url: str, profile: Profile, *, playlist: bool = False) -> dict:
    jsc_cache.manutencao()  # rotação dos players em cache (no máximo 1x/hora)
    # playlist/busca vão na instância flat: a lista, não o formato de cada item
    info = _get_ydl(profile, flat=playlist).extract_info(url, download=False)
    if not playlist and info and "entries" in info:
        entries = [e for e in info.get("entries") or [] if e]
        info = entries[0] if entries else None
    return info or {}


def _extract(url: str, *, playlist: bool = False) -> dict:
    """Extrai tentando os perfis a partir do preferido; o que funcionar vira preferido.

    playlist=True devolve o resultado sem achatar (busca `ytsearchN:`).
    """
    global _active, _active_since
    chain = profiles()
    if not chain:
        raise RuntimeError("nenhum perfil de extração configurado")
    start = _active % len(chain)
    if start and _now() - _active_since > _PROFILE_RECHECK_S:
        # janela vencida: recomeça do topo. Se o preferido continuar morto, a
        # rotação devolve o atual — custo de uma tentativa a cada 30min.
        start = 0
        _active_since = _now()
    deadline = _now() + _EXTRACT_BUDGET_S
    # Aceitar formato muxado (mp4 18: vídeo junto, áudio AAC pior) é melhor que
    # não tocar, mas não pode virar permanente — sem esta varredura o bot ficava
    # em 96kbps + banda de vídeo desperdiçada para sempre. Uma vez por janela.
    global _quality_probe_at
    caçando_audio = (
        not playlist
        and len(chain) > 1
        and _now() - _quality_probe_at > _PROFILE_RECHECK_S
    )
    muxado: dict | None = None
    muxado_index = -1
    last: BaseException | None = None
    for step in range(len(chain)):
        index = (start + step) % len(chain)
        profile = chain[index]
        if step and _now() > deadline:
            # a cadeia multiplica o pior caso: 4 perfis × socket_timeout ×
            # retries prenderia os 4 slots do pool por minutos, deixando TODOS
            # os servidores mudos. Melhor desistir desta faixa e ir para a
            # próxima (o breaker do player conta as falhas).
            log.warning("orçamento de extração estourado após o perfil %s", chain[index - 1].name)
            break
        try:
            info = _extract_once(url, profile, playlist=playlist)
            if playlist:
                if not info.get("entries"):
                    raise LookupError("busca sem resultados")
            elif not info.get("url"):
                # SABR: o YouTube devolve o formato sem URL. Sucesso aparente,
                # áudio nenhum — para a cadeia isso é falha igual a exceção.
                raise LookupError("nenhum formato de áudio com URL (SABR)")
        except Exception as exc:  # noqa: BLE001 — a cadeia existe para engolir e seguir
            last = exc
            if _is_permanent(exc):
                raise
            log.warning("extração falhou no perfil %s: %s", profile.name, friendly_error(exc))
            continue
        if caçando_audio and info.get("vcodec") not in (None, "none"):
            # guarda como rede de segurança e segue procurando audio-only
            if muxado is None:
                muxado, muxado_index = info, index
            continue
        if index != _active:
            # o % não é decorativo: são 4 threads no pool e a cadeia pode ter
            # encolhido (cookies removidos) desde que _active foi escrito
            log.warning(
                "perfil de extração agora é %s (era %s)",
                profile.name,
                chain[_active % len(chain)].name,
            )
            _active = index
            _active_since = _now()
        if caçando_audio:
            _quality_probe_at = _now()  # achou audio-only: varredura cumprida
        return info

    if muxado is not None:
        # nenhum perfil deu audio-only nesta janela: fica no muxado e só volta
        # a procurar daqui a _PROFILE_RECHECK_S
        _quality_probe_at = _now()
        log.warning(
            "nenhum perfil deu áudio puro; usando %s em formato com vídeo (%s)",
            chain[muxado_index].name,
            muxado.get("format_id"),
        )
        if muxado_index != _active:
            _active = muxado_index
            _active_since = _now()
        return muxado
    raise last if last is not None else RuntimeError("extração falhou sem erro registrado")


async def get_stream(
    video_id_or_url: str, *, use_cache: bool = True, priority: int = PRIO_FOREGROUND
) -> dict:
    """Resolve a URL de stream de áudio + metadados de um vídeo (sem baixar).

    use_cache=False força extração fresca (retentativa pós-URL vencida).
    priority=PRIO_PREFETCH para trabalho adiantado: entra atrás de quem vai
    tocar agora e, se ninguém mais esperar por ele antes de começar, some.
    """
    # URL de vídeo vira o id: é assim que um `/play <link>` de faixa quente
    # acha o cache, e que o adiantamento do cog (pela URL) e o pedido do
    # player (pelo id) caem na MESMA extração em voo em vez de duas
    video_id = (
        extract_video_id(video_id_or_url) if is_url(video_id_or_url) else video_id_or_url
    )
    url = (
        f"https://www.youtube.com/watch?v={video_id}"
        if video_id
        else video_id_or_url  # URL que não é de vídeo: o yt-dlp que decida
    )
    if use_cache and video_id:
        cached = _cache_get(video_id)
        if cached is not None:
            return cached
        falha = _recent_failure(video_id)
        if falha is not None:
            raise falha

    # Com o pool serializado, pedir a MESMA faixa que já está sendo extraída
    # custava o dobro: o segundo pedido entrava na fila atrás do primeiro e
    # refazia os mesmos ~8s. Acontece de verdade no /skip logo após uma faixa
    # começar (o prefetch dela ainda está em voo). Quem chega depois espera o
    # resultado do primeiro em vez de abrir outra extração.
    voo = _inflight.get(url)
    if voo is None:
        voo = _Voo(url)
        voo.job = _YTDLP_POOL.submit(
            functools.partial(_extract, url), priority=priority, label=video_id or url
        )
        voo.task = asyncio.ensure_future(_resolve(voo))
        _inflight[url] = voo
        # a entrada some quando a extração termina, não quando o primeiro a
        # pedir desiste — senão um /skip cancelado deixaria o próximo pedido
        # abrir uma segunda extração. Confere a identidade: um voo novo pode
        # já ter tomado a URL depois de este ter sido cancelado.
        voo.task.add_done_callback(lambda _t, u=url, v=voo: _esquece_voo(u, v))
    elif priority < voo.job.priority:
        # o foreground pediu uma faixa que o prefetch deixou NA FILA: sobe
        _YTDLP_POOL.bump(voo.job, priority)
    voo.waiters += 1
    try:
        # shield: quem desistir não cancela a extração dos outros que esperam
        return await asyncio.shield(voo.task)
    finally:
        voo.waiters -= 1
        if voo.waiters <= 0 and not voo.task.done() and _YTDLP_POOL.cancel(voo.job):
            # ninguém espera mais e nem começou: não gasta o worker com isso
            _esquece_voo(url, voo)
            voo.task.cancel()


def _esquece_voo(url: str, voo: _Voo) -> None:
    if _inflight.get(url) is voo:
        del _inflight[url]


def _consome_excecao(tarefa: asyncio.Task) -> None:
    """Callback para tarefa em background: erro vai para o log em DEBUG e
    pronto — sem "Task exception was never retrieved" no journal."""
    if not tarefa.cancelled() and tarefa.exception() is not None:
        log.debug("extração em background falhou: %r", tarefa.exception())


def prewarm(video_id: str) -> asyncio.Task | None:
    """Aquece o cache de UMA faixa provável — só se o pool estiver ocioso.

    Usado pelo /play <nome>: enquanto a pessoa escolhe no dropdown (2–10s),
    o 1º resultado (o escolhido na maioria das vezes) já é extraído. Com o
    pool serializado, um palpite errado entraria na FRENTE de uma faixa que
    alguém já está esperando — por isso só com nada em voo. Devolve None
    quando não há o que fazer (em voo ou já quente).
    """
    if _YTDLP_POOL.busy() or _cache_get(video_id) is not None:
        return None
    tarefa = asyncio.ensure_future(get_stream(video_id, priority=PRIO_PREFETCH))
    tarefa.add_done_callback(_consome_excecao)
    return tarefa


async def sonda_do_stream(url: str, tentativas: int = 3, timeout_s: float = 10.0) -> list:
    """Abre a URL do stream como o FFmpeg abriria; devolve o status de cada tentativa.

    Extrair não é tocar: em 20/09 a extração passava em toda sonda enquanto
    o googlevideo recusava (403) o primeiro acesso à URL. Pede 1 KB, para no
    primeiro 2xx e espera 1s entre tentativas (é o que o FFmpeg faz com
    -reconnect_on_http_error). Erro de rede entra como o nome da exceção.
    """
    from services import http

    vistos: list = []
    for tentativa in range(tentativas):
        if tentativa:
            await asyncio.sleep(1.0)
        try:
            async with http.get_session().get(
                url,
                headers={"Range": "bytes=0-1023", "User-Agent": "Lavf/60.16.100"},
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                vistos.append(resp.status)
        except Exception as exc:  # noqa: BLE001 — sonda: o erro É o resultado
            vistos.append(type(exc).__name__)
        if stream_abriu(vistos[-1:]):
            break
    return vistos


def serve_para_opus_direto(info: dict) -> bool:
    """O áudio desta extração pode ir para o Discord SEM decodificar/recodificar?

    Só Opus 48 kHz estéreo (o itag 251 de sempre): `-c:a copy` não reamostra nem
    remixa, e o Discord espera exatamente isso. Decide-se pelos metadados da
    extração; chave ausente (cache gravado por versão antiga, outro site) é
    "não sei" — e não sei vai pelo caminho conservador.
    """
    return (
        info.get("acodec") == "opus"
        and info.get("asr") == 48000
        and info.get("canais") == 2
    )


def descreve_audio(info: dict) -> str:
    """`opus/251` para o log de início de faixa (`?` = extração antiga, sem o dado)."""
    return f"{info.get('acodec') or '?'}/{info.get('itag') or '?'}"


def stream_abriu(vistos: list) -> bool:
    return any(isinstance(v, int) and 200 <= v < 300 for v in vistos)


# Medido na VM (20/09/2026): a URL passa a abrir 0,7–1,3s depois de extraída;
# esperando 1,5s ou mais, o primeiro acesso já dá 206. Até 3s ela é "fresca".
_URL_FRESCA_S = 3.0
_SONDA_A_CADA_S = 0.25
_ESPERA_MAXIMA_S = 4.0  # passou disto não é propagação: o FFmpeg tenta e o fim de faixa acusa


def _e_recusa(visto) -> bool:
    return visto == 403 or (isinstance(visto, int) and 500 <= visto < 600)


async def espera_stream_abrir(info: dict) -> tuple[float, int]:
    """Espera a URL recém-extraída começar a valer. Devolve (segundos, recusas).

    O googlevideo responde 403 a uma URL nos primeiros ~1s de vida dela. O
    FFmpeg re-tenta sozinho (-reconnect_on_http_error), mas com backoff de
    0s → 1s → 3s: quando erra a janela de 1s a faixa só começa aos ~4,4s.
    Sondar a cada 0,25s entrega ao FFmpeg uma URL que já abre. Só vale para
    URL fresca — a do prefetch envelheceu esperando a vez e não paga nada.
    """
    idade = _now() - info.get("extraida_em", float("-inf"))
    if not 0.0 <= idade < _URL_FRESCA_S:
        return 0.0, 0
    inicio = _now()
    recusas = 0
    while True:
        visto = (await sonda_do_stream(info["stream_url"], tentativas=1, timeout_s=3.0))[-1]
        if not _e_recusa(visto):
            break  # abriu — ou é erro que esperar não conserta
        recusas += 1
        if _now() - inicio >= _ESPERA_MAXIMA_S:
            break
        await asyncio.sleep(_SONDA_A_CADA_S)
    return _now() - inicio, recusas


async def _resolve(voo: _Voo) -> dict:
    """Espera o job do pool e normaliza para o formato que o player consome."""
    try:
        info = await voo.job.future
    except Exception as exc:
        video_id = extract_video_id(voo.url)
        if video_id:
            _lembra_falha(video_id, exc)
        raise
    result = {
        "video_id": info.get("id", ""),
        "title": info.get("title", "?"),
        "artist": info.get("uploader", "") or info.get("channel", ""),
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail", ""),
        "stream_url": info["url"],
        # quando a URL nasceu: nos primeiros ~1s o googlevideo a recusa (403)
        "extraida_em": _now(),
        # o que é o áudio: decide se dá para repassar o Opus sem recodificar
        "itag": str(info.get("format_id") or ""),
        "acodec": info.get("acodec") or "",
        "asr": int(info.get("asr") or 0),
        "canais": int(info.get("audio_channels") or 0),
    }
    if result["video_id"]:
        _cache_put(result["video_id"], result)
        _failures.pop(result["video_id"], None)
    return result
