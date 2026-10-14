"""Fim de faixa: o journal diz quanto tocou e por quê acabou — e faixa que morre
na largada ganha uma segunda chance em vez de virar silêncio.

20/09/2026: quatro faixas seguidas "tocaram" 0 segundos (403 do YouTube no
FFmpeg) e o journal não tinha UMA linha sobre isso. O bot anunciava
"tocando", ficava mudo e seguia a vida; quem estava na call pedia outra.
"""
import asyncio
import logging
import subprocess
import sys
import time

import messages as m
from core.audio import SaidaDoFfmpeg
from apoio import faz_player
from core.player import GuildPlayer, _Tocada
from core.queue import LoopMode, MusicQueue, Track


class FakeVoice:
    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return False

    def is_paused(self) -> bool:
        return False


class FakeGuild:
    id = 1

    def __init__(self) -> None:
        self.voice_client = FakeVoice()


class FakePanel:
    async def ensure_visible(self, channel) -> None:
        pass

    async def refresh(self) -> None:
        pass


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class FonteContada:
    """Só os contadores que o fim de faixa lê."""

    def __init__(self, segundos: float, sem_audio: int = 0) -> None:
        self.frames_entregues = int(segundos * 50)
        self.frames_sem_audio = sem_audio
        self.falhas_de_buffer = 1 if sem_audio else 0


def _player(channel: FakeChannel | None = None) -> GuildPlayer:
    player = faz_player(guild=FakeGuild(), channel=channel, panel=FakePanel())
    return player


def _tocada(track: Track, segundos: float, **kw) -> _Tocada:
    return _Tocada(track=track, fonte=FonteContada(segundos, **kw), saida=None)


FAIXA = Track(title="Mind Over Matter", artist="Young the Giant", video_id="LpRby_Osyjc", duration=183)


# ── o relato ─────────────────────────────────────────────────────────────────


def test_fim_normal_diz_quanto_tocou(caplog):
    player = _player()
    with caplog.at_level(logging.INFO, logger="core.player"):
        morreu = player._relata_fim(_tocada(FAIXA, 182.0), None)

    assert morreu is False
    assert "fim de Mind Over Matter" in caplog.text
    assert "3:02 de 3:03" in caplog.text
    assert "acabou" in caplog.text


def test_faixa_pulada_nao_e_morte(caplog):
    player = _player()
    tocada = _tocada(FAIXA, 2.0)
    tocada.pulada = True  # é o que o skip() marca na faixa que tocava
    with caplog.at_level(logging.INFO, logger="core.player"):
        morreu = player._relata_fim(tocada, None)

    assert morreu is False
    assert "pulada" in caplog.text


def test_faixa_que_nao_tocou_nada_e_morte_na_largada_e_vai_como_warning(caplog):
    player = _player()
    with caplog.at_level(logging.INFO, logger="core.player"):
        morreu = player._relata_fim(_tocada(FAIXA, 0.0), None)

    assert morreu is True
    registro = next(r for r in caplog.records if "fim de" in r.getMessage())
    assert registro.levelno == logging.WARNING  # WARNING entra no espelho que vai ao dono
    assert "MORREU NA LARGADA" in registro.getMessage()


def test_faixa_curta_que_tocou_inteira_nao_e_morte():
    vinheta = Track(title="Vinheta", video_id="x", duration=4)
    assert _player()._relata_fim(_tocada(vinheta, 3.9), None) is False


def test_faixa_cortada_no_meio_vira_warning(caplog):
    """rw_timeout ou reconexão esgotada: o FFmpeg desiste e a faixa acaba antes."""
    player = _player()
    with caplog.at_level(logging.INFO, logger="core.player"):
        morreu = player._relata_fim(_tocada(FAIXA, 70.0), None)

    assert morreu is False  # tocou mais de um minuto: recomeçar do zero seria pior
    registro = next(r for r in caplog.records if "fim de" in r.getMessage())
    assert registro.levelno == logging.WARNING
    assert "CORTADA" in registro.getMessage()


