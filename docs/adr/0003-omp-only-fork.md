# omp-only Windows-only fork scope

This fork sheds the upstream's multi-agent, multi-platform maintenance so one harness stays
small: omp on Windows. Anything another host or platform needs is deleted, not disabled.

**omp is the only host.** Non-omp session readers, host manifests, hook files and their docs
go; the shared mechanisms, the dispatcher, the `te` CLI and the omp extension stay.
State, settings and CLI names are unchanged so existing omp state keeps working. Rejected:
keeping other hosts behind a flag — the flag keeps the maintenance the fork exists to shed.

**Windows is the only lane.** The POSIX launchers go and the bootstrap plus its `.cmd`
pair is the one launch path; CI runs Windows only. Rejected: testing POSIX the fork no
longer ships.

**The experiments tree is gone.** Benchmark harnesses, adapters and run books go with the
hosts they compared; the quoted headline numbers stay as cited history. Rejected: keeping
the harness as reference — it reads the session formats just deleted.

## Considered Options

- Keep-everything fork with omp-first docs: re-proposes the upstream maintenance burden under a thinner manual.
