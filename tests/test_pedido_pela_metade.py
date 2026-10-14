"""Pedido dito pela metade não vira busca de lixo, e o nome que falta completa.

06/10, 22:21: "Arrodes, tocar com uma próxima música..." foi lido como play de
"com uma proxima musica" — a busca tocou "PASSO BEM SOLTO (Slowed)", o Otavio
limpou a fila e repetiu o pedido inteiro 17s depois. O "com uma" não estava
entre as marcas de "como próxima", e o nome da música ficou para o trecho
seguinte (a pessoa pensou no nome).
"""
import asyncio
import logging
import time

import pytest

from core.intents import mesmo_comando, parse_command
from core.listen import _Continuacao

from test_voice_continuacao import UM_SEGUNDO, _roda_worker
from test_wake_confirmada import _roda


@pytest.mark.parametrize("frase", [
    "Arrodes, tocar com uma próxima música...",   # 06/10 22:21
    "Arrodes, toca como uma próxima música",
    "Arrodes, toca uma música",
    "Arrodes, coloca a música",
])
def test_pedido_sem_o_nome_espera_o_nome(frase):
    assert parse_command(frase) == ("hello", "")


@pytest.mark.parametrize("frase, esperado", [
    ("Arrodes, toca com uma próxima música Mind Over Matter", ("play_next", "mind over matter")),
    ("Arrodes, coloca como uma próxima Mind Over Matter", ("play_next", "mind over matter")),
    ("Arrodes, toca uma música do Djavan", ("play", "musica do djavan")),
])
def test_com_o_nome_o_pedido_segue(frase, esperado):
    assert parse_command(frase) == esperado


def test_continuacao_guarda_o_pedido_incompleto():
    c = _Continuacao()
    c.abre(7, agora=100.0, pedido="Arrodes, toca como próxima música")

    completo = c.completa(7, "Mind Over Matter.", agora=103.0)

    assert parse_command(completo) == ("play_next", "mind over matter")


def test_chamada_so_com_o_nome_continua_como_antes():
    c = _Continuacao()
    c.abre(7, agora=100.0, pedido="Arrodes.")

    assert parse_command(c.completa(7, "pula", agora=101.0)) == ("skip", "")


def test_worker_junta_o_pedido_e_o_nome(monkeypatch):
    l, executados, _ = _roda(
        monkeypatch,
        ["Arrodes, toca como próxima música", "Mind Over Matter."],
        ["Arroz, toca como próxima música"],
    )
    agora = time.monotonic()

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, agora), (7, UM_SEGUNDO, agora)]))

    assert executados == [("hello", ""), ("play_next", "mind over matter")]


# ── 2ª escuta de um pedido sem o nome ────────────────────────────────────────


def test_pedido_de_tocar_nas_duas_escutas_confirma_a_chamada():
    assert mesmo_comando("Arroz, toca como próxima música", "hello")


@pytest.mark.parametrize("segunda", ["É isso.", "Arroz.", "Então, vamos lá."])
def test_hello_sem_pedido_continua_exigindo_o_nome(segunda):
    assert not mesmo_comando(segunda, "hello")


# ── o nome como sai na transcrição ───────────────────────────────────────────


def test_a_rodes_e_o_nome():
    """O nome saído em duas palavras: o "a" sai como muleta e "rodes" (0,83) passa."""
    assert parse_command("A Rodes, próxima.") == ("skip", "")
    assert parse_command("Herodes, próxima.") == ("skip", "")


def _sem_nome(monkeypatch, caplog, fala):
    l, executados, _ = _roda(monkeypatch, [fala], [])
    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))
    return executados


def test_sem_nome_mas_com_comando_claro_avisa_no_log(monkeypatch, caplog):
    """A chamada calada: o nome saiu irreconhecível e a pessoa teve de repetir
    "Arrodes, próxima." segundos depois. O log diz que o resto era um comando,
    para medir quantas se perdem assim, e como o nome saiu — sem o conteúdo."""
    assert _sem_nome(monkeypatch, caplog, "Rode, próxima.") == []
    assert "o resto leria skip" in caplog.text
    assert "→'rode' 0.73" in caplog.text


def test_conversa_sem_nome_nao_ganha_o_aviso(monkeypatch, caplog):
    _sem_nome(monkeypatch, caplog, "Cara, eu fui no mercado hoje.")
    assert "o resto leria" not in caplog.text


def test_barrada_diz_no_log_o_que_a_segunda_leu(monkeypatch, caplog):
    """06/10 22:16: barrada com a 2ª começando por "entao" e a pessoa repetiu 5s
    depois. Só a 1ª palavra no log não dizia se a 2ª ouviu outro comando ou nada."""
    l, executados, _ = _roda(monkeypatch, ["Arrodes, próxima."], ["Então, vamos lá."])

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert executados == []
    assert "2ª leu unknown" in caplog.text
    assert "vamos" not in caplog.text  # o conteúdo continua fora do journal