def test_duracao_desconhecida_nao_acusa_corte():
    ao_vivo = Track(title="Live", video_id="x", duration=0)
    player = _player()
    assert player._relata_fim(_tocada(ao_vivo, 600.0), None) is False


def test_relato_traz_os_contadores_do_ffmpeg(caplog):
    player = _player()
    saida = SaidaDoFfmpeg("1")
    saida.comeca()
    saida.reconexoes, saida.recusas = 5, 2
    tocada = _Tocada(track=FAIXA, fonte=FonteContada(182.0), saida=saida)
    with caplog.at_level(logging.INFO, logger="core.player"):
        player._relata_fim(tocada, None)

    assert "CDN reconectou 5x" in caplog.text
    assert "2 recusa(s) 403" in caplog.text


# ── a segunda chance ─────────────────────────────────────────────────────────


def test_faixa_morta_na_largada_toca_de_novo_com_extracao_fresca(monkeypatch):
    player = _player(FakeChannel())
    player.queue.current = FAIXA
    tentadas, invalidadas = [], []

    async def toca(track):
        tentadas.append(track)
        return True

    player._try_start = toca
    monkeypatch.setattr("core.player.youtube.invalidate", invalidadas.append)

    asyncio.run(player._segue_depois_do_fim(FAIXA, morreu=True, pulada=False))

    assert tentadas == [FAIXA], "a MESMA faixa, não a próxima da fila"
    assert invalidadas == ["LpRby_Osyjc"], "URL do cache pode ser a que morreu"
    assert player.text_channel.sent == []  # deu certo: ninguém precisa saber


def test_segunda_morte_da_mesma_faixa_avisa_e_segue_para_a_proxima(monkeypatch):
    canal = FakeChannel()
    player = _player(canal)
    proxima = Track(title="Proxima", video_id="p")
    player.queue.current = FAIXA
    player.queue.add(proxima)
    tentadas = []

    async def toca(track):
        tentadas.append(track)
        return True

    player._try_start = toca
    monkeypatch.setattr("core.player.youtube.invalidate", lambda vid: None)

    asyncio.run(player._segue_depois_do_fim(FAIXA, morreu=True, pulada=False))
    asyncio.run(player._segue_depois_do_fim(FAIXA, morreu=True, pulada=False))

    assert tentadas == [FAIXA, proxima]
    assert len(canal.sent) == 1 and "Mind Over Matter" in canal.sent[0]


def test_segunda_morte_nao_repete_a_faixa_nem_com_loop_de_faixa(monkeypatch):
    player = _player(FakeChannel())
    proxima = Track(title="Proxima", video_id="p")
    player.queue.current = FAIXA
    player.queue.add(proxima)
    player.queue.loop_mode = LoopMode.TRACK
    player._retentada = FAIXA  # já teve a segunda chance
    tentadas = []

    async def toca(track):
        tentadas.append(track)
        return True

    player._try_start = toca
    asyncio.run(player._segue_depois_do_fim(FAIXA, morreu=True, pulada=False))

    assert tentadas == [proxima]


def test_mortes_em_serie_param_o_player_em_vez_de_martelar_o_youtube(monkeypatch):
    """Rádio ligada + YouTube recusando tudo = uma extração (5s de CPU na VM)
    por faixa, para sempre. O mesmo disjuntor das falhas de extração vale aqui."""
    canal = FakeChannel()
    player = _player(canal)
    player.autoplay_enabled = True
    monkeypatch.setattr("core.player.youtube.invalidate", lambda vid: None)
    monkeypatch.setattr(player, "_schedule_prefetch", lambda: None, raising=False)
    tentadas = []

    async def toca(track):
        tentadas.append(track)
        return True

    player._try_start = toca
    for i in range(12):
        faixa = Track(title=f"F{i}", video_id=f"v{i}")
        player.queue.current = faixa
        asyncio.run(player._segue_depois_do_fim(faixa, morreu=True, pulada=False))
        if m.PLAYBACK_BROKEN in canal.sent:
            break

    assert m.PLAYBACK_BROKEN in canal.sent
    assert player.autoplay_enabled is False
    assert len(tentadas) < 12


