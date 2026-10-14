"""Interpretação dos comandos de voz: wake word fuzzy + intenções."""
import pytest

from core.intents import _WAKE_BLOCKLIST, inicio_para_log, parse_command


def test_ignores_speech_without_wake_word():
    assert parse_command("bora jogar mais uma?") is None
    assert parse_command("toca aquela música aí") is None
    assert parse_command("") is None


def test_wake_word_exact_and_fuzzy():
    assert parse_command("Arrodes, pula!") == ("skip", "")
    assert parse_command("arodes pula") == ("skip", "")
    assert parse_command("arro des pula") == ("skip", "")
    assert parse_command("ô arrodes, pula") == ("skip", "")


def test_play_extracts_query():
    assert parse_command("Arrodes, toca Master of Puppets") == (
        "play",
        "master of puppets",
    )
    assert parse_command("arrodes bota a musica do pink floyd") == (
        "play",
        "do pink floyd",
    )
    assert parse_command("arrodes coloca AC/DC") == ("play", "ac dc")


def test_control_intents():
    assert parse_command("arrodes pausa") == ("pause", "")
    assert parse_command("arrodes volta") == ("resume", "")
    assert parse_command("arrodes continua") == ("resume", "")
    assert parse_command("arrodes próxima") == ("skip", "")
    assert parse_command("arrodes embaralha") == ("shuffle", "")
    assert parse_command("arrodes autoplay") == ("autoplay", "")


def test_leave_intents():
    assert parse_command("arrodes sai da call") == ("leave", "")
    assert parse_command("arrodes tchau") == ("leave", "")
    assert parse_command("arrodes para tudo") == ("leave", "")


def test_bare_para_pauses_instead_of_leaving():
    # "para" sozinho = pausa (mais seguro que derrubar o bot da call)
    assert parse_command("arrodes para") == ("pause", "")
    assert parse_command("arrodes pare") == ("pause", "")


@pytest.mark.parametrize("frase, esperado", [
    # "Arrodes" não é palavra do português: o Whisper escreve o nome conhecido
    # mais perto. "A Rodes": o "a" sai como muleta e "rodes" (0,83) passa
    ("A Rodes, pula", ("skip", "")),
    ("Herodes, toca Legião Urbana", ("play", "legiao urbana")),
    ("Harrods, próxima", ("skip", "")),
    ("Arrodis pausa", ("pause", "")),
])
def test_grafias_que_o_whisper_da_ao_nome(frase, esperado):
    assert parse_command(frase) == esperado


@pytest.mark.parametrize("frase", [
    "arroz com feijão",
    "a rede caiu",
    "acordes de violão",
    "a roda gigante",
    "arrotes",
    "vamos jogar rodada",
    "arrodeia ali",
    "a rode, pula",  # "rode" (0,73) é verbo: o nome dito pela metade não chama
])
def test_palavra_perto_do_nome_nao_chama_o_bot(frase):
    assert parse_command(frase) is None


def test_comando_com_lixo_da_transcricao():
    action, query = parse_command("Herodes, vou tocar. Última noite, Leo Foguete.")
    assert action == "play"
    assert "ultima noite" in query
    assert parse_command("Arrodes, ahn, pausa.") == ("pause", "")
    assert parse_command("Harrods, despausa.") == ("resume", "")
    assert parse_command("A Rodes, sai da cal.") == ("leave", "")


def test_fuzzy_verbs():
    assert parse_command("arrodes pular essa") == ("skip", "")
    assert parse_command("arrodes pausar") == ("pause", "")
    assert parse_command("arrodes tocar bohemian rhapsody") == (
        "play",
        "bohemian rhapsody",
    )


def test_wake_word_alone_greets():
    assert parse_command("Arrodes?") == ("hello", "")


def test_unknown_command_is_reported():
    action, arg = parse_command("arrodes faz um sanduíche")
    assert action == "unknown"
    assert "sanduiche" in arg


def test_wake_word_variants():
    for wake in ("arodes", "arrodis", "arodis", "harrodes", "harodes", "herodes", "harrods",
                 "rhodes", "aroldes", "haroldes", "erodes", "arrods", "arrode", "Hero des"):
        assert parse_command(f"{wake} pula") == ("skip", ""), wake


