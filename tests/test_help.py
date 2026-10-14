"""O /help não pode ficar para trás de novo.

Em 21/09/2026 o Otavio abriu o /help no meio de um teste: não tinha /seek, /volume,
/diag, nem NENHUM dos comandos de voz de fila ("pula 3", "toca a terceira", "toca X
agora"…) — tudo tinha entrado no bot sem entrar no guia. Estes testes amarram o guia
ao que o bot faz de verdade: todo comando de barra registrado tem de aparecer nele, e
todo exemplo falado tem de ser uma frase que a gramática entende.
"""
import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs import help as ajuda
from cogs.diag import Diag
from cogs.favorites import Favorites
from cogs.help import Help
from cogs.music import Music
from cogs.stats import Stats
from cogs.voice import Voice
from core.intents import parse_command


@pytest.fixture(scope="module")
def comandos():
    asyncio.set_event_loop(asyncio.new_event_loop())
    bot = discord.Bot(intents=discord.Intents.default())
    for cog in (Music, Favorites, Diag, Stats, Voice, Help):
        bot.add_cog(cog(bot))
    return list(bot._pending_application_commands)


def _texto(embed: discord.Embed) -> str:
    partes = [embed.title or "", embed.description or "", embed.footer.text if embed.footer else ""]
    for campo in embed.fields:
        partes += [campo.name, campo.value]
    return "\n".join(partes)


def _tudo() -> str:
    return "\n".join(_texto(ajuda.monta_embed(chave)) for chave in ajuda.ASSUNTOS)


def test_todo_comando_de_barra_aparece_no_guia(comandos):
    guia = _tudo()
    faltando = [cmd.name for cmd in comandos if f"`/{cmd.name}" not in guia]
    assert not faltando, f"comandos fora do /help: {faltando}"


def test_o_guia_nao_anuncia_comando_que_nao_existe(comandos):
    existentes = {cmd.name for cmd in comandos}
    citados = set(re.findall(r"`/([a-z]+)", _tudo()))
    assert citados <= existentes, f"o /help cita comandos que não existem: {citados - existentes}"


def test_todo_exemplo_falado_e_uma_frase_que_o_bot_entende():
    """Exemplo entre aspas retas na página de voz = frase que a GRAMÁTICA entende
    (sem depender do LLM). Os de frase livre vão em itálico, com aspas curvas."""
    embed = ajuda.monta_embed("voz")
    frases = re.findall(r'"([^"]+)"', _texto(embed))
    assert len(frases) >= 30
    perdidas = []
    for frase in frases:
        frase = frase.replace("<música>", "evidências").replace("<nome>", "evidências")
        comando = parse_command(f"Arrodes, {frase}")
        if comando is None or comando[0] in ("unknown", "hello"):
            perdidas.append((frase, comando))
    assert not perdidas, f"exemplos do /help que o bot não entende: {perdidas}"


def test_o_que_o_bot_diz_quando_chamado_tambem_so_tem_frase_de_verdade():
    import messages as m

    for texto in (m.VOICE_HELLO, m.VOICE_LISTENING):
        for frase in re.findall(r'"([^"]+)"', texto):
            frase = frase.replace("<música>", "evidências")
            comando = parse_command(f"Arrodes, {frase}")
            assert comando is not None and comando[0] not in ("unknown", "hello"), (frase, comando)


@pytest.mark.parametrize("chave", list(ajuda.ASSUNTOS))
def test_cada_pagina_cabe_num_embed_do_discord(chave):
    embed = ajuda.monta_embed(chave)

    assert len(embed.title) <= 256 and len(embed.description or "") <= 4096
    assert len(embed.fields) <= 25
    for campo in embed.fields:
        assert 0 < len(campo.name) <= 256, campo.name
        assert 0 < len(campo.value) <= 1024, (campo.name, len(campo.value))
    assert len(_texto(embed)) <= 6000


def test_help_sem_assunto_abre_o_inicio_com_o_menu_de_assuntos():
    ctx = MagicMock()
    ctx.respond = AsyncMock()

    asyncio.run(Help(MagicMock()).help.callback(Help(MagicMock()), ctx, None))

    kwargs = ctx.respond.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == ajuda.monta_embed("inicio").title
    menu = kwargs["view"].children[0]
    assert [op.value for op in menu.options] == list(ajuda.ASSUNTOS)


def test_help_com_assunto_abre_direto_nele(comandos):
    ctx = MagicMock()
    ctx.respond = AsyncMock()
    rotulo = ajuda.ASSUNTOS["voz"]["rotulo"]

    asyncio.run(Help(MagicMock()).help.callback(Help(MagicMock()), ctx, rotulo))

    assert ctx.respond.await_args.kwargs["embed"].title == ajuda.monta_embed("voz").title
    opcao = next(cmd for cmd in comandos if cmd.name == "help").options[0]
    assert [c.name for c in opcao.choices] == [a["rotulo"] for a in ajuda.ASSUNTOS.values()]
    assert opcao.required is False


def test_o_menu_troca_de_assunto_na_mesma_mensagem():
    async def roda():
        view = ajuda.HelpView("inicio")
        interaction = MagicMock()
        interaction.data = {"values": ["fila"]}
        interaction.response.edit_message = AsyncMock()
        view._menu._interaction = interaction
        view._menu._selected_values = ["fila"]
        await view._escolheu(interaction)
        return interaction, view

    interaction, view = asyncio.run(roda())

    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["embed"].title == ajuda.monta_embed("fila").title
    assert [op.default for op in view._menu.options] == [chave == "fila" for chave in ajuda.ASSUNTOS]


def test_descricoes_cabem_no_limite_do_discord(comandos):
    """Passou de 100 caracteres, o Discord recusa o REGISTRO dos comandos na subida."""
    for cmd in comandos:
        assert 1 <= len(cmd.description) <= 100, (cmd.name, len(cmd.description))
        for opcao in cmd.options:
            assert 1 <= len(opcao.description) <= 100, (cmd.name, opcao.name, len(opcao.description))
            for escolha in opcao.choices or []:
                assert 1 <= len(escolha.name) <= 100
