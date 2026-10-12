from enum import Enum, auto

class UIState(Enum):
    """States for the UI."""
    IDLE = auto()
    THINKING = auto()
    VISION = auto()

class ControllerState(Enum):
    """High-level states for the AppController."""
    IDLE = auto()
    CHATTING = auto()
    TERMINAL = auto()
    VISION = auto()
    BROWSER = auto()

class VisionState(Enum):
    IDLE = auto()
    PLANNING = auto()
    WAITING_FOR_APPROVAL = auto()
    GROUNDING_ACTION = auto()
    EXECUTING = auto()
    EVALUATING = auto()
    PAUSED = auto()
    PAUSED_ON_ERROR = auto()
    WAITING_FOR_ELEMENT = auto()
    FINDING_WAIT_ORIGIN = auto()
    EXPLAINING_ERROR = auto()
