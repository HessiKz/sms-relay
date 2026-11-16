# Agent Log

This is an append-only record of work done by agents on this repository.

**Every agent must append an entry before finishing a task.** See
[`../AGENTS.md`](../AGENTS.md) for the binding rule. Never edit, reorder, or
delete an existing entry. Add new entries at the bottom.

## Entry format

Use this template:

```markdown
## YYYY-MM-DD — Short task title

- **Agent:** name/model and harness if relevant
- **Task:** what was requested and why
- **Files:** created/modified/deleted paths
- **Changed:** concise description of the actual change
- **Verification:** commands run and results, or why they could not run
- **Decisions / risks:** trade-offs, assumptions, known problems left behind
- **Follow-ups:** concrete next actions, or "none"
```

Do not include secrets, tokens, full SMS bodies, phone numbers, or contact
names.

---

## 2026-11-15 — Documentation reset and agent workflow

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode)
- **Task:** Remove the legacy documentation set and replace it with
  source-based documentation, an implementation guide for agents, an
  append-only agent log, and a binding rule that all agents update the log.
- **Files:** deleted the old `README.md`, `deploy/README.md`, and the entire
  old `docs/` tree (API, deployment, Mini App, security, troubleshooting,
  plans, and specs); created `README.md`, `AGENTS.md`,
  `docs/AGENT_GUIDE.md`, and `docs/AGENT_LOG.md`; updated documentation
  references in `deploy/install.sh` and `deploy/sms-relay-api.service`.
- **Changed:** rewrote the project documentation from the source at commit
  `e1a04bd`. The new README covers topology, runtime model, state machines,
  API surface, configuration, development, deployment, security, testing, and
  confirmed code-level issues. The agent guide maps every backend, Android,
  Mini App, and deploy module; records invariants; gives change recipes; and
  documents known traps. `AGENTS.md` makes updating this log mandatory.
- **Verification:** documentation reviewed against the source; `bash -n
  deploy/install.sh` passed; `systemd-analyze verify
  deploy/sms-relay-api.service` only reported the expected missing deployed
  interpreter at `/opt/sms-relay/venv/bin/python`. The backend test suite was
  not executed because the host did not have `python3` available during the
  review. No backend or Android application code was changed; only
  documentation and stale documentation references were updated.
- **Decisions / risks:** the documentation describes the code as it is,
  including the known bugs listed in the README and agent guide, rather than
  describing intended behavior. The old docs and generated plans were removed
  per the operator's request. FastAPI's default `/docs` route is noted as an
  exposure because it remains enabled in the application.
- **Follow-ups:** run `backend/.venv/bin/python -m pytest tests -q` in an
  environment with Python; fix the import/resume, import/live dedupe, and
  chunk-limit issues first; reconnect or remove the backend fallback settings;
  disable or protect FastAPI's default `/docs` and `/openapi.json`.

## 2026-11-16 — Production deployment on ai-barrow.ir

- **Agent:** GLM-5.3-Flash (OpenCode)
- **Task:** Deploy and verify the backend at `https://ai-barrow.ir/relay/`
  using the operator-supplied dedicated bot credentials, per the
  operator's explicit request ("run the server now", "move to /opt").
- **Files:** moved the repo from `/tmp/opencode/sms-relay` to
  `/opt/sms-relay-src`; installed `python3.11` via dnf and created
  `/opt/sms-relay/venv`; ran `deploy/install.sh --yes` (created the
  `smsrelay` user, `/opt/sms-relay/app`, `/var/lib/sms-relay`,
  `/etc/sms-relay`, the systemd unit, and the Caddy `handle /relay/*`
  block); wrote `/etc/sms-relay/env` (0640 root:smsrelay) with real bot
  credentials and generated `PHONE_A_TOKEN`/`ADMIN_TOKEN`; wrote
  `/root/relay-phone-token.txt` (0600); created
  `/etc/systemd/system/sms-relay-api.service.d/10-egress-proxy.conf`;
  updated `docs/AGENT_GUIDE.md` (egress-proxy operations note).
- **Changed:** service installed, enabled, and started; Caddy validated
  and reloaded; sing-box started and enabled at boot; Mini App menu
  button set to the `/relay/app/` URL; a test message was delivered to
  the admin chat through the proxy.
- **Verification:** local and public `/health` return `{"ok":true}`;
  `getMe`, `getWebhookInfo`, and `getChatMenuButton` succeed through the
  proxy (no webhook set, 0 pending updates); `sendMessage` delivered a
  test message to the admin chat; public `GET /relay/app/` returns the
  Mini App shell (HTTP 200, `<base href="/relay/app/">` present);
  `systemctl show` confirms the drop-in's `Wants`/`After=sing-box`;
  the service process holds an ESTABLISHED connection to the proxy
  (long-poll active) with zero errors in the journal; service ~38 MB RSS.
- **Decisions / risks:** direct Telegram egress is blocked on this host
  (DNS resolves `api.telegram.org` to an unreachable address), so all
  Bot API traffic depends on the local sing-box proxy; if sing-box dies,
  delivery/polling pause while the REST API keeps accepting and queueing
  messages. Receipt of a bot command was not yet exercised end-to-end
  (needs the admin to send one from Telegram). `deploy/install.sh` still
  creates fresh venvs with the system `python3` (3.6 here); the 3.11
  venv was pre-created as a workaround.
