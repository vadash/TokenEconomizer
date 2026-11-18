# omp support: a native host over Pi's session format

oh-my-pi (`omp`) loads Pi extensions through the `pi` manifest key, so it already runs the Pi
adapter's subprocess bridge and records turns; it still cannot run the product, because omp has
no system-prompt sections to set, and the plugin files those turns under the Pi host. omp is
therefore supported as its own host, not as Pi.

**omp is a distinct host, not a Pi alias.** It has its own host name, state directory and
manifest key, its own adapter beside the Pi one, and reuses the Pi session reader because omp's
JSONL is the same tree of entries. Rejected: running omp as `pi` — host names are a state and
settings surface, so a Pi label mis-reports omp sessions and applies Pi's levers.

**Every omp session is a session, at every depth.** Main, subagent and nested-subagent sessions
learn, receive the manual, and are guarded; only the person-facing notices are restricted to
top-level sessions. Rejected: Claude's treatment, where subagents are sidechains folded into the
parent and the guard is main-only — an omp subagent is a separate session with its own system
prompt and tool loop, and in recorded orchestration it carried most of the tool calls and most
of the failures.

**Pruning applies live, not through omp settings.** omp has no settings key that removes a tool
from a request, so the adapter applies the approved list with `setActiveTools` at session start
from TokenEconomizer's own state. Rejected: writing deny entries into omp's approval settings —
those refuse calls but still ship the schema, which leaves the token cost that pruning exists to
remove.
