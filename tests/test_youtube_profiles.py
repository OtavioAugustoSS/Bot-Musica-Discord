"""Cadeia de perfis de extração do yt-dlp (fallback + sticky).

O bug de origem: em agosto/2026 o YouTube passou a recusar os clients padrão
do yt-dlp 2026.7.4 ("The page needs to be reloaded"), e o bot tinha UMA
configuração só — sem alternativa, toda faixa era pulada.
"""
import asyncio

import pytest

from services import youtube


@pytest.fixture(autouse=True)
def _reset():
    youtube._stream_cache.clear()
    youtube.reset_profiles()
    yield
    youtube._stream_cache.clear()
    youtube.reset_profiles()


def _info(video_id: str = "vid1") -> dict:
    return {
        "id": video_id,
        "title": "T",
        "uploader": "A",
        "duration": 10,
        "thumbnail": "",
        "url": f"https://stream/{video_id}",
    }


def _fake_once(monkeypatch, behaviour):
    """behaviour(nome_do_perfil) -> dict ou raise. Devolve o log de tentativas."""
    tried: list[str] = []

    def fake(url: str, profile, **kwargs) -> dict:
        tried.append(profile.name)
        return behaviour(profile.name)

    monkeypatch.setattr(youtube, "_extract_once", fake)
    return tried


# ── fallback ────────────────────────────────────────────────────────────────


def test_cai_para_o_proximo_perfil_quando_o_primeiro_falha(monkeypatch):
    first = youtube.profile_names()[0]

    def behaviour(name):
        if name == first:
            raise RuntimeError("ERROR: [youtube] x: The page needs to be reloaded.")
        return _info()

    tried = _fake_once(monkeypatch, behaviour)
    result = asyncio.run(youtube.get_stream("vid1"))

    assert result["stream_url"] == "https://stream/vid1"
    assert tried[0] == first and len(tried) == 2


def test_extracao_sem_url_conta_como_falha(monkeypatch):
    """SABR: o YouTube devolve formato sem URL — sucesso aparente, áudio nenhum."""
    first = youtube.profile_names()[0]

    def behaviour(name):
        if name == first:
            return {"id": "vid1", "title": "T"}  # sem "url"
        return _info()

    tried = _fake_once(monkeypatch, behaviour)
    assert asyncio.run(youtube.get_stream("vid1"))["stream_url"] == "https://stream/vid1"
    assert len(tried) == 2


def test_todos_os_perfis_falham_propaga_o_erro(monkeypatch):
    _fake_once(monkeypatch, lambda name: (_ for _ in ()).throw(RuntimeError(f"falhou em {name}")))

    with pytest.raises(RuntimeError):
        asyncio.run(youtube.get_stream("vid1"))


def test_erro_permanente_do_video_nao_gasta_os_outros_perfis(monkeypatch):
    """Vídeo privado/removido é problema DO VÍDEO — trocar de client não ajuda."""
    tried = _fake_once(
        monkeypatch,
        lambda name: (_ for _ in ()).throw(RuntimeError("ERROR: [youtube] x: Video unavailable")),
    )

    with pytest.raises(RuntimeError):
        asyncio.run(youtube.get_stream("vid1"))
    assert len(tried) == 1


# ── sticky ──────────────────────────────────────────────────────────────────


def test_perfil_que_funcionou_vira_o_primeiro_da_proxima_vez(monkeypatch):
    names = youtube.profile_names()
    winner = names[1]

    def behaviour(name):
        if name != winner:
            raise RuntimeError("The page needs to be reloaded.")
        return _info()

    tried = _fake_once(monkeypatch, behaviour)

    asyncio.run(youtube.get_stream("vid1"))
    assert tried == [names[0], winner]
    assert youtube.active_profile() == winner

    tried.clear()
    asyncio.run(youtube.get_stream("vid2", use_cache=False))
    assert tried == [winner]  # sem pagar a falha do perfil quebrado de novo