- **Follow-ups:** operator sends `/status` in the bot chat to confirm
  the command path; install the Android app with the base URL and phone
  token from `/root/relay-phone-token.txt`; fix the installer's python3
  dependency; optionally commit the docs/deploy changes.

## 2026-11-16 — Onboarding review (no code change)

- **Agent:** DeepSeek V4.1 Flash (OpenCode)
- **Task:** Read the repository and explain its architecture, flows,
  invariants, and current state to the operator.
- **Files:** none (read-only review of `README.md`, `AGENTS.md`,
  `docs/AGENT_GUIDE.md`, `docs/AGENT_LOG.md`, and the backend, Android,
  Mini App, and deploy sources).
- **Changed:** no files changed.
- **Verification:** source cross-checked against the documentation: 26
  backend route handlers, 349 test functions, SQLite schema and state
  tables, delivery retry logic, Android fallback policy, and the systemd
  unit. The documentation matches the implementation, including the 12
  known code-level issues. Tests were not run: the host interpreter is
  Python 3.6 without FastAPI/pytest, and the deployed
  `/opt/sms-relay/venv` (3.11) has no pytest.
- **Decisions / risks:** none; investigation only. The pre-existing
  uncommitted documentation rewrite and untracked `.serena/` were left
  untouched.
- **Follow-ups:** install `backend/requirements-dev.txt` in a Python
  3.11+ virtualenv and run the 349-test suite; address the known issues
  starting with resume/import dedupe and the chunk-length mismatch.

## 2026-11-16 — Add lead delegation policy

- **Agent:** DeepSeek V4.1 Flash (OpenCode)
- **Task:** The operator designated this session as project lead and
  required every OpenCode agent to remember a delegation policy: low
  difficulty to GLM 5.3 Flash, medium difficulty to MiniMax 3.1 Flash,
  and the lead only plans, assigns, and reviews.
- **Files:** updated `/root/.config/opencode/AGENTS.md` (outside the
  repository), `AGENTS.md`, and `docs/AGENT_LOG.md`.
- **Changed:** added a binding "Lead delegation policy" section to the
  global OpenCode instructions, so every session on this host loads it,
  and to the repository rules. It maps low/medium/high work to exact
  model IDs and requires a written plan, context-complete delegation,
  diff review, and no overlapping subagent edits.
- **Verification:** model IDs checked against the OpenCode model
  catalog: `opencode-go/glm-5.3-flash` and
  `opencode-go/minimax-m3.1-flash` are both active. No application code
  changed and the backend suite was not run.
- **Decisions / risks:** the policy is instruction-level, not enforced
  by configuration, so a session that ignores `AGENTS.md` can bypass it.
  High-difficulty work is decomposed into low/medium packages because
  the operator specified only two model tiers.
- **Follow-ups:** use `subagent` with the mapped model on the next
  implementation task and verify the child receives enough context;
  optionally define named worker agents in `opencode.json` if mechanical
  enforcement is desired.

## 2026-11-16 — Design proposal: QR-paired multi-device relay (no code change)

- **Agent:** DeepSeek V4.1 Flash (OpenCode)
- **Task:** The operator asked for design ideas before implementation:
  replace manual device-token setup with a QR code shown by the Android
  app and scanned in the Mini App, and support multiple Android devices.
- **Files:** `docs/AGENT_LOG.md` only.
- **Changed:** no code. The proposal defines a per-device credential
  model, a one-time QR pairing flow whose claim secret never appears in
  the QR, a device registry, device attribution on messages and
  commands, strict command routing, revocation, and a legacy-token
  transition.
- **Verification:** design checked against the source: one shared
  `PHONE_A_TOKEN`; `pending_commands()` has no device filter;
  `finish_command()` has no ownership check; `device_status` is keyed by
  a client-supplied `X-Device-Info` id but `/status` returns only the
  newest row; `messages` and `sms_commands` have no `device_id`; Android
  stores the token in plain `SharedPreferences`. Backend tests were not
  run because no available interpreter has pytest.
- **Decisions / risks:** a credential is unavoidable; the QR removes
  manual copying and provisions a unique per-device token. The QR
  carries only the pairing id and a short scan code, never the token or
  claim secret. Multi-device routing must be strict, or two phones can
  send the same command. The known resume/import dedupe issues get worse
  with multiple devices and should be fixed in the same project. Open
  questions: same-person vs multi-person inboxes; whether Phone B stays
  an SMS fallback or becomes a paired relay; shared vs per-device
  Telegram chat; contact merging; default sender selection.
- **Follow-ups:** get operator answers, then split into backend
  pairing/auth, Mini App device management, Android pairing UI, and
  command-routing packages and delegate them.

## 2026-11-16 — Multi-device decisions locked; first fixes dispatched

- **Agent:** DeepSeek V4.1 Flash (OpenCode), lead
- **Task:** Lock the operator's product decisions for QR-paired
  multi-device support, then dispatch the first implementation work.
- **Files:** `docs/AGENT_LOG.md` only. The implementation contract is in
  `/tmp/opencode/multi_device_spec.md` (outside the repository); no
  application code was changed by the lead.
- **Changed:** recorded the approved decisions: all devices belong to
  the single admin, per-device contact lists and chat lists with no
  cross-device mixing, one shared Telegram chat with device labels,
  force re-pairing with the shared device token removed, QR scan plus
  short-code confirmation, Mini App-first UX, and Phone B cellular
  fallback frozen. Dispatched two MiniMax work packages: the Android
  default-SMS role fix and the Mini App history/smoothness fix.
