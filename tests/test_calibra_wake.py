"""scripts/calibra_wake.py: a lógica que não depende da rede.

A calibração manda áudio de verdade à Groq; aqui ficam o que dá para provar sem
ela — a leitura dos WAV, o ritmo de 20 por minuto, o veredito do parser, o
resumo e as sugestões de variação — e o laço inteiro com a Groq trocada.
"""
import asyncio
import importlib.util
import json
import os
import wave

import numpy as np
import pytest

from core.intents import WAKE_WORD
from services import speech

_CAMINHO = os.path.join(os.path.dirname(__file__), "..", "scripts", "calibra_wake.py")
_spec = importlib.util.spec_from_file_location("calibra_wake", _CAMINHO)
calibra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibra)

TURBO, V3 = "whisper-large-v3-turbo", "whisper-large-v3"
COM, SEM = calibra.PROMPT_PRODUCAO, calibra.PROMPT_SEM_NOME


def _grava_wav(caminho, amostras: np.ndarray, taxa: int, canais: int) -> None:
    with wave.open(str(caminho), "wb") as wav:
        wav.setnchannels(canais)
        wav.setsampwidth(2)
        wav.setframerate(taxa)
        wav.writeframes(amostras.astype(np.int16).tobytes())


def _registro(texto, prompt=COM, modelo=TURBO, arquivo="a.wav", acao_da_1a=None):
    r = {"arquivo": arquivo, "duracao_s": 1.0, "modelo": modelo, "prompt": prompt, "texto": texto, "erro": None}
    r.update(calibra.veredito(texto, prompt, acao_da_1a))
    return r


# ── áudio ────────────────────────────────────────────────────────────────────


def test_wav_do_voice_debug_vira_o_que_o_bot_manda(tmp_path):
    """48 kHz estéreo (o formato do Discord): a mesma conversão do speech."""
    pcm = (np.sin(np.arange(48000 * 2) / 7.0) * 8000).astype(np.int16)
    _grava_wav(tmp_path / "seg.wav", pcm, 48000, 2)

    audio = calibra.audio_do_wav(str(tmp_path / "seg.wav"))

    assert audio.dtype == np.float32 and len(audio) == 16000
    np.testing.assert_array_equal(audio, speech.pcm_to_float_mono_16k(pcm.tobytes()))


def test_wav_da_coleta_16k_mono_passa_direto(tmp_path):
    amostras = np.full(16000, 16384, dtype=np.int16)
    _grava_wav(tmp_path / "wake.wav", amostras, 16000, 1)

    audio = calibra.audio_do_wav(str(tmp_path / "wake.wav"))

    assert len(audio) == 16000 and audio[0] == pytest.approx(0.5)


@pytest.mark.parametrize("taxa, canais", [(8000, 1), (44100, 2), (48000, 1)])
def test_outras_taxas_viram_16k_mono(tmp_path, taxa, canais):
    _grava_wav(tmp_path / "x.wav", np.zeros(taxa * canais, dtype=np.int16), taxa, canais)
    assert abs(len(calibra.audio_do_wav(str(tmp_path / "x.wav"))) - 16000) <= 1


