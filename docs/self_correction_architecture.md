# Comprehensive Technical Description: Intelligent Self-Correction Architecture

## Introduction

At the core of the assistant's autonomous operation lies the intelligent **"Planning -> Execution -> Evaluation -> Correction"** cycle. This architecture is built on a strict separation of roles:
*   **LLM ("Brain"):** Is the sole source of intelligence. Responsible for creating strategic plans, tactical evaluation of results, and deciding on methods for error correction.
*   **VisionHandler ("Muscles"):** Is the executor. It does not contain complex decision-making logic; its task is to accurately execute commands received from the LLM and report on the results.

This document describes the full lifecycle of a system built on the **"Task-Contract"** paradigm and an enhanced, multi-level escalation protocol for reliable and flexible self-correction.

---

## Phase 1: Strategic Planning

Everything begins with the creation of a high-level plan that serves as a "roadmap" for achieving the user's goal.

*   **Where:** `src/handlers/vision_handler.py`, `start_task()` method
*   **Process:**
    1.  **Trigger:** The user provides a text request.
    2.  **Context Analysis:** The system takes a screenshot of the current screen and analyzes it.
    3.  **Prompt Formulation:** `src/brain.py` formulates a prompt for strategic planning (`prompts/strategic_planning_system.txt`), instructing the LLM to create the most complete and accurate plan on the first attempt.
    4.  **LLM Response:** The LLM returns a `strategic_plan` — a list of `Task` objects.

### Key Concept: "Task-Contract"

A plan is a list of "contracts". Each contract (`Task`) is a Pydantic model (`src/llm/schemas.py`) containing three mandatory fields:

1.  **`task_description` (Goal):** A human-readable description. *What are we doing?* (e.g., "Launch Google Chrome").
2.  **`actions` (Actions):** A list of atomic steps that should be as complete as possible (with coordinates, if the element is visible). *How are we doing this?*
3.  **`expected_outcome` (Success Criterion):** A clear, unambiguous description of what the screen should look like **after successfully** completing the Task. *How will we know if it worked?* (e.g., "The Google Chrome browser window should appear and be maximized.").

---

## Phase 2: Execution and Evaluation Cycle

After user approval of the plan, `VisionHandler` enters the main operational loop.

### Step 2.1: Task Execution
*   **Where:** `src/handlers/vision_handler.py`, `_execute_next_task()` method
*   **Process:** `VisionHandler` takes the current Task from the plan, takes a "BEFORE" screenshot, and sequentially executes its actions.

### Step 2.2: Evaluation Request Formulation
*   **Where:** `src/handlers/vision_handler.py`, `_evaluate_task_result()` method
*   **Process:** After execution, an "AFTER" screenshot is taken. `brain.py` assembles the prompt for evaluation (`prompts/evaluation_system.txt`), which now contains **extended context**:
    *   All standard context (plan, goal, screenshots, etc.).
    *   **Attempt Counter:** `correction_attempts` and `MAX_CORRECTION_ATTEMPTS`, so the LLM knows at which stage of escalation it is.

---

## Phase 3: The Core of Intelligence — Failure Escalation Protocol

This is the central part of the system. The old binary logic of "simple/strategic failure" has been replaced by a flexible four-level protocol, which is handled in `_handle_evaluation_response()`.

### Scenario A: Success (`status: 'success'`)
Task completed. `correction_attempts` is reset to 0, and the system proceeds to the next task.

### Scenario B: Failure (`status: 'failure'`)
Task failed. `correction_attempts` increments by 1. `VisionHandler` analyzes `next_move.type` and acts according to the hierarchy:

#### Level 1: `action_correction` (Tactical Correction — "Correcting My Step")
*   **What it is:** The most frequent type of failure. The LLM understands that the **method** chosen was correct, but the **parameters** were imprecise (e.g., a misclick) or incomplete.
*   **LLM Response:** Contains **one corrected atomic action** in `next_move.action`.
*   **Handler Actions:** Replaces the `actions` list in the *current* task and immediately **re-executes it**.

##### Level 1.1: Structural Self-Correction (Learning from Errors)
This is a special, critically important subtype of `action_correction` that makes the system robust against incomplete or syntactically incorrect commands from the LLM, especially from less powerful models.

