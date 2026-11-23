# Coach chat and check-in nudges

One conversation per coach–fighter pair, laid out like WhatsApp: a chat list,
bubbles, read receipts, typing, reactions, replies, photos and voice notes.
The one addition is a **training card**: "Message about this" on a session
opens the chat with that session attached. Coaches can also set a **check-in
nudge**: chosen weekdays and a local time at which the fighter is pushed to
check in, only if they have not.

## Routes

- `/coach/chats` (coach nav, second tab on phones) and `/chat` (fighter side
  menu). `?with=<profile id>` opens a chat; `&quote=<source>:<record id>`
  attaches a session (`unlxck` or `external`).
- The fighter profile (`/coach/fighters/[id]`) has **Message** and the
  check-in nudge controls.
- Session details show **Message about this** to coaches, and **Ask your coach
  about this** to fighters who have a connected coach.

## Speed

Every screen is one call, and nothing polls.

| Action | Call | Plan |
| --- | --- | --- |
| Inbox | `coach_chat_inbox()` RPC | Chat rows carry the preview and both unread counts, so nothing counts messages |
| Open a chat / page back | `GET coach_messages?chat_id=eq…&order=id.desc&limit=30` (RLS) | `coach_messages_chat_recent (chat_id, id desc)` range scan; keyset paging |
| Send | `coach_chat_send(...)` RPC | Message and inbox row written in one transaction; retries are free (`client_id`) |
| Read receipt | `coach_chat_read(chat)` RPC | Writes only when something is unread |
| New messages, reactions, receipts | Supabase Realtime `postgres_changes` | Filtered by the same SELECT policies |
| Typing | Realtime private broadcast `coach-chat:<chat id>` | Never touches a table |
| Photo / voice links | `GET /api/chats/files?ids=…` | One read, one batch signing call for up to 30 files |

Measured on PGlite (WASM Postgres, slower than a server) with 250 fighters and
50,000 messages, warm: open a chat 2 ms, page back 1 ms, send 3 ms, mark read
1 ms, inbox for a full 250-fighter roster 28 ms.

Sends are optimistic: the bubble shows at once and is confirmed or marked
"Not sent · Retry".

## Security

- Clients get `SELECT` only, through participant RLS. On the coach side the
  policy also needs the live connection, so **disconnecting ends a coach's
  access on the next request**; the fighter keeps the history read-only, as
  with feedback. Realtime applies the same policies.
- Every write is a `coach_private` security-definer function behind a
  security-invoker RPC, the same boundary as coach feedback. `coach_chat_send`
  locks the accepted connection and the chat row, rechecks approved roles,
  and accepts only:
  - text up to 2,000 characters;
  - the sender's own clean (`ready`) photo or voice file, used once;
  - a reply to a message in the same chat;
  - a quoted record that belongs to the fighter.
  Quoting needs the fighter's health-data consent. The card is built by the
  database, and injury or rehab work reads "Programme session" without
  injury sharing.
- Flood guard: 30 messages a minute per sender per chat (`PT429`).
- Reactions are one of six fixed emoji, one per participant, only while
  connected. Either side can report a message from the other
  (`coach_message_reports`, service-role only).
- Pushes never carry message text: "Leo · New message. Open the chat to read
  it." They use the recipient's "Coach messages" switch and quiet hours, and
  a new `conversation` class with its own daily allowance (40), so a busy chat
  never uses up the event allowance that plan, feedback and safety alerts
  share. Only the sender can trigger a push, once per message, within 10
  minutes.
- File links last 120 seconds and are issued per request after the participant
  check. Chat files are kept by attachment cleanup.
- The typing channel accepts only the chat's connected participants.

## Check-in nudges

`coach_checkin_nudges` holds one row per pair: weekdays (bit 0 = Monday), a
local time between 05:00 and 21:00, and on/off. The worker runs
`claim_coach_checkin_nudges()` every 5 minutes
(`UNLXCK_COACH_NUDGE_SWEEP_INTERVAL_SECONDS`). One statement finds every nudge
due in the fighter's own time zone within two hours of the chosen time, skips
fighters who already checked in today, and stamps the day, so a nudge is sent
at most once a day even with two workers.

- **Never for under-18s or without health-data consent.** The children's
  policy forbids nudging a child to disclose health data, and a check-in is
  health data. Both the setting and the claim enforce it.
- The push uses the fighter's `checkin_reminders` switch and quiet hours, and
  shares the readiness reminder's tag, so the device shows one.
- Chats shows **Missed check-in** for a fighter nudged in the last 20 hours
  who still has not checked in.

## Decisions

- **Guardians cannot see an under-18 fighter's chat.** No guardian accounts
  exist, and the children's policy forbids assuming parental access. Juniors
  get the same private chat with a visible Report option, and no nudges.
- **A chat starts with the first message.** Connected fighters without a chat
  still appear in the list ("Start the conversation").
- **Retention:** messages stay while both accounts exist. The coach loses
  access on disconnect, the fighter keeps history, and either account's
  deletion removes the chat.

## Deployment order

1. Apply `20261123020539_coach_chat_and_checkin_nudges.sql`. It is additive.
   It widens the `notification_deliveries` class check, adds three tables and
   the RPCs, adds `coach_messages` and `coach_chats` to the
   `supabase_realtime` publication, adds the typing-channel policies on
   `realtime.messages`, and teaches attachment cleanup to keep chat files.
2. Deploy the backend (routes, push class, worker sweep). The runtime schema
   gate requires the new tables and functions.
3. Deploy the frontend.

Rollback: application first. Do not drop the tables once messages exist.

## Verification

- `PGLITE_ROOT=<isolated install of @electric-sql/pglite> node tests/sql/coach_chat.test.mjs`
  checks permissions, idempotent sends, file and card rules, the flood guard,
  reactions and reports, nudge age and consent rules, once-a-day claims,
  cleanup, and disconnect.
- `python -m pytest tests/test_coach_chat.py` covers pushes (who, what, never
  the text), file links and their access rules, and the nudge sweep.
- `cd web && node --import tsx --test lib/coach-chat.test.ts` covers merge and
  dedupe, receipts, link parsing, error text and the day mask.
- Not yet verified against hosted Supabase: Realtime delivery and the private
  typing channel. Check them in staging with two accounts before release.
