from src.computer.tools.terminal.fs.copy_files import copy_files
from src.computer.tools.terminal.fs.create_directory import create_directory
from src.computer.tools.terminal.fs.delete_files import delete_files
from src.computer.tools.terminal.fs.find_files import find_files
from src.computer.tools.terminal.fs.list_directory import list_directory
from src.computer.tools.terminal.fs.move_files import move_files
from src.computer.tools.terminal.fs.path_exists import path_exists
from src.computer.tools.terminal.fs.read_files import read_files
from src.computer.tools.terminal.fs.replace import replace
from src.computer.tools.terminal.fs.replace_many import replace_many
from src.computer.tools.terminal.fs.search_file_content import search_file_content
from src.computer.tools.terminal.fs.write_file import write_file
from src.computer.tools.terminal.system.execute_shell import execute_shell
from src.computer.tools.terminal.system.poll_shell_job import poll_shell_job
from src.computer.tools.terminal.system.stop_shell_job import stop_shell_job
from src.computer.tools.terminal.system.wait_seconds import wait_seconds
from src.computer.tools.web.download_file import download_file
from src.computer.tools.web.web_fetchs import web_fetchs
from src.computer.tools.web.web_search import web_search
from src.computer.tools.common.communicate_to_user import communicate_to_user
from src.computer.tools.common.display_message import display_message
from src.computer.tools.common.finish import finish
from src.computer.tools.common.get_clipboard_content import get_clipboard_content

TOOLS_METADATA = {
    "copy_files": copy_files,
    "create_directory": create_directory,
    "delete_files": delete_files,
    "find_files": find_files,
    "list_directory": list_directory,
    "move_files": move_files,
    "path_exists": path_exists,
    "read_files": read_files,
    "replace": replace,
    "replace_many": replace_many,
    "search_file_content": search_file_content,
    "write_file": write_file,
    "execute_shell": execute_shell,
    "poll_shell_job": poll_shell_job,
    "stop_shell_job": stop_shell_job,
    "wait_seconds": wait_seconds,
    "download_file": download_file,
    "web_fetchs": web_fetchs,
    "web_search": web_search,
    "communicate_to_user": communicate_to_user,
    "display_message": display_message,
    "finish": finish,
    "get_clipboard_content": get_clipboard_content,
}

def get_tools_metadata():
    return TOOLS_METADATA
