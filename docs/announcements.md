# Announcing changes

How we tell people about Frame Control features and fixes as they merge. The
same few sentences feed the X post, the release notes and the website, so they
are written once, in the pull request, while the change is fresh.

## What gets announced

| Kind | Announce? | Example |
|---|---|---|
| **New** — something you can now do | Yes, its own post | Stream Mac windows into the Frame as panels |
| **Better** — something existing got noticeably easier, faster or wider | Yes, its own post or a roundup | APKs install without the Android SDK |
| **Fixed** — something broken that users hit | Yes if people reported it or it blocked a flow; otherwise the next roundup | Mac mirror showed a zoomed-in corner |
| **Release** — a tagged build | Always, one post linking the release | Frame Control 0.3.1 |
| Tests, refactors, CI, docs-only, website polish | No | Fake Frame tests, screenshot crop |

If a change isn't worth a sentence to someone who owns a Frame, it isn't
announced.

## The voice

Write it the way the README and release notes already read.

- **Lead with what the person can now do**, in their words: "Install older
  versions of an app when the newest won't run on the Frame", not "Add APK
  version fallback resolver".
- **Plain and specific.** Name the thing, give the number: "about 30 fps",
  "4,500 apps", "up to 8 older versions". No "blazing", "game-changing",
  "excited to announce", "huge", or exclamation marks.
- **Say where it works.** Platforms and what it was tested on, briefly:
  "Tested on a real Frame from macOS 27." Don't claim what wasn't tested.
- **Say the catch.** If it needs a setup step, an unsigned build, or only works
  on one OS, say so in the same post.
- **Sentence case**, full sentences, British spelling to match the docs.
  Contractions are fine.
- **No emoji in the text.** One image, GIF or short clip carries the tone
  instead. The only symbol is the kind label below.
- **Unofficial, always.** Never imply Valve made or endorses it. Say "Steam
  Frame" for the headset and "Frame Control" for the app.
- **Credit people.** If a user reported the bug or suggested the feature and is
  happy to be named, thank them by handle.

## The formats

Every announceable PR ends with an `## Announcement` section holding these.
The reviewer checks it like code.

### 1. The post (X, and any other social account)

```
<Kind>: <what you can do now, one sentence>

<one or two sentences: how it works, the catch, or what it was tested on>

<link>
```

- `<Kind>` is `New`, `Better` or `Fixed`.
- 280 characters maximum including the link (X counts any link as 23).
- One link: the release if it has shipped, otherwise the PR.
- One visual when the change is visible: a screenshot from the app, a GIF, or a
  short clip from the headset. Alt text describes what it shows.
- No hashtags, except `#SteamFrame` on releases and on posts about something
  new, because people search for it.

### 2. The release-note line

One bullet under **New in x.y.z**, same as the current release notes: the
first half of the post's first sentence, no kind label, no link.

### 3. Release post

```
Frame Control <version>: <the headline change>

<one sentence on the headline change>. Also: <two or three short items>.

Windows, macOS and Linux: <release link>
#SteamFrame
```

The release title on GitHub uses the same `Frame Control <version>: <headline>`
line, as 0.3.0 and 0.3.1 already do.

### Roundups

Small fixes that don't earn their own post wait for a roundup, posted with the
next release or when three or more have piled up:

```
Fixed in Frame Control this week:
- <fix>
- <fix>
- <fix>

<link>
```

## Examples from what has already merged

**#11, older APK versions**

```
New: when an Android app is too new for the Frame, Frame Control now offers
older versions that will install.

It checks F-Droid, its archive and IzzyOnDroid, and verifies each download
before it goes on the headset.

https://github.com/saphid/steam-frame/pull/11
```

**#8, Mac mirror fixes**

```
Fixed: mirroring your Mac into the Steam Frame now fits the whole desktop in
the panel, asks for the right password, and shows the cursor.

Tested end to end on a real Frame from macOS 27.

https://github.com/saphid/steam-frame/pull/8
```

**v0.3.1**

```
Frame Control 0.3.1: install APKs without the Android SDK

Frame Control now reads APK files itself, so there's nothing extra to install.
Also: Linux and Windows game sideloading, and one-click install links.

Windows, macOS and Linux: https://github.com/saphid/steam-frame/releases/tag/v0.3.1
#SteamFrame
```

## Posting

Nothing is posted without a person approving it. The flow is:

1. The PR carries its `## Announcement` section.
2. On merge, the post is drafted from that section (manually for now).
3. Alex approves or edits it, then it's posted from the project account.
4. Replies and questions that turn out to be bugs become GitHub issues labelled
   `feedback`, same as the website form.