- **Verification:** inspected
  `MainActivity.requestDefaultSmsAppRole()`: it returns immediately
  before Android 10 and does not handle the role result on Android 10+.
  Inspected the Mini App refresh path: every revision change calls
  `loadThread()` with a full reset and `ui.renderThread()` replaces all
  DOM children; `thread_timeline()` paginates by timestamp only, so
  same-second events can be skipped. No tests have run yet; the
  subagents will report.
- **Decisions / risks:** force re-pairing will return 401 to the
  existing APK until the new APK pairs. Existing rows are preserved
  under an archived `legacy` device so old history stays visible
  without mixing it into a new device. Thread identity becomes
  `(device_id, thread_key)`, so the Mini App API, queries, and tests
  must change in lockstep. Phone B fallback is not extended or removed.
- **Follow-ups:** review both work packages, then dispatch the backend
  device/pairing foundation and the Android pairing UI in parallel;
  then the Mini App device navigation and Telegram labels/routing.

## 2026-11-16 — Fix Android default-SMS role request

- **Agent:** MiniMax M3.1 Flash (subagent), reviewed by DeepSeek V4.1
  Flash (lead).
- **Task:** The operator reported that tapping "Default SMS app" did
  nothing. Fix the role request on Android 8/9 and Android 10+.
- **Files:** `android-app/.../ui/MainActivity.kt`,
  `android-app/.../util/RoleState.kt`,
  `android-app/.../ui/RespondViaMessageService.kt`,
  `android-app/app/src/main/res/values/strings.xml`.
- **Changed:** added an activity-result launcher; API 26-28 now uses
  `ACTION_CHANGE_DEFAULT` with `EXTRA_PACKAGE_NAME`; API 29+ uses the
  SMS role intent and handles the result; a system Default Apps
  settings fallback runs when the role is unavailable or the dialog
  cannot open; permissions and status refresh after the grant; the
  button reports "already default", granted, refused, and vendor-refused
  states. Removed the automatic role prompt on every launch; the button
  and the capture-status text are the entry points.
- **Verification:** no APK was built. This host has no JDK, Gradle,
  Android SDK, or reachable Google Maven, and the repository forbids
  building on the production VPS. Static checks passed: resource
  references resolve, XML parses, Kotlin bracket balance is clean,
  manifest components satisfy the SMS-role contract, and `git diff
  --check` is clean. The lead caught an invalid three-argument
  `registerForActivityResult("key", …)` call in the first revision; it
  was corrected to the standard two-argument form.
- **Decisions / risks:** OEMs may still suppress the role dialog; the
  settings fallback and four distinct toasts are the mitigation. The
  activity-result callback and permission request are not exercised on
  a device. The launch-time auto-prompt was deliberately removed to
  avoid showing the role dialog on every start.
- **Follow-ups:** build the APK off the VPS and run the manual Android 9
  and Android 13+ checks (role dialog, grant/refuse, settings fallback,
  incoming SMS, import, composed SMS).

## 2026-11-16 — Mini App full history and smooth refresh

- **Agent:** MiniMax M3.1 Flash (subagent), reviewed by DeepSeek V4.1
  Flash (lead).
- **Task:** The operator reported that the Mini App was laggy, reloaded
  itself, and did not reliably show older or complete conversations.
- **Files:** `backend/app/db.py`, `miniapp.py`, `rev.py`, `ingest.py`,
  `api.py`, `updater.py`, `main.py`,
  `static/app/assets/js/{store,ui,app}.js`, `tests/test_db.py`,
  `tests/test_miniapp_inbox.py`, `tests/test_miniapp_frontend_static.py`,
  `README.md`, `docs/AGENT_GUIDE.md`.
- **Changed:** added a stable `(timestamp, id)` cursor for threads and a
  `(timestamp, thread key)` cursor for the chat list; `has_more` is now
  truthful at page boundaries; the events long poll reports the scope of
  a change. The Mini App merges pages by event id instead of resetting,
  reuses DOM rows, preserves scroll, debounces bursts of changes, and
  loads older messages and conversations in stable pages. The
  `before_ts`-only chat cursor was corrected so it cannot repeat the
  cursor row.
- **Verification:** isolated `/tmp/opencode/venv311` run:
  **438 passed, 3 failed + 1 error**, the same four pre-existing
  failures (contact-search tests and a shutdown-timing test) confirmed
  against the clean tree. New cursor, same-second, full-history, chat
  paging, events-scope, and static frontend tests pass. `node --check`
  passes on all five JS modules; the subagent also ran a jsdom harness
  showing no DOM mutations on an unchanged refresh and preserved scroll.
  No deployment or service restart.
- **Decisions / risks:** a refresh fetches the newest thread page, so an
  old outbound status that has fallen out of that page is not updated
  until the page is loaded again; the scope hint is best-effort and
  falls back to a full refresh. Device scoping is not implemented in
  this package; the helpers are ready for a device filter.
- **Follow-ups:** add per-device filtering to the cursors and Mini App
  routes in the multi-device package.

## 2026-11-16 — Android QR pairing and encrypted device token

- **Agent:** MiniMax M3.1 Flash (subagent), reviewed by DeepSeek V4.1
  Flash (lead).
- **Task:** Replace the manually pasted shared token with QR pairing,
  store the per-device token encrypted, and handle forced re-pairing.
