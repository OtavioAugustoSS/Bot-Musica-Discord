"""Tamanho do pool de extração: casado com a CPU, não com um número mágico.

Medido na VM de produção (2 vCPU, 12/09/2026), extraindo do YouTube:

    simultâneas   por faixa   vazão
              1        8,3s   0,12/s
              2       21,9s   0,09/s
              4       66,3s   0,05/s

A extração é CPU-bound (executa o desafio JS do player do YouTube), então
concorrência acima do número de núcleos não só não ajuda como derruba latência
E vazão — e ainda rouba CPU do FFmpeg e da thread de voz, que é o "bot bugado,
sem tocar por um bom tempo". Com 4 workers numa máquina de 2 vCPU, quatro
comandos seguidos faziam cada extração levar 66s.
"""
import pytest

from services import youtube


@pytest.fixture(autouse=True)
def _limpa(monkeypatch):
    monkeypatch.setattr(youtube.config, "YTDLP_WORKERS", "")
    yield


def test_auto_deixa_folga_de_cpu_para_o_audio(monkeypatch):
    monkeypatch.setattr(youtube.os, "cpu_count", lambda: 8)
    assert youtube._pool_size() == 4


def test_maquina_de_2_vcpu_serializa(monkeypatch):
    """O caso da VM: 1 worker extraindo, 1 vCPU livre para FFmpeg/voz."""
    monkeypatch.setattr(youtube.os, "cpu_count", lambda: 2)
    assert youtube._pool_size() == 1


def test_nunca_passa_de_quatro(monkeypatch):
    """Máquina grande não vira 32 extrações simultâneas martelando o YouTube."""
    monkeypatch.setattr(youtube.os, "cpu_count", lambda: 64)
    assert youtube._pool_size() == 4


def test_nunca_fica_em_zero(monkeypatch):
    monkeypatch.setattr(youtube.os, "cpu_count", lambda: 1)
    assert youtube._pool_size() == 1


def test_cpu_count_desconhecido_nao_derruba(monkeypatch):
    monkeypatch.setattr(youtube.os, "cpu_count", lambda: None)
    assert youtube._pool_size() >= 1


def test_env_manda_quando_preenchida(monkeypatch):
    monkeypatch.setattr(youtube.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(youtube.config, "YTDLP_WORKERS", "3")
    assert youtube._pool_size() == 3


def test_env_com_lixo_cai_no_automatico(monkeypatch):
    monkeypatch.setattr(youtube.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(youtube.config, "YTDLP_WORKERS", "muitos")
    assert youtube._pool_size() == 1


def test_env_com_zero_ou_negativo_cai_no_automatico(monkeypatch):
    monkeypatch.setattr(youtube.os, "cpu_count", lambda: 8)
    for valor in ("0", "-2"):
        monkeypatch.setattr(youtube.config, "YTDLP_WORKERS", valor)
        assert youtube._pool_size() == 4


def test_pool_real_respeita_o_calculo():
    assert youtube._YTDLP_POOL._max_workers == youtube._pool_size()
