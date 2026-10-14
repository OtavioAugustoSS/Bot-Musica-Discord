"""Modo de voz mais rápido sem perder comando partido ao meio.

Medido no journal de 12/09: o Groq transcreve em 0,3–0,7s; o que domina a
latência é o corte por silêncio (VOICE_GAP_S, 1,2s) mais o tique de colheita
(0,25s). Encurtar o corte para 0,8s tira ~0,5s de cada comando — mas
aumenta a chance de "Arrodes … toca X" virar dois trechos. O journal já
mostrava esse caso mesmo com 1,2s: "comando=hello" (só a wake word) e, 8s
depois, "comando=-" (o resto, sem wake word, ignorado). Daí a janela de
continuação: quem acabou de chamar o bot sem dizer o comando tem alguns
segundos para completar, e o trecho seguinte é lido como o comando.
"""
import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import listen
from core.intents import first_word, parse_command
from core.listen import _Continuacao, VoiceListener

UM_SEGUNDO = b"\x00" * listen.BYTES_PER_SECOND


# ── janela de continuação (lógica pura) ──────────────────────────────────────


def test_trecho_logo_depois_de_chamar_vira_o_comando():
    c = _Continuacao()
    c.abre(7, agora=100.0)

    assert c.completa(7, "toca calcinha preta", agora=102.0) == "arrodes toca calcinha preta"
    assert parse_command("arrodes toca calcinha preta") == ("play", "calcinha preta")


def test_janela_expira():
    c = _Continuacao()
    c.abre(7, agora=100.0)

    assert c.completa(7, "toca x", agora=100.0 + listen._CONTINUACAO_S + 1) == "toca x"


def test_janela_e_de_uma_vez_so():
    c = _Continuacao()
    c.abre(7, agora=100.0)
    c.completa(7, "pula", agora=101.0)

    assert c.completa(7, "pula", agora=101.5) == "pula"


def test_janela_e_por_pessoa():
    c = _Continuacao()
    c.abre(7, agora=100.0)

    assert c.completa(8, "pula", agora=101.0) == "pula"
    assert c.completa(7, "pula", agora=101.0) == "arrodes pula"


@pytest.mark.parametrize("ruido", ["Tchau.", "Tchau, tchau.", "Obrigado.", "Obrigada.", "Valeu.", "E aí", "."])
def test_alucinacao_do_whisper_na_janela_nao_vira_comando(ruido):
    """17/09, 02:35: "Arrodes." e, 4s depois, um trecho transcrito como
    "Tchau." (6 chars) — o bot saiu da call. O Whisper devolve essas frases
    sobre ruído (36 "Tchau." sozinhos em 30 dias de journal, ninguém
    chamando o bot); dentro da janela elas perdiam a proteção da wake word."""
    c = _Continuacao()
    c.abre(7, agora=100.0)

    assert c.completa(7, ruido, agora=101.0) == ruido
    assert parse_command(ruido) is None


def test_ruido_nao_gasta_a_janela():
    c = _Continuacao()
    c.abre(7, agora=100.0)
    c.completa(7, "Tchau.", agora=101.0)

    assert c.completa(7, "sai da call", agora=102.0) == "arrodes sai da call"


def test_tchau_dito_junto_com_a_wake_word_continua_saindo():
    assert parse_command("Arrodes, tchau.") == ("leave", "")


def test_frase_que_so_comeca_com_tchau_nao_e_ruido():
    c = _Continuacao()
    c.abre(7, agora=100.0)

    assert c.completa(7, "tchau pessoal", agora=101.0) == "arrodes tchau pessoal"


def test_wake_word_repetida_na_continuacao_nao_atrapalha():
    """A pessoa chama de novo dentro da janela: o parser já ignora eco."""
    assert parse_command("arrodes arrodes pula") == ("skip", "")


# ── o worker de verdade, com fala falsa ──────────────────────────────────────


def _listener() -> VoiceListener:
    player = MagicMock()
    player.guild.id = 1
    listener_ = VoiceListener(player, MagicMock())
    listener_.active = True
    return listener_


