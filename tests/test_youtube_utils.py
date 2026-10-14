from services.youtube import is_allowed_url, is_url, parse_duration


def test_parse_duration_minutes_seconds():
    assert parse_duration("3:45") == 225


def test_parse_duration_with_hours():
    assert parse_duration("1:02:03") == 3723


def test_parse_duration_garbage_and_empty():
    assert parse_duration(None) == 0
    assert parse_duration("") == 0
    assert parse_duration("ao vivo") == 0


def test_is_url():
    assert is_url("https://youtube.com/watch?v=x")
    assert is_url("http://youtu.be/x")
    assert not is_url("never gonna give up you")


def test_allowed_url_accepts_youtube_hosts():
    assert is_allowed_url("https://youtube.com/watch?v=x")
    assert is_allowed_url("https://www.youtube.com/watch?v=x&list=y")
    assert is_allowed_url("https://m.youtube.com/watch?v=x")
    assert is_allowed_url("https://music.youtube.com/watch?v=x")
    assert is_allowed_url("http://youtu.be/dQw4w9WgXcQ")
    assert is_allowed_url("https://YOUTUBE.com/watch?v=x")  # host é case-insensitive


def test_allowed_url_blocks_ssrf_targets():
    # metadata da nuvem, localhost e IP literal: o motivo da allowlist
    assert not is_allowed_url("http://169.254.169.254/opc/v2/instance/")
    assert not is_allowed_url("http://127.0.0.1:8888/callback")
    assert not is_allowed_url("https://evil.com/watch?v=x")


def test_allowed_url_blocks_lookalike_hosts():
    assert not is_allowed_url("https://youtube.com.evil.com/watch?v=x")  # sufixo falso
    assert not is_allowed_url("https://www.youtube.com@evil.com/x")  # userinfo
    assert not is_allowed_url("ftp://youtube.com/x")  # esquema errado
    assert not is_allowed_url("nome de música qualquer")
