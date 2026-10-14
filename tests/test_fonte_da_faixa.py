"""FonteDaFaixa: a faixa inteira aos olhos do py-cord, mesmo que o FFmpeg mude por baixo.

O py-cord recebe UMA fonte por `voice.play()` e chama o `after` quando ela acaba.
Seek e troca de volume precisam de um FFmpeg novo no MEIO da faixa; se isso fosse
um `stop` + `play`, o `after` dispararia, a fila andaria e a pausa se perderia.
Trocando a fonte de dentro por atribuição, o py-cord não vê nada.

É também quem segura o estalo de escuta no caminho Opus: sem PCM não há onde
misturar, então o estalo entra como pacotes próprios e a música ESPERA — nada
dela se perde, ela só atrasa ~0,7s.
"""
import time

import discord

from core import audio
from core.audio import BufferedAudioSource, FonteDaFaixa

FRAME = audio.FRAME_BYTES


class _Fonte(discord.AudioSource):
    """N frames numerados; opus=True entrega "pacotes" curtos."""

    def __init__(self, quantos: int, *, opus: bool, marca: int = 0, trava: bool = False) -> None:
        self.quantos, self.opus, self.marca, self.trava = quantos, opus, marca, trava
        self.lidos = 0
        self.limpou = False
        self.fora_de_20ms = 0

    def read(self) -> bytes:
        while self.trava and not self.limpou:
            time.sleep(0.005)
        if self.lidos >= self.quantos:
            return b""
        self.lidos += 1
        corpo = bytes([0xF8, self.marca, self.lidos % 256])
        return corpo if self.opus else corpo + bytes(FRAME - 3)

    def is_opus(self) -> bool:
        return self.opus

    def cleanup(self) -> None:
        self.limpou = True


def _pulmao(quantos: int, *, opus: bool, marca: int = 0, trava: bool = False) -> BufferedAudioSource:
    return BufferedAudioSource(_Fonte(quantos, opus=opus, marca=marca, trava=trava), segundos=4.0, prebuffer_s=0.02)


def _cheio(pulmao: BufferedAudioSource, quantos: int) -> None:
    fim = time.monotonic() + 5
    while pulmao._fila.qsize() < quantos and time.monotonic() < fim:
        time.sleep(0.005)


def test_le_da_fonte_de_dentro_e_tem_o_formato_dela():
    for opus in (True, False):
        faixa = FonteDaFaixa(_pulmao(5, opus=opus))
        try:
            assert faixa.is_opus() is opus
            lidos = [faixa.read() for _ in range(6)]
        finally:
            faixa.cleanup()
        assert [d[2] for d in lidos[:5]] == [1, 2, 3, 4, 5] and lidos[5] == b""
        assert faixa.frames_entregues == 5


def test_estalo_em_opus_segura_a_musica_e_nada_se_perde(monkeypatch):
    monkeypatch.setattr(audio, "efeito_em_opus", lambda pcm: (b"e1", b"e2", b"e3"))
    faixa = FonteDaFaixa(_pulmao(10, opus=True))
    try:
        _cheio(faixa._fonte, 10)
        primeiro = faixa.read()
        faixa.sobrepor(b"pcm do estalo")
        durante = [faixa.read() for _ in range(3)]
        depois = faixa.read()
    finally:
        faixa.cleanup()

    assert primeiro[2] == 1
    assert durante == [b"e1", b"e2", b"e3"]
    assert depois[2] == 2  # a música retoma exatamente de onde parou
    assert faixa.frames_entregues == 2  # o estalo não conta como música tocada


def test_estalo_em_pcm_continua_sendo_mistura_por_cima():
    faixa = FonteDaFaixa(_pulmao(10, opus=False))
    try:
        _cheio(faixa._fonte, 10)
        faixa.sobrepor(bytes(FRAME))
        assert len(faixa._fonte._efeito) == 1  # foi para o pulmão, que mistura na leitura
        assert len(faixa.read()) == FRAME
    finally:
        faixa.cleanup()


def test_troca_da_fonte_nao_encerra_a_faixa_e_soma_os_contadores():
    """O seek: FFmpeg novo a partir de 60s. Para o py-cord a fonte é a mesma."""
    velha = _pulmao(50, opus=True, marca=1)
    faixa = FonteDaFaixa(velha)
    try:
        _cheio(velha, 50)
        for _ in range(10):
            faixa.read()
        assert faixa.posicao_s == 0.2

        nova = _pulmao(50, opus=True, marca=2)
        _cheio(nova, 50)
        devolvida = faixa.troca(nova, offset_s=60.0)
        lido = faixa.read()
    finally:
        faixa.cleanup()
        velha.cleanup()

    assert devolvida is velha
    assert lido[1] == 2  # já é áudio da fonte nova
    assert faixa.frames_entregues == 11  # 10 da velha + 1 da nova: o que TOCOU
    assert abs(faixa.posicao_s - 60.02) < 1e-9  # onde a música ESTÁ


def test_fonte_trocada_no_meio_de_uma_leitura_presa_nao_vira_fim_de_faixa():
    """A thread de áudio pode estar DENTRO do read() da fonte velha (pulmão vazio,
    FFmpeg lento) na hora do seek. O b"" que a velha devolve ao ser interrompida
    chegaria ao py-cord como fim da faixa — e a fila andaria no meio do seek."""
    import threading

    velha = _pulmao(5, opus=True, marca=1, trava=True)
    faixa = FonteDaFaixa(velha)
    lidos = []
    leitor = threading.Thread(target=lambda: lidos.append(faixa.read()))
    try:
        leitor.start()
        time.sleep(0.1)  # o leitor está preso esperando a velha
        nova = _pulmao(5, opus=True, marca=2)
        _cheio(nova, 5)
        faixa.troca(nova, offset_s=30.0)
        leitor.join(timeout=3)
    finally:
        faixa.cleanup()
        velha.cleanup()

    assert lidos and lidos[0] and lidos[0][1] == 2


def test_interrompe_encerra_ja_mesmo_com_estalo_pendente(monkeypatch):
    monkeypatch.setattr(audio, "efeito_em_opus", lambda pcm: (b"e1", b"e2"))
    faixa = FonteDaFaixa(_pulmao(10, opus=True))
    try:
        faixa.sobrepor(b"x")
        faixa.interrompe()
        assert faixa.read() == b""
    finally:
        faixa.cleanup()


def test_cleanup_mata_a_fonte_de_dentro():
    pulmao = _pulmao(5, opus=True)
    faixa = FonteDaFaixa(pulmao)
    faixa.cleanup()
    assert pulmao.original.limpou


def test_pacotes_fora_de_20ms_somam_entre_as_fontes():
    velha, nova = _pulmao(3, opus=True), _pulmao(3, opus=True)
    velha.original.fora_de_20ms, nova.original.fora_de_20ms = 2, 5
    faixa = FonteDaFaixa(velha)
    try:
        faixa.troca(nova, offset_s=10.0)
        assert faixa.fora_de_20ms == 7
    finally:
        faixa.cleanup()
        velha.cleanup()
