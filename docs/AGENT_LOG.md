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

## 2026-11-16 — Import sent SMS as well as received ones

- **Agent:** DeepSeek V4.1 Flash (OpenCode, subagent).
- **Task:** The historical import read only the phone's inbox, so every
  conversation in the Mini App showed only one side of the exchange. The
  operator asked for the phone's Sent folder to be imported too, as history,
  without ever turning old outbound messages into work for a phone or into
  Telegram messages.
- **Files:** `backend/app/importer.py`, `backend/app/db.py`,
  `backend/tests/test_import.py`, `backend/tests/test_device_isolation.py`,
  `README.md`, `docs/AGENT_GUIDE.md`, `docs/AGENT_LOG.md`; the Android side
  (`android-app/app/src/main/java/dev/smsrelay/work/ImportWorker.kt`,
  `net/RelayClient.kt`, `SettingsStore.kt`,
  `src/main/res/values/strings.xml`, `app/build.gradle.kts`) was committed
  separately as `a4f8f0a`.
- **Changed:** `ImportItem` gained `direction` (`"in"`/`"out"`, default
  `"in"` so an older APK keeps working). An inbound item is still a
  `messages` row with `source='import'` and `state='received'`; an outbound
  item is now written to `sms_commands` by the new
  `db.record_imported_command()` with `source='import'`, terminal
  `status='sent'`, and `result`/`reply_to_telegram_id` NULL, so
  `pending_commands()` (which filters `status='pending'`) can never hand it
  to a phone and no delivery path reads it. Import ids are deterministic and
  now include the device and the direction, so two phones may reuse a
  provider row id and an inbound and an outbound message of the same
  conversation cannot collapse. Dedupe is server-side and per device:
  `db.find_import_duplicate()` for inbound and the new
  `db.find_import_command_duplicate()` for outbound, both a ±60 s window on
  identical text in one conversation; the outbound check matches any
  `sms_commands` row of the device, so a message the relay queued and the
  phone later re-imported from Sent is one row. An import bumps `rev` once
  per request (device scope, no thread) only when something was inserted.
  Android now queries `content://sms` (not `Sms.Inbox`) with `TYPE` in the
  projection and `TYPE IN (INBOX, SENT)` in the selection, maps
  `MESSAGE_TYPE_SENT` to `"out"`, and adds `SettingsStore.importCursorFormat`
  (0 by default, 2 in this build) so an inbox-only install restarts its
  import from row 0 exactly once. APK bumped to versionCode 7 /
  versionName 1.3.2, and the import strings mention received and sent
  messages. README flow 4 and the guide's importer contract, data model,
  message-state diagram, Android map and trap list were updated.
- **Verification:** `cd backend && /opt/sms-relay/venv/bin/python -m pytest
  tests -q` collected 578 tests: **575 passed**, 3 failed and 1 error — the
  same four pre-existing failures in `test_commands.py` (contact search,
  uploaded contacts) and `test_db.py` (chat search), plus the
  `test_app_assembly.py` shutdown error. `pytest` was not present in the
  live venv, so it was installed there for the run only and uninstalled
  afterwards (`pip uninstall pytest pluggy iniconfig packaging`); the
  service was not restarted and `/health` returned `{"ok":true}` before and
  after. The new tests cover: direction
  defaulting, an outbound import landing in `sms_commands` with
  `status='sent'` and no job or Telegram id, replay as duplicate, overlap
  with a relay-queued command, the outbound row visible in the Mini App
  timeline as `out`, and two devices sharing a provider id. `GET
  /health` on `127.0.0.1:8090` returns `{"ok":true}` HTTP 200; the service
  was **not** restarted or redeployed by this entry. `cmp` shows
  `/opt/sms-relay/app/app/importer.py` and `/opt/sms-relay/app/app/db.py`
  byte-identical to the repository versions, i.e. the deployed code already
  is this change. Android build run `43944337415` finished successfully and
  published `sms-relay-debug.apk` for 1.3.2 with the stable debug key; that
  build and the APK came from CI, not from this host. The live database had
  29,418 imported messages before this change and no sent import had run
  yet, because no APK 1.3.2 is installed on the phone.
- **Decisions / risks:** the first import after 1.3.2 re-reads the whole
  provider from the start, so it can be long and can report many
  duplicates; server-side dedupe makes the inbound replay a no-op, but the
  ±60 s window can still merge two genuinely distinct identical texts inside
  a minute in either direction. Imported outbound rows are terminal history:
  they never authorize a fallback, never produce a status message and can
  never be edited or retried. MMS is not imported, so picture messages stay
  missing from both sides. Two phones that imported the same conversation
  each keep their own copy, because everything is device-scoped. The
  outbound dedupe matches on canonical thread key, so a sender form that
  normalizes to the same key is treated as the same conversation — that is
  intended, but it means an imported Sent row can be considered a duplicate
  of a relay command sent from a different SIM.
- **Follow-ups:** install APK 1.3.2 on the phone and run the import; then
  confirm in the Mini App that device threads now show sent rows on the
  right with `status='sent'`, that no imported outbound row appears in
  `GET /commands`, and that `/status` queue depth stays unchanged. Expect
  the first run to re-read the whole store; if it stalls, check the 413 /
  batch-size trap before touching the cursor. Consider a separate log entry
  once the sent rows actually exist, since this entry can only verify the
  code path, not the live import.


## 2026-11-16 — Close the native QR scanner after a scan

- **Agent:** DeepSeek V4.1 Flash (OpenCode, subagent).
- **Task:** The Mini App's pairing sheet opened Telegram's native QR
  scanner through `showScanQrPopup()` but never dismissed it, so after a
  code was read the camera preview kept running over the Mini App. The
  scanned text was resolved correctly; only the popup stayed open.
- **Files:** `backend/app/static/app/assets/js/telegram.js`,
  `backend/app/static/app/assets/js/app.js`,
  `backend/app/static/app/assets/js/api.js`,
  `backend/app/static/app/assets/app.js`,
  `backend/app/static/app/index.html`,
  `backend/tests/test_miniapp_frontend_static.py`,
  `docs/AGENT_LOG.md`.
- **Changed:** `telegram.js` gained a private `closeScanQrPopup()` helper
  that feature-detects `webApp.closeScanQrPopup` (`typeof
  webApp.closeScanQrPopup !== "function"` returns early) and wraps the
  call in `try/catch`, so an older client or a popup the client already
  closed cannot turn a successful scan into a failure. `showScanQr()`'s
  `finish()` now computes the scanned string first, calls the helper only
  when that string is non-empty, and then resolves the same value — the
  text is never lost and the cancel path (`scanQrPopupClosed` →
  `finish(null)`) does not close anything a second time. A version query
  `v=20261116-scan` was added to the `index.html` modulepreload and module
  entry, to the entry's `import "./js/app.js?..."`, and to the
  `./telegram.js` specifier in both `assets/js/app.js` and
  `assets/js/api.js`, so exactly one cache-busted copy of the wrapper loads
  in an existing Telegram WebView. No other module specifier was touched.
- **Verification:** `node --input-type=module --check` on all six bundle
  files (entry plus the five modules) passed. `cd backend &&
  /tmp/opencode/venv311/bin/python -m pytest
  tests/test_miniapp_frontend_static.py tests/test_miniapp_shell.py -q`
  gave **40 passed**. The new assertions require `closeScanQrPopup` in the
  scanner path, its feature detection and its `try/catch`, the unchanged
  `showScanQrPopup(params, callback)` signature, the version query on the
  shell entry and modulepreload, and that only `./telegram.js` carries it.
  The changed static files were copied to
  `/opt/sms-relay/app/app/static/app/` with paths preserved and the service
  was **not** restarted. Live checks after the copy:
  `https://ai-barrow.ir/relay/app/` serves
  `assets/app.js?v=20261116-scan`,
  `https://ai-barrow.ir/relay/app/assets/js/telegram.js?v=20261116-scan`
  serves `closeScanQrPopup`, `systemctl is-active sms-relay-api` is
  `active`, and `GET /health` on `127.0.0.1:8090` returns `{"ok":true}`.
- **Decisions / risks:** `closeScanQrPopup()` is a documented Telegram
  WebApp method, but a client that does not implement it keeps its old
  camera behaviour — the fix is a no-op there, and the guard means no error
  surfaces. Nothing server-side changed: the popup is client UI only, so a
  queued call or a message state cannot be affected. The version query is
  a manual cache-buster, so a future change to the wrapper needs a new
  value or installed WebViews keep the old module.
- **Follow-ups:** reopen the Mini App in Telegram (or reload the WebView)
  so the cache-busted entry is fetched, then pair a device and confirm the
  camera preview disappears as soon as the code is read. If it stays open
  on a specific client version, treat it as that client lacking
  `closeScanQrPopup` rather than a regression in the wrapper.

## 2026-11-16 — Pair a phone from its six-character code alone
- **Agent:** DeepSeek V4.1 Flash (OpenCode subagent).
- **Task:** The phone shows a QR code and a six-character code, but the Mini
  App's manual "Add device" input only accepted the full
  `pair_<pairing_id>_<scan_code>` payload or a t.me link. A bare code failed
  because the Mini App had no pairing id to send. Typing the displayed code
  has to work exactly like a scan.
- **Files:** `backend/app/pairing.py`, `backend/app/db.py`,
  `backend/tests/test_pairing.py`,
  `backend/tests/test_miniapp_frontend_static.py`,
  `backend/app/static/app/assets/js/telegram.js`,
  `backend/app/static/app/assets/js/store.js`,
  `backend/app/static/app/assets/js/app.js`,
  `backend/app/static/app/assets/js/api.js`,
  `backend/app/static/app/assets/app.js`,
  `backend/app/static/app/index.html`, `README.md`,
  `docs/AGENT_GUIDE.md`, `docs/AGENT_LOG.md`.
