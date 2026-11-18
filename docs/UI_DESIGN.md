# UI Design — SMS Relay as a local SMS client and dialer

Status: **design only, no code changed**. The code is the source of truth; this
document is the plan to reconcile it with the operator's product direction.
Date: 2026-11-17. App identity stays **SMS Relay** (`app_name`). Identity is
original; we copy layout and flow, never Google icons, wordmarks, animations or
code. Use Material Symbols (open font) / original vector paths and Material 3
components only.

## 0. Boundaries (non-negotiable)

- The phone is already the **default SMS app** (relay capture). The new UI must
  read the **real provider store** (`content://sms`,
  `content://mms-sms/conversations`, `content://mms-sms/canonical-addresses`)
  and send through the SIM — not render the relay's Room queue.
- The relay keeps working: an inbound SMS is still written to Room **before**
  upload (`SmsReceiver`); an outbound SMS is still mirrored to Room/backend.
  The **Mini App remains the remote view**; this UI is local only.
- **SMS only.** RCS needs Google's private backend and is impossible. MMS is a
  later phase; `MmsReceiver` acknowledges and ignores WAP pushes today.
- No JDK/Gradle/emulator on this host. Layout, Material 3 theme, insets,
  predictive back, provider cursors and all visuals require a device.

## 1. Current UI inventory and gap

| Screen | Files today | What it does | Gap |
|---|---|---|---|
| Setup | `ui/MainActivity.kt`, `res/layout/activity_main.xml` | Pairing/QR, permissions, default-SMS and dialer roles, battery/auto-start, SIM, contact/call/SMS sync, manual compose test — a wall of `Button`s found by `findViewById` | Not a daily-driver home; no navigation shell or material structure |
| Delivery history | `ui/HistoryActivity.kt`, `activity_history.xml`, `item_history.xml` | Paged list (50) of `MessageRecord` from Room `messages`, showing sender/body/timestamp/`uploadState`/fallback | It is the **relay queue**, not conversations: no threads, bubbles, read state, search or contact names |
| Compose stub | `ui/ComposeSmsActivity.kt`, `activity_compose_sms.xml` | `ACTION_SENDTO` handler; shows info + address then `finish()` on `onResume` | No real composer, recipient picker, drafts or send flow |
| Dialpad | `calls/DialerActivity.kt`, `activity_dialer.xml` | Digit buttons, SIM spinner from `callCapablePhoneAccounts`, call-via deep links (`CallDeepLinks`), emergency bypass, `ACTION_DIAL`/`tel:` handling, launcher | No tabs, call log, favorites, contacts, search, detail or missed badge |
| In-call | `calls/CallActivity.kt`, `activity_call.xml` | Answer/decline/hangup/mute/speaker/hold + DTMF keypad, plain buttons, `LiveCallRegistry` | No Material 3 states/avatar polish, no "add call" placeholder, no accessibility pass |

Foundation: `Theme.SmsRelay` = `Theme.MaterialComponents.DayNight.DarkActionBar`
(Material 2 via material 1.12.0); RecyclerView + AppCompat + Room + WorkManager
+ OkHttp. **No Compose, Navigation component, ViewPager2 or paging library.**
Build on XML views + RecyclerView (`ListAdapter`/`DiffUtil`), adding only small
dependencies if needed. Missing everywhere: conversation/thread models, search,
bottom nav, avatars, unread/read writes, drafts, delivery status, archived/
blocked, dialer tabs and a settings screen.

## 2. Target information architecture

One `ShellActivity` (new launcher) hosts **Messages**, **Calls**, **Contacts**
fragments in a `FragmentContainerView` with a `BottomNavigationView`; manual
back stack via `OnBackPressedDispatcher` (no Navigation dependency). Setup,
Settings and the in-call screen remain activities. Routes: `app://messages`,
`app://messages/thread/{threadId}`, `app://messages/new`,
`app://contacts/picker`, `app://dialer`, `app://dialer/contact/{lookupKey}`,
`app://calls/settings`, `app://setup`.

**Messages home** — `MaterialSearchBar` + avatar overflow (Settings) in a
`MaterialToolbar`; pinned section then conversation rows (circular avatar with
initials, name/number, snippet, date, SIM chip, unread dot + count, muted icon).
Extended FAB "New message". Long-press → contextual bar (archive, mark
read/unread, delete, block). Swipe → archive / mark unread / delete with
Snackbar undo. Bottom nav badge for unread. States: skeleton loading; empty =
permission missing / zero threads / no search results (distinct copy + action);
error = provider `SecurityException` → "Open default SMS settings"; offline =
quiet banner ("Messages still work locally"), because relay reachability is
irrelevant to reading the provider.

