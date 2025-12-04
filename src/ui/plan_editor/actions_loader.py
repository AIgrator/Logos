"""
Loads and caches UI tool schemas from the JSON file.
"""
from copy import deepcopy
import json
import os

# Module-level cache for actions
_ACTIONS_CACHE = None

def _get_project_root():
    """Calculates the project root directory from the current file's location."""
    # __file__ is in src/ui/plan_editor/actions_loader.py
    # The project root is 4 levels up.
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def load_actions(force_reload=False):
    """
    Loads action schemas from the ui_tools.json file.
    The result is cached to avoid repeated file I/O.

    Args:
        force_reload (bool): If True, reloads the data from the file, ignoring the cache.

    Returns:
        list: A list of action schema dictionaries.
    """
    global _ACTIONS_CACHE
    if _ACTIONS_CACHE is not None and not force_reload:
        return _ACTIONS_CACHE

    try:
        base_dir = _get_project_root()
        # The canonical tools file is in the 'cache' directory for the application.
        tools_file = os.path.join(base_dir, 'cache', 'vision', 'ui_tools.json')

        if os.path.exists(tools_file):
            with open(tools_file, encoding='utf-8') as f:
                _ACTIONS_CACHE = json.load(f)
        else:
            print(f"Warning: Tools file not found at {tools_file}")
            _ACTIONS_CACHE = []
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error loading actions file: {e}")
        _ACTIONS_CACHE = []

    return _ACTIONS_CACHE


_APPS_CACHE = None

def load_allowed_apps(force_reload=False):
    """
    Loads the list of allowed applications from the main settings.json file.
    """
    global _APPS_CACHE
    if _APPS_CACHE is not None and not force_reload:
        return _APPS_CACHE

    try:
        base_dir = _get_project_root()
        settings_file = os.path.join(base_dir, 'settings.json')

        if os.path.exists(settings_file):
            with open(settings_file, encoding='utf-8') as f:
                settings_data = json.load(f)
                # Get the 'allowed_apps' list from the settings data
                apps_data = settings_data.get("allowed_apps", [])
                # Extract just the 'name' from each app dictionary
                _APPS_CACHE = [app.get("name", "") for app in apps_data if app.get("name")]
        else:
            print(f"Warning: settings.json file not found at {settings_file}")
            _APPS_CACHE = []
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error loading allowed apps from settings.json: {e}")
        _APPS_CACHE = []

    return _APPS_CACHE


def get_action_schema_by_name(name: str):
    """
    Retrieves a deep copy of a single action schema by its name.

    Args:
        name (str): The name of the action to find.

    Returns:
        dict: A copy of the action schema, or a default schema if not found.
    """
    actions = load_actions()
    for action in actions:
        if action.get("name") == name:
            return deepcopy(action)

    # Return a default structure if the action is not found
    return {"name": name, "description": "Unknown action", "arguments": {}}
