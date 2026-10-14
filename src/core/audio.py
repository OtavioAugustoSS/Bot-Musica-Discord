"""Fontes de áudio auxiliares do player."""
from __future__ import annotations

import logging
import os
import queue
import re
import shlex
import threading
import time
import wave
from collections import deque
from functools import lru_cache

import discord
import numpy as np
from discord.oggparse import OggError, OggStream
from discord.opus import Encoder

log = logging.getLogger(__name__)

FRAME_BYTES = Encoder.FRAME_SIZE  # 3840 = 20ms de PCM 48kHz stereo s16
SILENCIO = b"\x00" * FRAME_BYTES
# 20ms de silêncio já em Opus — o mesmo pacote que o py-cord manda ao pausar
OPUS_SILENCIO = b"\xf8\xff\xfe"
_FRAMES_POR_S = 1000 / Encoder.FRAME_LENGTH  # 50
_CABECALHOS_OGG = (b"OpusHead", b"OpusTags")
# Byte TOC do Opus (RFC 6716, tabela 2): as configs cujo quadro tem 20ms, com
# código 0 = UM quadro no pacote. O py-cord avança 960 amostras (20ms) no relógio
# RTP a cada pacote sem olhar o conteúdo: pacote de 40/60ms tocaria em câmera lenta.
_CONFIGS_20MS = frozenset({1, 5, 9, 13, 15, 19, 23, 27, 31})
_TOC_DE_20MS = bytes((toc >> 3) in _CONFIGS_20MS and not toc & 3 for toc in range(256))
# quadros de silêncio codificados antes e depois do estalo em Opus: emendar
# pacotes de OUTRO stream no meio da música pega o decoder de quem ouve com o
# estado da música — 40ms de silêncio dão a ele onde assentar, nas duas pontas
ESTALO_BORDA_FRAMES = 2

# quanto esperar por um frame antes de desistir e mandar silêncio. Curto de
# propósito: o orçamento de cada volta do py-cord é 20ms.
_ESPERA_FRAME_S = 0.01
# teto para o pré-buffer: acima disso é melhor começar a tocar meio seco do
# que somar latência ao início da faixa
_PREBUFFER_TIMEOUT_S = 1.0
# Enquanto o pulmão não encheu pela PRIMEIRA vez, esperar é o certo — foi o
# que o py-cord sempre fez. Na VM o FFmpeg leva ~2,4s só para abrir o stream
# do YouTube e ainda hesita logo depois do primeiro frame; devolver silêncio
# nessa janela fazia a faixa começar picotada. O teto acompanha o rw_timeout
# do FFmpeg: passou disso, o stream não vem mesmo.
_ESPERA_INICIO_S = 15.0
# a espera do início é feita em fatias, conferindo entre elas se a fonte morreu
_FATIA_ESPERA_S = 0.05


def pacote_de_20ms(pacote: bytes) -> bool:
    """O pacote Opus tem exatamente um quadro de 20ms? (é o que o py-cord assume)"""
    return bool(pacote) and bool(_TOC_DE_20MS[pacote[0]])


# ── som de "estou ouvindo" (wake word do /ouvir) ─────────────────────────────
# o estalo foi cortado de um efeito de chicote (uma instância, fade no fim) e
# normalizado a -1 dBFS; 0,7 deixa margem para somar com a música
VOLUME_EFEITO = 0.7
# a música cai pela metade enquanto o estalo toca — como a Alexa faz; sem
# isso ele some no meio de um funk alto
DUCK = 0.5
EFEITO_ESCUTA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "escuta.wav"
)
_efeito_escuta: bytes | None = None