- **Files:** new `TokenStore.kt`, `net/PairingClient.kt`,
  `pairing/PairingController.kt`, `pairing/QrCode.kt`; modified
  `SettingsStore.kt`, `net/RelayClient.kt`, `ui/MainActivity.kt`,
  `work/{UploadWorker,CommandWorker,ImportWorker,Schedule}.kt`,
  `contacts/ContactsSync.kt`, `res/layout/activity_main.xml`,
  `res/values/strings.xml`, `app/build.gradle.kts`, `README.md`.
- **Changed:** the pairing screen shows a backend-supplied QR payload
  and short code, polls `/pair/claim`, and stores the issued token with
  AES-GCM under an Android Keystore key. The manual token field and
  plaintext token are gone. A 401/403 clears the token, stops uploads
  and commands, keeps the Room queue, and shows "pair again"; re-pairing
  reuses the device id. A base-URL change invalidates the token.
  Workers refuse to call the server without a token. Added ZXing core
  for offline QR rendering. Phone B fallback semantics were not changed.
- **Verification:** no APK was built. This host has no JDK, Gradle, or
  Android SDK, and the repository forbids building on the production
  VPS. Static checks passed: XML parse, resource/id references, Kotlin
  bracket balance, no `Log`/`println`, no plaintext token write, and
  ZXing class/JAR inspection. The backend pairing endpoints do not
  exist yet.
- **Decisions / risks:** the Keystore/GCM code is new and untested on
  hardware; a key invalidation forces a re-pair. A write failure is
  surfaced as a pairing failure. No device test was performed.
- **Follow-ups:** build the APK off the VPS; after the backend pairing
  endpoints land, run the pair/approve/revoke/re-pair manual checks and
  verify the queue drains after re-pairing.

## 2026-11-16 — Backend device registry, pairing and per-device auth

- **Agent:** MiniMax M3.1 Flash (subagent), reviewed by DeepSeek V4.1
  Flash (lead).
- **Task:** Backend half of the approved multi-device design: device
  identity, QR pairing, per-device tokens, forced re-pairing, migration
  of old rows, and strict command ownership.
- **Files:** new `backend/app/pairing.py`,
  `backend/tests/{device_support,test_pairing,test_devices,test_command_routing}.py`;
  modified `db.py`, `security.py`, `api.py`, `config.py`, `main.py`,
  `ingest.py`, `importer.py`, `miniapp.py`, `updater.py`, `flow.py`,
  tests, `deploy/env.template`, `deploy/install.sh`.
- **Changed:** added `devices`, `pairings`, `device_contacts`,
  `thread_reads_scoped`, and `messages/sms_commands.device_id` with an
  idempotent migration that backfills old rows into an archived
  `legacy` device. Removed `PHONE_A_TOKEN`; device routes now resolve a
  SHA-256 token hash to one device and update a throttled heartbeat.
  Added public `/pair/start` and `/pair/claim`, Mini App preview,
  approve, cancel, list, rename, and revoke endpoints. Ingest, import,
  contacts, commands, command results, `sms-state`, status, and the
  admin message/contact filters are device-scoped. Commands are never
  broadcast: explicit device, configured default, or the only active
  device, otherwise a clear no-device error. Telegram replies route to
  the original message's device.
- **Verification:** `/tmp/opencode/venv311` full suite: **516 passed,
  3 failed, 1 error**, exactly the four known pre-existing failures.
  Focused pairing/device/routing tests: 77 passed. `bash -n
  deploy/install.sh` passes. No deployment, restart, env, or live DB
  change.
- **Decisions / risks:** the Mini App/contact readers still union
  device contacts with the legacy table so the current `/app/api/chats`
  routes keep working; the next package makes them strictly per
  device. The old global Mini App routes still show all devices until
  that package lands, so this is not yet deployable for the operator's
  per-device requirement. `/pair/claim` is not yet rate-limited (the
  start endpoint is); follow up. The legacy backfill re-copies
  contacts/reads on every startup because the old tables are kept; it
  is idempotent but not free.
- **Follow-ups:** add strict per-device chat/thread/contact scoping and
  routes, scope `rev` notifications by device/thread, fix resume-replays
  imports and live-vs-import dedupe, add Telegram device labels, then
  rebuild the Mini App UI and update docs.

## 2026-11-16 — Telegram device labels

- **Agent:** GLM 5.3 Flash (subagent), reviewed by DeepSeek V4.1 Flash
  (lead).
- **Task:** The operator wants one shared Telegram chat, with each
  forwarded message labelled by the device that captured it.
- **Files:** `backend/app/delivery.py`,
  `backend/tests/test_delivery.py`.
- **Changed:** added a compact `📱 <b>device name</b>` header line,
  HTML-escaped, repeated on every chunk and included in the chunk-length
  calculation. A missing/unknown device omits the label rather than
  blocking delivery. Retry, chunking, reply mapping, and command-result
  notifications are unchanged.
- **Verification:** focused `tests/test_delivery.py`: 28 passed. The
  full suite was not run because the per-device scoping worker was
  changing other backend files at the same time. No deployment.
- **Decisions / risks:** an unknown device is silently unlabelled; a
  locked read per delivered message is added. This package is
  independent of the per-device chat/contact scoping.
- **Follow-ups:** run the full suite after the scoping package lands;
  decide whether an unknown device should show a placeholder label.

## 2026-11-16 — Strict per-device scoping and device-first Mini App API

