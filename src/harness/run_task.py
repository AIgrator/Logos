"""Headless task harness: real pipeline without the GUI.

Runs the full loop — prompt -> LLM provider -> parse -> execute via
Executor -> result back to the model — with no Qt widgets, no screenshots
of the desktop, no manual clicks. For browser tasks (DOM grounding only).

Usage (from project root):
    .venv\\Scripts\\python.exe src/harness/run_task.py --provider google --task "Open https://example.com and tell me the main heading"

Options:
    --provider  provider id (default: google)
    --model     override model (default: provider default)
    --task      user task (required)
    --max-steps max agent steps (default: 8)
    --headless  run the browser headless
    --close     close the browser at the end (default: stays open for inspection)
"""
import argparse
import functools
import json
import logging
import sys

sys.path.insert(0, '.')
sys.path.insert(0, 'src')

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

SYSTEM_PROMPT = """You are a browser automation agent. Achieve the user's goal using ONLY the tools below.
Rules:
- First action is ALWAYS browser_goto (unless the task is already on a page), then browser_snapshot.
- Act ONLY on refs from the latest snapshot; refs expire after every navigation or page change.
- One tool call per reply, strictly this JSON and nothing else: {{"name": "<tool>", "arguments": {{...}}}}.
- When the goal is achieved, reply {{"name": "finish", "arguments": {{"message": "<result for the user>"}}}}.
- Never ask for credentials. Never paste secrets.
- If you are blocked on user-specific input (birth date, name, a choice only
  the user can make), do NOT invent data: call communicate_to_user with your
  question and wait for the reply, which arrives as the next history entry.

Available tools:
{tools}
"""

MAX_HISTORY_CHARS = 6000


def build_tools_text() -> str:
    from src.computer.modes import browser as manifest
    lines = []
    for name, fn in manifest.get_tools_metadata().items():
        meta = getattr(fn, "__tool_meta__", {}).get("tool", {})
        desc = meta.get("strategic") or meta.get("description") or ""
        args = ", ".join((meta.get("arguments") or {}).keys())
        lines.append(f"- {name}({args}): {desc}")
    return "\n".join(lines)


def parse_tool_call(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        from src.llm.llm_parsing import repair_json_string
        fixed = repair_json_string(text)
        if isinstance(fixed, dict) and "name" in fixed:
            return fixed
    except Exception:
        pass
    raise ValueError(f"Could not parse tool call from model reply: {text[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="google")
    ap.add_argument("--model", default=None)
    ap.add_argument("--task", required=True)
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--close", action="store_true")
    args = ap.parse_args()

    from src.llm import get_provider_class
    from src.computer.computer import Computer
    from src.computer.modes import browser as manifest
    from src.executor.executor import Executor
    from src.browser.manager import BrowserManager
    import keyring
    from settings_manager import KEYRING_SERVICE_NAME

    api_key = keyring.get_password(KEYRING_SERVICE_NAME, args.provider)
    if not api_key:
        print(f"No API key in keyring for provider '{args.provider}'.")
        return 2
    provider_cls = get_provider_class(args.provider)
    defaults = provider_cls.get_default_settings()
    if args.model:
        defaults["model"] = args.model
    defaults["api_key"] = api_key
    provider = provider_cls(defaults)
    print(f"Provider: {args.provider} model={defaults.get('model')}")

    computer = Computer(settings_manager=None, mode="chat")
    for name, fn in manifest.get_tools_metadata().items():
        setattr(computer, name, functools.partial(fn, computer))
    browser_manager = BrowserManager(headless=args.headless)
    computer.browser_factory = lambda: browser_manager
    computer.browser_manager = None
    executor = Executor(computer, ui=None)

    system = SYSTEM_PROMPT.format(tools=build_tools_text())
    history: list = []
    ctx = {"shell_allowed": False}

    from src.llm.exceptions import FatalError, RetryableError
    import time

    def _show(prefix: str, text: str, limit: int = 400):
        try:
            print(f"{prefix} {text[:limit]}")
        except UnicodeEncodeError:
            print(f"{prefix} {text[:limit].encode('ascii', 'replace').decode()}")

    transient_marks = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded")
    transient_seen = 0

    for step in range(1, args.max_steps + 1):
        user_prompt = f"User task: {args.task}\n\nHistory (latest last):\n" + "\n".join(history[-6:]) if history else f"User task: {args.task}"
        print(f"--- step {step}: asking model...")
        try:
            resp = provider.get_response(user_prompt, None, system, None)
        except RetryableError as e:
            print(f"transient provider error, waiting 20s: {str(e)[:150]}")
            time.sleep(20)
            continue
        except FatalError as e:
            if any(m in str(e) for m in transient_marks) and transient_seen < 3:
                transient_seen += 1
                print(f"transient failure ({transient_seen}/3), waiting 30s...")
                time.sleep(30)
                continue
            print(f"provider failed, stopping: {str(e)[:300]}")
            return 3
        except Exception as e:
            print(f"provider failed, stopping: {str(e)[:300]}")
            return 3
        text = (resp.get("text") or "").strip()
        _show("model:", text)
        try:
            call = parse_tool_call(text)
        except ValueError as e:
            history.append(f"System: {e} Reply with JSON only.")
            continue
        name = call.get("name", "")
        if name == "finish":
            print("FINISH:", (call.get("arguments") or {}).get("message", text))
            break
        if name == "communicate_to_user":
            question = (call.get("arguments") or {}).get("message", text)
            print(f"MODEL ASKS YOU: {question}")
            try:
                reply = input("Your reply (empty continues): ").strip()
            except EOFError:
                reply = ""
            history.append(f"User reply: {reply or '(no reply, proceed on your own)'}")
            continue
        try:
            result = executor.execute({"name": name, "arguments": call.get("arguments", {}) or {}}, ctx)
        except ValueError as e:
            history.append(f"System: {e}")
            continue
        if not result.ok:
            history.append(f"System: tool '{name}' failed: {result.error}")
            continue
        out = result.output_text or "(ok, no output)"
        history.append(f"Tool {name} result:\n{out[:MAX_HISTORY_CHARS]}")
        print(f"tool {name}: ok")
    else:
        print("STOPPED: max steps reached.")

    if args.close:
        try:
            browser_manager.close()
        except Exception:
            pass
    else:
        print("Browser left open for inspection. It shares the persistent profile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
