"""Opções do FFmpeg que existem por causa de uma medição — não podem sumir num refactor.

20/09/2026, produção: 4 de 6 faixas pedidas em quatro minutos não tocaram. O
googlevideo recusa com 403 o PRIMEIRO acesso a uma URL recém-extraída e aceita
o mesmo acesso um segundo depois; faixa do prefetch (aberta 30s+ depois da
extração) tocava, faixa pedida na hora não. Medido na VM, FFmpeg 6.1, seis
aberturas de URL fresca para cada variante:

    opções de antes                        5 de 6 morreram com 403
    + -reconnect_on_http_error 403,5xx     6 de 6 tocaram (1,3–4,4s de espera)

`-reconnect 1` sozinho NÃO cobre isso: ele só vale para queda no meio do
stream, e erro HTTP na abertura é fatal sem a opção explícita.
"""
from services import youtube


def test_ffmpeg_tenta_de_novo_quando_a_abertura_leva_403():
    opcoes = youtube.FFMPEG_BEFORE.split()
    assert "-reconnect_on_http_error" in opcoes
    codigos = opcoes[opcoes.index("-reconnect_on_http_error") + 1]
    assert "403" in codigos.split(",")


def test_ffmpeg_tenta_de_novo_em_erro_de_rede_na_abertura():
    opcoes = youtube.FFMPEG_BEFORE.split()
    assert opcoes[opcoes.index("-reconnect_on_network_error") + 1] == "1"


def test_as_opcoes_de_reconexao_vem_antes_da_entrada():
    """São opções do protocolo http: depois do -i o FFmpeg as ignora (ou recusa)."""
    assert "-i" not in youtube.FFMPEG_BEFORE.split()
    assert "reconnect" not in youtube.FFMPEG_OPTIONS