**Chat thread** — toolbar: avatar + name/number + SIM label, call action
(`ACTION_DIAL`), overflow (view contact, block, report spam, delete thread).
Message list: in/out bubbles, date separators (Today / Yesterday / date),
per-outgoing status (pending → sent → delivered; failure in red, tap for
reason), SIM label on sent rows, attachment placeholder (MMS boundary),
long-press reply/copy/forward/delete, swipe-to-reply, inline reply strip.
Composer: text field + attach (+) + send; draft persisted per thread via
provider draft row. Scroll-to-bottom FAB with unread count and keyset "load
older". States: loading older, empty thread, send failure (no SIM/permission),
offline. SMS has **no read receipt**: label honestly as "Delivered" only.

**New conversation / picker / number entry** — search field, recent-contact
chips (from ContactsRepository), alphabetical contact list with fast scroller,
"Enter a number manually" → country-code + number + SIM selector → open thread.

**Dialer home** — tabs **Favorites / Recents / Contacts / Voicemail** with
`TabLayout` + `ViewPager2`; search bar and dialpad FAB (reuse `DialerActivity`
as a sheet). Recents read `content://call_log/calls` via `CallLogReader`: row =
name/number, kind icon (missed in red), SIM label, time, duration; tap = call
back or detail, long-press = block/report/delete. Favorites are local lookup
keys; Voicemail is an explicit "not available on this device" placeholder with
link to carrier app. Missed badge on Calls tab; SIM selection chip. States:
loading, empty per tab, permission error, offline (dialer never needs relay).

**In-call (already in progress)** — restyle `activity_call.xml` with Material 3
states: incoming (answer/decline), active (mute, speaker, hold, keypad, timer),
held, keypad open. Keep `LiveCallRegistry` control flow untouched. Add avatar,
contact name, disabled "Add call" placeholder (conference out of scope) and
content descriptions/haptics.

**Settings** — theme (system/light/dark), dynamic color (31+), default SIM for
SMS and calls, notifications, blocked numbers (local list), relay pairing (move
the controls out of `MainActivity` into `PairingController` reuse), default-SMS
and dialer role requests, About (version, identity, legal boundary).

## 3. Data layer plan

New `dev.smsrelay.messages` repository (XML/cursor based):

- **Threads:** `content://mms-sms/conversations` (`simple=true`), projection
  `_ID, DATE, MESSAGE_COUNT, RECIPIENT_IDS, SNIPPET, READ`; resolve each
  `RECIPIENT_IDS` member through `content://mms-sms/canonical-addresses/<id>`
  (`ADDRESS`), reverse map in `content://mms-sms/canonical-addresses`.
- **Messages:** `content://sms`, projection `_ID, THREAD_ID, ADDRESS, BODY,
  DATE, DATE_SENT, READ, SEEN, TYPE, SUBSCRIPTION_ID`; selection `THREAD_ID=?
  AND TYPE IN (INBOX,SENT,FAILED,DRAFT)`; order `DATE ASC`, keyset paging by
  `DATE/_ID`.
- **Read/unread:** `ContentValues(READ=1)` update on `content://sms/inbox` by
  `THREAD_ID` (thread open) or `_ID` (single message); `WRITE_SMS` is declared.
  Unread counts from the conversations `READ` column and inbox `read=0`.
- **Drafts:** `content://sms/draft` (`ADDRESS, BODY, THREAD_ID, DATE`); fallback
  to local prefs if the provider denies writes.
- **Contacts:** extend `contacts/ContactResolver.kt` and
  `data/ContactsRepository.kt` (60 s cache, 3000 cap) with
  `displayNameForAddress`, `lookupKey`, `avatarUri` via
  `ContactsContract.CommonDataKinds.Phone`; reuse `util/PhoneNumbers.kt`
  normalization for keys.
- **Refresh:** `ContentObserver` on `content://sms`, conversations, and
  `content://call_log/calls`; throttled 300–500 ms, emit a `Flow`, requery the
  visible screen.
- **Room mirror:** keep `messages` purely as the upload queue (`MessageRecord`
  fields: id, sender, senderKey, body, parts, partIndex, receivedAt,
  uploadState, uploadAttempts, lastError, backendState, fallbackState,
  groupThreadId, subId, source, createdAt). Add an **additive, idempotent**
  migration in `data/AppDatabase.kt` (after MIGRATION_3_4) adding `providerId`,
  `threadId`, `direction`, `read`, or a separate `sms_mirror` table. Never drop
  the queue. Dedupe on provider `_ID` via `util/Dedupe.kt`/`SenderNormalizer`.
- **Inbound consistency:** preserve `SmsReceiver` order — Room insert →
  `SmsProviderWriter.writeToInbox(READ=0)` → schedule `UploadWorker`. The new UI
  only reads the provider; it never touches `uploadState`.
