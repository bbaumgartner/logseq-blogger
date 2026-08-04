# logseq-blogger

Use [Logseq](https://logseq.com) as your blog editor. logseq-blogger reads the
**logseq-blog** interface (`type:: blog` markdown in your graph), uploads
immutable originals over SFTP, and fires the [syndicate](https://github.com/bbaumgartner/syndicator)
webhooks (Blog Post Publish + Reel Publish). Media adaptation, translation,
Hugo render, captions, and social drafts run in n8n on the syndicator side.

```
Logseq  →  logseq-blogger  →  SFTP source/ + webhooks  →  syndicator (n8n)  →  Postiz
                                         ↓ hugo-site/
                                 fetch + git push  →  Hugo site deploy
```

The **logseq-blog** input contract is documented below. The outbound syndicate
contract (SFTP layout, webhook JSON) is in the
[syndicator README](https://github.com/bbaumgartner/syndicator).

## Setup

One-time, per machine:

```bash
git clone git@github.com:bbaumgartner/logseq-blogger.git ~/git/logseq-blogger
cd ~/git/logseq-blogger
curl -LsSf https://astral.sh/uv/install.sh | sh     # if uv is missing
uv sync
cp logseq-blogger.yaml.example logseq-blogger.yaml  # SFTP host + webhook URLs
cp config.local.yaml.example config.local.yaml      # adjust paths + sftp_key!
```

Requirements:

- The Syncthing-synced Logseq graph (`saillog_dir` in `config.local.yaml`).
- Shared client config in `logseq-blogger.yaml`: `site.base_url` (for `post_url`
  in payloads), SFTP host/`base_dir`, and the two production webhook URLs.
- An SSH key for the chrooted SFTP staging user (`sftp_key`).
- A local clone of your Hugo site with push access — the site commit is a
  manual step after n8n finishes (fetch `/syndicator/hugo-site/` into that checkout).

No `OPENAI_API_KEY` is needed locally; crop-focus, translation and captions use
credentials stored in n8n.

The homepage journey-map video is **not** produced here — use the separate
[journeymap](https://github.com/bbaumgartner/journeymap) tool if you need it.

## Commands

```bash
uv run logseq-blogger syndicate                 # all new online posts (no marker yet)
uv run logseq-blogger syndicate --post <slug>   # only that post
uv run logseq-blogger redeploy --post <slug>    # site-only rebuild, no drafts
uv run logseq-blogger list                      # online slugs + syndicated-at
uv run logseq-blogger version
```

`syndicate` per post: upload originals under `source/` → one `/reel` per video →
`/publish` → write `syndicated-at::` once every webhook returned HTTP 2xx.
Already-marked posts are skipped. A `status:: online` post **without a
`header::` image is refused** before any work (reported and skipped in batch).

`redeploy` ignores the marker and re-runs the site only (`flags.redeploy: true`
→ n8n overwrites Hugo files in the staging tree, but creates no social drafts).

## Daily workflow

### New blog post

1. Write in Logseq, add a `header::` image, set `status:: online`
   (see [logseq-blog interface](#logseq-blog-interface)).
2. Run `uv run logseq-blogger syndicate` (optionally `--post <slug>`).
3. Once n8n has finished (a few minutes), fetch `/syndicator/hugo-site/` into
   your local Hugo checkout (e.g. `sailingnomads`), review, commit and push.
   Intro + reel **drafts** appear in the **Postiz** calendar.
4. In Postiz: edit captions if needed, tag locations, and **schedule** each
   draft. Nothing reaches a platform until you schedule it there.

### Cutover (once)

Before the first batch `syndicate`, **hand-seed** `syndicated-at::` on every
already-published `status:: online` post so they are not re-drafted.

## Failure & recovery

The `syndicated-at::` marker means **handed off**, not **published** (n8n
acknowledges immediately and runs async).

- **Handoff failure** (a webhook never returns HTTP 2xx after 3 local retries):
  no marker is written, so the next `syndicate` re-runs the post. Duplicates are
  harmless (site staging is idempotent; delete duplicate drafts by hand).
- **Site async failure** (render / translate in n8n): run
  `redeploy --post <slug>` — ignores the marker and overwrites that post in the
  mirrored Hugo staging tree.
- **Social async failure** (Postiz drafts): no CLI path by design. Re-run the
  failed execution in n8n or fix the draft in Postiz.

## Troubleshooting

- **Webhook HTTP errors / timeouts:** check the n8n workflow is active and the
  URL in `logseq-blogger.yaml` matches the production webhook. Webhooks respond
  immediately (`onReceived`); a long wait usually means n8n is unreachable.
- **SFTP upload fails:** verify `sftp_key`, `sftp.host`/`sftp.user`, and that
  the `/syndicator/` base dir exists in the chroot.
- **Post refused for missing header:** add a `header::` image and re-run.
- **Caption/model or translation quality:** prompts and models live on the n8n
  OpenAI nodes.
- **Staging area fills up:** purge the SFTP `/syndicator/` tree periodically by
  hand (e.g. a cron `find -mtime` job).

---

## logseq-blog interface

The **logseq-blog** interface is the contract between a Logseq graph and
logseq-blogger. A blog post is a Logseq markdown fragment marked with
`type:: blog`. logseq-blogger discovers those posts, validates them, and hands
them off through the [syndicate](https://github.com/bbaumgartner/syndicator)
interface (SFTP originals + Blog Post Publish / Reel Publish webhooks).

This section describes the **input** contract only. Webhook payloads and SFTP
layout are defined in the syndicator README.

### Discovery

Under the Logseq graph root (`saillog_dir` in `config.local.yaml`):

| Path | Role |
|------|------|
| `journals/` | Journal files (nested-list format) |
| `pages/` | Page files (top-level metadata format) |

Every markdown file in those trees is scanned. A post is found wherever a
property block contains the substring `type:: blog`.

### Selection and gates

| Rule | Behaviour |
|------|-----------|
| `type:: blog` | Required discovery marker |
| `status:: online` | Only online posts are considered for hand-off |
| `syndicated-at::` present | Skipped by `syndicate` (already handed off) |
| `redeploy --post <slug>` | Ignores the marker; site rebuild only |
| Missing or unreadable `header::` image | Refused before any SFTP/webhook work |

`syndicated-at::` means **handed off** to syndicate (webhooks accepted), not that
the site is live or that social drafts were scheduled.

### Properties

Logseq properties use `key:: value` syntax.

| Property | Required | Notes |
|----------|----------|-------|
| `type` | yes | Must be `blog` |
| `status` | yes (for publish) | Only `online` is selected |
| `date` | yes | `YYYY-MM-DD`; used in the slug |
| `title` | yes | Used in the slug (spaces → `_`) |
| `author` | yes | Passed through to syndicate `meta.author` |
| `header` | yes for hand-off | Markdown image, e.g. `![alt](../assets/foo.jpg)`. File must exist on disk |
| `summary` | no | Explicit summary; otherwise first body text block |
| `language` | no | Word form: `german`, `english`, `spanish`, `french`, `italian` → `de`/`en`/`es`/`fr`/`it` (default `de`) |
| `position` | no | Free text or coordinates; passed through to syndicate `meta.position` |
| `syndicated-at` | written by tool | ISO-8601 timestamp set after successful `syndicate` |

#### Slug

```text
{date}_{title with spaces replaced by underscores}
```

Example: `date:: 2026-01-17` + `title:: Frühlingspläne 2026` →
`2026-01-17_Frühlingspläne_2026`.

### Supported formats

#### Format 1 — Nested list (journals)

Metadata lives in the first list item under a parent (commonly `[[Blog]]`).
Sibling list items are the body.

```markdown
- [[Blog]]
  - type:: blog
    status:: online
    language:: german
    date:: 2026-01-17
    position:: Oberägeri
    title:: Frühlingspläne 2026
    author:: Benno
    header:: ![alt](../assets/2026_01_17/featured.jpeg)
  - First paragraph of content
  - ### Section Heading
  - More content here
  - ![photo.jpg](../assets/2026_01_17/photo.jpg)
```

**Characteristics:**

- Properties are indented under the `type:: blog` bullet
- Each subsequent list item becomes a content block
- Multiple blog posts may appear in one journal file
- Non-blog journal content (e.g. `current-position::` under other headings) is ignored

Fixture example: [`tests/fixtures/journals/2026_01_17.md`](tests/fixtures/journals/2026_01_17.md).

#### Format 2 — Top-level metadata (pages)

Properties at the top of the file; body as list items below.

```markdown
type:: blog
status:: online
language:: english
date:: 2024-06-14
position:: 38.98000,1.430000
title:: Renan
author:: Benno
header:: ![image](../assets/Renan/renand.jpg)

- First paragraph of content
- ### Section Heading
- ![photo.jpg](../assets/Renan/photo.jpg)
- More content
```

**Characteristics:**

- Clean separation between metadata and content
- One primary blog post per page file

Fixture example: [`tests/fixtures/pages/Renan.md`](tests/fixtures/pages/Renan.md).

### Content blocks

Body list items are classified as:

| Kind | How it is recognised |
|------|----------------------|
| `title` | Line starts with `#` … `######` |
| `text` | Ordinary prose (nested bullets become `* …` lines) |
| `media` | First line is `![alt](path)` — image or local video by extension |
| `youtube` | First line is `{{video https://…}}` with a YouTube URL |

**Media paths** are resolved relative to the source markdown file (typically
`../assets/…`). Local video extensions include `.mp4`, `.mov`, `.webm`, `.mkv`,
and similar.

**Videos** each produce one Reel Publish call (after originals are uploaded).
Section title/text around the video are derived for the reel caption context;
a `[VIDEO]` marker is placed at the video’s position in that context string.

When calling syndicate, Logseq-specific embeds are rewritten into the payload
shape expected by Blog Post Publish (asset basenames, YouTube ids, Hugo-oriented
shortcodes in text/title `raw` fields). See the syndicator README for the
exact JSON contract.

### Runtime behaviour

1. Scan `journals/` + `pages/` for `type:: blog` + `status:: online`.
2. Skip posts that already have `syndicated-at::` (unless `redeploy`).
3. Refuse posts without a resolvable `header::` image file.
4. Stage header + body assets; upload to SFTP `<base>/<slug>/source/`.
5. For each local video: `POST` Reel Publish.
6. `POST` Blog Post Publish (`flags.redeploy: false` for `syndicate`, `true` for `redeploy`).
7. On all HTTP 2xx for `syndicate`: write `syndicated-at::` into the property block.

Outbound transport details: [syndicator README](https://github.com/bbaumgartner/syndicator).
