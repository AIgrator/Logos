from src.computer.computer import tool_meta
from context import skills


@tool_meta(
    tool={
        "name": "use_skill",
        "description": (
            "Loads the full instructions of an installed skill by name. Call it "
            "when the task matches one of the skills listed in Available Skills; "
            "the result contains the instructions, the skill directory and the "
            "paths of auxiliary files."),
        "arguments": {
            "name": {
                "type": "STRING",
                "description": "Skill name exactly as listed in Available Skills.",
                "required": True,
            }
        },
        "returns": {
            "type": "STRING",
            "description": "The skill instructions and its auxiliary file paths.",
        },
    },
    flags={"platform": "any", "mode": "any"},
)
def use_skill(self, name: str = "", **kwargs) -> str:
    """Loads an Agent Skill installed for the active project or the user."""
    project_root = getattr(self, "working_directory", "") or ""
    return skills.load(project_root, name)