- **Outbound consistency:** send via existing `sms/SmsSender.kt` (SIM logic in
  `resolveSubscriptionId`/`managerForSubscription`), then insert a SENT row
  (`ADDRESS, BODY, DATE, DATE_SENT, TYPE=SENT, SUBSCRIPTION_ID`) into
  `content://sms/sent` and a Room mirror queued for `UploadWorker`. Keep the
  command guard (`HandledCommandDao`) and fallback invariants intact. Delivery
  status from `SendStatusReceiver`/provider `status`, not a fabricated receipt.
- **Calls:** dialer history reads the call log directly for responsiveness;
  `CallLogReader`/`CallLogWorker` remain the upload path (one `getUpdates`
  consumer, `device_id` isolation).
- **MMS boundary:** no PDU decode or send; MMS may appear as a provider snippet
  with a "Open in your carrier app" placeholder. Nothing is promised.

## 4. Component inventory

Migrate `themes.xml` to `Theme.Material3.DayNight.NoActionBar`, add
`DynamicColors.applyToActivitiesIfAvailable()` (guard API 31+), plus
`type.xml`/`shapes.xml` for typography and shape. Reusable pieces:
`MaterialToolbar`, `MaterialSearchBar`, `BottomNavigationView` +
`BadgeDrawable`, extended `FAB`, `MaterialChip`/`ChipGroup`, `ShapeableImageView`
avatar with initials fallback, `MaterialAlertDialog`, `BottomSheetDialog` (call
via, block/report), `ListItem`-based conversation/call rows, `Snackbar` undo;
`ListAdapter`+`DiffUtil` cursors, `selectableItemBackground` ripple,
`performHapticFeedback`, `WindowCompat` edge-to-edge + inset listeners, and
predictive back (`android:enableOnBackInvokedCallback="true"`, API 33+).
Original app icon/Material Symbols only; no Google marks or wordmark.

## 5. Phased implementation (S/M/L)

| Phase | Size | Files create / modify | Device-free tests |
|---|---|---|---|
| 0 Foundation | S | `themes.xml`, `strings.xml`, manifest launcher → new `ShellActivity` + fragment skeleton + bottom nav | Resource/manifest lint (toolchain needed) |
| 1 Read model | M | `messages/SmsRepository.kt`, `SmsContentObserver.kt`, `ConversationFragment` + row/search layouts | JVM: projection lists, thread-key grouping, snippet/date formatting |
| 2 Chat thread | L | `ThreadFragment` + bubble/date-separator/composer layouts, read + draft writes, provider SENT insert, status mapping | JVM: SQL selection builders, separator bucketing, status mapping |
| 3 New conversation | M | `NewConversationFragment`, picker/number-entry layouts, extend ContactResolver | JVM: number normalization + contact lookup key |
| 4 Dialer home | L | `DialerHomeFragment` + tabs, `CallLogReader` reuse, favorites, contact detail, missed badge | JVM: call-type → icon, missed aggregation |
| 5 In-call polish | M | `activity_call.xml`, `CallActivity` (visual states only), accessibility | JVM: state → control-visibility pure function |
| 6 Settings | S | `SettingsActivity`, pairing-control extraction, blocked numbers, About | JVM: SettingsStore preference keys |
| 7 Relay hardening | S | additive migration, provider↔Room reconciliation, dedupe, on-device checklist | JVM: migration idempotence, dedupe |

Verification: add `testImplementation("junit:junit:4.13.2")` and pure-JVM tests
for the helpers above; no emulator here. Any Android change must be built by
the `android-build` GitHub Actions workflow and published as
`sms-relay-debug.apk` on `android-latest`. A manual telephony matrix is
mandatory (stock/Samsung/Xiaomi, dual-SIM, no SIM, emergency, locked).

## 6. Risks

1. **No local verification.** No JDK/Gradle/emulator: no build, lint or visual
   check here; rely on GitHub Actions plus a real device for providers,
   insets, back animations, SIM behavior and theme.
2. **RCS impossible** and **MMS out of scope**: conversation threads built from
   `content://sms` alone will omit MMS/group messages until a PDU phase.
3. **OEM / platform variance**: default-role granting, `content://mms-sms`
   columns, call-log permissions, dual-SIM labels and battery/auto-start differ
   by vendor; test the matrix and degrade gracefully.
4. **Visual voicemail and Google spam/call screening** are proprietary services
   with no public API here: show explicit placeholders, never promise them.
5. **"Exact replica" legal boundary**: layout/flow only; any Google asset,
   icon, wordmark or animation copies would breach IP — keep original identity.
6. **Relay regression**: careless provider writes could drop inbound messages
   or double-upload; preserve Room-before-upload and the single `getUpdates`
   consumer, and keep the Mini App as the remote view.
