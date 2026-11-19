"""Reading omp session logs: the one place that knows the session file format.

Every mechanism reads a finished session through this package, so the format knowledge lives
here and nowhere else:

  * `calls`    -- `load_trajectory`: the session as ordered `(name, args, result, ok)` calls;
                  `call_cwds`, `tool_usage`, `strip_rtk_prefix`;
  * `omp`      -- an omp session file: usage, metadata, prompt count, and the active branch's
                  events.

Stdlib only.
"""
from .calls import RTK_PREFIX_RE, call_cwds, load_trajectory, strip_rtk_prefix, tool_usage
from .omp import (omp_active_branch, omp_json_reply, omp_prompt_count, omp_session_events,
                  omp_session_header, omp_session_usage, omp_write_session)

__all__ = [
    "RTK_PREFIX_RE", "call_cwds", "load_trajectory", "strip_rtk_prefix", "tool_usage",
    "omp_active_branch", "omp_json_reply", "omp_prompt_count", "omp_session_events",
    "omp_session_header", "omp_session_usage", "omp_write_session",
]
