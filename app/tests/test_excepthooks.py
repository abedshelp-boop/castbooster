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


import threading


def test_threading_excepthook_logs_non_main_thread_exception(caplog):
    from castbooster.main import _install_threading_excepthook

    original = threading.excepthook
    try:
        _install_threading_excepthook()
        # threading.ExceptHookArgs is a named tuple; build one manually
        # so we can call the hook without spinning a real thread.
        try:
            raise RuntimeError("synthetic side-thread crash")
        except RuntimeError as exc:
            # In Python 3.14, ExceptHookArgs is a C-level structseq that
            # only accepts a positional iterable — no keyword arguments.
            # Field order matches __match_args__: exc_type, exc_value,
            # exc_traceback, thread.
            args = threading.ExceptHookArgs(
                (type(exc), exc, exc.__traceback__, threading.current_thread())
            )
        with caplog.at_level(logging.ERROR, logger="castbooster"):
            threading.excepthook(args)
    finally:
        threading.excepthook = original

    matches = [r for r in caplog.records
               if "FATAL unhandled exception in thread" in r.getMessage()]
    assert matches, f"expected FATAL log, got {[r.getMessage() for r in caplog.records]}"
    assert matches[0].exc_info[0] is RuntimeError


def test_threading_excepthook_install_is_idempotent():
    """Calling _install_threading_excepthook twice must not chain hooks."""
    from castbooster.main import _install_threading_excepthook

    original = threading.excepthook
    try:
        _install_threading_excepthook()
        first_hook = threading.excepthook
        _install_threading_excepthook()
        second_hook = threading.excepthook
        assert first_hook is second_hook, (
            "second install must be a no-op; got a new hook object"
        )
    finally:
        threading.excepthook = original


import asyncio


def test_asyncio_exception_handler_logs_unhandled_coroutine_exception(caplog):
    from castbooster.proxy import _asyncio_exception_handler

    loop = asyncio.new_event_loop()
    try:
        try:
            raise KeyError("synthetic coroutine crash")
        except KeyError as exc:
            context = {
                "message": "unhandled coroutine exception",
                "exception": exc,
            }
        with caplog.at_level(logging.ERROR, logger="castbooster"):
            _asyncio_exception_handler(loop, context)
    finally:
        loop.close()

    matches = [r for r in caplog.records
               if "FATAL unhandled asyncio exception" in r.getMessage()]
    assert matches, f"expected FATAL log, got {[r.getMessage() for r in caplog.records]}"
