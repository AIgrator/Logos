"""
Main entry point of the application.
"""
import sys
import os
import time
import logging

def main():
    """Main function to run the application."""
    # Capture the original current working directory *before* changing it.
    # This is needed for the terminal mode's default working directory.
    original_cwd = os.getcwd()

    # Set the current working directory to the script's directory.
    # This ensures that all relative paths for resources (models, icons, etc.)
    # are resolved correctly, regardless of where the application is launched from.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # Print an immediate startup message so the user knows the app is loading
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Application starting, please wait for modules to load...")

    # Now perform the heavy imports
    # import shutil
    # import threading
    from PySide6.QtWidgets import QApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    # Add the 'src' directory to the Python path
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] before ModelConfigManager")
    from llm.model_config_manager import ModelConfigManager
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] after ModelConfigManager")
    # Initialize the model configuration before any other components are created.
    # This ensures that all parts of the app see the same, up-to-date model list.
    ModelConfigManager.initialize()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ModelConfigManager.initialized")
    import logging_config
    from controller import AppController
    from settings_manager import SettingsManager
    from thread_pool import ThreadPool
    from initial_setup import run_setup_wizard

    # --- Main Execution ---
    settings_manager = SettingsManager()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] settings_manager initialized")

    # --- Setup-only mode ---
    if '--run-setup' in sys.argv:
        app = QApplication(sys.argv)
        # The setup wizard now handles its own theme application
        setup_successful = run_setup_wizard(settings_manager)
        sys.exit(0 if setup_successful else 1)
    # --- End Setup-only mode ---

    # Setup logging as early as possible, using the path from the settings manager
    logs_dir = settings_manager.get('APP_LOGS_DIR')
    logging_config.setup_logging(log_to_file=True, logs_dir=logs_dir)

    from build_info import get_build_id
    logging.info(f"Build: {get_build_id()} (running from {script_dir})")

    # Crash diagnostics: on a hard crash (segfault) faulthandler writes the
    # Python stack of every thread to crash.log (normal logs die with the buffer).
    try:
        import faulthandler
        crash_log = open(os.path.join(logs_dir, "crash.log"), "w", encoding="utf-8")
        faulthandler.enable(file=crash_log)
        logging.info(f"faulthandler enabled, crashes will be logged to {crash_log.name}")
    except Exception as e:
        logging.warning(f"Could not enable faulthandler: {e}")

    # Determine the effective working directory for the terminal mode.
    # If a path is passed as a non-empty argument, use it; otherwise, use the original CWD.
    # This handles the case where `logos.bat` passes `""` as an argument.
    working_directory = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else original_cwd
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Effective working directory: {working_directory}")

    # --- Set AppUserModelID for Windows Taskbar Icon ---
    # This must be done BEFORE the QApplication is instantiated to ensure Windows
    # associates the correct icon with the process from the very beginning.
    if sys.platform == "win32":
        import ctypes
        myappid = 'Aigrator.Logos.Assistant.1' # arbitrary unique string
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    # --- End AppUserModelID ---

    app = QApplication(sys.argv)

    local_server = None
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] app initialized")
    # --- Single Instance Check ---
    if settings_manager.get("prevent_multiple_instances", True):
        server_name = "Logos-Assistant-Lock"
        socket = QLocalSocket()
        socket.connectToServer(server_name)

        if socket.waitForConnected(250):
            logging.info("Another instance is already running. Signaling it to show and exiting.")
            # Send a signal to the running instance to show its window
            socket.write(b'show_window')
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()
            sys.exit(0)
        else:
            # This is the first instance, so we create the server
            local_server = QLocalServer()
            # Clean up any leftover sockets from a previous crash
            local_server.removeServer(server_name)
            local_server.listen(server_name)
    # --- End Single Instance Check ---

    # --- First Run Setup ---
    # Check if settings exist. If not, this is the first run.
    if not os.path.exists(settings_manager.SETTINGS_FILE):
        # 1. Detect and set the system theme
        try:
            palette = app.palette()
            window_color = palette.color(palette.ColorGroup.Normal, palette.ColorRole.Window)
            detected_theme = "dark" if window_color.lightness() < 128 else "light"
            logging.info(f"First run: Detected system theme is '{detected_theme}'.")
            settings_manager.set("theme", detected_theme)
        except Exception as e:
            logging.warning(f"Could not detect system theme, defaulting to 'dark'. Error: {e}")
            settings_manager.set("theme", "dark")

        # 2. Run the setup wizard to get API keys, etc.
        if not run_setup_wizard(settings_manager):
            sys.exit(0) # User cancelled setup
    # --- End First Run Setup ---

    try:
        from computer.tool_registry import ToolRegistry
        # ToolRegistry now gets its paths from the settings_manager
        ToolRegistry(settings_manager=settings_manager).generate_all()
    except Exception:
        logging.debug("ToolRegistry.generate_all() failed during main startup", exc_info=True)

    thread_pool = ThreadPool()
    app.aboutToQuit.connect(thread_pool.cleanup)
    controller = AppController(app, settings_manager, thread_pool, local_server, working_directory)

    # Apply the theme using the new manager
    saved_theme = controller.settings_manager.get("theme", "dark")
    controller.theme_manager.apply_theme(saved_theme)

    controller.run()

    sys.exit(app.exec())

if __name__ == "__main__":
    # Minimal top-level code to start the main function
    main()


