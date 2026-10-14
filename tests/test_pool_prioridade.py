"""O pool de extração tem PRIORIDADE: quem vai tocar agora passa na frente.

Log de produção (12/09, 15:32): a pessoa embaralhou a fila e pulou; a faixa
seguinte esperou 21,4s de "extração" — na verdade ficou na fila FIFO do
ThreadPoolExecutor (1 worker na VM) atrás de duas extrações de prefetch que
o embaralhar tinha tornado inúteis. Regras novas:

  - foreground (vai tocar / vai responder) passa na frente de prefetch e
    prewarm que ainda não começaram;
  - se o foreground pede uma faixa cujo prefetch está NA FILA, o job dela
    sobe de prioridade em vez de esperar a vez de prefetch;
  - prefetch cancelado (fila mudou) cuja extração nem começou some da fila
    — não gasta o worker único com trabalho que ninguém espera mais;
  - extração que JÁ começou termina e aquece o cache (não dá para matar).
"""
import asyncio
import threading

import pytest

from services import youtube

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


@pytest.fixture
def pool(monkeypatch):
    """Pool de 1 worker (o caso da VM) e um _extract que bloqueia por URL."""
    youtube._stream_cache.clear()
    youtube._inflight.clear()
    monkeypatch.setattr(youtube, "_YTDLP_POOL", youtube._Scheduler(1))
    ordem: list[str] = []
    libera: dict[str, threading.Event] = {}

    def extrai(url, **kwargs):
        vid = url.rsplit("=", 1)[-1]
        ordem.append(vid)
        libera.setdefault(vid, threading.Event()).wait(timeout=5)
        return {"id": vid, "title": "T", "url": f"http://s/{vid}"}

    monkeypatch.setattr(youtube, "_extract", extrai)

    def solta(vid):
        libera.setdefault(vid, threading.Event()).set()

    yield ordem, solta
    for ev in libera.values():
        ev.set()
    youtube._stream_cache.clear()
    youtube._inflight.clear()


async def _espera(cond, timeout=2.0):
    fim = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > fim:
            raise AssertionError("condição não satisfeita a tempo")
        await asyncio.sleep(0.005)


def _prefetch(vid):
    return asyncio.create_task(youtube.get_stream(vid, priority=youtube.PRIO_PREFETCH))


def test_foreground_passa_na_frente_do_prefetch_na_fila(pool):
    ordem, solta = pool

    async def run():
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])  # A ocupa o worker
        pb = _prefetch(B)  # fica na fila
        await asyncio.sleep(0.01)
        fc = asyncio.create_task(youtube.get_stream(C))  # foreground chega depois
        await asyncio.sleep(0.01)
        solta(A)
        await _espera(lambda: len(ordem) == 2)
        solta(C)
        solta(B)
        await asyncio.gather(pa, pb, fc)

    asyncio.run(run())

    assert ordem == [A, C, B]


def test_foreground_que_pede_faixa_ja_na_fila_de_prefetch_sobe_a_prioridade(pool):
    ordem, solta = pool

    async def run():
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])
        pb = _prefetch(B)
        pc = _prefetch(C)
        await asyncio.sleep(0.01)
        fc = asyncio.create_task(youtube.get_stream(C))  # /skip caiu justo na C
        await asyncio.sleep(0.01)
        solta(A)
        await _espera(lambda: len(ordem) == 2)
        solta(C)
        solta(B)
        resultados = await asyncio.gather(pa, pb, pc, fc)
        return resultados

    resultados = asyncio.run(run())

    assert ordem == [A, C, B]
    assert resultados[2] == resultados[3]  # UMA extração de C para os dois


def test_prefetch_cancelado_antes_de_comecar_some_da_fila(pool):
    """Embaralhou: o prefetch da faixa que não é mais a próxima nem começou —
    não pode gastar o worker único com ela."""
    ordem, solta = pool

    async def run():
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])
        pb = _prefetch(B)
        await asyncio.sleep(0.01)
        pb.cancel()
        await asyncio.sleep(0.01)
        solta(A)
        await pa
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert ordem == [A]
    assert youtube._inflight == {}


def test_faixa_cancelada_na_fila_pode_ser_pedida_de_novo(pool):
    ordem, solta = pool

    async def run():
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])
        pb = _prefetch(B)
        await asyncio.sleep(0.01)
        pb.cancel()
        await asyncio.sleep(0.01)
        solta(A)
        await pa
        fb = asyncio.create_task(youtube.get_stream(B))  # agora de verdade
        await _espera(lambda: ordem == [A, B])
        solta(B)
        return await fb

    info = asyncio.run(run())

    assert info["stream_url"] == f"http://s/{B}"


def test_prefetch_cancelado_com_extracao_em_andamento_termina_e_aquece(pool):
    """Já começou: não dá para matar — e o resultado ainda vale (cache)."""
    ordem, solta = pool

    async def run():
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])
        pa.cancel()
        await asyncio.sleep(0.01)
        solta(A)
        await _espera(lambda: youtube.is_cached(A))

    asyncio.run(run())

    assert youtube._inflight == {}


