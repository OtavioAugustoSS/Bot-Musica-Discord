"""URL recém-extraída: esperar ela abrir em vez de deixar o FFmpeg bater a cabeça.

Medido na VM em 20/09/2026 (seis extrações, acesso à URL depois de D segundos):

    D = 0s    403@0,3s  206@0,7s          D = 1,5s  206 de primeira
    D = 0s    403 403 403  206@1,3s       D = 3s    206 de primeira (2x)
                                          D = 6s    206 de primeira

O 403 é por TEMPO: a URL passa a valer 0,7–1,3s depois de extraída. O FFmpeg
re-tenta em 0s → 1s → 3s; quando erra a janela de 1s, a faixa fria só começa
aos ~4,4s (2 de 6 na medição anterior). Sondando a cada 0,25s o FFmpeg já
abre com a URL valendo — e o prefetch, cuja URL já envelheceu, não paga nada.
"""
import asyncio

import pytest

from services import youtube


@pytest.fixture
def relogio(monkeypatch):
    agora = [1000.0]
    monkeypatch.setattr(youtube, "_now", lambda: agora[0])

    async def dorme(segundos: float) -> None:
        agora[0] += segundos

    monkeypatch.setattr(youtube.asyncio, "sleep", dorme)
    return agora


def _sondas(monkeypatch, respostas: list) -> list:
    feitas = []

    async def sonda(url, tentativas=3, timeout_s=10.0):
        feitas.append(url)
        return [respostas.pop(0)]

    monkeypatch.setattr(youtube, "sonda_do_stream", sonda)
    return feitas


def test_url_do_prefetch_nao_e_sondada(monkeypatch, relogio):
    feitas = _sondas(monkeypatch, [])
    info = {"stream_url": "http://x", "extraida_em": 1000.0 - 30.0}

    assert asyncio.run(youtube.espera_stream_abrir(info)) == (0.0, 0)
    assert feitas == []


def test_info_sem_carimbo_nao_e_sondada(monkeypatch, relogio):
    """Cache salvo por versão antiga do bot, ou dublês de teste."""
    feitas = _sondas(monkeypatch, [])

    assert asyncio.run(youtube.espera_stream_abrir({"stream_url": "http://x"})) == (0.0, 0)
    assert feitas == []


def test_carimbo_do_futuro_nao_e_fresco(monkeypatch, relogio):
    """Depois de um reboot o monotonic recomeça: o carimbo salvo fica "no futuro"."""
    feitas = _sondas(monkeypatch, [])
    info = {"stream_url": "http://x", "extraida_em": 1000.0 + 5000.0}

    assert asyncio.run(youtube.espera_stream_abrir(info)) == (0.0, 0)
    assert feitas == []


def test_url_fresca_que_ja_abre_custa_uma_sonda(monkeypatch, relogio):
    feitas = _sondas(monkeypatch, [206])
    info = {"stream_url": "http://x", "extraida_em": 1000.0 - 0.2}

    esperou, recusas = asyncio.run(youtube.espera_stream_abrir(info))

    assert recusas == 0 and len(feitas) == 1 and esperou == 0.0


def test_url_fresca_recusada_e_sondada_de_quarto_em_quarto_de_segundo(monkeypatch, relogio):
    feitas = _sondas(monkeypatch, [403, 403, 403, 206])
    info = {"stream_url": "http://x", "extraida_em": 1000.0}

    esperou, recusas = asyncio.run(youtube.espera_stream_abrir(info))

    assert recusas == 3 and len(feitas) == 4
    assert esperou == pytest.approx(0.75)


def test_recusa_que_nao_passa_tem_teto_e_devolve_o_problema_ao_ffmpeg(monkeypatch, relogio):
    feitas = _sondas(monkeypatch, [403] * 100)
    info = {"stream_url": "http://x", "extraida_em": 1000.0}

    esperou, recusas = asyncio.run(youtube.espera_stream_abrir(info))

    assert 3.0 <= esperou <= 4.5, "não pode segurar a faixa para sempre"
    assert recusas == len(feitas) < 30


def test_erro_que_nao_e_recusa_nao_fica_esperando(monkeypatch, relogio):
    """Timeout, 404, 410: esperar não conserta — o FFmpeg tenta e o fim de faixa acusa."""
    for resposta in ("TimeoutError", 404, 410):
        feitas = _sondas(monkeypatch, [resposta, 206])
        info = {"stream_url": "http://x", "extraida_em": 1000.0}

        esperou, recusas = asyncio.run(youtube.espera_stream_abrir(info))

        assert len(feitas) == 1 and esperou == 0.0 and recusas == 0


def test_extracao_carimba_a_hora_em_que_a_url_nasceu(monkeypatch, relogio):
    voo = youtube._Voo("https://www.youtube.com/watch?v=abcdefghijk")

    async def roda():
        voo.job = type("J", (), {})()
        voo.job.future = asyncio.get_running_loop().create_future()
        voo.job.future.set_result({"id": "abcdefghijk", "url": "http://x", "title": "t"})
        return await youtube._resolve(voo)

    info = asyncio.run(roda())
    youtube.invalidate("abcdefghijk")

    assert info["extraida_em"] == 1000.0


# ── o que a extração sabe sobre o áudio ──────────────────────────────────────


def _resolve(monkeypatch, bruto: dict) -> dict:
    voo = youtube._Voo("https://www.youtube.com/watch?v=abcdefghijk")

    async def roda():
        voo.job = type("J", (), {})()
        voo.job.future = asyncio.get_running_loop().create_future()
        voo.job.future.set_result({"id": "abcdefghijk", "url": "http://x", "title": "t", **bruto})
        return await youtube._resolve(voo)

    try:
        return asyncio.run(roda())
    finally:
        youtube.invalidate("abcdefghijk")


def test_resolve_guarda_o_codec(monkeypatch, relogio):
    """Opus direto (sem decodificar/recodificar) só vale para Opus 48 kHz estéreo:
    a decisão sai dos metadados da extração, não de espiar o stream tocando."""
    info = _resolve(monkeypatch, {"format_id": "251", "acodec": "opus", "asr": 48000, "audio_channels": 2})

    assert (info["itag"], info["acodec"], info["asr"], info["canais"]) == ("251", "opus", 48000, 2)
    assert youtube.serve_para_opus_direto(info) is True


def test_formato_com_video_ou_aac_nao_serve_para_opus_direto(monkeypatch, relogio):
    """itag 18 (mp4, AAC + vídeo) é o que os perfis de último recurso entregam."""
    info = _resolve(monkeypatch, {"format_id": "18", "acodec": "mp4a.40.2", "asr": 44100, "audio_channels": 2})

    assert youtube.serve_para_opus_direto(info) is False


def test_opus_mono_nao_serve(monkeypatch, relogio):
    """`-c copy` não remixa: mono iria mono para o Discord."""
    info = _resolve(monkeypatch, {"format_id": "600", "acodec": "opus", "asr": 48000, "audio_channels": 1})

    assert youtube.serve_para_opus_direto(info) is False


def test_cache_antigo_sem_codec_vira_desconhecido():
    """O cache é persistido em disco: entradas de antes desta versão não têm as
    chaves novas. Desconhecido = caminho conservador."""
    assert youtube.serve_para_opus_direto({"stream_url": "http://x", "duration": 10}) is False