async def _roda_worker(listener_: VoiceListener, segmentos: list[tuple[int, bytes, float]]):
    for seg in segmentos:
        listener_._segments.put_nowait(seg)
    task = asyncio.create_task(listener_._worker())
    fim = time.monotonic() + 2.0
    while not listener_._segments.empty() and time.monotonic() < fim:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # deixa o último trecho terminar de ser processado
    listener_.active = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_chamar_e_depois_dizer_o_comando_funciona(monkeypatch):
    falas = iter(["Arrodes", "toca calcinha preta"])
    executados = []

    async def transcreve(pcm):
        return next(falas)

    async def executa(user_id, heard, action, arg, **kw):
        executados.append((action, arg))

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    l = _listener()
    l._execute = executa
    agora = time.monotonic()

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, agora), (7, UM_SEGUNDO, agora)]))

    assert executados == [("hello", ""), ("play", "calcinha preta")]


def test_chamar_e_o_whisper_inventar_tchau_nao_tira_o_bot_da_call(monkeypatch):
    falas = iter(["Arrodes.", "Tchau.", "pula"])
    executados = []

    async def transcreve(pcm):
        return next(falas)

    async def executa(user_id, heard, action, arg, **kw):
        executados.append((action, arg))

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    l = _listener()
    l._execute = executa
    agora = time.monotonic()

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, agora)] * 3))

    assert executados == [("hello", ""), ("skip", "")]


def test_log_da_transcricao_diz_quanto_do_trecho_era_voz(monkeypatch, caplog):
    """Sem o conteúdo (privacidade), mas com o bastante para separar fala de
    ruído da próxima vez que um comando aparecer do nada."""
    async def transcreve(pcm):
        return "Tchau pessoal"

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (4, pcm[: len(pcm) // 2]))
    l = _listener()
    l._execute = AsyncMock()

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert "0.5s de fala aparada" in caplog.text
    assert "4 janelas com voz" in caplog.text
    assert "pessoal" not in caplog.text.lower()


def test_trecho_sem_wake_word_e_sem_chamada_previa_e_ignorado(monkeypatch):
    async def transcreve(pcm):
        return "toca calcinha preta"

    executados = []

    async def executa(*a, **kw):
        executados.append(a)

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    l = _listener()
    l._execute = executa

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert executados == []


# ── o journal explica os descartes ───────────────────────────────────────────


def test_descarte_por_ruido_de_trecho_longo_vai_para_o_log(monkeypatch, caplog):
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (1, b""))
    l = _listener()

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO * 2, time.monotonic())]))

    assert "descartado" in caplog.text and "ruído" in caplog.text


def test_descarte_de_trecho_curto_nao_polui_o_log(monkeypatch, caplog):
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (1, b""))
    l = _listener()
    meio_segundo = b"\x00" * (listen.BYTES_PER_SECOND // 2)

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, meio_segundo, time.monotonic())]))

    assert "descartado" not in caplog.text


def test_sem_wake_word_loga_so_o_comeco(monkeypatch, caplog):
    """Privacidade: nunca o conteúdo — só o começo e a grafia que a pessoa
    tentou usar como wake word, para ajustar as variações."""
    async def transcreve(pcm):
        return "A Rode, bota aquela do Djavan"

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    l = _listener()
    l._execute = AsyncMock()

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert l._execute.await_count == 0  # "rode" (0,73) não é o nome
    assert "sem wake word (começa com rode→'rode' 0.73)" in caplog.text
    for palavra in ("bota", "aquela", "djavan"):
        assert palavra not in caplog.text.lower()


def test_first_word_normaliza():
    assert first_word("Herodes, pula!") == "herodes"
    assert first_word("") == ""


# ── colheita mais frequente ──────────────────────────────────────────────────


def test_tique_de_colheita_e_curto():
    """0,25s somava até 0,25s (média 0,125s) a TODO comando; 0,1s é barato."""
    assert listen._TICK_S <= 0.1