- **Changed:** `db.pairing_by_scan_code(conn, scan_code, now)` hashes the
  normalized code once and compares it with `hmac.compare_digest` against
  every `pending`/`approved` pairing that has not expired; cancelled,
  expired and claimed rows are never candidates, so an old code cannot be
  revived through this path. A short code can collide, so it returns all
  matches and the caller decides. `POST /app/api/pair/lookup` in
  `build_miniapp_router` (behind `require_miniapp`) takes
  `{"scan_code": ...}`, trims, drops spaces and hyphens, uppercases, accepts
  1..12 characters, and answers the same fields as
  `/app/api/pair/preview`: `pairing_id`, `device_id`, `name`, `platform`,
  `app_version`, `expires_at`, `status`. A miss is the generic `404 unknown
  pairing`; two live pairings with the same code are `409 ambiguous code`
  instead of a guess. The lookups are rate limited per admin user id
  (`MAX_LOOKUPS_PER_ADMIN = 20` per 60 s). `telegram.js` gained
  `bareScanCode()`, a helper separate from `pairToken()`: it strips
  whitespace and hyphens, uppercases, and accepts only exactly six
  characters of the scan alphabet, so the full payload parser is unchanged.
  `store.pairLookup(scanCode)` posts `pair/lookup`. `previewPairing()` tries
  `tg.pairToken()` first, and only when that fails tries
  `tg.bareScanCode()`, resolves the code through `store.pairLookup()`, sets
  `pairDraft = {pairingId: found.pairing_id, scanCode: code}` and renders
  the preview with `found` plus the typed `scan_code`; the "not a pairing
  code" error is only shown when neither path works. Approval keeps using
  `pairDraft.pairingId` and `pairDraft.scanCode`. The pairing sheet copy,
  the input placeholder (`ABC123 or pair_…`) and the 404/409 messages now
  name the typed code. The cache-buster moved from `20261116-scan` to
  `20261116-pair` because `telegram.js` changed again.
- **Verification:** `cd backend && /tmp/opencode/venv311/bin/python -m pytest
  tests/test_pairing.py -q` → **55 passed** (11 new tests: bare code,
  lowercase/spaced/hyphenated code, wrong code 404, expired code 404,
  cancelled code 404, approved status, seeded collision 409, 401 without the
  admin credential, and the 20/min limit). The full suite gave **589 passed,
  3 failed, 1 error** — exactly the four known pre-existing failures
  (`test_commands` ×2, `test_db` chat search, `test_app_assembly` shutdown)
  and nothing new. `tests/test_miniapp_frontend_static.py` plus
  `test_miniapp_shell.py` → **43 passed**. `node --input-type=module --check`
  passed on all five modules and the entry; a small Node harness confirmed
  `bareScanCode("  abc-2w3 ") == "ABC2W3"`, rejected `ABC123`/`ABC1O3`/
  a full payload, and that `pairToken` still parses both the raw payload and
  a t.me link. Live: an online backup of `/var/lib/sms-relay/sms.db` was
  taken to `/var/backups/sms-relay/` (integrity `ok`, 7 pairings) before the
  change; only `pairing.py` and `db.py` were installed to
  `/opt/sms-relay/app/app/`, stale `__pycache__` was removed, and
  `sms-relay-api` was restarted (`active`, `/health` → `{"ok":true}`, zero
  journal errors). The static files were copied with paths preserved and no
  restart. Public checks: `https://ai-barrow.ir/relay/app/` serves
  `v=20261116-pair`, the `six-character code` copy and the
  `ABC123 or pair_…` placeholder; `assets/js/telegram.js` serves
  `bareScanCode`; `assets/js/store.js` serves `pair/lookup`; the deployed
  OpenAPI lists `/app/api/pair/lookup`; the endpoint answers `401` without
  initData (as designed) and was **not** called with faked credentials.
  `db.pairing_by_scan_code` was exercised against a copy of the live DB
  (running a real lookup would have needed a live code and faked initData).
- **Decisions / risks:** A six-character code has ~1.07e9 combinations, so a
  collision between two live pairings is possible; that is answered with
  `409` and the admin must use the full payload, never a guess. The lookup
  limit is keyed by the admin user id, and behind the proxy everyone shares
  one address, so an admin who retries quickly can hit `429`; the window is
  one minute. The lookup reveals nothing beyond what a correct code already
  proves: unknown, expired and cancelled codes all give the same `404`. The
  full payload and link path is untouched, and a lookup does not change the
  pairing state. Only the Mini App static files changed; the Android APK was
  not touched and no Android workflow was triggered.
- **Follow-ups:** If `409 ambiguous code` ever shows up in practice, consider
  lengthening the scan code; that is a server-side `_SCAN_CODE_LEN` change
  and the phone renders whatever it is given, so no APK update is needed.
- **Commit:** see the pushed commit on `main` for this entry.

## 2026-11-16 — Duplicate inbound SMS: diagnosis, Android 1.3.3 guard, and the notification-only Telegram redesign

This task delivered two related changes: the Android duplicate-capture fix
(1.3.3) and the backend notification redesign that stops sending SMS bodies
to Telegram. They are logged under separate headings below.

### Duplicate inbound SMS: diagnosis and the 1.3.3 receiver guard

- **Agent:** DeepSeek V4.1 Flash (OpenCode subagent).
- **Task:** Inbound SMS appeared twice in Telegram and in the Mini App. Two
  sources of duplication had to be separated: a live capture duplicate and
  the bulk historical re-import, and the live one had to be stopped without
  losing genuinely repeated messages.
- **Files:** `android-app/app/src/main/java/dev/smsrelay/receiver/SmsReceiver.kt`,
  `android-app/app/build.gradle.kts`, `docs/AGENT_LOG.md`.
- **Changed:** `SmsReceiver` now returns early when the broadcast is
  `SMS_RECEIVED` and this app holds the default-SMS-app role
  (`if (!isDefaultSmsApp && RoleState.isDefaultSmsApp(context)) return`),
  so `SMS_DELIVER` is the single canonical copy. Vendor builds (MIUI among
  them) deliver both broadcasts to the default app; both copies were stored
  with `source='sms'` and both were uploaded, because the local dedupe keeps
  same-source repeats on purpose (two genuine "ok" messages must both
  survive). The non-default case — where `SMS_RECEIVED` is the only
  broadcast — and the cross-path dedupe are unchanged. Version bumped to
  `versionCode 8` / `versionName 1.3.3`. Commit `149504e`.
- **Verification:** diagnosis against the live database copy: **1** extra
  live pair was attributable to the dual broadcast, and the bulk re-import
  produced **1,039 duplicate groups / 5,039 rows**, none of which were ever
  forwarded to Telegram (they are imported history). The APK was built by
  GitHub Actions run **43960881846**, which finished clean and published
  `sms-relay-debug.apk` on the rolling `android-latest` release; the asset
  was downloaded and its contents verified at
  <https://github.com/HessiKz/sms-relay/releases/download/android-latest/sms-relay-debug.apk>.
  This was not built on the VPS (no Android toolchain there).
- **Decisions / risks:** The fix is deliberately in the receiver, not in the
  local dedupe: weakening same-source dedupe would silently drop real
  repeats. A vendor build that sends only `SMS_RECEIVED` while the app is the
  default app would lose that message — no such build is known, and the
  platform contract delivers `SMS_DELIVER` to the default app. **The existing
  duplicate rows were not cleaned up**: the live pair and the 1,039 imported
  groups are still in the database.
- **Follow-ups:** Installing 1.3.3 stops new live duplicates, but existing
  duplicate rows remain. Cleaning them is a separate, deliberate data
  operation on the live database (not performed here) and needs its own
  backup and confirmation.

### Notification-only Telegram delivery with a deletion reaper

- **Agent:** DeepSeek V4.1 Flash (OpenCode subagent).
- **Task:** Every forwarded SMS put the whole message body into the Telegram
  chat, which is server-mediated and keeps the text forever. The chat should
  be a heads-up only; the content belongs in the Mini App.
- **Files:** `backend/app/db.py`, `backend/app/delivery.py`,
  `backend/app/tg.py`, `backend/app/main.py`,
  `backend/tests/test_delivery.py`, `backend/tests/test_db.py`,
  `backend/tests/test_miniapp_inbox.py`, `backend/tests/test_tg.py`,
  `backend/tests/test_app_assembly.py`, `README.md`, `docs/AGENT_GUIDE.md`,
  `docs/AGENT_LOG.md`.
