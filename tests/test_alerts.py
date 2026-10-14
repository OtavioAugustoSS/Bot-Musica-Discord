"""Avisos ao dono por DM: reinício sujo, extração quebrada, cookies mortos.

Na VM foram 23 subidas do serviço em 30 dias e ninguém soube o motivo de
nenhuma: o systemd reinicia (Restart=always) e o journal ninguém lê. E o
apagão de agosto/2026 durou dias porque o bot subia "saudável" enquanto
toda faixa falhava. Agora o bot guarda um marcador de execução e um
arquivo pequeno com os últimos WARNING/ERROR; ao subir depois de uma morte
sem desligamento limpo, manda o resumo por DM ao dono do app (sem precisar
configurar nada: o dono vem da própria API do Discord). Cada condição
avisa no máximo uma vez por janela.
"""
import asyncio
import json
import logging
import os
from unittest.mock import AsyncMock, MagicMock

from services import alerts


# ── marcador de execução ─────────────────────────────────────────────────────


def test_primeira_execucao_nao_acusa_reinicio_sujo(tmp_path):
    estado = alerts.RunState(str(tmp_path / "run-state.json"))

    assert estado.begin() is None


def test_morte_sem_desligamento_limpo_e_detectada(tmp_path):
    caminho = str(tmp_path / "run-state.json")
    alerts.RunState(caminho).begin()  # subiu… e morreu sem mark_clean_exit

    anterior = alerts.RunState(caminho).begin()

    assert anterior is not None and "started_at" in anterior


def test_desligamento_limpo_nao_acusa(tmp_path):
    caminho = str(tmp_path / "run-state.json")
    estado = alerts.RunState(caminho)
    estado.begin()
    estado.mark_clean_exit()

    assert alerts.RunState(caminho).begin() is None


def test_arquivo_corrompido_nao_derruba_o_boot(tmp_path):
    caminho = tmp_path / "run-state.json"
    caminho.write_text("{lixo", encoding="utf-8")

    assert alerts.RunState(str(caminho)).begin() is None


# ── um aviso por condição por janela ─────────────────────────────────────────


def test_should_send_limita_por_condicao(tmp_path, monkeypatch):
    relogio = [1000.0]
    monkeypatch.setattr(alerts, "_now", lambda: relogio[0])
    estado = alerts.RunState(str(tmp_path / "run-state.json"))
    estado.begin()

    assert estado.should_send("cookies", ttl_s=3600)
    assert not estado.should_send("cookies", ttl_s=3600)
    assert estado.should_send("extracao", ttl_s=3600)  # condição diferente
    relogio[0] += 3601
    assert estado.should_send("cookies", ttl_s=3600)


def test_janela_sobrevive_a_um_reinicio(tmp_path, monkeypatch):
    """Senão todo boot repetiria o mesmo aviso de cookies/yt-dlp."""
    monkeypatch.setattr(alerts, "_now", lambda: 1000.0)
    caminho = str(tmp_path / "run-state.json")
    estado = alerts.RunState(caminho)
    estado.begin()
    estado.should_send("ytdlp-2026.9.1", ttl_s=7 * 86400)
    estado.mark_clean_exit()

    de_novo = alerts.RunState(caminho)
    de_novo.begin()

    assert not de_novo.should_send("ytdlp-2026.9.1", ttl_s=7 * 86400)


# ── últimos erros ────────────────────────────────────────────────────────────


def test_handler_guarda_warning_e_error_em_arquivo(tmp_path):
    caminho = str(tmp_path / "recent-errors.log")
    handler = alerts.install_error_log(caminho)
    try:
        logging.getLogger("teste.alerts").warning("cookies venceram")
        logging.getLogger("teste.alerts").info("isto não entra")
        logging.getLogger("teste.alerts").error("explodiu")
        handler.flush()
        cauda = alerts.tail_errors(caminho, lines=5)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()

    assert "cookies venceram" in cauda and "explodiu" in cauda
    assert "não entra" not in cauda


def test_tail_errors_sem_arquivo():
    assert alerts.tail_errors(os.path.join("nao", "existe.log")) == ""


def test_tail_errors_pega_so_as_ultimas_linhas(tmp_path):
    caminho = tmp_path / "e.log"
    caminho.write_text("\n".join(f"linha {i}" for i in range(50)), encoding="utf-8")

    cauda = alerts.tail_errors(str(caminho), lines=3)

    assert cauda.splitlines() == ["linha 47", "linha 48", "linha 49"]


# ── entrega por DM ao dono ───────────────────────────────────────────────────


def _bot(dm_falha: Exception | None = None):
    bot = MagicMock()
    dono = MagicMock()
    dono.send = AsyncMock(side_effect=dm_falha)
    info = MagicMock()
    info.owner = dono
    bot.application_info = AsyncMock(return_value=info)
    return bot, dono


