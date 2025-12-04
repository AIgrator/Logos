"""
Cross-platform application scanner using the 'app-scanner' library.
"""
import json
import logging
import os
import platform
from typing import Any

from settings_manager import SettingsManager

try:
    import app_scanner
except ImportError:
    app_scanner = None

class AppScanner:
    """
    A class to scan for installed applications across Windows, macOS, and Linux
    using the app-scanner library and cache the results.
    """
    def __init__(self, settings_manager: SettingsManager = None):
        """
        Initializes the scanner.
        """
        super().__init__()
        self.settings_manager = settings_manager
        self.CACHE_DIR = "cache"
        self.CACHE_FILE = os.path.join(self.CACHE_DIR, "app_cache.json")

    @staticmethod
    def _get_unique_app_id(app: dict[str, Any]) -> str:
        """
        Generates a stable, unique identifier for an application based on its properties.
        """
        if platform.system() == "Windows":
            # On Windows, appid is the most reliable unique identifier.
            return app.get("appid", app["name"])
        elif platform.system() == "Darwin":
            # On macOS, the path to the .app bundle is unique.
            return app.get("path", app["name"])
        else: # Linux and other OSes
            # On Linux, the path to the executable or .desktop file is unique.
            return app.get("path", app["name"])

    def scan_and_cache(self) -> list[dict[str, Any]]:
        """
        Scans for applications using the app-scanner library, adds a unique ID,
        and caches the results to a JSON file.
        """
        if app_scanner is None:
            logging.warning("The 'app-scanner' library is not installed. Application scanning is disabled.")
            return []

        try:
            logging.info("Starting application scan...")
            apps_data = app_scanner.get_installed_apps()
            logging.info(f"Found {len(apps_data)} applications.")

            # Add a unique identifier to each app dictionary for internal use
            for app in apps_data:
                app["unique_id"] = self._get_unique_app_id(app)

            # Cache the results
            os.makedirs(self.CACHE_DIR, exist_ok=True)
            with open(self.CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(apps_data, f, indent=4, ensure_ascii=False)
            logging.info(f"Successfully cached {len(apps_data)} applications to {self.CACHE_FILE}")

            return apps_data

        except Exception as e:
            logging.error(f"An error occurred during the scan and cache process: {e}", exc_info=True)
            return [] # Return empty list on error

    def load_from_cache(self) -> list[dict[str, Any]]:
        """
        Loads the list of applications from the cache file.
        """
        if not os.path.exists(self.CACHE_FILE):
            logging.warning("Cache file not found. Run scan_and_cache() first.")
            return []
        try:
            with open(self.CACHE_FILE, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logging.error(f"Failed to read app cache at {self.CACHE_FILE}: {e}", exc_info=True)
            return []

    def get_cached_or_scanned_apps(self) -> list[dict[str, Any]]:
        """
        Returns the list of installed applications by reading the cache if present
        and valid, otherwise runs a scan and writes the cache.
        """
        if os.path.exists(self.CACHE_FILE):
            try:
                apps = self.load_from_cache()
                if isinstance(apps, list) and len(apps) > 0:
                    return apps
            except Exception as e:
                logging.error(f"Failed to read app cache, falling back to scan: {e}", exc_info=True)

        try:
            return self.scan_and_cache()
        except Exception as e:
            logging.error(f"Failed to scan for installed apps: {e}", exc_info=True)
            return []

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    scanner = AppScanner()
    apps = scanner.scan_and_cache()
    if apps:
        print(f"Scan complete. Found {len(apps)} applications.")
        # print("First 5 apps found:")
        # for app in apps[:5]:
        #     print(f"- {app.get('name')} (ID: {app.get('unique_id')})")

