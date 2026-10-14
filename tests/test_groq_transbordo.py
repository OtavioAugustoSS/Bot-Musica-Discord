"""429 do MINUTO num modelo: o trecho vai na hora para o outro.

01 e 03/10/2026, 5 pessoas na call: o turbo tomou 429 com 17–44 requisições no
último minuto (`retry-after=3`, 1100–1800 restantes no dia) e 38 trechos viraram
"comando perdido" — esperando o minuto do turbo enquanto o large-v3, com os seus
próprios 20 por minuto (tabela de limites da Groq), ficava parado.
"""
import asyncio

from services import speech

from test_speech_cota_do_dia import LARGE, TURBO, _pcm, _Resposta, groq  # noqa: F401 — fixture


def test_429_do_minuto_no_turbo_o_mesmo_trecho_vai_para_o_large(groq):
    sessao = groq([_Resposta(429, restantes=500, retry_after="3"), _Resposta(200, restantes=1999)])

    assert asyncio.run(speech.transcribe(_pcm(1.2))) == "arrodes pula"
    assert sessao.modelos == [TURBO, LARGE], "sem esperar o minuto do turbo"
    assert speech.leu_estatisticas()["transbordos"] == 1


def test_enquanto_o_minuto_do_turbo_esta_cheio_vai_direto_no_large(groq):
    sessao = groq([
        _Resposta(429, restantes=500, retry_after="3"), _Resposta(200, restantes=1999),
        _Resposta(200, restantes=1998),
    ])

    asyncio.run(speech.transcribe(_pcm(1.2)))
    asyncio.run(speech.transcribe(_pcm(1.2)))
    assert sessao.modelos == [TURBO, LARGE, LARGE], "sem gastar outra requisição num 429"


def test_o_turbo_volta_depois_do_retry_after(groq):
    sessao = groq([
        _Resposta(429, restantes=500, retry_after="3"), _Resposta(200, restantes=1999),
        _Resposta(200, restantes=499),
    ])

    asyncio.run(speech.transcribe(_pcm(1.2)))
    groq.relogio[0] += 4
    asyncio.run(speech.transcribe(_pcm(1.2)))
    assert sessao.modelos == [TURBO, LARGE, TURBO]


def test_turbo_com_20_no_minuto_ja_manda_para_o_large_sem_429(groq):
    sessao = groq([_Resposta(200, restantes=1999)])
    for _ in range(20):
        speech._conta_envio(TURBO)

    asyncio.run(speech.transcribe(_pcm(1.2)))
    assert sessao.modelos == [LARGE]


def test_os_dois_no_teto_do_minuto_esperam_como_antes(groq):
    sessao = groq([
        _Resposta(429, restantes=500, retry_after="2"),
        _Resposta(429, restantes=500, retry_after="2"),
        _Resposta(200, restantes=499),
    ])

    assert asyncio.run(speech.transcribe(_pcm(1.2))) == "arrodes pula"
    assert sessao.modelos == [TURBO, LARGE, LARGE], "o último espera o retry-after e tenta de novo"


def test_large_sem_o_dia_o_429_do_minuto_do_turbo_espera(groq):
    speech._cota[LARGE] = (0.0, 2000.0, groq.relogio[0])
    sessao = groq([_Resposta(429, restantes=500, retry_after="2"), _Resposta(200, restantes=499)])

    assert asyncio.run(speech.transcribe(_pcm(1.2))) == "arrodes pula"
    assert sessao.modelos == [TURBO, TURBO]


def test_teto_de_duracao_soma_o_minuto_dos_dois_modelos(groq):
    for _ in range(25):  # passava de 20 → antes, só trecho curto ia
        speech._conta_envio(TURBO)

    teto, usados = speech._teto_de_duracao()
    assert usados == 25 and teto is None
