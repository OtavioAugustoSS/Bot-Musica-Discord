"""Autoplay é uma fila SEPARADA — nunca fura a fila de quem pediu.

Bug relatado pelo Otavio em 12/09: "as músicas do autoplay se embaralham no
meio das músicas da playlist que eu já tinha colocado". A causa é estrutural:
`upcoming` era uma lista só, com faixas do usuário e do autoplay misturadas.
Consequências:

  - qualquer música adicionada depois entrava ATRÁS das do autoplay;
  - `shuffle` embaralhava as duas juntas;
  - `/queue` e o painel mostravam rádio como se fosse escolha da pessoa.

Agora a rádio vive em `radio` e só é consumida quando `upcoming` acaba.
"""
from core.queue import LoopMode, MusicQueue, Track


def _t(nome: str, autoplay: bool = False) -> Track:
    return Track(title=nome, video_id=nome, from_autoplay=autoplay)


def test_fila_do_usuario_toca_toda_antes_da_radio():
    q = MusicQueue()
    q.add_many([_t("minha1"), _t("minha2")])
    q.add_radio([_t("radio1", True), _t("radio2", True)])

    assert [q.advance().title for _ in range(4)] == [
        "minha1", "minha2", "radio1", "radio2"
    ]


def test_musica_adicionada_depois_fura_a_radio():
    """O caso exato do relato: adicionar durante o autoplay."""
    q = MusicQueue()
    q.add_radio([_t("radio1", True), _t("radio2", True)])
    q.add(_t("pedi agora"))

    assert q.advance().title == "pedi agora"
    assert q.advance().title == "radio1"


def test_shuffle_nao_mistura_a_radio():
    q = MusicQueue()
    q.add_many([_t(f"minha{i}") for i in range(10)])
    q.add_radio([_t("radio1", True), _t("radio2", True)])

    q.shuffle()

    assert {t.title for t in q.upcoming} == {f"minha{i}" for i in range(10)}
    assert [t.title for t in q.radio] == ["radio1", "radio2"]


def test_upcoming_so_mostra_o_que_a_pessoa_pediu():
    """`/queue` e o painel leem `upcoming` — rádio ali confundia."""
    q = MusicQueue()
    q.add(_t("minha"))
    q.add_radio([_t("radio1", True)])

    assert [t.title for t in q.upcoming] == ["minha"]


def test_next_up_enxerga_a_radio_quando_a_fila_acaba():
    """O prefetch precisa saber qual é a próxima, venha de onde vier."""
    q = MusicQueue()
    q.add_radio([_t("radio1", True)])
    assert q.next_up().title == "radio1"

    q.add(_t("minha"))
    assert q.next_up().title == "minha"


def test_next_up_vazio():
    assert MusicQueue().next_up() is None


def test_proximas_respeita_a_ordem_das_duas_filas():
    q = MusicQueue()
    q.add(_t("minha"))
    q.add_radio([_t("radio1", True), _t("radio2", True)])

    assert [t.title for t in q.proximas(3)] == ["minha", "radio1", "radio2"]


def test_clear_limpa_as_duas():
    q = MusicQueue()
    q.add(_t("minha"))
    q.add_radio([_t("radio1", True)])

    q.clear()

    assert q.upcoming == [] and q.radio == []


def test_loop_de_fila_devolve_a_faixa_para_a_lista_certa():
    """Faixa de rádio em loop de fila não pode virar 'escolha do usuário'."""
    q = MusicQueue()
    q.loop_mode = LoopMode.QUEUE
    q.add_radio([_t("radio1", True), _t("radio2", True)])

    q.advance()  # toca radio1
    q.advance()  # toca radio2, radio1 volta para a rádio

    assert [t.title for t in q.radio] == ["radio1"]
    assert q.upcoming == []


def test_loop_de_fila_com_faixa_do_usuario_continua_igual():
    q = MusicQueue()
    q.loop_mode = LoopMode.QUEUE
    q.add_many([_t("a"), _t("b")])

    q.advance()
    q.advance()

    assert [t.title for t in q.upcoming] == ["a"]


def test_radio_respeita_o_teto_da_fila():
    q = MusicQueue()
    from core.queue import MAX_QUEUE

    q.add_radio([_t(f"r{i}", True) for i in range(MAX_QUEUE + 50)])
    assert len(q.radio) <= MAX_QUEUE


def test_remove_indexa_so_a_fila_do_usuario():
    q = MusicQueue()
    q.add_many([_t("a"), _t("b")])
    q.add_radio([_t("radio", True)])

    assert q.remove(2).title == "b"
    assert [t.title for t in q.radio] == ["radio"]


def test_substituir_a_fila_tambem_limpa_a_radio():
    """`/play modo:substituir` é trocar tudo — rádio velha não pode sobrar."""
    import asyncio

    from apoio import faz_player

    from core.player import GuildPlayer

    p = faz_player(voice=None)
    p.queue.add(_t("antiga"))
    p.queue.add_radio([_t("radio velha", True)])
    p._prefetch_task = None
    p.text_channel = None
    p.panel = type("P", (), {"ensure_visible": lambda s, c: _noop()})()

    async def _noop():
        pass

    p.play_next = lambda **k: _noop()
    p.guild = type("G", (), {"voice_client": None})()

    asyncio.run(p.replace([_t("nova")], None))

    assert [t.title for t in p.queue.upcoming] == ["nova"]
    assert p.queue.radio == []
