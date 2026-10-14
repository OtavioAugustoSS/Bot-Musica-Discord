"""Cache do player do YouTube pré-processado — a extração não pode sequestrar a CPU.

Medido na VM de produção em 17/09, com música tocando: toda faixa nova
dispara a extração da próxima (prefetch) e cada uma roda 11 a 18s, quase
tudo no deno refazendo o parse do player JS inteiro (3,2MB). Nesse tempo o
steal da VM vai a 60–74% — o hipervisor pausa a máquina INTEIRA — e os
pacotes de voz saem com buracos de 80 a 320ms seguidos de rajadas. É o
"trava, fica lenta, fica rápida" que se ouve na call. `nice` não ajuda: ele
só reordena quem roda dentro da VM, não reduz a conta total.

O yt-dlp sabe guardar o player já pré-processado e mandar só ele para o
runtime JS, mas vem desligado ("arquivos grandes e sem rotação"). Medido no
dev com os clients do perfil de cookies: 2,8–4,1s de runtime por extração
sem cache, 0,4–0,56s com. A rotação e a proteção contra cache velho ficam
por nossa conta — é o que estes testes cobrem.
"""
import os

import pytest
from yt_dlp.extractor.youtube.jsc._builtin import ejs

from services import jsc_cache


@pytest.fixture
def raiz(tmp_path, monkeypatch):
    monkeypatch.setattr(jsc_cache, "_ultima_manutencao", float("-inf"))
    return tmp_path


def _player(raiz, nome: str, mtime: float) -> str:
    caminho = os.path.join(raiz, f"player,3Ahttps,3A{nome}.json")
    with open(caminho, "w", encoding="utf-8") as fh:
        fh.write('{"data": "x"}')
    os.utime(caminho, (mtime, mtime))
    return caminho


def _marca_versao_atual(raiz) -> None:
    with open(os.path.join(raiz, jsc_cache.MARCADOR), "w", encoding="utf-8") as fh:
        fh.write(jsc_cache.versao())


def test_ativa_liga_o_cache_do_player_preprocessado(monkeypatch):
    monkeypatch.setattr(ejs.EJSBaseJCP, "_ENABLE_PREPROCESSED_PLAYER_CACHE", False)
    monkeypatch.setattr(ejs.EJSBaseJCP, "_real_bulk_solve", ejs.EJSBaseJCP._real_bulk_solve)
    assert jsc_cache.ativa() is True
    assert ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE is True


def test_ativa_duas_vezes_nao_embrulha_a_rede_duas_vezes(monkeypatch):
    monkeypatch.setattr(ejs.EJSBaseJCP, "_real_bulk_solve", ejs.EJSBaseJCP._real_bulk_solve)
    jsc_cache.ativa()
    uma = ejs.EJSBaseJCP._real_bulk_solve
    jsc_cache.ativa()
    assert ejs.EJSBaseJCP._real_bulk_solve is uma


def test_rotacao_mantem_so_os_players_mais_novos(raiz):
    _marca_versao_atual(raiz)
    velhos = [_player(raiz, "a", 1000), _player(raiz, "b", 2000)]
    novos = [_player(raiz, "c", 3000), _player(raiz, "d", 4000)]
    # scripts do solver baixados moram na mesma seção e não são players
    script = os.path.join(raiz, "core.json")
    open(script, "w").close()

    removidos = jsc_cache.manutencao(str(raiz), manter=2)

    assert removidos == 2
    assert not any(os.path.exists(p) for p in velhos)
    assert all(os.path.exists(p) for p in novos)
    assert os.path.exists(script)


def test_versao_nova_do_yt_dlp_joga_fora_todos_os_players(raiz):
    with open(os.path.join(raiz, jsc_cache.MARCADOR), "w", encoding="utf-8") as fh:
        fh.write("yt-dlp 2020.01.01 / ejs 0.0.1")
    players = [_player(raiz, "a", 1000), _player(raiz, "b", 2000)]

    removidos = jsc_cache.manutencao(str(raiz), manter=2)

    assert removidos == 2
    assert not any(os.path.exists(p) for p in players)
    with open(os.path.join(raiz, jsc_cache.MARCADOR), encoding="utf-8") as fh:
        assert fh.read() == jsc_cache.versao()


def test_sem_marcador_os_players_existentes_sao_de_origem_desconhecida(raiz):
    player = _player(raiz, "a", 1000)
    assert jsc_cache.manutencao(str(raiz)) == 1
    assert not os.path.exists(player)
    assert os.path.exists(os.path.join(raiz, jsc_cache.MARCADOR))


