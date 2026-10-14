"""Ligar/desligar o autoplay tem de mexer na rádio, não só numa flag.

Três lugares (botão do painel, /autoplay, comando de voz) faziam
`player.autoplay_enabled = not player.autoplay_enabled` e mais nada. Com a
rádio numa fila separada isso virou dois bugs visíveis:

  - DESLIGAR deixava as faixas de rádio já enfileiradas — elas tocavam
    mesmo com o autoplay "parado e triste";
  - LIGAR no meio de uma música não enchia a rádio: o prefetch só roda no
    início de cada faixa, então a primeira transição pagava busca + ~8s de
    extração em silêncio.

E ligar com o bot parado na call (fila acabou) não fazia nada até alguém
mandar um /play — a pessoa ligava a rádio e ficava o silêncio.
"""
import asyncio

from apoio import faz_player
from core.player import GuildPlayer
from core.queue import MusicQueue, Track


class FakeVoice:
    def __init__(self, playing: bool = False, connected: bool = True) -> None:
        self.playing = playing
        self.connected = connected

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return False


class FakeGuild:
    id = 1

    def __init__(self, voice) -> None:
        self.voice_client = voice


class FakePanel:
    async def ensure_visible(self, channel):
        pass

    async def refresh(self):
        pass


def _player(voice: FakeVoice | None = None) -> GuildPlayer:
    p = faz_player(guild=FakeGuild(voice or FakeVoice()), panel=FakePanel())
    return p


def _radio(nome: str) -> Track:
    return Track(title=nome, video_id=nome, from_autoplay=True)


async def _noop():
    pass


def test_desligar_descarta_a_radio_enfileirada():
    p = _player(FakeVoice(playing=True))
    p.autoplay_enabled = True
    p.queue.add(Track(title="minha", video_id="m1"))
    p.queue.add_radio([_radio("r1"), _radio("r2")])
    p._prefetch = lambda track: _noop()

    async def run():
        p.set_autoplay(False)  # tocando: reprograma o prefetch (precisa do loop)
        await asyncio.sleep(0)

    asyncio.run(run())

    assert p.autoplay_enabled is False
    assert p.queue.radio == []
    assert [t.video_id for t in p.queue.upcoming] == ["m1"]  # a da pessoa fica


def test_ligar_tocando_reprograma_o_adiantamento():
    """A rádio precisa encher AGORA, não no fim da faixa."""
    p = _player(FakeVoice(playing=True))
    agendou = []
    p._schedule_prefetch = lambda: agendou.append(1)

    async def run():
        p.set_autoplay(True)

    asyncio.run(run())

    assert p.autoplay_enabled is True
    assert agendou == [1]


def test_ligar_parado_na_call_comeca_a_radio(monkeypatch):
    """Fila acabou, bot na call, pessoa liga o autoplay: tem de tocar."""
    p = _player(FakeVoice(playing=False, connected=True))
    p._recent_ids = ["ultima"]
    chamadas = []

    async def fake_play_next(**kwargs):
        chamadas.append(1)

    p.play_next = fake_play_next

    async def run():
        p.set_autoplay(True)
        await asyncio.sleep(0)  # deixa a task agendada rodar

    asyncio.run(run())

    assert chamadas == [1]


def test_ligar_parado_sem_historico_nao_tenta_tocar():
    """Sem uma faixa-semente não há rádio para buscar."""
    p = _player(FakeVoice(playing=False, connected=True))
    chamadas = []

    async def fake_play_next(**kwargs):
        chamadas.append(1)

    p.play_next = fake_play_next

    async def run():
        p.set_autoplay(True)
        await asyncio.sleep(0)

    asyncio.run(run())

    assert chamadas == []


def test_ligar_fora_da_call_nao_faz_nada_alem_da_flag():
    p = _player(FakeVoice(playing=False, connected=False))
    p._recent_ids = ["ultima"]
    chamadas = []

    async def fake_play_next(**kwargs):
        chamadas.append(1)

    p.play_next = fake_play_next

    async def run():
        p.set_autoplay(True)
        await asyncio.sleep(0)

    asyncio.run(run())

    assert p.autoplay_enabled is True
    assert chamadas == []


def test_repetir_o_mesmo_estado_e_inofensivo():
    p = _player(FakeVoice(playing=True))
    p.autoplay_enabled = True
    p.queue.add_radio([_radio("r1")])
    agendou = []
    p._schedule_prefetch = lambda: agendou.append(1)

    p.set_autoplay(True)

    assert p.queue.radio  # não limpou
    assert agendou == []  # nem reprogramou


