import logging
import subprocess
import sys

from src.computer.computer import tool_meta
from src.computer.tools.constants import SHELL_COMMAND_EXAMPLE

@tool_meta(
    tool={
        "name": "execute_shell",
        "description": "Executes a command in the system's default shell (cmd.exe on Windows, sh/bash elsewhere). Write plain shell syntax for THAT shell: on Windows use cmd commands (mkdir, dir, echo) or 'powershell -Command \"...\"' for cmdlets. Never hardcode user profile paths (C:\\Users\\...): use portable tokens {DESKTOP}, {HOME}, {WORKDIR}, {DOCUMENTS}, {DOWNLOADS}, expanded by the executor. Returns stdout, stderr and exit code.",
        "arguments": {
            "command": {"type": "STRING", "description": f"The command line to execute (e.g., {SHELL_COMMAND_EXAMPLE}).", "required": True}
        },
        "returns": {
            "type": "OBJECT",
            "description": "A dictionary containing the command's output.",
            "properties": {
                "stdout": {"type": "STRING", "description": "The standard output of the command."},
                "stderr": {"type": "STRING", "description": "The standard error output. Empty if no errors."},
                "exit_code": {"type": "INTEGER", "description": "The exit code of the process. 0 typically means success."}
            }
        }
    },
    flags={"platform": "any", "mode": "terminal"}
)
def execute_shell(self, command: str) -> dict:
    """
    Executes a shell command and returns its output.
    """
    if not command:
        raise ValueError("Action 'execute_shell' failed: Missing required 'command' argument.")

    try:
        logging.info(f"Executing shell command: {command}")

        # On Windows, a `powershell -Command "..."` wrapper routed through
        # cmd.exe mangles nested quotes (the inner command can degrade into
        # a quoted string that PowerShell merely echoes, exiting 0 while
        # doing nothing). Run such commands via powershell.exe directly.
        lowered = command.strip()
        ps_prefixes = ("powershell -command", "powershell.exe -command", "pwsh -command", "pwsh.exe -command")
        inner = None
        for prefix in ps_prefixes:
            if lowered.lower().startswith(prefix):
                inner = command.strip()[len(prefix):].strip()
                # Strip one layer of surrounding quotes if present.
                if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in ('"', "'"):
                    inner = inner[1:-1]
                break

        if inner is not None:
            # Make cmdlet errors terminating: by default powershell.exe
            # returns exit code 0 even when a cmdlet fails (e.g. removing
            # a non-existent path), which would fake a success. With
            # $ErrorActionPreference='Stop' failures surface as exit code 1
            # and the textual contract check catches them.
            inner = "$ErrorActionPreference = 'Stop'; " + inner
            process = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command', inner],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                encoding='utf-8',
                errors='ignore',
                cwd=self.working_directory
            )
        elif sys.platform == "win32":
            # Using shell=True on Windows is common, but splitting the command
            # with cmd /c is a bit safer.
            # Specify 'oem' encoding for Windows console output
            process = subprocess.run(
                ['cmd', '/c', command],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                encoding='oem',
                errors='ignore',
                cwd=self.working_directory
            )
        else:
            # For Unix-like systems, it's safer to let the shell handle the command string.
            process = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60, check=False, cwd=self.working_directory)

        stdout = process.stdout
        stderr = process.stderr
        exit_code = process.returncode

        logging.info(f"Command finished with exit code {exit_code}.")

        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code
        }

    except FileNotFoundError:
        logging.error(f"Command '{command.split()[0]}' not found.")
        return {
            "stdout": "",
            "stderr": f"Error: Command not found: {command.split()[0]}",
            "exit_code": 127  # Standard exit code for command not found
        }
    except subprocess.TimeoutExpired:
        logging.error(f"Command '{command}' timed out.")
        return {
            "stdout": "",
            "stderr": "Error: Command timed out after 60 seconds.",
            "exit_code": -1
        }
    except Exception as e:
        logging.error(f"An unexpected error occurred while executing command '{command}': {e}", exc_info=True)
        return {
            "stdout": "",
            "stderr": f"An unexpected error occurred: {e}",
            "exit_code": -1
        }
