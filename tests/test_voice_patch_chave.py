"""Depois de uma reconexão de voz, o leitor da escuta tem de usar a chave NOVA.

03/10/2026, 20:33 (BRT): o WebSocket de voz fechou com 1006 e o py-cord refez
a conexão em 1s, com `secret_key` nova — mas o AudioReader seguiu com o
decryptor da antiga. ~75 mil pacotes falharam ("Critical error at AEAD:
Decryption failed"), ninguém foi ouvido por 53 min e alguém clicou em sair.

No py-cord 2.8.1 `VoiceWebSocket.load_secret_key` grava a chave em
`state.secret_key` e ninguém chama `AudioReader.update_secret_key` (existe,
sem chamadores). Dois remendos: a chave nova vai para o leitor assim que chega
(item 10a), e um pacote que falha com a chave do cliente diferente da do box
atualiza o box na hora (item 10b) — rede para o caminho que o 10a não cobrir.
"""
import asyncio
import logging

import pytest

from core import pycord_voice_patch as patch

CHAVE_VELHA = bytes(range(32))
CHAVE_NOVA = bytes(range(32, 64))


class FakeConnection:
    dave_session = None


class FakeClient:
    def __init__(self, chave: bytes) -> None:
        self.secret_key = list(chave)  # o py-cord guarda a chave como list[int]
        self._connection = FakeConnection()


class FakeDecryptor:
    decrypt_rtp = patch._fixed_decrypt_rtp

    def __init__(self, client: FakeClient, chave_do_box: bytes, falha: Exception | None) -> None:
        self.client = client
        self._arrodes_chave = chave_do_box
        self.atualizacoes: list[bytes] = []
        self._falha = falha

    def _decryptor_rtp(self, packet):
        if self._falha is not None:
            raise self._falha
        return b"opus"

    def update_secret_key(self, chave) -> None:
        self.atualizacoes.append(bytes(chave))
        self._arrodes_chave = bytes(chave)


class FakePacket:
    ssrc = 7
    decrypted_data = None


# ── 10b: a falha com chave diferente atualiza o box ─────────────────────────


def test_falha_com_chave_trocada_atualiza_o_leitor(caplog):
    dec = FakeDecryptor(FakeClient(CHAVE_NOVA), CHAVE_VELHA, RuntimeError("Decryption failed"))

    with caplog.at_level(logging.WARNING, logger="core.pycord_voice_patch"):
        with pytest.raises(RuntimeError):
            dec.decrypt_rtp(FakePacket())

    assert dec.atualizacoes == [CHAVE_NOVA]
    assert "chave" in caplog.text.lower()


def test_falha_com_a_mesma_chave_nao_mexe_no_leitor():
    """Pacote avulso ruim (26–29/09: 1–4 por hora) não é reconexão."""
    dec = FakeDecryptor(FakeClient(CHAVE_VELHA), CHAVE_VELHA, RuntimeError("Decryption failed"))

    with pytest.raises(RuntimeError):
        dec.decrypt_rtp(FakePacket())

    assert dec.atualizacoes == []


def test_pacote_bom_segue_como_antes():
    dec = FakeDecryptor(FakeClient(CHAVE_VELHA), CHAVE_VELHA, None)

    assert dec.decrypt_rtp(FakePacket()) == b"opus"
    assert dec.atualizacoes == []


def test_decryptor_de_verdade_lembra_a_chave_que_esta_no_box():
    reader = pytest.importorskip("discord.voice.receive.reader")
    pytest.importorskip("nacl")
    patch.apply()

    dec = reader.PacketDecryptor("aead_xchacha20_poly1305_rtpsize", CHAVE_VELHA, FakeClient(CHAVE_VELHA))
    assert patch.chave_do_leitor(dec) == CHAVE_VELHA

    box_antigo = dec.box
    dec.update_secret_key(CHAVE_NOVA)
    assert patch.chave_do_leitor(dec) == CHAVE_NOVA
    assert dec.box is not box_antigo


# ── 10a: a chave nova chega pelo gateway e vai direto para o leitor ─────────


class FakeReader:
    def __init__(self) -> None:
        self.atualizacoes: list[bytes] = []

    def update_secret_key(self, chave) -> None:
        self.atualizacoes.append(bytes(chave))


class FakeState:
    def __init__(self, reader) -> None:
        self.channel_id = 1
        self.guild_id = 2
        self.secret_key = list(CHAVE_VELHA)
        self.client = type("C", (), {"_reader": reader})()


class FakeWS:
    def __init__(self, reader) -> None:
        self.state = FakeState(reader)
        self.secret_key = None
        self.falou = []

    async def speak(self, estado) -> None:
        self.falou.append(estado)


def _load_secret_key(ws, chave: bytes):
    gateway = pytest.importorskip("discord.voice.gateway")
    patch.apply()
    return asyncio.run(gateway.VoiceWebSocket.load_secret_key(ws, {"secret_key": list(chave)}))


def test_chave_nova_do_gateway_vai_para_o_leitor():
    reader = FakeReader()
    ws = FakeWS(reader)

    _load_secret_key(ws, CHAVE_NOVA)

    assert ws.state.secret_key == list(CHAVE_NOVA)  # o upstream continua fazendo o dele
    assert ws.falou  # ...inclusive o speak(none)
    assert reader.atualizacoes == [CHAVE_NOVA]


def test_sem_leitor_a_chave_nova_nao_explode():
    ws = FakeWS(None)  # MISSING do py-cord é falsy: sem gravação não há reader

    _load_secret_key(ws, CHAVE_NOVA)

    assert ws.state.secret_key == list(CHAVE_NOVA)


def test_apply_nao_embrulha_o_load_secret_key_duas_vezes():
    gateway = pytest.importorskip("discord.voice.gateway")
    patch.apply()
    primeiro = gateway.VoiceWebSocket.load_secret_key
    patch.apply()

    assert gateway.VoiceWebSocket.load_secret_key is primeiro
