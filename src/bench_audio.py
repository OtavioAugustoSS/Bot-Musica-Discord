"""Quanto custa, em CPU, tocar música por cada caminho de áudio — medido na VM.

    .venv/bin/python src/bench_audio.py --prepara      # baixa 60s de áudio real (1x)
    .venv/bin/python src/bench_audio.py                # 3 rodadas de P, C, R e K
    .venv/bin/python src/bench_audio.py --toc          # os pacotes do YouTube têm 20ms?

Por que existe: o bot decodifica o Opus do YouTube para PCM, aplica o volume em
numpy (Python, 50x por segundo) e o py-cord re-encoda tudo para Opus na thread
de tempo real — numa VM que engasga acima de ~20% de CPU. O py-cord aceita
Opus pronto (`encode=False`). Antes de reescrever o pipeline, o número.

Caminhos:
  P  o de hoje: FFmpeg→PCM → NumpyVolumeTransformer(0,8) → pulmão → encode libopus
  C  Opus COPIADO: FFmpeg `-c:a copy` → pacotes Ogg → pulmão → (nada)
  R  Opus RECODIFICADO no FFmpeg: `-af volume=0.8 -c:a libopus` → pacotes → pulmão
  K  só o que é comum a todos: cifrar o pacote (AEAD) + sendto UDP, a cada 20ms

Como não se enganar (lições de medições anteriores nesta VM): arquivo LOCAL
(URL re-extraída sai "quente", e os primeiros segundos de URL fresca dão 403);
nada de subprocess/print/vmstat dentro do laço (já criaram soluço falso de
60ms); rodadas intercaladas; o bot tem de estar ocioso; rodada com steal alto
é descartada pelo leitor, não escondida pelo script.
"""
from __future__ import annotations

import argparse
import array
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord  # noqa: E402
from discord.oggparse import OggStream  # noqa: E402

from core.audio import BufferedAudioSource, NumpyVolumeTransformer  # noqa: E402
from services import youtube  # noqa: E402

ARQUIVO = os.path.join("data", "bench-60s.webm")
VIDEO_DE_TESTE = "kJQP7kiw5Fk"  # público, sem restrição; só os 60s iniciais
QUADRO_S = 0.02
# configs do byte TOC do Opus cujos quadros têm 20ms (RFC 6716, tabela 2)
_CONFIGS_20MS = frozenset({1, 5, 9, 13, 15, 19, 23, 27, 31})
_CABECALHOS_OGG = (b"OpusHead", b"OpusTags")


def duracao_do_pacote_ms(pacote: bytes) -> float:
    """Duração de um pacote Opus pelo byte TOC. 0 = pacote vazio/inválido."""
    if not pacote:
        return 0.0
    toc = pacote[0]
    config, codigo = toc >> 3, toc & 3
    if config < 12:
        quadro = (10, 20, 40, 60)[config & 3]
    elif config < 16:
        quadro = (10, 20)[config & 1]
    else:
        quadro = (2.5, 5, 10, 20)[config & 3]
    if codigo == 0:
        quadros = 1
    elif codigo in (1, 2):
        quadros = 2
    else:
        quadros = pacote[1] & 0x3F if len(pacote) > 1 else 0
    return quadro * quadros


class FonteDePacotes(discord.AudioSource):
    """FFmpeg com saída Ogg/Opus → um pacote Opus por read() (sem os cabeçalhos)."""

    def __init__(self, args: list[str]) -> None:
        self.processo = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "warning", *args, "-f", "opus", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._pacotes = OggStream(self.processo.stdout).iter_packets()
        self.fora_de_20ms = 0

    def read(self) -> bytes:
        for pacote in self._pacotes:
            if pacote[:8] in _CABECALHOS_OGG:
                continue  # o OggStream entrega OpusHead/OpusTags como pacote comum
            if (pacote[0] >> 3) not in _CONFIGS_20MS or pacote[0] & 3:
                self.fora_de_20ms += 1
            return pacote
        return b""

    def is_opus(self) -> bool:
        return True

    def cleanup(self) -> None:
        if self.processo.poll() is None:
            self.processo.kill()
        self.processo.wait()


