"""Transcrição de fala: Groq (nuvem, se houver chave) com fallback local.

Local: faster-whisper tiny/int8 — rápido mas fraco com pt+inglês misturado.
Groq: whisper-large-v3-turbo — excelente com code-switching, ~1s, tier grátis.
"""
from __future__ import annotations

import asyncio
import functools
import gc
import io
import logging
import re
import threading
import time
import wave
from collections import deque

import numpy as np

import config
from core.intents import WAKE_WORD
from services import http

log = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

_model = None
_model_lock = threading.Lock()
_preload_task = None  # carga do modelo local em background (nunca no comando)

# O Groq se recupera de 429 em ~1-2s; o fallback local custa 46s FRIO na VM
# de produção (medido) e o trecho é descartado por STALE_SEGMENT_S antes
# disso. Insistir na nuvem é sempre melhor que cair para o local.
_GROQ_TENTATIVAS = 3

# Caps GLOBAIS — uma API key e uma CPU para TODOS os servidores. Cada guild
# tem 3 workers de escuta (core/listen.py); sem isso, N guilds viravam 3N
# requests simultâneos no Groq (429) e um stampede de Whisper local.
_groq_sem = asyncio.Semaphore(3)
_local_sem = asyncio.Semaphore(1)  # whisper local é CPU-bound (GIL): um por vez

_agora = time.monotonic  # indireção para os testes controlarem o relógio
# Medido em 20/09/2026: passando do teto do prompt (~224 tokens) a Groq não trunca,
# RECUSA com 400 (696 caracteres passaram, 1806 falharam). Se o contexto (títulos
# da fila) causar um 400, fica desligado por este tempo — senão todo comando de
# voz morreria enquanto aquela fila estivesse carregada.
_CASTIGO_DO_CONTEXTO_S = 600.0
_sem_contexto_ate = float("-inf")
# Acima disto, o fallback local custa mais do que vale: no journal de 16/09
# ele levou de 83 a 131 SEGUNDOS por frase nesta VM de um núcleo, engasgando
# a música e entregando o comando quando ninguém mais esperava. A primeira
# transcrição que passar do limite desliga o local pelo resto da execução.
_LOCAL_LENTO_S = 10.0
_local_lento = False

# ── orçamento da Groq ────────────────────────────────────────────────────────
# O tier grátis do Whisper dá 20 requisições por minuto, e TODA fala da call
# vira requisição (98% sem wake word, medido em 7.069 transcrições). No journal
# o pico foi exatamente 20/min: 112 falhas por 429 e 51 comandos perdidos — o
# 429 escolhe a vítima ao acaso. Dos 80 trechos que eram comando, o mais longo
# tinha 5,6s (p90 3,9s); dos que não eram, 300 passavam de 6s. Com o minuto
# acabando, quem fica de fora é o trecho comprido, que nunca é comando.
_COMANDO_LONGO_S = 6.0  # minuto apertado (75% do teto): acima disto não vai
_COMANDO_CURTO_S = 4.0  # no teto: só o que tem cara de comando
_ESPERA_MAX_429_S = 6.0  # quanto um trecho curto espera, no total, a Groq reabrir
_envios: deque[float] = deque()  # instantes das requisições do último minuto

# ── cota DIÁRIA ──────────────────────────────────────────────────────────────
# 2000 requisições por dia POR MODELO (medido na VM em 27/09/2026: turbo com 644
# restantes, large-v3 com 1999). Em 26/09 a conversa da call gastou o dia do
# turbo em ~4h e a escuta ficou surda: 86 comandos perdidos em 22min, cada um
# um 429. O balde enche aos poucos (limite/24h: uma requisição a cada 43s), então
# o que resta é estimado a partir do último cabeçalho visto.
_MODELOS = ("whisper-large-v3-turbo", "whisper-large-v3")
_RESERVA_DO_DIA = 10  # abaixo disto, passa para o próximo modelo antes do 429
_AVISO_SEM_COTA_S = 600.0
_cota: dict[str, tuple[float, float, float]] = {}  # modelo → (restantes, limite, visto em)
_aviso_sem_cota_em = float("-inf")


