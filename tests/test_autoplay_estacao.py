"""A rádio fica PRESA à música que a pessoa pediu, não à última que a rádio tocou.

Pedido do Otavio em 27/09/2026: "ele pega 3 músicas com base na que está tocando
e, depois de chegar na última da fila do autoplay, gera mais com base na última
música — e não traz mais recomendações com base na que pedi originalmente".
No journal: rádio ligada em "Amigo da Minha Saudade" (49 relacionadas), usou 3;
o lote seguinte já saiu de "Baqueado" (Panda) — cada lote um passo mais longe.

Agora a semente é a última faixa PEDIDA; as 49 da rádio dela são a estação,
consumida em ordem sem repetir. Esgotou: estende com a rádio das faixas da
própria estação (um passo da original, nunca a cadeia).
"""
import asyncio

import pytest

from core import autoplay


def _faixa(vid: str) -> dict:
    return {"title": vid, "artist": "x", "video_id": vid, "duration": 200, "thumbnail": ""}


@pytest.fixture
def ytm(monkeypatch):
    autoplay._estacoes.clear()
    radios: dict[str, list[str]] = {}
    pedidas: list[str] = []

    async def related_tracks(video_id: str, limit: int = 5) -> list[dict]:
        pedidas.append(video_id)
        return [_faixa(v) for v in radios.get(video_id, [])][:limit]

    monkeypatch.setattr(autoplay.youtube, "related_tracks", related_tracks)
    yield radios, pedidas
    autoplay._estacoes.clear()


def _ids(tracks) -> list[str]:
    return [t.video_id for t in tracks]


def test_lotes_seguintes_continuam_na_estacao_da_pedida(ytm):
    radios, pedidas = ytm
    radios["pedida"] = [f"p{i}" for i in range(10)]
    radios["p2"] = ["longe1", "longe2", "longe3"]

    primeiro = asyncio.run(autoplay.fetch_autoplay_tracks(["pedida"], semente="pedida"))
    tocadas = ["pedida"] + _ids(primeiro)
    segundo = asyncio.run(autoplay.fetch_autoplay_tracks(tocadas, semente="pedida"))

    assert _ids(primeiro) == ["p0", "p1", "p2"]
    assert _ids(segundo) == ["p3", "p4", "p5"], "antes: a rádio de p2, a última tocada"
    assert pedidas == ["pedida"], "a estação é buscada uma vez só"


def test_nao_repete_o_que_ja_deu_mesmo_se_nao_tocou(ytm):
    """Faixa da rádio que falhou na extração não entra em recent_ids."""
    radios, _ = ytm
    radios["pedida"] = [f"p{i}" for i in range(6)]

    asyncio.run(autoplay.fetch_autoplay_tracks(["pedida"], semente="pedida"))
    segundo = asyncio.run(autoplay.fetch_autoplay_tracks(["pedida"], semente="pedida"))
    assert _ids(segundo) == ["p3", "p4", "p5"]


def test_pular_as_ja_tocadas(ytm):
    radios, _ = ytm
    radios["pedida"] = ["a", "b", "c", "d"]
    assert _ids(asyncio.run(autoplay.fetch_autoplay_tracks(["b", "pedida"], semente="pedida"))) == ["a", "c", "d"]


def test_estacao_esgotada_estende_com_as_da_propria_estacao(ytm):
    radios, pedidas = ytm
    radios["pedida"] = ["p0", "p1", "p2", "p3"]
    radios["p0"] = ["p1", "q0", "q1", "q2"]

    asyncio.run(autoplay.fetch_autoplay_tracks(["pedida"], semente="pedida"))
    segundo = asyncio.run(autoplay.fetch_autoplay_tracks(["pedida", "p0", "p1", "p2"], semente="pedida"))
    assert _ids(segundo) == ["p3", "q0", "q1"]
    assert pedidas == ["pedida", "p0"], "estende pela 1ª da estação, não pela última tocada"


def test_semente_nova_troca_de_estacao(ytm):
    radios, _ = ytm
    radios["a"] = ["a1", "a2", "a3", "a4"]
    radios["b"] = ["b1", "b2", "b3"]

    asyncio.run(autoplay.fetch_autoplay_tracks(["a"], semente="a"))
    assert _ids(asyncio.run(autoplay.fetch_autoplay_tracks(["a", "b"], semente="b"))) == ["b1", "b2", "b3"]


def test_sem_semente_usa_a_ultima_tocada(ytm):
    radios, _ = ytm
    radios["ultima"] = ["u1"]
    assert _ids(asyncio.run(autoplay.fetch_autoplay_tracks(["x", "ultima"]))) == ["u1"]


def test_ytm_fora_nao_derruba(ytm, monkeypatch):
    async def explode(video_id, limit=5):
        raise RuntimeError("fora")

    monkeypatch.setattr(autoplay.youtube, "related_tracks", explode)
    assert asyncio.run(autoplay.fetch_autoplay_tracks(["a"], semente="a")) == []


# ── o player: quem é a semente ──────────────────────────────────────────────


def _player():
    from apoio import faz_player

    class Guild:
        id = 1
        voice_client = None

    class Panel:
        async def refresh(self):
            pass

    return faz_player(guild=Guild(), panel=Panel())


def test_musica_pedida_vira_semente_e_descarta_a_radio_velha():
    from core.queue import Track

    p = _player()
    p.queue.add_radio([Track(title="velha", video_id="velha", from_autoplay=True)])
    p._ancora_radio(Track(title="Pedida", video_id="pedida", requester_id=7))

    assert p._semente_radio == "pedida"
    assert p.queue.radio == [], "senão, depois da pedida, voltavam as sugestões da anterior"


def test_faixa_da_radio_nao_muda_a_semente():
    from core.queue import Track

    p = _player()
    p._ancora_radio(Track(title="Pedida", video_id="pedida", requester_id=7))
    p.queue.add_radio([Track(title="r2", video_id="r2", from_autoplay=True)])
    p._ancora_radio(Track(title="r1", video_id="r1", from_autoplay=True))

    assert p._semente_radio == "pedida"
    assert len(p.queue.radio) == 1
