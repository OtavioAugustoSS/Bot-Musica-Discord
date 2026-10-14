""""Arrodes, desliga o autoplay" fez o bot SAIR DA CALL (teste de 12/09, 21:13).

O Whisper escreveu "auto play" separado (27 chars, comando=leave no
journal). Com isso `"autoplay" in rest` não casava, e a checagem seguinte
via "desliga" — palavra de sair — na primeira posição. Duas correções:
"auto play"/"auto-play" viram "autoplay" antes de interpretar, e
ligar/desligar/tirar/parar "a rádio" também é autoplay, não sair.
"""
import pytest

from core.intents import parse_command


@pytest.mark.parametrize("frase", [
    "Arrodes, desliga o autoplay",
    "Arrodes desliga o auto play",
    "Arrodes, desliga o auto-play",
    "Arrodes, liga o auto play",
    "Arrodes tira o autoplay",
    "Arrodes, para o auto play",
    "Arrodes, desativa o autoplay",
    "Arrodes, desliga a rádio",
    "Arrodes liga a rádio",
    "Arrodes, para a rádio",
    "Arrodes, autoplay",
])
def test_mexer_no_autoplay_nunca_e_sair(frase):
    assert parse_command(frase) == ("autoplay", "")


def test_desliga_sozinho_continua_sendo_sair():
    assert parse_command("Arrodes, desliga") == ("leave", "")


def test_tocar_uma_radio_continua_sendo_tocar():
    # o parser devolve a busca normalizada (sem acentos)
    assert parse_command("Arrodes, toca rádio gaúcha") == ("play", "radio gaucha")


def test_tocar_musica_com_play_no_nome_nao_vira_autoplay():
    assert parse_command("Arrodes toca Sertanejo Play")[0] == "play"
