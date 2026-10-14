"""Voz: mexer na FILA — pular N, escolher qual toca, qual é a próxima, tirar, voltar.

Pedido do Otavio em 20/09/2026, depois do teste do seek/volume: "poder selecionar
as músicas que estão na fila nos comandos por voz, quantas quero pular, qual quero
que toque". A gramática devolve (ação, alvo); o alvo é "#N" (posição do painel:
1 = a próxima; "#-1" = a última; "#1-3" = da 1 à 3) ou o NOME dito — quem casa o
nome com a fila é quem executa (intents.acha_na_fila), porque só ele vê a fila.

Dois verbos, dois sentidos, de propósito:
  - "toca a quinta" PUXA a quinta para agora e não perde ninguém (as outras
    seguem na ordem);
  - "pula pra quinta" PULA as quatro que vinham antes — é o que pular quer dizer.
"""
import pytest

from core.intents import acha_na_fila, parse_command
from core.queue import Track


def _c(frase: str):
    return parse_command(f"Arrodes, {frase}")


# ── pular N ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("frase,esperado", [
    ("pula 3", ("skip", "3")),
    ("pula três músicas", ("skip", "3")),
    ("pula duas", ("skip", "2")),
    ("avança 2 músicas", ("skip", "2")),
    ("passa 4 faixas", ("skip", "4")),
    ("pula umas cinco", ("skip", "5")),
    ("pula uma", ("skip", "")),
    ("pula", ("skip", "")),
    ("pula essa", ("skip", "")),
    ("pula 15 segundos", ("seek", "+15")),  # com unidade de tempo é seek
])
def test_pular_varias(frase, esperado):
    assert _c(frase) == esperado


# ── pular PARA uma faixa (as de antes são puladas) ──────────────────────────


@pytest.mark.parametrize("frase,esperado", [
    ("pula pra quinta", ("jump", "#5")),
    ("pula para a música 4", ("jump", "#4")),
    ("vai pra terceira música", ("jump", "#3")),
    ("vai pra número 7", ("jump", "#7")),
    ("pula até a 6", ("jump", "#6")),
    ("avança pra última", ("jump", "#-1")),
    ("pula pra décima segunda", ("jump", "#12")),
    ("pula pra pyramids", ("jump", "pyramids")),
    ("vai pra música do frank ocean", ("jump", "frank ocean")),
    ("pula pra próxima", ("skip", "")),
    ("vai pro minuto 2", ("seek", "120")),
    ("vai embora", ("leave", "")),
])
def test_pular_para(frase, esperado):
    assert _c(frase) == esperado


# ── tocar AGORA uma da fila (ninguém se perde) ──────────────────────────────


@pytest.mark.parametrize("frase,esperado", [
    ("toca a terceira", ("play_queue", "#3")),
    ("toca a terceira da fila", ("play_queue", "#3")),
    ("toca a número 5", ("play_queue", "#5")),
    ("toca a 2", ("play_queue", "#2")),
    ("toca a música 4 da fila", ("play_queue", "#4")),
    ("toca a última", ("play_queue", "#-1")),
    ("toca a penúltima da fila", ("play_queue", "#-2")),
    ("bota a quinta pra tocar", ("play_queue", "#5")),
    ("toca hotel da fila", ("play_queue", "hotel")),
    ("toca pyramids que tá na fila", ("play_queue", "pyramids")),
    ("toca a do tame impala que está na fila", ("play_queue", "tame impala")),
    ("toca a próxima", ("skip", "")),
])
def test_tocar_uma_da_fila(frase, esperado):
    assert _c(frase) == esperado


@pytest.mark.parametrize("frase,esperado", [
    ("toca a quinta sinfonia de beethoven", ("play", "quinta sinfonia de beethoven")),
    ("toca 1979", ("play", "1979")),
    ("toca três", ("play", "tres")),
    ("toca a segunda-feira", ("play", "segunda feira")),
    ("toca pyramids", ("play", "pyramids")),  # quem confere a fila é quem executa
    ("toca as favoritas", ("play_favs", "")),
    ("toca 30 seconds to mars", ("play", "30 seconds to mars")),
])
def test_o_que_continua_sendo_busca(frase, esperado):
    assert _c(frase) == esperado


