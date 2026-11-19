# Trace

The one place that knows omp's session-log format. Every mechanism reads a finished session as
an ordered list of `(tool_name, args, result_text, success)` calls through `load_trajectory`.

| Module | |
|---|---|
| `calls.py` | `load_trajectory`, `call_cwds`, `tool_usage`, `strip_rtk_prefix` |
| `omp.py` | omp's session tree: the active branch, written as an event transcript; prompts; usage |
| `jsonl.py` | line and record readers |
