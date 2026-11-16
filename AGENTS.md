# Agent Rules

This file is binding for every human or AI agent working in this repository.
Read it before changing anything. When instructions conflict, the explicit
request from the operator wins, but the logging rule below still applies.

## Lead delegation policy (binding)

This project runs in a **lead/worker** model. The lead agent plans, splits,
assigns, and reviews; it does not implement low- or medium-difficulty work
itself.

| Difficulty | Model | Typical work |
|---|---|---|
| Low | `opencode-go/glm-5.3-flash` | mechanical edits, single-file changes, formatting, straightforward doc/test updates |
| Medium | `opencode-go/minimax-m3.1-flash` | multi-file changes, new endpoints/handlers, reproducible bug fixes, tests across modules |
| High | Lead decomposes into low/medium packages | architecture, ambiguous requirements, cross-cutting redesigns |

Every delegated task must state the goal, files, constraints, acceptance
criteria, and verification command. Child sessions start with fresh context,
so pass everything they need. The lead reviews the diff, enforces the
invariants below, and owns the final `docs/AGENT_LOG.md` entry (or verifies
the subagent wrote it). Never run overlapping subagents on the same files.

The lead may perform the minimum unblocking action only when delegation is
unavailable or keeps failing, and must say why. The safety rules in this file
still apply to subagents.

## APK build and release rule (binding)

Any change under `android-app/` (or the Android build workflow) must be built
by GitHub Actions before the task is considered done. The workflow must finish
with no errors and publish the debug APK as the asset `sms-relay-debug.apk` on
the rolling `android-latest` release. Report this download URL:

<https://github.com/HessiKz/sms-relay/releases/download/android-latest/sms-relay-debug.apk>

Do not hand out an Actions artifact URL (it requires GitHub authentication), do
not commit an APK to the repository, and do not deploy an APK that this
workflow did not build. If the build fails, fix it and rerun before finishing.
The workflow uses a stable debug signing key stored in GitHub Actions secrets;
do not remove or rotate it without rebuilding and telling the operator that
the update signature changes.

## The non-negotiable rule: update the agent log

**Every task must end with an entry in [`docs/AGENT_LOG.md`](docs/AGENT_LOG.md).**
A task is not complete until the entry exists. This applies to code changes,
documentation changes, investigations that produce a decision, deployment
work, and abandoned attempts that future agents need to know about.

Rules for the log:

- **Append only.** Never rewrite, reorder, truncate, or delete existing
  entries. Add the new entry at the bottom.
- Use the entry format defined in `docs/AGENT_LOG.md`.
- Include the date, agent name/model, task, files touched, what changed,
  verification performed, decisions/risks, and follow-ups.
- If tests could not be run, say so and explain why.
- Never put secrets, bot tokens, bearer tokens, full SMS bodies, phone
  numbers, or contact names in the log.
- A one-line "no-op" entry is still required for an investigation that ends
  without a code change; record what was checked and what was concluded.

## Required workflow

1. Read `README.md`, `docs/AGENT_GUIDE.md`, and the tail of
   `docs/AGENT_LOG.md`.
2. Inspect the actual code before trusting any document. The code is the
   source of truth.
3. Make the smallest change that satisfies the request. Preserve the existing
   style and architecture.
4. Run the relevant tests. If the environment cannot run them, record that
   clearly.
5. If behavior, contracts, commands, environment variables, deployment, or
   known limitations changed, update `README.md` and/or
   `docs/AGENT_GUIDE.md` in the same task.
6. Append an entry to `docs/AGENT_LOG.md`.
7. Report what changed, what was verified, and what remains unresolved.

## Safety rules

- Do not deploy, restart services, edit `/etc/sms-relay/env`, modify the live
  SQLite database, push to a remote, or rotate credentials unless the operator
  explicitly asks for that action.
- Do not run `deploy/install.sh`, `backup.sh`, `restore.sh`, or
  `purge-history.sh` against a live system without explicit confirmation.
- Never commit an `env` file, a database, an APK, a token, or message content.
- The backend is a **single-process** design. Do not add Uvicorn workers or
  move in-memory state (`rev`, updater offset, rate limiters) into a
  multi-process setup without a redesign.
- SQLite schema changes must be additive and idempotent. Never use a
  destructive migration for the message queue; queued messages are the point
  of the relay.
- Cellular fallback is authorized only by a confirmed backend `failed` state
  or a local 4xx rejection. Do not broaden that condition without an explicit
  product decision.
- Treat Telegram as a server-mediated channel, not an end-to-end encrypted
  one. Do not log message bodies.

## Repository-specific invariants

- The phone writes an SMS to Room before uploading it.
- The backend writes a message and a job before calling Telegram.
- A Telegram `timeout` or `network` error is ambiguous, not a delivery
  failure.
- `failed` is terminal for a message and is what authorizes fallback.
- A command guard row is written before the radio call; a command is never
  executed twice because a result report was lost.
- One Telegram bot token can have only one `getUpdates` consumer.
- The public backend is loopback-only behind Caddy at `/relay/*`.

See `docs/AGENT_GUIDE.md` for the module map and change recipes.
