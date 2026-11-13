# Knowledge base

Shared, versioned memory for every agent and contributor working on SimManager.
Rules for keeping it current: `AGENTS.md` → "Knowledge base".

| File | Read it when | Contains |
|---|---|---|
| [status.md](status.md) | always, first | Current milestone, what works, what's next, open questions, changelog |
| [project-brief.md](project-brief.md) | starting any feature | Scope, functional requirements, MVP services/plugins |
| [roadmap.md](roadmap.md) | planning or starting a milestone | Milestone plans with agreed design details |
| [conventions.md](conventions.md) | before writing code or committing | Coding, testing, privacy, git and communication rules |
| [domain.md](domain.md) | touching parsers, scheduler, data model | CFD/HPC facts: file formats, log patterns, job states, glossary |
| [clusters.md](clusters.md) | touching SSH, scheduler, polling | Anonymized facts about the target clusters |
| [assumptions.md](assumptions.md) | writing `# ASSUMPTION:` or receiving new samples | Register of unverified hypotheses and their status |
| [../architecture.md](../architecture.md) | changing structure | Authoritative architecture description |
| [../adr/](../adr/) | questioning a past decision | Architecture decision records |

Writing style: short, factual, English, present tense. Date entries (YYYY-MM-DD). Replace
outdated statements instead of appending contradictions.