def _cpu() -> tuple[float, float]:
    """(CPU deste processo, CPU dos filhos JÁ COLHIDOS) em segundos."""
    try:
        import resource
    except ImportError:  # Windows: só o próprio processo
        return time.process_time(), 0.0
    eu = resource.getrusage(resource.RUSAGE_SELF)
    filhos = resource.getrusage(resource.RUSAGE_CHILDREN)
    return eu.ru_utime + eu.ru_stime, filhos.ru_utime + filhos.ru_stime


def _steal() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            valores = [int(v) for v in fh.readline().split()[1:9]]
    except (OSError, ValueError):
        return None
    return sum(valores), valores[7]


class Coletor:
    """O lado do py-cord: um quadro a cada 20ms, encode se for PCM, cifra e envia."""

    def __init__(self) -> None:
        import nacl.bindings as nb

        self._cifra = nb.crypto_aead_xchacha20poly1305_ietf_encrypt
        self._chave = bytes(32)
        self._soquete = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cabecalho = bytes(12)
        self.encoder = discord.opus.Encoder()

    def envia(self, opus: bytes, sequencia: int) -> None:
        nonce = sequencia.to_bytes(24, "little")
        self._soquete.sendto(
            self._cabecalho + self._cifra(opus, self._cabecalho, nonce, self._chave),
            ("127.0.0.1", 9),
        )

    def fecha(self) -> None:
        self._soquete.close()


def roda(caminho: str) -> dict:
    fonte_bruta = None
    if caminho == "P":
        fonte_bruta = discord.FFmpegPCMAudio(ARQUIVO, options="-vn")
        fonte = BufferedAudioSource(NumpyVolumeTransformer(fonte_bruta, volume=0.8), segundos=8.0)
    elif caminho == "C":
        fonte_bruta = FonteDePacotes(["-i", ARQUIVO, "-vn", "-c:a", "copy"])
        fonte = BufferedAudioSource(fonte_bruta, segundos=8.0)
    elif caminho == "R":
        fonte_bruta = FonteDePacotes([
            "-i", ARQUIVO, "-vn", "-af", "volume=0.8", "-c:a", "libopus",
            "-b:a", "128k", "-frame_duration", "20", "-ar", "48000", "-ac", "2",
        ])
        fonte = BufferedAudioSource(fonte_bruta, segundos=8.0)
    else:  # K: 60s de um pacote pronto, sem FFmpeg nem pulmão
        fonte = None
    coletor = Coletor()
    pacote_fixo = coletor.encoder.encode(bytes(3840), 960)
    atrasos = array.array("d", bytes(8 * 3100))
    cpu0, filhos0 = _cpu()
    steal0 = _steal()
    inicio = time.perf_counter()
    quadros = 0
    while quadros < 3100:
        if fonte is None:
            if quadros >= 3000:
                break
            dado = pacote_fixo
        else:
            dado = fonte.read()
            if not dado:
                break
            if caminho == "P":
                dado = coletor.encoder.encode(dado, 960)
        coletor.envia(dado, quadros)
        quadros += 1
        alvo = inicio + QUADRO_S * quadros
        falta = alvo - time.perf_counter()
        atrasos[quadros - 1] = -falta if falta < 0 else 0.0
        if falta > 0:
            time.sleep(falta)
    parede = time.perf_counter() - inicio
    silencio = getattr(fonte, "frames_sem_audio", 0)
    primeiro = getattr(fonte, "primeiro_audio_s", None)
    fora = getattr(fonte_bruta, "fora_de_20ms", 0)
    if fonte is not None:
        fonte.cleanup()  # mata/colhe o FFmpeg: só então ele entra em RUSAGE_CHILDREN
    coletor.fecha()
    cpu1, filhos1 = _cpu()
    steal1 = _steal()
    usados = sorted(atrasos[:quadros])
    audio_s = quadros * QUADRO_S
    return {
        "caminho": caminho,
        "audio_s": audio_s,
        "parede_s": parede,
        "python_ms_por_s": 1000 * (cpu1 - cpu0) / audio_s,
        "ffmpeg_ms_por_s": 1000 * (filhos1 - filhos0) / audio_s,
        "steal_pct": (
            100 * (steal1[1] - steal0[1]) / max(1, steal1[0] - steal0[0])
            if steal0 and steal1 else float("nan")
        ),
        "atraso_p99_ms": 1000 * usados[int(0.99 * (len(usados) - 1))] if usados else 0.0,
        "atraso_max_ms": 1000 * usados[-1] if usados else 0.0,
        "silencio": silencio,
        "primeiro_audio_s": primeiro,
        "fora_de_20ms": fora,
    }


