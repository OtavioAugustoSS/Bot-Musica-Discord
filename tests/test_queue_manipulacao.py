"""A fila manipulada por posição e por faixa — a base dos comandos de voz de fila.

Pedido do Otavio em 20/09/2026: escolher por voz qual música da fila toca,
quantas pular, qual é a próxima, tirar uma, voltar para a anterior. Aqui só a
lógica pura. As posições são as que o painel mostra: 1 = a próxima a tocar,
contando a fila da pessoa e DEPOIS a rádio do autoplay.
"""
from core.queue import LoopMode, MusicQueue, Track


def _fila(n: int = 5, radio: int = 0) -> MusicQueue:
    fila = MusicQueue()
    fila.current = Track(title="Atual", artist="A", video_id="atual")
    fila.add_many([Track(title=f"Faixa {i}", artist="B", video_id=f"f{i}") for i in range(1, n + 1)])
    fila.add_radio([Track(title=f"Radio {i}", artist="R", video_id=f"r{i}", from_autoplay=True)
                    for i in range(1, radio + 1)])
    return fila


def _titulos(tracks) -> list[str]:
    return [t.title for t in tracks]


# ── enxergar ────────────────────────────────────────────────────────────────


def test_posicoes_contam_a_fila_e_depois_a_radio():
    fila = _fila(2, radio=2)

    assert _titulos(fila.todas()) == ["Faixa 1", "Faixa 2", "Radio 1", "Radio 2"]
    assert fila.na_posicao(1).title == "Faixa 1"
    assert fila.na_posicao(3).title == "Radio 1"
    assert fila.na_posicao(-1).title == "Radio 2"  # a última
    assert fila.na_posicao(-2).title == "Radio 1"  # a penúltima
    assert fila.na_posicao(0) is None and fila.na_posicao(9) is None and fila.na_posicao(-9) is None


def test_posicao_de_uma_faixa():
    fila = _fila(3)

    assert fila.posicao_de(fila.upcoming[2]) == 3
    assert fila.posicao_de(Track(title="de fora")) == 0


# ── tocar uma da fila ───────────────────────────────────────────────────────


def test_puxar_para_a_frente_nao_perde_ninguem():
    """ "toca a terceira": ela vira a próxima; as outras seguem na ordem."""
    fila = _fila(4)
    alvo = fila.upcoming[2]

    fila.puxa_para_a_frente(alvo)

    assert _titulos(fila.upcoming) == ["Faixa 3", "Faixa 1", "Faixa 2", "Faixa 4"]


def test_faixa_da_radio_escolhida_pela_pessoa_deixa_de_ser_radio():
    """Senão ela sumiria junto quando alguém desligasse o autoplay."""
    fila = _fila(1, radio=2)
    alvo = fila.radio[1]

    fila.puxa_para_a_frente(alvo, quem=42)

    assert fila.upcoming[0] is alvo and alvo not in fila.radio
    assert alvo.from_autoplay is False and alvo.requester_id == 42


def test_pular_para_uma_faixa_descarta_as_que_vinham_antes():
    """ "pula pra quarta": as três do meio foram puladas — é o que pular quer dizer."""
    fila = _fila(5)
    alvo = fila.upcoming[3]

    puladas = fila.descarta_ate(alvo)

    assert _titulos(puladas) == ["Faixa 1", "Faixa 2", "Faixa 3"]
    assert _titulos(fila.upcoming) == ["Faixa 4", "Faixa 5"]


def test_pular_para_uma_faixa_da_radio_leva_a_fila_inteira_junto():
    fila = _fila(2, radio=3)

    puladas = fila.descarta_ate(fila.radio[1])

    assert len(puladas) == 3 and fila.upcoming == []
    assert _titulos(fila.radio) == ["Radio 2", "Radio 3"]


