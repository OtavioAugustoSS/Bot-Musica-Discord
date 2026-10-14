"""Buffer de áudio: o que impede a música de "cortar pedaços".

Diagnóstico de 16/09 (logs de produção). Entre o FFmpeg e o Discord não há
buffer nenhum: o único pulmão é o pipe do sistema operacional, 64 KB, que
são 0,33s de PCM. E o laço do py-cord (`AudioPlayer._do_run`) faz:

    data = self.source.read()          # BLOQUEIA se o ffmpeg não produziu
    play_audio(data)
    delay = max(0, DELAY + (next_time - now))   # atrasou? delay = 0

Ou seja, qualquer pausa maior que 0,33s no fornecimento de PCM trava a
thread e, quando os dados voltam, ela dispara uma rajada de pacotes
atrasados para "recuperar" — rajada que o jitter buffer do cliente do
Discord descarta. O pedaço da música some. Duas causas de pausa, ambas
medidas no journal:

  - o CDN do googlevideo derruba a conexão a cada ~31s (7 a 20 vezes por
    hora de uso); cada reconexão é um TLS handshake novo;
  - a CPU satura: extração do yt-dlp com mediana de 12,7s e até 48,3s, e
    transcrição no Whisper local de 83 a 131 segundos — tudo num núcleo só.

Com alguns segundos de PCM adiantados na memória, nada disso chega ao
ouvinte: o FFmpeg passa a baixar à frente (o que também evita a
estagnação do TCP que provoca as quedas) e picos de CPU são absorvidos.
Quando mesmo assim falta áudio, devolvemos silêncio no ritmo certo em vez
de travar — silêncio curto não perde conteúdo, rajada perde.
"""
import threading
import time

import discord
from discord.opus import Encoder

from core.audio import BufferedAudioSource

FRAME = Encoder.FRAME_SIZE  # 3840 bytes = 20ms
SILENCIO = b"\x00" * FRAME


class FonteFake(discord.AudioSource):
    """Fonte com N frames; pode travar por um tempo no meio."""

    def __init__(self, frames: int = 50, trava_em: int | None = None, trava_s: float = 0.0):
        self.frames = frames
        self.trava_em = trava_em
        self.trava_s = trava_s
        self.lidos = 0
        self.limpou = False

    def read(self) -> bytes:
        if self.trava_em is not None and self.lidos == self.trava_em:
            self.trava_em = None
            time.sleep(self.trava_s)
        if self.lidos >= self.frames:
            return b""
        self.lidos += 1
        return bytes([self.lidos % 256]) * FRAME

    def cleanup(self) -> None:
        self.limpou = True

    def is_opus(self) -> bool:
        return False


class FonteTravada(discord.AudioSource):
    """Nunca entrega nada (CDN pendurado) até ser liberada."""

    def __init__(self):
        self.liberar = threading.Event()
        self.limpou = False

    def read(self) -> bytes:
        self.liberar.wait(timeout=5)
        return b"" if not self.liberar.is_set() else b"\x01" * FRAME

    def cleanup(self) -> None:
        self.limpou = True

    def is_opus(self) -> bool:
        return False


class FonteControlada(discord.AudioSource):
    """Entrega livremente até alguém travar; destravar faz voltar."""

    def __init__(self):
        self._livre = threading.Event()
        self._livre.set()
        self.lidos = 0

    def read(self) -> bytes:
        self._livre.wait(timeout=10)
        self.lidos += 1
        return bytes([(self.lidos % 255) + 1]) * FRAME

    def trava(self) -> None:
        self._livre.clear()

    def destrava(self) -> None:
        self._livre.set()

    def cleanup(self) -> None:
        pass

    def is_opus(self) -> bool:
        return False


class FonteQueEngrenaDevagar(discord.AudioSource):
    """Demora para abrir E hesita logo depois do primeiro frame.

    É o comportamento medido do FFmpeg com o stream do YouTube na VM: 2,4s
    para entregar o primeiro frame (mais que o teto do pré-buffer) e só
    então engrena nos 15x tempo real.
    """

    def __init__(self, abertura: float = 1.5, hesitacao: float = 0.3):
        self.abertura = abertura
        self.hesitacao = hesitacao
        self.lidos = 0

    def read(self) -> bytes:
        if self.lidos == 0:
            time.sleep(self.abertura)
        elif self.lidos == 1:
            time.sleep(self.hesitacao)
        self.lidos += 1
        return bytes([(self.lidos % 255) + 1]) * FRAME

    def cleanup(self) -> None:
        pass

    def is_opus(self) -> bool:
        return False