*   **Problem:** The LLM generates an action that passes JSON validation (it is syntactically correct) but is essentially incomplete (e.g., `{"name": "hotkey"}` without the mandatory `arguments` field).
*   **Mechanism:**
    1.  **Interception at Execution Level:** The error is not detected by the parser. It is caught directly in `src/actions.py` at the moment `VisionHandler` attempts to execute the action.
    2.  **Generating an "Instructional" Error:** Instead of a standard `KeyError`, the action function (e.g., `perform_hotkey`) generates a custom, informative `ValueError` exception. This message is specifically designed to be understandable to the LLM and contains three parts:
        *   **What broke:** `Action 'hotkey' failed...`
        *   **Why it broke:** `...The 'keys' argument must be a non-empty list...`
        *   **How to fix (Example):** `...Example: {'name': 'hotkey', 'arguments': {'keys': ['ctrl', 's']}}`
    3.  **Feedback:** `VisionHandler` catches this exception and passes its text to `_evaluate_task_result` as `task_result_error`.
    4.  **Closing the Loop:** This error enters the evaluation prompt. The LLM "reads" the message, sees its error and the example of the correct format, and then generates a corrected action as part of the standard `action_correction` response.

*   **Result:** The system doesn't just fix the error; it *teaches* the model the correct format of its own tools "on the fly". A potential fatal failure turns into a routine, corrective step, making the assistant significantly more reliable.

#### Level 2: `insertion_correction` (Predictable Deviation — "New Predictable Step")
*   **What it is:** The appearance of a standard UI element that is a predictable consequence of a previous action (confirmation dialog, cookie banner).
*   **LLM Response:** Contains an **entire new Task-Contract** in `next_move.new_task`.
*   **Handler Actions:** Considers the task that caused the deviation successful. **Increments `current_task_index`** and **inserts** the new task into the plan at this new position, then proceeds to execute it. This maintains the logical sequence of events.

#### Level 3: `task_replacement` (Uncertainty — "Trying to Solve Myself")
*   **What it is:** An action did not lead to a visible result, but the system still has attempts for correction (`correction_attempts < MAX_CORRECTION_ATTEMPTS`).
*   **LLM Response:** Contains an **entire new Task-Contract** in `next_move.new_task`, which represents an **alternative method** for achieving the goal (e.g., use a hotkey instead of a click).
*   **Handler Actions:** **Completely replaces** the current failed task with the new one and immediately executes it. `current_task_index` does not change.

#### Level 4: Escalation (Limit of Autonomy)
This level occurs when autonomous attempts are exhausted or a fatal failure has occurred.

*   **Sub-scenario 4A: Communication with User (`communicate_to_user`)**
    *   **Trigger:** `correction_attempts >= MAX_CORRECTION_ATTEMPTS`. The LLM has exhausted its autonomous attempts at Level 3.
    *   **LLM Response:** **One atomic action** `communicate_to_user` in `next_move.action`, where the assistant explains what it tried and asks for help.
    *   **Handler Actions:** Executes the action (shows a message to the user) and transitions to the `PAUSED_ON_ERROR` state.

*   **Sub-scenario 4B: Strategic Failure (`strategic_failure`)**
    *   **Trigger:** Something extraordinary and unresolvable occurred (application crashed, UI changed beyond recognition) OR the LLM took the initiative and decided that a complete replanning was required.
    *   **LLM Response (Two options):**
        1.  **Standard:** Returns `type: 'strategic_failure'` and a reason.
        2.  **Incorrect:** Returns any invalid JSON. In this case, the **universal fallback parser (`universal_fallback_parser`)** is activated, which operates according to a strict priority:
            -   **Priority 1: Search and "rescue" an entire plan.** First, the parser searches the response for a complete list of `Task` objects. If found, it "wraps" this plan into a valid `Evaluation` object with `type: 'strategic_failure'` and passes it on.
            -   **Priority 2: Fix standard evaluation.** If no plan is found, the parser searches for `evaluation` and `next_move` components, corrects typos and incorrect structure within them, and then assembles a valid `Evaluation` object from them.
    *   **Handler Actions (Dual role of `strategic_failure`):**
        1.  **If `new_plan` is absent:** The Handler initiates **Phase 3.5: Strategic Replanning**, triggering a new request to the LLM to create a plan.
        2.  **If `new_plan` is present (thanks to "rescue" at Priority 1):** The Handler understands that a plan was provided in advance. It **skips Phase 3.5**, immediately accepts this new plan, and begins its execution. This saves one API call and makes the system more efficient.