def test_todos_os_perfis_sao_tentados_a_partir_do_sticky(monkeypatch):
    names = youtube.profile_names()
    youtube.set_active_profile(names[-1])

    tried = _fake_once(monkeypatch, lambda name: (_ for _ in ()).throw(RuntimeError("nope")))
    with pytest.raises(RuntimeError):
        asyncio.run(youtube.get_stream("vid1"))

    assert tried[0] == names[-1]
    assert sorted(tried) == sorted(names)  # rotação cobre todos, sem repetir


def test_perfil_preferido_e_reavaliado_depois_de_um_tempo(monkeypatch):
    """Sem isso o bot ficava preso no perfil pior (mp4 18) mesmo com o bom de volta."""
    names = youtube.profile_names()
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])
    quebrado = {names[0]}

    def behaviour(name):
        if name in quebrado:
            raise RuntimeError("The page needs to be reloaded.")
        return _info()

    tried = _fake_once(monkeypatch, behaviour)

    asyncio.run(youtube.get_stream("v1", use_cache=False))
    assert youtube.active_profile() == names[1]

    quebrado.clear()  # o perfil bom voltou
    tried.clear()
    asyncio.run(youtube.get_stream("v2", use_cache=False))
    assert tried == [names[1]]  # antes da janela: continua no que funciona

    clock["t"] += youtube._PROFILE_RECHECK_S + 1
    tried.clear()
    asyncio.run(youtube.get_stream("v3", use_cache=False))
    assert tried == [names[0]]  # reavaliou e voltou para o preferido
    assert youtube.active_profile() == names[0]


def test_reavaliacao_que_falha_nao_perde_o_perfil_que_funciona(monkeypatch):
    names = youtube.profile_names()
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])

    def behaviour(name):
        if name == names[0]:
            raise RuntimeError("The page needs to be reloaded.")
        return _info()

    tried = _fake_once(monkeypatch, behaviour)
    asyncio.run(youtube.get_stream("v1", use_cache=False))

    clock["t"] += youtube._PROFILE_RECHECK_S + 1
    tried.clear()
    asyncio.run(youtube.get_stream("v2", use_cache=False))

    assert tried == [names[0], names[1]]  # tentou o preferido, caiu de volta
    assert youtube.active_profile() == names[1]


# ── seleção de perfis ───────────────────────────────────────────────────────


def test_perfis_com_cookies_somem_quando_nao_ha_arquivo(monkeypatch):
    monkeypatch.setattr(youtube, "_cookie_state", lambda: ("", 0.0))
    youtube.reset_profiles()
    assert youtube.profile_names()  # sobra pelo menos o anônimo
    assert all(not p.use_cookies for p in youtube.profiles())


def test_perfis_com_cookies_aparecem_quando_ha_arquivo(monkeypatch):
    monkeypatch.setattr(youtube, "_cookie_state", lambda: ("data/cookies.txt", 1.0))
    youtube.reset_profiles()
    assert any(p.use_cookies for p in youtube.profiles())


def test_ordem_pode_ser_fixada_por_env(monkeypatch):
    monkeypatch.setattr(youtube, "_cookie_state", lambda: ("data/cookies.txt", 1.0))
    monkeypatch.setattr(youtube.config, "YTDLP_PROFILES", "cookies,padrao")
    youtube.reset_profiles()
    assert youtube.profile_names() == ["cookies", "padrao"]


def test_env_com_nome_invalido_e_ignorado_sem_derrubar_o_bot(monkeypatch):
    monkeypatch.setattr(youtube.config, "YTDLP_PROFILES", "nao-existe")
    youtube.reset_profiles()
    assert youtube.profile_names()  # caiu no padrão em vez de ficar sem perfil


# ── estado do arquivo de cookies ────────────────────────────────────────────


def _cookies(tmp_path, nomes):
    path = tmp_path / "cookies.txt"
    linhas = ["# Netscape HTTP Cookie File"]
    linhas += [f".youtube.com\tTRUE\t/\tTRUE\t9999999999\t{n}\tvalor" for n in nomes]
    path.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return str(path)


