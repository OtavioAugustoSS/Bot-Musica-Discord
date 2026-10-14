"""A validade do cache vem do `expire=` da própria URL, não de 3h fixas.

As URLs do googlevideo trazem `expire=<epoch>` (~6h à frente). Com 3h fixas o
bot jogava fora metade da vida útil de cada extração — e extrair é a operação
mais lenta (4–5s) e mais cara em CPU (deno) que ele faz. Numa noite de uso as
mesmas músicas voltam: mais acerto de cache = menos faixa fria.

A margem é o tamanho da faixa: uma URL só serve se ainda valer até a música
ACABAR (o CDN derruba a conexão a cada ~31s e cada reconexão revalida o expire).
"""
import pytest

from services import youtube

AGORA_REAL = 1_789_900_000.0


@pytest.fixture
def relogios(monkeypatch):
    mono = {"t": 5000.0}
    monkeypatch.setattr(youtube, "_now", lambda: mono["t"])
    monkeypatch.setattr(youtube, "_relogio_real", lambda: AGORA_REAL + (mono["t"] - 5000.0))
    youtube._stream_cache.clear()
    yield mono
    youtube._stream_cache.clear()


def _info(video_id: str, *, expira_em_h: float | None, duracao: int = 200) -> dict:
    url = "https://rr1---sn-x.googlevideo.com/videoplayback?itag=251"
    if expira_em_h is not None:
        url += f"&expire={int(AGORA_REAL + expira_em_h * 3600)}&ei=abc"
    return {"video_id": video_id, "duration": duracao, "stream_url": url}


def test_url_de_6h_ainda_vale_depois_de_5h(relogios):
    youtube._cache_put("a", _info("a", expira_em_h=6))

    relogios["t"] += 5 * 3600
    assert youtube._cache_get("a") is not None, "com 3h fixas isto já teria sido jogado fora"


def test_url_sai_do_cache_antes_de_vencer(relogios):
    youtube._cache_put("a", _info("a", expira_em_h=6))

    relogios["t"] += 5 * 3600 + 45 * 60  # faltam 15min: menos que a margem de 30
    assert youtube._cache_get("a") is None


def test_faixa_longa_precisa_de_mais_folga(relogios):
    """Um set de 2h tocado a 1h do expire seria cortado no meio."""
    youtube._cache_put("mix", _info("mix", expira_em_h=6, duracao=2 * 3600))

    relogios["t"] += 3 * 3600 + 30 * 60  # sobram 2h30: dá para os 2h05 de folga
    assert youtube._cache_get("mix") is not None
    relogios["t"] += 45 * 60  # sobram 1h45: não dá
    assert youtube._cache_get("mix") is None


def test_url_sem_expire_continua_valendo_as_3h_de_antes(relogios):
    youtube._cache_put("a", _info("a", expira_em_h=None))

    relogios["t"] += youtube._STREAM_TTL_S - 60
    assert youtube._cache_get("a") is not None
    relogios["t"] += 120
    assert youtube._cache_get("a") is None


def test_url_ja_perto_de_vencer_nem_entra(relogios):
    youtube._cache_put("a", _info("a", expira_em_h=0.2))

    assert youtube._cache_get("a") is None


def test_expire_absurdo_tem_teto(relogios):
    """Defesa: um expire de anos à frente não pode virar cache eterno."""
    youtube._cache_put("a", _info("a", expira_em_h=24 * 365))

    relogios["t"] += 7 * 3600
    assert youtube._cache_get("a") is None


def test_validade_por_url_sobrevive_ao_restart(relogios, tmp_path):
    youtube._cache_put("a", _info("a", expira_em_h=6))
    caminho = str(tmp_path / "cache.json")
    assert youtube.dump_cache(caminho) == 1

    youtube._stream_cache.clear()
    relogios["t"] += 4 * 3600  # o bot ficou 4h fora
    assert youtube.load_cache(caminho) == 1
    assert youtube._cache_get("a") is not None
    relogios["t"] += 2 * 3600
    assert youtube._cache_get("a") is None