---

## Phase 3.5: Strategic Replanning

This new process is initiated only after `strategic_failure` is declared **AND** provided that the LLM has not supplied a plan beforehand. It clearly separates the roles of the tactical Evaluator and the strategic Planner.

*   **Where:** `src/handlers/vision_handler.py`, `_trigger_strategic_replan()` method
*   **Process:**
    1.  **Trigger:** `VisionHandler` receives a `strategic_failure` verdict **without** an embedded `new_plan`.
    2.  **Context Gathering:** The system gathers full context about the failure: the user's original request, the status of the previous plan's execution, and the reason for failure provided by the Evaluator.
    3.  **Planner Invocation:** `VisionHandler` again calls `brain.get_strategic_prompts`, but this time passes **extended context about the failure**.
    4.  **Prompt Formulation:** `brain.py` uses a special template (`prompts/strategic_planning_replan_context.txt`) to add this context to the prompt, giving the global Planner all the information needed to create a new, complete plan considering the current, changed situation.
    5.  **Plan Replacement:** The newly obtained `strategic_plan` **completely replaces** the old one. `current_task_index` is reset to 0, and the system begins executing the new plan from the very beginning.

---

## Phase 4: Irresolvable Error

This mechanism now primarily serves as a **failsafe**.

*   **Trigger:** `correction_attempts > MAX_CORRECTION_ATTEMPTS`. This occurs if the LLM-Evaluator on its last attempt violates the protocol (e.g., does not return `communicate_to_user`).
*   **Process:** The system transitions to the `PAUSED_ON_ERROR` state and asks the user for help, as before.

---

Comprehensive Description of the `wait_element` Tool

Overall Goal: `wait_element` is a key tool that allows the assistant to handle asynchronous, long-running operations (downloads, installations, applying settings, etc.). Its task is to pause the execution of the main plan until a specific indicator appears or disappears on the screen, signaling the completion of the operation.

The new system is hybrid. It combines the ability to proactively plan for waiting and dynamically react to unforeseen situations. There are two main scenarios in which `wait_element` can be triggered.

---

Scenario 1: Planned Waiting (Proactive Path)

This scenario is triggered when the system initially assumes that an action will be long-running.

1.  Planning Stage (LLM-Planner)
    *   File: `prompts/strategic_planning_prompt.txt`
    *   Logic: This prompt directly instructs the LLM: "Anticipate Long-Running Operations". If the user asks, for example, "download and install the program", the LLM-planner is obliged to include a `wait_element` step in the initial plan immediately after the action initiating the download.
    *   Plan example:
        1.  Click on the "Download" button.
        2.  `wait_element` with `wait_context`: "The download progress indicator should disappear, and a 'Finish' button should appear."
        3.  Click on the "Finish" button.

2.  Execution Stage (`VisionHandler`)
    *   File: `src/handlers/vision_handler.py`
    *   Function: `_execute_grounded_action(self, grounded_action: dict)`
    *   Logic: When the `wait_element` step is reached, this method:
        1.  Determines that `action_name == 'wait_element'`.
        2.  Takes a "clean" screenshot at this moment. This screenshot will serve as the "before" reference point for subsequent final evaluation.
        3.  Calls the `_handle_wait_action` method, passing it all information about the action and the newly taken screenshot.

---

Scenario 2: Reactive Waiting ("Detective" Path)

This scenario is our "insurance". It is triggered when a long-running operation was not foreseen in the plan, and a loading indicator unexpectedly appears on the screen.

