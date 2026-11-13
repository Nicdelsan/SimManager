# ADR 0001 — Own minimal Python kernel, concepts from Cordis

- Status: accepted (M0)

## Context

The desired architecture ("everything is a plugin": context, services, reactive dependency
injection, effects, events with waterfall) comes from DeepSeek Harness / Cordis. Cordis is
TypeScript and in developer preview; the CFD ecosystem and the maintainer are Python.

## Decision

Write a small Python kernel (`simmanager.kernel`) that keeps only the concepts:

- plugin classes with `provides` / `inject` / `config_schema` / `apply(ctx)`;
- services under dotted keys with an integer contract major version;
- reactive DI (PENDING until providers exist, consumers torn down when a provider leaves);
- LIFO effects, owned tasks, plain and waterfall events;
- layered YAML configuration validated with pydantic;
- discovery through the `simmanager.plugins` entry point group.

Deliberately left out: scoped/forked contexts beyond one per plugin, hot code reloading,
service mixins, multiple providers per key with priority. `inject` covers mandatory single
services only; optional or multi-provider lookups (`scheduler.*`) happen at use time with
`ctx.services.get/all`, so a missing PBS plugin never blocks the monitor.

## Consequences

- One `asyncio.Lock` serializes lifecycle transitions: simple and race-free, at the cost of
  no parallel plugin start-up (irrelevant at this scale).
- A plugin failing in `apply` or in an owned task becomes FAILED and is not retried
  automatically (avoids restart loops); `kernel.reload(name)` retries it.
- The kernel is type-checked with `mypy --strict` and kept free of domain imports.
