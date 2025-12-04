# Prompt Standardization Guide

All files in the `prompts/` folder should be written exclusively in English. This file simply describes how this should work.
This document describes a standardized structure for creating and managing prompts for the AI assistant. Adhering to this structure is crucial for ensuring the reliability, predictability, and performance of the model.

## Core Philosophy: System vs. User Prompt

The fundamental principle is a strict separation of responsibilities between the **System Prompt** and the **User Prompt**.

### 1. System Prompt (`*_system.txt`)

**Purpose:** To define the **personality, rules, and capabilities** of the AI for a specific task. This prompt acts as "firmware" or "operating instructions" for the model's role at a particular stage (e.g., planning, execution, evaluation). It should be as static as possible.

**Content:**

-   **Role Definition:** The very first line should clearly define the AI's role.
    -   *Example:* "You are an expert AI assistant specializing in strategic planning."
-   **Main Goal:** A brief description of the primary task for this role.
    -   *Example:* "Your main mission is to analyze the user's request and the current screen context to create a high-level, step-by-step plan."
-   **Rules and Limitations:** A list of immutable, strict rules that the model must follow. Here we define critical behavior.
    -   *Example:* "Always prefer keyboard shortcuts (`hotkey`) over mouse clicks.", "Never attempt to re-execute steps with `[DONE]` status."
-   **Available Tools (`{available_tools}`):** A complete list of tools (functions) that this role is allowed to use.
-   **Response Format (`{response_format_instructions}`):** A strict definition of the JSON schema that the model's response must adhere to.

**What to AVOID in the System Prompt:**

-   Any dynamic information dependent on a specific user request.
-   Any context from the current screen or environment.
-   Any history or data specific to the current session.

### 2. User Prompt (`*_user.txt`)

**Purpose:** To provide all **real-time dynamic context** that the AI needs to perform the task described in the System Prompt. It should be structured as a clear, well-organized brief for an expert (whose role is defined in the system prompt).

**Content:**

The User Prompt is built around a single, standardized structure called the **Standard Context Block**. This ensures that the model always knows where to find the necessary information, regardless of the current stage.

---

## Standard Context Block (for main stages)

This is a mandatory structure for User Prompts at the main stages (`strategic_planning`, `tactical_execution`, `evaluation`). It organizes information from the most general (mission) to the most specific (current environment).

```
### Mission ###
- Original User Request: {original_request}

### Plan Status ###
{plan_status_str}

### Last Action Analysis ###
- Action Performed: {action_description}
- Action Result: {action_result_or_error}

### Current Environment ###
#### Screen Analysis ####
{screen_context}

#### System Information ####
- Screen Resolution: {screen_resolution}
- Installed Applications:
  - {app1}
  - {app2}
  ...
```

---

## Specialized Prompts (for auxiliary tasks)

For highly specialized tasks, such as `wait_origin`, `wait_check`, and `error_explanation`, a **lightweight, minimally necessary context structure** is used. Providing a full context block in these cases is redundant "noise" and should be avoided.

### 1. `wait_origin` (Prompt for the "Detective")

-   **Task:** To find a single action in recent history that caused a loading indicator to appear.
-   **Required Context:** Action history and screenshots.
-   **Structure of `wait_origin_user.txt`:**
    ```
    ### Recent Action History ###
    - Action 1: {description_1} (see screenshot 1)
    - Action 2: {description_2} (see screenshot 2)
    ...

    ### Current Screen Analysis ###
    {current_screen_context_with_spinner}
    ```

### 2. `wait_check` (Prompt for the "Observer")

-   **Task:** To compare a given condition (e.g., "progress bar should disappear") with the current screen state.
-   **Required Context:** Wait condition and current screen analysis.
-   **Structure of `wait_check_user.txt`:**
    ```
    ### Wait Condition ###
    {wait_context}

    ### Current Screen Analysis ###
    {new_screen_context}
    ```

### 3. `error_explanation` (Prompt for the "Explainer")

-   **Task:** To explain to the user in simple terms why the assistant is "stuck".
-   **Required Context:** Information about the last action, the reason for the failure, and the user's original goal.
-   **Structure of `error_explanation_user.txt`:**
    ```
    ### Mission ###
    - Original User Request: {original_request}

    ### Last Action Analysis ###
    - Action Performed: {last_action}
    - Reason for Failure: {reason}
    ```

---
### Code Implementation

-   **Location:** The logic for assembling these context blocks should reside in `src/brain.py`.
-   **Recommendation:** Create a centralized helper function, e.g., `_build_context_block(**kwargs)`, for assembling the main block. For specialized prompts, formatting occurs within their own functions (`get_wait_check_prompts`, etc.).

By strictly following this standardized approach, we reduce the cognitive load on the model, minimize errors, and make the entire system more reliable and easier to debug.