def test_wake_word_blocklist_ignores_real_words():
    # palavras pt de verdade perto do nome (arrotes é 0,86) NÃO acionam o bot
    for palavra in sorted(_WAKE_BLOCKLIST):
        assert parse_command(f"{palavra} pula") is None, palavra
    assert parse_command("o arroz ta pronto") is None
    assert parse_command("que arrotes horriveis") is None


def test_wake_word_found_in_first_words():
    # transcrição costuma pôr lixo antes da wake word
    assert parse_command("é o arrodes pula") == ("skip", "")
    assert parse_command("meu deus arrodes pula") == ("skip", "")
    assert parse_command("opa arrodes pausa") == ("pause", "")
    assert parse_command("peraí a rodes pula") == ("skip", "")


def test_expanded_pause_vocabulary():
    for phrase in ("pausa", "pausar", "pause", "para", "pare", "espera",
                   "segura ai", "calma", "para a musica"):
        assert parse_command(f"arrodes {phrase}") == ("pause", ""), phrase


def test_expanded_resume_vocabulary():
    for phrase in ("volta", "continua", "despausa", "retoma", "solta",
                   "segue", "continua a tocar"):
        assert parse_command(f"arrodes {phrase}") == ("resume", ""), phrase


def test_expanded_skip_vocabulary():
    for phrase in ("pula", "pular essa", "proxima", "próxima música", "passa",
                   "next", "skipa", "troca", "muda ai", "outra", "avança",
                   "vai pra proxima"):
        assert parse_command(f"arrodes {phrase}") == ("skip", ""), phrase


def test_expanded_leave_vocabulary():
    for phrase in ("sai da call", "sai da chamada", "sai fora", "desconecta",
                   "desliga", "tchau", "vaza", "fui", "encerra", "vai embora"):
        assert parse_command(f"arrodes {phrase}") == ("leave", ""), phrase


def test_loop_intents():
    assert parse_command("arrodes repete essa") == ("loop", "")
    assert parse_command("arrodes repete") == ("loop", "")
    assert parse_command("arrodes bota no loop") == ("loop", "")
    assert parse_command("arrodes tira o loop") == ("loop_off", "")
    assert parse_command("arrodes para de repetir") == ("loop_off", "")
    assert parse_command("arrodes desliga o loop") == ("loop_off", "")


def test_clear_queue_intents():
    assert parse_command("arrodes limpa a fila") == ("clear_queue", "")
    assert parse_command("arrodes apaga tudo") == ("clear_queue", "")
    assert parse_command("arrodes esvazia a fila") == ("clear_queue", "")


def test_now_playing_intents():
    assert parse_command("arrodes que musica é essa") == ("now_playing", "")
    assert parse_command("arrodes qual musica ta tocando") == ("now_playing", "")
    assert parse_command("arrodes o que ta tocando") == ("now_playing", "")


def test_precedence_para_family():
    # "para" sozinho pausa; "para tudo" sai; "para de repetir" tira o loop;
    # "apaga tudo" limpa a fila — nenhum pode engolir o outro
    assert parse_command("arrodes para") == ("pause", "")
    assert parse_command("arrodes para tudo") == ("leave", "")
    assert parse_command("arrodes para de repetir") == ("loop_off", "")
    assert parse_command("arrodes apaga tudo") == ("clear_queue", "")


def test_desliga_family():
    # "desliga" sozinho = sair; com autoplay/loop, vale o objeto
    assert parse_command("arrodes desliga") == ("leave", "")
    assert parse_command("arrodes desliga o autoplay") == ("autoplay", "")
    assert parse_command("arrodes desliga o loop") == ("loop_off", "")


def test_play_word_alone_resumes_with_query_plays():
    assert parse_command("arrodes play") == ("resume", "")
    assert parse_command("arrodes play master of puppets") == (
        "play",
        "master of puppets",
    )


