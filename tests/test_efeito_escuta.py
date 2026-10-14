"""Som de "estou ouvindo" quando alguém chama o Arrodes — como a Alexa.

Pedido de 17/09: um estalo de chicote toda vez que o bot ouvir "arrodes"
no /ouvir, sozinho ou já com o comando. O py-cord toca UMA fonte por vez,
então o som não pode ser um segundo `voice.play()` por cima da música:

  - com música tocando, ele é misturado na própria fonte, na hora da
    leitura (não no pulmão de 8s — senão sairia 8s atrasado), com a música
    abaixada pela metade enquanto dura, para o estalo não sumir;
  - com o bot parado na call, toca sozinho — e se a música pedida começar
    antes de ele acabar, ele é cortado (o "toca X" não pode travar);
  - com a música pausada, o py-cord não lê nada: sem som;
  - "pula", "pausa" e "sai" esperam o estalo acabar, senão o próprio comando
    o cortaria no meio.
"""
import asyncio
import time
import wave
from unittest.mock import AsyncMock, MagicMock

import discord
import numpy as np
import pytest
from discord.opus import Encoder

import config
from core import audio, listen
from core.audio import BufferedAudioSource
from apoio import faz_player
from core.player import GuildPlayer

FRAME = Encoder.FRAME_SIZE


