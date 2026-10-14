"""O bot percebe sozinho que o YouTube mudou — e o canário do yt-dlp tem como avisar.

O apagão de agosto/2026 durou dias porque ninguém soube: o bot anunciava
"tocando", ficava mudo, e o dono só descobria quando alguém reclamava. Duas
peças fecham esse buraco:

  - Sintomas: faixas morrendo na largada, extrações falhando e áudio caindo para
    formato com vídeo (itag 18) são CONTADOS numa janela de 1h. Passou do limiar,
    o dono é avisado e o bot pede o canário (scripts/canario_ytdlp.sh), que testa
    a produção de verdade e só troca o yt-dlp se ela estiver quebrada E o nightly
    passar (decisão do Otavio, 20/09/2026).
  - Caixa de avisos: o canário é um shell script fora do bot; o que ele tem a
    dizer vira arquivo em data/avisos/, e o bot entrega ao dono.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import doctor
from apoio import FakeChannel, faz_player
from core.queue import Track
from services import alerts

FAIXA = Track(title="Numb", artist="Linkin Park", video_id="kXYiU_JCYtU", duration=187)


# ── caixa de avisos ─────────────────────────────────────────────────────────


def _dono(enviou: bool = True):
    dono = MagicMock()
    dono.send = AsyncMock(return_value=enviou)
    return dono


def test_aviso_em_arquivo_e_entregue_e_o_arquivo_some(tmp_path):
    (tmp_path / "20260920-canario.txt").write_text("promovi o yt-dlp 2026.08.19 → 2026.09.18", encoding="utf-8")
    dono = _dono()

    entregues = asyncio.run(alerts.entrega_avisos(dono, str(tmp_path)))

    assert entregues == 1
    chave, texto = dono.send.await_args.args
    assert chave == "aviso:20260920-canario.txt" and "2026.09.18" in texto
    assert list(tmp_path.iterdir()) == []


def test_aviso_que_nao_saiu_fica_para_a_proxima_rodada(tmp_path):
    (tmp_path / "a.txt").write_text("produção quebrada", encoding="utf-8")

    assert asyncio.run(alerts.entrega_avisos(_dono(enviou=False), str(tmp_path))) == 0
    assert (tmp_path / "a.txt").exists()


def test_aviso_nunca_leva_segredo_nem_estoura_o_limite_do_discord(tmp_path):
    (tmp_path / "a.txt").write_text("erro com gsk_abcdefghijklmnopqrstuvwxyz0123 " + "x" * 5000, encoding="utf-8")
    dono = _dono()

    asyncio.run(alerts.entrega_avisos(dono, str(tmp_path)))

    texto = dono.send.await_args.args[1]
    assert "gsk_abc" not in texto and len(texto) <= 1900


def test_pasta_que_nao_existe_e_arquivo_vazio_nao_quebram(tmp_path):
    assert asyncio.run(alerts.entrega_avisos(_dono(), str(tmp_path / "nao-existe"))) == 0
    (tmp_path / "vazio.txt").write_text("", encoding="utf-8")
    dono = _dono()

    assert asyncio.run(alerts.entrega_avisos(dono, str(tmp_path))) == 0
    dono.send.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []  # lixo não fica para sempre


# ── sintomas ────────────────────────────────────────────────────────────────


def _sintomas(tmp_path, relogio):
    dono = _dono()
    return alerts.Sintomas(dono, pedido_path=str(tmp_path / "canario-pedido"), agora=lambda: relogio[0]), dono


def test_tres_mortes_na_largada_em_uma_hora_acendem_o_alerta(tmp_path):
    relogio = [1000.0]
    vigia, _ = _sintomas(tmp_path, relogio)

    acendeu = []
    for _ in range(3):
        acendeu.append(vigia.conta("largada"))
        relogio[0] += 60

    assert acendeu == [False, False, True]


def test_sintoma_espalhado_por_mais_de_uma_hora_nao_acende(tmp_path):
    relogio = [1000.0]
    vigia, _ = _sintomas(tmp_path, relogio)

    acendeu = []
    for _ in range(5):
        acendeu.append(vigia.conta("largada"))
        relogio[0] += 40 * 60

    assert not any(acendeu)


def test_alerta_aceso_nao_repete_a_cada_falha(tmp_path):
    relogio = [1000.0]
    vigia, _ = _sintomas(tmp_path, relogio)

    acendeu = [vigia.conta("largada") for _ in range(8)]

    assert acendeu.count(True) == 1


def test_extracao_precisa_de_mais_falhas_que_a_largada(tmp_path):
    """Uma playlist com 4 vídeos removidos não é o YouTube quebrado."""
    relogio = [1000.0]
    vigia, _ = _sintomas(tmp_path, relogio)

    assert [vigia.conta("extracao") for _ in range(5)] == [False, False, False, False, True]


def test_avisar_manda_ao_dono_e_pede_o_canario(tmp_path):
    relogio = [1000.0]
    vigia, dono = _sintomas(tmp_path, relogio)

    asyncio.run(vigia.avisa("largada", "Numb — Linkin Park: HTTP error 403"))

    chave, texto = dono.send.await_args.args
    assert chave == "sintoma:largada" and "403" in texto
    assert (tmp_path / "canario-pedido").exists()


def test_pedido_que_nao_da_para_gravar_nao_derruba_o_aviso(tmp_path):
    vigia = alerts.Sintomas(_dono(), pedido_path=str(tmp_path / "nao" / "existe" / "pedido"))

    asyncio.run(vigia.avisa("extracao", "x"))  # não levanta


# ── os pontos do player que contam ──────────────────────────────────────────


def _player_com_vigia():
    bot = MagicMock()
    bot.sintomas.conta = MagicMock(return_value=False)
    bot.sintomas.avisa = AsyncMock()
    voz = MagicMock()
    voz.is_connected.return_value = True
    voz.is_playing.return_value = True  # play_next não tenta tocar nada
    player = faz_player(guild=MagicMock(voice_client=voz), bot=bot, channel=FakeChannel())
    player.guild.id = 1
    return player, bot


def test_morte_na_largada_conta_como_sintoma():
    player, bot = _player_com_vigia()
    player.queue.current = FAIXA

    asyncio.run(player._segue_depois_do_fim(FAIXA, morreu=True, pulada=False))

    bot.sintomas.conta.assert_called_once_with("largada")


def test_falha_de_extracao_conta_e_avisa_quando_acende():
    player, bot = _player_com_vigia()
    bot.sintomas.conta.return_value = True

    async def roda():
        player._record_failure(FAIXA, RuntimeError("Sign in to confirm you're not a bot"))
        await asyncio.sleep(0)

    asyncio.run(roda())

    bot.sintomas.conta.assert_called_once_with("extracao")
    bot.sintomas.avisa.assert_awaited_once()
    assert bot.sintomas.avisa.await_args.args[0] == "extracao"


def test_audio_que_nao_e_opus_conta_como_formato():
    player, bot = _player_com_vigia()

    player._confere_o_formato(FAIXA, {"acodec": "mp4a.40.2", "itag": "18"})
    player._confere_o_formato(FAIXA, {"acodec": "opus", "itag": "251"})
    player._confere_o_formato(FAIXA, {})  # extração antiga, sem o dado: não é sintoma

    bot.sintomas.conta.assert_called_once_with("formato")


def test_player_sem_vigia_segue_a_vida():
    player = faz_player()

    player._sintoma("largada", "x")  # bot=None nos testes antigos: não levanta


# ── doctor: extrair não é tocar ─────────────────────────────────────────────


@pytest.mark.parametrize("toca,codigo", [(True, 0), (False, 2)])
def test_doctor_sai_com_2_quando_extrai_mas_nao_toca(monkeypatch, toca, codigo, capsys):
    """O canário decide pelo código de saída: 1 = nenhum perfil extrai, 2 =
    extrai mas o FFmpeg não tira áudio da URL (o 403 de 20/09 era isso)."""
    perfil = MagicMock()
    perfil.name = "cookies"

    async def tocar(_perfil):
        return toca

    monkeypatch.setattr(doctor.youtube, "profiles", lambda: [perfil])
    monkeypatch.setattr(doctor.youtube, "_extract_once", lambda url, p: {
        "url": "http://x", "vcodec": "none", "acodec": "opus", "format_id": "251"})
    monkeypatch.setattr(doctor.youtube, "alerts", lambda: [])
    monkeypatch.setattr(doctor, "tocar_de_verdade", tocar)

    assert asyncio.run(doctor.perfis()) == codigo
