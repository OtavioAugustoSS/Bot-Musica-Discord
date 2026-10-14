"""O que o journal não contava: segredos mascarados, erros fora do logging,
ruído resumido, travadas do event loop e um retrato periódico da máquina.

Saiu da leitura do journal inteiro em 20/09/2026 (33 mil linhas, 50 dias):

  - a chave da Groq apareceu 25 vezes em texto puro (o repr do
    ClientResponseError do aiohttp traz os headers da request). O código que
    fazia isso já foi corrigido, mas a garantia não pode depender de ninguém
    nunca mais escrever um `%r` no lugar errado: o formatador mascara.
  - tracebacks de THREAD ("Exception in thread voice-receiver…", 37x) e de
    evento do py-cord caíam crus no stderr: sem horário do bot, sem nível e
    FORA do espelho de erros que vai ao dono quando o processo morre.
  - `CryptoError: Decryption failed` do receptor de voz: 256 registros de
    ERROR com ~15 linhas de traceback cada — uns 4 mil linhas enterrando o
    que importa. É rotina da transição de epoch do DAVE.
  - "Can't keep up, websocket is 12s behind" e "Shard has stopped responding"
    apareceram sem nada em volta que dissesse se o event loop travou ou se a
    VM ficou sem CPU (steal de 50–75% medido em 17/09).
  - RAM: 473 MB em uso + 257 MB de swap numa VM de 954 MB, vistos por acaso
    num `systemctl status`. Nenhuma linha do journal acompanha isso.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import sys
import threading
import time
import warnings

import config

log = logging.getLogger(__name__)

FORMATO = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# ── segredos ─────────────────────────────────────────────────────────────────

_PADROES_DE_SEGREDO = (
    (re.compile(r"gsk_[A-Za-z0-9]{8,}"), "gsk_***"),  # chave da Groq
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    # token de bot do Discord: id.base64.hmac
    (re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"), "***token-discord***"),
    # "Identifying ourselves: {... 'token': 'xxxxxxxx…', 'session_id': …}" — o
    # py-cord loga em INFO o token e a sessão de cada conexão de voz
    (re.compile(r"""(['"](?:token|session_id)['"]\s*:\s*['"])[^'"]{6,}(['"])"""), r"\1***\2"),
)


def _segredos_do_env() -> list[str]:
    valores = (
        config.DISCORD_TOKEN,
        config.GROQ_API_KEY,
        config.SPOTIFY_CLIENT_SECRET,
    )
    return [v for v in valores if len(v) >= 8]


def mascara(texto: str) -> str:
    """Tira do texto qualquer credencial conhecida — pelo valor e pelo formato."""
    for valor in _segredos_do_env():
        if valor in texto:
            texto = texto.replace(valor, "***")
    for padrao, troca in _PADROES_DE_SEGREDO:
        texto = padrao.sub(troca, texto)
    return texto


class Redator(logging.Formatter):
    """Formatter que mascara segredos na linha PRONTA (mensagem + traceback)."""

    def format(self, record: logging.LogRecord) -> str:
        return mascara(super().format(record))


# ── ruído conhecido ──────────────────────────────────────────────────────────


class FiltroDeRotina(logging.Filter):
    """Cala mensagens de biblioteca que são rotina mas saem como WARNING.

    WARNING entra no espelho de erros que vai ao dono quando o processo morre
    (services/alerts): rotina ali dentro empurra para fora o que explicaria a
    morte. `Socket reader … is waiting to be set as running` sai 2x por
    conexão de voz (105x no journal) — é só o leitor de voz esperando a vez.
    """

    def __init__(self, marcas: tuple[str, ...]) -> None:
        super().__init__()
        self._marcas = marcas

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            mensagem = record.getMessage()
        except Exception:  # noqa: BLE001 — filtro de log nunca pode levantar
            return True
        return not any(marca in mensagem for marca in self._marcas)


