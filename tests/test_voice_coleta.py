"""Coleta opt-in de amostras reais da wake word.

Para trocar "mandar a call inteira para a Groq" por um wake word local é preciso
um conjunto de teste com gente de verdade dizendo "Arrodes" — é o que mede o
recall de qualquer modelo antes de confiar nele. Autorizado pelo Otavio em
20/09/2026 com três travas: DESLIGADO por padrão (VOICE_COLETA=1 liga), só
trechos em que a Groq confirmou a wake word (fala dirigida ao bot, que ele já
repete no canal de texto) e teto de arquivos.
"""
import wave

import numpy as np

from core import listen


def _pcm(segundos: float = 1.0) -> bytes:
    onda = (np.sin(np.linspace(0, 2000, int(48000 * segundos))) * 8000).astype(np.int16)
    return np.repeat(onda, 2).tobytes()  # estéreo intercalado, como o Discord entrega


def test_amostra_vira_wav_16k_mono(tmp_path, monkeypatch):
    monkeypatch.setattr(listen, "_DIR_AMOSTRAS", str(tmp_path))

    caminho = listen.guarda_amostra(42, _pcm(1.0))

    with wave.open(caminho, "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert abs(w.getnframes() / 16000 - 1.0) < 0.05


def test_teto_de_arquivos_e_respeitado(tmp_path, monkeypatch):
    monkeypatch.setattr(listen, "_DIR_AMOSTRAS", str(tmp_path))
    monkeypatch.setattr(listen, "_MAX_AMOSTRAS", 3)

    feitos = [listen.guarda_amostra(i, _pcm(0.5)) for i in range(5)]

    assert [bool(c) for c in feitos] == [True, True, True, False, False]
    assert len(list(tmp_path.glob("*.wav"))) == 3


def test_falha_de_disco_nunca_derruba_a_escuta(tmp_path, monkeypatch):
    arquivo = tmp_path / "nao-e-pasta"
    arquivo.write_text("x")
    monkeypatch.setattr(listen, "_DIR_AMOSTRAS", str(arquivo))

    assert listen.guarda_amostra(1, _pcm(0.5)) == ""


def test_coleta_vem_desligada():
    import config

    assert config.VOICE_COLETA is False