def test_em_loop_de_fila_o_que_foi_pulado_volta_para_o_fim():
    fila = _fila(3)
    fila.loop_mode = LoopMode.QUEUE

    fila.descarta_da_frente(2)

    assert _titulos(fila.upcoming) == ["Faixa 3", "Faixa 1", "Faixa 2"]


def test_descartar_da_frente_conta_fila_e_radio():
    fila = _fila(1, radio=3)

    puladas = fila.descarta_da_frente(3)

    assert _titulos(puladas) == ["Faixa 1", "Radio 1", "Radio 2"]
    assert fila.upcoming == [] and _titulos(fila.radio) == ["Radio 3"]


# ── tirar ───────────────────────────────────────────────────────────────────


def test_tirar_faixas_da_fila():
    fila = _fila(4, radio=1)

    tiradas = fila.tira([fila.upcoming[1], fila.radio[0], Track(title="de fora")])

    assert _titulos(tiradas) == ["Faixa 2", "Radio 1"]
    assert _titulos(fila.todas()) == ["Faixa 1", "Faixa 3", "Faixa 4"]


# ── a anterior ──────────────────────────────────────────────────────────────


def test_avancar_guarda_o_que_ja_tocou():
    fila = _fila(3)

    fila.advance()
    fila.advance()

    assert _titulos(fila.historico) == ["Atual", "Faixa 1"]


def test_loop_de_faixa_nao_enche_o_historico_de_repeticoes():
    fila = _fila(2)
    fila.loop_mode = LoopMode.TRACK

    fila.advance()
    fila.advance()

    assert fila.historico == []


def test_voltar_para_a_anterior_e_depois_seguir_de_onde_estava():
    fila = _fila(3)
    fila.advance()  # tocando Faixa 1; histórico [Atual]

    anterior = fila.volta_para_a_anterior()
    fila.advance(ignore_loop=True)  # é o skip que o player dá em seguida

    assert anterior.title == "Atual" and fila.current is anterior
    assert _titulos(fila.upcoming) == ["Faixa 1", "Faixa 2", "Faixa 3"]  # a que tocava volta a ser a próxima
    assert fila.historico == []  # e não entrou no histórico: "anterior" de novo iria mais para trás


def test_anterior_duas_vezes_vai_cada_vez_mais_para_tras():
    fila = _fila(3)
    fila.advance()
    fila.advance()  # tocando Faixa 2; histórico [Atual, Faixa 1]

    fila.volta_para_a_anterior()
    fila.advance(ignore_loop=True)
    fila.volta_para_a_anterior()
    fila.advance(ignore_loop=True)

    assert fila.current.title == "Atual"
    assert _titulos(fila.upcoming)[:2] == ["Faixa 1", "Faixa 2"]


def test_voltar_duas_de_uma_vez_toca_na_ordem_em_que_tocaram():
    fila = _fila(3)
    fila.advance()
    fila.advance()  # tocando Faixa 2; histórico [Atual, Faixa 1]

    vai_tocar = fila.volta_para_a_anterior(2)
    fila.advance(ignore_loop=True)

    assert vai_tocar.title == "Atual" and fila.current is vai_tocar
    assert _titulos(fila.upcoming)[:3] == ["Faixa 1", "Faixa 2", "Faixa 3"]


def test_voltar_mais_do_que_ha_no_historico_vai_ate_a_primeira():
    fila = _fila(2)
    fila.advance()

    assert fila.volta_para_a_anterior(9).title == "Atual"


def test_voltar_em_loop_de_fila_nao_duplica_a_que_tocava():
    fila = _fila(2)
    fila.loop_mode = LoopMode.QUEUE
    fila.advance()  # tocando Faixa 1; Atual foi para o fim da fila

    fila.volta_para_a_anterior()
    fila.advance(ignore_loop=True)

    assert fila.current.title == "Atual"
    assert _titulos(fila.upcoming).count("Faixa 1") == 1
    assert "Atual" not in _titulos(fila.upcoming)  # nem a anterior ficou em dois lugares


def test_sem_historico_nao_ha_anterior():
    assert _fila(2).volta_para_a_anterior() is None