def test_swap_with_target_becomes_play():
    assert parse_command("arrodes troca pra pink floyd") == ("play", "pink floyd")
    assert parse_command("arrodes muda para forró") == ("play", "forro")
    assert parse_command("arrodes troca essa musica") == ("skip", "")


def test_favorite_intents():
    assert parse_command("arrodes favorita essa") == ("favorite", "")
    assert parse_command("arrodes favorita") == ("favorite", "")
    assert parse_command("arrodes salva essa musica") == ("favorite", "")
    assert parse_command("arrodes adiciona nos favoritos") == ("favorite", "")
    assert parse_command("arrodes bota nos favoritos") == ("favorite", "")


def test_play_favorites_intents():
    assert parse_command("arrodes toca minhas favoritas") == ("play_favs", "")
    assert parse_command("arrodes toca os favoritos") == ("play_favs", "")
    assert parse_command("arrodes bota as favoritas") == ("play_favs", "")


def test_palavra_de_duas_letras_nao_e_verbo_por_aproximacao():
    """26/09: "pe" é 0,8 de "poe" e "Pé, Pé, Pé, Quase…" virou play."""
    assert parse_command("Arrodes, Pé, Pé, Pé, Quase.") != ("play", "pe pe quase")
    assert parse_command("arrodes Tocca, ah, no chacau.") == ("play", "ah no chacau")
    assert parse_command("Arrodes, põe Djavan")[0] == "play"


def test_verbo_arrodear_nao_e_o_bot():
    assert parse_command("Arrodeia ali, tá ligado") is None
    assert parse_command("Ele arrodeou a quadra inteira") is None


def test_arroz_e_palavra_nao_e_o_bot():
    """Palavra perto do nome no meio da conversa não pula a música."""
    assert parse_command("É, ô gente, um arroz passa num trem desse não.") is None
    assert parse_command("Arrozes, macho, arrozes, coitado dos arrozes.") is None
    assert parse_command("Arrodes, arroz, arrozes, arrotes.") is not None  # chamou de verdade


@pytest.mark.parametrize("frase, esperado", [
    ("Arrodes, toca Rhodes", ("play", "rhodes")),
    ("Arrodes, toca Harrods", ("play", "harrods")),
    ("Arrodes, toca Rodes", ("play", "rodes")),
    ("Arrodes, toca música do Herodes", ("play", "musica do herodes")),
    ("Arrodes, toca Djavan, Arrodes", ("play", "djavan")),  # o nome repetido no fim sai
])
def test_nome_parecido_no_fim_e_parte_do_pedido(frase, esperado):
    """Variação do nome no fim é o que a pessoa pediu, não o nome repetido."""
    assert parse_command(frase) == esperado


def test_inicio_para_log_mostra_so_o_comeco_e_a_grafia_do_nome():
    """O que vai para o journal quando a frase não chamou o bot: a 1ª palavra
    (sem as muletas) e a grafia que mais lembrou o nome — nunca o resto da frase."""
    assert inicio_para_log("A Rode, bota aquela do Djavan") == "rode→'rode' 0.73"
    assert inicio_para_log("Ô, a Rodes") == "rodes→'rodes' 0.83"
    assert inicio_para_log("Hero des, pula") == "hero→'herodes' 0.71"
    assert inicio_para_log("Eu fui pra Rodes") == "eu→'prarodes' 0.80"
    # nada parecido com o nome: só a nota, as outras palavras ficam de fora
    assert inicio_para_log("Cara, eu fui no mercado hoje.") == "cara 0.46"
    assert inicio_para_log("Tchau pessoal") == "tchau 0.32"
    assert inicio_para_log("Ok.") == "-"
    assert inicio_para_log("") == "-"


def test_verbo_de_pedido():
    from core.intents import tem_pedido_de_tocar

    assert tem_pedido_de_tocar("Arrodes, toca Djavan")
    assert tem_pedido_de_tocar("Arrodes, quero ouvir algo mais animado")
    assert tem_pedido_de_tocar("Arrodes, põe aquela")
    assert not tem_pedido_de_tocar("Arrodes, Brincadeira, agora sim.")
    assert not tem_pedido_de_tocar("Arrodes, que música boa")