class SemCotaDoDia(Exception):
    """Nenhum modelo da Groq tem requisições sobrando hoje."""


class _CotaDoModeloAcabou(Exception):
    pass


class _MinutoDoModeloAcabou(Exception):
    """429 do minuto num modelo, com outro modelo livre para levar o trecho."""


def _anota_cota(modelo: str, headers) -> float | None:
    """Guarda o que a Groq disse que resta do dia; devolve os restantes."""
    try:
        restantes = float(headers.get("x-ratelimit-remaining-requests"))
        limite = float(headers.get("x-ratelimit-limit-requests"))
    except (TypeError, ValueError):
        return None
    _cota[modelo] = (restantes, limite, _agora())
    return restantes


def _cota_estimada(modelo: str) -> float:
    if modelo not in _cota:
        return float("inf")
    restantes, limite, visto_em = _cota[modelo]
    return restantes + (_agora() - visto_em) * limite / 86400.0


# ── teto do MINUTO, também por modelo ────────────────────────────────────────
# A tabela de limites da Groq dá 20 RPM a CADA modelo de Whisper. Em 01 e 03/10,
# com 5 pessoas na call, o turbo tomou 429 com 17–44 requisições no minuto e 38
# trechos se perderam esperando por ele — com o large-v3 parado e o dia folgado.
_envios_por_modelo: dict[str, deque[float]] = {}
_minuto_cheio_ate: dict[str, float] = {}  # modelo → até quando o 429 do minuto vale


def _no_minuto_do_modelo(modelo: str) -> int:
    envios = _envios_por_modelo.get(modelo)
    if not envios:
        return 0
    limite = _agora() - 60.0
    while envios and envios[0] < limite:
        envios.popleft()
    return len(envios)


def _minuto_tem_folga(modelo: str) -> bool:
    if _agora() < _minuto_cheio_ate.get(modelo, float("-inf")):
        return False
    rpm = config.GROQ_STT_RPM
    return rpm <= 0 or _no_minuto_do_modelo(modelo) < rpm


def _modelos_do_dia(evitar: list[str] = ()) -> list[str]:
    return [m for m in _MODELOS if m not in evitar and _cota_estimada(m) > _RESERVA_DO_DIA]


def _modelo_com_cota(evitar: list[str], so_com_folga: bool = False) -> str | None:
    """O primeiro modelo com cota no dia E folga no minuto; sem folga em nenhum,
    o primeiro com cota no dia (aí o 429 do minuto espera, como sempre)."""
    candidatos = _modelos_do_dia(evitar)
    for modelo in candidatos:
        if _minuto_tem_folga(modelo):
            return modelo
    if so_com_folga or not candidatos:
        return None
    return candidatos[0]


# contadores para o retrato de saúde (services/observabilidade): zeram na leitura
_estatisticas = {"ok": 0, "429": 0, "transbordos": 0, "falhas": 0, "perdidos": 0, "poupados": 0}
_aviso_de_limite_em = float("-inf")


def leu_estatisticas() -> dict[str, int]:
    """Contadores de STT desde a última leitura (e zera)."""
    lido = dict(_estatisticas)
    for chave in _estatisticas:
        _estatisticas[chave] = 0
    return lido


def _conta_envio(modelo: str | None = None) -> None:
    _envios.append(_agora())
    if modelo is not None:
        _envios_por_modelo.setdefault(modelo, deque()).append(_agora())


def _no_ultimo_minuto() -> int:
    limite = _agora() - 60.0
    while _envios and _envios[0] < limite:
        _envios.popleft()
    return len(_envios)


def _rpm_somado() -> int:
    return config.GROQ_STT_RPM * max(1, len(_modelos_do_dia()))


def _teto_de_duracao() -> tuple[float | None, int]:
    """(duração máxima que ainda vale enviar, requisições no último minuto).

    O teto é a soma dos modelos que ainda têm o dia: cada um tem o seu minuto."""
    usados, rpm = _no_ultimo_minuto(), _rpm_somado()
    if rpm <= 0:
        return None, usados
    if usados >= rpm - 1:
        return _COMANDO_CURTO_S, usados
    if usados >= rpm * 0.75:
        return _COMANDO_LONGO_S, usados
    return None, usados


