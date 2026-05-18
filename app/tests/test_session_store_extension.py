"""Smoke tests for the P2.4 StreamSession extension."""
from castbooster.session_store import SessionStore, StreamSession


def test_new_session_has_no_transcoder():
    store = SessionStore()
    sess = store.create("https://example.com/playlist.m3u8")
    assert sess.transcoder is None
    assert sess.output_dir is None
    assert sess.passthrough_only is False


def test_session_fields_are_assignable():
    sess = StreamSession(token="x", upstream_url="https://example.com/p.m3u8")
    # Sanity: fields exist + are mutable
    sess.passthrough_only = True
    assert sess.passthrough_only is True
