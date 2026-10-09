# Security Policy

## Reporting a vulnerability

Open a [private security advisory](https://github.com/p-a-x-e-m/PonishaBot/security/advisories/new)
on this repository. If you cannot use advisories, open a minimal issue asking for a
private channel — do not put the details in the issue.

Please include: what you did, what happened, the impact, and the version or commit you
tested. A proof of concept helps but is not required.

**Do not** open a public issue for anything that could expose a working session cookie or
token — this project stores live credentials, and a public report is itself the leak.

There is no bounty. Expect an acknowledgement within a few days; this is a personal
project, not a staffed product.

## Scope

In scope:

- The Python package (`ponishabot/`), the Django dashboard (`dashboard/`, `web/`), and
  the files under `deploy/`.
- Anything that leaks credentials, allows code execution, or bypasses the dashboard's
  (non-existent) access control.

Out of scope:

- The dashboard having no authentication. That is by design: it binds to `127.0.0.1` and
  expects nginx basic auth or an SSH tunnel in front of it. Exposing it directly is a
  misconfiguration, not a vulnerability — but if you find a way for a *remote* attacker
  to reach it through a correct deployment, that is in scope.
- The bot's core purpose: automating proposal submission on ponisha.ir. Whether that
  complies with the site's terms is a question for the operator, not a security report.
- ponisha.ir's own security.

## What this project stores, and where

Worth knowing before you test, because most of the risk is here:

| Location | Contents | Committed? |
|---|---|---|
| `ponisha.db` | Live session cookies — a full account takeover if leaked | No (gitignored) |
| `config.yaml` | Telegram bot token, AI API key | No (gitignored) |
| `deploy/bot.env` | `SECRET_KEY` and the same credentials | No (gitignored) |
| `bid_log.json` | Which projects have been bid on | No (gitignored) |
| `.env` | Nothing reads it; a reminder of what to export | No (gitignored) |

The OTP login path stores a Bearer JWT in `ponisha.db`'s `meta` table; the browser-login
path stores a `pss-at` cookie. Either one alone authenticates the account.

**Never attach any of these files to a report.** Redact tokens down to their length and
prefix.

## Deploying safely

The dashboard exposes the Telegram token and AI key in the settings page HTML and has no
authentication. Before putting it anywhere but localhost:

- Front it with nginx basic auth (`deploy/nginx.conf.example`) or an SSH tunnel.
- Never run it behind multiple workers. It is a single process holding a monitor thread
  and a Selenium driver; two instances means duplicate bids. See the README.
- Keep `bid.dry_run: true` until you have watched a full cycle.

## Supported versions

Only the tip of `main` is supported. There are no maintained release branches.
