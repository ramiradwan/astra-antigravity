"""Compatibility for explicit internal call seams, without retrying side effects."""

import inspect


def call_supported(fn, *args, **kwargs):
    parameters = inspect.signature(fn).parameters.values()
    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters):
        names = {p.name for p in parameters
                 if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               inspect.Parameter.KEYWORD_ONLY)}
        kwargs = {name: value for name, value in kwargs.items() if name in names}
    return fn(*args, **kwargs)