- **Agent:** MiniMax M3.1 Flash (subagent), reviewed by DeepSeek V4.1
  Flash (lead).
- **Task:** Remove the remaining cross-device mixing in the Mini App
  readers, make every chat/thread/contact/read query strictly
  `(device_id, thread_key)`, add the device-first Mini App API, scope
  revisions by device/thread, fix the resume/import and live/import
  dedupe bugs, and harden pairing claims.
- **Files:** `backend/app/{db,rev,miniapp,api,ingest,importer,updater,
  main,pairing}.py`; new `backend/tests/test_device_isolation.py`;
  updates to `test_db.py`, `test_miniapp_inbox.py`,
  `test_miniapp_send.py`, `test_ingest.py`, `test_import.py`,
  `test_pairing.py`, `test_devices.py`.
- **Changed:** `chat_summaries`, `thread_timeline`, `thread_exists`,
  `thread_meta`, `miniapp_contacts`, and `mark_thread_read` now require
  and filter by `device_id`; Mini App names come only from
  `device_contacts`, unread only from `thread_reads_scoped`. New routes
  are `/app/api/devices/{device_id}/chats...`; the old device-less
  routes return 410. `resume_received` never resumes `source='import'`
  rows, and `find_recent_duplicate` never treats an import as a capture
  twin. Revision scopes are `(device_id, thread_key)` and the events
  payload is `threads: [{device_id, thread_key}] | null`. Pairing claims
  now rate-limit only wrong secrets per pairing; valid polls are not
  blocked behind the shared Caddy address. Added
  `POST /app/api/devices/{id}/default` and a `default` device-list flag;
  revoking the default clears it. `backfill_legacy_device` has a fast
  path.
- **Verification:** `/tmp/opencode/venv311` full suite: **556 passed,
  3 failed, 1 error**, exactly the four known pre-existing failures.
  Focused `test_pairing.py` 44 passed; `test_devices.py` 37 passed.
  pyflakes clean on the touched files. No deployment, restart, env, or
  live DB change.
- **Decisions / risks:** the bundled Mini App JS is now intentionally
  broken because it still calls the removed routes; the UI rewrite must
  land before deployment. The old `/app/api/chats...` routes are 410 by
  design. Mini App contact readers no longer consult the legacy global
  table; the Telegram bot still has some global contact fallbacks,
  which the UI rewrite does not address. The events payload shape
  changed for the frontend.
- **Follow-ups:** finish the device-first Mini App UI, then update
  README/AGENT_GUIDE and the log; decide whether the bot's name search
  should be strictly device-scoped too.

## 2026-11-16 — Device-first Mini App UI

- **Agent:** MiniMax M3.1 Flash (subagent), reviewed by DeepSeek V4.1
  Flash (lead).
- **Task:** Replace the merged Mini App inbox with the approved
  device-first model and add QR pairing.
- **Files:** `backend/app/static/app/index.html`,
  `assets/app.css`, `assets/js/{telegram,api,store,ui,app}.js`,
  `backend/tests/test_miniapp_frontend_static.py`.
- **Changed:** routes are `#/` devices, `#/d/<id>` device chats, and
  `#/d/<id>/t/<thread>` threads. All conversation/contact calls use the
  new per-device API; the old device-less paths are gone. Added device
  cards with online/unread/default/archived/revoked badges, rename,
  revoke and "default for bot" actions, a pairing sheet using Telegram's
  native `showScanQrPopup` (no `getUserMedia`), manual code fallback,
  preview/approve, `?device=&thread=` deep links, per-device caches,
  device-scoped event scopes, and the existing merge/reconcile/scroll
  behavior. The composer is bound to the route's device; archived or
  revoked devices are read-only.
- **Verification:** full backend suite **564 passed, 3 failed, 1
  error**, the same four known pre-existing failures. Mini App static
  tests **38/38**; `node --check` passes on every module. The subagent
  also ran throwaway jsdom harnesses outside the repo (25/25 full flow,
  22/22 isolation/send/cursor/event scope, 10/10 scanner client
  shapes). No real Telegram client or browser test was performed; no
  deployment or service restart.
- **Decisions / risks:** the UI is untested on a real Telegram client;
  `showScanQrPopup`, the back button, theme and popup were exercised
  against stubs. The device list refreshes every 30 s because phone
  heartbeats do not bump the revision. Old `#/t/<thread>` bookmarks fall
  back to the device list. Archived/revoked devices remain readable.
- **Follow-ups:** exercise the scanner/pairing/back button in a real
  Telegram client after the backend and APK are deployed; update the
  README/agent guide (in progress); run the full suite once more after
  the documentation pass.

## 2026-11-16 — Documentation pass for the multi-device model

- **Agent:** GLM 5.3 Flash (README), MiniMax M3.1 Flash (agent guide),
  reviewed by DeepSeek V4.1 Flash (lead).
- **Task:** Make the documentation describe the implemented QR-paired,
  per-device, Mini App-first model instead of the old single-phone one.
- **Files:** `README.md`, `docs/AGENT_GUIDE.md`,
  `docs/AGENT_LOG.md`.
