"""Quem está na call é quem tem ESTADO DE VOZ no canal, não quem está no cache de membros.

28/09/2026: o deploy reiniciou o bot com três pessoas na call. Ao voltar, ele contou
"1 pessoa(s) no canal"; às 04:02 alguém saiu, a conta deu 0 e ele saiu da call com
DUAS pessoas ouvindo. O bot não tem o intent de membros: `VoiceChannel.members` do
py-cord só devolve quem já está no cache (quem usou um comando, quem entrou ou saiu
depois do boot) — quem já estava na call ficava invisível.
"""
from types import SimpleNamespace

from core.player import ouvintes

CANAL_ID, OUTRO_ID, BOT_ID = 10, 20, 999


def _canal(estados: dict[int, int], cache: dict[int, bool]):
    guild = SimpleNamespace(
        _voice_states={uid: SimpleNamespace(channel=SimpleNamespace(id=cid)) for uid, cid in estados.items()},
        _state=SimpleNamespace(self_id=BOT_ID),
        get_member=lambda uid: SimpleNamespace(bot=cache[uid]) if uid in cache else None,
    )
    return SimpleNamespace(id=CANAL_ID, guild=guild)


def test_quem_nao_esta_no_cache_tambem_conta():
    canal = _canal({1: CANAL_ID, 2: CANAL_ID, 3: CANAL_ID, BOT_ID: CANAL_ID}, cache={1: False})
    assert sorted(ouvintes(canal)) == [1, 2, 3]


def test_bot_conhecido_e_o_proprio_bot_nao_contam():
    canal = _canal({1: CANAL_ID, 7: CANAL_ID, BOT_ID: CANAL_ID}, cache={1: False, 7: True})
    assert ouvintes(canal) == [1]


def test_quem_esta_em_outro_canal_nao_conta():
    canal = _canal({1: CANAL_ID, 2: OUTRO_ID}, cache={})
    assert ouvintes(canal) == [1]


def test_estado_sem_canal_nao_conta():
    canal = _canal({1: CANAL_ID}, cache={})
    canal.guild._voice_states[2] = SimpleNamespace(channel=None)
    assert ouvintes(canal) == [1]
