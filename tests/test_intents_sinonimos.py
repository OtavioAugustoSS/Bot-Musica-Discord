"""Comandos de voz: jeitos diferentes de pedir a MESMA coisa, e conversa que não é pedido.

Pedido do Otavio em 28/09/2026: "a grande maioria dos usos do bot é por comandos de
voz, então eles têm que ficar extremamente refinados". Os casos "reais" saíram do
journal de 21 a 28/09, com o nome trocado pelo WAKE_WORD — pedidos sérios que
falharam ou foram mal entendidos ("retirar", "adicionar", "vou adicionar a fila…",
"calma, me espera", "volume duzentos", "coloca a próxima música como X"). A grafia
do nome neles não é a que o Whisper deu. As variações são o mesmo pedido dito
de outro jeito. E a CONVERSA (frases com "Arrodes" que não são comando, inventadas
no molde das que o journal tinha) não pode virar ação — "pode ser" e "você pode ver
isso aí" viravam busca de música, porque "pode" é 0,86 de "poe" no fuzzy.
"""
import pytest

from core.intents import parse_command

CASOS = [
    # ── reais do journal (o nome trocado pelo WAKE_WORD) ──
    ("Arrodes, coloca a realidade ou fantasia como próxima da fila.", ("play_next", "realidade ou fantasia")),
    ("Arrodes, retira a próxima música da fila.", ("remove", "#1")),
    ("Arro, Arrodes, coloca como próxima música, Traidor de Zé Vaquiri e Paulo Fernandes.", ("play_next", "traidor de ze vaquiri e paulo fernandes")),
    ("Arrodes, coloca a próxima música como amigo da minha saudade, Henrique & Juliano.", ("play_next", "amigo da minha saudade henrique juliano")),
    ("Couto Iba-Tá, arrodes, coloca a briga feia com o próximo da fila.", ("play_next", "briga feia")),
    ("Arrodes, tocar como próxima música, realidade ou fantasia?", ("play_next", "realidade ou fantasia")),
    ("Arrodes, retire a música de número um da fila.", ("remove", "#1")),
    ("Arrodes, toca como próxima da fila, realidade e fantasia de Henrique & Juliano.", ("play_next", "realidade e fantasia de henrique juliano")),
    ("Arrodes, colocar como próxima da fila, é confiável Léo Foguete.", ("play_next", "e confiavel leo foguete")),
    ("Arrodes, toca, até a próxima vida.", ("play", "ate a proxima vida")),
    ("Arrodes, vou adicionar a fila, até a próxima vida.", ("play", "ate a proxima vida")),
    ("arrodes Arrodes, vai adicionar a fila, até a próxima vida.", ("play", "ate a proxima vida")),
    ("Arrodes retirar o online Lang da fila.", ("remove", "online lang")),
    ("Arrodes retirar a segunda música da fila.", ("remove", "#2")),
    ("Arrodes, retirar, online Lang.", ("remove", "online lang")),
    ("Arrodes retirar online da fila.", ("remove", "online")),
    ("Arrodes, volume duzentos.", ("volume", "200")),
    ("Arrodes, Calma, me espera.", ("pause", "")),
    ("Arrodes, liga, volta a play.", ("resume", "")),
    ("Arrodes, segundo 30 dessa música.", ("seek", "30")),
    ("Arrodes, exclua as três próximas da fila.", ("remove", "#1-3")),
    ("ARRODES? Voltou a música, ARRODES.", ("resume", "")),
    ("Arrodes, quero escutar a ferida e curada do Zeneta Cristiano.", ("play", "ferida e curada do zeneta cristiano")),
    # ── variações naturais: tocar / adicionar ──
    ("Arrodes, adiciona Evidências na fila", ("play", "evidencias")),
    ("Arrodes, adicionar Evidências", ("play", "evidencias")),
    ("Arrodes, adiciona na fila Evidências", ("play", "evidencias")),
    ("Arrodes, acrescenta Evidências na fila", ("play", "evidencias")),
    ("Arrodes, inclui Evidências na fila", ("play", "evidencias")),
    ("Arrodes, põe Evidências", ("play", "evidencias")),
    ("Arrodes, ponha Evidências", ("play", "evidencias")),
    ("Arrodes, bota pra tocar Evidências", ("play", "evidencias")),
    ("Arrodes, toca pra mim Evidências", ("play", "evidencias")),
    ("Arrodes, toca aí Evidências", ("play", "evidencias")),
    ("Arrodes, por favor, toca Evidências", ("play", "evidencias")),
    ("Arrodes, toca Evidências por favor", ("play", "evidencias")),
    ("Arrodes, pode tocar Evidências", ("play", "evidencias")),
    ("Arrodes, você pode tocar Evidências?", ("play", "evidencias")),
    ("Arrodes, reproduz Evidências", ("play", "evidencias")),
    ("Arrodes, manda Evidências", ("play", "evidencias")),
    ("Arrodes, coloca Evidências de Chitãozinho e Xororó", ("play", "evidencias de chitaozinho e xororo")),
    ("Arrodes, coloca Evidências no final da fila", ("play_end", "evidencias")),
    ("Arrodes, adiciona Evidências como próxima", ("play_next", "evidencias")),
    ("Arrodes, adiciona como próxima Evidências", ("play_next", "evidencias")),
    ("Arrodes, põe Evidências pra tocar depois dessa", ("play_next", "evidencias")),
    ("Arrodes, toca Evidências em seguida", ("play_next", "evidencias")),
    ("Arrodes, toca agora Evidências", ("play_now", "agora evidencias")),
    ("Arrodes, toca Evidências agora", ("play_now", "evidencias agora")),
    # ── tirar da fila ──
    ("Arrodes, remove Evidências da fila", ("remove", "evidencias")),
    ("Arrodes, remover Evidências da fila", ("remove", "evidencias")),
    ("Arrodes, tirar Evidências da fila", ("remove", "evidencias")),
    ("Arrodes, exclui Evidências da fila", ("remove", "evidencias")),
    ("Arrodes, excluir a terceira", ("remove", "#3")),
    ("Arrodes, deleta a última", ("remove", "#-1")),
    ("Arrodes, tira a música número 4 da fila", ("remove", "#4")),
    ("Arrodes, tira a quarta música", ("remove", "#4")),
    # ── pular ──
    ("Arrodes, pula essa", ("skip", "")),
    ("Arrodes, pula essa música", ("skip", "")),
    ("Arrodes, passa essa", ("skip", "")),
    ("Arrodes, próxima música", ("skip", "")),
    ("Arrodes, outra música", ("skip", "")),
    ("Arrodes, troca de música", ("skip", "")),
    ("Arrodes, muda de música", ("skip", "")),
    ("Arrodes, tira essa", ("skip", "")),
    ("Arrodes, tira essa música", ("skip", "")),
    ("Arrodes, pular", ("skip", "")),
    ("Arrodes, pode pular", ("skip", "")),
    ("Arrodes, avança", ("skip", "")),
    # ── pausa / volta ──
    ("Arrodes, pausa", ("pause", "")),
    ("Arrodes, pausar", ("pause", "")),
    ("Arrodes, pausa a música", ("pause", "")),
    ("Arrodes, dá um pause", ("pause", "")),
    ("Arrodes, para a música", ("pause", "")),
    ("Arrodes, espera aí", ("pause", "")),
    ("Arrodes, perai", ("pause", "")),
    ("Arrodes, calma", ("pause", "")),
    ("Arrodes, continua", ("resume", "")),
    ("Arrodes, continuar", ("resume", "")),
    ("Arrodes, volta", ("resume", "")),
    ("Arrodes, volta a tocar", ("resume", "")),
    ("Arrodes, pode voltar", ("resume", "")),
    ("Arrodes, dá play", ("resume", "")),
    ("Arrodes, play", ("resume", "")),
    ("Arrodes, despausa", ("resume", "")),
    ("Arrodes, volta a música", ("resume", "")),
    ("Arrodes, solta o som", ("resume", "")),
    # ── volume ──
    ("Arrodes, aumenta o volume", ("volume", "+20")),
    ("Arrodes, abaixa o volume", ("volume", "-20")),
    ("Arrodes, aumenta o som", ("volume", "+20")),
    ("Arrodes, abaixa um pouco", ("volume", "-20")),
    ("Arrodes, mais alto", ("volume", "+20")),
    ("Arrodes, mais baixo", ("volume", "-20")),
    ("Arrodes, volume 50", ("volume", "50")),
    ("Arrodes, volume cem", ("volume", "100")),
    ("Arrodes, volume no máximo", ("volume", "100")),
    ("Arrodes, coloca o volume em 80", ("volume", "80")),
    # ── fila / info ──
    ("Arrodes, mostra a fila", ("queue", "")),
    ("Arrodes, o que tem na fila?", ("queue", "")),
    ("Arrodes, qual a próxima?", ("next_up", "")),
    ("Arrodes, que música é essa?", ("now_playing", "")),
    ("Arrodes, qual o nome dessa música?", ("now_playing", "")),
    ("Arrodes, embaralha a fila", ("shuffle", "")),
    ("Arrodes, modo aleatório", ("shuffle", "")),
    ("Arrodes, limpa a fila", ("clear_queue", "")),
    ("Arrodes, tira tudo da fila", ("clear_queue", "")),
    ("Arrodes, apaga a fila", ("clear_queue", "")),
    ("Arrodes, repete essa música", ("loop", "")),
    ("Arrodes, coloca no repeat", ("loop", "")),
    ("Arrodes, para de repetir", ("loop_off", "")),
    ("Arrodes, música anterior", ("previous", "")),
    ("Arrodes, volta a anterior", ("previous", "")),
    ("Arrodes, volta uma música", ("previous", "")),
    ("Arrodes, sai da call", ("leave", "")),
    ("Arrodes, vaza", ("leave", "")),
    ("Arrodes, liga o autoplay", ("autoplay", "")),
    ("Arrodes, desliga a rádio", ("autoplay", "")),
    ("Arrodes, toca a terceira", ("play_queue", "#3")),
    ("Arrodes, pula pra quinta", ("jump", "#5")),
    ("Arrodes, pula 2", ("skip", "2")),
    ("Arrodes, pula duas músicas", ("skip", "2")),
    ("Arrodes, avança 30 segundos", ("seek", "+30")),
    ("Arrodes, volta 10 segundos", ("seek", "-10")),
    ("Arrodes, vai pro minuto 2", ("seek", "120")),
    ("Arrodes, salva essa nos favoritos", ("favorite", "")),
    ("Arrodes, toca as minhas favoritas", ("play_favs", "")),
]