- **Changed:** `DeliveryWorker` no longer chunks and no longer sends
  `messages.body`; it posts one notification —
  `📥 New SMS`, `📱 <device name>`, the sender (device contact name, then the
  name the phone attached, then the normalized number), the UTC timestamp,
  and one `Open chat` `web_app` inline button built by
  `delivery.thread_url(miniapp_url, thread_key, device_id)`. Device and
  contact lookups degrade to `Unknown device` / raw sender instead of failing
  the delivery. The obsolete `MAX_CHUNK_LEN`, `_split_html()` and `_chunks()`
  were removed with it. `messages` gained `seen_at`, `telegram_sent_at`,
  `telegram_delete_at`, `telegram_deleted_at` (additive `_ensure_column`
  migrations plus the partial index `idx_messages_tg_delete`);
  `telegram_msg_id` is kept and still powers reply-to-SMS and the
  `details:`/`showmsg:` callbacks. `db.mark_message_notified()` records the
  send and writes the deletion deadline: `sent_at + 86400` by default,
  `sent_at + 300` when the message already has `seen_at`.
  `db.mark_thread_read()` now sets `seen_at = COALESCE(seen_at, now)` for the
  rows the read cursor covers and pulls that device/thread's notification
  deadlines forward to `now + 300` (never back). `db.due_notifications()`
  selects only rows with `telegram_sent_at`/`telegram_msg_id` set and no
  `telegram_deleted_at`, excluding imports. `tg.delete_message()` with
  `_interpret_ok()` treats a 2xx with `ok: true` **and** a 400
  "message to delete not found" as success. `delivery.NotificationReaper`
  runs every `REAPER_INTERVAL_S = 30` s on up to `REAPER_BATCH = 20` rows,
  clears the pointer via `db.mark_notification_deleted()` on success or a
  bad id, and calls `db.reschedule_notification()` (+`REAPER_RETRY_S = 300` s)
  on a transient `TGError` or unexpected exception, so no row spins the loop.
  `main.create_app()` starts the reaper next to the delivery worker, only
  when `run_workers=True`.
- **Verification:** full backend suite
  `/tmp/opencode/venv311/bin/python -m pytest tests -q` →
  **629 passed, 3 failed, 1 error** (632 collected), i.e. exactly the four
  known pre-existing failures (`test_commands` ×2, `test_db` chat search,
  `test_app_assembly` shutdown timing) and nothing new. `git diff --check`
  is clean. Before deployment an online backup of the live database was taken
  to `/var/backups/sms-relay/sms-relay-20261116-220253-pre-notify.db`
  (integrity `ok`) and the backend files were installed to
  `/opt/sms-relay/app/app/`; `systemctl is-active sms-relay-api` is `active`
  and `/health` returns `{"ok":true}`. **No live test notification was
  sent**, and the service was not restarted as part of this documentation
  task. The live `db.py`, `delivery.py`, `tg.py`, `main.py` were confirmed
  byte-identical to the repository.
- **Decisions / risks:** Old forwarded messages stay in the chat as full
  bodies and are never touched by the reaper (they have no
  `telegram_sent_at`), and Telegram cannot delete anything older than 48 h
  anyway — so the default deadline is 24 h, safely inside that limit.
  Deleting a notification keeps the message row and its body; only the
  Telegram pointer and deadline are cleared. Reply-to-SMS and the
  `details:`/`showmsg:` callbacks rely on `telegram_msg_id`, so once a
  notification is reaped there is nothing to reply to; that is intended.
  A notification whose delete is permanently rejected (wrong bot rights, chat
  migrated) is rescheduled every 300 s forever — one indexed row, not a
  crash, but it never disappears. The reaper only runs when the workers are
  enabled, so `create_app(run_workers=False)` never deletes anything.
- **Follow-ups:** Existing duplicate rows (1,039 imported groups / 5,039 rows
  and the one live pair) are still present; cleanup is pending and was **not**
  performed. Watch the journal for `reaper rescheduled` after a day of live
  traffic to confirm the default 24 h deadline deletes cleanly, and verify
  the bot keeps the delete right in the admin chat.
- **Commit:** see the pushed commit on `main` for this entry.

## 2026-11-17 — Dialer wave 1: call-log sync and the dialpad

- **Agent:** DeepSeek V4.1 Flash (OpenCode), lead/worker delegation.
- **Task:** Add wave 1 of the dialer feature to the Android app: capture the
  phone's call log, sync it to the new backend `/calls/*` routes, and give the
  app a working dialpad plus `tel:` / `ACTION_DIAL` handling — **without**
  declaring an `InCallService` and **without** requesting `ROLE_DIALER`, so the
  platform dialer keeps the in-call UI and the app stays uninstallable.
- **Files:** `android-app/app/src/main/AndroidManifest.xml`;
  `.../java/dev/smsrelay/SettingsStore.kt`, `RelayApp.kt`,
  `net/RelayClient.kt`, `ui/MainActivity.kt`, `util/RoleState.kt`,
  `work/Schedule.kt`; new `.../java/dev/smsrelay/calls/` package
  (`CallLogEntity.kt`, `CallLogDao.kt`, `CallLogDatabase.kt`,
  `CallLogReader.kt`, `CallLogWorker.kt`, `DialerActivity.kt`,
  `CallDeepLinks.kt`); new `res/layout/activity_dialer.xml`;
  `res/layout/activity_main.xml`; `res/values/strings.xml`. Backend routes in
  `backend/app/calls.py` (+ `backend/app/main.py` wiring) are from the
  companion backend task, not this one.
- **Changed:** four permissions (`READ_CALL_LOG`, `WRITE_CALL_LOG`,
  `CALL_PHONE`, `ANSWER_PHONE_CALLS`); `DialerActivity` exported with
  `ACTION_DIAL`+`DEFAULT` and `VIEW`+`tel` filters and **no** `ACTION_MAIN`;
  a `<queries>` block for the call-via schemes; `RoleState.isDialerRoleAvailable`
  / `isDialerRoleHeld` (read-only, never requests the role). A separate
  `CallLogDatabase` (version 1, no destructive migration) holds the upload
  queue. `CallLogWorker` forwards `_ID` above the stored cursor in batches of
  100 up to `LIMIT 100`, then re-reads a rolling 7-day `DATE` window
  (`LIMIT 200`) and upserts by `contentHash`; 401 queues and retries, any other
  4xx marks the row failed and still advances the cursor, and 429/5xx/network
  stop without advancing. `MainActivity` gained the "Calls" card, the runtime
  permission launcher and the sync state renderer; `Schedule` runs the sync
  every 15 minutes with `KEEP`. The dialpad works fully offline and sends
  `110/112/115/125/911/999/000` straight through with no SIM picker and no
  backend call.
- **Verification:** `python3` XML parse of the manifest, both edited layouts
  and `strings.xml` → OK; a cross-check script confirmed every `R.id` used by
  `DialerActivity` exists in `activity_dialer.xml` and every `R.string`
  reference in the new package and `MainActivity` resolves (the single hit,
  `R.string.cancel` in `MainActivity`, is a false positive: the line is
  `android.R.string.cancel`); `git diff --check` clean; `fallbackToDestructiveMigration`
  absent from the tree; backend contract tests
  `/tmp/opencode/venv311/bin/python -m pytest tests/test_calls.py
  tests/test_call_actions.py -q` → **41 passed**. **The Android app was not
  compiled**: this host has no JDK or Gradle, so no APK exists. Nothing was
  committed, pushed, deployed or restarted.
- **Decisions / risks:** `CallLogEntity.id` is the placeholder
  `local-<providerId>`, not a hash, so a provider-rewritten row keeps its
  identity across the recent window; `contentHash` is indexed but **not**
  unique and identity lookups go through `findByProvider`. The recent window
  re-queues a changed row unless it was already `FAILED`, which stays failed
  to avoid re-spending requests. `openSystemDialer()` names
  `TelecomManager.defaultDialerPackage` explicitly so the app cannot loop back
  into `DialerActivity`, and it returns false when this app already is the
  default dialer. `postCallEvent` and the sync map `is_read`/`muted`/`speaker`
  to `1`/`0`, while the backend model declares them `bool | None` — pydantic
  coerces `0`/`1` fine, but a stricter reader would reject it. The dialpad
  hides every call-via entry on API 30+ unless the `<queries>` block is
  present. Wave 1 is client-side only if the backend routes are not deployed.
- **Follow-ups:** the APK must be built by the `android-build` GitHub Actions
  workflow, which publishes `sms-relay-debug.apk` on the `android-latest`
  release; no download URL is valid until that workflow runs. Wave 2 must add
  the `InCallService` subclass and its `BIND_INCALL_SERVICE` manifest entry,
  the `ACTION_MAIN` launcher activity that makes the app role-eligible, the
  `RoleManager.createRequestRoleIntent(ROLE_DIALER)` flow, and the
  `CallService`/`CallActivity`/`CallController` wiring that drives
  `postCallEvent`, `pollCallActions` and `reportCallAction`. Exchange rate and
  `contentHash` collisions on the recent window deserve a real-device test.

## 2026-11-17 — Dialer wave 1: the Mini App calls surface
- **Agent:** DeepSeek V4.1 Flash (OpenCode), lead/worker delegation.
- **Task:** Add the device-scoped Calls surface to the Telegram Mini App —
  call history tab, live call card with actions, per-call sheet, call-via
  sheet, keypad, and call events in the thread timeline — coded against
  `docs/CALLS_DESIGN.md` while the backend `/calls/*` routes land in parallel.
- **Files:** `backend/app/static/app/assets/js/api.js`, `store.js`, `ui.js`,
  `app.js`; `backend/app/static/app/assets/app.css`;
  `backend/app/static/app/index.html` (version query only);
  `backend/tests/test_miniapp_frontend_static.py`; `docs/AGENT_GUIDE.md`
  (calls surface section and the new route row). The backend module
  `backend/app/calls.py`, `backend/app/db.py` and the Android call-log client
  come from the companion tasks, not this one.
