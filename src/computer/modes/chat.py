from src.computer.tools.web.web_fetchs import web_fetchs
from src.computer.tools.web.web_search import web_search


TOOLS_METADATA = {
    "web_fetchs": web_fetchs,
    "web_search": web_search,
}

def get_tools_metadata():
    return TOOLS_METADATA
