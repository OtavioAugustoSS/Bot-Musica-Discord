"""/seek e /volume: um FFmpeg novo no MEIO da faixa, sem o py-cord perceber.

Os dois são a mesma operação — montar outra fonte a partir de um ponto da música
e trocá-la por dentro da FonteDaFaixa. No seek o ponto é o destino e a troca é
imediata. No volume o ponto é "daqui a 3s" e a troca espera a música chegar lá:
o FFmpeg leva ~0,3s para abrir (medido na VM) e, trocando no ponto certo, nada
repete nem some.

Volume 100% no caminho Opus é CÓPIA (17 ms de CPU por segundo de áudio); qualquer
outro valor recodifica dentro do FFmpeg (43 ms/s) — ainda um terço do PCM antigo.
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import core.player as player_mod
from apoio import faz_player
from core.audio import FonteDaFaixa, SaidaDoFfmpeg
from core.player import _Tocada
from core.queue import Track
from db.database import Database

FAIXA = Track(title="Numb", artist="Linkin Park", video_id="kXYiU_JCYtU", duration=187)
INFO = {"stream_url": "http://x/a", "acodec": "opus", "asr": 48000, "canais": 2, "itag": "251", "duration": 187}


class Pulmao:
    """Um BufferedAudioSource de mentira: só o que a troca usa."""

    def __init__(self, *, viva: bool = True, tocou_s: float = 0.0) -> None:
        self.frames_entregues = int(tocou_s * 50)
        self.frames_sem_audio = self.falhas_de_buffer = 0
        self.primeiro_audio_s = 0.3
        self.original = SimpleNamespace(fora_de_20ms=0)
        self.viva = viva
        self.limpou = self.interrompida = False

    def is_opus(self) -> bool:
        return True

    def encheu(self) -> bool:
        return self.viva

    def acabou_vazia(self) -> bool:
        return not self.viva

    def quando_acabar(self, aviso) -> None:
        self.aviso = aviso

    def descarta(self, quantos: int) -> int:
        self.descartados = quantos
        return quantos

    def read(self) -> bytes:
        return b"x"

    def interrompe(self) -> None:
        self.interrompida = True

    def cleanup(self) -> None:
        self.limpou = True


@pytest.fixture
def tocando(monkeypatch):
    """Player tocando FAIXA aos 42s, com `_monta` de mentira que anota os pedidos."""
    voz = MagicMock()
    voz.is_connected.return_value = True
    voz.is_playing.return_value = True
    voz.is_paused.return_value = False
    bot = MagicMock()
    bot.db.set_volume = AsyncMock()
    bot.db.get_volume = AsyncMock(return_value=100)
    player = faz_player(guild=MagicMock(voice_client=voz), bot=bot)
    player.guild.id = 1
    player.queue.current = FAIXA
    velha = Pulmao(tocou_s=42.0)
    player._tocada = _Tocada(track=FAIXA, fonte=FonteDaFaixa(velha), saida=None)
    player._source = player._tocada.fonte
    player._started_at = 1.0
    pedidos: list[float] = []
    novas: list[Pulmao] = []

    async def monta(info, track, desde_s=0.0):
        pedidos.append(desde_s)
        novas.append(Pulmao())
        return novas[-1], None, "direto"

    async def stream(video_id, **kw):
        return INFO

    monkeypatch.setattr(player, "_monta", monta)
    monkeypatch.setattr(player_mod.youtube, "get_stream", stream)
    return SimpleNamespace(player=player, voz=voz, velha=velha, pedidos=pedidos, novas=novas, bot=bot)


# ── seek ────────────────────────────────────────────────────────────────────


def test_seek_troca_a_fonte_por_dentro_e_o_pycord_nao_ve_nada(tocando, caplog):
    p = tocando.player
    with caplog.at_level(logging.INFO, logger="core.player"):
        resultado = asyncio.run(p.seek(120))

    assert resultado == (42, 120)
    assert tocando.pedidos == [120]
    assert p._source._fonte is tocando.novas[0] and p.elapsed == 120
    assert tocando.velha.limpou  # o FFmpeg antigo não fica vivo
    tocando.voz.play.assert_not_called()
    tocando.voz.stop_playing.assert_not_called()
    assert "seek 0:42 → 2:00" in caplog.text and "direto" in caplog.text


def test_dois_seeks_seguidos_so_o_ultimo_vale(tocando, monkeypatch):
    p = tocando.player
    libera = asyncio.Event()

    async def monta(info, track, desde_s=0.0):
        tocando.pedidos.append(desde_s)
        if desde_s == 60:
            await libera.wait()  # o primeiro FFmpeg é lento para abrir
        tocando.novas.append(Pulmao())
        tocando.novas[-1].para = desde_s
        return tocando.novas[-1], None, "direto"

    monkeypatch.setattr(p, "_monta", monta)

    async def roda():
        primeiro = asyncio.create_task(p.seek(60))
        await asyncio.sleep(0.01)
        segundo = await p.seek(150)
        libera.set()
        return await primeiro, segundo

    primeiro, segundo = asyncio.run(roda())

    assert primeiro is None and segundo == (42, 150)
    assert p.elapsed == 150
    atrasada = next(n for n in tocando.novas if n.para == 60)
    assert atrasada.limpou and p._source._fonte is not atrasada


def test_fonte_nova_que_nao_abre_deixa_a_musica_onde_estava(tocando, monkeypatch, caplog):
    p = tocando.player

    async def monta(info, track, desde_s=0.0):
        tocando.novas.append(Pulmao(viva=False))
        return tocando.novas[-1], None, "direto"

    monkeypatch.setattr(p, "_monta", monta)
    with caplog.at_level(logging.WARNING, logger="core.player"):
        resultado = asyncio.run(p.seek(120))

    assert resultado is None
    assert p._source._fonte is tocando.velha and not tocando.velha.limpou
    assert tocando.novas[0].limpou
    assert "não abriu" in caplog.text


def test_seek_sem_nada_tocando_nao_monta_nada(tocando):
    tocando.voz.is_playing.return_value = False

    assert asyncio.run(tocando.player.seek(30)) is None
    assert tocando.pedidos == []


def test_faixa_que_muda_durante_o_seek_nao_recebe_a_fonte_da_outra(tocando, monkeypatch):
    p = tocando.player

    async def monta(info, track, desde_s=0.0):
        # enquanto o FFmpeg abria, a faixa acabou e a próxima começou
        p._tocada = _Tocada(track=FAIXA, fonte=FonteDaFaixa(Pulmao()), saida=None)
        tocando.novas.append(Pulmao())
        return tocando.novas[-1], None, "direto"

    monkeypatch.setattr(p, "_monta", monta)

    assert asyncio.run(p.seek(90)) is None
    assert tocando.novas[0].limpou


def test_contadores_do_ffmpeg_antigo_passam_para_o_novo(tocando, monkeypatch):
    p = tocando.player
    antiga, nova = SaidaDoFfmpeg("a"), SaidaDoFfmpeg("b")
    antiga.comeca()
    nova.comeca()
    antiga.reconexoes, antiga.recusas = 3, 1
    p._tocada.saida = antiga

    async def monta(info, track, desde_s=0.0):
        return Pulmao(), nova, "direto"

    monkeypatch.setattr(p, "_monta", monta)
    asyncio.run(p.seek(100))

    assert p._tocada.saida is nova
    assert (nova.reconexoes, nova.recusas) == (3, 1)  # o fim de faixa relata a faixa inteira


# ── volume ──────────────────────────────────────────────────────────────────


def test_volume_vale_para_o_proximo_ffmpeg_nos_dois_caminhos(monkeypatch):
    abertos = []
    monkeypatch.setattr(player_mod, "FonteOpus", lambda url, **kw: abertos.append(kw) or MagicMock())

    class Pcm(player_mod.discord.AudioSource):
        def read(self):
            return b""

    monkeypatch.setattr(player_mod.discord, "FFmpegPCMAudio", lambda url, **kw: Pcm())
    player = faz_player()
    saida = SaidaDoFfmpeg("t")
    saida.comeca()

    player.volume = 50
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", True)
    _, via = player._abre_ffmpeg(INFO, FAIXA, saida)
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", False)
    fonte_pcm, _ = player._abre_ffmpeg(INFO, FAIXA, saida)

    assert via == "recodificado"
    assert abertos[0]["copia"] is False and abertos[0]["volume"] == pytest.approx(0.5)
    assert fonte_pcm.volume == pytest.approx(0.8 * 0.5)


def test_volume_100_volta_a_copiar(monkeypatch):
    abertos = []
    monkeypatch.setattr(player_mod, "FonteOpus", lambda url, **kw: abertos.append(kw) or MagicMock())
    monkeypatch.setattr(config, "AUDIO_OPUS_DIRETO", True)
    player = faz_player()
    saida = SaidaDoFfmpeg("t")
    saida.comeca()

    _, via = player._abre_ffmpeg(INFO, FAIXA, saida)

    assert via == "direto" and abertos[0]["copia"] is True


def test_trocar_o_volume_tocando_agenda_a_troca_para_daqui_a_3s(tocando, caplog):
    p = tocando.player

    async def roda():
        tarefa = asyncio.create_task(p.set_volume(50))
        await asyncio.sleep(0.05)
        # a fonte nova já abriu (em 45s), mas a música ainda está em 42s: espera
        ainda_nao = p._source._fonte is tocando.velha
        tocando.velha.frames_entregues = 45 * 50
        return ainda_nao, await tarefa

    with caplog.at_level(logging.INFO, logger="core.player"):
        ainda_nao, volume = asyncio.run(roda())

    assert volume == 50 and p.volume == 50
    assert tocando.pedidos == [45.0]
    assert ainda_nao, "trocou antes de a música chegar ao ponto: 3s de música sumiriam"
    assert p._source._fonte is tocando.novas[0] and p.elapsed == 45
    tocando.bot.db.set_volume.assert_awaited_once_with(1, 50)
    assert "volume 100% → 50%" in caplog.text


def test_ffmpeg_que_abre_atrasado_nao_repete_musica(tocando):
    """Teste do Otavio em 20/09: "volume 50%: troca 1:26 → 1:29 em 3.7s" — o FFmpeg
    recodificando ficou pronto 0,7s DEPOIS do ponto marcado, e a troca voltou a
    música 0,7s. A fonte nova agora joga fora o que já tocou pela antiga."""
    p = tocando.player

    async def roda():
        tarefa = asyncio.create_task(p.set_volume(50))
        await asyncio.sleep(0.05)
        tocando.velha.frames_entregues = int(45.7 * 50)  # a música já passou dos 45s marcados
        await tarefa

    asyncio.run(roda())

    assert tocando.novas[0].descartados == 35  # 0,7s em frames de 20ms
    assert abs(p._source.posicao_s - 45.7) < 0.03


def test_pausado_o_volume_troca_na_hora_e_no_mesmo_ponto(tocando):
    tocando.voz.is_playing.return_value = False
    tocando.voz.is_paused.return_value = True

    asyncio.run(tocando.player.set_volume(30))

    assert tocando.pedidos == [42.0]
    assert tocando.player.elapsed == 42


def test_volume_fica_entre_0_e_150_e_valor_igual_nao_reabre_o_ffmpeg(tocando):
    p = tocando.player

    assert asyncio.run(p.set_volume(100)) == 100
    assert tocando.pedidos == []
    tocando.voz.is_playing.return_value = False  # parado: só guarda
    assert asyncio.run(p.set_volume(999)) == 150
    assert asyncio.run(p.set_volume(-5)) == 0
    assert tocando.pedidos == []


def test_no_fim_da_faixa_o_volume_novo_fica_para_a_proxima(tocando):
    tocando.velha.frames_entregues = 183 * 50  # faltam 4s

    asyncio.run(tocando.player.set_volume(60))

    assert tocando.pedidos == [] and tocando.player.volume == 60


def test_volume_do_servidor_e_lido_do_banco_ao_conectar(tocando):
    p = tocando.player
    tocando.bot.db.get_volume = AsyncMock(return_value=70)
    canal = MagicMock()
    tocando.voz.channel = canal

    asyncio.run(p.connect(canal))
    asyncio.run(p.connect(canal))

    assert p.volume == 70
    tocando.bot.db.get_volume.assert_awaited_once_with(1)


# ── banco ───────────────────────────────────────────────────────────────────


def test_volume_por_servidor_no_banco(tmp_path):
    async def roda():
        db = Database(str(tmp_path / "t.db"))
        await db.init()
        try:
            assert await db.get_volume(10) == 100  # nunca mexeram: o padrão
            await db.set_volume(10, 60)
            await db.set_volume(10, 45)
            await db.set_volume(20, 130)
            return await db.get_volume(10), await db.get_volume(20)
        finally:
            await db.close()

    assert asyncio.run(roda()) == (45, 130)


def test_banco_antigo_ganha_a_tabela_sem_migracao(tmp_path):
    """Aditivo: CREATE TABLE IF NOT EXISTS no schema, sem subir a versão."""
    async def roda():
        caminho = str(tmp_path / "t.db")
        db = Database(caminho)
        await db.init()
        await db._conn.execute("DROP TABLE guild_settings")
        await db._conn.commit()
        await db.close()
        db = Database(caminho)
        await db.init()
        try:
            return await db.get_volume(1)
        finally:
            await db.close()

    assert asyncio.run(roda()) == 100
