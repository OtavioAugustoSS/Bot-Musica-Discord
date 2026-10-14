"""Observabilidade: o que o journal de 50 dias mostrou que faltava (ou sobrava)."""
import logging
import threading

import pytest

from services import observabilidade as obs


def _registro(msg: str, *args, exc_info=None, nome: str = "x") -> logging.LogRecord:
    return logging.LogRecord(nome, logging.WARNING, __file__, 1, msg, args, exc_info)


# ── segredos ─────────────────────────────────────────────────────────────────


def test_chave_da_groq_no_repr_de_um_erro_sai_mascarada():
    """Foi assim que vazou 25 vezes: o repr do ClientResponseError traz os headers."""
    erro = "ClientResponseError(RequestInfo(headers={'Authorization': 'Bearer gsk_FalsaAbCdEf1234567890xyz'}))"
    linha = obs.Redator(obs.FORMATO).format(_registro("LLM do Groq falhou: %s", erro))

    assert "gsk_Falsa" not in linha
    assert "LLM do Groq falhou" in linha


def test_segredo_dentro_do_traceback_tambem_e_mascarado():
    try:
        raise RuntimeError("falhou com Bearer gsk_SegredoNoTraceback123456")
    except RuntimeError:
        import sys

        linha = obs.Redator(obs.FORMATO).format(_registro("deu ruim", exc_info=sys.exc_info()))

    assert "SegredoNoTraceback" not in linha
    assert "RuntimeError" in linha


def test_valor_do_env_e_mascarado_mesmo_sem_formato_conhecido(monkeypatch):
    monkeypatch.setattr(obs.config, "SPOTIFY_CLIENT_SECRET", "segredo-sem-padrao-42")
    assert "segredo-sem-padrao-42" not in obs.mascara("auth: segredo-sem-padrao-42 recusado")


def test_token_do_discord_e_mascarado():
    # id 111111111111111111 em base64 + partes inventadas: só o formato é real
    token = "MTExMTExMTExMTExMTExMTEx.Xfalso.token-falso-abcdefghijklmnopqrstuvwxyz"
    assert token not in obs.mascara(f"login com {token} falhou")


def test_token_e_sessao_da_conexao_de_voz_sao_mascarados():
    """O py-cord loga isto em INFO a cada conexão (visto no journal de 20/09)."""
    linha = (
        "Identifying ourselves: {'op': 0, 'd': {'server_id': '111111111111111111',"
        " 'session_id': 'sessao-falsa-0000', 'token': 'token-falso',"
        " 'max_dave_protocol_version': 1}}"
    )
    limpa = obs.mascara(linha)

    assert "token-falso" not in limpa and "sessao-falsa" not in limpa
    # a chave fica, só o valor some (uma 1ª versão trocava o par inteiro por
    # bytes de controle: o grupo do regex se perdeu num heredoc)
    assert "'token': '***'" in limpa and "'session_id': '***'" in limpa
    assert limpa.isprintable()
    assert "'server_id': '111111111111111111'" in limpa  # o resto continua legível
    assert "'max_dave_protocol_version': 1" in limpa


def test_texto_comum_passa_intacto():
    linha = "[123] tocando Mind Over Matter — Young the Giant em 4.8s"
    assert obs.mascara(linha) == linha


# ── ruído ────────────────────────────────────────────────────────────────────


def test_rajada_de_cryptoerror_vira_um_aviso_com_contagem():
    filtro = obs.FiltroDeRajada(("Decryption failed",), janela_s=60.0)
    agora = [100.0]
    filtro._relogio = lambda: agora[0]

    primeiro = _registro("Critical error at AEAD: Decryption failed.")
    assert filtro.filter(primeiro) is True
    for _ in range(40):
        assert filtro.filter(_registro("Critical error at AEAD: Decryption failed.")) is False

    agora[0] += 61.0
    depois = _registro("Critical error at AEAD: Decryption failed.")
    assert filtro.filter(depois) is True
    assert "+40 iguais" in depois.getMessage()


def test_rajada_perde_o_traceback_de_15_linhas():
    import sys

    filtro = obs.FiltroDeRajada(("CryptoError",))
    try:
        raise ValueError("x")
    except ValueError:
        registro = _registro("CryptoError while decoding a voice packet", exc_info=sys.exc_info())

    assert filtro.filter(registro) is True
    assert registro.exc_info is None


