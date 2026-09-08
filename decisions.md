# Decisions Log

Architecture and design decisions for mina-bot, in the order they were made.

---

## [2026-09-05] Durable submissions/metrics storage, separate from state.json

- **Status:** Accepted
- **Context:** `state.json` is overwritten fresh every morning by `run_morning`, which silently destroyed all checkme attempt/feedback history each day. `on_message` never stored public attempts at all — there was no history of the community's actual practice, only the current day's live session state.
- **Options Considered:**
  1. Stop wiping `state.json` / extend its lifetime. *Pros:* minimal change. *Cons:* `state.json` is meant to represent the current day's live session (posted_at/revealed_at, in-session checkme continuity); conflating it with permanent history breaks that model and the daily-reset behavior other code depends on.
  2. Add a new `storage.py` module with two append-only files: `submissions.jsonl` (rich per-attempt detail: content, feedback, voice path — trimmed to the most recent 7 per user) and `metrics.jsonl` (lean, analytics-only fields, never trimmed). *Pros:* clean separation between "what happened" and "today's live session"; `run_morning`/`run_evening`'s existing reset behavior stays untouched. *Cons:* two new files with different retention semantics to keep straight.
- **Decision:** Option 2. `storage.py` owns both files; `on_message` and `append_checkme_exchange` write to them without changing any existing `state.json` behavior.
- **Consequences:** Attempt and feedback history now survives the daily reset. Any future feature that wants "history" has to pick the right file — `submissions.jsonl` for recent detail, `metrics.jsonl` for anything spanning further back or needing completeness.

---

## [2026-09-05] is_test flag with per-(user, is_test) rolling trim

- **Status:** Accepted
- **Context:** Dev-channel testing needed to write to the same `submissions.jsonl` used by real users, without heavy test traffic evicting real users' most recent attempts out of the 7-entry retention window.
- **Options Considered:**
  1. Write dev-channel test data to entirely separate files. *Pros:* total isolation. *Cons:* doubles the file surface and duplicates all read/write logic.
  2. Single file, an `is_test` flag required on every row, with retention trimmed independently per `(user_id, is_test)` bucket. *Pros:* one file, one code path; a user's real and test rows can never compete for the same 7-slot window. *Cons:* every write call site must always pass `is_test` explicitly — it's a required argument, not a default.
- **Decision:** Option 2.
- **Consequences:** Dev testing can be arbitrarily heavy without threatening real users' retained history (verified directly: 10 test writes + 3 real writes for the same fake user correctly kept all 3 real rows and only the most recent 7 test rows). Every future `storage.append_*` call site has to remember which bucket it's writing to.

---

## [2026-09-05] Channel-based state file routing for /checkme

- **Status:** Accepted
- **Context:** `/checkme` and `append_checkme_exchange` only ever read/wrote the real `STATE_FILE`, so there was no way to exercise the full checkme flow in the dev channel without risking real users seeing feedback generated against test data.
- **Options Considered:**
  1. Keep a single shared state file and gate dev testing some other way. *Pros:* simplest. *Cons:* doesn't solve the actual requirement — testing the full flow end-to-end.
  2. Add `state_file_for_channel(channel_id)`, routing `BOT_DEV_CHANNEL_ID` to `TEST_STATE_FILE` and `MINA_BOT_CHANNEL_ID` to `STATE_FILE`, threaded through `send_checkme_feedback` and `on_message`. *Pros:* dev channel gets a fully working, isolated checkme flow (streaks, daily limit, and participation all separately exempted for test traffic). *Cons:* a larger diff than a minimal fix, and it touches `on_message`'s active-window check.
- **Decision:** Option 2.
- **Consequences:** Dev-channel testing now exercises the real code path, not a stub. Every future channel-aware feature has to remember to route through this function rather than assuming `STATE_FILE`.

---

## [2026-09-06] Multi-channel voice logging: voice-only, metrics-only, outside #minabot