1.  Evaluation Stage (LLM-Evaluator)
    *   File: `src/handlers/vision_handler.py`
    *   Function: `_evaluate_action_result(...)`
    *   Logic: After each executed action, this function takes a screenshot (`post_action_screenshot`) and sends it along with the previous screenshot (`pre_action_screenshot`) for analysis to the LLM-evaluator.
    *   File: `prompts/evaluation_prompt.txt`
    *   Logic: This prompt has a special rule CASE 0: Handle Loading Indicators. The LLM-evaluator analyzes the new screenshot and makes a decision:
        *   If the cause is obvious (e.g., the last action was "Click 'Save'", and a spinner appeared), it immediately returns a ready `wait_element` action.
        *   If the cause is not obvious (the indicator appeared, but it's unclear which of the recent actions caused it), it returns a special status: `evaluation.status: "unexpected_wait"`.

2.  Decision-Making Stage (`VisionHandler`)
    *   File: `src/handlers/vision_handler.py`
    *   Function: `_handle_evaluation_response(self, evaluation_data: dict | None)`
    *   Logic:
        *   If the Evaluator returned a ready `wait_element`: The method takes the very last screenshot from the history (`self.recent_action_history[-1]`), as it was guaranteed to be taken before the obvious operation, and calls `_handle_wait_action`.
        *   If the Evaluator returned `"unexpected_wait"`: The "detective" mechanism is launched. A new method `_find_wait_origin()` is called.

3.  Investigation Stage (LLM-"Detective")
    *   File: `src/handlers/vision_handler.py`
    *   Function: `_find_wait_origin(self)`
    *   Logic:
        1.  Gathers "evidence": text descriptions of the last 5 actions and their corresponding 5 "before" screenshots from `self.recent_action_history`.
        2.  Changes the system state to `FINDING_WAIT_ORIGIN`.
        3.  Sends all this evidence to the LLM, using a new, highly specialized prompt.
    *   File: `prompts/wait_origin_prompt.txt`
    *   Logic: This prompt gives the LLM one task: analyze the history and screenshots and return JSON with the index of the action (`origin_action_index`) that, in its opinion, started the waiting process.

4.  Verdict Stage (`VisionHandler`)
    *   File: `src/handlers/vision_handler.py`
    *   Function: `handle_plan_response(...)`
    *   Logic: When the system is in the `FINDING_WAIT_ORIGIN` state, it processes the "detective's" response.
        1.  Extracts `origin_action_index` from the response.
        2.  Uses this index to retrieve the correct historical screenshot from `self.recent_action_history`.
        3.  Creates a `wait_element` action "on the fly", as it was not in the plan.
        4.  Calls `_handle_wait_action` with this new action and the correct historical screenshot.

---

General Waiting Execution Cycle ("Monitoring" Mechanism)

Regardless of how `wait_element` was triggered, its execution follows a single algorithm:

1.  Start Monitoring
    *   File: `src/handlers/vision_handler.py`
    *   Function: `_handle_wait_action(self, wait_action, pre_wait_screenshot)`
    *   Logic:
        1.  Sets the state to `WAITING_FOR_ELEMENT`.
        2.  Saves `pre_wait_screenshot` (the correct one, taken "before" the operation began).
        3.  Initiates the first check immediately by calling `_perform_llm_wait_check()`.

2.  Check Cycle
    *   File: `src/handlers/vision_handler.py`
    *   Function: `_perform_llm_wait_check(self)`
    *   Logic:
        1.  Checks if the timeout has expired.
        2.  Takes a new, current screenshot.
        3.  Sends the text context of this screenshot to the LLM-checker.
    *   File: `prompts/wait_check_prompt.txt`
    *   Logic: This LLM has a simple task: compare the description from `wait_context` with the current screen state and return `{"condition_met": true/false}`.

3.  Check Result
    *   File: `src/handlers/vision_handler.py`
    *   Function: `handle_plan_response(...)`
    *   Logic: In the `WAITING_FOR_ELEMENT` state:
        *   If `condition_met: true`: Waiting is complete. `_evaluate_action_result` is called so the system can evaluate the result and proceed to the next step of the main plan.
        *   If `condition_met: false`: The condition is not yet met. A `QTimer` is started, which will call `_perform_llm_wait_check` again after 5 seconds, continuing the cycle.

This hybrid system allows `wait_element` to be both fast in simple cases and intelligent in complex ones, ensuring high reliability of the assistant.