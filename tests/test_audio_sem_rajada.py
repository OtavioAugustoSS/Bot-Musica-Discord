"""Atraso na thread de áudio vira um soluço, não música ACELERADA.

Relato do Otavio em 27/09/2026: "às vezes as músicas ficam aceleradas — é a única
coisa estranha do bot", e raro. O laço do AudioPlayer do py-cord agenda cada
pacote em `_start + 20ms × n`; se a thread perde a CPU (steal da VM: 50–75% nos
picos medidos em 17/09) ou o GIL, ao voltar ele manda TODOS os pacotes atrasados
sem dormir. O cliente do Discord (NetEQ do WebRTC) recebe a rajada e ACELERA a
reprodução para esvaziar o buffer. Em 26/09 20:47 o journal tem uma trava de
1,7s: ~85 pacotes numa rajada.

Aqui: atrasou mais que _ATRASO_TOLERADO_S, o relógio é realinhado — o áudio
continua de onde estava, no ritmo certo (o pulmão já garante que nada se perde).
"""
import threading

import pytest

from core import pycord_voice_patch as patch

DELAY = 0.02


class _Relogio:
    def __init__(self) -> None:
        self.agora = 100.0
        self.dormidas: list[float] = []

    def perf_counter(self) -> float:
        return self.agora

    def sleep(self, segundos: float) -> None:
        self.dormidas.append(segundos)
        self.agora += segundos


class _Fonte:
    def __init__(self, quadros: int, relogio: _Relogio, trava_no: int, trava_s: float) -> None:
        self.restam = quadros
        self.lidos = 0
        self.relogio = relogio
        self.trava_no, self.trava_s = trava_no, trava_s

    def read(self) -> bytes:
        if self.restam == 0:
            return b""
        self.restam -= 1
        self.lidos += 1
        if self.lidos == self.trava_no:
            self.relogio.agora += self.trava_s  # a thread perdeu a CPU aqui
        return b"x"

    def is_opus(self) -> bool:
        return True


class _Cliente:
    def __init__(self, relogio: _Relogio) -> None:
        self.relogio = relogio
        self.enviados: list[float] = []

    def send_audio_packet(self, data, encode=True) -> None:
        self.enviados.append(self.relogio.agora)

    def is_connected(self) -> bool:
        return True


class _Player:
    DELAY = DELAY

    def __init__(self, fonte, cliente) -> None:
        self.source, self.client = fonte, cliente
        self._end = threading.Event()
        self._resumed = threading.Event()
        self._resumed.set()
        self._played_frames_offset = 0

    def _speak(self, estado) -> None:
        pass

    def send_silence(self, count: int = 5) -> None:
        pass

    def stop(self) -> None:
        self._end.set()


@pytest.fixture
def relogio(monkeypatch):
    r = _Relogio()
    monkeypatch.setattr(patch.time, "perf_counter", r.perf_counter)
    monkeypatch.setattr(patch.time, "sleep", r.sleep)
    return r


def _intervalos(enviados: list[float]) -> list[float]:
    return [round(b - a, 4) for a, b in zip(enviados, enviados[1:])]


def test_trava_longa_nao_vira_rajada(relogio):
    fonte = _Fonte(60, relogio, trava_no=20, trava_s=0.5)
    cliente = _Cliente(relogio)

    patch._do_run_sem_rajada(_Player(fonte, cliente))

    intervalos = _intervalos(cliente.enviados)
    colados = [i for i in intervalos if i < 0.005]
    assert len(cliente.enviados) == 60, "nenhum pedaço da música se perde"
    assert len(colados) <= 1, f"rajada de {len(colados)} pacotes colados — o cliente acelera"
    assert all(abs(i - DELAY) < 1e-6 for i in intervalos[25:]), "depois da trava, ritmo normal"


def test_atraso_pequeno_ainda_e_compensado(relogio):
    """Jitter de 40ms é normal: aí recuperar o ritmo é o certo (senão a música atrasa sempre)."""
    sem_trava = _Cliente(relogio)
    patch._do_run_sem_rajada(_Player(_Fonte(30, relogio, trava_no=0, trava_s=0), sem_trava))
    cliente = _Cliente(relogio)
    patch._do_run_sem_rajada(_Player(_Fonte(30, relogio, trava_no=10, trava_s=0.04), cliente))

    duracao = cliente.enviados[-1] - cliente.enviados[0]
    assert duracao == pytest.approx(sem_trava.enviados[-1] - sem_trava.enviados[0], abs=1e-6)


def test_atraso_fica_registrado(relogio):
    patch.leu_atrasos_do_audio()
    fonte = _Fonte(40, relogio, trava_no=20, trava_s=0.3)

    patch._do_run_sem_rajada(_Player(fonte, _Cliente(relogio)))

    vezes, maior = patch.leu_atrasos_do_audio()
    assert vezes == 1 and maior == pytest.approx(0.3, abs=0.03)


def test_espera_do_pulmao_no_comeco_nao_e_atraso(relogio):
    """15/10: todo começo de faixa logava "atrasou 323ms" — era o 1º read()
    esperando o pulmão encher, contado no relógio que já corria."""
    sem_espera = _Cliente(relogio)
    patch._do_run_sem_rajada(_Player(_Fonte(30, relogio, trava_no=0, trava_s=0), sem_espera))
    patch.leu_atrasos_do_audio()
    cliente = _Cliente(relogio)

    patch._do_run_sem_rajada(_Player(_Fonte(30, relogio, trava_no=1, trava_s=0.35), cliente))

    assert patch.leu_atrasos_do_audio()[0] == 0
    # o mesmo ritmo de quem começou sem esperar nada
    assert _intervalos(cliente.enviados) == _intervalos(sem_espera.enviados)


def test_apply_troca_o_laco_do_player():
    import discord.player

    patch.apply()
    assert discord.player.AudioPlayer._do_run is patch._do_run_sem_rajada
