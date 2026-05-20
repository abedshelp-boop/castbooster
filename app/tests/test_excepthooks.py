"""Pillar 3.5 thread 1: verify our exception hooks log before exit."""
import logging
import sys


def test_sys_excepthook_logs_unhandled_main_thread_exception(caplog):
    from castbooster.main import _install_sys_excepthook

    original = sys.excepthook
    try:
        _install_sys_excepthook()
        try:
            raise ValueError("synthetic main-thread crash")
        except ValueError:
            exc_type, exc_value, exc_tb = sys.exc_info()
        with caplog.at_level(logging.ERROR, logger="castbooster"):
            sys.excepthook(exc_type, exc_value, exc_tb)
    finally:
        sys.excepthook = original

    matches = [r for r in caplog.records
               if "FATAL unhandled exception" in r.getMessage()]
    assert matches, f"expected FATAL log, got {[r.getMessage() for r in caplog.records]}"
    assert matches[0].exc_info is not None
    assert matches[0].exc_info[0] is ValueError


def test_sys_excepthook_install_is_idempotent():
    """Calling _install_sys_excepthook twice must not chain hooks."""
    from castbooster.main import _install_sys_excepthook

    original = sys.excepthook
    try:
        _install_sys_excepthook()
        first_hook = sys.excepthook
        _install_sys_excepthook()
        second_hook = sys.excepthook
        assert first_hook is second_hook, (
            "second install must be a no-op; got a new hook object"
        )
    finally:
        sys.excepthook = original
