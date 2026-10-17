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
    _start_t = time.perf_counter()

    def _native_splash_pid_file():
        import tempfile
        return os.path.join(tempfile.gettempdir(), "logos_splash.pid")

    def _close_native_splash():
        """Kills the transparent pre-splash started by bin/logos.bat (if any).

        Windows-only by construction (the launcher is a .bat); on other
        systems this is a no-op so startup stays cross-platform.
        """
        if sys.platform != "win32":
            return
        try:
            import subprocess
            pid_file = _native_splash_pid_file()
            if not os.path.exists(pid_file):
                return
            with open(pid_file) as f:
                pid = int(f.read().strip())
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout.lower()
            if "powershell" in out or "pwsh" in out:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True, timeout=10,
                )
            try:
                os.remove(pid_file)
            except Exception:
                pass
        except Exception:
            pass

    qt_splash = None

    def _phase(name):
        line = f"[+{time.perf_counter() - _start_t:.1f}s] {name}"
        print(line, flush=True)
        try:
            logging.info(f"startup {line}")
        except Exception:
            pass
        if qt_splash is not None:
            try:
                from PySide6.QtCore import Qt
                qt_splash.showMessage(name, Qt.AlignBottom | Qt.AlignHCenter)
            except Exception:
                pass

    def _close_splashes():
        """Closes whichever splash is up (native pre-splash and/or Qt fallback)."""
        nonlocal qt_splash
        _close_native_splash()
        if qt_splash is not None:
            try:
                qt_splash.close()
            except Exception:
                pass
            qt_splash = None

    # Now perform the heavy imports
    # import shutil
    # import threading
    from PySide6.QtWidgets import QApplication
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    _phase("Qt imports done")

    # --- Set AppUserModelID for Windows Taskbar Icon ---
    # This must be done BEFORE the QApplication is instantiated to ensure Windows
    # associates the correct icon with the process from the very beginning.
    if sys.platform == "win32":
        import ctypes
        myappid = 'Aigrator.Logos.Assistant.1' # arbitrary unique string
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    # --- End AppUserModelID ---

    # --- DPI awareness (MUST precede QApplication) ---
    # Without this the process gets a virtualized 1536x864 screen at 125%
    # scaling while screenshots are physical 1920x1080: every click beyond
    # the virtual region is silently clamped elsewhere. Per-Monitor V2 makes
    # coordinates physical everywhere (screenshots == clicks == cursor).
    if sys.platform == "win32":
        try:
            import ctypes as _ct
            try:
                _ct.windll.shcore.SetProcessDpiAwareness(2)
                print("DPI awareness: Per-Monitor V2", flush=True)
            except Exception:
                _ct.windll.user32.SetProcessDPIAware()
                print("DPI awareness: System (fallback)", flush=True)
        except Exception as e:
            print(f"DPI awareness setup failed: {e}", flush=True)
    # --- End DPI awareness ---

    # Create the application EARLY so a splash screen covers the rest of
    # startup (settings, controller). Without it the user stares at nothing
    # for seconds — and EXE builds have no console at all.
    app = QApplication.instance() or QApplication(sys.argv)
    _phase("QApplication created")

    # Add the 'src' directory to the Python path
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] before ModelConfigManager")
    from llm.model_config_manager import ModelConfigManager
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] after ModelConfigManager")
    # Initialize the model configuration before any other components are created.
    # This ensures that all parts of the app see the same, up-to-date model list.
    ModelConfigManager.initialize()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ModelConfigManager.initialized")
    _phase("ModelConfigManager.initialize done")
    import logging_config
    from controller import AppController
    from settings_manager import SettingsManager
    from initial_setup import run_setup_wizard

    # --- Main Execution ---
    settings_manager = SettingsManager()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] settings_manager initialized")
    _phase("SettingsManager done")

    # --- Setup-only mode ---
    if '--run-setup' in sys.argv:
        # The setup wizard now handles its own theme application
        # (app instance already exists — created early for the splash path)
        _close_native_splash()
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

    # Last-resort net for silent deaths: any uncaught main-thread exception
    # (including ones Qt would otherwise swallow) lands in app.log with a
    # traceback instead of vanishing with the process.
    def _log_uncaught(exc_type, exc_value, exc_tb):
        try:
            logging.critical("UNCAUGHT EXCEPTION — process may die",
                             exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            pass
        try:
            _orig_excepthook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    try:
        import sys as _sys
        _orig_excepthook = _sys.excepthook
        _sys.excepthook = _log_uncaught
    except Exception:
        pass

    # Determine the effective working directory for the terminal mode.
    # If a path is passed as a non-empty argument, use it; otherwise, use the original CWD.
    # This handles the case where `logos.bat` passes `""` as an argument.
    working_directory = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else original_cwd
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Effective working directory: {working_directory}")

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

    # Splash policy: exactly one. On Windows the transparent pre-splash from
    # bin/logos.bat is already up (it appears before the interpreter) and
    # stays for the whole startup. Everywhere else — Linux/macOS, or a direct
    # `python main.py` run — show the Qt fallback instead.
    if not os.path.exists(_native_splash_pid_file()):
        try:
            from PySide6.QtWidgets import QSplashScreen
            from PySide6.QtGui import QPixmap
            from PySide6.QtCore import Qt
            pix = QPixmap("assets/ico.png").scaled(
                256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            qt_splash = QSplashScreen(pix)
            qt_splash.show()
            qt_splash.showMessage("Starting Logos…", Qt.AlignBottom | Qt.AlignHCenter)
            app.processEvents()
        except Exception as e:
            print(f"qt splash failed: {e}", flush=True)
            qt_splash = None

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
        # Splashes are TopMost — close them so they don't cover the wizard.
        _close_splashes()
        if not run_setup_wizard(settings_manager):
            sys.exit(0) # User cancelled setup
    # --- End First Run Setup ---

    try:
        from computer.tool_registry import ToolRegistry
        # ToolRegistry now gets its paths from the settings_manager
        ToolRegistry(settings_manager=settings_manager).generate_all()
        _phase("ToolRegistry.generate_all done")
    except Exception:
        logging.debug("ToolRegistry.generate_all() failed during main startup", exc_info=True)

    controller = AppController(app, settings_manager, local_server, working_directory)
    _phase("AppController created")

    # Apply the theme using the new manager
    saved_theme = controller.settings_manager.get("theme", "dark")
    controller.theme_manager.apply_theme(saved_theme)

    controller.run()
    _phase("controller.run done (window should be visible)")

    # Splashes have done their job (the EXE build will use an animated GIF).
    if qt_splash is not None:
        try:
            main_window = getattr(getattr(controller, "ui", None), "main_window", None)
            if main_window is not None:
                qt_splash.finish(main_window)
            else:
                qt_splash.close()
        except Exception:
            pass
    _close_splashes()

    sys.exit(app.exec())

if __name__ == "__main__":
    # Minimal top-level code to start the main function
    main()