def _drena(buffer: BufferedAudioSource, quantos: int) -> list[bytes]:
    return [buffer.read() for _ in range(quantos)]


# ── entrega correta ──────────────────────────────────────────────────────────


def test_entrega_os_frames_na_ordem_e_termina_no_fim():
    fonte = FonteFake(frames=10)
    buffer = BufferedAudioSource(fonte, segundos=1.0)
    try:
        frames = _drena(buffer, 11)
    finally:
        buffer.cleanup()

    assert frames[:10] == [bytes([i % 256]) * FRAME for i in range(1, 11)]
    assert frames[10] == b""  # fonte acabou


def test_cleanup_para_a_thread_e_limpa_o_original():
    fonte = FonteFake(frames=1000)
    buffer = BufferedAudioSource(fonte, segundos=0.5)
    buffer.read()
    buffer.cleanup()

    assert fonte.limpou is True
    assert buffer._thread.is_alive() is False


def test_nao_le_a_fonte_inteira_de_uma_vez():
    """Teto de memória: 4 min de PCM são 46 MB e a VM tem 954 MB."""
    fonte = FonteFake(frames=10_000)
    buffer = BufferedAudioSource(fonte, segundos=1.0)  # ~50 frames
    try:
        time.sleep(0.3)
        lidos_sem_consumir = fonte.lidos
    finally:
        buffer.cleanup()

    assert lidos_sem_consumir <= 60, lidos_sem_consumir


# ── o que o buffer existe para resolver ──────────────────────────────────────


def _espera(condicao, timeout: float = 5.0) -> None:
    fim = time.monotonic() + timeout
    while not condicao() and time.monotonic() < fim:
        time.sleep(0.01)


def test_fonte_que_trava_2s_no_meio_nao_interrompe_a_entrega():
    """O caso real: reconexão do CDN no meio da música."""
    fonte = FonteFake(frames=1000, trava_em=200, trava_s=2.0)
    buffer = BufferedAudioSource(fonte, segundos=8.0)
    try:
        _espera(lambda: fonte.lidos >= 200)  # 4s adiantados, e a fonte travou
        inicio = time.monotonic()
        frames = _drena(buffer, 30)  # 600ms de áudio
        gasto = time.monotonic() - inicio
    finally:
        buffer.cleanup()

    assert all(f != b"" for f in frames), "faixa não pode terminar"
    assert SILENCIO not in frames, "não pode faltar áudio: o buffer tinha folga"
    assert gasto < 0.5, f"read() não pode bloquear: levou {gasto:.2f}s"


def test_sem_audio_devolve_silencio_no_ritmo_em_vez_de_travar():
    """Sem buffer, o py-cord travava e depois disparava uma rajada que o
    cliente descarta. Silêncio no ritmo certo não perde conteúdo.

    Vale DEPOIS que o pulmão encheu: no começo da faixa, esperar é o certo."""
    fonte = FonteControlada()
    buffer = BufferedAudioSource(fonte, segundos=1.0)
    try:
        _espera(lambda: buffer._fila.qsize() >= 30)
        buffer.read()  # pulmão cheio: a política de silêncio passa a valer
        fonte.trava()
        _drena(buffer, 60)  # esvazia a folga
        inicio = time.monotonic()
        frame = buffer.read()
        gasto = time.monotonic() - inicio
    finally:
        fonte.destrava()
        buffer.cleanup()

    assert frame == SILENCIO
    assert gasto < 1.5, f"read() demorou {gasto:.2f}s — deveria devolver silêncio"


def test_silencio_longo_demais_encerra_a_faixa():
    """Se o CDN pendurou de vez, não dá para tocar silêncio para sempre."""
    fonte = FonteControlada()
    buffer = BufferedAudioSource(fonte, segundos=1.0, silencio_max_s=0.2)
    try:
        _espera(lambda: buffer._fila.qsize() >= 30)
        buffer.read()
        fonte.trava()
        frames = _drena(buffer, 150)
    finally:
        fonte.destrava()
        buffer.cleanup()

    assert b"" in frames, "deveria ter encerrado"
    assert SILENCIO in frames[: frames.index(b"")], "silêncio antes de desistir"


