"""Prefetch: profundidade, autoplay e mudanças de fila.

Logs de uso real (12/09) mostraram o prefetch funcionando — faixas 2 e 4
tocaram em 0.0s — mas duas ficaram caras:

  07:02:20   9.6s  (extração  8.4s)   primeira da sessão, esperado
  07:04:18  15.2s  (extração 14.4s)   veio depois de um `shuffle` + `skip`

O `shuffle` reordena a fila e invalida o que estava adiantado, e nada
reprograma o prefetch. E com autoplay ligado a fila fica VAZIA durante a
música, então não há o que adiantar: a próxima só é buscada quando a atual
termina — os ~9s de silêncio entre faixas que o Otavio relatou.
"""
import asyncio

from apoio import faz_player
from core.player import GuildPlayer
from core.queue import MusicQueue, Track


class FakeVoice:
    def is_connected(self):
        return True

    def is_playing(self):
        return False

    def is_paused(self):
        return False


class FakeGuild:
    id = 1

    def __init__(self):
        self.voice_client = FakeVoice()


class FakePanel:
    async def ensure_visible(self, channel):
        pass

    async def refresh(self):
        pass


def _player() -> GuildPlayer:
    p = faz_player(guild=FakeGuild(), panel=FakePanel())
    return p


def _fill(p: GuildPlayer, n: int) -> None:
    for i in range(n):
        p.queue.add(Track(title=f"M{i}", video_id=f"v{i}"))


def _espiona_prefetch(p: GuildPlayer) -> list:
    adiantadas = []

    async def fake(track):
        adiantadas.append(track.video_id)

    p._prefetch = fake
    return adiantadas


async def _roda_prefetch(p: GuildPlayer) -> None:
    p._schedule_prefetch()
    if p._prefetch_task is not None:
        await p._prefetch_task


def test_adianta_mais_de_uma_faixa():
    """Dois /skip seguidos furavam o adiantamento de 1 e pagavam 8,3s."""
    p = _player()
    _fill(p, 5)
    adiantadas = _espiona_prefetch(p)

    asyncio.run(_roda_prefetch(p))

    assert len(adiantadas) >= 2
    assert adiantadas == [f"v{i}" for i in range(len(adiantadas))]


def test_fila_menor_que_a_profundidade_nao_quebra():
    p = _player()
    _fill(p, 1)
    adiantadas = _espiona_prefetch(p)

    asyncio.run(_roda_prefetch(p))

    assert adiantadas == ["v0"]


def test_fila_vazia_sem_autoplay_nao_faz_nada():
    p = _player()
    adiantadas = _espiona_prefetch(p)

    asyncio.run(_roda_prefetch(p))

    assert adiantadas == []


def test_autoplay_enche_a_RADIO_antes_da_faixa_acabar(monkeypatch):
    """Sem isso são ~9s de silêncio entre faixas — mas vai para a rádio, não
    para a fila da pessoa (senão fura o que ela pediu)."""
    p = _player()
    p.autoplay_enabled = True
    adiantadas = _espiona_prefetch(p)

    async def relacionadas(recent_ids, **_):
        return [Track(title="Rádio 1", video_id="r1", from_autoplay=True),
                Track(title="Rádio 2", video_id="r2", from_autoplay=True)]

    monkeypatch.setattr("core.player.fetch_autoplay_tracks", relacionadas)

    asyncio.run(_roda_prefetch(p))

    assert [t.video_id for t in p.queue.radio] == ["r1", "r2"]
    assert p.queue.upcoming == []  # a fila da pessoa segue intocada
    assert adiantadas[:2] == ["r1", "r2"]  # e já saem adiantadas


def test_autoplay_nao_busca_com_a_fila_cheia(monkeypatch):
    """Com 50 pedidas, buscar rádio agora é trabalho jogado fora."""
    p = _player()
    p.autoplay_enabled = True
    _fill(p, 10)
    _espiona_prefetch(p)
    buscou = []

    async def relacionadas(recent_ids, **_):
        buscou.append(1)
        return []

    monkeypatch.setattr("core.player.fetch_autoplay_tracks", relacionadas)

    asyncio.run(_roda_prefetch(p))

    assert buscou == []


