"""File-based logging for the Big QMT bridge.

Diagnostics used to be print()-only, which is lost when the QMT output panel
scrolls or the terminal restarts. This module wires Python's stdlib ``logging``
to a rotating file so errors survive restarts and can be reviewed after a
crash.

Usage (any module, both server and client):

    from bigqmt_signal_trader.logging_setup import get_logger
    log = get_logger("rpc")
    log.info("started")
    log.error("download failed: %s", exc)

Behavior:
- Log directory resolves to ``<qmt_python_dir>/logs`` when running inside QMT
  (found via a sys.path entry ending in ``\\python``), else ``~/.cache/bigqmt/logs``.
- Rotates at midnight into ``bigqmt.log.YYYY-MM-DD`` backups, keeping the last
  30 days by default (override with env BIGQMT_LOG_RETENTION_DAYS).
- Each record is also printed to stdout so the QMT output panel still shows it.
- Thread-safe (logging is; the print side is best-effort wrapped).
- Never raises: a logging failure must not bring down the strategy.
- Opt out via env BIGQMT_LOG_ENABLED=0 / BIGQMT_LOG_TO_STDOUT=0.
"""

import builtins
import datetime as _dt
import os
import sys
import threading
import time
import traceback

try:
    import logging
    import logging.handlers
except ImportError:
    # Some broker QMT Python bundles omit logging from python36.zip.  The
    # bridge still needs a minimal file/stdout logger to start and report why
    # later operations fail.
    logging = None

_LOGGER_NAME = "bigqmt"
_initialized = False
_DEFAULT_RETENTION_DAYS = 30
_panel_state = threading.local()


def _env_bool(name, default=True):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _resolve_log_dir():
    """Pick a writable log dir. Prefers the QMT python dir (the sys.path entry
    ending in ``\\python``) so logs sit beside the deployed strategy; falls back
    to a user cache dir otherwise. Deliberately does NOT use this package's own
    src/ directory (a repo checkout is not a writable runtime location)."""
    candidates = []
    for entry in sys.path:
        try:
            if entry and entry.endswith(r"\python") and os.path.isdir(entry):
                candidates.append(entry)
        except Exception:
            continue
    candidates.append(os.path.join(os.path.expanduser("~"), ".cache", "bigqmt"))
    for base in candidates:
        try:
            path = os.path.join(base, "logs")
            os.makedirs(path, exist_ok=True)
            # probe writability
            probe = os.path.join(path, ".write_test")
            with open(probe, "w"):
                pass
            try:
                os.remove(probe)
            except Exception:
                pass
            return path
        except Exception:
            continue
    return None


if logging is not None:
    class _SafeStreamHandler(logging.Handler):
        """print() the record so the QMT output panel shows it; never raises."""

        def emit(self, record):
            # The file handler already has this record; keep the panel tee
            # from writing it a second time.
            _panel_state.busy = True
            try:
                print(self.format(record))
            except Exception:
                pass
            finally:
                _panel_state.busy = False


class _FallbackLogger:
    """Small logger used only when the broker Python omits stdlib logging."""

    def __init__(self, name):
        self.name = name

    def _write(self, level, message, args, exception_text=None):
        try:
            rendered = str(message) % args if args else str(message)
        except Exception:
            rendered = "%s %s" % (message, args)
        if exception_text:
            rendered = "%s\n%s" % (rendered, exception_text)
        line = "%s [%s] [%s] %s" % (
            _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, self.name, rendered,
        )
        if _env_bool("BIGQMT_LOG_ENABLED", True):
            log_dir = _resolve_log_dir()
            if log_dir is not None:
                try:
                    with open(_fallback_path(log_dir), "a") as log_file:
                        log_file.write(line + "\n")
                except Exception:
                    pass
        if _env_bool("BIGQMT_LOG_TO_STDOUT", True):
            _panel_state.busy = True
            try:
                print(line)
            except Exception:
                pass
            finally:
                _panel_state.busy = False

    def debug(self, message, *args):
        self._write("DEBUG", message, args)

    def info(self, message, *args):
        self._write("INFO", message, args)

    def warning(self, message, *args):
        self._write("WARNING", message, args)

    def error(self, message, *args):
        self._write("ERROR", message, args)

    def exception(self, message, *args):
        self._write("ERROR", message, args, traceback.format_exc())