def test_volta_a_tocar_quando_o_audio_retorna():
    """Silêncio é uma ponte, não um fim: a música continua de onde parou."""
    fonte = FonteControlada()
    buffer = BufferedAudioSource(fonte, segundos=1.0)  # 50 frames de folga
    try:
        assert buffer.read() != SILENCIO  # a faixa começou
        fonte.trava()
        durante = _drena(buffer, 80)  # esvazia a folga e passa a faltar áudio
        fonte.destrava()
        depois = _drena(buffer, 20)
    finally:
        buffer.cleanup()

    assert SILENCIO in durante, "deveria ter tapado o buraco com silêncio"
    assert depois[-1] != SILENCIO, "deveria ter voltado a tocar"


# ── observabilidade: sem isso não dá para provar que melhorou ────────────────


def test_conta_os_frames_de_silencio_para_o_log():
    fonte = FonteControlada()
    buffer = BufferedAudioSource(fonte, segundos=1.0)
    try:
        _espera(lambda: buffer._fila.qsize() >= 30)  # deixa encher
        buffer.read()
        fonte.trava()
        _drena(buffer, 80)
    finally:
        fonte.destrava()
        buffer.cleanup()

    assert buffer.frames_sem_audio >= 5
    assert buffer.falhas_de_buffer >= 1  # sequências distintas de underrun


def test_engasgo_logo_apos_o_primeiro_frame_ainda_espera():
    """Medido na VM: o FFmpeg entrega o 1º frame e só então engrena. Se a
    política de silêncio valesse já no 2º frame, a faixa começava com um
    tapa-buraco de 100ms — foi o que aconteceu na primeira medição."""
    fonte = FonteQueEngrenaDevagar(abertura=1.5, hesitacao=0.3)
    buffer = BufferedAudioSource(fonte, segundos=4.0)
    try:
        primeiros = _drena(buffer, 40)
    finally:
        buffer.cleanup()

    assert SILENCIO not in primeiros
    assert buffer.frames_sem_audio == 0


def test_reproducao_limpa_nao_registra_falha():
    fonte = FonteFake(frames=30)
    buffer = BufferedAudioSource(fonte, segundos=8.0)
    try:
        time.sleep(0.5)
        _drena(buffer, 20)
    finally:
        buffer.cleanup()

    assert buffer.frames_sem_audio == 0
    assert buffer.falhas_de_buffer == 0


# ── integração com o player ──────────────────────────────────────────────────


def _player_fake():
    from unittest.mock import MagicMock

    from apoio import faz_player

    from core.player import GuildPlayer

    p = faz_player(guild=MagicMock())
    p.guild.id = 1
    p.guild.voice_client = MagicMock()
    p.skip_votes = set()
    p._stopping = True  # não agenda o play_next no after
    p._source = None
    return p


def test_player_toca_atraves_do_buffer(monkeypatch):
    import asyncio

    import core.player as player_mod

    p = _player_fake()
    monkeypatch.setattr(player_mod.discord, "FFmpegPCMAudio", lambda *a, **k: FonteFake(3))

    asyncio.run(p._build_and_play({"stream_url": "http://x"}))

    tocada = p.guild.voice_client.play.call_args.args[0]
    # por fora a FonteDaFaixa (o que o py-cord vê); o pulmão é a fonte de dentro
    assert isinstance(tocada._fonte, BufferedAudioSource)
    tocada.cleanup()


def test_fim_da_faixa_registra_falha_de_buffer(caplog):
    import logging

    p = _player_fake()
    fonte = FonteControlada()
    p._source = BufferedAudioSource(fonte, segundos=1.0)
    try:
        _espera(lambda: p._source._fila.qsize() >= 30)
        p._source.read()
        fonte.trava()
        _drena(p._source, 60)
        with caplog.at_level(logging.WARNING, logger="core.player"):
            p._after_track(None)
    finally:
        fonte.destrava()
        p._source.cleanup()

    assert "faltou áudio" in caplog.text


