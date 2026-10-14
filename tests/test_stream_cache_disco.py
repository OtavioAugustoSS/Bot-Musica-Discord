"""O cache de streams sobrevive a um restart.

As URLs do googlevideo valem ~6h e o cache guarda 3h. Um deploy jogava tudo
fora e a próxima faixa pagava os ~8,3s de extração de novo — com o pool
serializado, isso é silêncio no canal logo depois de subir.

O detalhe que faz isso funcionar: o cache marca idade com `time.monotonic()`,
que ZERA a cada processo. Para persistir é preciso converter para relógio de
parede na saída e de volta na entrada.
"""
import json

import pytest

from services import youtube


@pytest.fixture(autouse=True)
def _limpa():
    youtube._stream_cache.clear()
    yield
    youtube._stream_cache.clear()


def _info(vid: str) -> dict:
    return {"video_id": vid, "title": vid, "stream_url": f"https://stream/{vid}"}


def test_salva_e_recarrega(tmp_path):
    caminho = str(tmp_path / "cache.json")
    youtube._cache_put("a", _info("a"))
    youtube._cache_put("b", _info("b"))

    assert youtube.dump_cache(caminho) == 2

    youtube._stream_cache.clear()
    assert youtube.load_cache(caminho) == 2
    assert youtube._cache_get("a") == _info("a")
    assert youtube._cache_get("b") == _info("b")


def test_entrada_vencida_nao_volta(tmp_path, monkeypatch):
    caminho = str(tmp_path / "cache.json")
    relogio = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: relogio["t"])

    youtube._cache_put("velha", _info("velha"))
    relogio["t"] += youtube._STREAM_TTL_S - 60  # falta 1min de validade
    youtube._cache_put("nova", _info("nova"))

    youtube.dump_cache(caminho)
    youtube._stream_cache.clear()

    # quatro horas depois: as duas já venceram (o TTL é de 3h)
    relogio_real = youtube.time.time
    depois = relogio_real() + 4 * 3600
    monkeypatch.setattr(youtube.time, "time", lambda: depois)
    assert youtube.load_cache(caminho) == 0


def test_validade_restante_e_preservada(tmp_path, monkeypatch):
    """Uma entrada com 10min de vida não pode voltar valendo 3h."""
    caminho = str(tmp_path / "cache.json")
    relogio = {"t": 5000.0}
    monkeypatch.setattr(youtube, "_now", lambda: relogio["t"])

    youtube._cache_put("x", _info("x"))
    relogio["t"] += youtube._STREAM_TTL_S - 600  # sobram 10min
    youtube.dump_cache(caminho)

    youtube._stream_cache.clear()
    relogio["t"] = 90000.0  # monotonic de um processo novo
    youtube.load_cache(caminho)

    assert youtube._cache_get("x") is not None
    relogio["t"] += 700  # passa dos 10min que sobravam
    assert youtube._cache_get("x") is None


def test_arquivo_inexistente_nao_quebra(tmp_path):
    assert youtube.load_cache(str(tmp_path / "nao-existe.json")) == 0


def test_arquivo_corrompido_nao_quebra(tmp_path):
    caminho = tmp_path / "cache.json"
    caminho.write_text("{isso não é json", encoding="utf-8")
    assert youtube.load_cache(str(caminho)) == 0


def test_conteudo_inesperado_nao_quebra(tmp_path):
    caminho = tmp_path / "cache.json"
    caminho.write_text(json.dumps({"a": "string no lugar do dict"}), encoding="utf-8")
    assert youtube.load_cache(str(caminho)) == 0


def test_dump_sem_diretorio_nao_quebra(tmp_path):
    assert youtube.dump_cache(str(tmp_path / "nao" / "existe" / "c.json")) == 0


def test_dump_de_cache_vazio(tmp_path):
    assert youtube.dump_cache(str(tmp_path / "c.json")) == 0