def test_wav_que_nao_e_16_bits_e_recusado(tmp_path):
    with wave.open(str(tmp_path / "x.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(16000)
        wav.writeframes(b"\x80" * 1600)
    with pytest.raises(ValueError):
        calibra.audio_do_wav(str(tmp_path / "x.wav"))


def test_pasta_vira_os_wav_de_dentro_em_ordem(tmp_path):
    for nome in ("b.wav", "a.WAV", "notas.txt"):
        (tmp_path / nome).write_bytes(b"")
    solto = str(tmp_path / "solto.wav")

    assert [os.path.basename(c) for c in calibra.wavs_de([str(tmp_path), solto])] == ["a.WAV", "b.wav", "solto.wav"]


# ── ritmo ────────────────────────────────────────────────────────────────────


def test_ritmo_fica_abaixo_de_20_por_minuto():
    envios = [float(t) for t in range(19)]  # 19 nos últimos 60s
    assert calibra.espera_para(envios, agora=30.0, rpm=19) == pytest.approx(30.0)  # o 1º sai da janela em 60
    assert calibra.espera_para(envios[:18], agora=30.0, rpm=19) == 0.0
    assert calibra.espera_para(envios, agora=61.0, rpm=19) == 0.0  # o 1º já saiu
    assert calibra.espera_para(envios, agora=30.0, rpm=0) == 0.0  # 0 desliga


# ── o veredito do bot ────────────────────────────────────────────────────────


def test_veredito_da_passada_com_o_nome():
    v = calibra.veredito("A Rodes, pula.", COM)

    assert v["comando"] == ["skip", ""]
    assert v["nome_ok"] and v["de_frente"]
    assert v["primeira"] == "arodes" and v["grafia"] == "arodes"  # o "a" solto, colado
    assert v["inicio"] == "arodes→'arodes' 0.92"
    assert v["nome_no_audio"] is None and v["mesmo_comando"] is None


def test_veredito_da_passada_sem_o_nome():
    v = calibra.veredito("Herodes pula", SEM, acao_da_1a="skip")
    assert v["nome_no_audio"] is True and v["mesmo_comando"] is True

    v = calibra.veredito("Arroz, pula.", SEM, acao_da_1a="skip")
    assert v["nome_no_audio"] is False and v["mesmo_comando"] is True
    assert not v["nome_ok"] and v["comando"] is None


# ── resumo e sugestões ───────────────────────────────────────────────────────


def _sessao():
    return [
        _registro("Arrodes, pula.", COM, arquivo="1.wav"),
        _registro("Herodes, pula.", SEM, arquivo="1.wav", acao_da_1a="skip"),
        _registro("Rode, pula.", COM, arquivo="2.wav"),            # o nome dito pela metade
        _registro("Rode, pula.", SEM, arquivo="2.wav"),
        _registro("Haroldo, próxima.", COM, arquivo="3.wav"),      # grafia nova, não é palavra
        _registro("Arroz, próxima.", SEM, arquivo="3.wav", acao_da_1a=None),
        _registro("Arrotes, pula.", COM, arquivo="4.wav"),         # bloqueada de propósito
        _registro("Toca Djavan.", COM, arquivo="5.wav"),           # o nome sumiu
        {"arquivo": "6.wav", "duracao_s": 1.0, "modelo": V3, "prompt": COM, "texto": "", "erro": "HTTP 500"},
    ]


def test_resumo_conta_acertos_e_a_ponta_a_ponta():
    res = calibra.resumo(_sessao())

    assert res["taxas"][f"{TURBO} com nome"] == {"n": 5, "acertos": 1}
    assert res["taxas"][f"{TURBO} sem nome"] == {"n": 3, "acertos": 1}
    # 1.wav: a 1ª leu skip e a 2ª ouviu "Herodes" — o único que o bot atenderia
    assert res["ponta_a_ponta"][TURBO] == {"n": 3, "atendidas": 1}
    assert res["primeiras"][COM] == {"arrodes": 1, "rode": 1, "haroldo": 1, "arrotes": 1, "toca": 1}
    assert res["nome_sumiu"] == 1  # "Toca Djavan": nada parecido com o nome
    assert res["erros"] == 1


def test_sugestoes_marcam_palavra_do_portugues_e_bloqueada():
    sugs = {s["grafia"]: s for s in calibra.sugestoes(_sessao())}

    assert set(sugs) == {"rode", "haroldo", "arroz", "arrotes"}
    assert sugs["rode"]["vezes"] == 2 and sugs["rode"]["prompts"] == {COM: 1, SEM: 1}
    assert sugs["rode"]["palavra_pt"] and not sugs["rode"]["bloqueada"]
    assert not sugs["haroldo"]["palavra_pt"]
    assert sugs["arrotes"]["bloqueada"] and sugs["arrotes"]["palavra_pt"]
    assert not any(s["ja_aceita"] for s in sugs.values())
    assert calibra.sugestoes(_sessao())[0]["grafia"] == "rode"  # a mais vista primeiro


def test_sugestao_ja_aceita_e_problema_de_posicao():
    """A grafia passa no parser, mas a 2ª escuta olha só as 4 primeiras palavras,
    muletas inclusive: variação nova não resolveria."""
    r = _registro("Ô, então, tá, a Herodes, pula.", SEM)
    assert r["nome_no_audio"] is False and r["comando"] == ["skip", ""]
    assert calibra.sugestoes([r])[0]["ja_aceita"]


def test_dicionario_marca_mais_palavras(tmp_path):
    (tmp_path / "dic.txt").write_text("Haroldo\nÁrvore/XY\n", encoding="utf-8")

    palavras = calibra.carrega_palavras_pt([str(tmp_path / "dic.txt"), str(tmp_path / "nao-existe")])

    assert {"haroldo", "arvore", "rode"} <= palavras
    sugs = {s["grafia"]: s for s in calibra.sugestoes(_sessao(), palavras)}
    assert sugs["haroldo"]["palavra_pt"]


def test_resumo_formatado_aponta_as_candidatas_limpas():
    texto = calibra.formata_resumo(calibra.resumo(_sessao()), calibra.sugestoes(_sessao()))

    assert f"WAKE_WORD={WAKE_WORD!r}" in texto
    assert "PALAVRA DO PORTUGUÊS" in texto
    assert "candidatas limpas: haroldo" in texto
    assert "o nome sumiu" in texto


def test_resumo_sem_sugestoes():
    sessao = [_registro("Arrodes, pula.", COM)]
    assert "nenhuma" in calibra.formata_resumo(calibra.resumo(sessao), calibra.sugestoes(sessao))


# ── o laço, com a Groq trocada ───────────────────────────────────────────────


def test_calibra_passa_pelos_dois_modelos_e_prompts(tmp_path, monkeypatch, capsys):
    _grava_wav(tmp_path / "seg.wav", np.zeros(48000 * 2, dtype=np.int16), 48000, 2)
    pedidos = []

    async def groq(audio, wav, contexto, modelo, prompt=None, transborda=False):
        pedidos.append((modelo, prompt))
        if modelo == V3:
            raise speech._CotaDoModeloAcabou()
        return "Arrodes, pula." if prompt is None else "Herodes, pula."

    monkeypatch.setattr(speech, "_transcribe_no_modelo", groq)
    monkeypatch.setattr(speech, "_cota", {})
    saida = tmp_path / "saida.jsonl"

    with open(saida, "w", encoding="utf-8") as fh:
        registros = asyncio.run(calibra.calibra(
            [str(tmp_path / "seg.wav")], [TURBO, V3], [COM, SEM], rpm=19, saida=fh,
        ))

    # produção vai com o prompt do bot (None = VOICE_STT_PROMPT); a 2ª, sem o nome;
    # o v3 sem cota não é chamado de novo
    assert pedidos == [(TURBO, None), (V3, None), (TURBO, speech._prompt_sem_nome())]
    assert WAKE_WORD not in pedidos[2][1].lower()
    assert [r["erro"] for r in registros] == [None, "a cota do dia do modelo acabou", None]
    assert registros[2]["mesmo_comando"] is True  # a 1ª leu skip no mesmo modelo
    linhas = [json.loads(linha) for linha in saida.read_text(encoding="utf-8").splitlines()]
    assert [r["modelo"] for r in linhas] == [TURBO, V3, TURBO]
    assert "herodes→'herodes' 0.71" in capsys.readouterr().out


def test_sem_chave_da_groq_nao_comeca(monkeypatch, capsys):
    monkeypatch.setattr(calibra.config, "GROQ_API_KEY", "")
    assert calibra.main([]) == 2
    assert "GROQ_API_KEY" in capsys.readouterr().out