def carrega_efeito(caminho: str) -> bytes:
    """WAV 48kHz estéreo 16-bit → PCM com o volume do efeito, em frames inteiros.

    Frames inteiros porque o PCMAudio do py-cord descarta o último pedaço
    incompleto (o som terminaria com um corte seco).
    """
    with wave.open(caminho, "rb") as w:
        formato = (w.getframerate(), w.getnchannels(), w.getsampwidth())
        if formato != (48000, 2, 2):
            raise ValueError(f"{caminho}: precisa ser 48kHz estéreo 16-bit, é {formato}")
        bruto = w.readframes(w.getnframes())
    amostras = np.frombuffer(bruto[: len(bruto) - len(bruto) % 2], dtype=np.int16)
    pcm = (amostras.astype(np.float32) * VOLUME_EFEITO).astype(np.int16).tobytes()
    return pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)


def efeito_escuta() -> bytes:
    """O som de escuta, carregado uma vez. b"" (sem som) se o arquivo faltar."""
    global _efeito_escuta
    if _efeito_escuta is None:
        try:
            _efeito_escuta = carrega_efeito(EFEITO_ESCUTA_PATH)
        except (OSError, ValueError, wave.Error) as exc:
            log.warning("som de escuta indisponível (%s) — seguindo sem ele", exc)
            _efeito_escuta = b""
    return _efeito_escuta


@lru_cache(maxsize=4)
def efeito_em_opus(pcm: bytes) -> tuple[bytes, ...]:
    """O PCM do estalo → pacotes Opus prontos, codificados UMA vez por processo.

    No caminho Opus direto não existe PCM onde misturar: o estalo entra como
    pacotes próprios e a música fica retida enquanto ele toca (~0,7s). O encoder
    daqui vive só durante esta chamada; o `bytes` guarda o próprio hash, então a
    consulta ao cache não relê os 120 KB.
    """
    if not pcm:
        return ()
    encoder = Encoder()
    borda = [SILENCIO] * ESTALO_BORDA_FRAMES
    quadros = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
    return tuple(encoder.encode(q, Encoder.SAMPLES_PER_FRAME) for q in [*borda, *quadros, *borda])


def duracao_do_estalo_s(pcm: bytes, opus: bool) -> float:
    """Quanto o estalo ocupa a saída — o que skip/pausa/sair esperam antes de cortar."""
    quadros = len(pcm) // FRAME_BYTES
    if opus and quadros:
        quadros += 2 * ESTALO_BORDA_FRAMES
    return quadros / _FRAMES_POR_S


class PacotesProntos(discord.AudioSource):
    """Pacotes Opus já codificados como fonte: o estalo com o bot parado na call,
    sem o py-cord criar um encoder só para ele."""

    def __init__(self, pacotes) -> None:
        self._pacotes = iter(pacotes)

    def read(self) -> bytes:
        return next(self._pacotes, b"")

    def is_opus(self) -> bool:
        return True


