# Group chats

## One-time BotFather setup

| Command | Setting | Why |
| --- | --- | --- |
| `/setjoingroups` | **Enable** | otherwise nobody can add the bot to a group |
| `/setprivacy` | **Enable** (the default) | the bot receives only commands, replies to its own messages and service messages — not everything people write |

The bot also needs the right to **send media** in the group. In restricted groups an admin
grants it under the bot's member permissions (or promotes it). Without it the bot replies
"I'm not allowed to send media in this chat" and the job is dropped.

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

## What the bot stores

`group:<chat_id>` in Redis: title, type, who added it, when. Added on join, removed when the
bot is kicked or leaves, moved to the new id when a group becomes a supergroup. `/stats`
shows the count.

## Roadmap for groups

| Step | What | State |
| --- | --- | --- |
| G1 | Join/leave tracking, migration, `/dl` / reply / mention triggers, in-context replies, media-rights errors | done |
| G2 | `/settings` for group admins: auto-download on/off, max quality, allowed platforms; auto mode needs privacy off or admin and skips the quality menu | next |
| G3 | Per-group hourly limits, per-chat concurrency cap so one busy group can't starve others, quiet failures in auto mode | |
| G4 | `/stats` split private vs group, group count in alerts, docs polish | |