def test_envia_dm_ao_dono_do_app(tmp_path):
    bot, dono = _bot()
    estado = alerts.RunState(str(tmp_path / "s.json"))
    estado.begin()
    avisos = alerts.OwnerAlerts(bot, estado)

    assert asyncio.run(avisos.send("extracao", "quebrou")) is True
    dono.send.assert_awaited_once_with("quebrou")


def test_respeita_a_janela(tmp_path):
    bot, dono = _bot()
    estado = alerts.RunState(str(tmp_path / "s.json"))
    estado.begin()
    avisos = alerts.OwnerAlerts(bot, estado)

    asyncio.run(avisos.send("extracao", "quebrou"))
    assert asyncio.run(avisos.send("extracao", "quebrou de novo")) is False
    assert dono.send.await_count == 1


def test_dm_fechada_nao_explode_e_nao_gasta_a_janela(tmp_path):
    bot, dono = _bot(dm_falha=RuntimeError("Cannot send messages to this user"))
    estado = alerts.RunState(str(tmp_path / "s.json"))
    estado.begin()
    avisos = alerts.OwnerAlerts(bot, estado)

    assert asyncio.run(avisos.send("extracao", "quebrou")) is False
    assert estado.should_send("extracao", ttl_s=3600)  # tenta de novo depois


def test_mensagem_longa_e_cortada_no_limite_do_discord(tmp_path):
    bot, dono = _bot()
    estado = alerts.RunState(str(tmp_path / "s.json"))
    estado.begin()
    avisos = alerts.OwnerAlerts(bot, estado)

    asyncio.run(avisos.send("x", "a" * 5000))

    enviado = dono.send.await_args.args[0]
    assert len(enviado) <= 2000


def test_estado_persistido_e_json_legivel(tmp_path):
    caminho = tmp_path / "s.json"
    estado = alerts.RunState(str(caminho))
    estado.begin()

    dados = json.loads(caminho.read_text(encoding="utf-8"))
    assert dados["clean_exit"] is False and "started_at" in dados


# ── DM fechada: cai para um canal do servidor ────────────────────────────────
# Validado na VM (12/09, crash simulado): a DM ao dono deu 403 "Cannot send
# messages to this user" — o Discord do Otavio não aceita DM do bot. Sem
# fallback o aviso morre em silêncio, que é exatamente o que ele evita.


def _bot_com_canal(dm_falha: Exception, canal_por_id=None):
    bot, dono = _bot(dm_falha=dm_falha)
    dono.id = 42
    canal = MagicMock()
    canal.send = AsyncMock()
    canal.permissions_for = lambda membro: MagicMock(send_messages=True)
    guild = MagicMock()
    guild.system_channel = canal
    guild.text_channels = [canal]
    bot.guilds = [guild]
    bot.get_channel = lambda cid: canal_por_id if cid == 777 else None
    return bot, dono, canal


def test_dm_fechada_cai_para_o_canal_de_sistema_mencionando_o_dono(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts.config, "ALERT_CHANNEL_ID", "")
    bot, dono, canal = _bot_com_canal(RuntimeError("403 Cannot send messages to this user"))
    estado = alerts.RunState(str(tmp_path / "s.json"))
    estado.begin()

    assert asyncio.run(alerts.OwnerAlerts(bot, estado).send("x", "quebrou")) is True

    enviado = canal.send.await_args.args[0]
    assert "<@42>" in enviado and "quebrou" in enviado
    assert not estado.should_send("x", ttl_s=3600)  # entregue: janela gasta


def test_canal_configurado_tem_prioridade_sobre_o_de_sistema(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts.config, "ALERT_CHANNEL_ID", "777")
    configurado = MagicMock()
    configurado.send = AsyncMock()
    bot, dono, sistema = _bot_com_canal(RuntimeError("403"), canal_por_id=configurado)
    estado = alerts.RunState(str(tmp_path / "s.json"))
    estado.begin()

    asyncio.run(alerts.OwnerAlerts(bot, estado).send("x", "quebrou"))

    configurado.send.assert_awaited_once()
    sistema.send.assert_not_awaited()


def test_sem_dm_e_sem_canal_nao_gasta_a_janela(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts.config, "ALERT_CHANNEL_ID", "")
    bot, dono = _bot(dm_falha=RuntimeError("403"))
    bot.guilds = []
    estado = alerts.RunState(str(tmp_path / "s.json"))
    estado.begin()

    assert asyncio.run(alerts.OwnerAlerts(bot, estado).send("x", "quebrou")) is False
    assert estado.should_send("x", ttl_s=3600)