- **Changed:** `api.js` gained `getCalls`, `getLiveCall`, `sendCallAction` and
  `markCallsRead` over the device routes `/calls`, `/calls/live`,
  `/calls/actions`, `/calls/read`. `store.js` owns the call state (list,
  cursor, query, `callsAvailable`, `liveCall`, `callActionPending`) plus
  `loadCalls`/`loadMoreCalls`/`loadLiveCall`/`sendCallAction`/
  `markCallsRead`/`adoptSummaryCall`, resets it in `selectDevice`, and
  refreshes the live call from the revision poll even when the history was
  never opened. `ui.js` builds the tab bar, live card, call list, call sheet,
  call-via sheet and keypad lazily in `ensureCallsSurface()` (no new markup in
  `index.html`), renders every server string with `textContent`, and adds a
  call bubble to the thread timeline. `app.js` adds the
  `#/d/<device>/calls` route, `callsHash()`, `openCalls`/`loadCallsScreen`,
  the delegated `onCallsClick` handler, `runLiveAction`, `sendDtmf`,
  `dialNumber`, `openCallVia` and a tab-scoped search. Emergency numbers are
  never dialled remotely and never open the call-via sheet.
- **Verification:** `cd backend && /tmp/opencode/venv311/bin/python -m pytest
  tests/test_miniapp_frontend_static.py tests/test_miniapp_shell.py -q` →
  **56 passed, 1 warning**. `node --input-type=module --check` on `api.js`,
  `store.js`, `ui.js`, `app.js` and `assets/app.js` → all OK. A throwaway DOM
  shim (`/tmp/opencode/domshim.mjs`, not repo files) executed the real modules
  against a stubbed `fetch`: 33 checks pass — tab bar and `aria-selected`,
  both call rows with title/kind/duration/SIM/unread dot and
  `data-thread-key`, the live card and its action visibility per state, the
  posted `client_ref`, `409 stale_call` hiding the card and clearing the
  pending guard, emergency detection, the call sheet note, all nine call-via
  entries with the `wa.me` URL, the call bubble beside an SMS bubble, and the
  `404` → "not available" and empty-history states. A second run confirmed a
  retry reuses the failed action's `client_ref` while a new action gets a new
  one, and that `#/d/<id>/calls` selects the Calls tab and back returns to
  Chats. Full suite: **696 passed, 3 failed, 1 error** — exactly the four
  known pre-existing failures (`test_commands` contact search ×2, `test_db`
  chat search, and the prompt-shutdown timing test). Nothing was committed,
  pushed, deployed or restarted.
- **Decisions / risks:** `index.html` stays a version bump
  (`v=20261117-calls`) because the repo forbids new server-rendered strings
  there; every node is created in `ui.js` and reconciled, so a device switch
  reuses the rows. The `calls` route reuses the chats pane so the header,
  composer and back gesture keep working unchanged. A missing `/calls`
  endpoint is treated as "this device has no calls", not an error; a missing
  `/calls/live` only hides the card. Call ids are normalised in the store
  because the server sends `event_id` for calls and `id` elsewhere. The
  action guard is module-level rather than in `state`, so a failed request
  can never strand a pending flag. `phone_account` is shown as-is with a
  conditional `SIM ` prefix, because Android already reports "SIM 1" or a
  carrier name. The surface is inert against a backend that has not deployed
  `/calls/*` yet — it degrades to the empty state, which is the intended
  Wave 1 behaviour.
- **Follow-ups:** Wave 2 must drive the live card from real `call_state`
  revisions rather than the polling fallback, add the in-call keypad tones
  through the `dtmf` action once the backend accepts a `dtmf` payload, and
  cover the "call via WhatsApp/Telegram/Signal/Bale/Rubika" URLs on a real
  device, since only `tel:` can be checked here. The Android APK must still
  be built by the `android-build` workflow and published as
  `sms-relay-debug.apk` on the `android-latest` release before any download
  URL is handed out.

---

## 2026-11-17 — UI design for a local SMS client and dialer

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode, subagent)
- **Task:** Produce a screen-by-screen design/implementation plan
  (`docs/UI_DESIGN.md`) to turn SMS Relay into a Google Messages-style local
  SMS client plus a Google Dialer-style dialer. Design document only; no
  Kotlin/XML/backend/Mini App changes, no commit or push.
- **Files:** created `docs/UI_DESIGN.md`; appended this entry to
  `docs/AGENT_LOG.md` (binding log rule). No other file touched.
- **Changed:** a concrete design covering: current UI inventory and gaps
  (setup, Room delivery history, `ACTION_SENDTO` stub, dialpad, in-call);
  target IA with routes and empty/loading/error/offline states for Messages
  home, chat thread, new conversation, dialer tabs, in-call and settings;
  a provider-based data plan (`content://sms`, `mms-sms/conversations`,
  `canonical-addresses`, read/draft writes, `ContentObserver`, additive Room
  mirror, relay consistency and MMS boundary); Material 3 component inventory;
  S/M/L phases with device-free tests; risks. Original "SMS Relay" identity,
  no copied assets, SMS only (RCS impossible).
- **Verification:** design was read against source — `AGENTS.md`,
  `docs/CALLS_DESIGN.md`, AGENT_GUIDE section 6, the `ui/`/`calls/`
  activities, all layouts, `AndroidManifest.xml`, and the `data/`, `sms/`,
  `work/` packages. `docs/UI_DESIGN.md` is 194 lines (target <220). No build,
  tests or visual verification: this host has no JDK/Gradle/emulator. No
  application code changed, so no `android-build` workflow run was required.
- **Decisions / risks:** single `ShellActivity` + fragment bottom nav chosen
  over adding a Navigation dependency, matching the existing XML/AppCompat
  style; provider is the display source of truth while Room stays the upload
  queue. Biggest risks: no local/visual verification; MMS excluded (RCS
  impossible); OEM/provider and default-role variance; visual voicemail and
  Google spam/screening have no public API; keep the "exact replica" boundary
  to layout/flow only.
- **Follow-ups:** implement phases in `docs/UI_DESIGN.md` order via
  DeepSeek V4.1 Flash subagents; any Android change must be built by the
  `android-build` workflow and published as `sms-relay-debug.apk` on
  `android-latest`, with a real-device telephony matrix before release.

---

## 2026-11-18 — Messages home and read-only thread screen (phase 1)

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode)
- **Task:** Add a Google Messages-style conversation list as the app's
  launcher screen and a read-only thread screen, both reading the phone's
  real SMS store. No composer and no send path: that is phase 2. Operator
  asked for no commit, no push, no deploy, no restart, and no edits outside
  `android-app/` apart from this log entry.
- **Files:** created
  `java/dev/smsrelay/messages/{SmsThread,SmsMessage,SmsRepository,SmsObserver}.kt`,
  `java/dev/smsrelay/ui/{ShellActivity,ThreadActivity}.kt`,
  `java/dev/smsrelay/ui/messages/{AvatarDrawable,MessageDates,MessagesBanner,ConversationAdapter,ThreadAdapter}.kt`,
  `res/layout/{activity_shell,activity_thread,item_conversation,item_message_in,item_message_out,item_day_header}.xml`,
  `res/menu/menu_shell.xml`, `res/values/dimens.xml`,
  `res/drawable/{bg_pill,bg_unread_dot,ic_back,ic_call,ic_search,ic_start_chat}.xml`;
  modified `res/values/themes.xml` (new `Theme.SmsRelay.Messages`),
  `res/values/strings.xml` (messages/thread strings), `AndroidManifest.xml`
  (launcher moved to `ShellActivity`, `ThreadActivity` added, `DialerActivity`
  keeps `ACTION_DIAL` + `tel:` but loses MAIN/LAUNCHER). `calls/`,
  `MainActivity`, `SmsReceiver`, `SmsProviderWriter`, `UploadWorker`,
  `backend/`, Mini App and other docs untouched.
- **Changed:** read-only local SMS client over
  `content://mms-sms/conversations?simple=true` (`_id, date, recipient_ids,
  snippet`, `date DESC`) with `recipient_ids` resolved through
  `content://mms-sms/canonical-addresses/<id>` (`address`), and
  `content://sms` (`_id, thread_id, address, body, date, type, read, seen,
  status, subscription_id`, `thread_id = ?`, `date DESC, _id DESC LIMIT
  n+1`). Conversation list: toolbar with `SearchView` and an overflow menu
  (Dialer, Setup), extended FAB "Start chat" over
  `CommonDataKinds.Phone.CONTENT_URI`, avatar with initials, relative dates,
  unread dot with a counter only when more than one, in-memory search,
  role/relay banners, loading/empty/"No results"/unreadable states, refresh
  on `onResume` and from a 300 ms debounced `ContentObserver`. Thread screen:
  toolbar with back, avatar, name and an `ACTION_DIAL` call button, day
  separators, in/out bubbles, failure and pending notes, a SIM note only
  when the card is still reported, "Load older messages" paging, mark-read
  on open, and a footer saying sending is the next update.
- **Verification:** no JDK/Gradle/emulator on this host, so nothing was
  compiled or run and no APK was produced. Static checks only: all 25 XML
  files under `res/` parse; every `R.id`/`R.string`/`R.layout`/`R.dimen`/
  `R.drawable`/`R.menu` reference in the new Kotlin resolves against the
  layouts and values; no duplicate string names; no `android:text="` with a
  literal in the new layouts; no unused imports or unused private fields in
  the new files; no unclosed brace or paren; exactly one MAIN/LAUNCHER pair
  (on `ShellActivity`), `ThreadActivity` unexported with
  `parentActivityName`, `tel:` filters still on `DialerActivity`; no
  `setSupportActionBar` call anywhere; `git diff --check` clean for
  `android-app/`.
- **Decisions / risks:** the relay's Room queue is never rendered — the
  provider is the display source of truth. Read-marking only runs when the
  page really holds unread inbox rows, otherwise the write would wake the
  observer in a loop. "Load older" pages by `date` plus `_id` because two
  messages can share a second. The contact picker opens the setup screen
  instead of asking for `READ_CONTACTS` out of context. The thread screen
  deliberately has no composer. Dynamic colour is applied on API 31+ only;
  `java.time` is safe at minSdk 26. Nothing was built, so a compile error
  remains possible until CI runs.