def prepara() -> None:
    """60s de áudio real (itag 251) num arquivo local — uma vez só."""
    perfil = {p.name: p for p in youtube._ALL_PROFILES}["cookies"]
    info = youtube._get_ydl(perfil).extract_info(
        f"https://www.youtube.com/watch?v={VIDEO_DE_TESTE}", download=False
    )
    print(f"formato {info.get('format_id')} ({info.get('acodec')}, {info.get('asr')} Hz)")
    time.sleep(3)  # a URL fresca leva 403 no primeiro ~1s de vida
    os.makedirs(os.path.dirname(ARQUIVO), exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *youtube.FFMPEG_BEFORE.split()[1:],
         "-i", info["url"], "-t", "62", "-vn", "-c:a", "copy", ARQUIVO],
        check=True,
    )
    print(f"ok: {ARQUIVO} ({os.path.getsize(ARQUIVO) // 1024} KB)")


def levantamento_toc(ids: list[str]) -> None:
    """Os pacotes Opus do YouTube têm mesmo 20ms? (o py-cord avança 960 amostras
    por pacote sem olhar: 40/60ms tocariam em câmera lenta)"""
    perfil = {p.name: p for p in youtube._ALL_PROFILES}["cookies"]
    for vid in ids:
        info = youtube._get_ydl(perfil).extract_info(
            f"https://www.youtube.com/watch?v={vid}", download=False
        )
        for formato in info.get("formats") or []:
            if formato.get("acodec") != "opus" or formato.get("vcodec") not in (None, "none"):
                continue
            time.sleep(2)
            fonte = FonteDePacotes([*youtube.FFMPEG_BEFORE.split()[1:], "-i", formato["url"],
                                    "-t", "20", "-vn", "-c:a", "copy"])
            duracoes: dict[float, int] = {}
            while True:
                pacote = fonte.read()
                if not pacote:
                    break
                ms = duracao_do_pacote_ms(pacote)
                duracoes[ms] = duracoes.get(ms, 0) + 1
            fonte.cleanup()
            print(f"{vid} itag {formato.get('format_id')} {formato.get('audio_channels')}ch"
                  f" {formato.get('asr')}Hz: {dict(sorted(duracoes.items()))}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prepara", action="store_true")
    ap.add_argument("--toc", nargs="*", metavar="VIDEO_ID")
    ap.add_argument("--rodadas", type=int, default=3)
    ap.add_argument("--caminhos", default="PCRK")
    args = ap.parse_args()
    if args.prepara:
        prepara()
        return
    if args.toc is not None:
        levantamento_toc(args.toc or [VIDEO_DE_TESTE])
        return
    if not os.path.exists(ARQUIVO):
        sys.exit(f"falta {ARQUIVO} — rode com --prepara primeiro")
    ordem = list(args.caminhos)
    resultados = []
    for rodada in range(args.rodadas):
        for caminho in ordem[rodada % len(ordem):] + ordem[: rodada % len(ordem)]:
            r = roda(caminho)
            resultados.append(r)
            print(
                f"{r['caminho']}  python {r['python_ms_por_s']:6.1f}  ffmpeg {r['ffmpeg_ms_por_s']:6.1f}"
                f"  total {r['python_ms_por_s'] + r['ffmpeg_ms_por_s']:6.1f} ms/s"
                f" | steal {r['steal_pct']:4.1f}% | atraso p99 {r['atraso_p99_ms']:5.1f} máx {r['atraso_max_ms']:6.1f} ms"
                f" | silêncio {r['silencio']} | 1º áudio {r['primeiro_audio_s']}"
                f" | fora de 20ms {r['fora_de_20ms']} | {r['audio_s']:.0f}s",
                flush=True,
            )
    print("\nmediana do total (ms de CPU por segundo de áudio; 10 ms/s = 1% de um núcleo):")
    for caminho in ordem:
        totais = sorted(
            r["python_ms_por_s"] + r["ffmpeg_ms_por_s"] for r in resultados if r["caminho"] == caminho
        )
        if totais:
            print(f"  {caminho}: {totais[len(totais) // 2]:6.1f}   (rodadas: {[round(t, 1) for t in totais]})")


if __name__ == "__main__":
    main()
