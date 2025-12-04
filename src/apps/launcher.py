# src/apps/launcher.py
import logging
import subprocess
import platform

class CrossPlatformAppLauncher:
    """
    Handles launching applications in a cross-platform manner.
    """

    def launch(self, app_info: dict):
        """
        Launches an application based on its info dictionary.

        :param app_info: A dictionary containing application details,
                         expected to have 'appid' on Windows and 'path' on macOS/Linux.
        """
        system = platform.system()
        app_name = app_info.get('name', 'Unknown App')

        try:
            if system == "Windows":
                appid = app_info.get("appid")
                if not appid:
                    raise ValueError(f"Missing 'appid' for launching '{app_name}' on Windows.")
                command = rf"shell:Appsfolder\{appid}"
                logging.info(f"Launching on Windows with command: explorer.exe {command}")
                subprocess.Popen(['explorer.exe', command])

            elif system == "Darwin": # macOS
                path = app_info.get("path")
                if not path:
                    raise ValueError(f"Missing 'path' for launching '{app_name}' on macOS.")
                logging.info(f"Launching on macOS with command: open '{path}'")
                subprocess.Popen(['open', path])

            elif system == "Linux":
                desktop_id = app_info.get("desktop_id")
                wm_class = app_info.get("wm_class")
                path = app_info.get("path")

                # --- Attempt to launch with different identifiers ---
                # We try desktop_id first, then wm_class, as wm_class can sometimes
                # be a more reliable pointer to the .desktop file name, especially for Snap apps.
                ids_to_try = []
                if desktop_id:
                    ids_to_try.append(desktop_id)
                if wm_class and wm_class not in ids_to_try:
                    ids_to_try.append(wm_class)

                if ids_to_try:
                    for app_id in ids_to_try:
                        try:
                            command = ['gtk-launch', app_id]
                            logging.info(f"Attempting to launch on Linux via gtk-launch with ID: '{app_id}'")
                            # Use subprocess.run to wait for the command and check for errors.
                            # It will throw CalledProcessError on failure.
                            # We redirect stdout to DEVNULL to prevent the process from hanging
                            # while waiting for the pipe to close, which happens with some apps.
                            # stderr is still captured for error logging.
                            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                            # If launch is successful, exit the function
                            logging.info(f"Successfully launched '{app_name}' using gtk-launch with ID '{app_id}'.")
                            return
                        except FileNotFoundError:
                            logging.warning("gtk-launch command not found. Cannot use desktop_id/wm_class. Falling back to direct path execution.")
                            # If gtk-launch is not found, no point in trying other IDs.
                            break
                        except subprocess.CalledProcessError as e:
                            # This is expected if the ID is wrong. We'll log it and try the next one.
                            error_message = e.stderr.decode().strip() if e.stderr else "(no stderr)"
                            logging.warning(f"gtk-launch failed for ID '{app_id}': {error_message}")
                        except Exception as e:
                            logging.error(f"An unexpected error occurred with gtk-launch for ID '{app_id}': {e}")

                    logging.warning(f"All gtk-launch attempts failed for '{app_name}'. Falling back to direct path execution.")

                # --- Fallback to direct execution via path ---
                if not path:
                    raise ValueError(f"Missing 'path' for launching '{app_name}' on Linux after all launch attempts failed.")

                import shlex
                command_to_run = shlex.split(path)

                logging.info(f"Launching on Linux directly with command: {command_to_run}")
                subprocess.Popen(command_to_run)

            else:
                logging.warning(f"Unsupported operating system for launching applications: {system}")

        except FileNotFoundError as e:
            logging.error(f"Command not found while trying to launch '{app_name}': {e}")
            raise
        except Exception as e:
            logging.error(f"Failed to launch application '{app_name}'. Error: {e}")
            raise
