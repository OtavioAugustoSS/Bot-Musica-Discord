"""Todo caminho que muda "quem é a próxima" reprograma o adiantamento.

O prefetch roda UMA vez, no início de cada faixa, para as 2 seguintes. Se a
fila muda depois disso, o que foi adiantado pode não ser mais a próxima:

  - /play durante uma rádia ou com a fila vazia: a música pedida vira a
    próxima e NÃO estava adiantada — a transição pagava os ~8,3s de
    extração em silêncio ("demora pra começar a tocar");
  - botão 🔀 do painel: o /shuffle de texto e o de voz já reprogramavam, o
    botão (o caminho mais usado) não;
  - remover pelo dropdown do /queue e o /clear: idem.

`queue_changed()` é barato quando nada mudou de fato (cache quente = zero
extração), então o custo de chamar demais é nenhum.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from apoio import faz_player
from core.player import GuildPlayer
from core.queue import MusicQueue, Track


class FakeVoice:
    def __init__(self, playing: bool = True) -> None:
        self.playing = playing

    def is_connected(self):
        return True

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


def _player(playing: bool = True) -> GuildPlayer:
    p = faz_player(guild=FakeGuild(FakeVoice(playing)), panel=FakePanel())
    return p


def _espiona(p: GuildPlayer) -> list:
    adiantadas = []

    async def fake(track):
        adiantadas.append(track.video_id)

    p._prefetch = fake
    return adiantadas


async def _espera_prefetch(p: GuildPlayer) -> None:
    if p._prefetch_task is not None:
        await p._prefetch_task


def test_enqueue_tocando_adianta_a_musica_nova():
    """O caso do relato: pedir uma música com a rádio tocando."""
    p = _player(playing=True)
    p.queue.add_radio([Track(title="r", video_id="r1", from_autoplay=True)])
    adiantadas = _espiona(p)

    async def run():
        await p.enqueue([Track(title="pedida", video_id="p1")], None)
        await _espera_prefetch(p)

    asyncio.run(run())

    assert adiantadas[:1] == ["p1"]  # a pedida agora é a próxima — e já quente


def test_enqueue_parado_nao_agenda_prefetch():
    """Parado, quem resolve a próxima é o play_next (não duplicar trabalho)."""
    p = _player(playing=False)
    _espiona(p)
    chamadas = []

    async def fake_play_next(**kwargs):
        chamadas.append(1)

    p.play_next = fake_play_next
    asyncio.run(p.enqueue([Track(title="x", video_id="x1")], None))

    assert chamadas == [1]
    assert p._prefetch_task is None


def test_botao_shuffle_do_painel_reprograma():
    from core.panel import PlayerView

    p = _player(playing=True)
    for i in range(5):
        p.queue.add(Track(title=f"m{i}", video_id=f"m{i}"))
    reprogramou = []
    p.queue_changed = lambda origem=None: reprogramou.append(origem)
    p.panel = MagicMock()

    canal = MagicMock()
    canal.members = []  # sozinho com o bot: control_error libera
    p.guild.voice_client.channel = canal

    async def run():
        view = PlayerView(p)
        interaction = MagicMock()
        interaction.user.voice.channel = canal
        interaction.response.edit_message = AsyncMock()
        await view._shuffle(interaction)

    asyncio.run(run())

    assert reprogramou == ["botão embaralhar"]


def test_remover_pelo_dropdown_do_queue_reprograma():
    from cogs.music import QueueView

    p = _player(playing=True)
    for i in range(3):
        p.queue.add(Track(title=f"m{i}", video_id=f"m{i}", requester_id=7))
    reprogramou = []
    p.queue_changed = lambda origem=None: reprogramou.append(origem)
    p.panel = MagicMock()
    p.panel.refresh = AsyncMock()

    async def run():
        view = QueueView(p)
        interaction = MagicMock()
        interaction.user.id = 7  # dona da música: pode remover sem permissão
        interaction.data = {"values": ["0"]}
        interaction.response.edit_message = AsyncMock()
        # o py-cord só expõe `.values` depois de refresh_state(interaction)
        view._remove._interaction = interaction
        view._remove._selected_values = ["0"]  # remove a primeira (a adiantada)
        await view._on_remove(interaction)

    asyncio.run(run())

    assert [t.video_id for t in p.queue.upcoming] == ["m1", "m2"]
    assert reprogramou == ["remover pelo /queue"]


def test_clear_reprograma(monkeypatch):
    """Depois do /clear a próxima é a rádio (se ligada) — que precisa ser
    buscada e adiantada agora, não no fim da faixa."""
    import core.player as player_mod
    from cogs import music as music_cog

    p = _player(playing=True)
    p.queue.add(Track(title="m", video_id="m1"))
    reprogramou = []
    p.queue_changed = lambda origem=None: reprogramou.append(origem)
    p.panel = MagicMock()
    p.panel.refresh = AsyncMock()
    monkeypatch.setattr(player_mod, "get_player", lambda bot, guild: p)
    monkeypatch.setattr(music_cog, "control_error", lambda player, member: None)

    cog = music_cog.Music.__new__(music_cog.Music)
    cog.bot = MagicMock()
    ctx = MagicMock()
    ctx.respond = AsyncMock()

    asyncio.run(cog.clear.callback(cog, ctx))

    assert p.queue.upcoming == []
    assert reprogramou == ["/clear"]
