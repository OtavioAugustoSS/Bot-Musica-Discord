"""O bot só atende quando o nome foi DITO — não quando o Whisper o inventou.

O prompt da Groq começa com "Arrodes,": o Whisper usa o prompt como texto
anterior e abre a transcrição com o nome sobre qualquer coisa — "É isso." pode
sair "Arrodes.", e letra de música vazando no microfone de alguém, "Arrodes,
I'm going to ask you…". Cada um desses toca o estalo de escuta e manda "não
conheço esse comando" no chat. E palavras de verdade perto do nome (arrotes,
arroz, acordes) não podem chamar o bot.

Conserto: parser exigente (variações exatas, lista de bloqueio e fuzzy de
0,82) e uma SEGUNDA escuta do mesmo trecho, sem o nome no prompt, antes de
atender.
"""
import asyncio
import logging
import time

import pytest

import config
from core import listen
from core.intents import WAKE_WORD, chamou_de_frente, mesmo_comando, nome_no_audio, parse_command
from services import speech

from test_voice_continuacao import UM_SEGUNDO, _listener, _roda_worker

# o conftest troca a conferência por um "não deu para conferir" em todo teste
_confere_de_verdade = speech.confere_chamada


# ── parser: palavra parecida não é o nome ────────────────────────────────────


@pytest.mark.parametrize("frase", [
    "Arrotes de novo, que nojo.",
    "Arroz com feijão.",
    "A rede caiu de novo.",
    "Acordes de violão.",
    "A roda gigante.",
    "Vamos jogar rodada dupla.",
    "Arrodeia ali, tá ligado.",
    "Rode o vídeo de novo.",
    "Tá vendo o carro ali, né?",
    "Pode pedir, Arruda.",
    # colada, a conversa forma o nome: "carro de" e "arroz de" dão 0,86
    "O carro de som passou aqui.",
    "O carro de som, pula.",
    "Arroz de forno é bom.",
    "O carro desse cara, toca muito alto.",
    "Carrões, toca isso.",
])
def test_palavra_parecida_com_o_nome_nao_chama_o_bot(frase):
    assert parse_command(frase) is None
    assert not chamou_de_frente(frase)


@pytest.mark.parametrize("frase, esperado", [
    ("Arrodes, próxima.", ("skip", "")),
    ("Arodes, limpa a fila.", ("clear_queue", "")),
    ("A Rodes, pula.", ("skip", "")),
    ("Herodes, pula.", ("skip", "")),
    ("Harrods, próxima.", ("skip", "")),
    ("Arro des, pula.", ("skip", "")),
    ("Hero des, pula.", ("skip", "")),
    ("Arro de, pula.", ("skip", "")),  # variação exata: vale mesmo com "de"
    ("Peraí, arrodes, pula.", ("skip", "")),
])
def test_grafias_do_nome_continuam_chamando(frase, esperado):
    assert parse_command(frase) == esperado


# ── a segunda escuta: o nome está no áudio? ──────────────────────────────────


@pytest.mark.parametrize("segunda", [
    # sem o nome no prompt, o Whisper escreve o nome conhecido mais perto
    "A Rodes, pula.",
    "Herodes pula",
    "arrodis",
    "Harrods, coloca na fila Legião Urbana.",
    "Rhodes, próxima.",                            # 0,77: vale por ser variação
    "Peraí, Arodis, toca Djavan.",
    "Ah, Erodes, desliga o autoplay.",
    "Arro des, próxima.",
    "Arro de, próxima.",
])
def test_nome_no_audio_reconhece_o_nome_mal_escrito(segunda):
    assert nome_no_audio(segunda)


@pytest.mark.parametrize("segunda", [
    "É isso.",                                     # a 1ª escuta escreveu "Arrodes."
    "I'm going to ask you, I'm going to ask you",
    "Tá vendo o carro ali, né?",
    "Sozinha.",
    "",
    # a 0,7 estas confirmavam o nome
    "acordes bonitos",
    "rode o vídeo",
    "a rodas",
    "rodeios",
    "arredores",
    # palavras de verdade da lista de bloqueio (arrotes é 0,86)
    "Arrotes de novo.",
    "Arrodeia ali.",
    # pedaço do nome só vale numa palavra inteira: colado, "querodescansar" tem "rodes"
    "Eu quero descansar.",
    "Vamos pro desafio.",
    "vamos jogar rodada",
    # par colado sem a forma do nome
    "O carro de som passou aqui.",
    "Eu comi arroz de forno.",
    "Carros de corrida.",
    "O carro desse cara.",
    "Carrões.",
])
def test_nome_no_audio_recusa_o_que_nao_e_o_nome(segunda):
    assert not nome_no_audio(segunda)


# ── ...ou o mesmo comando nas duas escutas ───────────────────────────────────
# Sem o nome no prompt ele pode sair uma palavra qualquer ("Arroz", "Haroldo"):
# barrada, a pessoa tinha de repetir "Arrodes, próxima." segundos depois.


@pytest.mark.parametrize("segunda, acao", [
    ("Arroz, próxima.", "skip"),
    ("A rede, próxima.", "skip"),
    ("Haroldo, próxima.", "skip"),
    ("Até a próxima vida.", "skip"),
    ("Tá, embaralha a fila.", "shuffle"),
    # o nome mal ouvido na frente quebrava o parse — barrada à toa
    ("Aroldo, com o próximo.", "skip"),
    ("Rodas, limpa a fila.", "clear_queue"),
])
def test_mesmo_comando_nas_duas_escutas_confirma(segunda, acao):
    assert mesmo_comando(segunda, acao)


