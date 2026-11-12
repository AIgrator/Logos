import logging
import os

def setup_logging(log_to_file=False, logs_dir=None, log_level="DEBUG"):
    # --- Basic Root Logger Setup (Robust Method) ---
    log_level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(filename)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # --- Silence noisy third-party loggers ---
    # Pillow library is very verbose in DEBUG mode, showing every image plugin it tries to import.
    # We raise its log level to INFO to hide these messages without affecting our own logs.
    # httpx/httpcore/openai DEBUG is an SSE-per-packet flood that drowns the
    # step-by-step story — INFO keeps the timeline readable.
    logging.getLogger('PIL').setLevel(logging.INFO)
    logging.getLogger('keyring').setLevel(logging.INFO)
    logging.getLogger('watchdog').setLevel(logging.INFO)
    logging.getLogger('httpx').setLevel(logging.INFO)
    logging.getLogger('httpcore').setLevel(logging.INFO)
    logging.getLogger('openai').setLevel(logging.INFO)
    logging.getLogger('anthropic').setLevel(logging.INFO)
    # Python-Markdown logs three DEBUG lines every time it builds a Markdown
    # instance, and the chat builds one per streamed frame: a single long
    # answer wrote 10,677 lines (1.7 MB) of "Successfully loaded extension".
    logging.getLogger('MARKDOWN').setLevel(logging.INFO)
    # -----------------------------------------

    # Clear existing handlers to avoid duplication
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Add console handler (INFO: user-facing story, no DEBUG flood)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # Add file handler if requested
    if log_to_file and logs_dir:
        try:
            # Ensure the directory exists
            os.makedirs(logs_dir, exist_ok=True)
            log_file_path = os.path.join(logs_dir, "app.log")
            # Use mode='w' to overwrite the log on each run
            file_handler = logging.FileHandler(log_file_path, mode='w', encoding="utf-8")
            file_handler.setFormatter(formatter)
            # One timeline = one line per record. Provider files stay dumb:
            # any multi-line message (pretty JSON, raw chunks) is flattened
            # here, centrally. Tracebacks are appended by the formatter
            # AFTER filters run, so they keep their lines.
            class _SingleLineFilter(logging.Filter):
                def filter(self, record):
                    try:
                        if isinstance(record.msg, str) and "\n" in record.msg:
                            record.msg = " | ".join(
                                line.strip() for line in
                                record.msg.splitlines() if line.strip())
                    except Exception:
                        pass
                    return True
            file_handler.addFilter(_SingleLineFilter())
            root_logger.addHandler(file_handler)
        except Exception as e:
            logging.error(f"Failed to set up file logging to {log_file_path}: {e}", exc_info=True)

    # --- Single-timeline logging ---
    # There is deliberately ONE log file (app.log). The former
    # llm_communication.log / llm_raw_response.log writers now propagate to
    # the root handlers, so model dialogue (single-line JSON) interleaves
    # chronologically with everything else — no cross-file timestamp
    # matching. Console stays INFO (no DEBUG flood), the file keeps DEBUG.
    for _name in ('llm_communication', 'llm_raw_response'):
        _lg = logging.getLogger(_name)
        _lg.setLevel(logging.DEBUG)
        _lg.propagate = True
        if _lg.hasHandlers():
            _lg.handlers.clear()
