# coding: utf-8
"""log_file_path() names the file the bridge is really writing."""

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from bigqmt_signal_trader import logging_setup  # noqa: E402


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for name in ("BIGQMT_LOG_ENABLED", "BIGQMT_LOG_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("BIGQMT_LOG_TO_STDOUT", "0")
    monkeypatch.setenv("BIGQMT_ACCOUNT_ID", "T9")
    logger = logging.getLogger("bigqmt")
    # Hand the shared logger back exactly as found: other tests attach their
    # own handlers and rely on setup having already run.
    stash, was_initialized = list(logger.handlers), logging_setup._initialized
    for handler in stash:
        logger.removeHandler(handler)
    logging_setup._initialized = False
    yield tmp_path
    logging_setup._detach_existing_handlers(logger)
    for handler in stash:
        logger.addHandler(handler)
    logging_setup._initialized = was_initialized


def test_path_is_the_per_account_file_the_handler_writes(clean):
    logging_setup.get_logger("rpc").error("hello")
    path = Path(logging_setup.log_file_path())

    assert path.name == "bigqmt-T9.log"
    assert path.parent == clean / ".cache" / "bigqmt" / "logs"
    assert "hello" in path.read_text(encoding="utf-8")


def test_pinned_name_is_reported(clean, monkeypatch):
    monkeypatch.setenv("BIGQMT_LOG_NAME", "custom.log")
    logging_setup.get_logger("rpc").error("x")
    assert Path(logging_setup.log_file_path()).name == "custom.log"


def test_path_is_none_when_file_logging_is_off(clean, monkeypatch):
    monkeypatch.setenv("BIGQMT_LOG_ENABLED", "0")
    assert logging_setup.log_file_path() is None