- **Status:** Accepted
- **Context:** Extending presence logging beyond `MINA_BOT_CHANNEL_ID` raised a real privacy question: what's acceptable to record from channels people joined for reasons unrelated to Japanese practice, and never opted into being measured in.
- **Options Considered:**
  1. Log full content and a `submissions.jsonl` row from every channel, same treatment as `MINA_BOT_CHANNEL_ID`. *Pros:* richest possible data. *Cons:* stores people's actual message text from channels with no expectation of being logged — rejected on privacy grounds.
  2. Log voice messages only, to `metrics.jsonl` only (`channel_id`, `channel_name`, `user_id`, `is_voice`, `timestamp` — no content, no submissions row, no audio download), for every channel the bot can access except `BOT_DEV_CHANNEL_ID`. *Pros:* respects the implicit privacy boundary of general channels while still capturing the one cross-channel signal that's actually useful — voice adoption. *Cons:* `metrics.jsonl` now holds two different row shapes, which broke code that indexed `row["type"]` directly (fixed by switching to `.get("type")` in six places).
- **Decision:** Option 2.
- **Consequences:** Enabled real cross-channel voice analysis (the FUNNEL section, per-channel voice rates) without ever capturing anyone's words outside the feature they chose to use. Every future `metrics.jsonl` consumer has to handle both row shapes rather than assume one uniform schema.

---

## [2026-09-06] backfill.py idempotency: message_id with a composite-key fallback

- **Status:** Accepted
- **Context:** Re-running `backfill.py` re-inserted every already-backfilled row into `metrics.jsonl` as a fresh duplicate — `submissions.jsonl` self-healed via its trim, but `metrics.jsonl` never trims. Discovered after a real incident: 131 duplicate rows from two runs against the same channel.
- **Options Considered:**
  1. Re-scan Discord, patch a `message_id` onto the ~290 pre-existing rows that predate the field, then match on `message_id` alone from then on. *Pros:* one matching strategy, permanently. *Cons:* costs another full multi-channel Discord scan and an Oracle stop/restart cycle; still has to match old rows to messages by `(user_id, timestamp)` internally to perform the patch, so it doesn't remove that assumption — just relocates it to a one-time step.
  2. Match on `message_id` when both sides have one; fall back to `(user_id, timestamp, channel_id)` for rows written before the field existed. Never patch the old rows. *Pros:* zero extra Discord calls or downtime; gets strictly more precise over time as more rows carry a real `message_id`. *Cons:* two matching strategies live in the code permanently, since the pre-existing rows will never gain a `message_id` under this approach.
- **Decision:** Option 2, chosen explicitly over Option 1 after weighing both.
- **Consequences:** `backfill.py` can be re-run safely at any time — verified against both the local and Oracle datasets post-fix, reporting 0 new/100% already-backfilled on a clean re-run. A future reader of `backfill.py` needs the context for why two matching strategies coexist.

---

## [2026-09-06] Weekly report: explicit MINABOT / SERVER-WIDE / FUNNEL scoping

- **Status:** Accepted
- **Context:** Once multi-channel logging landed, the report's engagement metrics (active users, total attempts, etc.) started silently pulling from the full cross-channel dataset instead of just the `#minabot` feature they were meant to describe — "active users" was inflated by people who'd only ever sent a voice memo in an unrelated channel.
- **Options Considered:**
  1. Leave the metrics channel-agnostic and document the caveat. *Pros:* no code change. *Cons:* the numbers stay actively misleading.
  2. Split every metric into three explicitly labeled sections — MINABOT (rows with no `channel_name`), SERVER-WIDE (voice, all channels), FUNNEL (kaiwa crew vs. broader membership) — and scope each function's input rows accordingly.
- **Decision:** Option 2.
- **Consequences:** Every number in the report now means exactly what its section header says. Computing a MINABOT metric requires remembering to filter to mina-bot rows at the call site, which is easy to get wrong in a future addition — the dashboard's `voice_per_channel` bucketing hit this exact bug (bucket range clipped to mina-bot's date range) before it shipped.

---

## [2026-09-07] Admin dashboard reachability: localhost-only + SSH tunnel

