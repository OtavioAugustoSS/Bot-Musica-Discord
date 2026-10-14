"""Boas-vindas ao entrar num servidor novo.

Hoje o bot entra e não acontece nada — ninguém descobre que existe `/play`
nem que dá para falar com ele. Com o multi-servidor chegando, um servidor
novo tem que saber o que fazer com ele no primeiro minuto.
"""
import asyncio
from unittest.mock import MagicMock

from cogs.lifecycle import Lifecycle


class FakeCanal:
    def __init__(self, pode_escrever: bool = True, nome: str = "geral") -> None:
        self.pode_escrever = pode_escrever
        self.name = nome
        self.enviadas: list = []

    def permissions_for(self, membro):
        p = MagicMock()
        p.send_messages = self.pode_escrever
        p.embed_links = self.pode_escrever
        return p

    async def send(self, content=None, **kwargs):
        self.enviadas.append(content or kwargs.get("embed"))


class FakeGuild:
    def __init__(self, canais, system_channel=None) -> None:
        self.id = 1
        self.name = "Servidor Novo"
        self.text_channels = canais
        self.system_channel = system_channel
        self.me = MagicMock()


def _cog() -> Lifecycle:
    bot = MagicMock()
    return Lifecycle(bot)


def test_manda_boas_vindas_no_canal_de_sistema():
    sistema = FakeCanal(nome="boas-vindas")
    outro = FakeCanal(nome="geral")
    guild = FakeGuild([outro, sistema], system_channel=sistema)

    asyncio.run(_cog().on_guild_join(guild))

    assert len(sistema.enviadas) == 1
    assert outro.enviadas == []


def test_sem_canal_de_sistema_usa_o_primeiro_que_da_para_escrever():
    mudo = FakeCanal(pode_escrever=False, nome="anuncios")
    bom = FakeCanal(nome="geral")
    guild = FakeGuild([mudo, bom], system_channel=None)

    asyncio.run(_cog().on_guild_join(guild))

    assert len(bom.enviadas) == 1
    assert mudo.enviadas == []


def test_canal_de_sistema_sem_permissao_cai_para_outro():
    sistema = FakeCanal(pode_escrever=False, nome="regras")
    bom = FakeCanal(nome="geral")
    guild = FakeGuild([bom], system_channel=sistema)

    asyncio.run(_cog().on_guild_join(guild))

    assert len(bom.enviadas) == 1


def test_servidor_sem_nenhum_canal_escrevivel_nao_quebra():
    mudo = FakeCanal(pode_escrever=False)
    guild = FakeGuild([mudo], system_channel=None)

    asyncio.run(_cog().on_guild_join(guild))  # não pode levantar

    assert mudo.enviadas == []


def test_erro_do_discord_nao_derruba_o_evento():
    class CanalQuebrado(FakeCanal):
        async def send(self, content=None, **kwargs):
            raise RuntimeError("Discord fora")

    guild = FakeGuild([CanalQuebrado()], system_channel=None)

    asyncio.run(_cog().on_guild_join(guild))  # não pode levantar


def test_mensagem_ensina_o_basico():
    import messages as m

    texto = m.GUILD_WELCOME
    assert "/play" in texto
    assert "/help" in texto