- **Changed:** README now covers multiple paired phones, the shared
  Telegram chat with device labels, device-first Mini App routes,
  QR pairing, force re-pairing, the archived legacy device, per-device
  contacts/chats/read markers, the new HTTP surface, the removed
  `PHONE_A_TOKEN`, the new configuration, security model, upgrade steps,
  and the reduced known-issue list. The agent guide now documents the
  pairing module, the new schema, per-device invariants, the Mini App
  and Android maps, change recipes, testing, and new traps. Test counts
  were corrected to 569 collected / 565 passing across 23 modules; the
  import batch is 100 from the app and up to 200 at the backend; stale
  commit/Compose references were removed; the bot's legacy-contact
  fallback was added as a known issue/trap.
- **Verification:** final full suite **565 passed, 3 failed, 1 error**
  (the same four pre-existing failures: two contact-search tests, one
  chat-search test, and the shutdown-timing teardown error). Mini App
  static tests 38/38; `node --check` on every JS module; `git diff
  --check` clean; `bash -n deploy/install.sh` clean. `PHONE_A_TOKEN`
  appears nowhere in the README or deploy template; the guide mentions
  it only as removed. No deployment, restart, env, or live-DB change.
- **Decisions / risks:** documentation is source-based, but the bot's
  contact picker still has a legacy global fallback. The Mini App and
  Android flows were not exercised in a real Telegram client or on a
  device; the APK was not built.
- **Follow-ups:** deploy only together with a new APK and a re-pair;
  run the manual Telegram/Android checks; make the bot contact search
  device-aware; remove `PHONE_A_TOKEN` from the live env file during
  the upgrade.

## 2026-11-16 — GitHub Actions APK build and rolling release

- **Agent:** DeepSeek V4.1 Flash (OpenCode, lead), with GitHub Actions
  as the builder.
- **Task:** The operator asked to push the APK to GitHub, ensure it
  builds without errors, provide a download URL, and make that
  mandatory on every Android edit.
- **Files:** `.github/workflows/android-build.yml`, `AGENTS.md`,
  `README.md`, `.gitignore`, `docs/AGENT_LOG.md`.
- **Changed:** the workflow now has `contents: write`, a serialized
  `android-latest` concurrency group, and a release step that stages the
  APK as `sms-relay-debug.apk` and publishes it on the rolling
  `android-latest` GitHub release (replacing the old asset). The
  project rules and README now require every Android change to build in
  GitHub Actions and publish that asset; Actions artifact URLs are not
  accepted as downloads. The local `.serena/` cache is ignored.
- **Verification:** pushed the implementation and workflow commits
  (`48e2bb5`, `202bf34`); workflow run `43921509808` completed with
  `success`; the release has one asset, `sms-relay-debug.apk`
  (7,315,405 bytes, state `uploaded`). An anonymous download returned
  HTTP 200 and the file is a valid APK zip (909 entries). No local
  Android build, deployment, or service change was performed.
- **Decisions / risks:** the rolling release is marked prerelease and
  its URL is stable; the APK is a debug build signed with the debug
  keystore, so installing over a release-signed build may require
  uninstalling the old app. The workflow triggers only for changes
  under `android-app/` or the workflow itself.
- **Follow-ups:** install the APK on a phone and run the pairing,
  default-SMS, upload and command checks; keep the release URL in the
  rule.

## 2026-11-16 — Production deployment of the multi-device build

- **Agent:** DeepSeek V4.1 Flash (OpenCode, lead), with explicit
  operator authorization.
- **Task:** Deploy the new multi-device backend and Mini App bundle to
  `ai-barrow.ir/relay` after the GitHub APK release.
- **Files:** live `/opt/sms-relay/app` (new backend + static bundle),
  `/etc/sms-relay/env` (`PHONE_A_TOKEN` removed), live
  `/var/lib/sms-relay/sms.db` (additive migration), and this log.
- **Changed:** stopped `sms-relay-api`, replaced the backend/static
  bundle, copied `requirements.txt`, removed the obsolete shared phone
  token from the live env, and started the service. The additive
  migration created `devices`, `pairings`, `device_contacts`,
  `thread_reads_scoped` and the `device_id` columns. The live database
  contained no messages, commands, contacts or read markers, so no
  archived `legacy` device was needed.
- **Verification:** took an online SQLite backup, a tar of the previous
  app directory, and an env copy under `/var/lib/backups/sms-relay/`.
  Local and public `/health` return `{"ok":true}`. The public
  `/relay/app/` serves the new shell (`<base href="/relay/app/">`,
  `device-list`, `pair-sheet`). The old `PHONE_A_TOKEN` gets 401 on
  `/commands`. `POST /pair/start` returns a 32-character pairing id, a
  6-character code and a `t.me` QR payload. Telegram `getMe` succeeds
  through sing-box; admin `/status` returns `paused=false`, queue 0, no
  devices yet. No journal errors, conflict, or traceback.
- **Decisions / risks:** this is a forced re-pair deployment: the old
  APK is now unauthorized, and the new APK from the rolling release is
  required. The live database had no pre-upgrade history to preserve.
  The smoke-test pairing is unapproved and expires automatically. The
  Mini App may need to be reopened to drop an old cached bundle.
- **Follow-ups:** install the GitHub APK, pair each phone by scanning
  its QR in the Mini App, then verify default-SMS role, SMS upload,
  command delivery, and the new device-first Mini App on a real
  Telegram client. Remove the old APK from the phones.

## 2026-11-16 — Stable signing for rolling CI APKs

- **Agent:** DeepSeek V4.1 Flash (OpenCode, lead).
- **Task:** A GitHub-hosted runner would generate a different debug
  signing key on each run, so the next APK would not install over the
  previous one. Make the rolling release update-safe.
