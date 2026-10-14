"""Modelo do LLM e log de erro que não vaza a chave.

Dois problemas achados no journal de produção (12/09):

1. `llama-3.3-70b-versatile` foi aposentado pela Groq: todo `comando=unknown`
   virava 404 e o interpretador de linguagem natural estava morto havia
   semanas, em silêncio. Medido com o prompt real na VM, `groq/compound-mini`
   acerta 5/5 em ~0,7s; `gpt-oss-120b` 1/5; `qwen3.6` e `gpt-oss-20b` 0/5.
2. O log usava `%r` na exceção do aiohttp, que inclui os headers da request —
   ou seja, gravava `Authorization: Bearer gsk_...` em texto puro no journal.
"""
import logging

import pytest

from services import llm


def test_modelo_padrao_existe_hoje_na_groq():
    """Modelos da Groq são aposentados; o default tem que ser um vivo.

    26/09/2026: `groq/compound-mini` também foi aposentado (404 desde 25/09).
    Remedido na VM com o prompt real, 8 frases: `gpt-oss-20b` 8/8 em 0,32s,
    `qwen3.8-27b` 8/8 em 0,33s, `gpt-oss-120b` 7/8 — o 1/5 de antes era o
    raciocínio comendo os 120 tokens de saída, não o modelo errando.
    """
    nomes = [nome for nome, _ in llm._CANDIDATOS]
    assert nomes[0] == "openai/gpt-oss-20b"
    assert len(nomes) >= 2, "sem reserva, a próxima aposentadoria mata a voz de novo"
    assert not any("llama" in n or "compound" in n for n in nomes)  # aposentados


def test_modelo_que_raciocina_tem_saida_para_a_resposta():
    """gpt-oss raciocina antes de responder: com 120 tokens a resposta vinha vazia."""
    for nome, extra in llm._CANDIDATOS:
        if nome.startswith("openai/gpt-oss"):
            assert extra.get("max_tokens", 0) >= 300
            assert extra.get("reasoning_effort") == "low"


class _Resposta:
    def __init__(self, status: int, conteudo: str = "") -> None:
        self.status, self._conteudo = status, conteudo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise _FalhaComHeaders()

    async def json(self):
        return {"choices": [{"message": {"content": self._conteudo}}]}


def test_modelo_aposentado_cai_para_o_reserva_na_mesma_frase(caplog, monkeypatch):
    """404 no primeiro: a MESMA frase vai para o próximo, e o morto não é
    tentado de novo (cada tentativa perdida é meio segundo a mais no comando)."""
    monkeypatch.setattr(llm.config, "GROQ_API_KEY", "chave")
    monkeypatch.setattr(llm, "_aposentados", set())
    pedidos: list[str] = []

    class Sessao:
        def post(self, url, json=None, **k):
            pedidos.append(json["model"])
            if json["model"] == llm._CANDIDATOS[0][0]:
                return _Resposta(404)
            return _Resposta(200, '{"action": "remove", "query": "#2"}')

    monkeypatch.setattr(llm.http, "get_session", lambda: Sessao())
    import asyncio

    with caplog.at_level(logging.WARNING, logger="services.llm"):
        assert asyncio.run(llm.interpret("tira a segunda")) == ("remove", "#2")
        assert asyncio.run(llm.interpret("tira a segunda")) == ("remove", "#2")

    primeiro, segundo = llm._CANDIDATOS[0][0], llm._CANDIDATOS[1][0]
    assert pedidos == [primeiro, segundo, segundo]
    assert caplog.text.count("404") == 1


def test_modelo_pode_vir_do_env(monkeypatch):
    """Quando a Groq aposentar este também, dá para trocar sem deploy."""
    import config

    assert hasattr(config, "GROQ_LLM_MODEL")


class _FalhaComHeaders(Exception):
    """Imita a ClientResponseError do aiohttp, que carrega os headers no repr."""

    status = 404  # o atributo real que o código usa para reconhecer o 404

    def __repr__(self) -> str:
        return (
            "ClientResponseError(RequestInfo(headers=<CIMultiDictProxy("
            "'Authorization': 'Bearer gsk_CHAVE_SECRETA_AQUI')>), status=404)"
        )

    def __str__(self) -> str:
        return "404, message='Not Found'"


def test_erro_do_llm_nao_vaza_a_chave(caplog, monkeypatch):
    monkeypatch.setattr(llm.config, "GROQ_API_KEY", "gsk_CHAVE_SECRETA_AQUI")

    class SessaoQuebrada:
        def post(self, *a, **k):
            raise _FalhaComHeaders()

    monkeypatch.setattr(llm.http, "get_session", lambda: SessaoQuebrada())

    import asyncio

    with caplog.at_level(logging.WARNING, logger="services.llm"):
        assert asyncio.run(llm.interpret("toca alguma coisa")) is None

    assert "gsk_" not in caplog.text, "a chave da Groq vazou para o log"
    assert caplog.text.strip(), "mas o erro precisa continuar aparecendo"


def test_404_diz_o_que_fazer(caplog, monkeypatch):
    """404 = modelo aposentado. Sem essa dica ficou semanas quebrado calado."""
    monkeypatch.setattr(llm.config, "GROQ_API_KEY", "chave")

    class SessaoQuebrada:
        def post(self, *a, **k):
            raise _FalhaComHeaders()

    monkeypatch.setattr(llm.http, "get_session", lambda: SessaoQuebrada())

    import asyncio

    with caplog.at_level(logging.WARNING, logger="services.llm"):
        asyncio.run(llm.interpret("toca alguma coisa"))

    texto = caplog.text.lower()
    assert "404" in texto
    assert "modelo" in texto  # aponta a causa, não só o status


@pytest.mark.parametrize("resposta,esperado", [
    ('{"action": "play", "query": "forró animado"}', ("play", "forró animado")),
    ('{"action": "skip", "query": ""}', ("skip", "")),
    ('{"action": "none", "query": ""}', ("none", "")),
    ('{"action": "play", "query": ""}', None),  # play sem busca é inútil
    ('{"action": "explodir"}', None),
    ("sem json nenhum", None),
])
def test_parse_da_resposta_continua_valendo(resposta, esperado):
    assert llm.parse_llm_reply(resposta) == esperado