def test_fim_de_faixa_limpa_nao_polui_o_log(caplog):
    import logging

    p = _player_fake()
    p._source = BufferedAudioSource(FonteFake(5), segundos=1.0)
    try:
        _drena(p._source, 3)
        with caplog.at_level(logging.WARNING, logger="core.player"):
            p._after_track(None)
    finally:
        p._source.cleanup()

    assert "faltou áudio" not in caplog.text


# ── início da faixa: esperar é certo, silêncio é errado ──────────────────────
# Medido na VM (16/09): o FFmpeg leva ~1,3s para abrir o stream do YouTube.
# Com o pré-buffer desistindo em 1s, os primeiros reads pegavam fila vazia e
# a faixa COMEÇAVA com 1,5s de silêncio. Antes de o primeiro frame sair, o
# certo é bloquear — como o py-cord já fazia.


class FonteLentaParaAbrir(discord.AudioSource):
    """Demora `atraso` para o primeiro frame, depois entrega normalmente."""

    def __init__(self, atraso: float):
        self.atraso = atraso
        self.lidos = 0

    def read(self) -> bytes:
        if self.lidos == 0:
            time.sleep(self.atraso)
        self.lidos += 1
        return b"\x02" * FRAME

    def cleanup(self) -> None:
        pass

    def is_opus(self) -> bool:
        return False


def test_faixa_nao_comeca_com_silencio_quando_o_ffmpeg_demora_a_abrir():
    fonte = FonteLentaParaAbrir(atraso=1.8)
    buffer = BufferedAudioSource(fonte, segundos=8.0)
    try:
        primeiros = _drena(buffer, 5)
    finally:
        buffer.cleanup()

    assert SILENCIO not in primeiros, "o começo da faixa não pode ser silêncio"
    assert all(f == b"\x02" * FRAME for f in primeiros)


class FonteMortaNaLargada(discord.AudioSource):
    """O FFmpeg que levou 403 ao abrir a URL: EOF no primeiro read()."""

    def read(self) -> bytes:
        return b""

    def cleanup(self) -> None:
        pass

    def is_opus(self) -> bool:
        return False


def test_fonte_que_morre_na_largada_nao_segura_o_player_por_15s():
    """20/09, produção: o FFmpeg levava 403 do YouTube e saía na hora, mas o
    read() esperava os 15s inteiros do início de faixa ANTES de olhar se a
    fonte já tinha acabado — 15s de call muda por faixa, sem uma linha no log."""
    buffer = BufferedAudioSource(FonteMortaNaLargada(), segundos=8.0)
    try:
        inicio = time.monotonic()
        frame = buffer.read()
        gasto = time.monotonic() - inicio
    finally:
        buffer.cleanup()

    assert frame == b"", "fonte vazia é fim de faixa"
    assert gasto < 1.0, f"levou {gasto:.1f}s para perceber que a fonte morreu"


def test_conta_os_frames_de_audio_entregues():
    """É o que diz, no fim da faixa, quanto tocou de verdade (e se morreu cedo)."""
    buffer = BufferedAudioSource(FonteFake(frames=12), segundos=8.0)
    try:
        while buffer.read():
            pass
    finally:
        buffer.cleanup()

    assert buffer.frames_entregues == 12


def test_pular_na_largada_solta_a_thread_em_menos_de_100ms():
    """`stop_playing()` só marca o fim no AudioPlayer; a thread dele continua
    presa no read() do pulmão até vir o 1º frame (FFmpeg abrindo: até 15s) —
    e só então dispara o `after`, atrasado. `interrompe()` solta na hora."""
    fonte = FonteTravada()
    buffer = BufferedAudioSource(fonte, segundos=8.0)
    resultado = {}

    def le():
        inicio = time.monotonic()
        resultado["frame"] = buffer.read()
        resultado["gasto"] = time.monotonic() - inicio

    leitor = threading.Thread(target=le)
    leitor.start()
    time.sleep(0.3)  # o read() já está esperando o início da faixa
    pedido = time.monotonic()
    buffer.interrompe()
    leitor.join(timeout=2.0)
    solto_em = time.monotonic() - pedido
    fonte.liberar.set()
    buffer.cleanup()

    assert not leitor.is_alive(), "o read() continuou preso depois do interrompe()"
    assert resultado["frame"] == b""
    assert solto_em < 0.15, f"levou {solto_em:.2f}s para soltar a thread de áudio"