- **Files:** `.github/workflows/android-build.yml`,
  `android-app/app/build.gradle.kts`, `.gitignore`, `README.md`,
  `AGENTS.md`, `docs/AGENT_LOG.md`.
- **Changed:** generated a dedicated PKCS12 debug key and stored only
  the base64 keystore, password, alias and key password in GitHub
  Actions secrets. The workflow decodes the keystore before the build
  and passes the signing environment to Gradle; local builds without
  those variables keep the platform debug key. The keystore path is
  ignored by git. The README and binding APK rule now state that the
  signing key must not be rotated without warning, because rotation
  changes the update signature.
- **Verification:** workflow run `43923060224` completed with
  `success`; the rolling release asset `sms-relay-debug.apk` was
  replaced (7,315,405 bytes) and downloads with HTTP 200. Parsing the
  APK v2 signature block shows subject `CN = SMS Relay Debug, O = SMS
  Relay, C = US`, fingerprint
  `88:4E:7C:4E:7D:9D:6B:91:1B:0D:D7:8D:8B:0B:49:EF:36:57:45:D3:0C:47:BC:44:37:CD:44:C2:DF:1A:EC:1B`,
  not a runner-generated Android debug key. No APK or keystore was
  committed.
- **Decisions / risks:** the key is a debug key stored in Actions
  secrets; if the secrets are removed, future builds will not update
  over old installs. A build signed by a previous, different debug key
  must be uninstalled once before installing this one. The rule still
  forbids deploying an APK the workflow did not build.
- **Follow-ups:** install the latest asset, pair, and verify the
  default-SMS and upload flows; retain the four signing secrets.

## 2026-11-16 — Fix default-messaging-app role eligibility

- **Agent:** DeepSeek V4.1 Flash (OpenCode, lead).
- **Task:** The installed APK was not listed as a messaging app and could
  not be selected as the default SMS app.
- **Files:** `android-app/app/src/main/AndroidManifest.xml`,
  new `android-app/app/src/main/java/dev/smsrelay/receiver/MmsReceiver.kt`,
  `android-app/app/build.gradle.kts`, `README.md`,
  `docs/AGENT_GUIDE.md`, `docs/AGENT_LOG.md`.
- **Changed:** added the MMS/WAP-push part of Android's default-SMS role
  contract: `RECEIVE_MMS`, `RECEIVE_WAP_PUSH`, and an exported
  `WAP_PUSH_DELIVER` receiver protected by `BROADCAST_WAP_PUSH` with the
  MMS MIME type. `MmsReceiver` is a deliberate no-op because the relay
  does not implement MMS; it exists so Android and OEM role pickers list
  the app. Bumped the APK to versionCode 5 / versionName 1.3.0.
- **Verification:** GitHub Actions run `43927798131` completed with
  `success`; the rolling asset downloads with HTTP 200 (7,315,681
  bytes). The published APK's merged manifest contains `RECEIVE_MMS`,
  `RECEIVE_WAP_PUSH`, `SmsReceiver` for `SMS_DELIVER`, and `MmsReceiver`
  for `WAP_PUSH_DELIVER` with `application/vnd.wap.mms-message`; the v2
  signature is still the stable `CN = SMS Relay Debug` key. Not tested on
  a physical device.
- **Decisions / risks:** MMS is not forwarded; the receiver only
  satisfies the role contract. Some OEMs may still impose their own
  restrictions. Version 1.3.0 updates over the stable-signed 1.2.1; an
  older differently signed debug build needs one uninstall.
- **Follow-ups:** install 1.3.0, open Settings → Default apps → SMS app
  (or tap the app's button), select SMS Relay, and verify incoming SMS
  capture and upload.

## 2026-11-16 — Duplicate pairing codes, unclaimed retry, and APK 1.3.1

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode, subagent).
- **Task:** A phone could hold two live pairing codes and an admin could
  approve the older one, leaving the phone polling a superseded code. An
  approved-but-unclaimed device row then blocked every later approval with
  `409 device is already paired`. Fix both, ship APK 1.3.1, and verify the
  build and the rolling APK.
- **Files:** `android-app/app/src/main/java/dev/smsrelay/ui/MainActivity.kt`,
  `android-app/app/src/main/res/values/strings.xml`,
  `android-app/app/build.gradle.kts`, `backend/app/db.py`,
  `backend/app/pairing.py`, `backend/tests/test_pairing.py`, `README.md`,
  `docs/AGENT_GUIDE.md`, `docs/AGENT_LOG.md`.
- **Changed:** `pairing_create` now marks older `pending`/`approved`
  pairings of the same device `cancelled` before inserting the new code, so
  an old QR cannot be approved after the phone moved on (the claim and
  approve paths answer `410 pairing expired or replaced`). `pair_approve`
  distinguishes an active device (has `token_hash`, not revoked → `409`)
  from an unclaimed row (no token, e.g. the previous approval expired
  before the phone claimed it), which may now be approved again. Pairing
  TTL stays 600 s. On the phone, the Pair button resumes a live pairing
  instead of silently minting a second code, `hasActivePairing()` checks
  state + `pairingExpiresAt`, and the New-code button asks for confirmation
  before replacing a live code. Version bumped to versionCode 6 /
  versionName 1.3.1.