def test_um_dos_dois_que_esperam_cancela_e_o_outro_recebe(pool):
    ordem, solta = pool

    async def run():
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])
        pb = _prefetch(B)
        fb = asyncio.create_task(youtube.get_stream(B))
        await asyncio.sleep(0.01)
        pb.cancel()  # o prefetch desistiu, mas o foreground ainda quer
        await asyncio.sleep(0.01)
        solta(A)
        await _espera(lambda: ordem == [A, B])
        solta(B)
        return await fb

    info = asyncio.run(run())

    assert info["stream_url"] == f"http://s/{B}"


def test_estado_da_faixa_para_o_log(pool):
    ordem, solta = pool

    async def run():
        assert youtube.stream_state(A) == "fria"
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])
        assert youtube.stream_state(A) == "em voo"
        solta(A)
        await pa
        assert youtube.stream_state(A) == "no cache"

    asyncio.run(run())


def test_cada_job_mede_espera_e_execucao_para_o_log(pool, caplog):
    """Log de 12/09: extração "em voo" de 21,1s — quanto foi fila e quanto
    foi o yt-dlp rodando (com a música tocando na mesma CPU)? Sem separar
    os dois não dá para dizer se o gargalo é o pool ou a máquina."""
    import logging

    ordem, solta = pool

    async def run():
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])
        fb = asyncio.create_task(youtube.get_stream(B))
        await asyncio.sleep(0.05)  # B espera na fila enquanto A roda
        solta(A)
        await _espera(lambda: ordem == [A, B])
        solta(B)
        await asyncio.gather(pa, fb)

    with caplog.at_level(logging.INFO, logger="services.youtube"):
        asyncio.run(run())

    linhas = [r.message for r in caplog.records if "esperou" in r.message]
    assert len(linhas) == 2
    assert B in linhas[1] and "rodou" in linhas[1]
    espera_b = youtube._inflight  # já limpo; a medida fica no log
    assert espera_b == {}


def test_busca_e_playlist_tambem_passam_pelo_escalonador_como_foreground(pool, monkeypatch):
    ordem, solta = pool

    async def run():
        pa = _prefetch(A)
        await _espera(lambda: ordem == [A])
        pb = _prefetch(B)
        await asyncio.sleep(0.01)
        busca = asyncio.create_task(youtube.playlist_tracks("PLx"))
        await asyncio.sleep(0.01)
        solta(A)
        # a listagem falsa termina na hora e B entra logo atras: >= 2
        await _espera(lambda: len(ordem) >= 2)
        solta(B)
        await asyncio.gather(pa, pb, busca)

    monkeypatch.setattr(
        youtube, "_ytdlp_playlist",
        lambda list_id: (ordem.append(list_id), ("L", [], 0))[1],
    )

    asyncio.run(run())

    assert ordem == [A, "PLx", B]


# ── prioridade de CPU: extrair nunca pode atropelar o áudio ──────────────────
# Journal de 16/09: com a música tocando, a extração levou de 12,7s (mediana)
# a 48,3s num núcleo só, e o áudio cortava. A thread que manda áudio precisa
# de pouquíssima CPU, mas precisa dela A CADA 20ms; a extração precisa de
# muita, mas pode esperar. `nice` na thread do pool (e nos subprocessos que
# ela cria, como o deno) resolve exatamente essa assimetria.


def _espera_sync(condicao, timeout=2.0):
    import time as _t

    fim = _t.monotonic() + timeout
    while not condicao() and _t.monotonic() < fim:
        _t.sleep(0.005)


def test_cada_worker_do_pool_baixa_a_propria_prioridade(monkeypatch):
    chamadas = []
    monkeypatch.setattr(youtube, "_baixa_prioridade_de_cpu", lambda: chamadas.append(1))

    youtube._Scheduler(2, name="teste-nice")

    _espera_sync(lambda: len(chamadas) == 2)
    assert len(chamadas) == 2


def test_baixar_prioridade_nao_quebra_onde_nao_existe(monkeypatch):
    """os.nice não existe no Windows (dev)."""
    monkeypatch.delattr(youtube.os, "nice", raising=False)

    youtube._baixa_prioridade_de_cpu()  # não pode levantar


def test_baixar_prioridade_engole_falta_de_permissao(monkeypatch):
    def nega(_incremento):
        raise PermissionError("sem permissão")

    monkeypatch.setattr(youtube.os, "nice", nega, raising=False)

    youtube._baixa_prioridade_de_cpu()  # não pode levantar


def test_nice_da_extracao_e_absoluto_e_nao_um_incremento(monkeypatch):
    """os.nice(n) SOMA n ao nível atual. Com a unit do systemd em Nice=-5 (o
    áudio na frente), `os.nice(10)` deixaria a extração em +5 — a 10 níveis da
    thread de áudio em vez dos 15 pretendidos."""
    nivel = [-5]

    def nice(incremento):
        nivel[0] += incremento
        return nivel[0]

    monkeypatch.setattr(youtube.os, "nice", nice, raising=False)

    youtube._baixa_prioridade_de_cpu()

    assert nivel[0] == youtube._NICE_EXTRACAO


def test_nice_nunca_tenta_subir_a_prioridade(monkeypatch):
    """Quem já está em +15 fica onde está: baixar o nice exige privilégio."""
    nivel = [15]
    pedidos = []

    def nice(incremento):
        pedidos.append(incremento)
        nivel[0] += incremento
        return nivel[0]

    monkeypatch.setattr(youtube.os, "nice", nice, raising=False)

    youtube._baixa_prioridade_de_cpu()

    assert nivel[0] == 15 and all(p >= 0 for p in pedidos)