- **Follow-ups:** the `android-build` workflow must build this and publish
  `sms-relay-debug.apk` on the `android-latest` release; verify the list and
  thread on a real phone with the default-SMS role granted and revoked, on
  dual SIM, and in dark mode. Phase 2 adds the composer and the send path
  through the relay mirror.

---

## 2026-11-18 — Messages UI follow-up: groups, contact cache, MMS placeholder

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode)
- **Task:** Review fixes on the phase-1 Messages screens: stop labelling a
  group SMS thread as a private chat with its first member, stop reading the
  whole contact list on every refresh, and stop presenting an MMS-only thread
  as an empty conversation. Scope stayed the same: `messages/`,
  `ui/ShellActivity`, `ui/ThreadActivity`, `ui/messages`, the new
  layouts/strings/themes/manifest. (The repo's binding rule requires this
  entry even though the request asked for no doc edits; nothing else under
  `docs/` was touched.)
- **Files:** modified
  `java/dev/smsrelay/messages/{SmsThread,SmsRepository}.kt`,
  `java/dev/smsrelay/ui/{ShellActivity,ThreadActivity}.kt`,
  `java/dev/smsrelay/ui/messages/ConversationAdapter.kt`,
  `res/layout/activity_thread.xml`, `res/values/strings.xml`; created
  `java/dev/smsrelay/ui/messages/ThreadLabels.kt`.
- **Changed:** `resolveAddress` became `resolveRecipients`, which resolves
  every id of `RECIPIENT_IDS` into a de-duplicated `List<String>` with the
  per-call cache kept. `SmsThread` now carries `recipients`,
  `recipientNames`, a derived `isGroup`/`recipientCount`/`avatarKey`, and
  `address` is empty for a group. Group labels come from the new
  `ThreadLabels` helper ("A, B +N", else "Group (n)"); no member's number is
  ever used as a group label, and `threadIdForAddress` now skips any thread
  whose resolved recipient count is not one. `ThreadActivity` takes
  `EXTRA_IS_GROUP`/`EXTRA_LABEL`/`EXTRA_GROUP_COUNT`/`EXTRA_SNIPPET`, re-reads
  the conversation row through the new `SmsRepository.threadFor` on every
  load (so a restored screen and a changed membership are right), hides the
  call button and refuses `dial()` for a group, and shows
  `messages_mms_unsupported` when the SMS page is empty but the conversation
  snippet is not, plus the optional `messages_mms_partial` note when the
  snippet is not the newest SMS body. Contacts are cached in `SmsRepository`
  for 60 s behind a single `@Volatile` holder, keyed off
  `SystemClock.elapsedRealtime()`, cleared when `READ_CONTACTS` is missing.
  `ShellActivity` search now matches group members and the group label.
- **Verification:** no compiler, no emulator, no APK. Static only: all 25
  `res/` XML files parse; every `R.*` reference in the new Kotlin resolves;
  no duplicate string names; every owned string is defined and used; no
  hardcoded `android:text`; no unused imports; brace/paren/bracket balance
  zero in all 12 new or touched Kotlin files; no leftover `resolveAddress`,
  `thread.label` or `isFailed` call sites; all three `SmsThread(` construction
  sites pass the new fields; one MAIN/LAUNCHER, two `tel:` filters,
  `ThreadActivity` exported=false; `git diff --check` clean for
  `android-app/`.
- **Decisions / risks:** a group is identified by recipient count, not by an
  MMS type, because the conversations provider does not say whether the last
  message was MMS; the snippet comparison is the cheapest honest signal for
  the mixed case and is deliberately prefix-tolerant against provider
  truncation. `threadFor` is an extra query per thread open, but it is one
  row and it removes the "trust the extras" failure mode. Marking read is
  unchanged. Nothing was compiled, so a build error is still possible.
- **Follow-ups:** the `android-build` workflow must build this and publish
  `sms-relay-debug.apk` on `android-latest`; check a real group thread, an
  MMS-only thread and a mixed thread on a device with the default-SMS role.
## 2026-11-18 — Messages composer and local send path (phase 2)

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode)
- **Task:** Add the composer to `ThreadActivity` and make a one-to-one SMS
  leave through the SIM, land in the phone's SMS provider and be mirrored to
  the relay as outbound history. A group or an MMS-only thread must stay
  read-only with a clear hint. Backend, Mini App, `calls/`, the relay
  workers, `SmsReceiver`, `SmsSender`, `SmsProviderWriter` and `RelayClient`
  were to stay untouched.
- **Files:** modified
  `java/dev/smsrelay/ui/ThreadActivity.kt`,
  `java/dev/smsrelay/messages/SmsRepository.kt` (new `insertSent` +
  `StoredSms` + `threadIdOfRow`), `res/layout/activity_thread.xml`,
  `res/values/strings.xml`, `AndroidManifest.xml` (only
  `windowSoftInputMode` on `ThreadActivity`); created
  `res/drawable/ic_send.xml`; this log entry (the repo rule requires one even
  though the request said no doc edits; nothing else under `docs/` changed).
- **Changed:** the phase-1 footer hint is gone. `ThreadActivity` now has a
  bottom composer bar: a multiline `EditText` inside a `MaterialCardView`
  with `?attr/colorSurfaceVariant` and 22dp corners, and a 48dp circular
  send `ImageButton` on a `MaterialCardView` in `?attr/colorPrimary`, using
  the new original 24dp paper-plane vector `ic_send.xml` tinted with
  `?attr/colorOnPrimary`, plus a determinate-free `ProgressBar` spinner that
  replaces the plane while a send is in flight. The bar is padded with
  `max(systemBars.bottom, ime.bottom)` through `ViewInsetsCompat`, and the
  list is scrolled to the newest rows when the IME opens, when the field
  gains focus, and after a send (a one-shot `scrollToEndNext` flag, so the
  provider observer's own reload cannot win the race and leave the new
  bubble below the fold). `ThreadActivity` is `adjustResize` in the
  manifest. Send sequence: `SmsSender.isValidTarget` on the resolved
  `address`, then on `Dispatchers.IO` one hop that reads
  `SmsSender.resolveSubscriptionId` (for the mirror only) and calls
  `SmsSender.send(..., subscriptionId = null)`; on `Outcome.Sent`
  `SmsRepository.insertSent` writes `ADDRESS, BODY, DATE, DATE_SENT,
  TYPE=MESSAGE_TYPE_SENT, READ=1, SEEN=1, SUBSCRIPTION_ID (only when > 0)`
  into `content://sms` and reads the new row's `thread_id` back; the
  composer is cleared only after the store answers, `threadId` comes from
  the inserted row when known, and the relay mirror runs afterwards via
  `RelayClient.importBatch(ImportMessage(providerId, target, body,
  DATE/1000, subId, DIRECTION_OUT))`. A failed mirror logs a category only
  and never changes the local result; `/sms` is never called. Groups
  (`isGroup`), MMS-only threads (blank `address`, or an empty page with a
  non-blank snippet once a page has been read) and a failed read replace the
  bar with `messages_group_send_unsupported`,
  `messages_mms_send_unsupported` or `messages_error_read` in the same
  slot, and the app-wide `sendingEnabled` switch is enforced at the tap with
  `hint_sending_disabled`. A send whose provider insert fails shows
  `composer_sent_not_stored`; a refused send restores the draft and focus
  and maps the `SmsSender` reason to the existing `reason_*` strings.
  Double-send is blocked by one `sending` flag.
- **Verification:** no compiler, no emulator, no APK. Static only: all 26
  `res/` XML files parse; every `R.id`/`R.string`/`R.layout`/`R.drawable`
  reference in `ThreadActivity` resolves and every id it uses exists in
  `activity_thread.xml` (16 ids, no unused ones); no duplicate string names;
  no hardcoded `android:text` in any layout; no newline inside a string; no
  unused imports; brace/paren/bracket balance zero in `ThreadActivity.kt`
  and `SmsRepository.kt`; the only log lines are the two category strings for
  a skipped mirror; `git diff --check -- android-app/` clean; one
  MAIN/LAUNCHER in the manifest and `windowSoftInputMode` present once;
  `SmsSender.kt`, `SmsProviderWriter.kt`, `SmsReceiver.kt`,
  `ImportWorker.kt`, `backend/` and `docs/` were not modified by this task.
- **Decisions / risks:** the local row is written only after the radio
  accepts, so there is no optimistic bubble — the reload draws the real one.
  The mirror is best effort with no durable outbound queue yet, so a dropped
  mirror is missing history but never a lost message. The card sent to the
  mirror is the one `resolveSubscriptionId` reports, while the radio picks
  the card itself, so on a dual-SIM phone that changes mid-send the note may
  name a different card than the one used; a single `subscriptionId` would
  fix that in a later phase. `settings.sendingEnabled` is off by default, so
  a fresh install sees the field and a Snackbar explaining why nothing
  leaves. Nothing was compiled, so a build error is still possible.
- **Follow-ups:** the `android-build` workflow must build this and publish
  `sms-relay-debug.apk` on `android-latest`. On a device: send in a one-to-one
  thread with the relay paired and unpaired, kill the radio (no SIM) to see
  the failure path keep the draft, deny the SMS role to see the read notice,
  and confirm the bubble appears without a manual refresh.