class FiltroDeRajada(logging.Filter):
    """Deixa passar UM registro por janela e conta os outros.

    Para ruído que é rotina mas não pode sumir de vez: o primeiro da janela
    sai (sem traceback), os seguintes viram "(+N iguais)" no próximo que sair.
    """

    def __init__(self, marcas: tuple[str, ...], janela_s: float = 60.0) -> None:
        super().__init__()
        self._marcas = marcas
        self._janela_s = janela_s
        self._ultimo = float("-inf")
        self._calados = 0
        self._relogio = time.monotonic

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            mensagem = record.getMessage()
        except Exception:  # noqa: BLE001 — filtro de log nunca pode levantar
            return True
        if not any(marca in mensagem for marca in self._marcas):
            return True
        agora = self._relogio()
        if agora - self._ultimo < self._janela_s:
            self._calados += 1
            return False
        record.msg = mensagem + (
            f" (+{self._calados} iguais caladas desde o último aviso)" if self._calados else ""
        )
        record.args = ()
        record.exc_info = None  # ~15 linhas de traceback por pacote de voz
        record.exc_text = None
        self._ultimo = agora
        self._calados = 0
        return True


# ── erros que não passavam pelo logging ──────────────────────────────────────


def _excecao_nao_tratada(tipo, valor, tb) -> None:
    if issubclass(tipo, KeyboardInterrupt):
        sys.__excepthook__(tipo, valor, tb)
        return
    log.critical("exceção não tratada — o processo vai cair", exc_info=(tipo, valor, tb))


