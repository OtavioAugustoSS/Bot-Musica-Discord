"""Escuta da call (modo "Arrodes"): captura fala, transcreve e executa.

Fluxo: py-cord entrega pacotes PCM por usuário na thread de áudio →
CommandSink acumula por usuário → um ticker no event loop fecha o trecho
quando o usuário para de falar: o Discord PARA de mandar pacotes no
silêncio, mas quem está de microfone aberto manda ruído baixo sem parar —
então o corte também vale por VOICE_GAP_S sem energia de fala (04/10/2026:
35% dos trechos de um usuário viravam 12s e morriam no teto sem ir à Groq)
→ Whisper transcreve → intents interpreta → GuildPlayer executa.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import wave
from typing import TYPE_CHECKING

import numpy as np
from discord.sinks import Sink

import config
import messages as m
from core import audio
from core.intents import (
    WAKE_WORD,
    acha_na_fila,
    agora_e_do_nome,
    chamou_de_frente,
    first_word,
    inicio_para_log,
    is_stt_noise,
    mesmo_comando,
    nome_no_audio,
    parse_command,
    parse_volume,
    sem_o_agora,
    tem_pedido_de_tocar,
)
from core.panel import format_time
from core.queue import LoopMode, Track
from services import llm, speech, youtube

if TYPE_CHECKING:
    from core.player import GuildPlayer

log = logging.getLogger(__name__)

BYTES_PER_SECOND = 192_000  # 48kHz * 2 canais * 2 bytes
MIN_SPEECH_S = 0.4
STALE_SEGMENT_S = 12.0  # trecho mais velho que isso é descartado (anti-lag)
MAX_SEGMENT_S = 12.0
_RMS_WIN_S = 0.02  # análise de energia em janelas de 20ms
_MIN_SPEECH_WINDOWS = 3  # fala de verdade tem ≥3 janelas acima do limiar
# colheita dos trechos terminados: 0,25s somava até 0,25s (média 0,125s) a
# TODO comando de voz; a varredura é barata (só compara timestamps)
_TICK_S = 0.1
# "Arrodes" [pausa] "toca X": com o corte por silêncio curto a frase pode
# virar dois trechos. Quem chamou o bot sem dizer o comando tem esta janela
# para completar — o trecho seguinte é lido como o comando. O journal já
# mostrava o caso mesmo com 1,2s de corte: "comando=hello" e, 8s depois,
# "comando=-" (o resto, sem wake word, ignorado).
_CONTINUACAO_S = 6.0
# orçamento dos títulos da fila no prompt do Whisper (ver _contexto_da_fila): o
# prompt fixo tem ~300 caracteres e, com mais ~240 de títulos, passou na Groq
_PESO_DO_CONTEXTO = 240
# descarte de trecho curto (respiro, clique) não vai para o journal; a
# partir daqui era fala de verdade e a pessoa merece saber por que sumiu
_LOG_DISCARD_MIN_S = 1.0


class _Continuacao:
    """Janela por pessoa depois de uma chamada sem comando."""

    def __init__(self) -> None:
        self._ate: dict[int, tuple[float, str]] = {}

    def abre(self, user_id: int, agora: float, pedido: str = "") -> None:
        """`pedido`: a chamada como veio. "Arrodes, toca como próxima música" sem o
        nome (06/10) — o trecho seguinte é o nome, e o "como próxima" vale para ele."""
        self._ate[user_id] = (agora + _CONTINUACAO_S, pedido or WAKE_WORD)

    def completa(self, user_id: int, text: str, agora: float) -> str:
        """O trecho novo é o comando de quem acabou de chamar? Uma vez só."""
        if is_stt_noise(text):
            # 17/09: "Arrodes." e, 4s depois, ruído transcrito como "Tchau." —
            # o bot saiu da call. Ruído não é o comando e não gasta a janela.
            return text
        ate, pedido = self._ate.pop(user_id, (0.0, ""))
        if ate > agora:
            return f"{pedido} {text}"
        return text


def _play_sem_pedido(heard: str, action: str, arg: str) -> bool:
    """O LLM leu uma música NOVA numa frase sem verbo de pedir? ("#3" é da fila: vale)"""
    if action not in ("play", "play_next") or arg.startswith("#"):
        return False
    return not tem_pedido_de_tocar(heard)


def _resto_seria_comando(text: str) -> str:
    """", o resto leria skip" quando, sem a 1ª palavra, a frase é um comando claro.

    É a chamada calada: o nome saiu irreconhecível e nada acontece. Só a ação vai
    para o journal (o conteúdo, não); hello/unknown são conversa demais para contar.
    """
    palavras = text.split(maxsplit=1)
    if len(palavras) < 2:
        return ""
    lido = parse_command(f"{WAKE_WORD}, {palavras[1]}")
    if lido is None or lido[0] in ("hello", "unknown"):
        return ""
    return f", o resto leria {lido[0]}"


_DEBUG_DIR = os.path.join("data", "voice-debug")


def _debug(msg: str) -> None:
    """Diagnóstico opt-in (VOICE_DEBUG=1): print bloqueia o event loop."""
    if config.VOICE_DEBUG:
        print(msg)


def _save_debug_wav(user_id: int, pcm: bytes) -> str:
    """Guarda o trecho como .wav (mantém os 10 últimos) para diagnóstico."""
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        existing = sorted(f for f in os.listdir(_DEBUG_DIR) if f.endswith(".wav"))
        for old in existing[:-9]:
            os.remove(os.path.join(_DEBUG_DIR, old))
        path = os.path.join(_DEBUG_DIR, f"seg-{int(time.time() * 1000)}-{user_id}.wav")
        with wave.open(path, "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(48000)
            out.writeframes(pcm)
        return path
    except Exception:  # noqa: BLE001 — diagnóstico nunca derruba a escuta
        return ""


_DIR_AMOSTRAS = os.path.join("data", "wake-amostras")
_MAX_AMOSTRAS = 300


def guarda_amostra(user_id: int, pcm: bytes) -> str:
    """Guarda um trecho em que a wake word foi CONFIRMADA — WAV 16 kHz mono.

    É o conjunto de teste de qualquer wake word local (o caminho para parar de
    mandar 98% da conversa à Groq): sem gente de verdade dizendo "Arrodes" não
    dá para medir o recall de um modelo antes de confiar nele. Só com
    VOICE_COLETA=1, só fala dirigida ao bot, e para de guardar no teto. Devolve
    o caminho, ou "" se não guardou. Roda em thread (disco + numpy).
    """
    try:
        os.makedirs(_DIR_AMOSTRAS, exist_ok=True)
        if sum(1 for f in os.listdir(_DIR_AMOSTRAS) if f.endswith(".wav")) >= _MAX_AMOSTRAS:
            return ""
        mono = speech.pcm_to_float_mono_16k(pcm)
        caminho = os.path.join(_DIR_AMOSTRAS, f"wake-{int(time.time() * 1000)}-{user_id}.wav")
        with open(caminho, "wb") as fh:
            fh.write(speech._to_wav_bytes(mono))
        return caminho
    except Exception:  # noqa: BLE001 — coleta nunca derruba a escuta
        log.debug("não consegui guardar a amostra da wake word", exc_info=True)
        return ""


def analyze_speech(pcm: bytes, threshold: float) -> tuple[int, bytes]:
    """(janelas de 20ms com energia ≥ limiar, PCM aparado nas bordas mudas).

    RMS por janela em vez de RMS global: uma fala curta ("pula") dentro de
    um trecho comprido não é diluída pela média. O trim corta o silêncio
    das pontas — payload menor para o STT. Pura (numpy) para testes.
    """
    arr = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16).astype(
        np.float32
    )
    win = int(BYTES_PER_SECOND / 2 * _RMS_WIN_S)  # amostras int16 por janela
    usable = len(arr) - (len(arr) % win)
    if usable <= 0:
        return 0, b""
    windows = arr[:usable].reshape(-1, win)
    rms = np.sqrt(np.mean(windows * windows, axis=1))
    hot = np.flatnonzero(rms >= threshold)
    if len(hot) == 0:
        return 0, b""
    first = max(int(hot[0]) - 1, 0)  # 1 janela de margem de cada lado
    last = min(int(hot[-1]) + 2, len(windows))
    end = last * win * 2 if last < len(windows) else len(pcm)
    return len(hot), pcm[first * win * 2 : end]


def peak_rms(pcm: bytes) -> float:
    """Maior RMS de janela de 20ms do trecho — para o log de descarte dizer
    quão longe do limiar a fala ficou (calibração de VOICE_MIN_RMS por mic)."""
    arr = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16).astype(
        np.float32
    )
    win = int(BYTES_PER_SECOND / 2 * _RMS_WIN_S)
    usable = len(arr) - (len(arr) % win)
    if usable <= 0:
        return 0.0
    windows = arr[:usable].reshape(-1, win)
    return float(np.sqrt(np.mean(windows * windows, axis=1)).max())


def rms_do_pacote(pcm: bytes) -> float:
    """RMS de um pacote (20ms) — a mesma régua de analyze_speech, pacote a pacote."""
    arr = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16)
    if arr.size == 0:
        return 0.0
    arr = arr.astype(np.float32)
    return float(np.sqrt(np.mean(arr * arr)))


# pausa entre frases dentro de um fôlego só: menor que VOICE_GAP_S (0,8s),
# maior que um respiro (0,2s)
_PAUSA_INTERNA_S = 0.3


def corta_na_pausa(pcm: bytes, threshold: float, teto_s: float, pausa_s: float = _PAUSA_INTERNA_S) -> bytes:
    """Trecho acima do teto: fica só até a última pausa interna que cabe no teto.

    A wake word abre a frase (chamou_de_frente), então a 1ª frase é a que pode
    ser comando; o resto do fôlego é conversa. Sem pausa que caiba, volta
    inteiro (e o teto descarta, como antes). Pura (numpy) para testes.
    """
    win = int(BYTES_PER_SECOND / 2 * _RMS_WIN_S)
    arr = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16).astype(np.float32)
    usable = len(arr) - (len(arr) % win)
    total = usable // win
    if total * _RMS_WIN_S <= teto_s:
        return pcm
    windows = arr[:usable].reshape(-1, win)
    quiet = np.sqrt(np.mean(windows * windows, axis=1)) < threshold
    minimo = max(1, int(round(pausa_s / _RMS_WIN_S)))
    teto_janelas = int(teto_s / _RMS_WIN_S)
    corte = None
    inicio = None
    for i, q in enumerate(quiet):
        if q and inicio is None:
            inicio = i
        elif not q and inicio is not None:
            if i - inicio >= minimo and inicio <= teto_janelas:
                corte = inicio
            inicio = None
    if inicio is not None and total - inicio >= minimo and inicio <= teto_janelas:
        corte = inicio
    if not corte or int(np.count_nonzero(~quiet[:corte])) < _MIN_SPEECH_WINDOWS:
        return pcm
    return pcm[: corte * win * 2]


# O corte por energia (04/10) é para o microfone aberto, cujo trecho não para de
# crescer. Valendo para tudo, em 06/10 fechou quase todo trecho (52 cortes em
# 5min, ~40 transcrições): o fim da frase dito mais baixo virava outro trecho,
# sem o nome ou descartado como ruído — e os descartes calados subiram de 8,3
# para 14,3 a cada 100 transcrições. Abaixo disto, fecha quando o Discord para.
_CORTE_POR_ENERGIA_MIN_S = 2.5

# contadores para a linha `saúde:` (services/observabilidade): zeram na leitura.
# É como se mede no journal se os cortes de 04/10 estão salvando trechos.
_estatisticas = {"cortes_por_energia": 0, "cortes_na_pausa": 0}


def leu_estatisticas() -> dict[str, int]:
    """Cortes da escuta desde a última leitura (e zera)."""
    lido = dict(_estatisticas)
    for chave in _estatisticas:
        _estatisticas[chave] = 0
    return lido


class _Buffer:
    __slots__ = ("data", "last", "last_loud", "loud")

    def __init__(self) -> None:
        self.data = bytearray()
        self.last = time.monotonic()
        self.last_loud = 0.0  # último pacote com energia de fala
        self.loud = 0  # quantos pacotes acima do limiar


class CommandSink(Sink):
    """Acumula PCM por usuário; a colheita acontece no event loop.

    Os atributos ``__sink_listeners__``/``walk_children``/``is_opus`` são
    shims para a API nova de recepção do py-cord 2.8, que a base antiga não
    tem (ver core/pycord_voice_patch.py).
    """

    __sink_listeners__: list[tuple[str, str]] = []

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._buffers: dict[int, _Buffer] = {}
        self.diag_writes = 0
        self.diag_bytes = 0

    def walk_children(self):
        return iter(())

    def is_opus(self) -> bool:
        return False  # queremos PCM já decodificado

    def write(self, data, user) -> None:  # thread de áudio do py-cord
        # data: discord.voice.packets.VoiceData; user: Member/Object/None
        pcm = getattr(data, "pcm", None) or b""
        if not pcm:
            return
        user_id = getattr(user, "id", 0) or 0
        if user_id == 0:
            # SSRC ainda sem Member mapeado: bucket próprio por fonte (id
            # negativo não colide com IDs reais) — antes tudo caía no bucket
            # 0 e vozes de pessoas diferentes iam misturadas para o STT
            ssrc = getattr(getattr(data, "packet", None), "ssrc", 0)
            user_id = -ssrc if ssrc else 0
        # energia do pacote FORA do lock (numpy sobre 1920 amostras, ~15µs): é o
        # que fecha o trecho quando a pessoa cala com o microfone aberto (harvest)
        alto = rms_do_pacote(pcm) >= config.VOICE_MIN_RMS
        with self._lock:
            self.diag_writes += 1
            self.diag_bytes += len(pcm)
            buffer = self._buffers.setdefault(user_id, _Buffer())
            buffer.data += pcm
            buffer.last = time.monotonic()
            if alto:
                buffer.last_loud = buffer.last
                buffer.loud += 1

    def harvest(self) -> list[tuple[int, bytes, float]]:
        """Trechos de fala terminados (usuário calou ou estourou o máximo)."""
        now = time.monotonic()
        ended: list[tuple[int, bytes]] = []
        # dentro do lock SÓ o corte dos buffers: a thread de áudio do
        # py-cord espera por ele a cada pacote (e segura o RLock do router
        # enquanto isso) — numpy/print aqui dentro atrasava todo mundo
        with self._lock:
            for user_id, buffer in list(self._buffers.items()):
                duration = len(buffer.data) / BYTES_PER_SECOND
                # calou: falou e já faz VOICE_GAP_S sem energia de fala, mesmo que
                # o microfone aberto continue mandando ruído baixo — só em trecho
                # comprido: no curto, o fim dito baixo virava outro trecho (06/10)
                calou = (
                    buffer.loud > 0
                    and duration >= _CORTE_POR_ENERGIA_MIN_S
                    and (now - buffer.last_loud) >= config.VOICE_GAP_S
                )
                de_sempre = (now - buffer.last) >= config.VOICE_GAP_S or duration >= MAX_SEGMENT_S
                if de_sempre or calou:
                    if not de_sempre:
                        _estatisticas["cortes_por_energia"] += 1
                    del self._buffers[user_id]
                    ended.append((user_id, bytes(buffer.data)))
        # aqui só o filtro barato de duração; o gate de energia (numpy sobre
        # MB) roda no worker, fora do event loop
        done: list[tuple[int, bytes, float]] = []
        for user_id, pcm in ended:
            duration = len(pcm) / BYTES_PER_SECOND
            if duration >= MIN_SPEECH_S:
                done.append((user_id, pcm, now))
            else:
                _debug(f"🎙️ segmento user={user_id} dur={duration:.2f}s -> curto demais")
        return done

    def cleanup(self) -> None:  # base escreveria arquivos; não gravamos nada
        with self._lock:
            self._buffers.clear()


class VoiceListener:
    """Um por servidor; vive enquanto o modo de voz estiver ligado."""

    def __init__(self, player: "GuildPlayer", text_channel) -> None:
        self.player = player
        self.text_channel = text_channel
        self.active = False
        self._sink: CommandSink | None = None
        # maxsize limita o PCM bruto retido (~8 × 2,3MB); cheia = drop-oldest
        self._segments: asyncio.Queue[tuple[int, bytes, float]] = asyncio.Queue(maxsize=8)
        self._tasks: list[asyncio.Task] = []
        self._continuacao = _Continuacao()
        self._sobrecarga = 0  # trechos descartados por fila cheia desde o último aviso
        self._sobrecarga_avisada = float("-inf")

    async def start(self) -> None:
        self._sink = CommandSink()
        voice = self.player.voice
        self._sink.init(voice)  # o start_recording do py-cord 2.8 não faz mais isso
        voice.start_recording(self._sink, self._on_finished)
        self.active = True
        log.info(
            "🎙️ [%s] escuta ligada em %s (motor: %s)",
            self.player.guild.id, getattr(voice, "channel", "?"),
            "Groq" if config.GROQ_API_KEY else f"Whisper local {config.WHISPER_MODEL}",
        )
        if config.AUDIO_OPUS_DIRETO and config.VOICE_SOM_ESCUTA:
            # o estalo em Opus é codificado uma vez por processo (~36 encodes):
            # agora e fora do event loop, não no meio do primeiro "Arrodes"
            asyncio.get_running_loop().run_in_executor(
                None, lambda: audio.efeito_em_opus(audio.efeito_escuta())
            )
        # Groq é IO-bound: 3 transcrições em paralelo dão conta de várias
        # pessoas falando junto; o whisper local é CPU-bound → fica serial
        workers = 3 if config.GROQ_API_KEY else 1
        self._tasks = [asyncio.create_task(self._ticker())] + [
            asyncio.create_task(self._worker()) for _ in range(workers)
        ]

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        log.info("🎙️ [%s] escuta desligada", self.player.guild.id)
        voice = self.player.voice
        try:
            # is_recording() é MÉTODO no py-cord 2.8 (não existe atributo
            # .recording) — o getattr antigo dava sempre False e a gravação
            # nunca era desligada: sink órfão acumulando PCM + religar o
            # /ouvir explodia com "Already recording audio"
            if voice is not None and voice.is_recording():
                voice.stop_recording()
        except Exception:  # noqa: BLE001 — desligar nunca pode explodir
            pass
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        while not self._segments.empty():  # PCM pendente (MB) não fica preso
            self._segments.get_nowait()

    def _on_finished(self, error: Exception | None = None, *args) -> None:
        """Callback síncrono do AudioReader quando a gravação termina.

        Se a escuta ainda está ligada, a gravação morreu por baixo de nós
        (ex.: erro no PacketRouter) — agenda um reinício.
        """
        if error is not None:
            # o parâmetro era ignorado: "gravação caiu" sem nunca dizer por quê
            log.warning("🎙️ [%s] a gravação terminou com erro: %r", self.player.guild.id, error)
        if self.active:
            asyncio.run_coroutine_threadsafe(
                self._restart_recording(), self.player.bot.loop
            )

    async def confere_a_gravacao(self) -> None:
        """Depois de uma reconexão de voz (cogs/lifecycle): se a gravação caiu junto
        com a conexão, religa; se segue viva, não mexe."""
        await self._restart_recording()

    async def _restart_recording(self) -> None:
        await asyncio.sleep(0.5)
        if not self.active:
            return
        voice = self.player.voice
        if voice is None or not voice.is_connected() or voice.is_recording():
            return
        log.warning("🎙️ gravação caiu — reiniciando a escuta")
        self._sink = CommandSink()
        self._sink.init(voice)
        try:
            voice.start_recording(self._sink, self._on_finished)
        except Exception as exc:  # noqa: BLE001 — tenta de novo no próximo ciclo
            log.warning("🎙️ falha ao reiniciar escuta: %r", exc)

    async def _ticker(self) -> None:
        from core.pycord_voice_patch import DIAG

        ticks = 0
        while self.active:
            await asyncio.sleep(_TICK_S)
            ticks += 1
            for item in self._sink.harvest():
                if self._segments.full():  # sobrecarga: sacrifica o mais velho
                    self._segments.get_nowait()
                    self._sobrecarga += 1
                    if time.monotonic() - self._sobrecarga_avisada > 30.0:
                        # sumia calado: era um comando possível indo embora
                        # porque o STT não estava dando conta da conversa
                        log.warning(
                            "🎙️ [%s] fila de transcrição cheia: %d trecho(s) descartado(s)"
                            " sem transcrever — o STT não acompanha a conversa",
                            self.player.guild.id, self._sobrecarga,
                        )
                        self._sobrecarga_avisada = time.monotonic()
                        self._sobrecarga = 0
                self._segments.put_nowait(item)
            if config.VOICE_DEBUG and ticks % int(5 / _TICK_S) == 0:  # diagnóstico a cada 5s
                log.info(
                    "🎙️ diag: sink_writes=%d sink_kb=%d %s",
                    self._sink.diag_writes,
                    self._sink.diag_bytes // 1024,
                    DIAG,
                )

    async def _worker(self) -> None:
        while self.active:
            user_id, pcm, born = await self._segments.get()
            duracao = len(pcm) / BYTES_PER_SECOND
            if time.monotonic() - born > STALE_SEGMENT_S:
                log.info(
                    "🎙️ [%s] trecho de %.1fs descartado: transcrição atrasada",
                    user_id, duracao,
                )
                continue
            bruto = pcm
            hot, pcm = await asyncio.to_thread(
                analyze_speech, pcm, config.VOICE_MIN_RMS
            )
            if hot < _MIN_SPEECH_WINDOWS or len(pcm) / BYTES_PER_SECOND < MIN_SPEECH_S:
                if duracao >= _LOG_DISCARD_MIN_S:
                    # sem isto "falei e nada aconteceu" era invisível no journal;
                    # o pico diz se foi fala baixa (perto do limiar) ou silêncio
                    pico = await asyncio.to_thread(peak_rms, bruto)
                    # INFO só para o que chegou PERTO de ser fala (é o que ajuda
                    # a ajustar o limiar). No teste de 20/09, 20 destas em 8min
                    # eram "0 janelas com voz, pico 90": teclado e respiração.
                    perto = hot >= 1 or pico >= 0.8 * config.VOICE_MIN_RMS
                    log.log(
                        logging.INFO if perto else logging.DEBUG,
                        "🎙️ [%s] trecho de %.1fs descartado como ruído"
                        " (%d janelas com voz, pico RMS %.0f, limiar VOICE_MIN_RMS=%.0f)",
                        user_id, duracao, hot, pico, config.VOICE_MIN_RMS,
                    )
                else:
                    _debug(f"🎙️ trecho de user={user_id} descartado (ruído, {hot} janelas)")
                continue
            if 0 < config.VOICE_TETO_S < len(pcm) / BYTES_PER_SECOND:
                # passou do teto: em vez de sumir calado, a 1ª frase (até a última
                # pausa que cabe) vai — é nela que estaria o "Arrodes, ..."
                cortado = await asyncio.to_thread(
                    corta_na_pausa, pcm, config.VOICE_MIN_RMS, config.VOICE_TETO_S
                )
                if len(cortado) < len(pcm):
                    _estatisticas["cortes_na_pausa"] += 1
                    log.info(
                        "🎙️ [%s] trecho de %.1fs cortado na pausa: só a 1ª frase (%.1fs) vai"
                        " para a Groq, o resto do fôlego passava do teto de %.0fs",
                        user_id, len(pcm) / BYTES_PER_SECOND,
                        len(cortado) / BYTES_PER_SECOND, config.VOICE_TETO_S,
                    )
                    pcm = cortado
            if config.VOICE_DEBUG:  # WAV de 2,3MB: só opt-in, e fora do loop
                await asyncio.to_thread(_save_debug_wav, user_id, pcm)
            started = time.monotonic()
            try:
                contexto = self._contexto_da_fila()
                # sem contexto, a chamada de sempre (um argumento só)
                text = await (speech.transcribe(pcm, contexto) if contexto else speech.transcribe(pcm))
            except Exception as exc:  # noqa: BLE001 — trecho ruim não derruba a escuta
                log.warning("🎙️ erro na transcrição: %r", exc)
                continue
            elapsed = time.monotonic() - started
            # quem acabou de chamar o bot sem dizer o comando: este trecho é o comando
            texto = self._continuacao.completa(user_id, text, time.monotonic()) if text else text
            command = parse_command(texto) if texto else None
            if command is not None and texto == text and not await self._nome_confirmado(user_id, pcm, command[0]):
                continue  # nem estalo, nem resposta, nem janela de continuação
            # a pessoa chamou o bot NESTE trecho (não só completou a chamada
            # anterior, que já tocou o som): estalo antes de qualquer resposta
            chamou = command is not None if texto == text else parse_command(text) is not None
            if chamou:
                try:
                    self.player.tocar_efeito(audio.efeito_escuta())
                except Exception:  # noqa: BLE001 — o som nunca impede o comando
                    log.warning("🎙️ não consegui tocar o som de escuta", exc_info=True)
                if config.VOICE_COLETA:
                    # só aqui: a Groq CONFIRMOU a wake word neste trecho
                    asyncio.get_running_loop().run_in_executor(None, guarda_amostra, user_id, pcm)
            if command is None and text:
                # só o começo (a 1ª palavra e a grafia que mais lembrou o nome),
                # nunca o conteúdo: é o que mostra como o nome sai escrito nas
                # chamadas perdidas e permite ajustar as variações aceitas
                log.info(
                    "🎙️ [%s] sem wake word (começa com %s)%s",
                    user_id, inicio_para_log(text), _resto_seria_comando(text),
                )
            elif command is not None and command[0] == "hello":
                self._continuacao.abre(user_id, time.monotonic(), texto)
            if config.VOICE_DEBUG:
                log.info("🎙️ [%s] transcrito em %.1fs: %r", user_id, elapsed, text)
            else:
                # sem o conteúdo: fala da call não pode virar log permanente
                # no journald (privacidade) — texto completo só no VOICE_DEBUG.
                # Duração e janelas com voz separam fala de ruído transcrito.
                log.info(
                    "🎙️ [%s] transcrito em %.1fs (%d chars, %.1fs de fala aparada,"
                    " %d janelas com voz, comando=%s)",
                    user_id,
                    elapsed,
                    len(text or ""),
                    len(pcm) / BYTES_PER_SECOND,
                    hot,
                    command[0] if command else "-",
                )
            if command is not None:
                # com wake word a frase é PARA o bot — e ele já a repete no
                # canal de texto. Sem ela no journal, os 23% de "unknown"
                # (27 de 119 comandos em cinco dias) eram impossíveis de
                # estudar: não dava para saber o que a pessoa tinha pedido.
                log.info(
                    "🎙️ [%s] comando de voz %s%s: %r",
                    user_id, command[0],
                    f" ({command[1]!r})" if command[1] else "",
                    (texto or "")[:160],
                )
                try:
                    # `texto` e não `text`: quem completou um "Arrodes?" anterior chamou sim
                    await self._execute(user_id, text, *command, de_frente=chamou_de_frente(texto))
                except Exception:  # noqa: BLE001 — worker não pode morrer
                    log.exception("🎙️ erro ao executar comando de voz")

    async def _nome_confirmado(self, user_id: int, pcm: bytes, acao: str) -> bool:
        """O nome foi DITO, ou só o prompt o pôs na transcrição? (2ª escuta, sem ele)

        Também confirma quando a 2ª escuta pede a mesma ação específica que a 1ª:
        sem o prompt o nome pode sair uma palavra qualquer, mas o "próxima"
        continua lá."""
        segunda = await speech.confere_chamada(pcm)
        if segunda is None or nome_no_audio(segunda) or mesmo_comando(segunda, acao):
            return True
        # só o começo e a ação: a frase não era para o bot (privacidade), mas é o
        # que mostra se a conferência barrou o nome dito de um jeito novo — e se
        # a 2ª ouviu outro comando ou nada (06/10: "entao" e a pessoa repetiu)
        lido = parse_command(f"{WAKE_WORD}, {segunda}")
        log.info(
            "🎙️ [%s] '%s' só na 1ª escuta (que leu %s) — a 2ª escuta (sem o"
            " nome no prompt) começa com %r, 2ª leu %s: ignorado",
            user_id, WAKE_WORD.capitalize(), acao, first_word(segunda), lido[0] if lido else "-",
        )
        return False

    def _contexto_da_fila(self) -> str:
        """Títulos e artistas da faixa atual e das próximas, para o prompt do Whisper.

        Título em inglês dito por brasileiro sai aportuguesado na transcrição
        ("pirâmides" para "Pyramids") e aí nem a busca nem a fila casam.

        Curto de propósito, e por PESO: o prompt tem teto de ~224 tokens e a Groq
        RECUSA (400) o que passa — não trunca. Caractere não-ASCII pesa 4: título
        em japonês gasta muito mais tokens que um em inglês do mesmo tamanho.
        """
        try:
            fila = self.player.queue
            faixas = ([fila.current] if fila.current is not None else []) + fila.proximas(8)
            nomes: list[str] = []
            for faixa in faixas:
                for nome in (faixa.title, faixa.artist):
                    if isinstance(nome, str) and nome and nome not in nomes:
                        nomes.append(nome)
        except Exception:  # noqa: BLE001 — contexto é cortesia, a escuta segue
            return ""
        texto, peso = "", 0
        for nome in nomes:
            custo = 2 + sum(1 if ch.isascii() else 4 for ch in nome)
            if peso + custo > _PESO_DO_CONTEXTO:
                break
            texto, peso = texto + ("" if not texto else ", ") + nome, peso + custo
        return f"Músicas: {texto}." if texto else ""

    def _alvo_na_fila(self, arg: str) -> tuple[list[Track], int, str]:
        """O alvo falado ("#3", "#-1", "#1-3" ou um nome) → (faixas, posição da
        primeira, recusa pronta). Com recusa, as faixas vêm vazias."""
        fila = self.player.queue
        todas = fila.todas()
        if not todas:
            return [], 0, m.VOICE_QUEUE_EMPTY
        if not arg.startswith("#"):
            achado = acha_na_fila(todas, arg)
            if achado is None:
                return [], 0, m.VOICE_NOT_IN_QUEUE.format(alvo=arg)
            return [achado[1]], achado[0], ""
        corpo = arg[1:]
        try:
            if "," in corpo:  # "#3,#5": o LLM devolve assim "as duas do Tame Impala"
                posicoes = [int(parte.strip().lstrip("#")) for parte in corpo.split(",") if parte.strip()]
                faixas = [f for f in (fila.na_posicao(p) for p in posicoes) if f is not None]
                recusa = "" if faixas else m.VOICE_NO_POSITION.format(total=len(todas), pos=abs(posicoes[0]))
                return faixas, posicoes[0] if faixas else 0, recusa
            if "-" in corpo[1:]:  # "#1-3"; o "-" de "#-1" é o primeiro caractere
                de, ate = (int(parte) for parte in corpo.split("-", 1))
                faixas = todas[max(1, de) - 1 : max(de, ate)]
                return faixas, de, "" if faixas else m.VOICE_NO_POSITION.format(total=len(todas), pos=de)
            posicao = int(corpo)
        except ValueError:  # o LLM inventou um alvo que não dá para ler
            return [], 0, m.VOICE_NOT_IN_QUEUE.format(alvo=arg)
        faixa = fila.na_posicao(posicao)
        if faixa is None:
            return [], 0, m.VOICE_NO_POSITION.format(total=len(todas), pos=abs(posicao))
        return [faixa], fila.posicao_de(faixa), ""

    async def _execute(
        self, user_id: int, heard: str, action: str, arg: str, *,
        from_llm: bool = False, de_frente: bool = True,
    ) -> None:
        player = self.player
        channel = self.text_channel
        if action in ("play", "play_now", "play_end"):
            # "toca X" com X já NA FILA: antes o bot buscava de novo e punha uma
            # cópia no fim (a 51ª). Só com certeza (exigente): o normal é buscar.
            pedido = sem_o_agora(arg) if action == "play_now" else arg
            na_fila = acha_na_fila(player.queue.todas(), pedido, exigente=True)
            if na_fila is not None and action == "play_end":
                # "coloca X no final": X já espera a vez — nem corta nem duplica
                await channel.send(m.VOICE_JA_NA_FILA.format(track=na_fila[1].display, pos=na_fila[0]))
                return
            if na_fila is not None:
                log.info("🎙️ [%s] %r já está na fila (%dª): tocando de lá", user_id, arg, na_fila[0])
                action, arg = "play_queue", f"#{na_fila[0]}"
        if action in ("pause", "leave"):
            # esses cortam o áudio: sem esperar, o próprio comando engolia o
            # estalo de "estou ouvindo" no meio. Os de fila (skip, jump,
            # play_queue, previous) esperam por ele JUNTO com a preparação do corte.
            await player.espera_efeito()
        if action in ("play", "play_now", "play_end"):
            if action == "play_now":
                # "toca AGORA in the pool": com o "agora" na busca o YouTube Music
                # devolveu um cover de piano (sem ele, a 1ª é a original). Mas
                # "agora" também é nome de música ("Agora Só Falta Você"): as duas
                # buscas saem juntas, e só é corte se a música achada pela frase
                # inteira não tiver o "agora" no nome.
                completa, limpa = await asyncio.gather(youtube.search_one(arg), youtube.search_one(pedido))
                if completa is not None and agora_e_do_nome(completa["title"], arg):
                    found, action = completa, "play"
                else:
                    found = limpa or completa
            else:
                found = await youtube.search_one(arg)
            if found is None:
                log.warning("🎙️ [%s] busca por voz sem resultado: %r", user_id, arg)
                await channel.send(m.VOICE_NOT_FOUND.format(query=arg))
                return
            track = Track(
                title=found["title"],
                artist=found["artist"],
                video_id=found["video_id"],
                duration=found["duration"],
                thumbnail=found["thumbnail"],
                requester_id=user_id,
            )
            # Teste de 21/09: "toca So Am I" entrou como a próxima; ele repetiu o
            # pedido duas vezes ("Soul A M I", "sou M.I." na transcrição), a busca
            # achou a MESMA música e a fila ficou com três cópias — e ela nunca
            # tocou. Pedir de novo o que já está lá é insistir: toca AGORA.
            atual = player.queue.current
            if player.is_playing and atual is not None and atual.video_id == track.video_id:
                await channel.send(m.VOICE_ALREADY_PLAYING.format(track=atual.display))
                return
            repetida = next((t for t in player.queue.todas() if t.video_id == track.video_id), None)
            if repetida is not None and action == "play_end":
                await channel.send(m.VOICE_JA_NA_FILA.format(
                    track=repetida.display, pos=player.queue.posicao_de(repetida)))
                return
            if repetida is not None:
                posicao = player.queue.posicao_de(repetida)
                log.info(
                    "🎙️ [%s] a busca por %r achou a %dª da fila (%s): tocando de lá",
                    user_id, arg, posicao, repetida.display,
                )
                await channel.send(m.VOICE_PLAY_QUEUE.format(track=repetida.display, pos=posicao))
                await player.toca_da_fila(repetida, quem=user_id)
                return
            if action == "play_now" and player.is_playing:
                # "toca X agora": entra na frente e o corte cai nela — extraída
                # antes, com a música atual tocando (como o "toca a terceira")
                await channel.send(m.VOICE_PLAY_NOW.format(heard=heard, track=track.display))
                player.text_channel = channel
                player.queue.poe_na_frente(track, quem=user_id)
                await player.toca_da_fila(track, quem=user_id)
                return
            if action == "play_end":
                posicao = len(player.queue.upcoming) + 1
                if not player.is_playing or not player.queue.todas():
                    await channel.send(m.VOICE_HEARD_PLAY.format(heard=heard, track=track.display))
                else:
                    await channel.send(m.VOICE_HEARD_PLAY_END.format(heard=heard, track=track.display, pos=posicao))
                await player.enqueue([track], channel)
                return
            # o pedido por voz vira a PRÓXIMA, atrás só dos outros pedidos por voz
            # que ainda não tocaram (decisão do Otavio em 21/09: no teste dele os
            # dois pedidos foram para o fim de uma fila de 38)
            posicao = player.queue.quantas_na_frente() + 1
            if not player.is_playing or not player.queue.todas():
                resposta = m.VOICE_HEARD_PLAY
            elif posicao == 1:
                resposta = m.VOICE_HEARD_PLAY_NEXT
            else:
                resposta = m.VOICE_HEARD_PLAY_POS
            await channel.send(resposta.format(heard=heard, track=track.display, pos=posicao))
            await player.enqueue([track], channel, na_frente=True)
        elif action == "play_favs":
            if user_id <= 0:  # áudio ainda sem usuário mapeado (SSRC)
                await channel.send(m.VOICE_FAV_WHO)
                return
            rows = await player.bot.db.get_favorites(
                user_id, player.guild.id, limit=100
            )
            if not rows:
                await channel.send(m.FAVS_EMPTY)
                return
            tracks = [
                Track(
                    title=row["title"],
                    artist=row["artist"],
                    video_id=row["video_id"],
                    duration=row["duration"],
                    thumbnail=row["thumbnail"],
                    requester_id=user_id,
                )
                for row in rows
            ]
            await channel.send(m.VOICE_PLAY_FAVS.format(count=len(tracks)))
            await player.enqueue(tracks, channel)
        elif action == "favorite":
            current = player.queue.current
            if current is None or not current.video_id:
                await channel.send(m.NOTHING_PLAYING)
            elif user_id <= 0:
                await channel.send(m.VOICE_FAV_WHO)
            else:
                added = await player.bot.db.add_favorite(
                    user_id, player.guild.id, current
                )
                msg = m.VOICE_FAV_ADDED if added else m.VOICE_FAV_ALREADY
                await channel.send(
                    msg.format(user=f"<@{user_id}>", track=current.display)
                )
        elif action == "skip":
            quantas = int(arg) if arg.isdigit() else 1
            destino = player.queue.na_posicao(quantas) if quantas > 1 else None
            if quantas <= 1:
                # são 0,72s de estalo antes do corte: o FFmpeg da próxima abre nesse
                # tempo, e o pulo sai sem o ~0,4s de silêncio (fumaça na VM, 21/09)
                antes = player.tocada_agora()
                await asyncio.gather(player.espera_efeito(), player.prepara_o_corte())
                # a faixa pode ter acabado sozinha nessa espera: aí pular derrubava a seguinte
                player.pula_se_ainda_for(antes)
                await channel.send(m.VOICE_SKIPPED)
            elif destino is None:  # pediu mais do que a fila tem: pula tudo
                puladas = player.queue.descarta_da_frente(quantas - 1)
                await player.espera_efeito()
                player.skip()
                await channel.send(m.VOICE_SKIPPED_N_END.format(n=1 + len(puladas)))
            else:
                # "pula 3" = "pula pra terceira": no teste de 21/09 a terceira não
                # estava extraída (o adiantamento cobre as primeiras) e o corte veio
                # antes da extração — 6,8s de silêncio. Agora ela é extraída com a
                # música tocando, e a resposta sai antes dessa espera.
                await channel.send(m.VOICE_SKIPPED_N.format(n=quantas, track=destino.display))
                await player.toca_da_fila(destino, pulando=True, quem=user_id)
        elif action in ("play_queue", "jump"):
            faixas, posicao, recusa = self._alvo_na_fila(arg)
            if recusa:
                await channel.send(recusa)
            else:
                # a resposta sai ANTES: se a faixa ainda não foi extraída, o corte
                # espera ~5s por ela (com a música atual tocando) — na VM a
                # mensagem só aparecia 7,9s depois de a pessoa falar
                if action == "jump" and posicao > 1:
                    await channel.send(m.VOICE_JUMPED.format(track=faixas[0].display, n=posicao - 1))
                elif action == "jump":  # já era a próxima: ninguém ficou pelo caminho
                    await channel.send(m.VOICE_JUMPED_NEXT.format(track=faixas[0].display))
                else:
                    await channel.send(m.VOICE_PLAY_QUEUE.format(track=faixas[0].display, pos=posicao))
                await player.toca_da_fila(faixas[0], pulando=action == "jump", quem=user_id)
        elif action == "play_next":
            faixas, _, recusa = self._alvo_na_fila(arg)
            if faixas:
                player.proxima_sera(faixas[0], quem=user_id)
                await channel.send(m.VOICE_PLAY_NEXT.format(track=faixas[0].display))
            elif arg.startswith("#"):
                await channel.send(recusa)
            else:
                # não está na fila: "bota X como próxima" busca X e põe na frente
                found = await youtube.search_one(arg)
                if found is None:
                    log.warning("🎙️ [%s] busca por voz sem resultado: %r", user_id, arg)
                    await channel.send(m.VOICE_NOT_FOUND.format(query=arg))
                    return
                nova = Track(
                    title=found["title"],
                    artist=found["artist"],
                    video_id=found["video_id"],
                    duration=found["duration"],
                    thumbnail=found["thumbnail"],
                    requester_id=user_id,
                )
                player.text_channel = channel
                player.proxima_sera(nova, quem=user_id)
                await channel.send(m.VOICE_PLAY_NEXT_NEW.format(track=nova.display))
                if not player.is_playing:
                    await player.play_next()
        elif action == "remove":
            faixas, _, recusa = self._alvo_na_fila(arg)
            if recusa:
                await channel.send(recusa)
            else:
                tiradas = player.tira_da_fila(faixas)
                if len(tiradas) == 1:
                    await channel.send(m.VOICE_REMOVED.format(track=tiradas[0].display))
                else:
                    await channel.send(m.VOICE_REMOVED_N.format(n=len(tiradas)))
        elif action == "queue":
            todas = player.queue.todas()
            if not todas:
                await channel.send(m.VOICE_QUEUE_EMPTY)
            else:
                # a fila TODA, por páginas, igual ao /queue (06/10: eram as 10
                # primeiras e "… e mais 40" — a fila quase sempre é uma playlist)
                from cogs.music import QueueView  # cogs importa core: aqui, não no topo

                view = QueueView(player)
                await channel.send(embed=view.build_embed(), view=view)
        elif action == "next_up":
            proxima = player.queue.next_up()
            if proxima is None:
                await channel.send(m.VOICE_QUEUE_EMPTY)
            else:
                await channel.send(m.VOICE_NEXT_UP.format(track=proxima.display))
        elif action == "previous":
            voltou = await player.anterior(int(arg) if arg.isdigit() else 1)
            if voltou is None:
                await channel.send(m.VOICE_NO_PREVIOUS)
            else:
                await channel.send(m.VOICE_PREVIOUS.format(track=voltou.display))
        elif action == "pause":
            player.pause()
            await channel.send(m.VOICE_PAUSED)
        elif action == "resume":
            player.resume()
            await channel.send(m.VOICE_RESUMED)
        elif action == "shuffle":
            player.queue.shuffle()
            # sem isto o adiantamento ficava apontando para a faixa errada
            # e o /skip seguinte pagava a extração inteira (11,8s no log)
            player.queue_changed("embaralhar por voz")
            await channel.send(m.VOICE_SHUFFLED)
        elif action == "autoplay":
            player.set_autoplay(not player.autoplay_enabled)
            state = "ligado" if player.autoplay_enabled else "desligado"
            await channel.send(m.VOICE_AUTOPLAY.format(state=state))
        elif action == "loop":
            player.queue.loop_mode = LoopMode.TRACK
            await channel.send(m.VOICE_LOOP_ON)
        elif action == "loop_off":
            player.queue.loop_mode = LoopMode.OFF
            await channel.send(m.VOICE_LOOP_OFF)
        elif action == "clear_queue":
            # a rádio do autoplay também sai: "limpa a fila" é limpar tudo
            player.queue.upcoming.clear()
            player.queue.radio.clear()
            player.queue_changed("limpar por voz")  # rádio ligada: adiantar a próxima já
            await channel.send(m.VOICE_QUEUE_CLEARED)
        elif action == "now_playing":
            current = player.queue.current
            if current is None:
                await channel.send(m.NOTHING_PLAYING)
            else:
                await channel.send(m.VOICE_NOW_PLAYING.format(track=current.display))
        elif action == "seek":
            estado, destino = await player.pede_seek(arg)
            atual = player.queue.current
            if estado == "ok":
                await channel.send(m.VOICE_SEEK_DONE.format(
                    pos=format_time(destino),
                    dur=format_time(atual.duration) if atual and atual.duration else "?:??",
                ))
            elif estado == "falhou":
                await channel.send(m.VOICE_SEEK_FAILED.format(pos=format_time(destino)))
            elif estado == "nada":
                await channel.send(m.NOTHING_PLAYING)
            elif estado == "alem":
                await channel.send(m.VOICE_SEEK_BEYOND.format(dur=format_time(destino)))
            else:  # o LLM inventou um tempo que não dá para ler
                await channel.send(m.VOICE_UNKNOWN.format(heard=heard))
        elif action == "volume":
            alvo = parse_volume(arg, player.volume)
            if alvo is None:
                await channel.send(m.VOICE_UNKNOWN.format(heard=heard))
            else:
                ficou = await player.set_volume(alvo)
                await channel.send(m.VOICE_VOLUME_SET.format(volume=ficou))
        elif action == "leave":
            await channel.send(m.VOICE_BYE)
            await player.stop(f"comando de voz de {user_id}")  # também desliga esta escuta (via player.listener)
            return
        elif action == "hello":
            await channel.send(m.VOICE_HELLO)
        else:  # unknown → deixa o LLM tentar entender a frase livre
            if not de_frente:
                # "Não, do Herodes que eu te falei." — conversa entre duas
                # pessoas, "Herodes" é grafia aceita da wake word, e o bot se
                # metia com um "não conheço esse comando"; indo ao LLM, ele
                # inventava um play com o resto da frase. Wake word no MEIO da
                # frase e nada que a gramática reconheça: fica quieto, sem LLM.
                log.info("🎙️ [%s] wake word no meio da frase e nenhum comando: era conversa", user_id)
                return
            if not from_llm:
                inicio = time.monotonic()
                # com a fila na mão o LLM resolve "aquela do Frank Ocean" para
                # uma posição — é onde a gramática não chega
                fila = [t.display for t in player.queue.todas()[:20]]
                result = await (llm.interpret(heard, fila=fila) if fila else llm.interpret(heard))
                log.info(
                    "🎙️ [%s] LLM leu %r como %s em %.1fs",
                    user_id, heard[:160], result, time.monotonic() - inicio,
                )
                if result is not None and _play_sem_pedido(heard, *result):
                    # 26/09: 9 músicas numa noite saíram de conversa ("a gente não
                    # falou da tia" → Zé Neto & Cristiano). O LLM sempre acha
                    # uma busca; quem pede música diz "toca", "põe", "quero ouvir".
                    log.info("🎙️ [%s] LLM quis tocar %r sem ninguém pedir: ignorado", user_id, result[1])
                    return
                if result is not None and result[0] != "none":
                    await self._execute(user_id, heard, *result, from_llm=True)
                    return
            await channel.send(m.VOICE_UNKNOWN.format(heard=heard))
        # painel desce para o fim do chat: as respostas acima o deslocam
        # ("play"/"play_favs" já fazem isso dentro do enqueue)
        if action not in ("play", "play_favs"):
            await player.panel.ensure_visible(channel)
