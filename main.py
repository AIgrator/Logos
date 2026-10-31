"""
Main entry point of the application.
"""
import sys
import os
import shutil
import time
import logging

_BUNDLED_DIRS = ("assets", "prompts")


def _materialize_bundle_resources(bundle_dir, app_dir):
    """Copies read-only resources out of a one-file bundle into the data folder.

    The bundle unpacks to a temp folder that is deleted on exit, while prompts
    and icons are resolved relative to the working directory. The executable's
    timestamp and size stamp the copy, so an upgrade refreshes it and a normal
    launch only reads the stamp.
    """
    stamp_path = os.path.join(app_dir, ".logos-resources")
    try:
        stat = os.stat(sys.executable)
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
        try:
            with open(stamp_path, encoding="utf-8") as f:
                if f.read().strip() == stamp:
                    return
        except OSError:
            pass
        for name in _BUNDLED_DIRS:
            src = os.path.join(bundle_dir, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(app_dir, name), dirs_exist_ok=True)
        with open(stamp_path, "w", encoding="utf-8") as f:
            f.write(stamp)
    except OSError:
        logging.warning("Could not copy bundled resources next to the executable", exc_info=True)


def main():
    """Main function to run the application."""
    # Set the current working directory: the script's directory from source, or
    # the writable per-user data folder for a one-file build (its install
    # folder may be read-only and its unpack folder is temporary). All relative
    # resource and data paths resolve from there.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        from app_paths import data_root
        app_dir = data_root()
        try:
            os.makedirs(app_dir, exist_ok=True)
            os.chdir(app_dir)
        except OSError:
            app_dir = os.path.dirname(sys.executable)
            os.chdir(app_dir)
        _materialize_bundle_resources(script_dir, app_dir)
    else:
        os.chdir(script_dir)

    # Print an immediate startup message so the user knows the app is loading
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Application starting, please wait for modules to load...")
    _start_t = time.perf_counter()

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
        """Closes the startup splash, if it is still up."""
        nonlocal qt_splash
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

    # Startup splash, owned by THIS process. Because it is one of our own
    # windows (not a helper process spawned by the .bat), it can never be
    # orphaned: if anything fails — a bad import, a crash, a hard exit — the
    # OS destroys it together with the process. On top of that it is closed
    # explicitly on every normal exit path (_close_splashes) and by the
    # aboutToQuit backstop below.
    if "--run-setup" not in sys.argv:
        try:
            from PySide6.QtWidgets import QSplashScreen
            from PySide6.QtGui import QPixmap
            from PySide6.QtCore import Qt
            pix = QPixmap(os.path.join(script_dir, "assets", "ico.png")).scaled(
                256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            qt_splash = QSplashScreen(pix)
            qt_splash.show()
            qt_splash.showMessage("Starting Logos…", Qt.AlignBottom | Qt.AlignHCenter)
            app.processEvents()
            # Backstop: whenever the event loop ends, make sure it is gone.
            app.aboutToQuit.connect(_close_splashes)
        except Exception as e:
            print(f"qt splash failed: {e}", flush=True)
            qt_splash = None

    # Add the 'src' directory to the Python path. A frozen build keeps its
    # modules in the archive under their canonical names; adding the bundle's
    # src folder would remap top-level imports to the src.* entries and miss
    # their submodules.
    if not getattr(sys, 'frozen', False):
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
    from project_manager import ProjectManager
    from initial_setup import run_setup_wizard

    # --- Main Execution ---
    settings_manager = SettingsManager()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] settings_manager initialized")
    _phase("SettingsManager done")

    # --- Setup-only mode ---
    if '--run-setup' in sys.argv:
        # The setup wizard handles its own theme application.
        # (No startup splash in this mode — it is a quick settings window.)
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

    # Route Qt's own diagnostics into the log. A native Qt fatal calls abort()
    # (0xc0000409) and would otherwise only print to a stderr nobody sees in an
    # EXE build — capturing it here is the only clue left after such a crash.
    # Must run after QApplication exists and after logging is configured.
    try:
        from PySide6.QtCore import qInstallMessageHandler

        _qt_levels = {
            0: logging.DEBUG,     # QtDebugMsg
            1: logging.WARNING,   # QtWarningMsg
            2: logging.CRITICAL,  # QtCriticalMsg
            3: logging.CRITICAL,  # QtFatalMsg
            4: logging.INFO,      # QtInfoMsg
        }

        def _qt_message_handler(mode, context, message):
            try:
                code = int(getattr(mode, "value", mode))
                level = _qt_levels.get(code, logging.WARNING)
                loc = ""
                if context is not None and getattr(context, "file", None):
                    loc = f" ({context.file}:{context.line})"
                logging.getLogger("Qt").log(level, f"[Qt] {message}{loc}")
                if code >= 2:  # critical/fatal: flush so it survives abort()
                    for h in logging.getLogger().handlers:
                        try:
                            h.flush()
                        except Exception:
                            pass
            except Exception:
                pass

        qInstallMessageHandler(_qt_message_handler)
        logging.info("Qt message handler installed (Qt fatal messages -> app.log)")
    except Exception as e:
        logging.warning(f"Could not install Qt message handler: {e}")

    # Worker-thread deaths are invisible to sys.excepthook; give threads one too.
    try:
        import threading as _threading

        def _log_thread_uncaught(args):
            try:
                name = args.thread.name if args.thread is not None else "?"
                logging.critical(
                    f"UNCAUGHT EXCEPTION IN THREAD '{name}'",
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            except Exception:
                pass

        _threading.excepthook = _log_thread_uncaught
        logging.info("threading.excepthook installed (worker-thread crashes -> app.log)")
    except Exception as e:
        logging.warning(f"Could not install threading.excepthook: {e}")

    # An explicit path argument (e.g. from logos.bat) is treated as the
    # project to open; otherwise the remembered active project is used.
    project_arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else ""

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
        # Splashes are TopMost — close them so they don't cover the wizard.
        _close_splashes()
        if not run_setup_wizard(settings_manager):
            sys.exit(0) # User cancelled setup
    # --- End First Run Setup ---

    # --- Active project resolution ---
    # Every project owns a .logos folder holding its chats, sessions and
    # context; the project root is also the terminal/browser working directory
    # and the root of the file index.
    project_manager = ProjectManager(settings_manager)
    project_root = project_manager.resolve_startup_project(project_arg)
    if project_root:
        try:
            project_manager.open_project(project_root)
        except Exception as e:
            logging.warning(f"Could not open remembered project '{project_root}': {e}")
            project_root = ""
    working_directory = project_manager.get_active_root() if project_root else ""
    if working_directory:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Active project: {working_directory}")
        logging.info(f"Active project: {working_directory}")
    else:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] No active project; showing home screen.")
        logging.info("No active project; showing home screen.")
    # --- End Active project resolution ---

    try:
        from computer.tool_registry import ToolRegistry
        # ToolRegistry now gets its paths from the settings_manager
        ToolRegistry(settings_manager=settings_manager).generate_all()
        _phase("ToolRegistry.generate_all done")
    except Exception:
        logging.debug("ToolRegistry.generate_all() failed during main startup", exc_info=True)

    controller = AppController(app, settings_manager, local_server, working_directory,
                               project_manager=project_manager)
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

    # Warm provider SDK imports off the GUI thread once the main window is up.
    # Without this, the first Settings open imports every provider SDK on the
    # main thread (seconds of freeze); the window itself no longer waits for it.
    try:
        from PySide6.QtCore import QTimer
        from llm import start_provider_warmup
        QTimer.singleShot(1500, start_provider_warmup)
    except Exception:
        logging.debug("Provider warm-up scheduling failed", exc_info=True)

    sys.exit(app.exec())

if __name__ == "__main__":
    # Minimal top-level code to start the main function
    main()


