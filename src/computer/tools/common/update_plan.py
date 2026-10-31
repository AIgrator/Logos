from src.computer.computer import tool_meta


@tool_meta(
    tool={
        "name": "update_plan",
        "description": (
            "Sets or refreshes the short visible plan of the current task. "
            "Call it once near the start with 3-6 steps, then call it again "
            "whenever a step starts or finishes so the plan stays current. "
            "It is not executed; it only updates the plan shown above the steps."),
        "arguments": {
            "steps": {
                "type": "ARRAY",
                "description": "The complete plan, in order. Send every step each time, with its current status.",
                "required": True,
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "text": {
                            "type": "STRING",
                            "description": "Short imperative step, in the user's language.",
                            "required": True,
                        },
                        "status": {
                            "type": "STRING",
                            "description": "Current state of this step.",
                            "enum": ["pending", "in_progress", "done"],
                            "required": True,
                        },
                    },
                },
            },
        },
    },
    flags={"platform": "any", "mode": "any"},
)
def update_plan(self, steps=None, **kwargs):
    """Handled by the agent loop; never executed as a computer action."""
    return None
