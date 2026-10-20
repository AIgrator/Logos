import logging

from src.computer.computer import tool_meta

@tool_meta(
    tool={
        "name": "launch_app",
        "description": "Launches an application using its exact name.",
        "arguments": {
            "description": {"type": "STRING", "description": "A description of the launch action (e.g., 'Launching Notepad.')", "required": True},
            "app_name": {"type": "STRING", "description": "The exact name of the application to launch. Must be one of the names from the 'Installed Applications' list provided in the context, e.g., 'Notepad'.", "required": True}
        }
    },
    flags={"platform": "any", "mode": "vision"}
)
def launch_app(self, **kwargs):
    app_name = kwargs.get('app_name')
    if not app_name:
        raise ValueError("Action 'launch_app' failed: Missing required 'app_name' argument. Example: {'name': 'launch_app', 'arguments': {'description': 'Open calculator', 'app_name': 'Calculator'}}")

    apps = list(kwargs.get('allowed_apps_list') or [])
    launcher = kwargs.get('app_launcher')
    restrict = False
    try:
        config = (self.settings_manager.get("vision", {}) or {}
                  if self.settings_manager else {})
        restrict = bool(config.get("restrict_to_allowed_apps", False))
    except Exception:
        restrict = False

    if launcher is None:
        from src.apps.launcher import CrossPlatformAppLauncher
        launcher = CrossPlatformAppLauncher()

    if not restrict:
        try:
            from src.apps.installed_apps import AppScanner
            apps = AppScanner(self.settings_manager).get_cached_or_scanned_apps()
        except Exception as e:
            logging.warning(f"Could not load the application catalog: {e}")

    try:
        logging.info(f"Attempting to launch application with exact name: '{app_name}'")

        if not apps:
            raise RuntimeError(
                "No applications are available. Select apps in Applications "
                "or scan for installed applications.")

        matched_app = next((app for app in apps if app['name'].lower() == app_name.lower()), None)

        if not matched_app:
            available_apps = [app['name'] for app in apps]
            raise FileNotFoundError(f"Application '{app_name}' not found. Available apps: {available_apps}")

        launcher.launch(matched_app)
        logging.info(f"Successfully issued launch command for '{app_name}'.")

    except Exception as e:
        logging.error(f"Error launching application '{app_name}': {e}", exc_info=True)
        raise
