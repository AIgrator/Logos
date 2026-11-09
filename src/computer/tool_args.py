"""Checks model-supplied tool arguments against the tool's real signature.

The response envelope declares ``arguments`` as a free-form object, so a weak
model can send a parameter the tool does not accept (``description`` on
``stop_shell_job``) or forget a required one (``job_id``). Without a check the
call dies inside the function with a Python ``TypeError`` that names neither
the tool nor the accepted arguments, and the model repeats the same call.

``validate_tool_arguments`` runs right before the call and raises one readable
``ValueError`` instead. A tool that takes ``**kwargs`` (every browser tool and
``launch_app``) accepts anything, so it is left alone: those tools already
filter or forward their own keywords.
"""
import functools
import inspect


def _tool_label(func) -> str:
    target = func
    while isinstance(target, functools.partial):
        target = target.func
    name = getattr(target, "__name__", "")
    return name or "tool"


def validate_tool_arguments(func, args) -> dict:
    """Returns ``args`` unchanged, or raises ``ValueError`` naming the problem.

    Only the argument *names* are checked. Types are the caller's business
    (``coerce_arguments`` converts stringified scalars before this runs).
    """
    if not isinstance(args, dict):
        args = {}
    try:
        parameters = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return args
    if any(p.kind is p.VAR_KEYWORD for p in parameters):
        return args
    accepted = [
        p.name for p in parameters
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    required = [
        p.name for p in parameters
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        and p.default is p.empty
    ]
    missing = [name for name in required if name not in args]
    unexpected = [name for name in args if name not in accepted]
    if not missing and not unexpected:
        return args
    problems = []
    if missing:
        noun = "argument" if len(missing) == 1 else "arguments"
        problems.append(f"missing required {noun} " + ", ".join(repr(n) for n in missing))
    if unexpected:
        noun = "argument" if len(unexpected) == 1 else "arguments"
        problems.append(f"unexpected {noun} " + ", ".join(repr(n) for n in unexpected))
    accepted_text = ", ".join(accepted) if accepted else "none"
    raise ValueError(
        f"Action '{_tool_label(func)}' failed: " + "; ".join(problems)
        + f". Accepted arguments: {accepted_text}.")
