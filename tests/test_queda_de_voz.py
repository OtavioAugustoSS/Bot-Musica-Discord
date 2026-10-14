"""Queda da conexão de voz NÃO é expulsão.

21/09/2026, 04:22:13 UTC, no meio de um teste do Otavio ("ele parou de tocar do
nada, tava perfeitinho"): o WebSocket de voz do Discord fechou com 1006 (queda de
rede). O py-cord reconectou sozinho em 0,7s — e o AudioPlayer dele espera a
reconexão e retoma o áudio. Mas a reconexão passa por um voice state "bot fora do
canal" (25ms depois, "bot no canal" de novo), e o nosso handler lia esse instante
como "fui expulso": player.stop() — música parada, fila limpa, escuta desligada, e
o bot mudo dentro da call.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs import lifecycle
from cogs.lifecycle import Lifecycle

BOT_ID = 99


class Voz:
    def __init__(self, volta_em: float | None) -> None:
        self._desde = asyncio.get_event_loop().time()
        self._volta_em = volta_em

    def is_connected(self) -> bool:
        if self._volta_em is None:
            return False
        return asyncio.get_event_loop().time() - self._desde >= self._volta_em


def _cena(monkeypatch, voz_de):
    monkeypatch.setattr(lifecycle, "_QUEDA_ESPERA_S", 0.3)
    monkeypatch.setattr(lifecycle, "_QUEDA_ESPERA_MAX_S", 0.6)
    monkeypatch.setattr(lifecycle, "_QUEDA_PASSO_S", 0.01)
    guild = SimpleNamespace(id=1, voice_client=None, me=SimpleNamespace(voice=SimpleNamespace(channel=object())))
    player = MagicMock()
    player.stop = AsyncMock()
    player.guild = guild
    player.listener = None
    type(player).voice = property(lambda self: guild.voice_client)
    monkeypatch.setattr(lifecycle, "peek_player", lambda gid: player)
    bot = MagicMock()
    bot.user.id = BOT_ID
    membro = SimpleNamespace(id=BOT_ID, guild=guild)

    async def roda():
        guild.voice_client = voz_de()
        await Lifecycle(bot).on_voice_state_update(
            membro, SimpleNamespace(channel=object()), SimpleNamespace(channel=None)
        )

    return roda, player, guild


def test_conexao_que_cai_e_volta_sozinha_nao_para_a_musica(monkeypatch, caplog):
    roda, player, _ = _cena(monkeypatch, lambda: Voz(volta_em=0.05))

    with caplog.at_level("INFO"):
        asyncio.run(roda())

    player.stop.assert_not_awaited()
    assert "voltou sozinha" in caplog.text


def test_depois_de_reconectar_confere_se_a_escuta_segue_gravando(monkeypatch):
    roda, player, _ = _cena(monkeypatch, lambda: Voz(volta_em=0.02))
    player.listener = MagicMock(active=True)
    player.listener.confere_a_gravacao = AsyncMock()

    asyncio.run(roda())

    player.listener.confere_a_gravacao.assert_awaited_once()
    player.stop.assert_not_awaited()


def test_expulso_de_verdade_continua_limpando_tudo(monkeypatch):
    """Moderador tirou o bot: o py-cord desiste, o voice client some — para já."""
    roda, player, guild = _cena(monkeypatch, lambda: None)

    asyncio.run(roda())

    player.stop.assert_awaited_once()
    assert "desconectado" in player.stop.await_args.args[0]


def test_conexao_que_nao_volta_no_prazo_tambem_limpa(monkeypatch):
    roda, player, _ = _cena(monkeypatch, lambda: Voz(volta_em=None))

    asyncio.run(roda())

    player.stop.assert_awaited_once()


def test_reconectou_mas_o_bot_nao_esta_em_canal_nenhum_e_expulsao(monkeypatch):
    roda, player, guild = _cena(monkeypatch, lambda: Voz(volta_em=0.0))
    guild.me.voice = None

    asyncio.run(roda())

    player.stop.assert_awaited_once()


@pytest.mark.parametrize("outro_id", [7])
def test_outra_pessoa_saindo_nao_passa_por_aqui(monkeypatch, outro_id):
    roda, player, guild = _cena(monkeypatch, lambda: Voz(volta_em=0.0))
    player.listeners = MagicMock(return_value=[object()])
    bot = MagicMock()
    bot.user.id = BOT_ID

    async def sai():
        guild.voice_client = Voz(volta_em=0.0)
        await Lifecycle(bot).on_voice_state_update(
            SimpleNamespace(id=outro_id, guild=guild), SimpleNamespace(channel=object()), SimpleNamespace(channel=None)
        )

    asyncio.run(sai())

    player.stop.assert_not_awaited()
    player.cancel_empty_leave.assert_called_once()


def test_reconexao_lenta_com_o_bot_de_volta_no_canal_nao_e_expulsao(monkeypatch):
    """26/09/2026, 03:48 UTC: fila vazia, escuta ligada, o WebSocket de voz caiu
    (1006) e o handshake do py-cord levou 30s. O gateway já dizia "bot no canal"
    1s depois, mas os 8s de espera acabaram antes — escuta desligada e, quando a
    reconexão terminou, o bot ficou na call surdo."""
    roda, player, _ = _cena(monkeypatch, lambda: Voz(volta_em=0.5))
    monkeypatch.setattr(lifecycle, "_QUEDA_ESPERA_MAX_S", 2.0)

    asyncio.run(roda())

    player.stop.assert_not_awaited()


def test_fora_do_canal_segue_desistindo_no_prazo_curto(monkeypatch):
    """Sem o gateway dizer que o bot voltou, a espera longa não vale."""
    roda, player, guild = _cena(monkeypatch, lambda: Voz(volta_em=0.5))
    monkeypatch.setattr(lifecycle, "_QUEDA_ESPERA_MAX_S", 2.0)
    guild.me.voice = None

    asyncio.run(roda())

    player.stop.assert_awaited_once()