## 2026-11-18 — Composer follow-up: local consent and blank-address guard

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode)
- **Task:** Two corrections to the phase-2 composer in `ThreadActivity`.
  First, `SettingsStore.sendingEnabled` must not gate a local human send: the
  flag exists so a remote command (Telegram, the Mini App) cannot make the
  phone send without its owner, while typing here and tapping Send is consent.
  With the default `false`, a fresh install had a composer that could never
  send. Second, a one-to-one thread with a blank/unresolved `address` could
  still show an enabled composer whose send then failed with `invalid_target`.
- **Files:** modified
  `java/dev/smsrelay/ui/ThreadActivity.kt`,
  `res/values/strings.xml`; this log entry. No other file was touched, and
  `CommandWorker`, `SmsSender`, `SettingsStore` and every remote path are
  unchanged.
- **Changed:** the `if (!settings.sendingEnabled)` early return and its
  Snackbar were removed from `sendComposer()`, replaced by a comment saying
  the check is deliberately absent and why. `hint_sending_disabled` stays
  defined and is still used four times by `MainActivity`. `isMmsOnly()` no
  longer treats a blank address as MMS; instead `renderComposer()` gained an
  explicit `address.isBlank()` branch, ordered before the MMS case, which
  replaces the bar with the new `messages_no_address` sentence ("This
  conversation has no number to send to."). `canSend()` already required
  `address.isNotBlank()`, so no enabled send button is ever shown for such a
  thread. The remaining gates are the conversation shape (group, MMS-only,
  unreadable) and, at the tap, `SmsSender.isValidTarget` plus the radio.
- **Verification:** no compiler, no emulator, no APK. Static only:
  `grep -n 'sendingEnabled' ThreadActivity.kt` returns exactly one hit, the
  explanatory comment; `canSend`/`address.isBlank`/`composer_failed` greps
  show the blank-address branch and the failure mapping intact; all 26 `res/`
  XML files parse; no missing `R.*` reference in `ThreadActivity`; no
  duplicate string names; `messages_no_address` is defined once and used
  once; no unused imports; brace/paren/bracket balance zero in
  `ThreadActivity.kt` and `SmsRepository.kt`; `git diff --check --
  android-app/` clean.
- **Decisions / risks:** the local composer now sends regardless of the
  remote-send toggle, so the product statement becomes "the switch controls
  remote sends only". A user who wants no sending at all must keep the SMS
  permission withdrawn; the read-failure notice then replaces the bar. A
  blank-address thread is reported as "no number" rather than as MMS, which
  is the honest cause. Nothing was compiled, so a build error is still
  possible.
- **Follow-ups:** the `android-build` workflow must build this and publish
  `sms-relay-debug.apk` on `android-latest`. On a device: fresh install with
  the switch off must still send from a one-to-one thread, and a thread whose
  canonical address does not resolve must show the "no number" notice with no
  send button.
## 2026-11-18 — Phone home: Favorites / Recents / Contacts / Voicemail

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode)
- **Task:** UI phase 3 of the local SMS-client work: a Google-Dialer-style
  Phone home that reads the phone's real call log and contacts, with search,
  four tabs and a dialpad FAB, without rewriting the in-call screen, the
  default-dialer role or the dialpad.
- **Files:** created
  `java/dev/smsrelay/ui/DialerHomeActivity.kt`,
  `java/dev/smsrelay/ui/dialer/{DialerRepository,DialerTabs,RecentsAdapter,ContactsAdapter,FavoritesAdapter}.kt`,
  `res/layout/{activity_dialer_home,item_recent_call,item_dialer_contact,item_dialer_favorite}.xml`,
  `res/drawable/{ic_call_made,ic_call_missed,ic_call_received,ic_voicemail,ic_star,ic_person,ic_dialpad_fab}.xml`;
  modified `res/values/strings.xml`, `res/values/dimens.xml`,
  `AndroidManifest.xml`, `java/dev/smsrelay/ui/ShellActivity.kt` (menu entry
  only); this log entry. `calls/CallService.kt`, `CallActivity.kt`,
  `CallExecutor.kt`, `DialerActivity.kt`, the backend, the Mini App and every
  worker are untouched.
- **Changed:** the Messages menu's "Dialer" entry now opens the Phone home
  instead of the dialpad; the dialpad stays one tap away on the home's FAB
  and its own menu entry, and keeps the only `ACTION_DIAL` and `tel:`
  filters. Recents reads `CallLog.Calls.CONTENT_URI` with `_ID, NUMBER,
  CACHED_NAME, DATE, DURATION, TYPE, SUBSCRIPTION_ID, PHONE_ACCOUNT_ID,
  IS_READ`, ordered `DATE DESC LIMIT 200`, maps INCOMING / OUTGOING /
  MISSED / REJECTED / BLOCKED / VOICEMAIL / ANSWERED_EXTERNALLY (as
  incoming) and leaves an unknown vendor type generic; the row shows a 40dp
  initial circle or a person glyph, a type glyph (red and bold for an unread
  missed call), the name or the formatted number, the kind, the duration for
  a connected call, the SIM label only on a multi-SIM phone, and the
  Today/Yesterday/date stamp. Contacts read `CommonDataKinds.Phone` with
  `DISPLAY_NAME, NUMBER, CONTACT_ID, LOOKUP_KEY`, sorted by name, collapsed
  by `PhoneNumbers.normalize`, starred via one extra `Contacts` id query;
  search is an in-memory case-insensitive name/number filter. Favorites read
  `Contacts` with `STARRED = 1 AND HAS_PHONE_NUMBER = 1` (`_ID,
  DISPLAY_NAME`) plus one bounded `IN (...)` phone query for the first
  number of each. Voicemail is a placeholder sentence and a
  `TelecomManager.getVoiceMailNumber()` button; there is no PDU or voicemail
  provider code. A tap on any row is `ACTION_DIAL` with `tel:`, never a
  call, so the dialpad keeps the emergency path. Long press opens a sheet:
  Call / SMS (`ThreadActivity.intentForAddress`) / Copy number / Delete from
  call log (needs `WRITE_CALL_LOG`, one toast on refusal). All reads run on
  `Dispatchers.IO`, report permission, failure and empty as three separate
  states, and never log a number or a name.
- **Verification:** no compiler, no emulator, no APK. Static only: all 38
  `res/` XML files and the manifest parse; every `R.id`/`R.string`/
  `R.layout`/`R.drawable`/`R.dimen`/`R.menu` reference in the new and changed
  sources resolves; every id the activity and the three adapters ask for
  exists in its layout; no duplicate string names; no unused new string and
  no unused import; brace/paren/bracket balance zero in all seven files;
  `git diff --check -- android-app/` clean. Manifest: `DialerHomeActivity` is
  `exported=false`, has no intent filter, parent `ShellActivity`, theme
  `Theme.SmsRelay.Messages`; `calls.DialerActivity` is still the only
  `ACTION_DIAL` and `tel:` handler; `ShellActivity` is still the only
  MAIN/LAUNCHER; `CallService` unchanged.
- **Decisions / risks:** nothing in this screen places a call, so an
  emergency number in a log row cannot be dialled by a single touch; the
  dialpad applies its own routing. A private number keeps a row (with the
  existing "Private number" sentence) but offers no call/SMS/copy actions.
  Deleting a call needs `WRITE_CALL_LOG`; when the read is granted and the
  write is not, one quiet card says so instead of failing silently. The
  FAB is always visible, including on Voicemail, because it is the whole
  screen's one fixed action. Nothing was compiled, so a build error is still
  possible.
- **Follow-ups:** the `android-build` workflow must build this and publish
  `sms-relay-debug.apk` on `android-latest`. On a device: tab survives
  rotation, permission grant reloads the tab, a missed call shows red,
  long-press delete works with `WRITE_CALL_LOG`, and voicemail dials the
  carrier number when the phone has one.
## 2026-11-18 — Phone home follow-up: permission answer, contact key, avatar lettering

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode)
- **Task:** Self-review of the Phone home above, before it is built: make the
  permission answer redraw the tab, use the documented contacts lookup
  column, and drop a redundant avatar expression.
- **Files:** modified
  `java/dev/smsrelay/ui/DialerHomeActivity.kt`,
  `java/dev/smsrelay/ui/dialer/DialerRepository.kt`,
  `java/dev/smsrelay/ui/dialer/RecentsAdapter.kt`,
  `res/layout/activity_dialer_home.xml`, `res/values/strings.xml`; this entry.
- **Changed:** (1) the `RequestPermission` callback no longer branches on the
  grant: it clears the pending flag, redraws the note and re-reads the tab,
  because both paths already ask the system what is granted, so a refusal
  and a grant for the other tab are drawn correctly. (2) `LOOKUP_KEY` is
  read through `ContactsContract.Contacts.LOOKUP_KEY` instead of the
  `Phone.LOOKUP_KEY` alias, which is the constant the contacts table
  documents. (3) The Recents avatar letters `label` directly instead of a
  one-use `lettering` copy. Also added `dialer_permission_settings` for the
  toast that a settings page could not be opened, so `dialer_permission_grant`
  ("Allow") is no longer used as a message.
- **Verification:** same static set as the entry above, re-run after the
  edits: 38 `res/` XML files and the manifest parse; all `R.*` references
  resolve; every id the activity asks for exists in its layout; no duplicate
  string or dimen name; no unused `dialer_*` string; no unused import; all
  seven files at brace/paren/bracket balance zero with no line over 100
  columns; `git diff --check -- android-app/` clean; manifest unchanged in
  shape (`DialerHomeActivity` unexported and without filters,
  `calls.DialerActivity` the only `ACTION_DIAL` and `tel:` owner,
  `ShellActivity` the only launcher). Not compiled, not built.