- **Status:** Accepted
- **Context:** The dashboard's only authentication is a single HTTP Basic Auth password from `.env`. How it's exposed on the network determines whether that's actually sufficient.
- **Options Considered:**
  1. Bind to `0.0.0.0`, open the port publicly, plain HTTP. *Pros:* reachable from anywhere, no extra setup. *Cons:* Basic Auth credentials travel effectively in cleartext on every request.
  2. Bind to `0.0.0.0` behind a reverse proxy doing TLS (Caddy/nginx + Let's Encrypt). *Pros:* safe to expose publicly. *Cons:* needs a domain name, reverse-proxy configuration, and certificate renewal — real ongoing maintenance for a personal admin tool.
  3. Bind to `127.0.0.1` only, reached via `ssh -L` tunnel. *Pros:* no TLS needed since traffic never leaves the VM unencrypted; least setup. *Cons:* only reachable with SSH access, not a plain browsable URL.
- **Decision:** Option 3.
- **Consequences:** Basic Auth is safe as the sole auth layer, with nothing extra to maintain. Reaching the dashboard always requires an SSH tunnel first.

---

## [2026-09-07] dashboard_data.py: duplicate primitives, never import bot.py

- **Status:** Accepted
- **Context:** `dashboard_data.py` needed several primitives that already exist in `metrics_report.py` (`TIMEZONE`, mina-bot row scoping, fraction formatting) and a handful of constants that live in `bot.py` (`TIMEZONE`, `MINA_BOT_CHANNEL_ID`, `LEVEL_LABEL_MOVED_TO_EVENING`, the evening-reveal hour). `bot.run()` is already guarded behind `if __name__ == "__main__"`, so importing `bot.py` from another script is technically safe.
- **Options Considered:**
  1. Extract the shared primitives into a new `metrics_lib.py`, imported by both `metrics_report.py` and `dashboard_data.py`. *Pros:* single source of truth, no drift risk. *Cons:* touches `metrics_report.py`, which is already deployed and working, immediately after a real production incident (the backfill duplication bug) — risk of a change rippling further than expected.
  2. Duplicate the handful of needed primitives directly in `dashboard_data.py`; never import `bot.py` at all, even though doing so would be safe. *Pros:* zero risk to the deployed report code; the dashboard never constructs live Anthropic/Discord client objects for no reason. *Cons:* constants and small helper functions now have two hand-maintained copies.
- **Decision:** Option 2, on both counts — accept the duplication, and don't import `bot.py` even though it's technically safe to.
- **Consequences:** `metrics_report.py` stays untouched and low-risk; the dashboard has no accidental coupling to Discord/Anthropic client construction. Four constants and several small functions will drift if one copy changes without the other being updated by hand. Revisit extraction once the dashboard's shape has settled.

---

## [2026-09-07] Self-declared level: bot writes a snapshot, dashboard never touches Discord

- **Status:** Accepted
- **Context:** The self-declared-level chart needs live Discord role membership (who holds N5/N4/N3/N2/N1/日本人), but the dashboard process is required to be read-only and Discord-independent.
- **Options Considered:**
  1. Have `dashboard.py` connect to Discord itself to fetch role membership on each page load. *Pros:* always fresh. *Cons:* gives the read-only dashboard a live Discord dependency and its own token/connection to manage — breaks the "dashboard never connects to Discord" boundary.
  2. Have `bot.py` (already connected) periodically snapshot role membership to a gitignored JSON file; `dashboard_data.py` only ever reads that file. Refresh is explicit: tied to the weekly report run, plus a manual `!refreshlevels` command.
- **Decision:** Option 2.
- **Consequences:** The dashboard's "read-only, no Discord connection" property holds without exception. The self-declared-level chart can lag reality between refreshes. Every human member — including those holding none of the tracked roles — has to be written to the snapshot (with an empty list) so "unknown" is distinguishable from "not yet scanned."

---

## [2026-09-06] Move JLPT level label from morning drop to evening reveal

- **Status:** Accepted
- **Context:** The morning drop displayed the day's difficulty ("college level, around JLPT N2"). Most members have never taken a JLPT test and don't know where they sit, so the label may cause self-selection out before attempting. It also confounds any measurement of whether difficulty affects participation, since members are reacting to the label rather than the sentence.
- **Options Considered:**
  a. **Keep the label in the morning.** Pro: gives members context for whether the sentence is a stretch. Con: invites self-assessment against a scale they can't calibrate; suppresses attempts on high-level days for reasons unrelated to actual difficulty.
  b. **Remove the label entirely.** Pro: fully level-blind. Con: loses genuinely useful information after the fact, when knowing "that was N2" is encouraging rather than deterring.
  c. **Move it to the evening reveal.** Pro: morning prompt is level-blind; members still learn what they attempted. Con: none identified.
- **Decision:** (c). The label's informational value is retrospective, not prospective. Members don't need to know the difficulty to attempt — only to interpret how they did.
- **Consequences:** Participation-by-level becomes a clean measurement rather than a reaction to a label. Creates a natural experiment split on 2026-09-06; the report shows "insufficient data" until 7+ days exist on each side. Some members may be surprised by difficulty mid-attempt with no warning.

---

## [2026-09-06] Retention: raw content expires, derived metrics persist

- **Status:** Accepted
- **Context:** The member-facing report requires longitudinal data ("you've started using structures you weren't using a month ago"), but holding members' Japanese attempts and voice recordings indefinitely is not something I want to do.
- **Options Considered:**
  a. **Flat 7-day retention on everything.** Pro: simple, clearly privacy-respecting. Con: destroys the product — you cannot show a month-long trend from seven days of data.
  b. **Retain everything indefinitely.** Pro: maximum analytical flexibility. Con: unnecessary custody of personal content, and most of it is never read again.
  c. **Split raw from derived.** Raw attempt text, feedback text, and audio expire after 7 attempts per user. Derived numbers — timestamps, grammar point, character counts, voice flag, exchange depth — persist. Pro: full trend capability with a one-week window on actual content. Con: cannot cite old attempts as evidence in a report; derived metrics must be computed at write time, not retroactively.
- **Decision:** (c). The sensitive material is the Japanese someone wrote and their recorded voice. A row reading `{2026-09-01, 〜たら, avoided, 34 chars, text}` is barely personal data. This is more privacy-respecting than a flat rule, because a flat rule keeps a full week of content that mostly isn't needed.
- **Consequences:** "Show the receipts" works for recent claims only — older patterns are counts without examples. Trimming a submission row must also delete its `.ogg` file, or retention silently doesn't cover the most personal data. Derived fields cannot be added retroactively, so any new metric starts from the day it's implemented.

---

## [2026-09-06] Keep streaks off the member-facing dashboard

- **Status:** Accepted
- **Context:** minabot already has a streak mechanic (`current_streak`, `longest_streak`, `/streak`). The dashboard's purpose is to test whether informational feedback alone — no score, no penalty — brings someone back to practice.
- **Options Considered:**
  a. **Show the streak on the dashboard.** Pro: proven engagement driver, already built, likely lifts return visits. Con: any return becomes unattributable — the streak is doing work the feedback was supposed to do, and the experiment is confounded by my own existing mechanic.
  b. **Remove the streak from the product entirely.** Pro: cleanest test. Con: removing an existing reward is a one-way door (Gneezy & Rustichini — lateness stayed elevated after the fine was withdrawn), and members currently use it.
  c. **Leave `/streak` in Discord, keep the number off the dashboard.**
- **Decision:** (c). Deliberately withholding a mechanic I already have, in order to isolate the one I'm testing.
- **Consequences:** Dashboard return rates will likely be lower than if streaks were shown, and that's the point — a return can be attributed to the feedback. Also a defensible design decision to explain externally. Risk: if return rates are poor, the temptation will be to add streaks, which forecloses the test permanently.

---

## [2026-09-06] Log voice server-wide, text only in #minabot

- **Status:** Accepted
- **Context:** Voice output was only measured in `#minabot`, so server-wide speaking behavior was invisible. Widening collection means logging activity from channels people joined for community reasons, not to be measured.
- **Options Considered:**
  a. **Keep logging scoped to #minabot only.** Pro: narrowest consent footprint. Con: cannot answer whether different channels reach different people — which turned out to be the most important question available.
  b. **Log all messages, all channels, with content.** Pro: maximum analytical power. Con: storing members' messages from social channels is a real overreach for the question being asked.
  c. **Log voice presence server-wide (channel, user, timestamp, no content); text with content only in #minabot.**
- **Decision:** (c). Presence data answers the question; content isn't needed for it. DMs excluded — a DM isn't a channel someone chose to be measured in.
- **Consequences:** Made the finding possible that 9 of 10 voice senders are channel-exclusive — different surfaces reach different people. `metrics.jsonl` grows faster. Members have not yet been told any of this; a data-retention notice is owed before the dashboard ships.

---

## [2026-09-07] Reject /pair me and partner-matching mechanics

- **Status:** Accepted
- **Context:** 69% of survey respondents (33/48) named "I don't have anyone to practice with" as their biggest speaking barrier — the top answer by a wide margin. The obvious response is pairing members for practice.
- **Options Considered:**
  a. **Build `/pair me`** — drop two members into a private voice room with a prompt. Pro: directly addresses the stated barrier; validates concurrency before committing to a bigger build. Con: accountability is unreliable; converts practice into an obligation; no compatibility guarantee; recreates HelloTalk, which several respondents named as their *least* helpful resource.
  b. **Build co-presence without interaction** — a space where members are present and not obligated to speak.
  c. **Do nothing until "no one to practice with" is disambiguated.** The phrase could mean a partner, an audience, co-presence, or simply an occasion to speak — only the first requires matching, scheduling, and compatibility.
- **Decision:** (a) rejected. Between (b) and (c), take (c) first — ask members what they meant before designing for it.
- **Consequences:** Leaves the top-cited barrier unaddressed for now. Notably, every path to relatedness so far has failed on either facilitation burden (conversation tables) or reliability (pairing) — which may mean Minaverse can serve the "can't find words" and "scared of sounding wrong" barriers well and cannot solve the partner barrier without something already ruled out. That's worth stating explicitly rather than arriving at by elimination.

---

## [2026-09-07] Zero-voice denominator is all verified members, not kaiwa crew

- **Status:** Accepted
- **Context:** "Never gone voice" needs a denominator, and the choice determines what the metric means. Candidates: all verified members (482, `len(roles) > 1`), kaiwa crew (25, the daily-ping opt-in), or ever-attempted (12).
- **Options Considered:**
  a. **Kaiwa crew (25).** Pro: people who opted into daily practice. Con: kaiwa crew is a notification opt-in, not an access gate — all verified members can see and post in `#minabot`, so this excludes people who could have spoken.
  b. **All verified members (482).** Pro: the true population with access and therefore the true opportunity denominator. Con: includes many who never engaged with the practice feature at all, making the percentage look extreme.
  c. **Report all three.**
- **Decision:** (b) as the headline, with (c) in the report — three denominators answering three different questions: who could have spoken, who opted into prompts, and who practices but doesn't speak.
- **Consequences:** The headline number (97%, 467/482) reads as a funnel finding rather than a speaking-friction finding, and needs the other two lines beside it to be interpreted correctly. Requires the members intent and a role snapshot, since role data is not in the JSONL files.

---

## [2026-09-07] PENDING — Which segment does minabot serve?

- **Status:** Proposed
- **Context:** Of 253 members holding the self-declared N5 role, **1 has ever produced a single logged row** anywhere the bot measures. N2 members are roughly 6x more likely to attempt and 8x more likely to send voice. The check-in channel was killed because advanced members set an implicit bar that discouraged beginners — that dynamic appears to have followed into minabot despite hint keywords and a structured prompt.
- **Options Considered:**
  a. **Serve the advanced users who already produce.** Pro: they're active, engaged, and the product works for them. Con: ~12 people; the server's majority is beginners.
  b. **Rebuild for beginners.** Pro: 253 N5 members represent the actual unmet need. Con: requires solving the implicit-bar problem, which has already defeated one channel design; a different product from what exists.
  c. **Attempt both.** Con: likely serves neither well at current capacity.
- **Decision:** Not yet made. Blocked on qualitative input.
- **Next step:** Private threads with chartlez (the single N5 who produces — what made it possible?) and 4–5 silent N5 members from the survey consent list (isai_xov, mizafa, maissag_, flamingyawn, mkreptile, psykko_smokke, macekoel, shurahos, stalexi). Decide after.