def test_breaker_que_desliga_o_autoplay_tambem_limpa_a_radio():
    """Depois de 5 falhas o breaker desliga a rádio — o que sobrou dela não
    pode continuar na fila, senão o próximo /play toca rádio "desligada"."""
    p = _player(FakeVoice(playing=False))
    p.autoplay_enabled = True
    p.queue.add_radio([_radio(f"r{i}") for i in range(10)])

    async def sempre_falha(track):
        return False

    p._try_start = sempre_falha
    asyncio.run(p.play_next())

    assert p.autoplay_enabled is False
    assert p.queue.radio == []


# ── corridas apontadas na revisão de 12/09 ───────────────────────────────────


def _prefetch_lento(monkeypatch, p: GuildPlayer, atraso: float) -> list:
    """fetch_autoplay_tracks que demora `atraso`s; devolve o log de extrações."""
    import core.player as player_mod

    async def relacionadas(recent_ids, **_):
        await asyncio.sleep(atraso)
        return [_radio(f"r{i}") for i in range(3)]

    monkeypatch.setattr(player_mod, "fetch_autoplay_tracks", relacionadas)
    extraidas = []

    async def fake_prefetch(track):
        extraidas.append(track.video_id)

    p._prefetch = fake_prefetch
    return extraidas


def test_desligar_durante_a_busca_da_radio_nao_deixa_a_radio_encher(monkeypatch):
    """Prefetch em `await fetch_autoplay_tracks` (0,5–2s na VM); a pessoa
    desliga no meio. A busca volta e enchia a rádio com autoplay desligado —
    e ainda gastava o worker único extraindo faixas que não deviam tocar."""
    p = _player(FakeVoice(playing=True))
    p.autoplay_enabled = True
    extraidas = _prefetch_lento(monkeypatch, p, atraso=0.05)

    async def run():
        p._schedule_prefetch()
        await asyncio.sleep(0.01)
        p.set_autoplay(False)
        await asyncio.sleep(0.1)

    asyncio.run(run())

    assert p.queue.radio == []
    assert extraidas == []


def test_desligar_tocando_mantem_a_fila_da_pessoa_adiantada(monkeypatch):
    p = _player(FakeVoice(playing=True))
    p.autoplay_enabled = True
    p.queue.add(Track(title="minha", video_id="m1"))
    extraidas = _prefetch_lento(monkeypatch, p, atraso=0.05)

    async def run():
        p.set_autoplay(False)
        await asyncio.sleep(0.02)

    asyncio.run(run())

    assert extraidas == ["m1"]


def test_desligar_durante_o_avanco_nao_toca_radio(monkeypatch):
    """Faixa acabou com a fila vazia: play_next está em `await
    fetch_autoplay_tracks`; a pessoa desliga → a rádio não pode entrar."""
    import core.player as player_mod

    p = _player(FakeVoice(playing=False))
    p.autoplay_enabled = True
    p._recent_ids = ["semente"]

    async def relacionadas(recent_ids, **_):
        await asyncio.sleep(0.05)
        return [_radio("r1")]

    monkeypatch.setattr(player_mod, "fetch_autoplay_tracks", relacionadas)
    tocadas = []

    async def fake_try_start(track):
        tocadas.append(track.video_id)
        return True

    p._try_start = fake_try_start

    async def run():
        task = asyncio.create_task(p.play_next())
        await asyncio.sleep(0.01)
        p.set_autoplay(False)
        await task

    asyncio.run(run())

    assert tocadas == []
    assert p.queue.radio == []


def test_loop_de_fila_com_radio_tocando_e_autoplay_desligado_nao_repete():
    """Pré-existente: em loop de fila, a faixa de rádio atual voltava para a
    rádio no fim e tocava para sempre, mesmo com o autoplay desligado."""
    from core.queue import LoopMode

    p = _player(FakeVoice(playing=False))
    p.queue.loop_mode = LoopMode.QUEUE
    p.queue.current = _radio("tocando")
    p.autoplay_enabled = False
    tocadas = []

    async def fake_try_start(track):
        tocadas.append(track.video_id)
        return True

    p._try_start = fake_try_start
    asyncio.run(p.play_next())

    assert tocadas == []
    assert p.queue.radio == []


def test_breaker_cancela_o_prefetch_em_voo():
    """Depois de "playback quebrado" o prefetch não pode seguir extraindo
    faixas de uma rádio que acabou de ser desligada."""
    p = _player(FakeVoice(playing=False))
    p.autoplay_enabled = True
    p.queue.add_radio([_radio(f"r{i}") for i in range(6)])
    extraidas = []

    async def prefetch_lento(track):
        await asyncio.sleep(0.05)  # extração em andamento quando o breaker dispara
        extraidas.append(track.video_id)

    p._prefetch = prefetch_lento

    async def sempre_falha(track):
        return False

    p._try_start = sempre_falha

    async def run():
        p._schedule_prefetch()
        await asyncio.sleep(0.01)
        await p.play_next()
        await asyncio.sleep(0.1)

    asyncio.run(run())

    assert p.queue.radio == []
    assert extraidas == []
