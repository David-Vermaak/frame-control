# Contributing to Frame Control

This guide exists to save both sides time. The process is borrowed from
[pi](https://github.com/badlogic/pi-mono/blob/main/CONTRIBUTING.md).

## Just want to report something?

Use the [feedback form](https://frame-control.pages.dev/feedback/). It needs no
GitHub account, and what you send becomes an issue here that stays open.

## The One Rule

**You must understand your code.** If you can't explain what your change does
and how it interacts with the rest of the app, your PR will be closed.

Using AI to write code is fine. Submitting AI-generated slop you don't
understand is not.

## Contribution gate

Issues and PRs opened on GitHub by new contributors are auto-closed by default.
A maintainer reviews auto-closed issues regularly and reopens worthwhile ones.
Issues that don't meet the quality bar below won't be reopened or get a reply.

Approval happens through maintainer replies on issues:

- `lgtmi`: your future issues won't be auto-closed
- `lgtm`: your future issues and PRs won't be auto-closed

The word must be at the start of the reply (optionally after one or more
`@username` mentions) or at the end. Only `lgtm` lets you open PRs. Approved
people are listed in [`.github/APPROVED_CONTRIBUTORS`](.github/APPROVED_CONTRIBUTORS).

## Quality bar for issues

Use one of the issue templates, and keep it short, concrete and worth reading.

- If it doesn't fit on one screen, it's too long.
- Write in your own voice. If you must use an LLM, say so in a clearly labelled
  follow-up comment.
- State the bug or request clearly, and why it matters.
- For bugs, include your OS, your SteamOS build (Steam Settings → System), and
  the server log (**Frame → Show Server Log** in the app).
- If you want to implement the change yourself, say so.

## Before opening a PR

Don't open a PR until a maintainer has approved you with `lgtm`. Open an
[idea or contribution proposal](https://github.com/saphid/frame-control/issues/new?template=idea.yml)
first.

Then check your change:

```sh
python3 -m unittest discover -s tests   # server tests; no headset needed
node --test site/test/*.test.mjs        # website feedback function
```

Say what you tested, and whether you tried it on a real Steam Frame.

## Blocking

If you ignore this document twice, or spam the tracker with agent-generated
issues, your GitHub account will be blocked from the repo.

## Why auto-close?

This is a hobby project with one maintainer. Auto-closing is a buffer against
burnout and tracker spam: issues get reviewed on the maintainer's schedule, and
the good ones are reopened. Short, concrete, reproducible reports and thoughtful
contributions are welcome.
