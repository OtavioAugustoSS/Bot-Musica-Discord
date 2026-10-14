"""Validação da resposta do LLM (parse defensivo)."""
from services.llm import parse_llm_reply


def test_parses_clean_json():
    assert parse_llm_reply('{"action": "skip", "query": ""}') == ("skip", "")
    assert parse_llm_reply('{"action": "play", "query": "pink floyd"}') == (
        "play",
        "pink floyd",
    )


def test_parses_json_with_fences_and_chatter():
    reply = 'Claro! ```json\n{"action": "pause", "query": ""}\n```'
    assert parse_llm_reply(reply) == ("pause", "")


def test_rejects_invalid_action_and_garbage():
    assert parse_llm_reply('{"action": "dance", "query": ""}') is None
    assert parse_llm_reply("não sei") is None
    assert parse_llm_reply('{"action": "play"}') is None  # play sem query


def test_none_action_passes_through():
    assert parse_llm_reply('{"action": "none", "query": ""}') == ("none", "")


def test_new_actions_are_valid():
    for action in ("loop", "loop_off", "clear_queue", "now_playing", "favorite", "play_favs"):
        assert parse_llm_reply(f'{{"action": "{action}", "query": ""}}') == (action, "")
