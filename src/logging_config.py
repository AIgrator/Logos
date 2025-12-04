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
    logging.getLogger('PIL').setLevel(logging.INFO)
    logging.getLogger('keyring').setLevel(logging.INFO)
    logging.getLogger('watchdog').setLevel(logging.INFO)
    # -----------------------------------------

    # Clear existing handlers to avoid duplication
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Add console handler
    stream_handler = logging.StreamHandler()
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
            root_logger.addHandler(file_handler)
        except Exception as e:
            logging.error(f"Failed to set up file logging to {log_file_path}: {e}", exc_info=True)

    # --- Dedicated LLM Communication Logger ---
    if logs_dir:
        try:
            os.makedirs(logs_dir, exist_ok=True)
            llm_log_file = os.path.join(logs_dir, "llm_communication.log")

            llm_logger = logging.getLogger('llm_communication')
            llm_logger.setLevel(logging.DEBUG)
            llm_logger.propagate = False

            # Clear existing handlers to avoid duplication
            if llm_logger.hasHandlers():
                llm_logger.handlers.clear()

            # Create a file handler that writes clean JSON
            llm_handler = logging.FileHandler(llm_log_file, mode='w', encoding='utf-8')

            # The formatter will now just output the message as is, since the message will be a JSON string
            llm_formatter = logging.Formatter('%(message)s')

            llm_handler.setFormatter(llm_formatter)
            llm_logger.addHandler(llm_handler)

            logging.info(f"LLM communication logging is set up to file: {llm_log_file}")

            # --- Dedicated RAW LLM RESPONSE Logger ---
            raw_llm_log_file = os.path.join(logs_dir, "llm_raw_response.log")
            raw_llm_logger = logging.getLogger('llm_raw_response')
            raw_llm_logger.setLevel(logging.INFO)
            raw_llm_logger.propagate = False
            if raw_llm_logger.hasHandlers():
                raw_llm_logger.handlers.clear()
            raw_llm_handler = logging.FileHandler(raw_llm_log_file, mode='w', encoding='utf-8')
            raw_llm_handler.setFormatter(logging.Formatter('%(message)s'))
            raw_llm_logger.addHandler(raw_llm_handler)
            logging.info(f"Raw LLM response logging is set up to file: {raw_llm_log_file}")

        except Exception as e:
            logging.error(f"Failed to set up LLM communication logger: {e}", exc_info=True)