@pytest.mark.parametrize("segunda, acao", [
    ("É isso.", "hello"),           # o nome sozinho, inventado pelo prompt
    ("Sozinha.", "unknown"),        # unknown/hello só pelo nome
    ("Arroz, próxima.", "shuffle"),  # ações diferentes
    ("", "skip"),
])
def test_mesmo_comando_nao_confirma_o_resto(segunda, acao):
    assert not mesmo_comando(segunda, acao)


def test_nome_mal_ouvido_com_o_mesmo_comando_executa(monkeypatch):
    l, executados, estalos = _roda(monkeypatch, ["Arrodes, próxima."], ["Arroz, próxima."])

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert executados == [("skip", "")]
    assert len(estalos) == 1


def test_barrada_diz_no_log_o_que_a_primeira_leu(monkeypatch, caplog):
    l, executados, _ = _roda(monkeypatch, ["Arrodes, próxima."], ["Abre a porta."])

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert executados == []
    assert "que leu skip" in caplog.text
    assert f"'{WAKE_WORD.capitalize()}' só na 1ª escuta" in caplog.text


def test_segunda_escuta_vai_sem_o_nome_no_prompt(monkeypatch):
    pedidos = []

    async def groq(audio, contexto="", prompt=None):
        pedidos.append(prompt)
        return "É isso."

    monkeypatch.setattr(config, "GROQ_API_KEY", "k")
    monkeypatch.setattr(speech, "_transcribe_groq", groq)

    assert asyncio.run(_confere_de_verdade(UM_SEGUNDO)) == "É isso."
    assert WAKE_WORD in config.VOICE_STT_PROMPT.lower()  # o da 1ª escuta tem o nome
    assert pedidos and WAKE_WORD not in pedidos[0].lower()
    assert "toca" in pedidos[0]  # o resto do vocabulário continua


def test_prompt_sem_nome_segue_o_wake_word(monkeypatch):
    """O nome sai do prompt pelo WAKE_WORD — não por uma grafia escrita à mão."""
    monkeypatch.setattr(config, "VOICE_STT_PROMPT", f"{WAKE_WORD.capitalize()}, toca, pula. {WAKE_WORD}.")
    assert speech._prompt_sem_nome() == "toca, pula."
    monkeypatch.setattr(speech, "WAKE_WORD", "fulano")
    monkeypatch.setattr(config, "VOICE_STT_PROMPT", "Fulano, toca, pula, fulaninho.")
    assert speech._prompt_sem_nome() == "toca, pula, fulaninho."


def test_segunda_escuta_que_falha_devolve_none(monkeypatch):
    async def groq(audio, contexto="", prompt=None):
        raise RuntimeError("fora do ar")

    monkeypatch.setattr(config, "GROQ_API_KEY", "k")
    monkeypatch.setattr(speech, "_transcribe_groq", groq)

    assert asyncio.run(_confere_de_verdade(UM_SEGUNDO)) is None


def test_sem_groq_nao_ha_segunda_escuta(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "")
    assert asyncio.run(_confere_de_verdade(UM_SEGUNDO)) is None


# ── o worker ─────────────────────────────────────────────────────────────────


def _roda(monkeypatch, primeiras, segundas):
    primeiras, segundas = iter(primeiras), iter(segundas)
    executados, estalos = [], []

    async def transcreve(pcm, contexto=""):
        return next(primeiras)

    async def confere(pcm):
        return next(segundas)

    async def executa(user_id, heard, action, arg, **kw):
        executados.append((action, arg))

    monkeypatch.setattr(listen.speech, "transcribe", transcreve)
    monkeypatch.setattr(listen.speech, "confere_chamada", confere)
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    l = _listener()
    l._contexto_da_fila = lambda: ""
    l.player.tocar_efeito = lambda efeito: estalos.append(efeito)
    l._execute = executa
    return l, executados, estalos


def test_nome_inventado_pelo_prompt_e_ignorado_sem_estalo(monkeypatch, caplog):
    l, executados, estalos = _roda(monkeypatch, ["Arrodes, sozinha."], ["Sozinha."])

    with caplog.at_level(logging.INFO, logger="core.listen"):
        asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert executados == []
    assert estalos == []
    assert "2ª escuta" in caplog.text
    assert "comando de voz" not in caplog.text


def test_nome_confirmado_na_segunda_escuta_executa(monkeypatch):
    l, executados, estalos = _roda(
        monkeypatch, ["Arrodes, próxima."], ["Herodes, próxima."]
    )

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert executados == [("skip", "")]
    assert len(estalos) == 1


def test_segunda_escuta_indisponivel_vale_a_primeira(monkeypatch):
    """Groq fora ou sem cota na conferência: melhor atender do que perder o comando."""
    l, executados, _ = _roda(monkeypatch, ["Arrodes, próxima."], [None])

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, time.monotonic())]))

    assert executados == [("skip", "")]


def test_continuacao_de_chamada_confirmada_nao_e_conferida_de_novo(monkeypatch):
    """"Arrodes" [pausa] "toca X": o 2º trecho não tem o nome, e nem precisa."""
    l, executados, _ = _roda(
        monkeypatch, ["Arrodes.", "toca calcinha preta"], ["Herodes."]
    )
    agora = time.monotonic()

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, agora), (7, UM_SEGUNDO, agora)]))

    assert executados == [("hello", ""), ("play", "calcinha preta")]


def test_chamada_barrada_nao_abre_a_janela_de_continuacao(monkeypatch):
    l, executados, _ = _roda(monkeypatch, ["Arrodes.", "pula"], ["É isso."])
    agora = time.monotonic()

    asyncio.run(_roda_worker(l, [(7, UM_SEGUNDO, agora), (7, UM_SEGUNDO, agora)]))

    assert executados == []
