# TokenEconomizer

A plugin that reads finished agent sessions and changes what later sessions in the same
repository pay for. These are the agents and session shapes its mechanisms see.

## Language

**omp**:
The oh-my-pi agent harness: its own session store, event names and context levers, which also
loads Pi-compatible extensions.
_Avoid_: Pi, pi fork

**Subagent session**:
An omp session spawned by another session's task tool; it has its own transcript, system prompt
and tool loop rather than being a sidechain inside the parent's transcript.
_Avoid_: sidechain, sub-session, child thread