def test_raiz_que_nao_existe_e_criada_com_o_marcador(tmp_path, monkeypatch):
    monkeypatch.setattr(jsc_cache, "_ultima_manutencao", float("-inf"))
    raiz = tmp_path / "yt-dlp" / "challenge-solver"
    assert jsc_cache.manutencao(str(raiz)) == 0
    assert (raiz / jsc_cache.MARCADOR).exists()


def test_manutencao_roda_no_maximo_uma_vez_por_intervalo(raiz, monkeypatch):
    relogio = [5000.0]
    monkeypatch.setattr(jsc_cache, "_agora", lambda: relogio[0])
    _marca_versao_atual(raiz)
    for i in range(3):
        _player(raiz, str(i), 1000 + i)
    assert jsc_cache.manutencao(str(raiz), manter=2) == 1

    for i in range(3, 6):
        _player(raiz, str(i), 1000 + i)
    relogio[0] += 60
    assert jsc_cache.manutencao(str(raiz), manter=2) == 0  # cedo demais

    relogio[0] += jsc_cache.INTERVALO_S
    assert jsc_cache.manutencao(str(raiz), manter=2) == 3


# ── ligação com services.youtube ───────────────────────────────────────────


def test_importar_o_youtube_ja_liga_o_cache():
    from services import youtube  # noqa: F401 — o efeito é a importação

    assert ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE is True
    assert getattr(ejs.EJSBaseJCP._real_bulk_solve, "_bot_rede", False)


def test_cada_extracao_real_passa_pela_manutencao(monkeypatch):
    from services import youtube

    chamadas = []
    monkeypatch.setattr(jsc_cache, "manutencao", lambda *a, **k: chamadas.append(1) or 0)

    class _Ydl:
        def extract_info(self, url, download=False):
            return {"id": "x", "url": "https://stream/x"}

    monkeypatch.setattr(youtube, "_get_ydl", lambda profile, flat=False: _Ydl())
    youtube._extract_once("https://www.youtube.com/watch?v=x", youtube.profiles()[0])
    assert chamadas == [1]


# ── rede de segurança: player em cache que não resolve ─────────────────────


class _Cache:
    def __init__(self, dados: dict):
        self.dados = dados

    def load(self, section, key, *args, **kwargs):
        return self.dados.get((section, key))

    def store(self, section, key, data, *args, **kwargs):
        self.dados[(section, key)] = data


class _Req:
    def __init__(self, url: str):
        self.input = type("Entrada", (), {"player_url": url})()


class _Provider:
    _CACHE_SECTION = "challenge-solver"

    def __init__(self, cache: _Cache):
        self.ie = type("IE", (), {"cache": cache})()


URL = "https://www.youtube.com/s/player/abc/base.js"
CHAVE = ("challenge-solver", f"player:{URL}")


def _original_que_falha_com_cache(chamadas: list):
    def original(self, requests):
        usou_cache = bool(self.ie.cache.load(*CHAVE))
        chamadas.append(usou_cache)
        if usou_cache:
            raise ValueError("preprocessed player quebrado")
        yield "resposta"

    return original


def test_player_em_cache_que_falha_e_apagado_e_resolve_sem_ele():
    chamadas: list = []
    rede = jsc_cache._com_rede(_original_que_falha_com_cache(chamadas))
    provider = _Provider(_Cache({CHAVE: "player velho"}))

    assert list(rede(provider, [_Req(URL)])) == ["resposta"]
    assert chamadas == [True, False]
    assert not provider.ie.cache.load(*CHAVE)


def test_falha_sem_player_em_cache_sobe_sem_repetir():
    chamadas: list = []

    def original(self, requests):
        chamadas.append(1)
        raise ValueError("deno morreu")
        yield  # nunca chega aqui: só mantém a função como gerador

    rede = jsc_cache._com_rede(original)
    with pytest.raises(ValueError, match="deno morreu"):
        list(rede(_Provider(_Cache({})), [_Req(URL)]))
    assert chamadas == [1]


def test_falha_depois_de_entregar_respostas_nao_repete():
    """Repetir entregaria respostas em dobro ao yt-dlp — só limpa e sobe."""
    chamadas: list = []

    def original(self, requests):
        chamadas.append(1)
        yield "primeira"
        raise ValueError("segundo grupo quebrou")

    rede = jsc_cache._com_rede(original)
    provider = _Provider(_Cache({CHAVE: "player velho"}))
    respostas = rede(provider, [_Req(URL)])
    assert next(respostas) == "primeira"
    with pytest.raises(ValueError):
        next(respostas)
    assert chamadas == [1]
    assert not provider.ie.cache.load(*CHAVE)