# ── como PRÓXIMA ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("frase,esperado", [
    ("bota a quinta como próxima", ("play_next", "#5")),
    ("coloca hotel como próxima", ("play_next", "hotel")),
    ("toca a quarta em seguida", ("play_next", "#4")),
    ("toca a quarta depois dessa", ("play_next", "#4")),
    ("depois toca a terceira", ("play_next", "#3")),
    ("bota pyramids de próxima", ("play_next", "pyramids")),
    ("a próxima é a sexta", ("play_next", "#6")),
])
def test_como_proxima(frase, esperado):
    assert _c(frase) == esperado


# 27/09, frases reais do journal: a marca de "próxima" dita ANTES do nome ia
# inteira para a busca ("toca como próxima música…" tocou "A próxima vítima"),
# e o "com a" da transcrição no lugar de "como a" virou tocar DA fila
@pytest.mark.parametrize("frase,esperado", [
    ("coloca como próxima música, Se Eu Fosse Você, de Henrique Juliano.",
     ("play_next", "se eu fosse voce de henrique juliano")),
    ("COLOCA COMO PRÓXIMA MÚSICA ARRAINHÃO DE HENRIQUE JULIANO.",
     ("play_next", "arrainhao de henrique juliano")),
    ("coloca como a próxima Última Noite", ("play_next", "ultima noite")),
    ("coloca Amigo da Minha Saudade com a próxima música.",
     ("play_next", "amigo da minha saudade")),
    ("toca, jeito carinhoso de Jesse Jetson, com a próxima da fila.",
     ("play_next", "jeito carinhoso de jesse jetson")),
    ("toca como próxima música...", ("hello", "")),
    ("coloca a segunda música da fila como a próxima a tocar.", ("play_next", "#2")),
    ("coloca a segunda como próxima a tocar", ("play_next", "#2")),
    ("coloca hotel como a próxima a tocar", ("play_next", "hotel")),
])
def test_como_proxima_na_frente_do_nome(frase, esperado):
    assert _c(frase) == esperado


@pytest.mark.parametrize("frase,esperado", [
    ("COLOCA NA FILA JEITO CARINHOSO.", ("play", "jeito carinhoso")),
    ("COLOCA NA FILA E ESQUECI VOCÊ DE HENRIQUE E DIEGO.",
     ("play", "e esqueci voce de henrique e diego")),
    ("bota na fila", ("hello", "")),
])
def test_na_fila_antes_do_nome_nao_vai_para_a_busca(frase, esperado):
    assert _c(frase) == esperado


# ── tirar da fila ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("frase,esperado", [
    ("tira a terceira", ("remove", "#3")),
    ("tira a terceira da fila", ("remove", "#3")),
    ("remove a número 2", ("remove", "#2")),
    ("apaga a 4 da fila", ("remove", "#4")),
    ("deleta a última", ("remove", "#-1")),
    ("tira hotel da fila", ("remove", "hotel")),
    ("tira a próxima", ("remove", "#1")),
    ("tira as próximas duas", ("remove", "#1-2")),
    ("pula as próximas 3", ("remove", "#1-3")),
    ("remove as 2 próximas", ("remove", "#1-2")),
    ("tira o loop", ("loop_off", "")),
    ("tira a rádio", ("autoplay", "")),
    ("limpa a fila", ("clear_queue", "")),
])
def test_tirar_da_fila(frase, esperado):
    assert _c(frase) == esperado


