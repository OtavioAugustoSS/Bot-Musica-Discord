"""Opus direto: o áudio do YouTube vai para o Discord sem decodificar nem recodificar.

Medido na VM em 20/09/2026 (src/bench_audio.py, 3 rodadas intercaladas sobre 60s
de áudio real, steal <= 2%), em ms de CPU por segundo de áudio:

    P  FFmpeg->PCM -> volume em numpy -> pulmão -> encode libopus   118,0
    C  FFmpeg `-c:a copy` -> pacotes Ogg -> pulmão                   17,0  (-86%)
    R  FFmpeg `-af volume -c:a libopus` -> pacotes -> pulmão         43,3  (-63%)
    K  só cifrar e enviar (comum a todos)                             7,1

Dos 118 ms do caminho de hoje, ~100 eram Python na thread de tempo real (numpy
do volume + encode Opus, 50x por segundo) — numa VM que engasga acima de ~20% de
CPU. O YouTube já entrega Opus 48 kHz estéreo em pacotes de 20ms (itag 251), que
é exatamente o que o Discord quer.

O que pode dar errado, e está testado aqui: os cabeçalhos do Ogg (OpusHead,
OpusTags) chegam do OggStream do py-cord como pacotes comuns; o py-cord avança
20ms no relógio RTP por pacote sem olhar o conteúdo (pacote de 40ms tocaria em
câmera lenta); e matar o FFmpeg num skip corta uma página Ogg ao meio.
"""
import io
import logging
import struct
import time

import discord
import pytest

from core import audio
from core.audio import OPUS_SILENCIO, BufferedAudioSource, FonteOpus, PacotesProntos

TOC_20MS = 0xF8  # CELT fullband, 20ms, um quadro — o que o YouTube manda
TOC_40MS = 0xF9  # o mesmo com DOIS quadros no pacote
TOC_10MS = 0xF0


def _pacote(toc: int = TOC_20MS, tamanho: int = 160, marca: int = 0) -> bytes:
    return bytes([toc, marca % 256]) + bytes(tamanho - 2)


