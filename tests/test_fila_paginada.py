"""A fila inteira, por páginas — também quando pedida por voz.

06/10, pedido do Otavio: "mostra a fila" por voz trazia só as 10 primeiras e um
"… e mais 40". Quase sempre a fila é uma playlist de 50+: ele quer ver todas,
mas não uma embaixo da outra — em páginas, com um menu como o do /help.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from apoio import faz_player
from cogs.music import QueueView
from core import listen
from core.queue import Track


def _player(n: int):
    p = faz_player(voice=None)
    for i in range(1, n + 1):
        p.queue.add(Track(title=f"musica {i}", video_id=f"v{i}", duration=200))
    return p


def _view(p) -> QueueView:
    async def monta():
        return QueueView(p)

    return asyncio.run(monta())


def test_menu_de_paginas_cobre_a_fila_toda():
    view = _view(_player(53))

    opcoes = [o.label for o in view._paginas.options]
    assert opcoes[0].startswith("1–10") and opcoes[-1].startswith("51–53")
    assert len(opcoes) == 6


def test_escolher_a_pagina_no_menu_mostra_aquelas_musicas():
    view = _view(_player(53))
    view.page = 4
    view._sync()

    embed = view.build_embed()

    assert "musica 41" in embed.description and "musica 50" in embed.description
    assert "musica 40" not in embed.description
    assert "Página 5/6" in embed.footer.text


def test_fila_de_uma_pagina_nao_tem_menu_de_paginas():
    view = _view(_player(7))

    assert view._paginas not in view.children


def test_fila_enorme_respeita_o_limite_de_25_opcoes_do_discord():
    view = _view(_player(400))
    view.page = 30
    view._sync()

    opcoes = view._paginas.options
    assert len(opcoes) <= 25
    assert any(o.default and o.label.startswith("301–310") for o in opcoes)


def test_pedir_a_fila_por_voz_manda_a_fila_paginada():
    p = _player(53)
    canal = MagicMock()
    canal.send = AsyncMock()
    escuta = listen.VoiceListener(p, canal)

    asyncio.run(escuta._execute(7, "Arrodes, mostra a fila", "queue", ""))

    kwargs = canal.send.await_args.kwargs
    assert isinstance(kwargs["view"], QueueView)
    assert "musica 10" in kwargs["embed"].description
    assert "Página 1/6" in kwargs["embed"].footer.text