def test_cookies_ausentes(monkeypatch, tmp_path):
    monkeypatch.setattr(youtube.config, "YTDLP_COOKIES", str(tmp_path / "nao-existe.txt"))
    estado, _ = youtube.cookies_status()
    assert estado == "ausente"


def test_cookies_com_login_sao_ok(monkeypatch, tmp_path):
    path = _cookies(tmp_path, ["LOGIN_INFO", "SAPISID", "PREF"])
    monkeypatch.setattr(youtube.config, "YTDLP_COOKIES", path)
    assert youtube.cookies_status()[0] == "ok"


def test_cookies_reescritos_pelo_ytdlp_sao_detectados(monkeypatch, tmp_path):
    """Estado real da VM depois de a conta burner cair: o arquivo fica, o login some."""
    path = _cookies(tmp_path, ["PREF", "YSC", "VISITOR_INFO1_LIVE", "__Secure-3PAPISID"])
    monkeypatch.setattr(youtube.config, "YTDLP_COOKIES", path)
    estado, texto = youtube.cookies_status()
    assert estado == "sem-login"
    assert "PERDEU" in texto


# ── avisos do yt-dlp ────────────────────────────────────────────────────────


def test_aviso_de_cookie_vencido_vira_warning_e_fica_visivel(monkeypatch, caplog):
    youtube._alertas.clear()
    logger = youtube._YdlLogger()

    with caplog.at_level("WARNING", logger="services.youtube"):
        logger.warning(
            "WARNING: [youtube] The provided YouTube account cookies are no longer valid."
        )

    assert "cookies do YouTube venceram" in caplog.text
    assert any("cookies" in a for a in youtube.alerts())


def test_aviso_repetido_nao_spamma_o_log(monkeypatch, caplog):
    youtube._alertas.clear()
    logger = youtube._YdlLogger()
    msg = "WARNING: cookies are no longer valid"

    with caplog.at_level("WARNING", logger="services.youtube"):
        for _ in range(5):
            logger.warning(msg)

    assert caplog.text.count("cookies do YouTube venceram") == 1
    assert len(youtube.alerts()) == 1


def test_aviso_expira_depois_da_janela(monkeypatch):
    youtube._alertas.clear()
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])

    youtube._YdlLogger().warning("cookies are no longer valid")
    assert youtube.alerts()

    clock["t"] += youtube._ALERTA_TTL_S + 1
    assert youtube.alerts() == []


def test_aviso_comum_do_ytdlp_nao_polui(caplog):
    youtube._alertas.clear()
    with caplog.at_level("WARNING", logger="services.youtube"):
        youtube._YdlLogger().warning("WARNING: Falling back to generic extractor")
    assert caplog.text == ""
    assert youtube.alerts() == []


# ── mensagem de erro para o usuário ─────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,esperado",
    [
        ("ERROR: [youtube] x: The page needs to be reloaded.", "YouTube recusou"),
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "cookies"),
        ("ERROR: [youtube] x: Video unavailable", "indisponível"),
        ("ERROR: [youtube] x: Private video", "privado"),
        ("ERROR: [youtube] x: Requested format is not available", "formato"),
    ],
)
def test_friendly_error_traduz_os_erros_conhecidos(raw, esperado):
    assert esperado in youtube.friendly_error(RuntimeError(raw))


def test_friendly_error_encurta_erro_desconhecido():
    texto = youtube.friendly_error(RuntimeError("x" * 500))
    assert len(texto) <= 160


def test_cadeia_encolhendo_nao_estoura_indice(monkeypatch):
    """4 threads no pool: a cadeia pode encolher entre a escrita e a leitura de _active."""
    monkeypatch.setattr(youtube, "_cookie_state", lambda: ("data/cookies.txt", 1.0))
    youtube.reset_profiles()
    youtube.set_active_profile(youtube.profile_names()[-1])

    monkeypatch.setattr(youtube, "_cookie_state", lambda: ("", 0.0))  # cookies sumiram
    _fake_once(monkeypatch, lambda name: _info())

    assert asyncio.run(youtube.get_stream("v1", use_cache=False))["video_id"] == "vid1"