# ── perguntar ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("frase,esperado", [
    ("o que tem na fila", ("queue", "")),
    ("mostra a fila", ("queue", "")),
    ("qual é a fila", ("queue", "")),
    ("quais são as próximas", ("queue", "")),
    ("quantas músicas tem na fila", ("queue", "")),
    ("fila", ("queue", "")),
    ("qual a próxima", ("next_up", "")),
    ("qual é a próxima música", ("next_up", "")),
    ("o que vem depois", ("next_up", "")),
    ("que música é essa", ("now_playing", "")),
    ("embaralha a fila", ("shuffle", "")),
])
def test_perguntas_sobre_a_fila(frase, esperado):
    assert _c(frase) == esperado


# ── a anterior ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("frase,esperado", [
    ("volta uma música", ("previous", "")),
    ("música anterior", ("previous", "")),
    ("anterior", ("previous", "")),
    ("toca a anterior", ("previous", "")),
    ("volta pra música anterior", ("previous", "")),
    ("volta duas músicas", ("previous", "2")),
    ("volta", ("resume", "")),
    ("volta a música", ("resume", "")),
    ("volta 10 segundos", ("seek", "-10")),
    ("volta pro começo", ("seek", "0")),
])
def test_musica_anterior(frase, esperado):
    assert _c(frase) == esperado


# ── o que o teste de 20/09 mandou para o LLM sem precisar ───────────────────


@pytest.mark.parametrize("frase,esperado", [
    ("volume máximo", ("volume", "100")),
    ("volume no máximo", ("volume", "100")),
    ("volume normal", ("volume", "100")),
    ("volume na metade", ("volume", "50")),
    ("volume mínimo", ("volume", "10")),
    ("minuto 5", ("seek", "300")),
    ("minuto 1 e 30", ("seek", "90")),
])
def test_atalhos_que_iam_para_o_llm(frase, esperado):
    assert _c(frase) == esperado


# ── casar o nome dito com a fila ────────────────────────────────────────────

FILA = [
    Track(title="Pyramids", artist="Frank Ocean"),
    Track(title="Hotel", artist="Montell Fish"),
    Track(title="New Person, Same Old Mistakes", artist="Tame Impala"),
    Track(title="Hotel California (2013 Remaster)", artist="Eagles"),
    Track(title="Love Story", artist="Taylor Swift"),
    Track(title="Nights", artist="Frank Ocean"),
]


@pytest.mark.parametrize("dito,posicao", [
    ("pyramids", 1),
    ("piramides", 1),  # o Whisper aportuguesa
    ("hotel", 2),  # e não o Hotel California
    ("hotel california", 4),
    ("tame impala", 3),
    ("new person same old mistakes", 3),
    ("frank ocean", 1),  # pelo artista: a primeira dele
    ("nights do frank ocean", 6),
    ("a do eagles", 4),
])
def test_acha_na_fila(dito, posicao):
    achado = acha_na_fila(FILA, dito)
    assert achado is not None and achado[0] == posicao


@pytest.mark.parametrize("dito", ["despacito", "bohemian rhapsody", "", "a da"])
def test_o_que_nao_esta_na_fila_nao_e_achado(dito):
    assert acha_na_fila(FILA, dito) is None


def test_toca_x_sem_dizer_da_fila_so_casa_com_certeza():
    """ "toca love" não pode virar "Love Story" só porque a palavra aparece: sem
    "da fila" na frase, o normal é buscar no YouTube."""
    assert acha_na_fila(FILA, "love", exigente=True) is None
    assert acha_na_fila(FILA, "hotel", exigente=True)[0] == 2
    assert acha_na_fila(FILA, "frank ocean", exigente=True)[0] == 1
    assert acha_na_fila(FILA, "hotel california", exigente=True)[0] == 4
    assert acha_na_fila(FILA, "california dreamin", exigente=True) is None


# ── o teste de 21/09: o que o Otavio falou e a gramática leu errado ─────────
# Fila de 38 (playlist do Spotify). "coloca a realidade ou fantasia como próxima
# DA FILA" virou play_queue com o nome 'realidade ou fantasia como proxima': a
# marca de "próxima" só era procurada no FIM da frase, e o fim era "da fila". O
# bot CORTOU a música que tocava (1:39 de 2:39) em vez de só pôr a outra na frente.


