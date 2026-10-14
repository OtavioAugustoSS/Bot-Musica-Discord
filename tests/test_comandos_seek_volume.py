"""/seek, /volume e os mesmos pedidos por voz — a casca em volta de player.seek/set_volume."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import messages as m
from apoio import faz_player
from core import listen
from core.intents import parse_volume
from core.queue import Track
from services.llm import parse_llm_reply

FAIXA = Track(title="Numb", artist="Linkin Park", video_id="kXYiU_JCYtU", duration=187)


# ── o argumento do volume ───────────────────────────────────────────────────


@pytest.mark.parametrize("texto,atual,esperado", [
    ("50", 100, 50),
    ("+20", 100, 120),
    ("-20", 100, 80),
    ("-20", 10, 0),  # não desce de zero
    ("+20", 140, 150),  # nem passa de 150
    ("999", 100, 150),
    (" 80 ", 100, 80),
    ("80%", 100, 80),
])
def test_volume_pedido(texto, atual, esperado):
    assert parse_volume(texto, atual) == esperado


@pytest.mark.parametrize("texto", ["", "alto", "+", "1.5", "--3"])
def test_volume_invalido(texto):
    assert parse_volume(texto, 100) is None


# ── player.pede_seek ────────────────────────────────────────────────────────


def _tocando(posicao: int = 42):
    voz = MagicMock()
    voz.is_connected.return_value = True
    voz.is_playing.return_value = True
    voz.is_paused.return_value = False
    player = faz_player(guild=MagicMock(voice_client=voz))
    player.queue.current = FAIXA
    player._started_at = 1.0
    player._source = MagicMock(posicao_s=float(posicao))
    player.seek = AsyncMock(return_value=(posicao, 0))
    return player, voz


def test_pede_seek_relativo_parte_da_posicao_atual():
    player, _ = _tocando(42)
    player.seek.return_value = (42, 72)

    assert asyncio.run(player.pede_seek("+30")) == ("ok", 72)
    player.seek.assert_awaited_once_with(72)


def test_pede_seek_relativo_nao_passa_do_fim_da_faixa():
    player, _ = _tocando(42)
    player.seek.return_value = (42, 182)

    asyncio.run(player.pede_seek("+300"))

    player.seek.assert_awaited_once_with(182)  # 187s de faixa, 5s de folga


def test_pede_seek_para_um_ponto_que_a_musica_nao_tem_nao_mexe_nela():
    """Teste de 21/09: "minuto trinta… não, segundo trinta" virou seek para 30:00
    numa faixa de 4:19 — o bot foi para 4:14 e a música ACABOU 5s depois. Ponto
    absoluto além do fim é engano de quem falou (ou de quem transcreveu)."""
    player, _ = _tocando(42)

    assert asyncio.run(player.pede_seek("59:00")) == ("alem", 187)
    assert asyncio.run(player.pede_seek("1800")) == ("alem", 187)
    player.seek.assert_not_awaited()


def test_pede_seek_que_nao_entende_o_tempo_nao_mexe_na_musica():
    player, _ = _tocando()

    assert asyncio.run(player.pede_seek("daqui a pouco")) == ("invalido", 0)
    player.seek.assert_not_awaited()


def test_pede_seek_sem_musica():
    player, voz = _tocando()
    voz.is_playing.return_value = False

    assert asyncio.run(player.pede_seek("1:00")) == ("nada", 0)


def test_pede_seek_quando_o_ffmpeg_novo_nao_abre():
    player, _ = _tocando()
    player.seek.return_value = None

    assert asyncio.run(player.pede_seek("1:00")) == ("falhou", 60)


# ── voz ─────────────────────────────────────────────────────────────────────


def _ouvinte():
    player = MagicMock()
    player.guild.id = 1
    player.queue.current = FAIXA
    player.volume = 100
    player.espera_efeito = AsyncMock()
    player.pede_seek = AsyncMock(return_value=("ok", 72))
    player.set_volume = AsyncMock(return_value=120)
    player.panel.ensure_visible = AsyncMock()
    canal = MagicMock()
    canal.send = AsyncMock()
    return listen.VoiceListener(player, canal), player, canal


def test_avanca_30_segundos_por_voz():
    ouvinte, player, canal = _ouvinte()

    asyncio.run(ouvinte._execute(7, "arrodes avança 30 segundos", "seek", "+30"))

    player.pede_seek.assert_awaited_once_with("+30")
    canal.send.assert_awaited_once_with(m.VOICE_SEEK_DONE.format(pos="1:12", dur="3:07"))


def test_seek_por_voz_que_falha_avisa_em_vez_de_fingir():
    ouvinte, player, canal = _ouvinte()
    player.pede_seek.return_value = ("falhou", 72)

    asyncio.run(ouvinte._execute(7, "arrodes avança 30 segundos", "seek", "+30"))

    canal.send.assert_awaited_once_with(m.VOICE_SEEK_FAILED.format(pos="1:12"))


def test_seek_por_voz_sem_musica():
    ouvinte, player, canal = _ouvinte()
    player.pede_seek.return_value = ("nada", 0)

    asyncio.run(ouvinte._execute(7, "arrodes volta 10 segundos", "seek", "-10"))

    canal.send.assert_awaited_once_with(m.NOTHING_PLAYING)


def test_aumenta_o_volume_por_voz():
    ouvinte, player, canal = _ouvinte()

    asyncio.run(ouvinte._execute(7, "arrodes aumenta o volume", "volume", "+20"))

    player.set_volume.assert_awaited_once_with(120)
    canal.send.assert_awaited_once_with(m.VOICE_VOLUME_SET.format(volume=120))


def test_volume_que_o_llm_inventou_cai_no_nao_entendi():
    ouvinte, player, canal = _ouvinte()

    asyncio.run(ouvinte._execute(7, "arrodes volume no talo", "volume", "no talo", from_llm=True))

    player.set_volume.assert_not_awaited()
    canal.send.assert_awaited_once_with(m.VOICE_UNKNOWN.format(heard="arrodes volume no talo"))


# ── LLM ─────────────────────────────────────────────────────────────────────


def test_llm_pode_pedir_seek_e_volume():
    assert parse_llm_reply('{"action": "seek", "query": "+30"}') == ("seek", "+30")
    assert parse_llm_reply('{"action": "seek", "query": "1:30"}') == ("seek", "1:30")
    assert parse_llm_reply('{"action": "volume", "query": "-20"}') == ("volume", "-20")


def test_llm_sem_o_numero_nao_vale():
    assert parse_llm_reply('{"action": "seek", "query": ""}') is None
    assert parse_llm_reply('{"action": "volume"}') is None