def test_rotina_do_leitor_de_voz_nao_vai_para_o_espelho_de_erros():
    filtro = obs.FiltroDeRotina(("is waiting to be set as running",))
    rotina = _registro("Socket reader voice-socket-event-reader:0x7be1 is waiting to be set as running")

    assert filtro.filter(rotina) is False
    assert filtro.filter(_registro("Disconnected from voice... Reconnecting in 1.2s")) is True


def test_filtro_nao_toca_no_que_nao_e_ruido():
    filtro = obs.FiltroDeRajada(("Decryption failed",))
    for _ in range(5):
        assert filtro.filter(_registro("Disconnected from voice... Reconnecting in 1.2s")) is True


# ── erros fora do logging ────────────────────────────────────────────────────


def test_excecao_em_thread_passa_pelo_logging(caplog):
    """Eram 37 tracebacks crus no journal — e fora do espelho que vai ao dono."""
    anterior = threading.excepthook
    threading.excepthook = obs._excecao_em_thread
    try:
        with caplog.at_level(logging.ERROR, logger="services.observabilidade"):
            t = threading.Thread(target=lambda: 1 / 0, name="voice-receiver-packet-router")
            t.start()
            t.join()
    finally:
        threading.excepthook = anterior

    assert "voice-receiver-packet-router" in caplog.text
    assert "ZeroDivisionError" in caplog.text


# ── travadas do loop ─────────────────────────────────────────────────────────


def test_lag_pequeno_nao_avisa_mas_fica_no_maximo():
    vigia = obs.VigiaDoLoop()
    assert vigia.registra(0.2, agora=10.0) is None
    assert vigia.leu_o_maior() == pytest.approx(0.2)
    assert vigia.leu_o_maior() == 0.0  # zera na leitura


def test_travada_avisa_uma_vez_por_janela_com_a_pior():
    vigia = obs.VigiaDoLoop()
    assert "2.5s" in vigia.registra(2.5, agora=100.0)
    assert vigia.registra(4.0, agora=105.0) is None  # dentro da janela: só conta
    assert vigia.registra(1.0, agora=110.0) is None
    aviso = vigia.registra(0.9, agora=140.0)
    assert "4.0s" in aviso and "3 travada" in aviso


# ── retrato ──────────────────────────────────────────────────────────────────


def test_retrato_sai_em_qualquer_plataforma(monkeypatch):
    class Bot:
        latency = 0.123

    linha = obs.retrato(Bot(), obs.VigiaDoLoop())

    assert "threads" in linha and "calls 0" in linha and "gateway 123ms" in linha
    assert "perfil" in linha


def test_retrato_nao_parece_atividade_para_a_checagem_do_deploy():
    """Antes de reiniciar, o deploy procura "tocando", "na fila" e "Connecting
    to voice" nos últimos 10min do journal. A primeira versão desta linha tinha
    as duas primeiras — e um bot parado passava a parecer em uso para sempre."""
    linha = obs.retrato(object())

    for palavra in ("tocando", "na fila", "Connecting to voice"):
        assert palavra not in linha


def test_retrato_inclui_a_groq_quando_houve_movimento(monkeypatch):
    from services import speech

    speech.leu_estatisticas()
    speech._estatisticas["ok"] = 12
    speech._estatisticas["429"] = 3

    linha = obs.retrato(object())

    assert "groq ok=12 429=3" in linha
    assert speech.leu_estatisticas()["ok"] == 0


def test_retrato_conta_os_cortes_da_escuta(monkeypatch):
    """Quantos trechos o corte por energia (mic aberto) e o corte na pausa
    (fôlego acima do teto) salvaram — é como se mede, no journal, se o conserto
    de 04/10 está agindo."""
    from core import listen

    listen.leu_estatisticas()
    listen._estatisticas["cortes_por_energia"] = 4
    listen._estatisticas["cortes_na_pausa"] = 1

    linha = obs.retrato(object())

    assert "escuta cortes_por_energia=4 cortes_na_pausa=1" in linha
    assert listen.leu_estatisticas()["cortes_por_energia"] == 0


# ── CPU dos filhos (FFmpeg e deno) ───────────────────────────────────────────


def test_stat_de_processo_com_espaco_e_parentese_no_nome():
    """O comm vem entre parênteses e pode conter ')' — conta-se do ÚLTIMO."""
    stat = "4242 (ffmpeg (copy) x) S 1 4242 4242 0 -1 4194304 100 0 0 0 731 219 0 0 20 0 3 0 1 2 3"

    assert obs._cpu_de_stat(stat) == ("ffmpeg (copy) x", 731 + 219)


def test_stat_truncado_nao_levanta():
    assert obs._cpu_de_stat("4242 (ffmpeg) S 1 2") is None
    assert obs._cpu_de_stat("lixo") is None