- **Verification:** the push build ran as GitHub Actions run
  `43932205731` (`push`, status `completed`, conclusion `success`; all
  steps green, including "Install the stable debug signing key",
  "Assemble debug APK", and "Publish rolling APK release"). The earlier
  "Cancelled" line was the polling command, not the workflow. Release
  `android-latest` has exactly one asset, `sms-relay-debug.apk`
  (`state uploaded`, 7,317,165 bytes). An anonymous download returns
  HTTP 200 with that same length. Androguard: package `dev.smsrelay`,
  versionName `1.3.1`, versionCode `6`; `RECEIVE_MMS` and
  `RECEIVE_WAP_PUSH` present; `MmsReceiver` handles `WAP_PUSH_DELIVER`,
  `SmsReceiver` handles `SMS_DELIVER`, `ComposeSmsActivity` handles
  `SENDTO`, `RespondViaMessageService` handles `RESPOND_VIA_MESSAGE`; the
  dex contains the new `hasActivePairing`, `confirmNewPairingCode`, and
  `startNewPairing` methods. The APK is v2-signed only (no v1/v3) by
  subject `CN = SMS Relay Debug, O = SMS Relay, C = US`, SHA-256
  `88:4E:7C:4E:7D:9D:6B:91:1B:0D:D7:8D:8B:0B:49:EF:36:57:45:D3:0C:47:BC:44:37:CD:44:C2:DF:1A:EC:1B`
  — the same stable key as the previous release, so 1.3.1 updates in place.
  `backend/tests/test_pairing.py` passes (46 tests). Live read-only checks:
  `systemctl is-active sms-relay-api` → `active`, `GET /health` on
  `127.0.0.1:8090` → `{"ok":true}`, and the deployed
  `/opt/sms-relay/app` copies of `pairing.py`/`db.py` are byte-identical to
  this repository, so the pairing fix is live. The DB shows no pending or
  approved pairing for the Xiaomi device: its two 1.3.0 pairings are
  `expired` (one was approved but never claimed), and `pairing_cleanup`
  already deleted its unclaimed device row, so there is no Xiaomi row in
  `devices` to inspect. The only pending pairing belongs to a `deploy-test`
  device from the deploy smoke test and expires on its own.
- **Decisions / risks:** the supersede rule is device-scoped, so a phone
  that starts a code after a reset still only has one live code. An
  approved-but-unclaimed row is now reusable, which means an approval can
  be repeated until the TTL passes; that is intentional and bounded by the
  600 s TTL. Cancelling the older pairing is not a security downgrade: it
  only removes validity, and the claim/approve paths already refuse
  cancelled rows. The APK is debug-signed; it is not a Play release.
- **Follow-ups:** on the phone, install 1.3.1, open pairing, generate a
  code, and approve that new code in the Mini App — the admin must do the
  approval; this agent did not approve anything. Then confirm the device
  row gains a token, check default-SMS selection, and send one test SMS
  end to end. The test driver in `tests/test_pairing.py` covers the
  supersede and unclaimed-retry cases, not the UI flow on a real handset.

## 2026-11-16 — DeepSeek-only subagent delegation policy

- **Agent:** DeepSeek V4.1 Flash (OpenCode, subagent).
- **Task:** The operator required every subagent to use the DeepSeek V4.1
  Flash model from the two `100milionseek` providers, with the lead session
  restricted to planning, decomposing, assigning, and reviewing work.
- **Files:** `/opt/sms-relay-src/AGENTS.md`,
  `/root/.config/opencode/AGENTS.md` (global, outside the repository),
  `docs/AGENT_LOG.md`.
- **Changed:** replaced the old GLM/MiniMax difficulty table in the "Lead
  delegation policy (binding)" section with an explicit DeepSeek-only rule:
  every subagent must use `100milionseek/deepseek-v4.1-flash-free` or
  `100milionseek-2/deepseek-v4.1-flash-free`, alternating the providers for
  parallel work or when one is rate-limited, and MiniMax, GLM, Claude, GPT,
  or any other model or provider is prohibited unless the operator
  explicitly changes the rule. Added the rule that subagents perform all
  implementation, verification, build, documentation, and log work while the
  lead only plans, assigns, reviews, and reports. Noted that irreversible
  actions still need explicit operator confirmation. Retained the APK
  build/release rule and the mandatory append-only log rule unchanged. The
  same policy text was added to the global OpenCode instructions outside
  this repository.
- **Verification:** `grep -Ein
  'glm|minimax|claude|gpt|opencode-go' AGENTS.md` returns only the
  prohibition sentence, so no stale model assignments or difficulty table
  remain; older entries in this log still name the models that were used at
  the time, which is correct because the log is append-only. `git status
  --short` shows only `AGENTS.md` modified before the
  log entry, and only `AGENTS.md` plus `docs/AGENT_LOG.md` after it; no code,
  README, `docs/AGENT_GUIDE.md`, deploy file, workflow, or live service or
  database change was made.
- **Decisions / risks:** the rule is an instruction, not an enforcement
  mechanism, so a session that ignores `AGENTS.md` can still pick another
  model. Both `100milionseek` providers serve the same DeepSeek V4.1 Flash
  model, so alternating them changes routing and rate limits, not model
  identity; free-tier availability and rate limits are the single point of
  failure for delegated work. No Android workflow was triggered because
  these paths do not match its filter.
- **Follow-ups:** use exactly these two provider/model IDs for every future
  subagent in this project, and re-read `AGENTS.md` when starting a session
  so the policy applies from the first delegation.