def _pagina(pacotes: list[bytes], seq: int = 0) -> bytes:
    tabela = b""
    for p in pacotes:
        tabela += b"\xff" * (len(p) // 255) + bytes([len(p) % 255])
    return (
        b"OggS"
        + struct.pack("<BBQIIIB", 0, 0, 0, 1, seq, 0, len(tabela))
        + tabela
        + b"".join(pacotes)
    )


def _ogg(pacotes: list[bytes], por_pagina: int = 50) -> bytes:
    paginas = [_pagina([b"OpusHead" + bytes(11)], 0), _pagina([b"OpusTags" + bytes(20)], 1)]
    for i in range(0, len(pacotes), por_pagina):
        paginas.append(_pagina(pacotes[i : i + por_pagina], 2 + i // por_pagina))
    return b"".join(paginas)


class _ProcessoFake:
    pid = 4242
    returncode = 0

    def __init__(self, saida: bytes) -> None:
        self.stdout = io.BytesIO(saida)
        self.morto = False

    def kill(self) -> None:
        self.morto = True

    def poll(self):
        return 0

    def communicate(self):
        return b"", b""


@pytest.fixture
def ffmpeg(monkeypatch):
    """FonteOpus sem FFmpeg de verdade: guarda os argumentos e serve um Ogg pronto."""
    estado = {"args": None, "saida": b"", "processo": None}

    def spawn(self, args, **kwargs):
        estado["args"] = args
        estado["processo"] = _ProcessoFake(estado["saida"])
        return estado["processo"]

    monkeypatch.setattr(FonteOpus, "_spawn_process", spawn)
    return estado


def _le_tudo(fonte) -> list[bytes]:
    lidos = []
    while True:
        dado = fonte.read()
        if not dado:
            return lidos
        lidos.append(dado)


# ── os pacotes ──────────────────────────────────────────────────────────────


def test_entrega_um_pacote_por_leitura_sem_os_cabecalhos_do_ogg(ffmpeg):
    pacotes = [_pacote(marca=i) for i in range(120)]
    ffmpeg["saida"] = _ogg(pacotes)

    fonte = FonteOpus("http://x/audio")

    assert fonte.is_opus() is True
    assert _le_tudo(fonte) == pacotes  # nem OpusHead nem OpusTags foram para o Discord
    assert fonte.fora_de_20ms == 0


def test_pacote_maior_que_255_bytes_atravessa_segmentos_e_paginas(ffmpeg):
    pacotes = [_pacote(tamanho=700, marca=1), _pacote(tamanho=255, marca=2), _pacote(marca=3)]
    ffmpeg["saida"] = _ogg(pacotes, por_pagina=1)

    assert _le_tudo(FonteOpus("http://x/audio")) == pacotes


def test_pacote_que_nao_tem_20ms_e_contado(ffmpeg):
    """Não se mata a faixa em tempo real por isso: conta-se, o fim da faixa
    avisa, e a próxima vez desse vídeo vai recodificada."""
    ffmpeg["saida"] = _ogg([_pacote(), _pacote(TOC_40MS), _pacote(TOC_10MS), _pacote()])

    fonte = FonteOpus("http://x/audio")

    assert len(_le_tudo(fonte)) == 4
    assert fonte.fora_de_20ms == 2


def test_tabela_do_toc():
    assert audio.pacote_de_20ms(_pacote(TOC_20MS))
    assert audio.pacote_de_20ms(bytes([0x78]))  # config 15: híbrido fullband 20ms
    assert not audio.pacote_de_20ms(_pacote(TOC_40MS))
    assert not audio.pacote_de_20ms(_pacote(TOC_10MS))
    assert not audio.pacote_de_20ms(b"")
    assert audio.pacote_de_20ms(OPUS_SILENCIO)


def test_ffmpeg_morto_por_nos_no_meio_da_pagina_e_fim_normal(ffmpeg, caplog):
    """Skip/stop/seek matam o FFmpeg com a página Ogg pela metade. Isso ia para o
    `log.exception` da thread do pulmão — e de lá para o espelho de erros que o
    dono recebe por DM, a cada skip."""
    inteiro = _ogg([_pacote(marca=i) for i in range(10)])
    ffmpeg["saida"] = inteiro[:-400] + b"OggS\x00"  # cabeçalho de página cortado

    fonte = FonteOpus("http://x/audio")
    fonte.read()
    fonte.cleanup()
    with caplog.at_level(logging.DEBUG):
        resto = _le_tudo(fonte)

    assert ffmpeg["processo"].morto
    assert len(resto) < 10
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_stream_quebrado_sem_ninguem_ter_matado_vira_aviso_e_fim(ffmpeg, caplog):
    ffmpeg["saida"] = _ogg([_pacote()]) + b"lixo que nao e ogg"

    fonte = FonteOpus("http://x/audio")
    with caplog.at_level(logging.WARNING):
        lidos = _le_tudo(fonte)

    assert len(lidos) == 1
    assert "Ogg" in caplog.text


# ── os argumentos do FFmpeg ─────────────────────────────────────────────────


def test_copia_nao_decodifica_nem_passa_opcao_de_encoder(ffmpeg):
    """A FFmpegOpusAudio do py-cord passa -b:a, -fec e -packet_loss mesmo em
    copy: o FFmpeg avisa "não usado" no stderr de TODA faixa."""
    FonteOpus("http://x/audio", before_options="-nostdin -reconnect 1")

    args = ffmpeg["args"]
    assert args[:4] == ["ffmpeg", "-nostdin", "-reconnect", "1"]
    assert args[args.index("-c:a") + 1] == "copy"
    assert args[-3:] == ["-f", "opus", "pipe:1"]
    for proibida in ("-af", "-b:a", "-fec", "-packet_loss", "-ss", "-copypriorss"):
        assert proibida not in args


def test_volume_recodifica_no_ffmpeg_em_pacotes_de_20ms(ffmpeg):
    fonte = FonteOpus("http://x/audio", copia=False, volume=0.5)

    args = ffmpeg["args"]
    assert fonte.copia is False
    assert args[args.index("-af") + 1] == "volume=0.50"
    assert args[args.index("-c:a") + 1] == "libopus"
    assert args[args.index("-frame_duration") + 1] == "20"
    assert args[args.index("-ar") + 1] == "48000" and args[args.index("-ac") + 1] == "2"


def test_seek_vai_antes_da_entrada(ffmpeg):
    """`-ss` ANTES do `-i`: o FFmpeg pula pelo índice do arquivo (um Range HTTP)
    em vez de baixar e jogar fora tudo até lá."""
    FonteOpus("http://x/audio", inicio_s=83.5)

    args = ffmpeg["args"]
    assert args[args.index("-ss") + 1] == "83.50"
    assert args.index("-ss") < args.index("-i")
    # em cópia o FFmpeg começa do ponto de busca ANTERIOR (até 2,3s antes,
    # medido) a não ser que mandem descartar o que vem antes do -ss
    assert args[args.index("-copypriorss") + 1] == "0"


def test_recodificando_o_seek_ja_e_exato(ffmpeg):
    FonteOpus("http://x/audio", inicio_s=10, copia=False)

    assert "-copypriorss" not in ffmpeg["args"]


# ── o pulmão com pacotes ────────────────────────────────────────────────────


class _Pacotes(discord.AudioSource):
    def __init__(self, quantos: int, trava_em: int | None = None, trava_s: float = 0.0):
        self.quantos, self.trava_em, self.trava_s, self.lidos = quantos, trava_em, trava_s, 0

    def read(self) -> bytes:
        if self.lidos == self.trava_em:
            self.trava_em = None
            time.sleep(self.trava_s)
        if self.lidos >= self.quantos:
            return b""
        self.lidos += 1
        return _pacote(marca=self.lidos)

    def is_opus(self) -> bool:
        return True


def test_pulmao_serve_pacotes_opus_e_conta_igual():
    fonte = BufferedAudioSource(_Pacotes(40), segundos=2.0, prebuffer_s=0.1, silencio=OPUS_SILENCIO)
    try:
        lidos = _le_tudo(fonte)
    finally:
        fonte.cleanup()

    assert lidos == [_pacote(marca=i) for i in range(1, 41)]
    assert fonte.frames_entregues == 40 and fonte.frames_sem_audio == 0
    assert fonte.is_opus() is True


def test_pulmao_vazio_manda_silencio_em_opus_e_nao_pcm_zerado():
    """3840 bytes de zeros mandados como se fossem um pacote Opus são lixo para
    o decoder de quem ouve."""
    fonte = BufferedAudioSource(
        _Pacotes(30, trava_em=20, trava_s=0.3), segundos=2.0, prebuffer_s=0.1, silencio=OPUS_SILENCIO
    )
    try:
        lidos = []
        for _ in range(60):
            dado = fonte.read()
            if not dado:
                break
            lidos.append(dado)
            time.sleep(0.005)
    finally:
        fonte.cleanup()

    assert OPUS_SILENCIO in lidos
    assert all(len(d) < 1000 for d in lidos)
    assert fonte.frames_sem_audio == lidos.count(OPUS_SILENCIO) > 0
    assert fonte.frames_entregues == 30


def test_marca_inicio_zera_o_cronometro_do_primeiro_audio():
    """Fonte montada ANTES de a faixa anterior acabar ficaria acusando "1º áudio
    só 8s depois do play" — o tempo em que ela esperou a vez."""
    fonte = BufferedAudioSource(_Pacotes(10), segundos=1.0, prebuffer_s=0.05, silencio=OPUS_SILENCIO)
    try:
        time.sleep(0.3)
        fonte.marca_inicio()
        fonte.read()
    finally:
        fonte.cleanup()

    assert fonte.primeiro_audio_s < 0.25


# ── o estalo em Opus ────────────────────────────────────────────────────────


def test_estalo_vira_pacotes_prontos_uma_vez_so():
    pcm = audio.carrega_efeito(audio.EFEITO_ESCUTA_PATH)

    pacotes = audio.efeito_em_opus(pcm)

    quadros = len(pcm) // audio.FRAME_BYTES
    assert len(pacotes) == quadros + 2 * audio.ESTALO_BORDA_FRAMES
    assert all(audio.pacote_de_20ms(p) for p in pacotes)
    assert audio.efeito_em_opus(pcm) is pacotes  # codificado uma vez, não a cada "Arrodes"
    assert audio.efeito_em_opus(b"") == ()


def test_pacotes_prontos_tocam_sem_encoder():
    fonte = PacotesProntos([b"a", b"b"])

    assert fonte.is_opus() is True
    assert [fonte.read(), fonte.read(), fonte.read()] == [b"a", b"b", b""]
