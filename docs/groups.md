# Group chats

## One-time BotFather setup

| Command | Setting | Why |
| --- | --- | --- |
| `/setjoingroups` | **Enable** | otherwise nobody can add the bot to a group |
| `/setprivacy` | **Enable** (the default) | the bot receives only commands, replies to its own messages and service messages — not everything people write |

**Make the bot an admin of the group.** With privacy mode on (the default), Telegram only
delivers `/commands` and replies to the bot's own messages — not plain links or `@mentions`.
An admin bot receives everything, which is what `@bot <link>` and auto-download need. The
greeting says so: as a plain member it asks to be made an admin and lists only `/dl`; once
promoted it thanks you and lists mentions and auto-download. (Alternatively turn privacy
mode off in BotFather for every group at once; the bot detects either.)

The bot also needs the right to **send media** in the group. In restricted groups an admin
grants it under the bot's member permissions. Without it the bot replies "I'm not allowed to
send media in this chat" and the job is dropped.

## How people use it in a group

With privacy mode on (recommended), these work:

- `/dl <link>` — download that link
- reply to a message that contains a link with `/dl` — the bot replies to *that* message
- `/dl <link> audio` — YouTube audio only

`@botname <link>` only reaches the bot when privacy mode is **off** or the bot is an admin
(Telegram does not forward mere mentions to privacy-mode bots). The bot handles it either
way; the docs just can't promise Telegram delivers it.

The bot replies to the message that carried the link, inside its forum topic if the group
uses topics. The "⏳ Working on it…" placeholder is deleted on success and edited to a short
reason on failure, so the bot never piles up messages. The quality menu for YouTube can only
be used by the person who asked; everyone else gets "Only the person who asked can choose".

Per-user limits (2 at a time, 20/hour) apply inside groups exactly as in private chats.

## Limits and fairness

On top of the per-user limits, each group has its own: `MAX_ACTIVE_PER_CHAT` downloads in
flight (default 2) and `GROUP_HOURLY_LIMIT` requests per hour (default 60), shared by all
its members. That is the fairness mechanism: a worker with concurrency 3 or 4 can never be
fully occupied by one group, so private chats and other groups keep getting slots, and the
queue behind it stays short. Keep `MAX_ACTIVE_PER_CHAT` below each worker's concurrency.

When a group limit is hit, `/dl` gets a one-line reply ("This group already has 2 downloads
running…"); in auto mode the link is skipped silently. The refusal holds nothing: a user
turned away by a group limit can still download in a private chat.

## Per-group settings (`/settings`, admins only)

| Setting | Default | Effect |
| --- | --- | --- |
| Auto-download | off | When on, every supported link posted in the group is fetched without `/dl`: no "working" message, no quality menu (the max quality is used), and failures are only logged, never posted. Telegram only delivers plain messages to the bot when it is an **admin** or privacy mode is **off** — turn auto on without one of those and nothing happens. |
| Max quality | best | Caps YouTube: `/dl` menus only show rungs up to the cap, auto mode downloads at the cap. `audio` always allowed. |
| Platforms | all | Links from a disabled platform are refused with a short reply (`/dl`) or ignored (auto). |

Taps on the keyboard are re-checked against the group's admin list and the chat the keyboard
belongs to, so forwarding a settings message elsewhere does nothing. Anonymous admins
(posting as the group) count as admins.

## What the bot stores

`group:<chat_id>` in Redis: title, type, who added it, when. Added on join, removed when the
bot is kicked or leaves, moved to the new id when a group becomes a supergroup. `/stats`
shows the count.

## Roadmap for groups

| Step | What | State |
| --- | --- | --- |
| G1 | Join/leave tracking, migration, `/dl` / reply / mention triggers, in-context replies, media-rights errors | done |
| G2 | `/settings` for group admins: auto-download on/off, max quality, allowed platforms; auto mode needs privacy off or admin and skips the quality menu | done |
| G3 | Per-group hourly limits, per-chat concurrency cap so one busy group can't starve others, quiet failures in auto mode | done |
| G4 | `/stats` split private vs group, group counts, docs | done |

## Live checklist (one pass over G1–G4)

1. BotFather: `/setjoingroups` → Enable. Keep `/setprivacy` enabled for now.
2. Add the bot to a test group → greeting; `redis-cli keys 'group:*'` shows `group:<id>`.
3. `/dl <youtube link>` → quality menu as a reply to your message → video replied to it.
   Another member taps the menu → "Only the person who asked can choose".
4. Post a link, reply to it with `/dl` → video replies to the *original* message.
5. `/settings` as a non-admin → refused. As admin: set *Max quality* to 480p →
   `/dl <youtube link>` shows no rung above 480p. Turn YouTube off → `/dl <youtube link>`
   says it is turned off; Instagram still works.
6. Auto-download: promote the bot to admin (or BotFather `/setprivacy` → Disable and re-add
   the bot), turn *Auto-download* on → post a link with no command → media arrives as a
   reply with no "working" message; post a TikTok link → nothing at all.
7. Limits: with `MAX_ACTIVE_PER_CHAT=1` in `.env`, two members `/dl` long videos back to
   back → the second gets "This group already has 1 downloads running"; a private chat
   `/dl` at the same time still goes through.
8. Revoke the bot's *send media* right → `/dl` yields "I'm not allowed to send media".
9. `/stats` (admin, private chat) shows the group lines; remove the bot → entry gone.