def _excecao_em_thread(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    log.error(
        "exceção não tratada na thread %s",
        args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def configura_logging() -> None:
    """Logging do bot: formato único, segredos mascarados, nada fora dele."""
    raiz = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in raiz.handlers):
        raiz.addHandler(logging.StreamHandler())
    raiz.setLevel(logging.WARNING)
    for handler in raiz.handlers:
        handler.setFormatter(Redator(FORMATO))
    # módulos do bot em INFO (eventos operacionais); libs continuam em WARNING
    for nome in ("main", "core", "services", "cogs", "db"):
        logging.getLogger(nome).setLevel(logging.INFO)
    # INFO nos módulos de voz: mostra close codes e o porquê de desconexões
    logging.getLogger("discord.voice").setLevel(logging.INFO)
    logging.getLogger("discord.voice.state").addFilter(
        FiltroDeRotina(("is waiting to be set as running",))
    )
    # o bot não pede o intent de membros: quem já estava na call quando ele entra
    # não está no cache e o py-cord avisa disso em WARNING a cada conexão — e
    # WARNING vai para o espelho de erros do dono
    logging.getLogger("discord.voice.client").addFilter(
        FiltroDeRotina(("Skipping member referencing ID",))
    )
    # spam de SenderReport a cada segundo durante a gravação
    leitor = logging.getLogger("discord.voice.receive.reader")
    leitor.setLevel(logging.WARNING)
    leitor.addFilter(FiltroDeRajada(("Decryption failed", "CryptoError")))
    # o spotipy loga o erro HTTP como ERROR e depois levanta: o WARNING de
    # services/spotify_api já conta a história (e o fallback do embed resolve)
    logging.getLogger("spotipy.client").setLevel(logging.CRITICAL)

    sys.excepthook = _excecao_nao_tratada
    threading.excepthook = _excecao_em_thread
    # warnings.warn também caía cru no stderr; o do DAVE (118x no journal) é
    # justamente o que core/pycord_voice_patch.py conserta — puro ruído
    logging.captureWarnings(True)
    warnings.filterwarnings("ignore", message="Voice reception is currently broken")


# ── travadas do event loop ───────────────────────────────────────────────────

_TIQUE_S = 0.5
_LAG_AVISO_S = 0.5  # o heartbeat de voz e o envio de comandos sentem a partir daqui
_AVISO_NO_MAXIMO_A_CADA_S = 30.0


class VigiaDoLoop:
    """Mede quanto o event loop atrasa para acordar — travada ou falta de CPU.

    Um `sleep(0.5)` que volta em 3s significa 2,5s em que NADA assíncrono
    rodou: heartbeat do gateway, respostas de interação (janela de 3s),
    colheita da escuta. O steal da VM dá o contexto: lag com steal alto é a
    máquina; lag com steal baixo é código bloqueando o loop.
    """

    def __init__(self) -> None:
        self.maior = 0.0  # desde a última leitura (retrato de saúde)
        self._pior_da_janela = 0.0
        self._travadas = 0
        self._ultimo_aviso = float("-inf")

    def leu_o_maior(self) -> float:
        maior, self.maior = self.maior, 0.0
        return maior

    def registra(self, lag: float, agora: float) -> str | None:
        """Contabiliza um tique; devolve o texto do aviso quando é hora de avisar."""
        self.maior = max(self.maior, lag)
        if lag < _LAG_AVISO_S:
            return None
        self._travadas += 1
        self._pior_da_janela = max(self._pior_da_janela, lag)
        if agora - self._ultimo_aviso < _AVISO_NO_MAXIMO_A_CADA_S:
            return None
        texto = (
            f"event loop travou {self._pior_da_janela:.1f}s"
            f" ({self._travadas} travada(s) desde o último aviso)"
        )
        self._ultimo_aviso = agora
        self._pior_da_janela = 0.0
        self._travadas = 0
        return texto

    async def roda(self) -> None:
        # READY no primeiro giro do loop, e não no on_ready: com o Discord fora
        # do ar o on_ready não vem, o systemd (Type=notify) daria a partida
        # por falha e reiniciaria o bot em círculos por algo que não é dele
        avisa_systemd("READY=1")
        ultimo_batimento = float("-inf")
        faltas_na_travada = 0
        while True:
            antes = time.monotonic()
            faltas_antes = _faltas_de_pagina_do_disco()
            await asyncio.sleep(_TIQUE_S)
            agora = time.monotonic()
            if agora - ultimo_batimento >= _BATIMENTO_S:
                # o batimento sai DAQUI (task do event loop) de propósito: loop
                # travado = sem batimento = o systemd mata e sobe de novo. É a
                # falha de 19/09 ("Shard has stopped responding"), que um
                # batimento vindo de thread nunca acusaria.
                avisa_systemd("WATCHDOG=1")
                ultimo_batimento = agora
            lag = agora - antes - _TIQUE_S
            if lag >= _LAG_AVISO_S and faltas_antes is not None:
                faltas_na_travada += (_faltas_de_pagina_do_disco() or faltas_antes) - faltas_antes
            aviso = self.registra(lag, agora)
            if aviso:
                log.warning("%s — %s%s", aviso, _cpu_agora(), _memoria_da_travada(faltas_na_travada))
                faltas_na_travada = 0


# ── systemd (sd_notify sem dependência) ──────────────────────────────────────

_BATIMENTO_S = 20.0  # um terço do WatchdogSec=60 da unit: dois podem falhar


def _abre_soquete() -> socket.socket:
    """Datagrama unix. Função à parte para os testes trocarem só ISTO — trocar
    `socket.socket` quebra o próprio asyncio, que abre um socketpair no Windows.
    Sem AF_UNIX (Windows) levanta OSError, que o chamador trata."""
    familia = getattr(socket, "AF_UNIX", None)
    if familia is None:
        raise OSError("plataforma sem socket unix")
    return socket.socket(familia, socket.SOCK_DGRAM)


def avisa_systemd(estado: str) -> bool:
    """Manda `estado` ("READY=1", "WATCHDOG=1") ao systemd. False fora dele.

    É o protocolo do sd_notify: um datagrama no socket unix que o systemd
    deixa em $NOTIFY_SOCKET ("@nome" = socket abstrato). Fora do systemd
    (dev no Windows, testes) a variável não existe e nada acontece.
    """
    destino = os.environ.get("NOTIFY_SOCKET", "")
    if not destino:
        return False
    if destino.startswith("@"):
        destino = "\0" + destino[1:]
    try:
        with _abre_soquete() as sock:
            sock.sendto(estado.encode("ascii"), destino)
    except OSError as exc:
        log.debug("não consegui avisar o systemd (%s): %s", estado, exc)
        return False
    return True


# ── retrato da máquina ───────────────────────────────────────────────────────


def _le_proc_status() -> dict[str, int]:
    """VmRSS/VmSwap/Threads do processo em kB (Linux). Vazio fora dele."""
    campos: dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for linha in fh:
                nome, _, resto = linha.partition(":")
                if nome in ("VmRSS", "VmSwap", "Threads"):
                    campos[nome] = int(resto.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return campos


def _faltas_de_pagina_do_disco() -> int | None:
    """Faltas de página MAIORES do processo (Linux): página que estava no swap
    ou no disco e teve de ser lida — o processo inteiro para esperando."""
    try:
        with open("/proc/self/stat", encoding="ascii", errors="replace") as fh:
            depois_do_nome = fh.read().rpartition(")")[2].split()
        return int(depois_do_nome[9])  # campo 12 (majflt); o 3º é o 1º daqui
    except (OSError, ValueError, IndexError):
        return None


def _memoria_da_travada(faltas: int) -> str:
    """Travada com steal e CPU em zero no começo de toda sessão (01–03/10): se
    for o swap devolvendo a memória de horas ocioso, aparece aqui."""
    status = _le_proc_status()
    if not status:
        return ""
    return f", {faltas} falta(s) de página do disco na travada, swap {status.get('VmSwap', 0) // 1024}MB"


def _le_cpu_total() -> tuple[int, int] | None:
    """(jiffies totais, jiffies de steal) da máquina (Linux)."""
    try:
        with open("/proc/stat", encoding="ascii", errors="replace") as fh:
            partes = fh.readline().split()
        valores = [int(v) for v in partes[1:9]]
    except (OSError, ValueError):
        return None
    if len(valores) < 8:
        return None
    return sum(valores), valores[7]


def _cpu_de_stat(texto: str) -> tuple[str, int] | None:
    """(comm, utime+stime em jiffies) a partir do conteúdo de /proc/<pid>/stat.

    O comm vem entre parênteses e pode ter espaço e parêntese dentro: os
    campos numéricos contam a partir do ÚLTIMO ')'.
    """
    abre, fecha = texto.find("("), texto.rfind(")")
    if abre < 0 or fecha < abre:
        return None
    resto = texto[fecha + 2 :].split()
    try:
        return texto[abre + 1 : fecha], int(resto[11]) + int(resto[12])
    except (IndexError, ValueError):
        return None


def _filhos_vivos() -> list[tuple[str, int]]:
    """(comm, jiffies de CPU) de cada processo filho vivo — FFmpeg e deno.

    Os filhos ficam pendurados na THREAD que os criou (o FFmpeg nasce num
    asyncio.to_thread), por isso a varredura é por task.
    """
    filhos: list[tuple[str, int]] = []
    try:
        tarefas = os.listdir("/proc/self/task")
    except OSError:
        return filhos
    for tid in tarefas:
        try:
            with open(f"/proc/self/task/{tid}/children", encoding="ascii") as fh:
                pids = fh.read().split()
        except OSError:
            continue
        for pid in pids:
            try:
                with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
                    lido = _cpu_de_stat(fh.read())
            except OSError:
                continue  # o filho saiu entre a lista e a leitura
            if lido is not None:
                filhos.append(lido)
    return filhos


def _cpu_dos_filhos_s(vivos: list[tuple[str, int]]) -> float:
    """CPU (s) gasta por filhos: os já colhidos (os.times) + os ainda vivos.

    Quando um filho morre e é colhido, o tempo dele sai da parcela dos vivos
    e entra na de os.times — a soma não dá salto.
    """
    tempos = os.times()
    colhidos = tempos.children_user + tempos.children_system
    try:
        tique = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return colhidos
    return colhidos + sum(jiffies for _, jiffies in vivos) / tique


_cpu_anterior: tuple[int, int] | None = None
_processo_anterior: tuple[float, float] | None = None
_filhos_anterior: float | None = None


def _cpu_agora() -> str:
    """Steal da VM, CPU do processo e CPU dos filhos desde a última chamada."""
    global _cpu_anterior, _processo_anterior, _filhos_anterior
    partes = []
    atual = _le_cpu_total()
    if atual is not None and _cpu_anterior is not None and atual[0] > _cpu_anterior[0]:
        steal = 100.0 * (atual[1] - _cpu_anterior[1]) / (atual[0] - _cpu_anterior[0])
        partes.append(f"steal {steal:.0f}%")
    _cpu_anterior = atual
    relogio, gasto = time.monotonic(), time.process_time()
    vivos = _filhos_vivos()
    dos_filhos = _cpu_dos_filhos_s(vivos)
    if _processo_anterior is not None and relogio > _processo_anterior[0]:
        janela = relogio - _processo_anterior[0]
        uso = 100.0 * (gasto - _processo_anterior[1]) / janela
        partes.append(f"cpu do bot {uso:.0f}%")
        if _filhos_anterior is not None:
            # process_time() NÃO conta filhos: sem esta parcela, mover o
            # encode do Python para dentro do FFmpeg pareceria ganho de CPU
            partes.append(
                f"filhos (ffmpeg/deno) {max(0.0, 100.0 * (dos_filhos - _filhos_anterior) / janela):.0f}%"
            )
    _processo_anterior = (relogio, gasto)
    _filhos_anterior = dos_filhos
    if sys.platform.startswith("linux"):
        # sempre, inclusive 0: FFmpeg que sobra vivo depois da faixa é vazamento
        partes.append(f"ffmpeg vivos {sum(1 for comm, _ in vivos if comm.startswith('ffmpeg'))}")
    try:
        partes.append(f"load {os.getloadavg()[0]:.2f}")
    except (AttributeError, OSError):
        pass
    return ", ".join(partes) or "sem medida de CPU nesta plataforma"


def _descritores_abertos() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def retrato(bot, vigia: VigiaDoLoop | None = None) -> str:
    """Uma linha com o estado da máquina e do bot — o que o `systemctl status`
    mostrava por acaso, agora no journal com horário."""
    from core.player import all_players
    from services import speech, youtube
    from core import listen, pycord_voice_patch

    partes = []
    status = _le_proc_status()
    if status:
        partes.append(f"ram {status.get('VmRSS', 0) // 1024}MB")
        partes.append(f"swap {status.get('VmSwap', 0) // 1024}MB")
    partes.append(f"threads {threading.active_count()}")
    descritores = _descritores_abertos()
    if descritores is not None:
        partes.append(f"fds {descritores}")
    partes.append(_cpu_agora())
    if vigia is not None:
        partes.append(f"lag máx do loop {vigia.leu_o_maior():.2f}s")
    players = all_players()
    conectados = [p for p in players if p.voice and p.voice.is_connected()]
    # sem as palavras "tocando" e "na fila": é por elas que o deploy confere,
    # no journal, se alguém está usando o bot antes de reiniciar
    partes.append(
        f"calls {len(conectados)} (com música {sum(1 for p in conectados if p.is_playing)},"
        f" com escuta {sum(1 for p in conectados if p.listener is not None)})"
    )
    partes.append(f"cache de streams {len(youtube._stream_cache)}")
    partes.append(f"pool {youtube._YTDLP_POOL.pending()} pendente(s)")
    partes.append(f"perfil {youtube.active_profile()}")
    stt = speech.leu_estatisticas()
    if any(stt.values()):
        partes.append("groq " + " ".join(f"{k}={v}" for k, v in stt.items() if v))
    cortes = listen.leu_estatisticas()
    if any(cortes.values()):
        partes.append("escuta " + " ".join(f"{k}={v}" for k, v in cortes.items() if v))
    realinhos, maior = pycord_voice_patch.leu_atrasos_do_audio()
    if realinhos:
        partes.append(f"áudio realinhado {realinhos}x (maior atraso {maior * 1000:.0f}ms)")
    latencia = getattr(bot, "latency", None)
    if isinstance(latencia, float) and latencia == latencia:  # NaN antes do 1º heartbeat
        partes.append(f"gateway {latencia * 1000:.0f}ms")
    return " | ".join(partes)


_RETRATO_EM_USO_S = 5 * 60
_RETRATO_OCIOSO_S = 30 * 60


async def _retratos(bot, vigia: VigiaDoLoop) -> None:
    from core.player import all_players

    ultimo = float("-inf")
    while True:
        await asyncio.sleep(60)
        em_uso = any(p.voice and p.voice.is_connected() for p in all_players())
        intervalo = _RETRATO_EM_USO_S if em_uso else _RETRATO_OCIOSO_S
        if time.monotonic() - ultimo < intervalo:
            continue
        ultimo = time.monotonic()
        try:
            log.info("saúde: %s", retrato(bot, vigia))
        except Exception:  # noqa: BLE001 — diagnóstico nunca derruba o bot
            log.exception("não consegui tirar o retrato de saúde")


def inicia(bot, loop: asyncio.AbstractEventLoop | None = None) -> list[asyncio.Task]:
    """Liga o vigia do loop (e o batimento do watchdog) e os retratos. Uma vez.

    Com `loop` (o `bot.loop`, ANTES do `bot.run()`), as tasks nascem junto com
    o event loop — o batimento não depende de o Discord estar no ar.
    """
    loop = loop or asyncio.get_running_loop()
    vigia = VigiaDoLoop()
    _cpu_agora()  # primeira leitura: a próxima já tem com o que comparar
    return [
        loop.create_task(vigia.roda(), name="vigia-do-loop"),
        loop.create_task(_retratos(bot, vigia), name="retratos-de-saude"),
    ]
