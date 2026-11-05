# coding: utf-8
"""Everything the strategy print()s to the QMT panel is also saved to the log file."""

import builtins
import io
import logging
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from bigqmt_signal_trader import logging_setup  # noqa: E402


def _reset_logger():
    logger = logging.getLogger("bigqmt")
    logging_setup._detach_existing_handlers(logger)
    logging_setup._initialized = False


@pytest.fixture
def panel(monkeypatch, tmp_path):
    """A private HOME, a clean logger, and a factory for a fake QMT panel."""
    for name in ("BIGQMT_LOG_ENABLED", "BIGQMT_LOG_TO_STDOUT", "BIGQMT_LOG_NAME",
                 "BIGQMT_LOG_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("BIGQMT_ACCOUNT_ID", "T1")
    saved = sys.stdout
    logger = logging.getLogger("bigqmt")
    # Hand the shared logger back exactly as found: other tests attach their
    # own handlers and rely on setup having already run.
    stash, was_initialized = list(logger.handlers), logging_setup._initialized
    for handler in stash:
        logger.removeHandler(handler)
    logging_setup._initialized = False
    previous_tee = getattr(builtins, "_bigqmt_panel_tee", None)

    def swap_in():
        # pytest re-points sys.stdout between setup and call, so the fake panel
        # has to be installed from inside the test body.
        fake = io.StringIO()
        sys.stdout = fake
        return fake

    yield swap_in
    sys.stdout = saved
    logging_setup._detach_existing_handlers(logger)
    for handler in stash:
        logger.addHandler(handler)
    logging_setup._initialized = was_initialized
    if previous_tee is None:
        if hasattr(builtins, "_bigqmt_panel_tee"):
            del builtins._bigqmt_panel_tee
    else:
        builtins._bigqmt_panel_tee = previous_tee


def _log_text(tmp_path):
    files = sorted((tmp_path / ".cache" / "bigqmt" / "logs").glob("bigqmt-T1.log*"))
    return "".join(f.read_text(encoding="utf-8") for f in files)


def test_print_reaches_panel_unchanged_and_the_file_with_timestamp(panel, tmp_path):
    out = panel()
    logging_setup.install_panel_capture()
    print("[bigqmt_signal_trader] init ok")
    print("a", end="")
    print("b")

    assert out.getvalue() == "[bigqmt_signal_trader] init ok\nab\n"
    text = _log_text(tmp_path)
    assert re.search(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d .*\[bigqmt_signal_trader\] init ok$",
                     text, re.M)
    assert re.search(r" ab$", text, re.M)


def test_logger_lines_are_written_once(panel, tmp_path):
    out = panel()
    logging_setup.install_panel_capture()
    logging_setup.get_logger("rpc").error("boom %s", 7)

    assert out.getvalue().count("boom 7") == 1
    assert _log_text(tmp_path).count("boom 7") == 1


def test_reload_does_not_stack_writers(panel, tmp_path):
    out = panel()
    logging_setup.install_panel_capture()
    logging_setup._initialized = False  # a purged-and-reimported module
    logging_setup.install_panel_capture()
    logging_setup._initialized = False
    logging_setup.install_panel_capture()
    print("once")

    assert out.getvalue() == "once\n"
    assert _log_text(tmp_path).count("once") == 1
    depth, stream = 0, sys.stdout
    while hasattr(stream, "_bigqmt_inner"):
        depth, stream = depth + 1, stream._bigqmt_inner
    assert depth == 1


def test_a_write_failure_never_reaches_the_caller(panel, monkeypatch):
    out = panel()
    logging_setup.install_panel_capture()

    def explode(record):
        raise OSError("disk full")

    for handler in logging.getLogger("bigqmt").handlers:
        monkeypatch.setattr(handler, "handle", explode, raising=False)
    print("still shown")

    assert out.getvalue() == "still shown\n"


def test_default_retention_is_30_days_and_env_overrides(panel, monkeypatch):
    panel()
    logging_setup.install_panel_capture()
    kept = [h.backupCount for h in logging.getLogger("bigqmt").handlers
            if hasattr(h, "backupCount")]
    assert kept == [30]

    _reset_logger()
    monkeypatch.setenv("BIGQMT_LOG_RETENTION_DAYS", "5")
    logging_setup.install_panel_capture()
    kept = [h.backupCount for h in logging.getLogger("bigqmt").handlers
            if hasattr(h, "backupCount")]
    assert kept == [5]


def test_two_bridges_use_two_files(panel, monkeypatch, tmp_path):
    panel()
    logging_setup.install_panel_capture()
    print("from T1")
    _reset_logger()
    sys.stdout = io.StringIO()
    monkeypatch.setenv("BIGQMT_ACCOUNT_ID", "T2")
    logging_setup.install_panel_capture()
    print("from T2")

    logs = tmp_path / ".cache" / "bigqmt" / "logs"
    assert "from T1" in (logs / "bigqmt-T1.log").read_text(encoding="utf-8")
    assert "from T2" in (logs / "bigqmt-T2.log").read_text(encoding="utf-8")
    assert "from T2" not in (logs / "bigqmt-T1.log").read_text(encoding="utf-8")


def test_disabled_leaves_stdout_alone(panel, monkeypatch):
    panel()
    monkeypatch.setenv("BIGQMT_LOG_ENABLED", "0")
    before = sys.stdout
    logging_setup.install_panel_capture()
    assert sys.stdout is before


def test_capture_works_when_broker_python_omits_logging(monkeypatch, tmp_path):
    original_import = builtins.__import__

    def import_without_logging(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "logging" or name.startswith("logging."):
            raise ImportError("broker python omits logging")
        return original_import(name, globals, locals, fromlist, level)

    for name in ("BIGQMT_LOG_ENABLED", "BIGQMT_LOG_NAME", "BIGQMT_LOG_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("BIGQMT_ACCOUNT_ID", "T1")
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(builtins, "_bigqmt_panel_tee", None, raising=False)
    source = ROOT / "src" / "bigqmt_signal_trader" / "logging_setup.py"
    namespace = {"__name__": "bigqmt_logging_nolog",
                 "__builtins__": dict(vars(builtins), __import__=import_without_logging)}
    exec(compile(source.read_bytes(), str(source), "exec"), namespace)

    namespace["install_panel_capture"]()
    print("no logging module")
    namespace["install_panel_capture"]()
    print("second")

    assert fake.getvalue() == "no logging module\nsecond\n"
    files = list((tmp_path / ".cache" / "bigqmt" / "logs").glob("bigqmt-T1*.log"))
    assert len(files) == 1
    text = files[0].read_text()
    assert text.count("no logging module") == 1 and text.count("second") == 1