def mistura(frame: bytes, efeito: bytes, de: float = DUCK, ate: float = DUCK) -> bytes:
    """Um frame da música (abaixada) + um frame do efeito, sem estourar o int16.

    `de`/`ate`: ganho da música no começo e no fim do frame. Cortar pela
    metade de uma vez é um degrau na forma de onda — soa como clique.
    """
    musica = np.frombuffer(frame, dtype=np.int16).astype(np.int32)
    som = np.frombuffer(efeito, dtype=np.int16).astype(np.int32)
    if len(musica) != len(som):
        return frame  # nunca acontece com frames do py-cord; não arrisca o áudio
    ganho = de if de == ate else np.repeat(np.linspace(de, ate, len(musica) // 2), 2)
    saida = (musica * ganho).astype(np.int32) + som
    return np.clip(saida, -0x8000, 0x7FFF).astype(np.int16).tobytes()


_URL_RE = re.compile(r"https?://\S+")
# a rotina do CDN: o googlevideo derruba a conexão a cada ~31s e o FFmpeg
# reconecta sozinho. Eram 3 linhas cruas no journal por queda (654 em 50 dias)
_ROTINA_FFMPEG = ("error in the pull function", "connection reset by peer")
# os status que o FFmpeg re-tenta (-reconnect_on_http_error 403,5xx)
_RECUSA_HTTP_RE = re.compile(r"http error (403|5\d\d)\b")


class SaidaDoFfmpeg:
    """O stderr do FFmpeg com dono: cada linha sai no log com servidor e faixa.

    Herdando o stderr do processo, as linhas do FFmpeg caíam cruas no
    journal — sem horário do bot, sem nível, sem dizer de qual faixa eram, e
    fora do espelho de erros que vai ao dono (services/alerts). Foi assim
    que o 403 de 20/09 passou: "HTTP error 403 Forbidden" estava lá, solto,
    e nada ligava aquilo à faixa que não tocou.

    Aqui a rotina (queda e reconexão do CDN, 403 que o FFmpeg re-tenta) vira
    CONTADOR, relatado uma vez no fim da faixa; o resto vira WARNING na hora.
    Um pipe de verdade, e não um objeto com write(): o leitor de stderr do
    py-cord gira em falso (CPU a 100%) entre o fim do FFmpeg e o cleanup.
    """

    def __init__(self, rotulo: str) -> None:
        leitura, escrita = os.pipe()
        self.escrita = os.fdopen(escrita, "wb")  # esta ponta vai para o Popen
        self._leitura = os.fdopen(leitura, "rb")
        self.rotulo = rotulo
        self.reconexoes = 0  # quedas do CDN no meio do stream
        self.recusas = 0  # 403/5xx que o FFmpeg re-tentou
        self._recusa_pendente = False
        self.erros: list[str] = []  # o que não é rotina (as últimas)
        self._thread = threading.Thread(target=self._le, name="ffmpeg-stderr", daemon=True)

    def comeca(self) -> None:
        """Depois do Popen: o pai solta a ponta de escrita, senão o EOF nunca vem."""
        try:
            self.escrita.close()
        except OSError:
            pass
        self._thread.start()

    def espera(self, timeout: float = 0.5) -> None:
        """Dá tempo de as últimas linhas chegarem (o FFmpeg acabou de sair)."""
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _le(self) -> None:
        try:
            for bruta in self._leitura:
                self._linha(bruta.decode("utf-8", errors="replace").strip())
        except (OSError, ValueError):
            pass  # pipe fechado por baixo: a faixa acabou
        finally:
            try:
                self._leitura.close()
            except OSError:
                pass

    def _linha(self, linha: str) -> None:
        if not linha:
            return
        # a URL assinada tem ~1200 caracteres e não explica nada
        linha = _URL_RE.sub("<url>", linha)[:300]
        baixa = linha.lower()
        if _RECUSA_HTTP_RE.search(baixa):
            # com -reconnect_on_http_error o FFmpeg tenta de novo: só é
            # problema se a faixa morrer, e isso o fim da faixa acusa. Visto
            # na VM (FFmpeg 6.1): "HTTP error 403 Forbidden" e, na linha
            # SEGUINTE, "Will reconnect at 0 in 1 second(s)." sem o motivo.
            self.recusas += 1
            self._recusa_pendente = True
            log.debug("[%s] ffmpeg: %s", self.rotulo, linha)
            return
        if "will reconnect" in baixa:
            if self._recusa_pendente:
                self._recusa_pendente = False  # é a re-tentativa da recusa acima
            else:
                self.reconexoes += 1
            log.debug("[%s] ffmpeg: %s", self.rotulo, linha)
            return
        if any(marca in baixa for marca in _ROTINA_FFMPEG):
            log.debug("[%s] ffmpeg: %s", self.rotulo, linha)
            return
        self.erros = [*self.erros[-9:], linha]
        log.warning("[%s] ffmpeg: %s", self.rotulo, linha)


class NumpyVolumeTransformer(discord.PCMVolumeTransformer):
    """PCMVolumeTransformer com read() vetorizado.

    O Python 3.13 removeu o ``audioop``; sem ele o py-cord aplica o volume
    num loop Python puro por amostra (~96 mil iterações/s segurando a GIL
    na thread de áudio, que disputa com a recepção de voz). Mesma
    matemática do upstream — escala, floor e clamp em int16 — em três
    operações numpy. O setter de ``volume`` continua funcionando ao vivo.
    """

    def read(self) -> bytes:
        frame = self.original.read()
        if not frame:
            return frame
        volume = min(self._volume, 2.0)
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) * volume
        return np.floor(samples).clip(-0x8000, 0x7FFF).astype(np.int16).tobytes()


class FonteOpus(discord.FFmpegAudio):
    """FFmpeg com saída Ogg/Opus → UM pacote Opus pronto por read().

    O bot decodificava o Opus do YouTube para PCM, aplicava o volume em numpy
    (Python, 50x por segundo) e o py-cord recodificava tudo para Opus na thread
    de tempo real. Medido na VM (src/bench_audio.py, 20/09/2026), em ms de CPU
    por segundo de áudio: 118,0 nesse caminho; 17,0 copiando o Opus (`copia`);
    43,3 recodificando DENTRO do FFmpeg, que é o que se usa quando há volume a
    aplicar ou o áudio não é Opus 48 kHz estéreo.

    Subclasse da FFmpegAudio e não a FFmpegOpusAudio do py-cord: aquela passa
    -b:a, -fec e -packet_loss mesmo em copy (aviso no stderr a cada faixa) e
    entrega OpusHead/OpusTags ao Discord como se fossem áudio.
    """

    def __init__(
        self,
        source: str,
        *,
        before_options: str = "",
        copia: bool = True,
        volume: float = 1.0,
        inicio_s: float = 0.0,
        stderr=None,
        executable: str = "ffmpeg",
    ) -> None:
        args = shlex.split(before_options)
        if inicio_s > 0:
            # ANTES do -i: o FFmpeg pula pelo índice do arquivo (um Range HTTP)
            # em vez de baixar e descartar tudo até lá
            args += ["-ss", f"{inicio_s:.2f}"]
        args += ["-i", source, "-vn", "-loglevel", "warning"]
        if copia:
            args += ["-c:a", "copy"]
            if inicio_s > 0:
                # sem isto a cópia começa no ponto de busca ANTERIOR ao pedido:
                # medido com -ss 2 / 17,3 / 41, saíram 2,00 / 2,32 / 1,02s a
                # mais; com a opção, 0,00s nos três
                args += ["-copypriorss", "0"]
        else:
            args += [
                "-af", f"volume={volume:.2f}", "-c:a", "libopus", "-b:a", "128k",
                "-frame_duration", "20", "-ar", "48000", "-ac", "2",
            ]
        args += ["-f", "opus", "pipe:1"]
        self.copia = copia
        self.fora_de_20ms = 0  # pacotes que não têm um quadro de 20ms (ver _TOC_DE_20MS)
        self._encerrada = False
        super().__init__(source, executable=executable, args=args, stderr=stderr)
        self._pacotes = OggStream(self._stdout).iter_packets()

    def read(self) -> bytes:
        try:
            for pacote in self._pacotes:
                if not pacote or pacote[:8] in _CABECALHOS_OGG:
                    continue  # o OggStream entrega os cabeçalhos como pacote comum
                if not _TOC_DE_20MS[pacote[0]]:
                    self.fora_de_20ms += 1
                return pacote
        except (OggError, OSError, ValueError) as exc:
            # FFmpeg morto por NÓS (skip, stop, seek) corta a página Ogg ao meio:
            # é fim normal, e não pode virar ERROR no espelho que vai ao dono
            if not self._encerrada:
                log.warning("stream Ogg do FFmpeg quebrou no meio: %r", exc)
        return b""

    def is_opus(self) -> bool:
        return True

    def cleanup(self) -> None:
        self._encerrada = True
        super().cleanup()


class BufferedAudioSource(discord.AudioSource):
    """Alguns segundos de áudio adiantados numa thread — o pulmão do playback.

    Serve aos dois formatos: frames de PCM (3840 bytes) ou pacotes Opus prontos;
    para o pulmão são só "20ms de áudio" numa fila.

    Sem isto o único buffer entre o FFmpeg e o Discord é o pipe do sistema
    operacional: 64 KB, 0,33s de áudio. E o laço do py-cord bloqueia no
    ``read()`` e, quando os dados voltam, dispara uma rajada de pacotes
    atrasados que o jitter buffer do cliente descarta — é assim que "some
    um pedaço da música". No journal de produção as duas causas de pausa
    aparecem o tempo todo: o CDN do googlevideo derrubando a conexão a cada
    ~31s e a CPU saturada pela extração (até 48s) ou pelo Whisper local
    (até 131s), tudo num núcleo só.

    Com o pulmão cheio, nada disso chega ao ouvinte. De quebra o FFmpeg
    passa a baixar à frente em vez de em tempo real, e é justamente a
    estagnação do TCP que faz o CDN derrubar a conexão.

    Quando mesmo assim falta áudio, devolve silêncio no ritmo certo em vez
    de travar: silêncio curto não perde conteúdo, rajada perde.
    """

    def __init__(
        self,
        original: discord.AudioSource,
        *,
        segundos: float = 8.0,
        prebuffer_s: float = 0.6,
        silencio_max_s: float = 20.0,
        silencio: bytes | None = None,
    ) -> None:
        self.original = original
        # o que sai quando falta áudio, no FORMATO da fonte: PCM zerado mandado
        # como se fosse pacote Opus é lixo para o decoder de quem ouve
        self._opus = bool(original.is_opus())
        self._silencio = silencio if silencio is not None else (OPUS_SILENCIO if self._opus else SILENCIO)
        self._fila: queue.Queue[bytes] = queue.Queue(
            maxsize=max(2, int(segundos * _FRAMES_POR_S))
        )
        self._prebuffer = max(1, int(prebuffer_s * _FRAMES_POR_S))
        self._silencio_max = max(1, int(silencio_max_s * _FRAMES_POR_S))
        self._fim = threading.Event()  # a fonte acabou (EOF)
        # chamado UMA vez, da thread do pulmão, quando a fonte acaba sozinha: o
        # FFmpeg terminou de baixar e ainda há até `segundos` de música na fila
        # — é a deixa para o player montar a próxima faixa (ver quando_acabar)
        self._ao_acabar = None
        self._trava_aviso = threading.Lock()
        self._parar = threading.Event()  # cleanup pedido
        self._aquecido = False
        self._silencio_seguido = 0
        self._encheu = False  # o pulmão já chegou ao alvo alguma vez?
        # contadores para o log do fim da faixa: é o que prova se o ouvinte
        # perdeu áudio ou não
        self.frames_sem_audio = 0
        self.falhas_de_buffer = 0
        # frames de áudio DE VERDADE entregues ao py-cord: quanto a faixa tocou
        self.frames_entregues = 0
        # do nascimento da fonte ao primeiro frame de áudio que saiu: é o
        # começo REAL da música ("tocando X" no log é só o FFmpeg subindo)
        self._criada_em = time.monotonic()
        self.primeiro_audio_s: float | None = None
        # efeito por cima da música (som de escuta): misturado na LEITURA, não
        # no pulmão — senão sairia AUDIO_BUFFER_S depois de a pessoa chamar
        self._efeito: deque[tuple[bytes, tuple[float, float]]] = deque()
        self._thread = threading.Thread(
            target=self._abastece, name="audio-buffer", daemon=True
        )
        self._thread.start()

    # --- thread de leitura ---

    def _abastece(self) -> None:
        try:
            while not self._parar.is_set():
                frame = self.original.read()
                if not frame:
                    break
                while not self._parar.is_set():
                    try:
                        self._fila.put(frame, timeout=0.2)
                        break
                    except queue.Full:
                        continue  # consumidor mais lento: normal, é o buffer cheio
        except Exception:  # noqa: BLE001 — a thread não pode morrer calada
            log.exception("buffer de áudio falhou ao ler a fonte")
        finally:
            self._fim.set()
            self._avisa_fim()

    def quando_acabar(self, aviso) -> None:
        """`aviso()` roda UMA vez quando a fonte acabar sozinha — na hora, se já
        acabou (faixa curta que coube inteira no pulmão antes de alguém pedir)."""
        self._ao_acabar = aviso
        if self._fim.is_set():
            self._avisa_fim()

    def _avisa_fim(self) -> None:
        with self._trava_aviso:
            aviso, self._ao_acabar = self._ao_acabar, None
        if aviso is None or self._parar.is_set():
            return  # skip/stop/seek mataram a fonte: não é "a faixa está acabando"
        try:
            aviso()
        except Exception:  # noqa: BLE001 — aviso é cortesia, o áudio segue
            log.exception("aviso de fim de fonte falhou")

    def _aquece(self) -> None:
        """Espera o pulmão encher antes do primeiro frame, com teto de tempo.

        O teto é por relógio e não por número de esperas: contando iterações
        de 10ms o teto virava 2s numa máquina com timer grosso (Windows).
        """
        self._aquecido = True
        limite = time.monotonic() + _PREBUFFER_TIMEOUT_S
        while time.monotonic() < limite:
            if (
                self._fila.qsize() >= self._prebuffer
                or self._fim.is_set()
                or self._parar.is_set()
            ):
                return
            time.sleep(0.005)

    # --- efeito por cima ---

    def sobrepor(self, pcm: bytes) -> None:
        """Toca `pcm` por cima da música a partir da próxima leitura.

        Chamado do event loop; a thread de áudio só faz popleft. Trocar a
        deque inteira é atômico: um efeito novo substitui o que tocava.
        A música desce no primeiro frame e volta no último.

        Só existe em PCM: num pulmão de pacotes Opus não há onde somar amostras
        (lá o estalo é da FonteDaFaixa, que segura a música enquanto ele toca).
        """
        if self._opus:
            return
        pedacos = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
        # outro efeito ainda tocando: a música já está abaixada, desce de lá
        inicio = DUCK if self._efeito else 1.0
        ganhos = [(DUCK, DUCK)] * len(pedacos)
        if len(pedacos) == 1:
            ganhos[0] = (inicio, 1.0)
        elif pedacos:
            ganhos[0] = (inicio, DUCK)
            ganhos[-1] = (DUCK, 1.0)
        self._efeito = deque(zip(pedacos, ganhos))

    def _com_efeito(self, frame: bytes) -> bytes:
        efeito = self._efeito
        if not efeito:
            return frame
        try:
            pedaco, (de, ate) = efeito.popleft()
        except IndexError:  # a deque esvaziou entre o teste e o pop
            return frame
        return mistura(frame, pedaco, de, ate)

    # --- AudioSource ---

    def _pega(self, espera: float) -> bytes | None:
        """Próximo frame do pulmão, esperando até `espera` — mas não por fonte morta.

        A espera longa do início de faixa (15s) é fatiada: com a fonte já
        encerrada e o pulmão vazio não há o que esperar. Sem isto, o FFmpeg
        que levava 403 ao abrir a URL custava 15s de call muda por faixa.
        """
        limite = time.monotonic() + espera
        while True:
            resta = limite - time.monotonic()
            try:
                return self._fila.get(timeout=max(0.0, min(_FATIA_ESPERA_S, resta)))
            except queue.Empty:
                if resta <= _FATIA_ESPERA_S or self._fim.is_set() or self._parar.is_set():
                    # o último frame pode ter entrado entre o timeout e o teste
                    try:
                        return self._fila.get_nowait()
                    except queue.Empty:
                        return None

    def read(self) -> bytes:
        if not self._aquecido:
            self._aquece()
        if not self._encheu and self._fila.qsize() >= self._prebuffer:
            self._encheu = True
        # no começo da faixa espera de verdade; com o pulmão já cheio uma
        # vez, silêncio curto é melhor que travar (o py-cord descarta o que
        # ficou para trás)
        frame = self._pega(_ESPERA_FRAME_S if self._encheu else _ESPERA_INICIO_S)
        if frame is not None:
            self._silencio_seguido = 0
            if not self.frames_entregues:
                self.primeiro_audio_s = time.monotonic() - self._criada_em
            self.frames_entregues += 1
            return self._com_efeito(frame)
        if self._fim.is_set() or self._parar.is_set():
            return b""  # a fonte acabou de verdade: fim da faixa
        if self._silencio_seguido == 0:
            self.falhas_de_buffer += 1
        self._silencio_seguido += 1
        self.frames_sem_audio += 1
        if self._silencio_seguido >= self._silencio_max:
            log.warning(
                "sem áudio por %.0fs — encerrando a faixa",
                self._silencio_seguido / _FRAMES_POR_S,
            )
            return b""
        return self._com_efeito(self._silencio)

    def is_opus(self) -> bool:
        return self._opus

    def encheu(self) -> bool:
        """Já tem áudio para começar sem buraco? (quem troca a fonte no meio da
        faixa — seek, volume — espera isto antes de trocar)"""
        return self._fila.qsize() >= self._prebuffer or (self._fim.is_set() and not self._fila.empty())

    def descarta(self, quantos: int) -> int:
        """Joga fora até `quantos` frames JÁ na fila; devolve quantos foram.

        Para a fonte que ficou pronta DEPOIS da hora marcada (troca de volume):
        o começo dela já tocou pela fonte antiga."""
        feitos = 0
        while feitos < quantos:
            try:
                self._fila.get_nowait()
            except queue.Empty:
                break
            feitos += 1
        return feitos

    def acabou_vazia(self) -> bool:
        """A fonte encerrou sem deixar nada: o FFmpeg não abriu."""
        return self._fim.is_set() and self._fila.empty()

    def marca_inicio(self) -> None:
        """A faixa começa AGORA: fonte montada antes da vez dela (a próxima faixa,
        pronta antes de a atual acabar) não pode contar a espera como demora."""
        self._criada_em = time.monotonic()

    def interrompe(self) -> None:
        """Faz o read() devolver fim de faixa JÁ — chamar antes de parar o player.

        `stop_playing()` só marca o fim no AudioPlayer do py-cord; a thread
        dele segue presa no read() daqui até vir um frame (FFmpeg abrindo: até
        15s) e só então dispara o `after`, atrasado e com a fila já em outro
        lugar. O cleanup(), que soltaria, só roda DEPOIS que esse laço sai.
        """
        self._parar.set()

    def cleanup(self) -> None:
        self._parar.set()
        try:
            # mata o FFmpeg primeiro: é o que destrava um read() pendurado
            self.original.cleanup()
        finally:
            while True:  # esvazia para destravar um put() em espera
                try:
                    self._fila.get_nowait()
                except queue.Empty:
                    break
            self._thread.join(timeout=2.0)


class FonteDaFaixa(discord.AudioSource):
    """A faixa inteira aos olhos do py-cord — mesmo que o FFmpeg mude por baixo.

    O py-cord recebe UMA fonte por `voice.play()` e chama o `after` quando ela
    acaba. Seek e troca de volume precisam de um FFmpeg novo no MEIO da faixa:
    com `stop` + `play` o `after` dispararia, a fila andaria e a pausa se
    perderia. Aqui a fonte de DENTRO (um pulmão) troca por atribuição e o py-cord
    não vê nada. O formato não muda durante a faixa: `is_opus()` é constante.

    É também a dona do estalo de escuta no caminho Opus: sem PCM não há onde
    misturar, então o estalo entra como pacotes próprios e a música fica RETIDA
    enquanto ele toca — nada se perde, ela só atrasa ~0,7s.
    """

    def __init__(self, fonte: BufferedAudioSource, *, offset_s: float = 0.0) -> None:
        self._fonte = fonte
        self._opus = bool(fonte.is_opus())
        # em que ponto da MÚSICA a fonte de dentro começou (0, ou o destino do seek)
        self.offset_s = offset_s
        self._parar = False
        self._estalo: deque[bytes] = deque()
        # o que as fontes já trocadas entregaram: o fim de faixa relata a soma
        self._entregues = self._sem_audio = self._falhas = self._fora = 0
        self._primeiro_audio_s: float | None = None

    # --- o que o fim de faixa lê ---

    @property
    def frames_entregues(self) -> int:
        """Quanto TOCOU (soma de todas as fontes); `posicao_s` é onde a música está."""
        return self._entregues + self._fonte.frames_entregues

    @property
    def frames_sem_audio(self) -> int:
        return self._sem_audio + self._fonte.frames_sem_audio

    @property
    def falhas_de_buffer(self) -> int:
        return self._falhas + self._fonte.falhas_de_buffer

    @property
    def fora_de_20ms(self) -> int:
        return self._fora + getattr(self._fonte.original, "fora_de_20ms", 0)

    @property
    def primeiro_audio_s(self) -> float | None:
        if self._primeiro_audio_s is not None:
            return self._primeiro_audio_s
        return self._fonte.primeiro_audio_s

    @property
    def posicao_s(self) -> float:
        return self.offset_s + self._fonte.frames_entregues / _FRAMES_POR_S

    # --- troca e estalo (chamados do event loop) ---

    def troca(self, nova: BufferedAudioSource, offset_s: float) -> BufferedAudioSource:
        """Passa a ler de `nova`, que começa em `offset_s` da música. Devolve a
        antiga para quem chamou limpar FORA do event loop (o cleanup faz join)."""
        antiga = self._fonte
        self._entregues += antiga.frames_entregues
        self._sem_audio += antiga.frames_sem_audio
        self._falhas += antiga.falhas_de_buffer
        self._fora += getattr(antiga.original, "fora_de_20ms", 0)
        if self._primeiro_audio_s is None:
            self._primeiro_audio_s = antiga.primeiro_audio_s
        self.offset_s = offset_s
        self._fonte = nova  # atribuição atômica: a próxima leitura já é da nova
        antiga.interrompe()  # solta a thread de áudio se estiver presa na antiga
        return antiga

    def sobrepor(self, pcm: bytes) -> None:
        if self._opus:
            self._estalo = deque(efeito_em_opus(pcm))  # trocar a deque inteira é atômico
        else:
            self._fonte.sobrepor(pcm)

    # --- AudioSource (thread de áudio) ---

    def read(self) -> bytes:
        if self._parar:
            return b""
        if self._estalo:
            try:
                return self._estalo.popleft()
            except IndexError:  # esvaziou entre o teste e o pop
                pass
        fonte = self._fonte
        dado = fonte.read()
        if not dado and fonte is not self._fonte and not self._parar:
            # trocaram a fonte DURANTE esta leitura: o fim da antiga (interrompida
            # pelo seek) não é o fim da faixa
            dado = self._fonte.read()
        return dado

    def is_opus(self) -> bool:
        return self._opus

    def interrompe(self) -> None:
        """Fim de faixa JÁ (skip/stop): ver BufferedAudioSource.interrompe."""
        self._parar = True
        self._fonte.interrompe()

    def cleanup(self) -> None:
        self._parar = True
        self._fonte.cleanup()
