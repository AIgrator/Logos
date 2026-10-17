from enum import Enum, auto

class ControllerState(Enum):
    """High-level states for the AppController."""
    IDLE = auto()
    CHATTING = auto()
    TERMINAL = auto()
    VISION = auto()
    BROWSER = auto()

class VisionState(Enum):
    """Runtime states of the shared agent loop (terminal/browser/vision)."""
    IDLE = auto()
    EXECUTING = auto()
    PAUSED = auto()
    PAUSED_ON_ERROR = auto()