- **Decisions / risks:** still static only. The approval dialog is asked from
  the screen; a permanent refusal routes the same tap to the app's settings
  page instead of repeating a dialog Android will not show.
- **Follow-ups:** build through the `android-build` workflow; device check of
  the permission flow, the delete action and the voicemail button.
## 2026-11-18 — Phone home hardening: security exceptions, string escaping

- **Agent:** DeepSeek V4.1 Flash Free (OpenCode)
- **Task:** Close the remaining ways this screen could crash or build badly
  before it is handed to the build workflow.
- **Files:** modified
  `java/dev/smsrelay/ui/dialer/DialerRepository.kt`,
  `java/dev/smsrelay/ui/DialerHomeActivity.kt`,
  `res/values/strings.xml`, `res/layout/activity_dialer_home.xml`; this entry.
- **Changed:** (1) each of the three reads now inspects the failure instead of
  swallowing it: a `SecurityException` from the provider (a grant revoked
  between the check and the query, or a work-profile policy) is reported as
  `denied` so the screen draws the permission state, while anything else stays
  `failed` and draws the read-error sentence. (2) Two new strings used a
  literal apostrophe, which aapt rejects without an escape; both are escaped
  the same way the existing `capture_none` and `hint_permissions` do it.
  (3) The clipboard label of "Copy number" is `app_name` instead of the
  unrelated `dialer_call` sentence. (4) The FAB tint moved to `app:tint`,
  which is the attribute the Material FAB reads.
- **Verification:** 38 `res/` XML files and the manifest parse; all `R.*`
  references resolve; balance zero in all seven Kotlin files; no unused
  import; no duplicate string name; no unescaped apostrophe left in any
  string; no unused `dialer_*` string; `git diff --check -- android-app/`
  clean. Not compiled, not built.
- **Decisions / risks:** still static only, so a type or resource error is
  possible. The `denied` path is deliberately the same state as the
  pre-check, so a revocation mid-read never blanks the tab into "No calls
  yet".
- **Follow-ups:** build with the `android-build` workflow and publish
  `sms-relay-debug.apk`; on device, verify that revoking the call log
  permission in Settings and returning to the tab shows the permission card
  rather than an empty list.

## 2026-11-18 — Calls, default dialer and local Messages client (lead summary)

- **Agent:** DeepSeek V4.1 Flash and Hy4 flash (OpenCode), lead/worker
  delegation. The lead planned, reviewed every diff and owns this entry; the
  implementation, tests and docs were delegated to subagents.
- **Task:** Turn the relay phone into a daily-driver SMS client and a real
  Android dialer, without breaking the relay or the Mini App:
  call log + live call + remote actions; Telegram Answer/Decline/Call back;
  `ROLE_DIALER` with `InCallService` and an in-call UI; a local Messages home,
  thread and composer over the phone's real SMS store; a Google-Dialer-style
  home with Favorites / Recents / Contacts / Voicemail.
- **Files:** backend `app/calls.py`, `app/db.py`, `app/main.py`,
  `app/pairing.py`, `app/updater.py`, tests `test_calls.py`,
  `test_call_actions.py`, `test_updater.py`, `test_db.py`;
  Mini App `static/app/assets/js/{api,store,ui,app}.js`, `app.css`,
  `index.html`, `test_miniapp_frontend_static.py`;
  Android `calls/` (`CallLog*`, `DialerActivity`, `CallService`,
  `CallActivity`, `CallExecutor`, `CallActionGuard`, `CallSession`,
  `CallDeepLinks`), `messages/`, `ui/ShellActivity`, `ui/ThreadActivity`,
  `ui/DialerHomeActivity`, `ui/messages/`, `ui/dialer/`, manifest, themes,
  layouts, strings; `README.md`, `docs/AGENT_GUIDE.md`,
  `docs/CALLS_DESIGN.md`, `docs/UI_DESIGN.md`.
- **Changed:** new call tables (`call_log`, `call_sessions`,
  `call_actions`) with a single freshness rule (2 min ringing, 12 h active,
  60 s ended), tolerant per-item `/calls/sync`, atomic action claim and
  at-most-once guards, stale-`call_ref` refusal, emergency numbers never
  remote-dialled, no remote audio. Telegram now mirrors ringing/missed calls
  with Answer / Decline / Call back; callback data carries the device only,
  and the send is capped at 2 s. Android gained the default-dialer service
  and full-screen in-call UI, the call-log queue, the dialpad, and the local
  Messages + dialer homes. A local send goes SIM → `content://sms/sent` →
  best-effort `/import` with `direction=out`; a failed mirror never undoes the
  local SMS. Group and MMS-only threads are read-only with explicit
  placeholders; RCS remains out of scope. The working-tree README/GUIDE had
  been accidentally reverted to the old "forwards the body" text; they were
  restored from HEAD before the calls/UI documentation was merged.
- **Verification:** backend suite `738 collected — 734 passed, 3 failed,
  1 error`; the four failures are the known pre-existing ones
  (`test_commands` contact search ×2, `test_db` chat search,
  `test_app_assembly` shutdown timing). Mini App static tests and all JS
  syntax checks pass. **The APK was not built**: this host has no JDK, Gradle
  or emulator, so the ~3,000 lines of new Android code are static-reviewed
  only. No device test was run.
- **Decisions / risks:** audio never leaves the phone; emergency calls are
  local and offline; `SettingsStore.sendingEnabled` gates remote commands only,
  never the local composer; a group is never represented by one member's
  number; a provider row inserted after the radio accepts can still fail to
  store; the outbound relay mirror is best effort. Android API traps found in
  review and fixed: `Call.Details.getState()` is API 31 (use `Call.state`),
  the in-call theme needed `NoActionBar`, the full-screen intent needs
  `USE_FULL_SCREEN_INTENT`, and a non-`dial` action claimed while idle is now
  reported `rejected` / `no live call` instead of being stranded.
- **Follow-ups:** commit to a feature branch and run `android-build` with
  `workflow_dispatch` (the release step only runs on a `main` push, so the
  rolling APK is not overwritten); fix compiler errors before any merge.
  Then a real-device matrix (default SMS/dialer roles, incoming call while
  locked with and without the full-screen grant, answer/reject from screen and
  notification, mute/speaker/hold/DTMF, dual-SIM, emergency with the relay
  offline, service rebind, vendor provider quirks, notifications). Still to
  build: durable outbound mirror queue, group send, MMS, visual parity pass,
  and the final APK release. No secrets, numbers, names or message bodies were
  added to this log.

## 2026-11-18 — Android build green: calls, dialer and local Messages

- **Agent:** DeepSeek V4.1 Flash (OpenCode), lead/worker delegation.
- **Task:** compile the calls/dialer/Messages work on GitHub and publish the
  rolling debug APK, as the repository rule requires.
- **Files:** compile-only fixes in
  `android-app/.../calls/CallLogReader.kt`,
  `.../messages/SmsObserver.kt`,
  `.../ui/ShellActivity.kt`,
  `.../ui/DialerHomeActivity.kt`,
  `.../ui/dialer/DialerRepository.kt`; this entry.
- **Changed:** no behaviour. `CallLog.Calls.SUBSCRIPTION_ID` is not a public
  SDK constant, so the projection and column lookups use the literal
  `"subscription_id"`; `ContentObserver` is imported from
  `android.database`; the FAB inset code uses the real
  `ViewGroup.MarginLayoutParams.bottomMargin` field; the voicemail lookup
  passes the first call-capable `PhoneAccountHandle` to
  `TelecomManager.getVoiceMailNumber`, because the no-arg overload is hidden.
- **Verification:** GitHub Actions run `44248359040` on commit `a83f972`
  completed **success**; `Assemble debug APK`, `Upload APK` and
  `Publish rolling APK release` all ran. The published asset is
  `sms-relay-debug.apk` (7,638,352 bytes, sha256
  `d91a044b39ff8569007152a9a086b6678be4021cf30431fb51470ab7176d54ef`),
  updated 2026-11-18T06:03:29Z:
  <https://github.com/HessiKz/sms-relay/releases/download/android-latest/sms-relay-debug.apk>
  Backend suite remains 738 collected — 734 passed, 3 failed, 1 error
  (the four known pre-existing failures).
- **Decisions / risks:** the APK is compiled and published, but it has not
  been run on a phone. The build is a debug build signed with the stable
  CI debug key, so it updates the previous install. The new UI and dialer
  need the default-SMS/default-dialer roles and the call/notification
  permissions granted on the device. MMS, group send, RCS and the durable
  outbound mirror remain out of scope; the relay mirror of a locally sent
  SMS is best effort.
- **Follow-ups:** install the APK and run the device matrix: first launch
  and role prompts, conversation list, send and Mini App mirror, incoming
  call while locked with and without the full-screen grant, answer/reject
  from screen and notification, mute/speaker/hold/DTMF, dual-SIM, emergency
  number with the relay offline, service rebind, and the vendor call-log /
  contact quirks. Then a screenshot pass for visual parity, and the MMS /
  group-send / durable-mirror phases.

## 2026-11-18 — Role/message fixes, pairing confirmation, notif snippets and nav

- **Agent:** DeepSeek V4.1 Flash (OpenCode), lead/worker delegation.
- **Task:** Fix the operator-reported blockers (empty Messages list, dialer
  role not settable), add a phone-side pairing confirmation, put OTP/code
  snippets in Telegram notifications without a date, expire the "SMS sent"
  notes, and add a bottom navigation bar.