def _retention_days():
    try:
        return int(os.environ.get("BIGQMT_LOG_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        return _DEFAULT_RETENTION_DAYS


def _cleanup_old_logs(log_dir, retention_days):
    """Delete rotated log files older than retention_days.

    TimedRotatingFileHandler only prunes backups at rotation time; this sweeps
    stale files on startup too (e.g. after a weekend gap or a config change).
    """
    try:
        cutoff = time.time() - retention_days * 86400
        for name in os.listdir(log_dir):
            if not (name.startswith("bigqmt") and name.endswith(".log") or ".log." in name):
                continue
            path = os.path.join(log_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                continue
    except Exception:
        pass


def _detach_existing_handlers(logger):
    """Drop and CLOSE whatever a previous run left on this logger (issue #139).

    Idempotence cannot live in a module global here. The entry purges every
    bigqmt_signal_trader module from sys.modules on each start
    (_clear_local_modules, and reload_deployment does the same), so
    _initialized is back to False on the next import -- while
    logging.getLogger("bigqmt") lives in the logging module's own registry,
    which is never purged. The module state resets, the logger does not, so
    _setup() kept appending: after a day of restarts one log line was written
    to bigqmt.log SIXTEEN times.

    close() is the half that is easy to miss. Removing a handler without
    closing it leaves the file handle open, and TimedRotatingFileHandler then
    cannot rename its own file:

        PermissionError: [WinError 32] ... 'bigqmt.log' -> 'bigqmt.log.2026-08-31'

    which fired on every log write, and meant rotation never succeeded -- so
    backupCount pruning never ran and BIGQMT_LOG_RETENTION_DAYS did nothing.
    """
    for handler in list(getattr(logger, "handlers", [])):
        try:
            logger.removeHandler(handler)
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass


def _log_file_name():
    """Log file name, scoped so two processes never share one file (#139/#144).

    A user running two bridges at once -- one live account, one simulated --
    got both clients writing the same ~/.cache/bigqmt/logs/bigqmt.log. Two OS
    handles on one file means TimedRotatingFileHandler can never rename it on
    Windows:

        PermissionError: [WinError 32] ... 'bigqmt.log' -> 'bigqmt.log.2026-09-01'

    raised on every write, and rotation never succeeding means backupCount
    pruning never runs either. No amount of in-process handler bookkeeping
    fixes that -- the other handle belongs to another process.

    Account id first: it is stable across restarts, so a bridge keeps writing
    the same file and rotation still has something to roll. PID otherwise.
    BIGQMT_LOG_NAME pins it explicitly for anyone who wants the old behaviour.
    """
    explicit = str(os.environ.get("BIGQMT_LOG_NAME") or "").strip()
    if explicit:
        return explicit
    account = str(os.environ.get("BIGQMT_ACCOUNT_ID") or "").strip()
    tag = "".join(ch for ch in account if ch.isalnum()) or ("pid%d" % os.getpid())
    return "bigqmt-%s.log" % tag


def _tolerant_rotator(source, dest):
    """Rename for rotation, but never raise if the file is held.

    Belt and braces next to _log_file_name: someone pinning BIGQMT_LOG_NAME
    across two processes is back to a shared file, and a failed rename must not
    turn every subsequent log write into a traceback in the QMT panel. Losing
    one rotation is a smaller problem than losing the log's readability.
    """
    try:
        if os.path.exists(source):
            os.replace(source, dest)
    except Exception:
        pass


def _setup():
    global _initialized
    if _initialized:
        return
    _initialized = True
    if logging is None:
        return
    logger = logging.getLogger(_LOGGER_NAME)
    _detach_existing_handlers(logger)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not _env_bool("BIGQMT_LOG_ENABLED", True):
        logger.addHandler(logging.NullHandler())
        return

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler: rotate at midnight, keep the retention window only.
    log_dir = _resolve_log_dir()
    if log_dir is not None:
        try:
            fname = os.path.join(log_dir, _log_file_name())
            file_handler = logging.handlers.TimedRotatingFileHandler(
                fname,
                when="midnight",
                interval=1,
                backupCount=_retention_days(),
                encoding="utf-8",
                utc=False,
            )
            file_handler.rotator = _tolerant_rotator
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
            _cleanup_old_logs(log_dir, _retention_days())
        except Exception:
            pass

    # Stdout handler so the QMT panel still shows logs.
    if _env_bool("BIGQMT_LOG_TO_STDOUT", True):
        stream = _SafeStreamHandler()
        stream.setLevel(logging.INFO)
        stream.setFormatter(fmt)
        logger.addHandler(stream)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())


def get_logger(name=""):
    """Return a module logger under the shared ``bigqmt`` root.

    ``get_logger("rpc")`` -> logger named ``bigqmt.rpc``; the tag is shown in
    each log line so the old ``[bigqmt_rpc]`` prefixes remain visible.
    """
    _setup()
    suffix = str(name or "").strip(".")
    full = _LOGGER_NAME if not suffix else "%s.%s" % (_LOGGER_NAME, suffix)
    if logging is None:
        return _FallbackLogger(full)
    return logging.getLogger(full)


def _active_file_handler():
    for handler in getattr(logging.getLogger(_LOGGER_NAME), "handlers", []):
        if isinstance(handler, logging.handlers.TimedRotatingFileHandler):
            return handler
    return None


def _fallback_path(log_dir):
    """Per-day file for the no-logging-module path (no rotation exists there)."""
    name = _log_file_name()
    stem = name[:-4] if name.endswith(".log") else name
    return os.path.join(log_dir, "%s-%s.log" % (stem, _dt.date.today().strftime("%Y%m%d")))


def log_file_path():
    """Return the file the bridge is writing right now (None if file logging is off)."""
    _setup()
    if logging is not None:
        handler = _active_file_handler()
        return handler.baseFilename if handler is not None else None
    if not _env_bool("BIGQMT_LOG_ENABLED", True):
        return None
    log_dir = _resolve_log_dir()
    return None if log_dir is None else _fallback_path(log_dir)


class _PanelTee(object):
    """sys.stdout wrapper: the panel gets every byte first and unchanged, then
    each completed line is saved to the log file. Capture can never raise."""

    _bigqmt_panel_tee = True

    def __init__(self, inner, sink):
        self._bigqmt_inner = inner
        self._sink = sink
        self._enabled = True
        self._pending = threading.local()

    def write(self, text):
        result = self._bigqmt_inner.write(text)
        if self._enabled and not getattr(_panel_state, "busy", False):
            try:
                self._capture(text)
            except Exception:
                pass
        return result

    def _capture(self, text):
        pending = getattr(self._pending, "text", "") + str(text)
        lines = pending.split("\n")
        rest = lines.pop()
        if len(rest) > 65536:
            lines.append(rest)
            rest = ""
        self._pending.text = rest
        for line in lines:
            line = line.rstrip("\r")
            if line.strip():
                self._sink(line)

    def __getattr__(self, name):
        return getattr(self._bigqmt_inner, name)


def _make_panel_sink():
    if logging is not None:
        file_handler = _active_file_handler()
        if file_handler is None:
            return None

        def sink(line):
            record = logging.LogRecord("bigqmt.panel", logging.INFO, "", 0, line, None, None)
            file_handler.handle(record)

        return sink
    log_dir = _resolve_log_dir()
    if log_dir is None:
        return None
    _cleanup_old_logs(log_dir, _retention_days())

    def fallback_sink(line):
        stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_fallback_path(log_dir), "a") as log_file:
            log_file.write("%s [INFO] [bigqmt.panel] %s\n" % (stamp, line))

    return fallback_sink


def install_panel_capture():
    """Also save everything print()ed to the QMT panel into the log file.

    Idempotent across strategy reloads: the previous tee is switched off (it is
    found through ``builtins``, which survives the module purge), so only one
    writer is ever live. Never raises.
    """
    try:
        _setup()
        previous = getattr(builtins, "_bigqmt_panel_tee", None)
        if previous is not None:
            previous._enabled = False
        stream = sys.stdout
        while getattr(stream, "_bigqmt_panel_tee", False):
            stream = stream._bigqmt_inner
        sys.stdout = stream
        if not _env_bool("BIGQMT_LOG_ENABLED", True) or stream is None:
            return
        sink = _make_panel_sink()
        if sink is None:
            return
        tee = _PanelTee(stream, sink)
        builtins._bigqmt_panel_tee = tee
        sys.stdout = tee
    except Exception:
        pass
