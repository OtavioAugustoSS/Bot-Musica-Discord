"""O leitor do byte TOC do Opus — é ele que responde "o YouTube manda pacotes de 20ms?".

O py-cord envia um pacote por tique de 20ms e avança o timestamp RTP em 960
amostras SEM olhar o pacote: repassar Opus direto só é seguro se cada pacote
tiver exatamente 20ms. (RFC 6716, seção 3.1.)
"""
import pytest

import bench_audio as bench


def _toc(config: int, codigo: int = 0, estereo: bool = True) -> int:
    return (config << 3) | (int(estereo) << 2) | codigo


@pytest.mark.parametrize("config", sorted(bench._CONFIGS_20MS))
def test_todas_as_configs_de_20ms(config):
    assert bench.duracao_do_pacote_ms(bytes([_toc(config)]) + b"xx") == 20


@pytest.mark.parametrize("config,esperado", [(0, 10), (2, 40), (3, 60), (16, 2.5), (30, 10), (12, 10)])
def test_outros_tamanhos_de_quadro(config, esperado):
    assert bench.duracao_do_pacote_ms(bytes([_toc(config)]) + b"xx") == esperado


def test_dois_quadros_de_20ms_sao_40ms():
    """Código 1/2 = dois quadros no pacote: tocaria em câmera lenta no py-cord."""
    assert bench.duracao_do_pacote_ms(bytes([_toc(31, codigo=1)]) + b"xx") == 40
    assert bench.duracao_do_pacote_ms(bytes([_toc(31, codigo=2)]) + b"xx") == 40


def test_codigo_3_le_a_contagem_no_segundo_byte():
    assert bench.duracao_do_pacote_ms(bytes([_toc(31, codigo=3), 3]) + b"xx") == 60


def test_pacote_vazio_nao_levanta():
    assert bench.duracao_do_pacote_ms(b"") == 0.0
    assert bench.duracao_do_pacote_ms(bytes([_toc(31, codigo=3)])) == 0.0


def test_silencio_opus_do_discord_tem_20ms():
    """b"\\xf8\\xff\\xfe" é o quadro de silêncio que o próprio py-cord envia."""
    assert bench.duracao_do_pacote_ms(b"\xf8\xff\xfe") == 20