def test_faixa_que_toca_de_verdade_zera_a_contagem_de_mortes():
    player = _player(FakeChannel())
    player._mortes_seguidas = 3

    async def toca(track):
        return True

    player._try_start = toca
    asyncio.run(player._segue_depois_do_fim(FAIXA, morreu=False, pulada=False))

    assert player._mortes_seguidas == 0


def test_substituir_a_fila_durante_a_morte_nao_ressuscita_a_faixa_velha(monkeypatch):
    """/play modo:substituir zera `current` — a faixa que morreu não é mais de ninguém."""
    player = _player(FakeChannel())
    nova = Track(title="Nova", video_id="n")
    player.queue.current = None
    player.queue.add(nova)
    monkeypatch.setattr("core.player.youtube.invalidate", lambda vid: None)
    tentadas = []

    async def toca(track):
        tentadas.append(track)
        return True

    player._try_start = toca
    asyncio.run(player._segue_depois_do_fim(FAIXA, morreu=True, pulada=False))

    assert tentadas == [nova]


# ── stderr do FFmpeg com dono ────────────────────────────────────────────────


def _roda(script: str, saida: SaidaDoFfmpeg) -> None:
    subprocess.run([sys.executable, "-c", script], stderr=saida.escrita, check=False)
    saida.comeca()
    saida.espera(5.0)


def test_stderr_do_ffmpeg_sai_no_log_com_o_servidor(caplog):
    saida = SaidaDoFfmpeg("42 Mind Over Matter")
    with caplog.at_level(logging.DEBUG, logger="core.audio"):
        _roda("import sys; sys.stderr.write('[in#0] Error opening input: Server returned 403\\n')", saida)

    registro = next(r for r in caplog.records if "Error opening input" in r.getMessage())
    assert registro.levelno == logging.WARNING
    assert "[42 Mind Over Matter]" in registro.getMessage()
    assert saida.erros and "Error opening input" in saida.erros[0]


def test_rotina_do_cdn_vira_contador_e_nao_tres_linhas_por_queda(caplog):
    saida = SaidaDoFfmpeg("1")
    script = (
        "import sys\n"
        "for _ in range(3):\n"
        "    sys.stderr.write('[tls @ 0x1] Error in the pull function.\\n')\n"
        "    sys.stderr.write('[tls @ 0x1] IO error: Connection reset by peer\\n')\n"
        "    sys.stderr.write('[https @ 0x2] Will reconnect at 344 in 0 second(s), error=Connection reset by peer.\\n')\n"
        # formato visto na VM (FFmpeg 6.1.1): o motivo NÃO vem na linha do reconnect
        "sys.stderr.write('[https @ 0x2] HTTP error 403 Forbidden\\n')\n"
        "sys.stderr.write('[https @ 0x2] Will reconnect at 0 in 1 second(s).\\n')\n"
    )
    with caplog.at_level(logging.INFO, logger="core.audio"):
        _roda(script, saida)

    assert saida.reconexoes == 3
    assert saida.recusas == 1
    assert caplog.text == "", "rotina não vai para o journal linha a linha"


def test_url_assinada_nao_vai_para_o_log(caplog):
    saida = SaidaDoFfmpeg("1")
    url = "https://rr2---sn-x.googlevideo.com/videoplayback?expire=1&sig=SEGREDO" + "A" * 900
    with caplog.at_level(logging.WARNING, logger="core.audio"):
        _roda(f"import sys; sys.stderr.write('Error opening input file {url}\\n')", saida)

    assert "SEGREDO" not in caplog.text
    assert "<url>" in caplog.text


def test_leitor_termina_sozinho_quando_o_processo_sai():
    saida = SaidaDoFfmpeg("1")
    _roda("pass", saida)
    inicio = time.monotonic()
    while saida._thread.is_alive() and time.monotonic() - inicio < 3:
        time.sleep(0.01)

    assert not saida._thread.is_alive(), "thread presa = descritor vazando a cada faixa"


