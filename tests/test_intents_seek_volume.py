"""Voz: "avança 30 segundos", "volta 10", "volume 50" — sem roubar os comandos antigos.

"volta" sozinho é despausar, "avança aí" e "pula" são pular a faixa, e continuam
sendo. Seek e volume só disparam quando a frase traz um NÚMERO com unidade de
tempo, ou a palavra "volume" — é o que impede "volta" de virar seek e "pula"
de virar "pula 0 segundos".
"""
import pytest

from core.intents import parse_command, parse_posicao


@pytest.mark.parametrize("frase,esperado", [
    ("Arrodes, avança 30 segundos", ("seek", "+30")),
    ("Arrodes, adianta um minuto", ("seek", "+60")),
    ("Arrodes, pula 15 segundos", ("seek", "+15")),
    ("Arrodes, avança um minuto e meio", ("seek", "+90")),
    ("Arrodes, volta 10 segundos", ("seek", "-10")),
    ("Arrodes, volta dez segundos", ("seek", "-10")),
    ("Arrodes, retrocede meio minuto", ("seek", "-30")),
    ("Arrodes, volta 2 minutos", ("seek", "-120")),
    ("Arrodes, vai para o minuto 2", ("seek", "120")),
    ("Arrodes, vai pro minuto 1 e 30", ("seek", "90")),
    ("Arrodes, volta pro começo", ("seek", "0")),
    ("Arrodes, recomeça a música", ("seek", "0")),
])
def test_seek_por_voz(frase, esperado):
    assert parse_command(frase) == esperado


@pytest.mark.parametrize("frase,esperado", [
    ("Arrodes, volume 50", ("volume", "50")),
    ("Arrodes, volume em cinquenta", ("volume", "50")),
    ("Arrodes, bota o volume em 80", ("volume", "80")),
    ("Arrodes, volume 100 por cento", ("volume", "100")),
    ("Arrodes, aumenta o volume", ("volume", "+20")),
    ("Arrodes, sobe o volume", ("volume", "+20")),
    ("Arrodes, abaixa o volume", ("volume", "-20")),
    ("Arrodes, diminui o volume", ("volume", "-20")),
    ("Arrodes, mais alto", ("volume", "+20")),
    ("Arrodes, mais baixo", ("volume", "-20")),
])
def test_volume_por_voz(frase, esperado):
    assert parse_command(frase) == esperado


@pytest.mark.parametrize("frase,esperado", [
    ("Arrodes, volta", ("resume", "")),
    ("Arrodes, volta aí", ("resume", "")),
    ("Arrodes, continua", ("resume", "")),
    ("Arrodes, pula", ("skip", "")),
    ("Arrodes, pula essa", ("skip", "")),
    ("Arrodes, próxima", ("skip", "")),
    ("Arrodes, avança aí", ("skip", "")),
    ("Arrodes, pausa", ("pause", "")),
    ("Arrodes, toca 30 seconds to mars", ("play", "30 seconds to mars")),
    ("Arrodes, toca volume morto", ("play", "volume morto")),
    ("Arrodes, toca dez minutos", ("play", "dez minutos")),
])
def test_comandos_antigos_nao_mudam(frase, esperado):
    assert parse_command(frase) == esperado


# ── o argumento do /seek ─────────────────────────────────────────────────────


@pytest.mark.parametrize("texto,atual,duracao,esperado", [
    ("1:30", 10, 200, 90),
    ("01:05", 0, 200, 65),
    ("1:02:03", 0, 9000, 3723),
    ("90", 10, 200, 90),
    ("+30", 100, 200, 130),
    ("-10", 100, 200, 90),
    ("-30", 12, 200, 0),  # não volta para antes do começo
    ("+500", 100, 200, 195),  # nem passa do fim: para 5s antes, a faixa acaba sozinha
    ("2m", 0, 600, 120),
    ("1m30s", 0, 600, 90),
    ("45s", 0, 600, 45),
    ("+30", 100, 0, 130),  # duração desconhecida (ao vivo): sem teto
])
def test_posicao(texto, atual, duracao, esperado):
    assert parse_posicao(texto, atual_s=atual, duracao_s=duracao) == esperado


@pytest.mark.parametrize("texto", ["", "abc", "1:2:3:4", "1:99", "--5", "+"])
def test_posicao_invalida(texto):
    assert parse_posicao(texto, atual_s=10, duracao_s=200) is None


@pytest.mark.parametrize("frase,esperado", [
    # "pausa" e "passa" são vizinhos no fuzzy: o seek casa verbo EXATO
    ("Arrodes, pausa 10 segundos", ("pause", "")),
    ("Arrodes, avança 1 minuto 30", ("seek", "+90")),
    ("Arrodes, avança 1 minuto e 30 segundos", ("seek", "+90")),
    ("Arrodes, vai para 2 minutos", ("seek", "120")),
    ("Arrodes, vai pro minuto 1:30", ("seek", "90")),
    ("Arrodes, vai 30 segundos pra frente", ("seek", "+30")),
    ("Arrodes, volta 30 segundos atrás", ("seek", "-30")),
    ("Arrodes, aumenta o volume para 80", ("volume", "80")),
    ("Arrodes, sobe o som em 10", ("volume", "+10")),
    ("Arrodes, abaixa um pouco o volume", ("volume", "-20")),
    ("Arrodes, volume cento e vinte", ("volume", "120")),
    ("Arrodes, volume vinte e cinco", ("volume", "25")),
    ("Arrodes, bota volume morto", ("play", "volume morto")),
    ("Arrodes, bota 30 segundos", ("play", "30 segundos")),
    ("Arrodes, volta a música", ("resume", "")),
    ("Arrodes, pula pra próxima", ("skip", "")),
])
def test_fronteiras(frase, esperado):
    assert parse_command(frase) == esperado