def _limites_do_429(headers) -> str:
    """Os cabeçalhos de limite da Groq: dizem QUAL teto estourou (por minuto,
    por dia ou segundos de áudio por hora) e quando reabre."""
    achados = []
    for nome, valor in headers.items():
        baixo = str(nome).lower()
        if baixo == "retry-after" or baixo.startswith("x-ratelimit-"):
            achados.append(f"{baixo}={valor}")
    return " ".join(sorted(achados)) or "sem cabeçalhos de limite"


def _get_model():
    global _model
    if _model is None:
        with _model_lock:  # chamado de threads do executor: sem lock, double-load
            if _model is None:
                from faster_whisper import WhisperModel

                _model = WhisperModel(
                    config.WHISPER_MODEL, device="cpu", compute_type="int8"
                )
    return _model


def preload() -> None:
    """Carrega o modelo E faz uma inferência de aquecimento — a primeira
    transcrição real custa caro (alocação/threads); melhor pagar no boot."""
    _get_model()
    _transcribe_sync(np.zeros(8000, dtype=np.float32))


def model_ready() -> bool:
    """O Whisper local já está carregado? (carregar leva ~46s na VM)"""
    return _model is not None


def _ensure_preload_started() -> None:
    """Começa a carregar o modelo local em BACKGROUND, no máximo uma vez."""
    global _preload_task
    if _model is not None:
        return
    if _preload_task is not None and not _preload_task.done():
        return
    log.info("🎙️ carregando o Whisper local em background (~46s na VM)")
    _preload_task = asyncio.get_running_loop().create_task(
        asyncio.to_thread(preload)
    )


def pcm_to_float_mono_16k(pcm: bytes) -> np.ndarray:
    """PCM 48kHz stereo s16le (formato do Discord) → float32 mono 16kHz.

    48k→16k via média de blocos de 3 amostras (filtro-caixa + decimação):
    decimação crua ([::3]) gera aliasing que atrapalha o Whisper.
    """
    arr = np.frombuffer(pcm, dtype=np.int16)
    arr = arr[: len(arr) - (len(arr) % 2)].reshape(-1, 2).mean(axis=1)
    arr = arr[: len(arr) - (len(arr) % 3)].reshape(-1, 3).mean(axis=1)
    return (arr / 32768.0).astype(np.float32)


