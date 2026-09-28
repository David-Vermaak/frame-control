# Website

The Frame Control website, <https://frame-control.pages.dev>, on Cloudflare Pages.

- `public/`: static pages. `/` is the landing page, `/feedback/` the feedback form, `/privacy/` the privacy note.
- `functions/api/feedback.js`: `POST /api/feedback`, which turns the form into a GitHub issue labelled `feedback`.
- `lib/feedback.js`: validation and issue formatting, tested by `test/feedback.test.mjs`.
- `public/js/site.js`: settings, including the Ko-fi page name for the donate buttons.

## Feedback → GitHub issues

The function needs a `GITHUB_TOKEN` secret: a fine-grained token with **Issues: read and write** on
`saphid/frame-control` only. Issues are opened as the token's owner, so they pass the contributor gate
(`.github/workflows/issue-gate.yml`) and stay open. Without the token the form answers 503 and offers a
prefilled GitHub issue instead.

```sh
cd site
npx wrangler pages secret put GITHUB_TOKEN --project-name frame-control
```

Spam protection: a hidden honeypot field, a 3-second minimum fill time, 5 submissions per hour per IP
(a salted hash, kept in the `FEEDBACK_RL` KV namespace for about an hour), and 100 a day in total.
User text has `@mentions` and `#123` references broken so nobody gets pinged.

## Run and deploy

```sh
cd site
node --test test/*.test.mjs
npx wrangler pages dev --port 8788           # local; put GITHUB_TOKEN/GITHUB_REPO in .dev.vars to test issues
npx wrangler pages deploy --branch main      # production
```

Point `GITHUB_REPO` in `.dev.vars` at a scratch repo when testing locally so test issues don't land on the real tracker.