CONVERSA = [
    "Arrodes, Pirimpimpim Parapapá, Tuntum Má, Caba Lá.", "Arrodes, tá ficando esperto, hein.",
    "Arrodes super, super rápido.", "Música Arrodes, F.M.", "Arrodes, Belarmino.",
    "Eu deixei o arrodes ligado a noite toda, mano.", "Arrodes, Genoveva.",
    "Arrodes- Eu duvido, difícil fazer uma IA que não responde besteira.",
    "Herodes, quantas vezes pausam o jogo?", "ARRODES, deixa eu ver se tem alguma boa aí.",
    "Arrodes, que vozeirão, cantor da última música.",
    "Arrodes, tá? Agora eu tenho que escolher um personagem pra jogar, né?",
    "Não, Harodes, seu lerdo.", "Arrodes, eu acho que o vídeo que eu vou postar não ficou muito bom.",
    "Arrodes, essa partida aqui tá foda, vou ter que trocar a build toda.",
    "Arrodes, eu quero sentir a vibe, Arrodes.", "Arrodes, eu vou passar no mercado.",
    "Arrodes, a gente nem falou da praia tipo aí, sabe.", "Arrodes, Brincadeira, agora sim.",
    "Arrodes, Eita.", "Arrodes tem comando pra caramba.", "Arrodes, Fulano, Beltrano.",
    "Arrodes, tranquilo.", "Arrodes, Arrodinho, Arrodão, Arrodito.",
    "Arrodes, pode ser", "Arrodes, você pode ver isso aí", "Arrodes, vou jogar agora",
    "Arrodes, espera que eu vou ali comprar um negócio e já volto",
    "Arrodes, vai tomar banho", "Arrodes, calma aí que eu tô falando com o cara",
    "Arrodes, por favor né", "Arrodes, tem como não", "Arrodes, consegue não",
    "Arrodes, vamos jogar", "Arrodes, roda de conversa", "Arrodes, da pra ver que não",
]



@pytest.mark.parametrize("frase,esperado", CASOS)
def test_pedido_entendido(frase, esperado):
    assert parse_command(frase) == esperado


@pytest.mark.parametrize("frase", CONVERSA)
def test_conversa_nao_vira_acao(frase):
    resultado = parse_command(frase)
    assert resultado is None or resultado[0] in ("unknown", "hello"), resultado