def _frame(valor: int) -> bytes:
    return np.full(FRAME // 2, valor, dtype=np.int16).tobytes()


def _amostras(frame: bytes) -> np.ndarray:
    return np.frombuffer(frame, dtype=np.int16)


class Musica(discord.AudioSource):
    """Fonte PCM com amplitude constante — fácil de reconhecer na mistura."""

    def __init__(self, valor: int = 1000, frames: int = 400):
        self.valor = valor
        self.frames = frames

    def read(self) -> bytes:
        if self.frames <= 0:
            return b""
        self.frames -= 1
        return _frame(self.valor)

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        pass


def _espera(condicao, timeout: float = 5.0) -> None:
    fim = time.monotonic() + timeout
    while not condicao():
        if time.monotonic() > fim:
            raise AssertionError("condição não aconteceu a tempo")
        time.sleep(0.005)


# ── mistura ─────────────────────────────────────────────────────────────────


def test_mistura_abaixa_a_musica_e_soma_o_efeito():
    saida = _amostras(audio.mistura(_frame(1000), _frame(2000)))
    assert (saida == 1000 * audio.DUCK + 2000).all()


def test_mistura_nao_estoura_o_int16():
    saida = _amostras(audio.mistura(_frame(32000), _frame(30000)))
    assert (saida == 32767).all()
    saida = _amostras(audio.mistura(_frame(-32000), _frame(-30000)))
    assert (saida == -32768).all()


def test_mistura_com_rampa_nao_da_degrau_na_musica():
    """Cortar a música pela metade de uma vez é um degrau — soa como clique.
    A queda e a volta acontecem ao longo de um frame (20ms)."""
    desce = _amostras(audio.mistura(_frame(1000), _frame(0), de=1.0, ate=audio.DUCK))
    sobe = _amostras(audio.mistura(_frame(1000), _frame(0), de=audio.DUCK, ate=1.0))

    assert desce[0] == 1000 and desce[-1] == 1000 * audio.DUCK
    assert sobe[0] == 1000 * audio.DUCK and sobe[-1] == 1000
    assert (np.diff(desce.astype(int)) <= 0).all()  # sem pulos no meio
    assert desce[0] == desce[1]  # esquerdo e direito do mesmo instante, mesmo ganho


def test_efeito_sai_na_proxima_leitura_e_nao_depois_do_pulmao():
    fonte = BufferedAudioSource(Musica(1000), segundos=2.0)
    try:
        _espera(lambda: fonte._fila.qsize() >= 50)  # 1s já adiantado
        fonte.read()
        fonte.sobrepor(_frame(2000) + _frame(3000) + _frame(4000))

        lidos = [_amostras(fonte.read()) for _ in range(4)]
    finally:
        fonte.cleanup()

    primeiro, meio, ultimo, depois = lidos
    assert primeiro[0] == 1000 + 2000  # a música começa a descer aqui
    assert primeiro[-1] == 1000 * audio.DUCK + 2000
    assert (meio == 1000 * audio.DUCK + 3000).all()
    assert ultimo[0] == 1000 * audio.DUCK + 4000
    assert ultimo[-1] == 1000 + 4000  # e já voltou inteira no fim do efeito
    assert (depois == 1000).all()


def test_efeito_novo_substitui_o_que_estava_tocando():
    fonte = BufferedAudioSource(Musica(0), segundos=1.0)
    try:
        _espera(lambda: fonte._fila.qsize() >= 10)
        fonte.sobrepor(_frame(100) * 5)
        fonte.read()
        fonte.sobrepor(_frame(700) * 3)
        depois = [fonte.read() for _ in range(3)]
        fim = fonte.read()
    finally:
        fonte.cleanup()

    assert all((_amostras(f) == 700).all() for f in depois)  # música é 0: só o efeito
    assert (_amostras(fim) == 0).all()


# ── o arquivo do som ────────────────────────────────────────────────────────


def _grava_wav(caminho, pcm: bytes, canais=2, taxa=48000):
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(canais)
        w.setsampwidth(2)
        w.setframerate(taxa)
        w.writeframes(pcm)


def test_carrega_efeito_completa_o_ultimo_frame_e_aplica_o_volume(tmp_path):
    caminho = tmp_path / "e.wav"
    _grava_wav(caminho, np.full(1000, 10000, dtype=np.int16).tobytes())  # 500 quadros

    pcm = audio.carrega_efeito(str(caminho))

    assert len(pcm) == FRAME  # PCMAudio só entrega frames inteiros
    amostras = _amostras(pcm)
    assert amostras[0] == int(10000 * audio.VOLUME_EFEITO)
    assert (amostras[1000:] == 0).all()


def test_carrega_efeito_recusa_formato_errado(tmp_path):
    caminho = tmp_path / "mono.wav"
    _grava_wav(caminho, b"\x00\x00" * 1600, canais=1, taxa=16000)
    with pytest.raises(ValueError):
        audio.carrega_efeito(str(caminho))


def test_som_que_vai_junto_com_o_bot_e_valido():
    pcm = audio.carrega_efeito(audio.EFEITO_ESCUTA_PATH)
    duracao = len(pcm) / (FRAME * 50)
    assert 0.3 <= duracao <= 1.0
    assert np.abs(_amostras(pcm)).max() > 10000  # não é silêncio


def test_sem_o_arquivo_o_bot_segue_sem_som(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(audio, "EFEITO_ESCUTA_PATH", str(tmp_path / "nao-existe.wav"))
    monkeypatch.setattr(audio, "_efeito_escuta", None)
    assert audio.efeito_escuta() == b""
    assert "som de escuta" in caplog.text


# ── player ──────────────────────────────────────────────────────────────────

EFEITO = _frame(2000) * 30  # 0,6s


def _player(estado: str) -> GuildPlayer:
    p = faz_player(guild=MagicMock())
    p.guild.id = 1
    voz = p.guild.voice_client
    voz.is_connected.return_value = True
    voz.is_playing.return_value = estado == "tocando"
    voz.is_paused.return_value = estado == "pausado"
    p._source = MagicMock()
    p.skip_votes = set()
    p._stopping = True
    p.queue = MagicMock()
    return p


@pytest.fixture(autouse=True)
def _som_ligado(monkeypatch):
    monkeypatch.setattr(config, "VOICE_SOM_ESCUTA", True)


def test_com_musica_o_efeito_e_misturado_na_fonte():
    p = _player("tocando")
    p.tocar_efeito(EFEITO)
    p._source.sobrepor.assert_called_once_with(EFEITO)
    p.voice.play.assert_not_called()


def test_parado_na_call_o_efeito_toca_sozinho_e_nao_conta_como_musica():
    p = _player("parado")
    p.tocar_efeito(EFEITO)

    p.voice.play.assert_called_once()
    p.voice.is_playing.return_value = True  # o py-cord agora está tocando o estalo
    assert p.is_playing is False  # enqueue/play_next não podem achar que há música


def test_pausado_nao_toca_nada():
    p = _player("pausado")
    p.tocar_efeito(EFEITO)
    p._source.sobrepor.assert_not_called()
    p.voice.play.assert_not_called()


def test_desligado_no_env_nao_toca(monkeypatch):
    monkeypatch.setattr(config, "VOICE_SOM_ESCUTA", False)
    p = _player("tocando")
    p.tocar_efeito(EFEITO)
    p._source.sobrepor.assert_not_called()


def test_fora_da_call_nao_toca():
    p = _player("parado")
    p.voice.is_connected.return_value = False
    p.tocar_efeito(EFEITO)
    p.voice.play.assert_not_called()


def test_musica_que_comeca_durante_o_efeito_corta_o_efeito(monkeypatch):
    import core.player as player_mod

    p = _player("parado")
    p.tocar_efeito(EFEITO)
    p.voice.is_playing.return_value = True
    ordem = []
    p.voice.stop_playing.side_effect = lambda: ordem.append("corta efeito")
    p.voice.play.side_effect = lambda fonte, **kw: ordem.append(type(fonte).__name__)
    monkeypatch.setattr(player_mod.discord, "FFmpegPCMAudio", lambda *a, **k: Musica(0, 3))

    asyncio.run(p._build_and_play({"stream_url": "http://x"}))
    p._source.cleanup()

    assert ordem == ["corta efeito", "FonteDaFaixa"]
    assert p.is_playing is True


def test_fim_de_um_efeito_antigo_nao_apaga_o_estado_do_novo():
    p = _player("parado")
    p.tocar_efeito(EFEITO)
    fim_do_primeiro = p.voice.play.call_args.kwargs["after"]
    p.tocar_efeito(EFEITO)  # chamou de novo antes de o primeiro acabar

    fim_do_primeiro(None)

    p.voice.is_playing.return_value = True
    assert p.is_playing is False  # ainda é só o estalo tocando


def test_pausar_ou_pular_durante_o_estalo_sozinho_nao_mexe_nele():
    p = _player("parado")
    p.tocar_efeito(EFEITO)
    p.voice.is_playing.return_value = True

    p.pause()
    p.skip()

    p.voice.pause.assert_not_called()
    p.voice.stop_playing.assert_not_called()


def test_espera_efeito_aguarda_so_o_que_falta(monkeypatch):
    p = _player("tocando")
    p.tocar_efeito(EFEITO)
    dormiu = []

    async def dorme(s):
        dormiu.append(s)

    monkeypatch.setattr(asyncio, "sleep", dorme)
    asyncio.run(p.espera_efeito())
    assert len(dormiu) == 1 and 0.5 < dormiu[0] <= 0.6

    p._efeito_ate = time.monotonic() - 1
    dormiu.clear()
    asyncio.run(p.espera_efeito())
    assert dormiu == []


# ── escuta ──────────────────────────────────────────────────────────────────


def _listener(falas: list[str]):
    player = MagicMock()
    player.guild.id = 1
    player.espera_efeito = AsyncMock()
    ouvinte = listen.VoiceListener(player, MagicMock())
    ouvinte.active = True
    sequencia = iter(falas)

    async def transcreve(pcm):
        return next(sequencia)

    return ouvinte, transcreve


async def _roda(ouvinte, n: int) -> None:
    agora = time.monotonic()
    for _ in range(n):
        ouvinte._segments.put_nowait((7, b"\x00" * listen.BYTES_PER_SECOND, agora))
    task = asyncio.create_task(ouvinte._worker())
    fim = time.monotonic() + 2.0
    while not ouvinte._segments.empty() and time.monotonic() < fim:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    ouvinte.active = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture
def escuta(monkeypatch):
    monkeypatch.setattr(listen, "analyze_speech", lambda pcm, thr: (10, pcm))
    monkeypatch.setattr(listen.audio, "efeito_escuta", lambda: EFEITO)

    def monta(falas):
        ouvinte, transcreve = _listener(falas)
        monkeypatch.setattr(listen.speech, "transcribe", transcreve)
        ordem = []
        ouvinte.player.tocar_efeito.side_effect = lambda pcm: ordem.append("som")

        async def executa(user_id, heard, action, arg, **kw):
            ordem.append(action)

        ouvinte._execute = executa
        return ouvinte, ordem

    return monta


def test_chamar_sozinho_toca_o_som(escuta):
    ouvinte, ordem = escuta(["Arrodes."])
    asyncio.run(_roda(ouvinte, 1))
    assert ordem == ["som", "hello"]


def test_chamar_com_comando_toca_o_som_antes_de_executar(escuta):
    ouvinte, ordem = escuta(["Arrodes, toca calcinha preta"])
    asyncio.run(_roda(ouvinte, 1))
    assert ordem == ["som", "play"]


def test_fala_sem_wake_word_nao_toca(escuta):
    ouvinte, ordem = escuta(["toca calcinha preta", "Tchau."])
    asyncio.run(_roda(ouvinte, 2))
    assert ordem == []


def test_comando_que_completa_a_chamada_nao_toca_de_novo(escuta):
    ouvinte, ordem = escuta(["Arrodes.", "toca calcinha preta"])
    asyncio.run(_roda(ouvinte, 2))
    assert ordem == ["som", "hello", "play"]


@pytest.mark.parametrize("acao,metodo", [("skip", "pula_se_ainda_for"), ("pause", "pause")])
def test_pular_e_pausar_esperam_o_som_acabar(acao, metodo):
    ouvinte, _ = _listener([])
    player = ouvinte.player
    ordem = []
    player.espera_efeito = AsyncMock(side_effect=lambda: ordem.append("esperou"))
    player.prepara_o_corte = AsyncMock()  # o pulo monta a próxima ENQUANTO o estalo toca
    getattr(player, metodo).side_effect = lambda *a: ordem.append(metodo)
    ouvinte.text_channel = MagicMock()
    ouvinte.text_channel.send = AsyncMock()
    player.panel.ensure_visible = AsyncMock()

    asyncio.run(ouvinte._execute(7, "x", acao, ""))

    assert ordem == ["esperou", metodo]


def test_sair_espera_o_som_acabar():
    ouvinte, _ = _listener([])
    player = ouvinte.player
    ordem = []
    player.espera_efeito = AsyncMock(side_effect=lambda: ordem.append("esperou"))
    player.stop = AsyncMock(side_effect=lambda motivo="": ordem.append("stop"))
    ouvinte.text_channel = MagicMock()
    ouvinte.text_channel.send = AsyncMock()

    asyncio.run(ouvinte._execute(7, "x", "leave", ""))

    assert ordem == ["esperou", "stop"]


@pytest.mark.parametrize("valor,ligado", [
    ("", True), ("1", True), ("sim", True), ("0", False), ("false", False), ("NAO", False), ("off", False),
])
def test_config_tem_chave_para_desligar(monkeypatch, valor, ligado):
    monkeypatch.setenv("TESTE_LIGA", valor)
    assert config._env_liga("TESTE_LIGA") is ligado
