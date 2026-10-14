"""Os comandos de voz de fila, do que foi ouvido até a fila mudar e a resposta sair.

A gramática (tests/test_intents_fila.py) só diz O QUE foi pedido; aqui é quem
executa — o player mexe na fila e corta (ou não) a faixa atual, e a escuta
resolve o alvo ("#3", "#1-2", "hotel") contra a fila de verdade e responde.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import messages as m
from apoio import FakeChannel, faz_player
from core import listen
from core.player import _Tocada
from core.queue import Track

TITULOS = ["Pyramids", "Hotel", "New Person, Same Old Mistakes", "Nights", "Let It Happen"]
ARTISTAS = ["Frank Ocean", "Montell Fish", "Tame Impala", "Frank Ocean", "Tame Impala"]


def _tocando(n: int = 5, tocando: bool = True):
    voz = MagicMock()
    voz.is_connected.return_value = True
    voz.is_playing.return_value = tocando
    voz.is_paused.return_value = False
    canal = FakeChannel()
    player = faz_player(guild=MagicMock(voice_client=voz), channel=canal)
    player.guild.id = 1
    player.queue.current = Track(title="Les", artist="Childish Gambino", video_id="atual")
    player.queue.add_many([
        Track(title=t, artist=a, video_id=f"v{i}") for i, (t, a) in enumerate(zip(TITULOS[:n], ARTISTAS[:n]))
    ])
    fonte = MagicMock()
    player._tocada = _Tocada(track=player.queue.current, fonte=fonte, saida=None)
    player._source = fonte
    player._schedule_prefetch = MagicMock()
    player._prefetch = AsyncMock()  # aquecer a faixa antes de cortar: sem YouTube nos testes
    player.play_next = AsyncMock()
    return player, voz, canal


def _titulos(player) -> list[str]:
    return [t.title for t in player.queue.todas()]


# ── o player ────────────────────────────────────────────────────────────────


def test_pular_tres_tira_duas_da_frente_e_corta_a_atual():
    player, voz, _ = _tocando()

    proxima = player.skip(3)

    assert _titulos(player) == TITULOS[2:]
    assert proxima.title == "New Person, Same Old Mistakes"
    assert player._tocada.pulada is True
    voz.stop_playing.assert_called_once()


def test_tocar_uma_da_fila_nao_perde_as_outras():
    player, voz, _ = _tocando()
    alvo = player.queue.upcoming[2]

    asyncio.run(player.toca_da_fila(alvo))

    assert _titulos(player) == ["New Person, Same Old Mistakes", "Pyramids", "Hotel", "Nights", "Let It Happen"]
    voz.stop_playing.assert_called_once()
    # NÃO reprograma o adiantamento antes do corte: ele pediria ESTA faixa em
    # prioridade baixa, na frente do pedido de verdade (o início da faixa reprograma)
    player._schedule_prefetch.assert_not_called()


def test_pular_para_uma_faixa_descarta_as_anteriores():
    player, voz, _ = _tocando()
    alvo = player.queue.upcoming[3]

    puladas = asyncio.run(player.toca_da_fila(alvo, pulando=True))

    assert [t.title for t in puladas] == TITULOS[:3]
    assert _titulos(player) == ["Nights", "Let It Happen"]
    voz.stop_playing.assert_called_once()


def test_com_o_bot_parado_tocar_da_fila_comeca_a_tocar():
    player, voz, _ = _tocando(tocando=False)

    asyncio.run(player.toca_da_fila(player.queue.upcoming[1]))

    voz.stop_playing.assert_not_called()
    player.play_next.assert_awaited_once()


def test_como_proxima_nao_corta_a_que_esta_tocando():
    player, voz, _ = _tocando()

    player.proxima_sera(player.queue.upcoming[4])

    assert _titulos(player)[0] == "Let It Happen"
    voz.stop_playing.assert_not_called()


def test_voltar_para_a_anterior():
    player, voz, _ = _tocando()
    antes = Track(title="Redbone", artist="Childish Gambino", video_id="antes")
    player.queue.historico.append(antes)

    voltou = asyncio.run(player.anterior())

    assert voltou is antes and _titulos(player)[:2] == ["Redbone", "Les"]
    voz.stop_playing.assert_called_once()


def test_sem_historico_nao_corta_nada():
    player, voz, _ = _tocando()

    assert asyncio.run(player.anterior()) is None
    voz.stop_playing.assert_not_called()


# ── a escuta ────────────────────────────────────────────────────────────────


def _ouvinte(n: int = 5, tocando: bool = True):
    player, voz, canal = _tocando(n, tocando)
    player.espera_efeito = AsyncMock()
    return listen.VoiceListener(player, canal), player, voz, canal


def _executa(ouvinte, acao: str, arg: str, ouvido: str = "arrodes ..."):
    asyncio.run(ouvinte._execute(7, ouvido, acao, arg))


def test_pula_tres_diz_o_que_vem():
    ouvinte, player, voz, canal = _ouvinte()

    _executa(ouvinte, "skip", "3")

    assert canal.sent == [m.VOICE_SKIPPED_N.format(n=3, track="New Person, Same Old Mistakes — Tame Impala")]
    voz.stop_playing.assert_called_once()


def test_pula_mais_do_que_a_fila_tem():
    ouvinte, player, _, canal = _ouvinte(2)

    _executa(ouvinte, "skip", "9")

    assert _titulos(player) == [] and canal.sent == [m.VOICE_SKIPPED_N_END.format(n=3)]


def test_toca_a_terceira():
    ouvinte, player, voz, canal = _ouvinte()

    _executa(ouvinte, "play_queue", "#3")

    assert _titulos(player)[0] == "New Person, Same Old Mistakes"
    assert canal.sent == [m.VOICE_PLAY_QUEUE.format(track="New Person, Same Old Mistakes — Tame Impala", pos=3)]
    voz.stop_playing.assert_called_once()


def test_toca_pelo_nome_que_esta_na_fila():
    ouvinte, player, _, canal = _ouvinte()

    _executa(ouvinte, "play_queue", "hotel")

    assert _titulos(player)[0] == "Hotel"
    assert "Hotel — Montell Fish" in canal.sent[0]


def test_nome_que_nao_esta_na_fila_nao_mexe_em_nada():
    ouvinte, player, voz, canal = _ouvinte()

    _executa(ouvinte, "play_queue", "despacito")

    assert _titulos(player) == TITULOS and canal.sent == [m.VOICE_NOT_IN_QUEUE.format(alvo="despacito")]
    voz.stop_playing.assert_not_called()


def test_posicao_que_nao_existe_diz_quantas_tem():
    ouvinte, _, voz, canal = _ouvinte()

    _executa(ouvinte, "jump", "#12")

    assert canal.sent == [m.VOICE_NO_POSITION.format(total=5, pos=12)]
    voz.stop_playing.assert_not_called()


def test_fila_vazia():
    ouvinte, _, _, canal = _ouvinte(0)

    _executa(ouvinte, "play_queue", "#1")

    assert canal.sent == [m.VOICE_QUEUE_EMPTY]


def test_pula_pra_quarta_conta_as_que_ficaram_pelo_caminho():
    ouvinte, player, _, canal = _ouvinte()

    _executa(ouvinte, "jump", "#4")

    assert _titulos(player) == ["Nights", "Let It Happen"]
    assert canal.sent == [m.VOICE_JUMPED.format(track="Nights — Frank Ocean", n=3)]


def test_pular_para_a_que_ja_era_a_proxima_nao_fala_em_zero_pelo_caminho():
    ouvinte, _, _, canal = _ouvinte()

    _executa(ouvinte, "jump", "pyramids")

    assert canal.sent == [m.VOICE_JUMPED_NEXT.format(track="Pyramids — Frank Ocean")]


def test_toca_a_ultima():
    ouvinte, player, _, _ = _ouvinte()

    _executa(ouvinte, "play_queue", "#-1")

    assert _titulos(player)[0] == "Let It Happen"


def test_bota_a_quinta_como_proxima():
    ouvinte, player, voz, canal = _ouvinte()

    _executa(ouvinte, "play_next", "#5")

    assert _titulos(player)[0] == "Let It Happen"
    assert canal.sent == [m.VOICE_PLAY_NEXT.format(track="Let It Happen — Tame Impala")]
    voz.stop_playing.assert_not_called()


def test_como_proxima_uma_musica_que_nem_estava_na_fila_busca_e_poe_na_frente(monkeypatch):
    ouvinte, player, _, canal = _ouvinte()
    achada = {"title": "Redbone", "artist": "Childish Gambino", "video_id": "Kp7eSUU9oy8",
              "duration": 327, "thumbnail": ""}
    monkeypatch.setattr(listen.youtube, "search_one", AsyncMock(return_value=achada))

    _executa(ouvinte, "play_next", "redbone")

    assert _titulos(player)[0] == "Redbone" and len(_titulos(player)) == 6
    assert canal.sent == [m.VOICE_PLAY_NEXT_NEW.format(track="Redbone — Childish Gambino")]


def test_tira_as_proximas_duas():
    ouvinte, player, voz, canal = _ouvinte()

    _executa(ouvinte, "remove", "#1-2")

    assert _titulos(player) == TITULOS[2:]
    assert canal.sent == [m.VOICE_REMOVED_N.format(n=2)]
    voz.stop_playing.assert_not_called()


def test_tira_varias_posicoes_de_uma_vez():
    """É como o LLM devolve "tira as duas do Tame Impala" (validado na Groq)."""
    ouvinte, player, _, canal = _ouvinte()

    _executa(ouvinte, "remove", "#3,#5")

    assert _titulos(player) == ["Pyramids", "Hotel", "Nights"]
    assert canal.sent == [m.VOICE_REMOVED_N.format(n=2)]


def test_alvo_com_o_nome_inteiro_como_aparece_na_fila():
    """O LLM às vezes devolve "Título — Artista" em vez da posição."""
    ouvinte, player, _, _ = _ouvinte()

    _executa(ouvinte, "play_next", "Nights — Frank Ocean")

    assert _titulos(player)[0] == "Nights" and len(_titulos(player)) == 5


def test_tira_uma_pelo_nome():
    ouvinte, player, _, canal = _ouvinte()

    _executa(ouvinte, "remove", "nights")

    assert "Nights" not in _titulos(player)
    assert canal.sent == [m.VOICE_REMOVED.format(track="Nights — Frank Ocean")]


def test_o_que_tem_na_fila():
    ouvinte, _, _, canal = _ouvinte()

    _executa(ouvinte, "queue", "")

    texto = canal.embeds[0].description
    assert "`1.` Pyramids — Frank Ocean" in texto and "`5.` Let It Happen — Tame Impala" in texto


def test_qual_a_proxima():
    ouvinte, _, _, canal = _ouvinte()

    _executa(ouvinte, "next_up", "")

    assert canal.sent == [m.VOICE_NEXT_UP.format(track="Pyramids — Frank Ocean")]


def test_musica_anterior_sem_historico():
    ouvinte, _, _, canal = _ouvinte()

    _executa(ouvinte, "previous", "")

    assert canal.sent == [m.VOICE_NO_PREVIOUS]


def test_toca_x_que_ja_esta_na_fila_nao_vai_ao_youtube(monkeypatch):
    """Antes: buscava de novo e punha uma CÓPIA no fim da fila (a 51ª)."""
    ouvinte, player, voz, canal = _ouvinte()
    busca = AsyncMock()
    monkeypatch.setattr(listen.youtube, "search_one", busca)

    _executa(ouvinte, "play", "pyramids", ouvido="arrodes toca pyramids")

    busca.assert_not_awaited()
    assert len(_titulos(player)) == 5 and _titulos(player)[0] == "Pyramids"
    voz.stop_playing.assert_called_once()


def test_toca_x_que_nao_esta_na_fila_segue_buscando(monkeypatch):
    ouvinte, player, _, _ = _ouvinte()
    busca = AsyncMock(return_value=None)
    monkeypatch.setattr(listen.youtube, "search_one", busca)

    _executa(ouvinte, "play", "despacito")

    busca.assert_awaited_once_with("despacito")


def test_o_llm_recebe_a_fila_para_casar_nomes(monkeypatch):
    ouvinte, _, _, _ = _ouvinte()
    visto = {}

    async def interpreta(texto, fila=None):
        visto["fila"] = fila
        return ("none", "")

    monkeypatch.setattr(listen.llm, "interpret", interpreta)

    _executa(ouvinte, "unknown", "aquela do frank ocean", ouvido="arrodes aquela do frank ocean")

    assert visto["fila"][:2] == ["Pyramids — Frank Ocean", "Hotel — Montell Fish"]


def test_o_whisper_recebe_os_titulos_da_fila_como_contexto():
    """Título em inglês dito por brasileiro sai errado na transcrição; com os
    nomes da fila no prompt do Whisper ele acerta a grafia."""
    ouvinte, _, _, _ = _ouvinte()

    contexto = ouvinte._contexto_da_fila()

    assert "Pyramids" in contexto and "Frank Ocean" in contexto and "Les" in contexto
    assert len(contexto) <= 260


@pytest.mark.parametrize("acao", ["jump", "play_queue", "previous", "skip"])
def test_o_que_corta_o_som_espera_o_estalo(acao):
    ouvinte, player, _, _ = _ouvinte()
    player.queue.historico.append(Track(title="Redbone", artist="Childish Gambino", video_id="antes"))

    _executa(ouvinte, acao, "#2" if acao in ("jump", "play_queue") else "")

    player.espera_efeito.assert_awaited_once()


# ── o LLM ───────────────────────────────────────────────────────────────────


def test_llm_pode_pedir_as_acoes_de_fila():
    from services.llm import parse_llm_reply

    assert parse_llm_reply('{"action": "play_queue", "query": "#3"}') == ("play_queue", "#3")
    assert parse_llm_reply('{"action": "jump", "query": "#-1"}') == ("jump", "#-1")
    assert parse_llm_reply('{"action": "remove", "query": "#1-3"}') == ("remove", "#1-3")
    assert parse_llm_reply('{"action": "skip", "query": "3"}') == ("skip", "3")
    assert parse_llm_reply('{"action": "queue", "query": ""}') == ("queue", "")
    assert parse_llm_reply('{"action": "previous"}') == ("previous", "")
    # sem dizer QUAL, não é comando
    for acao in ("jump", "play_queue", "play_next", "remove"):
        assert parse_llm_reply(f'{{"action": "{acao}", "query": ""}}') is None


def test_a_fila_vai_na_mensagem_para_o_llm(monkeypatch):
    from services import llm

    visto = {}

    class Resposta:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return {"choices": [{"message": {"content": '{"action": "play_queue", "query": "#2"}'}}]}

    class Sessao:
        def post(self, url, **kw):
            visto.update(kw["json"])
            return Resposta()

    monkeypatch.setattr(llm.config, "GROQ_API_KEY", "chave")
    monkeypatch.setattr(llm.http, "get_session", lambda: Sessao())

    resultado = asyncio.run(llm.interpret("aquela do montell", fila=["Pyramids — Frank Ocean", "Hotel — Montell Fish"]))

    assert resultado == ("play_queue", "#2")
    mensagem = visto["messages"][-1]["content"]
    assert "aquela do montell" in mensagem and "2. Hotel — Montell Fish" in mensagem


# ── ativação falsa: o nome no meio da conversa ──────────────────────────────


def test_wake_word_no_meio_da_conversa_e_nenhum_comando_o_bot_fica_quieto(monkeypatch):
    from core.intents import chamou_de_frente, parse_command

    # "Herodes" é grafia aceita do nome; "arrozes" (0,86) é palavra de verdade e
    # nem chama o bot
    frase = "Não, do Herodes que eu te falei."
    ouvinte, _, _, canal = _ouvinte()
    monkeypatch.setattr(listen.llm, "interpret", AsyncMock(return_value=("none", "")))

    assert parse_command("Não, dos arrozes que eu te falei.") is None
    assert parse_command(frase) == ("unknown", "que eu te falei")
    assert chamou_de_frente(frase) is False
    asyncio.run(ouvinte._execute(7, frase, "unknown", "que eu te falei", de_frente=False))

    assert canal.sent == []


def test_quem_chamou_de_frente_e_nao_foi_entendido_continua_sendo_avisado(monkeypatch):
    from core.intents import chamou_de_frente

    frase = "Arrodes, faz um negócio aí"
    ouvinte, _, _, canal = _ouvinte()
    monkeypatch.setattr(listen.llm, "interpret", AsyncMock(return_value=("none", "")))

    assert chamou_de_frente(frase) and chamou_de_frente("E aí, a rodes, pula")
    asyncio.run(ouvinte._execute(7, frase, "unknown", "faz um negocio ai", de_frente=True))

    assert canal.sent == [m.VOICE_UNKNOWN.format(heard=frase)]


def test_embed_do_spotify_sem_espaco_invisivel():
    from services.spotify_api import faixas_do_embed

    faixas = faixas_do_embed({"trackList": [
        {"title": "All I Want Is You (feat. J. Cole)", "subtitle": "Miguel," + chr(160) + "J. Cole",
         "duration": 298000},
        {"title": "", "subtitle": "sem título não entra"},
    ]})

    assert faixas == [{"title": "All I Want Is You (feat. J. Cole)", "artist": "Miguel, J. Cole",
                       "duration": 298, "thumbnail": ""}]


# ── o corte sem silêncio e a resposta sem espera (fumaça na VM, 21/09) ──────
# Na VM, "toca a última" levou 7,9s para responder: a extração entrou ATRÁS de um
# adiantamento na fila do pool e a mensagem só saía depois do corte. E todo corte
# manual tinha ~0,4s de silêncio até o FFmpeg novo dar áudio.


def test_a_resposta_sai_antes_da_espera_pela_extracao():
    ouvinte, player, _, canal = _ouvinte()
    ordem = []

    async def toca(track, **kw):
        ordem.append(("cortando", list(canal.sent)))
        return []

    player.toca_da_fila = toca

    _executa(ouvinte, "play_queue", "#3")

    assert ordem[0][1] == [m.VOICE_PLAY_QUEUE.format(track="New Person, Same Old Mistakes — Tame Impala", pos=3)]


def test_pular_para_diz_quantas_ficaram_pelo_caminho_sem_esperar_o_corte():
    ouvinte, player, _, canal = _ouvinte()
    player.toca_da_fila = AsyncMock(return_value=[])

    _executa(ouvinte, "jump", "#4")

    assert canal.sent == [m.VOICE_JUMPED.format(track="Nights — Frank Ocean", n=3)]
    player.toca_da_fila.assert_awaited_once()
    assert player.toca_da_fila.await_args.kwargs["pulando"] is True


def test_extracao_para_tocar_agora_fura_a_fila_do_pool(monkeypatch):
    import core.player as player_mod

    player, voz, _ = _tocando()
    del player._prefetch  # o de verdade
    pedidos = []

    async def stream(video_id, **kw):
        pedidos.append((video_id, kw))
        return {"duration": 100, "thumbnail": ""}

    monkeypatch.setattr(player_mod.youtube, "get_stream", stream)
    monkeypatch.setattr(player_mod.youtube, "stream_state", lambda vid: "fria")
    player.prepara_o_corte = AsyncMock()
    adiantamento = MagicMock()
    adiantamento.done.return_value = False
    player._prefetch_task = adiantamento

    asyncio.run(player.toca_da_fila(player.queue.upcoming[3]))

    assert pedidos == [("v3", {})]  # prioridade de quem vai tocar AGORA, não a do adiantamento
    adiantamento.cancel.assert_called_once()  # os adiantamentos pendentes saem da frente
    player.prepara_o_corte.assert_awaited_once()
    voz.stop_playing.assert_called_once()


def test_corte_pre_monta_a_faixa_e_so_entao_corta(monkeypatch):
    import core.player as player_mod

    player, voz, _ = _tocando()
    alvo = player.queue.upcoming[2]
    ordem = []
    monkeypatch.setattr(player_mod.youtube, "stream_state", lambda vid: "no cache")

    async def prepara(tocada, proxima=None):
        ordem.append(("montou", proxima.title))

    player._prepara_a_proxima = prepara
    voz.stop_playing.side_effect = lambda: ordem.append(("cortou", None))

    asyncio.run(player.toca_da_fila(alvo))

    assert ordem == [("montou", "New Person, Same Old Mistakes"), ("cortou", None)]


def test_pula_tres_tambem_monta_a_que_vai_entrar(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    vistas = []

    async def prepara(track=None, espera_s=1.5):
        vistas.append(player.queue.next_up().title)  # as do meio já saíram da fila

    player.prepara_o_corte = prepara

    _executa(ouvinte, "skip", "3")

    assert vistas == ["New Person, Same Old Mistakes"]
    assert canal.sent == [m.VOICE_SKIPPED_N.format(n=3, track="New Person, Same Old Mistakes — Tame Impala")]
    voz.stop_playing.assert_called_once()


def test_pula_por_voz_monta_a_proxima_enquanto_o_estalo_toca():
    """São 0,72s de estalo antes do corte: o FFmpeg da próxima abre nesse tempo."""
    ouvinte, player, voz, _ = _ouvinte()
    player.prepara_o_corte = AsyncMock()

    _executa(ouvinte, "skip", "")

    player.prepara_o_corte.assert_awaited_once()
    player.espera_efeito.assert_awaited_once()
    voz.stop_playing.assert_called_once()


# ── o teste do Otavio de 21/09 (fila de 38 do Spotify) ──────────────────────
# "pula três" caiu numa faixa que ninguém tinha extraído (o adiantamento só cobre
# as 2 próximas) e o corte veio ANTES da extração: 6,8s de silêncio no journal
# (busca 0,7s + extração 4,4s fria + FFmpeg 1,8s). "toca a terceira" e "pula pra
# quinta" já extraíam com a música tocando — "pula N" passa a ser igual.


def test_pula_tres_extrai_a_que_vai_entrar_antes_de_cortar(monkeypatch):
    import core.player as player_mod

    ouvinte, player, voz, canal = _ouvinte()
    ordem = []
    monkeypatch.setattr(player_mod.youtube, "stream_state", lambda vid: "fria")

    async def extrai(track, agora=False):
        ordem.append(("extraiu", track.title, agora, list(canal.sent)))

    player._prefetch = extrai
    player.prepara_o_corte = AsyncMock()
    voz.stop_playing.side_effect = lambda: ordem.append(("cortou",))

    _executa(ouvinte, "skip", "3")

    resposta = m.VOICE_SKIPPED_N.format(n=3, track="New Person, Same Old Mistakes — Tame Impala")
    # a resposta sai antes da espera (~5s na VM), a extração vem antes do corte
    assert ordem == [("extraiu", "New Person, Same Old Mistakes", True, [resposta]), ("cortou",)]
    assert _titulos(player) == TITULOS[2:]


def test_o_estalo_toca_enquanto_a_faixa_e_extraida(monkeypatch):
    """Eram 0,72s de estalo ANTES de começar a extrair: no journal, 1,3s entre
    "toca a terceira" e a extração começar. O estalo só não pode ser cortado —
    e o corte vem segundos depois."""
    import core.player as player_mod

    player, voz, _ = _tocando()
    ordem = []
    monkeypatch.setattr(player_mod.youtube, "stream_state", lambda vid: "fria")

    async def estalo():
        ordem.append("estalo começou")
        await asyncio.sleep(0.03)
        ordem.append("estalo acabou")

    async def extrai(track, agora=False):
        ordem.append("extração começou")
        await asyncio.sleep(0.01)
        ordem.append("extração acabou")

    player.espera_efeito = estalo
    player._prefetch = extrai
    player.prepara_o_corte = AsyncMock()
    voz.stop_playing.side_effect = lambda: ordem.append("cortou")

    asyncio.run(player.toca_da_fila(player.queue.upcoming[3]))

    assert ordem == ["estalo começou", "extração começou", "extração acabou", "estalo acabou", "cortou"]


def test_com_o_bot_parado_o_estalo_tambem_nao_e_atropelado():
    player, _, _ = _tocando(tocando=False)
    player.espera_efeito = AsyncMock()

    asyncio.run(player.toca_da_fila(player.queue.upcoming[1]))

    player.espera_efeito.assert_awaited_once()
    player.play_next.assert_awaited_once()


# ── pedido por voz entra como a PRÓXIMA (decisão do Otavio, 21/09) ──────────
# "quero escutar a ferida curada" e "toca Realidade ou Fantasia" entraram no fim
# de uma fila de 38, e ele precisou de um segundo comando nas duas vezes.


def _achou(monkeypatch, titulo="Ferida Curada", artista="Zé Neto & Cristiano", video_id="fc"):
    busca = AsyncMock(return_value={
        "title": titulo, "artist": artista, "video_id": video_id, "duration": 159, "thumbnail": "",
    })
    monkeypatch.setattr(listen.youtube, "search_one", busca)
    return busca


def test_toca_x_com_fila_entra_como_a_proxima_sem_cortar_nada(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    _achou(monkeypatch)

    _executa(ouvinte, "play", "ferida curada", ouvido="Arrodes, toca ferida curada")

    assert _titulos(player)[:2] == ["Ferida Curada", "Pyramids"]
    assert canal.sent == [m.VOICE_HEARD_PLAY_NEXT.format(
        heard="Arrodes, toca ferida curada", track="Ferida Curada — Zé Neto & Cristiano")]
    voz.stop_playing.assert_not_called()
    player._schedule_prefetch.assert_called()  # a pedida entra no adiantamento já


def test_segundo_pedido_por_voz_entra_atras_do_primeiro(monkeypatch):
    ouvinte, player, _, canal = _ouvinte()
    _achou(monkeypatch)
    _executa(ouvinte, "play", "ferida curada")
    _achou(monkeypatch, "Evidências", "Chitãozinho & Xororó", "ev")

    _executa(ouvinte, "play", "evidencias", ouvido="Arrodes, toca evidências")

    assert _titulos(player)[:3] == ["Ferida Curada", "Evidências", "Pyramids"]
    assert canal.sent[-1] == m.VOICE_HEARD_PLAY_POS.format(
        heard="Arrodes, toca evidências", track="Evidências — Chitãozinho & Xororó", pos=2)


def test_toca_x_com_a_fila_vazia_responde_como_sempre(monkeypatch):
    ouvinte, player, _, canal = _ouvinte(0)
    _achou(monkeypatch)

    _executa(ouvinte, "play", "ferida curada", ouvido="Arrodes, toca ferida curada")

    assert canal.sent == [m.VOICE_HEARD_PLAY.format(
        heard="Arrodes, toca ferida curada", track="Ferida Curada — Zé Neto & Cristiano")]
    assert _titulos(player) == ["Ferida Curada"]


def test_toca_x_agora_corta_para_ela(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    _achou(monkeypatch)

    _executa(ouvinte, "play_now", "ferida curada", ouvido="Arrodes, toca ferida curada agora")

    assert _titulos(player)[:2] == ["Ferida Curada", "Pyramids"]  # é a da frente: o corte cai nela
    assert canal.sent == [m.VOICE_PLAY_NOW.format(
        heard="Arrodes, toca ferida curada agora", track="Ferida Curada — Zé Neto & Cristiano")]
    voz.stop_playing.assert_called_once()
    player._prefetch.assert_awaited()  # extraída ANTES do corte, com a música tocando


def test_toca_x_agora_com_x_na_fila_puxa_de_la(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    busca = _achou(monkeypatch)

    _executa(ouvinte, "play_now", "nights")

    busca.assert_not_awaited()
    assert _titulos(player)[0] == "Nights" and len(_titulos(player)) == 5
    assert canal.sent == [m.VOICE_PLAY_QUEUE.format(track="Nights — Frank Ocean", pos=4)]
    voz.stop_playing.assert_called_once()


def test_toca_x_agora_com_o_bot_parado_so_toca(monkeypatch):
    ouvinte, player, voz, _ = _ouvinte(0, tocando=False)
    _achou(monkeypatch)

    _executa(ouvinte, "play_now", "ferida curada")

    player.play_next.assert_awaited_once()
    voz.stop_playing.assert_not_called()


# ── teste do Otavio de 21/09, 03:30–03:35 UTC: "So Am I" e "in the pool" ─────
# "toca So Am I, de Ava Max" entrou como a próxima (certo). Ele repetiu o pedido
# mais duas vezes — o Whisper escreveu "Soul A M I" e "sou M.I." — e a busca achou
# a MESMA música: três cópias na fila, e ela nunca tocou. Pedir de novo o que já
# está na fila é insistir: toca AGORA (a mesma regra do "toca X" com X na fila).


def test_pedir_de_novo_a_musica_que_a_busca_acha_igual_nao_duplica_e_toca_agora(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    _achou(monkeypatch, "Nights", "Frank Ocean", "v3")  # a 4ª da fila, achada pela BUSCA

    _executa(ouvinte, "play", "naites do franqui oxean", ouvido="Arrodes, toca naites")

    assert len(_titulos(player)) == 5 and _titulos(player)[0] == "Nights"
    assert canal.sent == [m.VOICE_PLAY_QUEUE.format(track="Nights — Frank Ocean", pos=4)]
    voz.stop_playing.assert_called_once()


def test_pedir_a_musica_que_ja_esta_tocando_so_avisa(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    _achou(monkeypatch, "Les", "Childish Gambino", "atual")

    _executa(ouvinte, "play", "les do childish gambino")

    assert _titulos(player) == TITULOS
    assert canal.sent == [m.VOICE_ALREADY_PLAYING.format(track="Les — Childish Gambino")]
    voz.stop_playing.assert_not_called()


# "Herodes, toca agora in the pool": o "agora" foi junto para a busca e o YouTube
# Music devolveu um cover de piano (com "in the pool" limpo, a 1ª é a original).
# Mas "agora" também abre nome de música ("Agora Só Falta Você"): as duas buscas
# saem juntas, e o "agora" só é comando se a música achada não o tiver no nome.


def _buscas(monkeypatch, respostas: dict):
    pedidas = []

    async def busca(consulta):
        pedidas.append(consulta)
        achado = respostas.get(consulta)
        if achado is None:
            return None
        titulo, artista, video_id = achado
        return {"title": titulo, "artist": artista, "video_id": video_id, "duration": 200, "thumbnail": ""}

    monkeypatch.setattr(listen.youtube, "search_one", busca)
    return pedidas


def test_toca_agora_x_busca_sem_o_agora_e_corta_para_ela(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    pedidas = _buscas(monkeypatch, {
        "agora in the pool": ("in the pool", "The Dreamer Piano", "piano"),
        "in the pool": ("in the pool", "kensuke ushio", "original"),
    })

    _executa(ouvinte, "play_now", "agora in the pool", ouvido="Herodes, toca agora in the pool.")

    assert sorted(pedidas) == ["agora in the pool", "in the pool"]
    assert player.queue.upcoming[0].video_id == "original"
    assert canal.sent == [m.VOICE_PLAY_NOW.format(
        heard="Herodes, toca agora in the pool.", track="in the pool — kensuke ushio")]
    voz.stop_playing.assert_called_once()


def test_agora_que_e_o_nome_da_musica_nao_e_comando(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    _buscas(monkeypatch, {
        "agora so falta voce": ("Agora Só Falta Você", "Rita Lee", "rita"),
        "so falta voce": ("Só Falta Você (Ao Vivo)", "Jorge & Mateus", "jm"),
    })

    _executa(ouvinte, "play_now", "agora so falta voce", ouvido="Arrodes, toca agora só falta você")

    assert player.queue.upcoming[0].video_id == "rita"  # entra como a próxima, sem cortar
    assert canal.sent == [m.VOICE_HEARD_PLAY_NEXT.format(
        heard="Arrodes, toca agora só falta você", track="Agora Só Falta Você — Rita Lee")]
    voz.stop_playing.assert_not_called()


def test_seek_para_alem_do_fim_avisa_em_vez_de_acabar_com_a_musica():
    ouvinte, player, _, canal = _ouvinte()
    player.pede_seek = AsyncMock(return_value=("alem", 259))

    _executa(ouvinte, "seek", "1800")

    assert canal.sent == [m.VOICE_SEEK_BEYOND.format(dur="4:19")]


# ── música que ninguém pediu: conversa que o LLM leu como pedido ────────────


@pytest.mark.parametrize("frase, busca", [
    ("Arrodes, eu acho que o vídeo que eu vou postar não ficou muito bom.", "algo mais animado"),
    ("Arrodes, a gente nem falou da praia tipo aí, sabe.", "Praia"),
    ("Arrodes, Brincadeira, agora sim.", "Brincadeira"),
    ("Arrodes, Eita.", "Eita"),
    ("Arrodes, eu quero sentir a vibe, Arrodes.", "Vibe"),
])
def test_play_do_llm_sem_verbo_de_pedido_nao_toca_nada(monkeypatch, frase, busca):
    ouvinte, player, _, canal = _ouvinte()
    busca_yt = AsyncMock()
    monkeypatch.setattr(listen.youtube, "search_one", busca_yt)
    monkeypatch.setattr(listen.llm, "interpret", AsyncMock(return_value=("play", busca)))

    asyncio.run(ouvinte._execute(7, frase, "unknown", "x", de_frente=True))

    busca_yt.assert_not_awaited()
    assert canal.sent == [] and len(_titulos(player)) == 5


def test_play_do_llm_com_verbo_de_pedido_segue_tocando(monkeypatch):
    ouvinte, _, _, _ = _ouvinte()
    busca_yt = AsyncMock(return_value=None)
    monkeypatch.setattr(listen.youtube, "search_one", busca_yt)
    monkeypatch.setattr(listen.llm, "interpret", AsyncMock(return_value=("play", "Sexy Narcisista")))

    frase = "Arrodes, toca, sexy, narcisista e violent vaira."
    asyncio.run(ouvinte._execute(7, frase, "unknown", "x", de_frente=True))

    busca_yt.assert_awaited_once_with("Sexy Narcisista")


def test_llm_apontando_a_fila_nao_precisa_de_verbo():
    frase = "Arrodes, a do tame impala depois dessa"
    assert listen._play_sem_pedido(frase, "play_next", "#3") is False
    assert listen._play_sem_pedido(frase, "remove", "Nights") is False
    assert listen._play_sem_pedido(frase, "play_next", "Nights") is True


def test_wake_word_no_meio_nem_chega_ao_llm(monkeypatch):
    frase = "Músicas Pai Arrodes e Tuntum 2."
    ouvinte, _, _, canal = _ouvinte()
    interpreta = AsyncMock(return_value=("play", "Pai Arrodes, Tuntum 2"))
    monkeypatch.setattr(listen.llm, "interpret", interpreta)

    asyncio.run(ouvinte._execute(7, frase, "unknown", "tuntum 2", de_frente=False))

    interpreta.assert_not_awaited()
    assert canal.sent == []


# ── "no final da fila" (pedido do Otavio em 28/09) ───────────────────────────
# Todo pedido por voz entra como a PRÓXIMA (decisão de 21/09); "coloca X no final
# da fila" é o jeito de dizer que dessa vez é para esperar a vez.

_REDBONE = {"title": "Redbone", "artist": "Childish Gambino", "video_id": "Kp7eSUU9oy8",
            "duration": 327, "thumbnail": ""}


def test_no_final_da_fila_entra_no_fim(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    monkeypatch.setattr(listen.youtube, "search_one", AsyncMock(return_value=_REDBONE))
    player.enqueue = AsyncMock()

    _executa(ouvinte, "play_end", "redbone", ouvido="arrodes coloca redbone no final da fila")

    player.enqueue.assert_awaited_once()
    assert player.enqueue.await_args.kwargs.get("na_frente", False) is False
    assert canal.sent == [m.VOICE_HEARD_PLAY_END.format(
        heard="arrodes coloca redbone no final da fila", track="Redbone — Childish Gambino", pos=6)]
    voz.stop_playing.assert_not_called()


def test_no_final_da_fila_com_ela_ja_na_fila_nao_corta_nem_duplica(monkeypatch):
    ouvinte, player, voz, canal = _ouvinte()
    busca = AsyncMock()
    monkeypatch.setattr(listen.youtube, "search_one", busca)
    player.enqueue = AsyncMock()

    _executa(ouvinte, "play_end", "pyramids")

    busca.assert_not_awaited()
    player.enqueue.assert_not_awaited()
    assert _titulos(player) == TITULOS
    assert canal.sent == [m.VOICE_JA_NA_FILA.format(track="Pyramids — Frank Ocean", pos=1)]
    voz.stop_playing.assert_not_called()


@pytest.mark.parametrize("frase,esperado", [
    ("Arrodes, coloca Evidências no final da fila", ("play_end", "evidencias")),
    ("Arrodes, adiciona Evidências no fim da fila", ("play_end", "evidencias")),
    ("Arrodes, bota Evidências no final", ("play_end", "evidencias")),
    ("Arrodes, coloca Evidências no final da lista", ("play_end", "evidencias")),
    ("Arrodes, coloca Evidências na fila", ("play", "evidencias")),  # "na fila" segue: próxima
])
def test_gramatica_do_final_da_fila(frase, esperado):
    from core.intents import parse_command

    assert parse_command(frase) == esperado