- **Files:** Android `messages/SmsRepository.kt`, `ui/ShellActivity.kt`,
  `ui/DialerHomeActivity.kt`, `ui/MainActivity.kt`,
  `util/RoleState.kt`, `pairing/PairingController.kt`,
  `net/PairingClient.kt`, layouts/menu/drawables/strings; backend
  `delivery.py`, `db.py`, `api.py`, `updater.py`, `pairing.py`, tests;
  `README.md`, `docs/AGENT_GUIDE.md`.
- **Changed:** the Messages list no longer drops a thread when
  `canonical-addresses` cannot resolve it: it falls back to the first SMS
  row's `ADDRESS`, and keeps an "Unknown sender" thread with its real
  `thread_id` if even that fails. The SMS and dialer roles are requested
  without a permission gate and have prominent cards plus a
  `RoleState.diagnosticsReport()` dialog. `POST /pair/claim` now supports
  `confirmed` (`null` -> `202 awaiting_phone_confirmation` with the
  approver, `true` -> token once, `false` -> cancelled; absent -> old
  behaviour), and the phone shows a non-cancelable confirmation before the
  token is stored. Telegram notifications now carry a 160-character body
  snippet and an extracted `Code:` line, no date; masked/labelled
  identifiers are not codes. The `tg_messages` table and the reaper delete
  "SMS sent" notes after one hour. A Material 3 bottom bar switches
  Messages / Phone / Contacts / Settings.
- **Verification:** backend suite **785 collected — 781 passed, 3 failed,
  1 error** (the four known pre-existing failures). GitHub Actions run
  `44365278140` on `d02de4d` completed **success**; the rolling asset
  `sms-relay-debug.apk` is 7,670,521 bytes, sha256
  `79f377384dc40deb7482b1073bdb2e3349bec6193a36dbc4cec366f68f3b70a9`,
  updated 2026-11-18T18:04:02Z.
- **Decisions / risks:** the APK is compiled and published, but the
  backend changes are **not deployed**; the pairing dialog and the new
  notification format need a service restart to take effect. The APK was
  not device-tested. OEM role suppression is still possible; the
  diagnostics dialog is the first check. OTP extraction is heuristic; the
  snippet still shows unusual formats, and a code-shaped card/order number
  must not be labelled a code.
- **Follow-ups:** deploy the backend with operator approval, install the
  new APK, and run the device matrix: role cards and diagnostics, Messages
  list and "Unknown sender" threads, send/mirror, pairing confirmation,
  OTP notifications, note expiry after an hour, the bottom bar/gesture
  area, and the dialer role/call flows.

## 2026-11-18 — Backend deployed to production

- **Agent:** DeepSeek V4.1 Flash (OpenCode), lead; operator approved the
  production restart.
- **Task:** Deploy the calls/pairing/notification backend to the live
  service and restart `sms-relay-api`.
- **Files:** live copy of `backend/app` (including `calls.py` and the new
  Mini App static files) synced to `/opt/sms-relay/app/app` with rsync;
  no live env file, Caddyfile or database edit. The database migration ran
  on startup and is additive.
- **Backups:** `/opt/sms-relay/app.pre-20261118-203310.tgz` (the previous
  app tree) and
  `/var/lib/backups/sms-relay/sms-relay-20261118-203310.db` (online SQLite
  backup, 25 MB, `PRAGMA integrity_check` = ok). Neither was deleted.
- **Verification:** `systemctl restart` completed, the unit is `active`
  (`running`), `GET /health` is `{"ok":true}` on loopback and through
  `https://ai-barrow.ir/relay/health`; `/calls/actions` without a token is
  `401` (the route exists); OpenAPI lists `/calls/sync`,
  `/calls/events`, `/calls/actions`, the call-result route and the four
  Mini App call routes; `PairClaimBody` has `confirmed`; the database has
  `call_log`, `call_sessions`, `call_actions`, `tg_messages` and the
  `pairings.approved_by_name/username` columns; the public Mini App serves
  `v=20261117-calls`; the phone's next `GET /calls/actions?wait=0` changed
  from `404` to `200`; no error/traceback lines since the restart.
- **Decisions / risks:** the restart is a brief outage of the bot/worker;
  queued jobs and the SQLite queue survive. The live backend is now the
  new code, but the APK with the phone-side pairing dialog and the
  role/message fixes must still be installed on the phone. The old APK
  keeps sending no `confirmed` field and gets the legacy token path, so
  the dialog only appears after the new APK is installed. The backup
  tarball and database are the rollback path.
- **Follow-ups:** install the `d02de4d` APK, grant the SMS and dialer
  roles, run the pairing flow through the new phone dialog, and verify an
  OTP/code notification and the one-hour deletion of the "SMS sent" note.
  If a role is still refused, collect the "Role & SMS diagnostics" lines.
  No secrets, numbers, names or message bodies were added to this entry.

## 2026-11-18 — Fix the two device blockers: empty Messages list and the dialer role

- **Agent:** DeepSeek V4.1 Flash (OpenCode). No subagent tool was exposed in
  this runtime, so the lead did the implementation itself; that is the
  documented fallback, not the preferred path.
- **Task:** the installed APK showed (a) an empty Messages list and (b) no way
  to become the default dialer. Both were fixed at the cause, not with a
  prompt.
- **Files:** `android-app/.../messages/SmsRepository.kt`,
  `.../ui/ShellActivity.kt`, `.../ui/MainActivity.kt`,
  `.../ui/DialerHomeActivity.kt`, `.../ui/messages/MessagesBanner.kt`,
  `.../util/RoleState.kt`, `.../res/values/strings.xml`; this entry.
  No change to the manifest, the backend, `calls/`, `messages/SmsObserver`,
  `ThreadActivity` or the pairing files.
- **Changed — Messages list:** `conversations()` no longer drops a thread when
  `content://mms-sms/canonical-addresses` resolves nothing (that `continue`
  was why the screen was empty even with messages on the phone). The address
  is now read from the thread's first SMS row (`thread_id = ?`,
  `DATE ASC, _ID ASC LIMIT 1`) for a one-to-one thread, and a thread that
  still has none is kept with a blank address, so the label falls back to the
  existing "Unknown" string and the composer stays disabled. `threadFor` and
  `addressForThread` use the same fallback so a thread screen opened by id
  agrees with the list. A debug-only line logs counts (threads read, with
  address, without address) and nothing else.
- **Changed — dialer role:** `MainActivity.requestDialerRole()` no longer
  returns early when a call permission is missing; the role dialog is the
  first thing it opens, and the missing permissions are asked afterwards
  through a launcher, never as a condition. The role is re-read in
  `onResume`, an unavailable role intent now opens the default-apps settings,
  and the pending flag only repeats the request after a later grant.
  `DialerHomeActivity` has a prominent "not your dialer" card with held /
  available / unavailable states and a settings fallback; the result is
  re-read rather than trusted.
- **Changed — SMS role:** `ShellActivity` replaces the bare
  `startActivity(createRequestRoleIntent)` with the setup screen's flow:
  a `registerForActivityResult` launcher, role manager on API 29+,
  `ACTION_CHANGE_DEFAULT` + `EXTRA_PACKAGE_NAME` on 8/9, default-apps settings
  as the fallback, and a reload after the result. The role is now a prominent
  card at the top of the list ("messages are hidden until you allow it"), and
  the empty state names the role instead of a read error while it is missing.
- **Changed — diagnostics:** a "Role & SMS diagnostics" entry in the Messages
  menu (added in code, so the shared menu file of the Phone home is unchanged;
  also on long-press of the role card) opens a scrollable dialog with the SDK
  level, SMS and dialer role availability and holding, the default SMS package
  and default dialer package, this package name, and the READ_SMS /
  READ_CALL_LOG / READ_CONTACTS state. Built from `RoleState` helpers, so it
  cannot disagree with the cards; no number, name or body is in it.
- **Verification:** static only, no JDK/Gradle/emulator on this host. All 39
  resource XML files and the manifest parse; no duplicate string names; every
  `R.string` reference resolves; braces/parens balance in all six touched
  Kotlin files with strings and comments stripped; no duplicate function names
  or unused imports in them; `recipients.isEmpty()` appears only in the two
  fallback branches of `conversations`/`threadFor`, never as a drop;
  `pendingDialerRole` is gone and `hasAllCallPermissions()` is no longer a
  gate anywhere in the role path; the manifest diff is empty;
  `git diff --check` is clean.
- **Decisions / risks:** keeping an address-less thread is deliberate - a
  conversation the user can open beats a silently missing one, and the call
  button and composer already refuse a blank address. `ThreadActivity` still
  carries its own copy of the old raw role `startActivity`; it was outside the
  owned files and was not touched, so its banner is unchanged (follow-up).
  Untested on a device: the vendor provider may still answer nothing for a
  non-default app, which is exactly what the diagnostics dialog is for.
- **Follow-ups:** GitHub Actions must compile this; the APK must be rebuilt and
  published by the lead as `sms-relay-debug.apk`
  (<https://github.com/HessiKz/sms-relay/releases/download/android-latest/sms-relay-debug.apk>).
  Then on the device: open Messages with and without the role, grant the role
  from the card, open the diagnostics and report the two lines about the SMS
  role and the default SMS package, and make SMS Relay the dialer from the
  Phone home card. Port the robust role flow to `ThreadActivity` next.
- **Addendum (same task, same files):** the address fallback of the list is
  bounded (`ADDRESS_FALLBACK_LIMIT = 300` per read), so a phone whose
  canonical table refuses everything cannot turn one list read into
  thousands of queries; the remaining threads are still listed, just
  address-less. The debug line also reports how many fallbacks ran.
