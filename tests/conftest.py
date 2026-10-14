import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest


@pytest.fixture(autouse=True)
def _llm_sem_aposentados():
    """O 404 de um teste marca o modelo como aposentado no módulo; não vaza
    para o seguinte."""
    from services import llm

    llm._aposentados.clear()
    yield
    llm._aposentados.clear()


@pytest.fixture(autouse=True)
def _segunda_escuta_sem_rede(monkeypatch):
    """A 2ª escuta da chamada (services/speech.confere_chamada) iria à Groq de
    verdade com a chave do .env: nos testes ela "não consegue conferir" e vale
    a 1ª. Quem testa a conferência substitui por cima."""
    from services import speech

    async def _nao_confere(pcm):
        return None

    monkeypatch.setattr(speech, "confere_chamada", _nao_confere)


@pytest.fixture(autouse=True)
def _minuto_da_groq_limpo():
    """Envios e 429 do minuto, por modelo, não vazam de um teste para outro."""
    from services import speech

    speech._envios_por_modelo.clear()
    speech._minuto_cheio_ate.clear()
    yield
    speech._envios_por_modelo.clear()
    speech._minuto_cheio_ate.clear()