def _transcribe_sync(audio: np.ndarray) -> str:
    # sem vad_filter (nosso gate de RMS já corta silêncio) e sem
    # initial_prompt (o Whisper alucina o prompt de volta sobre ruído)
    segments, _info = _get_model().transcribe(
        audio, language="pt", beam_size=1, condition_on_previous_text=False
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def _to_wav_bytes(audio: np.ndarray) -> bytes:
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(pcm16.tobytes())
    return buffer.getvalue()


def _retry_delay(status: int, retry_after: str | None) -> float | None:
    """Atraso antes de retentar o Groq, ou None se o status não merece retry.

    Cap de 2s: segmentos além de STALE_SEGMENT_S são descartados no worker —
    esperar mais só produziria transcrição de fala velha.
    """
    if status not in (429, 500, 502, 503):
        return None
    try:
        return min(float(retry_after or 1.0), 2.0)
    except ValueError:
        return 1.0


def _segundos_do_retry(retry_after: str | None) -> float:
    try:
        return max(1.0, float(retry_after or 1.0))
    except ValueError:
        return 1.0


def _espera_do_429(retry_after: str | None, paciente: bool, ja_esperou: float) -> float | None:
    """Quanto esperar depois de um 429, ou None para desistir.

    O teto da Groq é por MINUTO e o `retry-after` diz quanto falta. Trecho com
    cara de comando (curto) espera o que ela pedir, até _ESPERA_MAX_429_S no
    total: comando atrasado é melhor que comando perdido (foram 51 em cinco
    dias). Trecho longo desiste já — segurar um dos 3 workers da escuta por 6s
    em fala que quase nunca é comando derruba da fila o comando de verdade. E
    um `retry-after` maior que o orçamento é teto diário/de áudio: esperar 6s
    não resolve.
    """
    if not paciente:
        return None
    try:
        pedido = max(0.1, float(retry_after or 1.0))
    except ValueError:
        pedido = 1.0
    if ja_esperou + pedido > _ESPERA_MAX_429_S:
        return None
    return pedido


async def _groq_com_contexto(audio: np.ndarray, contexto: str) -> str:
    """A transcrição com os títulos da fila no prompt — sem nunca perder o comando
    por causa deles: 400 com contexto = repete sem, e o contexto fica de castigo."""
    global _sem_contexto_ate
    if not contexto or _agora() < _sem_contexto_ate:
        return await _transcribe_groq(audio)
    try:
        return await _transcribe_groq(audio, contexto)
    except Exception as exc:  # noqa: BLE001 — só o 400 é assunto daqui
        if getattr(exc, "status", None) != 400:
            raise
        _sem_contexto_ate = _agora() + _CASTIGO_DO_CONTEXTO_S
        log.warning(
            "🎙️ a Groq recusou o prompt com os títulos da fila (400: passou do teto de"
            " tokens) — transcrevendo sem eles pelos próximos %.0fmin",
            _CASTIGO_DO_CONTEXTO_S / 60,
        )
        return await _transcribe_groq(audio)


async def _transcribe_groq(audio: np.ndarray, contexto: str = "", prompt: str | None = None) -> str:
    """O trecho no primeiro modelo com cota no dia; o dia de um acabou, tenta o outro.

    `prompt`: no lugar do VOICE_STT_PROMPT (a 2ª escuta tira o nome dele)."""
    wav = await asyncio.to_thread(_to_wav_bytes, audio)
    tentados: list[str] = []
    while True:
        modelo = _modelo_com_cota(tentados)
        if modelo is None:
            raise SemCotaDoDia()
        # num 429 do minuto, só vale trocar se outro modelo tem folga AGORA
        transborda = _modelo_com_cota([*tentados, modelo], so_com_folga=True) is not None
        try:
            return await _transcribe_no_modelo(audio, wav, contexto, modelo, prompt, transborda)
        except _CotaDoModeloAcabou:
            tentados.append(modelo)
        except _MinutoDoModeloAcabou:
            tentados.append(modelo)
            _estatisticas["transbordos"] += 1


def _prompt_sem_nome() -> str:
    """O VOICE_STT_PROMPT sem o nome do bot (WAKE_WORD): o prompt da 2ª escuta."""
    return re.sub(rf"(?i)\b{re.escape(WAKE_WORD)}\b[,.]?\s*", "", config.VOICE_STT_PROMPT).strip()


async def confere_chamada(pcm: bytes) -> str | None:
    """Segunda escuta de um trecho que a primeira leu como chamada ao bot.

    O prompt abre com o nome do bot e o Whisper o usa como texto anterior:
    abre a transcrição com o nome sobre qualquer coisa — "É isso." vira
    "Arrodes.", e conversa ou música vazando no microfone viram "comando".
    Aqui o mesmo áudio vai com o vocabulário, mas SEM o nome.

    None = não deu para conferir (sem Groq, sem cota, falha): quem chamou
    decide — e decide atender, que comando perdido é pior.
    """
    if not config.GROQ_API_KEY:
        return None
    audio = await asyncio.to_thread(pcm_to_float_mono_16k, pcm)
    try:
        return await _transcribe_groq(audio, prompt=_prompt_sem_nome())
    except Exception as exc:  # noqa: BLE001 — a conferência nunca derruba o comando
        log.info("🎙️ não deu para conferir a chamada (%s) — vale a 1ª escuta", type(exc).__name__)
        return None


async def _transcribe_no_modelo(
    audio: np.ndarray, wav: bytes, contexto: str, modelo: str, prompt: str | None = None,
    transborda: bool = False,
) -> str:
    import aiohttp  # dependência do py-cord, já instalada

    paciente = len(audio) / 16000 <= _COMANDO_CURTO_S
    ja_esperou = 0.0  # em 429, somado
    tentativas_5xx = 0
    delay: float | None = None
    while True:
        if delay is not None:
            await asyncio.sleep(delay)  # fora do semáforo: os outros seguem
        # FormData não é reutilizável entre requests: monta a cada tentativa
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", modelo)
        vocabulario = config.VOICE_STT_PROMPT if prompt is None else prompt
        if vocabulario:
            # bias de vocabulário: inclina o Whisper a transcrever o nome do bot
            # e os comandos certos (o aviso de alucinação lá embaixo é sobre o
            # initial_prompt do whisper LOCAL tiny — o large-v3 lida bem)
            # `contexto`: os títulos da fila. Título em inglês dito por brasileiro
            # sai aportuguesado ("pirâmides"); com o nome no prompt sai "Pyramids"
            form.add_field("prompt", f"{vocabulario} {contexto}".strip())
        # sem "language": o large-v3 detecta sozinho e lida bem com pt+en misturado
        async with _groq_sem:
            _conta_envio(modelo)  # cada tentativa gasta do minuto, inclusive o retry
            async with http.get_session().post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                retry_after = resp.headers.get("Retry-After")
                restantes = _anota_cota(modelo, resp.headers)
                if resp.status == 429:
                    _avisa_limite(resp.headers)
                    if restantes is not None and restantes <= 0:
                        raise _CotaDoModeloAcabou()  # o dia; a espera do minuto não resolve
                    _minuto_cheio_ate[modelo] = _agora() + _segundos_do_retry(retry_after)
                    if transborda:
                        raise _MinutoDoModeloAcabou()  # o outro modelo leva, sem esperar
                    delay = _espera_do_429(retry_after, paciente, ja_esperou)
                    ja_esperou += delay or 0.0
                else:
                    # 5xx: soluço da Groq, some em 1–2s — as 3 tentativas curtas
                    delay = _retry_delay(resp.status, retry_after)
                    tentativas_5xx += 1
                    if tentativas_5xx >= _GROQ_TENTATIVAS:
                        delay = None
                if delay is None:
                    resp.raise_for_status()
                    data = await resp.json()
                    return (data.get("text") or "").strip()


def _avisa_limite(headers) -> None:
    """429 da Groq: conta sempre, explica no log no máximo uma vez por minuto."""
    global _aviso_de_limite_em
    _estatisticas["429"] += 1
    if _agora() - _aviso_de_limite_em < 60.0:
        return
    _aviso_de_limite_em = _agora()
    log.warning(
        "🎙️ Groq devolveu 429 com %d requisições no último minuto — %s",
        _no_ultimo_minuto(), _limites_do_429(headers),
    )


def _avisa_sem_cota() -> None:
    """Sem cota em nenhum modelo: nenhuma requisição sai, e o log explica uma vez."""
    global _aviso_sem_cota_em
    if _agora() - _aviso_sem_cota_em < _AVISO_SEM_COTA_S:
        return
    _aviso_sem_cota_em = _agora()
    estimativas = ", ".join(f"{m} ~{_cota_estimada(m):.0f}" for m in _MODELOS)
    log.warning(
        "🎙️ a cota do dia da Groq acabou em todos os modelos (%s restantes) — a escuta"
        " fica surda e nada é enviado até o balde encher (uma requisição a cada ~43s"
        " por modelo)", estimativas,
    )


async def _transcribe_local(audio: np.ndarray) -> str:
    """Whisper local, cronometrado: se for lento demais aqui, é a última vez."""
    global _local_lento
    loop = asyncio.get_running_loop()
    async with _local_sem:  # N guilds caindo para o local viram fila, não stampede
        comeco = _agora()
        texto = await loop.run_in_executor(
            None, functools.partial(_transcribe_sync, audio)
        )
        gasto = _agora() - comeco
    if config.GROQ_API_KEY and not _local_lento and gasto > _LOCAL_LENTO_S:
        _local_lento = True
        _solta_o_modelo()
        log.warning(
            "🎙️ o Whisper local levou %.0fs para %.1fs de fala nesta máquina —"
            " desligando o fallback local (ele satura a CPU e corta a música)."
            " Os comandos de voz passam a depender só do Groq.",
            gasto, len(audio) / 16000,
        )
    return texto


def _solta_o_modelo() -> None:
    """Devolve a RAM do Whisper local: medido lento, não é mais usado nesta
    execução — e são ~150 MB numa VM de 954 MB que já vive com swap."""
    global _model
    with _model_lock:
        _model = None
    gc.collect()


async def transcribe(pcm: bytes, contexto: str = "") -> str:
    """Transcreve um trecho de fala (bytes PCM do Discord) para texto.

    `contexto`: vocabulário da hora (os títulos da fila) para o Whisper da Groq.
    """
    # numpy sobre megabytes: fora do event loop
    audio = await asyncio.to_thread(pcm_to_float_mono_16k, pcm)
    if len(audio) < 1600:  # < 0.1s: ruído
        return ""
    if config.GROQ_API_KEY:
        duracao = len(audio) / 16000
        if 0 < config.VOICE_TETO_S < duracao:
            # fixo, não só com o minuto apertado: nenhum dos 80 comandos
            # medidos passou de 5,6s, e o que passava de 6s eram 13% das
            # requisições e 37% do áudio da call mandado a terceiros
            _estatisticas["poupados"] += 1
            log.info(
                "🎙️ trecho de %.1fs não foi para a Groq: acima do teto de %.0fs"
                " (nenhum comando medido passou de 5,6s)",
                duracao, config.VOICE_TETO_S,
            )
            return ""
        teto, usados = _teto_de_duracao()
        if teto is not None and duracao > teto:
            _estatisticas["poupados"] += 1
            log.info(
                "🎙️ trecho de %.1fs não foi para a Groq: %d de %d requisições do"
                " minuto já gastas — guardando o resto para o que tem tamanho de"
                " comando (até %.0fs)",
                duracao, usados, _rpm_somado(), teto,
            )
            return ""
        try:
            texto = await _groq_com_contexto(audio, contexto)
            _estatisticas["ok"] += 1
            return texto
        except Exception as exc:  # noqa: BLE001 — nuvem fora: cai para o local
            _estatisticas["falhas"] += 1
            if isinstance(exc, SemCotaDoDia) and not config.VOICE_WHISPER_LOCAL:
                _estatisticas["perdidos"] += 1
                _avisa_sem_cota()
                return ""
            if not config.VOICE_WHISPER_LOCAL:
                _estatisticas["perdidos"] += 1
                log.warning(
                    "🎙️ Groq falhou (%s) e o fallback local está desligado"
                    " (VOICE_WHISPER_LOCAL=0) — comando perdido",
                    type(exc).__name__,
                )
                return ""
            if _local_lento:
                _estatisticas["perdidos"] += 1
                # já medimos: aqui o local custa mais que o comando vale
                log.warning(
                    "🎙️ Groq falhou (%s) e o Whisper local é lento demais nesta"
                    " máquina — comando perdido, mas a música segue inteira",
                    type(exc).__name__,
                )
                return ""
            if not model_ready():
                # carregar o modelo AQUI custaria ~46s na VM, saturando as 2
                # vCPUs (engasga a música) e segurando _local_sem — e o
                # trecho seria descartado por STALE_SEGMENT_S no fim. Melhor
                # perder ESTE comando e deixar o modelo pronto para o próximo.
                _ensure_preload_started()
                _estatisticas["perdidos"] += 1
                log.warning(
                    "🎙️ Groq falhou (%s) e o Whisper local ainda está frio —"
                    " comando perdido; carregando o local para os próximos",
                    exc,
                )
                return ""
            # %s e não %r: o repr da ClientResponseError inclui os headers,
            # ou seja, grava a chave da Groq em texto puro no journal
            log.warning(
                "🎙️ Groq falhou (%s: %s) — usando Whisper local",
                type(exc).__name__, exc,
            )
    return await _transcribe_local(audio)