def test_cpu_dos_filhos_soma_colhidos_e_vivos(monkeypatch):
    """process_time() não conta filhos: sem esta parcela, mover o encode do
    Python para dentro do FFmpeg pareceria um ganho de CPU que não existe."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        obs.os, "times",
        lambda: SimpleNamespace(children_user=3.0, children_system=1.0),
    )
    monkeypatch.setattr(obs.os, "sysconf", lambda nome: 100, raising=False)

    assert obs._cpu_dos_filhos_s([("ffmpeg", 250), ("deno", 50)]) == pytest.approx(7.0)


def test_filhos_vivos_nao_explode_fora_do_linux():
    assert isinstance(obs._filhos_vivos(), list)


# ── systemd: READY e batimento do watchdog ───────────────────────────────────


class _SoqueteFalso:
    enviados: list = []

    def __init__(self, *args) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def sendto(self, dados, destino) -> None:
        _SoqueteFalso.enviados.append((dados, destino))


def _com_systemd(monkeypatch, destino: str) -> None:
    _SoqueteFalso.enviados = []
    monkeypatch.setenv("NOTIFY_SOCKET", destino)
    monkeypatch.setattr(obs, "_abre_soquete", _SoqueteFalso)


def test_fora_do_systemd_avisar_nao_faz_nada(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    assert obs.avisa_systemd("READY=1") is False


def test_aviso_vai_para_o_socket_que_o_systemd_deixou(monkeypatch):
    _com_systemd(monkeypatch, "/run/systemd/notify")

    assert obs.avisa_systemd("WATCHDOG=1") is True
    assert _SoqueteFalso.enviados == [(b"WATCHDOG=1", "/run/systemd/notify")]


def test_socket_abstrato_troca_a_arroba_por_nulo(monkeypatch):
    _com_systemd(monkeypatch, "@/org/freedesktop/systemd1/notify/123")

    obs.avisa_systemd("READY=1")

    assert _SoqueteFalso.enviados[0][1] == "\0/org/freedesktop/systemd1/notify/123"


def test_vigia_avisa_pronto_e_bate_o_watchdog_de_dentro_do_loop(monkeypatch):
    """O batimento sai de uma TASK: event loop travado = sem batimento = o
    systemd reinicia. Batimento de thread nunca acusaria o loop parado."""
    import asyncio

    _com_systemd(monkeypatch, "/run/systemd/notify")
    monkeypatch.setattr(obs, "_TIQUE_S", 0.01)

    async def roda_um_pouco():
        tarefa = asyncio.ensure_future(obs.VigiaDoLoop().roda())
        await asyncio.sleep(0.08)
        tarefa.cancel()

    asyncio.run(roda_um_pouco())
    estados = [dados for dados, _ in _SoqueteFalso.enviados]

    assert estados[0] == b"READY=1"
    assert estados.count(b"WATCHDOG=1") == 1, "um batimento por janela, não um por tique"


def test_inicia_aceita_o_loop_antes_de_ele_rodar():
    """main.py chama antes do bot.run(): o batimento não pode depender do on_ready."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        tarefas = obs.inicia(object(), loop)
        assert [t.get_name() for t in tarefas] == ["vigia-do-loop", "retratos-de-saude"]
        for tarefa in tarefas:
            tarefa.cancel()
        loop.run_until_complete(asyncio.gather(*tarefas, return_exceptions=True))
    finally:
        loop.close()


def test_faltas_de_pagina_le_o_campo_majflt(monkeypatch, tmp_path):
    # nome do processo com espaço e parêntese, como o /proc permite
    stat = tmp_path / "stat"
    stat.write_text("4242 (python (bot)) S 1 2 3 4 5 6 7 8 99 10 11 12\n", encoding="ascii")
    abre = open
    monkeypatch.setattr(
        "builtins.open",
        lambda caminho, *a, **k: abre(stat, *a, **k) if caminho == "/proc/self/stat" else abre(caminho, *a, **k),
    )
    assert obs._faltas_de_pagina_do_disco() == 99


def test_memoria_da_travada_mostra_faltas_e_swap(monkeypatch):
    monkeypatch.setattr(obs, "_le_proc_status", lambda: {"VmSwap": 36 * 1024})
    assert obs._memoria_da_travada(812) == ", 812 falta(s) de página do disco na travada, swap 36MB"
    monkeypatch.setattr(obs, "_le_proc_status", lambda: {})
    assert obs._memoria_da_travada(812) == ""