# ── quando o som saiu de verdade ─────────────────────────────────────────────


def test_primeiro_audio_demorado_entra_no_relato(caplog):
    """"tocando X em 5,7s" é até o FFmpeg SUBIR. No teste de 20/09 a primeira
    faixa ainda levou duas recusas 403 depois disso, e nada dizia quanto."""
    player = _player()
    fonte = FonteContada(50.0)
    fonte.primeiro_audio_s = 3.4
    with caplog.at_level(logging.INFO, logger="core.player"):
        player._relata_fim(_Tocada(track=FAIXA, fonte=fonte, saida=None), None)

    assert "1º áudio só 3.4s depois do play" in caplog.text


def test_primeiro_audio_normal_nao_polui_o_relato(caplog):
    player = _player()
    fonte = FonteContada(182.0)
    fonte.primeiro_audio_s = 1.2  # FFmpeg abrir + 0,6s de pré-buffer
    with caplog.at_level(logging.INFO, logger="core.player"):
        player._relata_fim(_Tocada(track=FAIXA, fonte=fonte, saida=None), None)

    assert "1º áudio" not in caplog.text


def test_inicio_de_faixa_conta_quanto_a_url_fresca_custou(monkeypatch, caplog):
    player = _player(FakeChannel())
    player.queue.add(Track(title="Fria", artist="A", video_id="v1"))

    async def stream(video_id, **kwargs):
        return {"duration": 10, "thumbnail": "", "stream_url": "http://x"}

    async def espera(info):
        return 1.1, 3

    async def monta(info, track=None):
        pass

    monkeypatch.setattr("core.player.youtube.get_stream", stream)
    monkeypatch.setattr("core.player.youtube.espera_stream_abrir", espera)
    monkeypatch.setattr(player, "_build_and_play", monta)
    monkeypatch.setattr(player, "_schedule_prefetch", lambda: None, raising=False)

    class Db:
        async def add_history(self, *a) -> None:
            pass

    player.bot = type("B", (), {"db": Db()})()
    with caplog.at_level(logging.INFO, logger="core.player"):
        asyncio.run(player.play_next())

    assert "URL fresca só abriu depois de 1.1s (3x 403)" in caplog.text


# ── pular não pode esperar por um FFmpeg que ainda está vivo ─────────────────


def test_faixa_pulada_nao_espera_o_stderr():
    """Regressão de 20/09: o `after` do py-cord roda ANTES do cleanup(), então
    no skip o FFmpeg ainda está vivo e o stderr dele não fecha. `saida.espera()`
    estourava o timeout de 0,5s em TODO skip — no journal: "pulando" às
    18:19:59,501 e "fim de … (pulada)" às 18:20:00,007."""
    saida = SaidaDoFfmpeg("1")
    vivo = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], stderr=saida.escrita
    )
    saida.comeca()
    player = _player()
    try:
        inicio = time.monotonic()
        player._relata_fim(
            _Tocada(track=FAIXA, fonte=FonteContada(50.0), saida=saida, pulada=True), None
        )
        gasto = time.monotonic() - inicio
    finally:
        vivo.kill()
        vivo.wait()

    assert gasto < 0.1, f"o skip esperou {gasto:.2f}s pelo stderr de um FFmpeg vivo"


def test_faixa_que_acabou_sozinha_ainda_colhe_as_ultimas_linhas(caplog):
    """No fim natural o FFmpeg já saiu: vale esperar o leitor drenar o stderr
    (é de lá que vem o motivo de uma morte na largada)."""
    saida = SaidaDoFfmpeg("1")
    _roda("import sys; sys.stderr.write('[in#0] Error opening input: Server returned 403\n')", saida)
    player = _player()
    with caplog.at_level(logging.WARNING, logger="core.player"):
        player._relata_fim(_Tocada(track=FAIXA, fonte=FonteContada(0.0), saida=saida), None)

    assert "Error opening input" in caplog.text