@pytest.mark.parametrize("frase,esperado", [
    ("coloca a realidade ou fantasia como próxima da fila", ("play_next", "realidade ou fantasia")),
    ("coloca a quinta como próxima da fila", ("play_next", "#5")),
    ("bota evidências como a próxima música", ("play_next", "evidencias")),
    ("coloca evidências como próxima música da fila", ("play_next", "evidencias")),
    ("bota a terceira na frente", ("play_next", "#3")),
    ("bota hotel california na frente da fila", ("play_next", "hotel california")),
    ("coloca a última no começo da fila", ("play_next", "#-1")),
    ("bota pyramids pra tocar depois dessa", ("play_next", "pyramids")),
    # e o que NÃO pode mudar: posição dentro da música é seek
    ("volta pro começo", ("seek", "0")),
    ("coloca no minuto 2", ("seek", "120")),
])
def test_como_proxima_com_da_fila_no_fim(frase, esperado):
    assert _c(frase) == esperado


# "quero escutar a ferida curada do Zé Neto e Cristiano" foi para o LLM (0,9s a
# mais, e ele devolveu o nome reescrito: 'A Ferida, Curada - Cristiano Zeneta').
# "quero ouvir X" é "toca X" — inclusive para a fila ("quero ouvir a terceira").


@pytest.mark.parametrize("frase,esperado", [
    ("quero escutar a ferida curada do zé neto e cristiano", ("play", "ferida curada do ze neto e cristiano")),
    ("quero ouvir hotel california", ("play", "hotel california")),
    ("queria ouvir a terceira", ("play_queue", "#3")),
    ("eu quero ouvir a última da fila", ("play_queue", "#-1")),
    ("quero que toque evidências", ("play", "evidencias")),
    ("gostaria de ouvir pyramids como próxima", ("play_next", "pyramids")),
    ("quero ouvir as minhas favoritas", ("play_favs", "")),
    ("quero ouvir", ("unknown", "quero ouvir")),  # sem dizer o quê: o LLM que se vire
])
def test_quero_ouvir_e_tocar(frase, esperado):
    assert _c(frase) == esperado


def test_nome_de_dupla_que_o_whisper_juntou():
    """ "Zé Neto" chegou como "Zeneta": nenhuma palavra do título casava com ela
    sozinha — comparando também com as vizinhas coladas ("zeneto"), casa."""
    fila = FILA + [Track(title="Ferida Curada", artist="Zé Neto & Cristiano")]
    ultima = len(fila)

    assert acha_na_fila(fila, "a ferida e curada do zeneta cristiano", exigente=True)[0] == ultima
    assert acha_na_fila(fila, "A Ferida, Curada - Cristiano Zeneta", exigente=True)[0] == ultima  # como o LLM devolveu
    assert acha_na_fila(fila, "zeneto e cristiano")[0] == ultima
    assert acha_na_fila(fila, "love", exigente=True) is None  # colar vizinhas não afrouxa o resto


# ── "toca X agora" (21/09): com o pedido por voz entrando como PRÓXIMA, o ─────
# "agora" é como se pede para cortar. Antes o "agora" ia junto para a busca do
# YouTube, e "toca pyramids da fila agora" nem era reconhecido como da fila.


@pytest.mark.parametrize("frase,esperado", [
    # o alvo vai INTEIRO, com o "agora" onde foi dito: quem executa confere se o
    # "agora" não é o próprio nome da música ("Agora Só Falta Você") antes de tirar
    ("toca evidências agora", ("play_now", "evidencias agora")),
    ("bota evidências do chitãozinho agora mesmo", ("play_now", "evidencias do chitaozinho agora mesmo")),
    ("quero ouvir hotel california já", ("play_now", "hotel california ja")),
    ("toca agora in the pool", ("play_now", "agora in the pool")),  # teste de 21/09, 03:33
    ("toca já evidências", ("play_now", "ja evidencias")),
    ("toca pyramids da fila agora", ("play_queue", "pyramids")),
    ("toca a terceira agora", ("play_queue", "#3")),
    ("toca evidências", ("play", "evidencias")),
    # "agora" que é o próprio nome, ou sobra curta demais para ser um pedido
    ("toca agora", ("play", "agora")),
    ("toca e agora", ("play", "e agora")),
    ("toca até agora", ("play", "ate agora")),
])
def test_toca_x_agora(frase, esperado):
    assert _c(frase) == esperado


