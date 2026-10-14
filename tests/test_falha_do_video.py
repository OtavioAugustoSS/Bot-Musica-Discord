"""Vídeo que falhou por ser DELE (idade, privado, removido) fica lembrado por horas.

Journal de 27/09: uma faixa com restrição de idade falhou às 00:30:30 (adiantamento),
de novo às 00:31:27 (do cache negativo de 60s) e às 00:32:04 pagou a extração
inteira outra vez — dois perfis, ~6s no worker único — para falhar igual. O
cache negativo de 60s serve para erro passageiro; restrição de idade não passa.
"""
from services import youtube


def _falha(monkeypatch, texto: str, idade_s: float) -> BaseException | None:
    youtube._failures.clear()
    relogio = [1000.0]
    monkeypatch.setattr(youtube, "_now", lambda: relogio[0])
    youtube._lembra_falha("vid", RuntimeError(texto))
    relogio[0] += idade_s
    return youtube._recent_failure("vid")


def test_restricao_de_idade_fica_lembrada(monkeypatch):
    erro = "ERROR: [youtube] ukDQtrjAgSk: Sorry, this content is age-restricted"
    assert _falha(monkeypatch, erro, 30 * 60) is not None


def test_video_privado_fica_lembrado(monkeypatch):
    assert _falha(monkeypatch, "ERROR: [youtube] x: Private video. Sign in", 30 * 60) is not None


def test_indisponivel_segue_curto(monkeypatch):
    """ "Video unavailable" às vezes depende do client: não trava por horas."""
    assert _falha(monkeypatch, "ERROR: Video unavailable", 90) is None


def test_erro_passageiro_continua_curto(monkeypatch):
    assert _falha(monkeypatch, "HTTP Error 429: Too Many Requests", 30) is not None
    assert _falha(monkeypatch, "HTTP Error 429: Too Many Requests", 90) is None


def test_lembrado_nao_e_para_sempre(monkeypatch):
    assert _falha(monkeypatch, "Sorry, this content is age-restricted", 7 * 3600) is None


def test_mensagem_de_idade_em_portugues():
    assert "idade" in youtube.friendly_error(RuntimeError("Sorry, this content is age-restricted"))