def test_autoplay_busca_quando_a_fila_esta_acabando(monkeypatch):
    """Com a última música na fila, a rádio precisa estar pronta."""
    p = _player()
    p.autoplay_enabled = True
    _fill(p, 1)
    _espiona_prefetch(p)

    async def relacionadas(recent_ids, **_):
        return [Track(title="Rádio", video_id="r1", from_autoplay=True)]

    monkeypatch.setattr("core.player.fetch_autoplay_tracks", relacionadas)

    asyncio.run(_roda_prefetch(p))

    assert [t.video_id for t in p.queue.radio] == ["r1"]


def test_autoplay_que_falha_nao_derruba_o_prefetch(monkeypatch):
    p = _player()
    p.autoplay_enabled = True
    _espiona_prefetch(p)

    async def explode(recent_ids, **_):
        raise RuntimeError("YouTube Music fora")

    monkeypatch.setattr("core.player.fetch_autoplay_tracks", explode)

    asyncio.run(_roda_prefetch(p))  # não pode levantar


def test_mudanca_de_fila_reprograma_o_adiantamento():
    """`shuffle` invalidava o adiantado e ninguém reprogramava (os 14,4s)."""
    p = _player()
    p.guild.voice_client.is_playing = lambda: True  # embaralha COM música tocando
    _fill(p, 5)
    adiantadas = _espiona_prefetch(p)

    async def run():
        p.queue_changed()
        if p._prefetch_task is not None:
            await p._prefetch_task

    asyncio.run(run())

    assert adiantadas[:1] == ["v0"]


def test_mudanca_de_fila_parado_nao_dispara_nada():
    """Sem música tocando, quem resolve a próxima é o play_next."""
    p = _player()
    _fill(p, 5)
    adiantadas = _espiona_prefetch(p)

    p.queue_changed()

    assert p._prefetch_task is None
    assert adiantadas == []


def test_prefetch_preenche_duracao_e_capa_da_faixa(monkeypatch):
    """Faixa criada pelo oEmbed (/play <link>) chega sem duração: o
    adiantamento, que já extraiu tudo, preenche o que faltava."""
    import core.player as player_mod

    p = _player()
    track = Track(title="Rápido", video_id="v1", duration=0, thumbnail="")

    async def stream(video_id, **kwargs):
        return {"video_id": video_id, "title": "T", "artist": "A", "duration": 212,
                "thumbnail": "http://capa", "stream_url": "http://x"}

    monkeypatch.setattr(player_mod.youtube, "get_stream", stream)

    asyncio.run(p._prefetch(track))

    assert track.duration == 212
    assert track.thumbnail == "http://capa"


# ── teste do Otavio de 21/09: comandos de voz por POSIÇÃO ───────────────────
# "toca a terceira", "pula três" e "pula pra quinta" caíram todas fora das 2
# adiantadas: de 5,4 a 8,6s até a música trocar. Com a faixa no cache (as duas
# vezes em que aconteceu), 1,1 a 1,5s. A extração hoje leva ~4,0s no journal — a
# profundidade 2 foi escolhida quando levava 8,3s.


def test_a_fila_pedida_e_adiantada_ate_a_quarta():
    p = _player()
    _fill(p, 10)
    adiantadas = _espiona_prefetch(p)

    asyncio.run(_roda_prefetch(p))

    assert adiantadas == ["v0", "v1", "v2", "v3"]


def test_a_radio_segue_adiantada_so_ate_a_segunda():
    """Ninguém pede "a quarta" da rádio — e ela é refeita a cada pedido."""
    p = _player()
    p.queue.add_radio([Track(title=f"R{i}", video_id=f"r{i}", from_autoplay=True) for i in range(6)])
    adiantadas = _espiona_prefetch(p)

    asyncio.run(_roda_prefetch(p))

    assert adiantadas == ["r0", "r1"]


def test_fila_pedida_curta_completa_com_a_radio_so_ate_duas():
    p = _player()
    _fill(p, 1)
    p.queue.add_radio([Track(title=f"R{i}", video_id=f"r{i}", from_autoplay=True) for i in range(6)])
    adiantadas = _espiona_prefetch(p)

    asyncio.run(_roda_prefetch(p))

    assert adiantadas == ["v0", "r0"]
