# Roadmap status

Tick a step only when its done-criteria pass. "Needs a live check" means the code is in and
unit-tested, but the real round-trip against YouTube/Instagram/Pinterest/Telegram still has
to be run from a machine with network access (`make dev`, send the bot a link).

| Step | What | State |
| --- | --- | --- |
| 0 | Repo skeleton, Compose, Makefile, CLAUDE.md | done |
| 1 | Contracts, detector, settings, logging | done (33 URL forms + 18 rejects tested) |
| 2 | Gateway MVP: commands, enqueue, per-user limits | done |
| 3 | YouTube worker, quality ladder, cache | done — needs a live check |
| 4 | Instagram worker (gallery-dl, yt-dlp fallback) | done — needs a live check |
| 5 | Pinterest worker (pin.it resolve, boards ≤ 10) | done — needs a live check |
| 6 | Quality keyboard from a probe, 60 s default | done — needs a live check |
| 7 | Resource controls: janitor, disk guard, mem limits, rate limit | done |
| 8 | Breaker, heartbeats, transient retries, /health | done |
| 9 | Self-hosted Bot API server | compose profile + docs; opt-in |
| 10 | Tests, CI, 50-user load test | done — `docs/loadtest-report.md` |
| 11 | Server release: GHCR image, ssh deploy, weekly rebuild, backups | done — needs a VPS and secrets |
| 12 | /stats, admin alerts, log shipping notes | done |
| 13 | Scaling moves | documented in `docs/deploy.md` |
| G1 | Groups: join/leave tracking, migration, `/dl` / reply / mention triggers, in-context replies, media-rights errors | done — needs a live check |
| G2 | `/settings` for group admins: auto-download, max quality, platforms | done — needs a live check |
| G3 | Per-group hourly limit and in-flight cap (fairness), silent refusals in auto mode | done |
| G4 | `/stats` split private vs group, group counts, docs | done |

See `docs/groups.md` for the group-chat behaviour and BotFather settings.

Open questions from the roadmap: bot `@username` (reserve with BotFather), default quality
(currently best under the cap), Instagram cookies policy (public-only until a cookies file is
mounted), repo visibility.