def test_orcamento_de_tempo_corta_a_cadeia(monkeypatch):
    """4 perfis pendurados prenderiam os 4 slots do pool e calariam o bot inteiro."""
    monkeypatch.setattr(youtube, "_cookie_state", lambda: ("data/cookies.txt", 1.0))
    youtube.reset_profiles()
    assert len(youtube.profile_names()) >= 3  # cadeia longa o bastante para o teste

    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])

    def lento(name):
        clock["t"] += 20.0  # cada perfil demora 20s
        raise RuntimeError("timed out")

    tried = _fake_once(monkeypatch, lento)
    with pytest.raises(RuntimeError):
        asyncio.run(youtube.get_stream("vid1", use_cache=False))

    assert len(tried) == 2  # parou no orçamento em vez de queimar a cadeia toda


def test_orcamento_nao_impede_a_primeira_tentativa(monkeypatch):
    """Mesmo estourado, o perfil preferido sempre é tentado — senão nada tocaria."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])

    def lento(name):
        clock["t"] += 999.0
        return _info()

    tried = _fake_once(monkeypatch, lento)
    assert asyncio.run(youtube.get_stream("vid1", use_cache=False))["video_id"] == "vid1"
    assert len(tried) == 1


def _muxado(video_id: str = "vid1") -> dict:
    """Formato 18: mp4 com vídeo junto, áudio AAC — toca, mas é o pior caso."""
    return {**_info(video_id), "vcodec": "avc1.42001E", "acodec": "mp4a.40.2",
            "format_id": "18"}


def _audio(video_id: str = "vid1") -> dict:
    return {**_info(video_id), "vcodec": "none", "acodec": "opus", "format_id": "251"}


def test_muxado_no_perfil_preferido_nao_impede_procurar_opus(monkeypatch):
    """Sem a varredura o bot ficava em 96kbps + banda de vídeo para sempre."""
    names = youtube.profile_names()
    tried = _fake_once(
        monkeypatch, lambda name: _muxado() if name == names[0] else _audio()
    )

    asyncio.run(youtube.get_stream("v1", use_cache=False))

    assert tried == names[:2]
    assert youtube.active_profile() == names[1]  # ficou no que dá áudio puro


def test_sem_nenhum_audio_puro_usa_o_muxado(monkeypatch):
    """Muxado é pior que opus, mas infinitamente melhor que silêncio."""
    tried = _fake_once(monkeypatch, lambda name: _muxado())

    got = asyncio.run(youtube.get_stream("v1", use_cache=False))

    assert got["stream_url"] == "https://stream/vid1"
    assert len(tried) == len(youtube.profile_names())


def test_varredura_de_qualidade_acontece_uma_vez_por_janela(monkeypatch):
    """Se tudo é muxado, não dá para pagar a cadeia inteira em toda faixa."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube, "_now", lambda: clock["t"])
    tried = _fake_once(monkeypatch, lambda name: _muxado())

    asyncio.run(youtube.get_stream("v1", use_cache=False))
    total = len(tried)
    assert total == len(youtube.profile_names())

    tried.clear()
    asyncio.run(youtube.get_stream("v2", use_cache=False))
    assert len(tried) == 1  # dentro da janela: uma tentativa só

    clock["t"] += youtube._PROFILE_RECHECK_S + 1
    tried.clear()
    asyncio.run(youtube.get_stream("v3", use_cache=False))
    assert len(tried) == total  # janela venceu: procura opus de novo


def test_busca_nao_paga_a_varredura_de_qualidade(monkeypatch):
    """`ytsearch:` devolve playlist — não tem vcodec para julgar."""
    tried = _fake_once(monkeypatch, lambda name: {"entries": [{"id": "a1", "title": "T"}]})

    youtube._ytdlp_search("consulta", 3)

    assert len(tried) == 1
