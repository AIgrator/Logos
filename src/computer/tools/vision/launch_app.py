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

    allowed_apps = kwargs.get('allowed_apps_list')
    launcher = kwargs.get('app_launcher')

    if allowed_apps is None or launcher is None:
        raise ValueError("launch_app requires 'allowed_apps_list' and 'app_launcher' to be provided internally.")

    try:
        logging.info(f"Attempting to launch application with exact name: '{app_name}'")

        if not allowed_apps:
            raise RuntimeError("The list of allowed applications is empty.")

        matched_app = next((app for app in allowed_apps if app['name'].lower() == app_name.lower()), None)

        if not matched_app:
            available_apps = [app['name'] for app in allowed_apps]
            raise FileNotFoundError(f"Application '{app_name}' not found in the allowed list. Available apps: {available_apps}")

        launcher.launch(matched_app)
        logging.info(f"Successfully issued launch command for '{app_name}'.")

    except Exception as e:
        logging.error(f"Error launching application '{app_name}': {e}", exc_info=True)
        raise