def test_historico_tem_teto():
    fila = _fila(60)
    for _ in range(60):
        fila.advance()

    assert len(fila.historico) == 30


def test_limpar_a_fila_mantem_o_historico_de_quem_quer_voltar():
    fila = _fila(2)
    fila.advance()

    fila.upcoming.clear()

    assert fila.volta_para_a_anterior().title == "Atual"


# ── pedido por voz entra NA FRENTE (decisão do Otavio, 21/09/2026) ──────────
# No teste dele, "quero escutar a ferida curada" e "toca Realidade ou Fantasia"
# entraram no FIM de uma fila de 38 (playlist do Spotify) e, nas duas vezes, ele
# precisou de um segundo comando para ouvir. Perguntado, escolheu: o pedido por
# voz vira a PRÓXIMA — atrás dos outros pedidos por voz que ainda não tocaram
# (ninguém fura ninguém) e na frente do resto. Nada é cortado.


def test_pedido_por_voz_entra_na_frente_da_playlist():
    fila = _fila(5)
    pedida = Track(title="Ferida Curada", artist="Zé Neto & Cristiano", video_id="fc")

    assert fila.poe_na_frente(pedida) == 1
    assert _titulos(fila.todas())[:2] == ["Ferida Curada", "Faixa 1"]


def test_pedidos_por_voz_nao_furam_uns_aos_outros():
    fila = _fila(3)
    a, b = Track(title="A", video_id="a"), Track(title="B", video_id="b")

    assert (fila.poe_na_frente(a), fila.poe_na_frente(b)) == (1, 2)
    assert _titulos(fila.todas()) == ["A", "B", "Faixa 1", "Faixa 2", "Faixa 3"]


def test_como_proxima_dito_com_todas_as_letras_continua_indo_para_a_frente_de_tudo():
    fila = _fila(3)
    fila.poe_na_frente(Track(title="A", video_id="a"))

    fila.puxa_para_a_frente(fila.na_posicao(3))  # "bota a terceira como próxima"
    fila.poe_na_frente(Track(title="B", video_id="b"))  # o pedido seguinte não passa na frente dela

    assert _titulos(fila.todas()) == ["Faixa 2", "A", "B", "Faixa 1", "Faixa 3"]


def test_pedir_por_voz_a_que_ja_esta_na_fila_muda_de_lugar_sem_duplicar():
    fila = _fila(4, radio=1)

    assert fila.poe_na_frente(fila.na_posicao(4)) == 1
    assert fila.poe_na_frente(fila.na_posicao(5)) == 2  # era da rádio: passa a ser pedido
    assert _titulos(fila.todas()) == ["Faixa 4", "Radio 1", "Faixa 1", "Faixa 2", "Faixa 3"]
    assert fila.radio == [] and fila.upcoming[1].from_autoplay is False


def test_quem_toca_deixa_de_contar_como_pedido_na_frente():
    """Em loop de fila a faixa volta para o fim: ali ela não é mais "da frente"."""
    fila = _fila(2)
    fila.loop_mode = LoopMode.QUEUE
    fila.poe_na_frente(Track(title="A", video_id="a"))

    assert fila.advance().title == "A"
    fila.advance()  # "A" volta para o fim da fila que gira

    assert fila.quantas_na_frente() == 0
    assert fila.poe_na_frente(Track(title="B", video_id="b")) == 1


def test_embaralhar_nao_enterra_os_pedidos_da_frente():
    fila = _fila(30)
    fila.poe_na_frente(Track(title="A", video_id="a"))
    fila.poe_na_frente(Track(title="B", video_id="b"))

    fila.shuffle()

    titulos = _titulos(fila.todas())
    assert titulos[:2] == ["A", "B"]
    assert sorted(titulos[2:]) == sorted(f"Faixa {i}" for i in range(1, 31))
    assert titulos[2:] != [f"Faixa {i}" for i in range(1, 31)]
