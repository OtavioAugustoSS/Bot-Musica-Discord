from core.queue import MAX_QUEUE, LoopMode, MusicQueue, Track


def track(name: str) -> Track:
    return Track(title=name)


def test_queue_is_capped_at_max():
    q = MusicQueue()
    added = q.add_many([track(f"t{i}") for i in range(MAX_QUEUE + 50)])
    assert added == MAX_QUEUE
    assert len(q) == MAX_QUEUE
    q.add(track("estourada"))  # add unitário também respeita o teto
    assert len(q) == MAX_QUEUE
    assert q.add_many([track("x")]) == 0


def test_loop_queue_still_recycles_at_cap():
    q = MusicQueue()
    q.add_many([track(f"t{i}") for i in range(MAX_QUEUE)])
    q.loop_mode = LoopMode.QUEUE
    first = q.advance()
    q.advance()  # devolve a atual para o fim da fila mesmo cheia
    assert first in q.upcoming


def test_advance_follows_fifo_order():
    q = MusicQueue()
    q.add_many([track("a"), track("b")])
    assert q.advance().title == "a"
    assert q.advance().title == "b"
    assert q.advance() is None


def test_advance_on_empty_queue_returns_none():
    q = MusicQueue()
    assert q.advance() is None
    assert q.current is None


def test_loop_track_repeats_current():
    q = MusicQueue()
    q.add_many([track("a"), track("b")])
    q.advance()
    q.loop_mode = LoopMode.TRACK
    assert q.advance().title == "a"
    assert q.advance().title == "a"


def test_loop_track_ignored_on_skip():
    q = MusicQueue()
    q.add_many([track("a"), track("b")])
    q.advance()
    q.loop_mode = LoopMode.TRACK
    assert q.advance(ignore_loop=True).title == "b"


def test_loop_queue_cycles():
    q = MusicQueue()
    q.add_many([track("a"), track("b")])
    q.loop_mode = LoopMode.QUEUE
    assert q.advance().title == "a"
    assert q.advance().title == "b"
    assert q.advance().title == "a"  # voltou ao início


def test_remove_is_one_based():
    q = MusicQueue()
    q.add_many([track("a"), track("b"), track("c")])
    removed = q.remove(2)
    assert removed.title == "b"
    assert [t.title for t in q.upcoming] == ["a", "c"]


def test_remove_invalid_position_returns_none():
    q = MusicQueue()
    q.add(track("a"))
    assert q.remove(0) is None
    assert q.remove(2) is None
    assert len(q) == 1


def test_shuffle_preserves_tracks():
    q = MusicQueue()
    names = [f"t{i}" for i in range(20)]
    q.add_many([track(n) for n in names])
    q.shuffle()
    assert sorted(t.title for t in q.upcoming) == sorted(names)


def test_clear_resets_everything():
    q = MusicQueue()
    q.add_many([track("a"), track("b")])
    q.advance()
    q.clear()
    assert q.current is None
    assert len(q) == 0


def test_loop_mode_cycles_through_all_modes():
    assert LoopMode.OFF.cycle() is LoopMode.TRACK
    assert LoopMode.TRACK.cycle() is LoopMode.QUEUE
    assert LoopMode.QUEUE.cycle() is LoopMode.OFF


def test_track_search_query_and_display():
    t = Track(title="Time", artist="Pink Floyd")
    assert t.search_query == "Pink Floyd Time"
    assert t.display == "Time — Pink Floyd"
    assert Track(title="Solta").display == "Solta"


def test_track_url_requires_video_id():
    assert Track(title="x").url == ""
    assert "watch?v=abc" in Track(title="x", video_id="abc").url