def test_sem_o_agora():
    from core.intents import sem_o_agora

    assert sem_o_agora("agora in the pool") == "in the pool"
    assert sem_o_agora("ja evidencias") == "evidencias"
    assert sem_o_agora("evidencias agora mesmo") == "evidencias"
    assert sem_o_agora("hotel california ja") == "hotel california"
    assert sem_o_agora("evidencias") == "evidencias"


# ── teste do Otavio de 21/09, 03:30–03:35 UTC ───────────────────────────────
# "minuto trinta, minuto, não, segundo trinta, essa música" virou seek para o
# minuto 30 de uma faixa de 4:19: o bot foi para 4:14 e a música ACABOU 5s depois.
# E "segundo 30 dessa música", sozinho, foi para o LLM (1,0s a mais).


@pytest.mark.parametrize("frase,esperado", [
    ("segundo 30 dessa música", ("seek", "30")),
    ("segundo trinta", ("seek", "30")),
    ("vai pro segundo 45", ("seek", "45")),
    ("minuto trinta, minuto, não, segundo trinta, essa música", ("seek", "30")),
    ("avança 30 segundos, não, 10 segundos", ("seek", "+10")),  # a cauda herda o verbo
    ("volume 80, não, volume 50", ("volume", "50")),
    # e o que NÃO pode mudar
    ("toca não quero dinheiro", ("play", "nao quero dinheiro")),
    ("toca a segunda", ("play_queue", "#2")),
    ("pula pra segunda", ("jump", "#2")),
    ("avança 30 segundos", ("seek", "+30")),
])
def test_segundo_n_e_quem_se_corrige_no_meio_da_frase(frase, esperado):
    assert _c(frase) == esperado


# O Whisper às vezes começa a transcrição ECOANDO o fim do prompt ("Músicas: a,
# b."). No teste saiu algo como 'Músicas Preta. Herodes, toca mágica.' e só
# funcionou porque a wake word caiu na 3ª palavra; com um eco mais longo o
# comando sumia.


def test_eco_do_prompt_do_whisper_na_frente_do_comando():
    from core.intents import chamou_de_frente

    assert parse_command("Músicas Preta. Herodes, toca mágica.") == ("play", "magica")
    longo = "Músicas: Seja Ex, Henrique & Juliano, Mágica, Calcinha Preta. Arrodes, pula."
    assert parse_command(longo) == ("skip", "")
    assert chamou_de_frente(longo) is True
    assert parse_command("Músicas sertanejas são as melhores, né?") is None
    assert parse_command("eu acho que músicas assim o arrodes não acha") is None  # conversa segue conversa


# "So Am I" já estava na fila e o Otavio repetiu o pedido: o Whisper escreveu
# "Soul A M I, de Ava Max" e "sou M.I. de Ava Max". Palavra por palavra não casa
# ("soul"/"sou" ≠ "so", "m" ≠ "am"); colando tudo, é quase a mesma coisa.


def test_titulo_em_ingles_soletrado_pelo_whisper_casa_com_a_fila():
    fila = FILA + [Track(title="So Am I", artist="Ava Max")]
    ultima = len(fila)

    assert acha_na_fila(fila, "soul a m i de ava max", exigente=True)[0] == ultima
    assert acha_na_fila(fila, "sou m i de ava max", exigente=True)[0] == ultima
    assert acha_na_fila(fila, "so am i", exigente=True)[0] == ultima
    assert acha_na_fila(fila, "sou emae", exigente=True) is None  # irreconhecível: segue para a busca
    assert acha_na_fila(fila, "ava", exigente=True) is None  # curto demais para o "colado" valer
