"""Orchestration: stage originals, upload over SFTP, fire /publish + /reel."""

from __future__ import annotations

import logging
import re
import shutil
import stat
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .config import Config
from .extract import VIDEO_EXTENSIONS, BlogPost
from .marker import is_syndicated, read_syndicated_at, set_syndicated_at
from .sftp import SftpUploader, sftp_session
from .webhook import WebhookError, post_webhook

log = logging.getLogger(__name__)

# Filename may contain one level of nested (...), e.g. foo_(bar)_123.png
ASSET_RE = re.compile(
    r"!\[([^\]]*)\]\((.*?assets/)((?:[^()]|\([^()]*\))*)\)(?:\{[^}]*\})?"
)
LOGSEQ_VIDEO_RE = re.compile(r"\{\{video\s+(https?://[^\s}]+)\s*\}\}")
YOUTUBE_ID_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]+)")

_KEEP_CATEGORIES = ("L", "N", "M")
_KEEP_CHARS = set("-._")

# Languages n8n writes under hugo-site/content/posts/<slug>/index.<lang>.md
HUGO_INDEX_LANGS = ("en", "de", "es", "fr", "it", "arrr")

# Adapt Hugo Media encodes to ``<name>.mp4.encoding`` then renames into place.
HUGO_ENCODING_SUFFIX = ".encoding"


# --- URL helpers -------------------------------------------------------------

def hugo_path_segment(name: str) -> str:
    out = []
    for ch in name:
        if ch in _KEEP_CHARS or unicodedata.category(ch)[0] in _KEEP_CATEGORIES:
            out.append(ch.lower())
    return "".join(out)


def lang_prefix(cfg: Config, lang: str) -> str:
    return "" if lang == cfg.shared.site.default_language else f"/{lang}"


def post_url(cfg: Config, slug: str, lang: str) -> str:
    segment = quote(hugo_path_segment(slug))
    return f"{cfg.shared.site.base_url}{lang_prefix(cfg, lang)}/posts/{segment}/"


# --- Content helpers ---------------------------------------------------------

def hugo_basename(original: str) -> str:
    """Mirror n8n Hugo naming (images keep name; videos → ``.mp4``)."""
    path = Path(original)
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return f"{path.stem}.mp4"
    return path.name


def build_content(post: BlogPost) -> str:
    parts = [b.raw.strip() for b in post.blocks if b.raw.strip()]
    return "\n\n".join(parts)


def summary_for(post: BlogPost) -> str:
    if post.meta.summary:
        return post.meta.summary
    if post.blocks:
        return post.blocks[0].raw.replace("\n", " ")
    return ""


def collect_asset_copies(content: str, source_dir: Path) -> list[tuple[Path, str]]:
    copies: list[tuple[Path, str]] = []
    for m in ASSET_RE.finditer(content):
        src = (source_dir / (m.group(2) + m.group(3))).resolve()
        copies.append((src, Path(m.group(3)).name))
    return copies


def transform_content(content: str) -> str:
    def replace_video_embed(m: re.Match[str]) -> str:
        url = m.group(1)
        yt = YOUTUBE_ID_RE.search(url)
        if yt:
            return f"{{{{< youtube {yt.group(1)} >}}}}"
        return m.group(0)

    content = LOGSEQ_VIDEO_RE.sub(replace_video_embed, content)

    def replace_asset(m: re.Match[str]) -> str:
        alt = m.group(1)
        filename = Path(m.group(3)).name
        if Path(filename).suffix.lower() in VIDEO_EXTENSIONS:
            return f'{{{{< video src="{filename}" >}}}}'
        return f"![{alt}]({filename})"

    return ASSET_RE.sub(replace_asset, content)


# --- SFTP path helpers -------------------------------------------------------

def _base(cfg: Config) -> str:
    return cfg.shared.sftp.base_dir.rstrip("/")


def source_remote(cfg: Config, slug: str, name: str) -> str:
    return f"{_base(cfg)}/{slug}/source/{name}"


def hugo_content_remote(cfg: Config) -> str:
    return f"{_base(cfg)}/hugo-site/content"


def hugo_post_remote(cfg: Config, slug: str) -> str:
    return f"{hugo_content_remote(cfg)}/posts/{slug}"


def hugo_index_paths(cfg: Config, slug: str) -> list[str]:
    root = hugo_post_remote(cfg, slug)
    return [f"{root}/index.{lang}.md" for lang in HUGO_INDEX_LANGS]


def hugo_indexes_present(sftp: SftpUploader, cfg: Config, slug: str) -> bool:
    return all(sftp.exists(path) for path in hugo_index_paths(cfg, slug))


def hugo_post_bundle_sizes(sftp: SftpUploader, cfg: Config, slug: str) -> dict[str, int] | None:
    """Return ``filename -> size`` for the post bundle, or ``None`` if not pullable yet.

    Not pullable while any index is missing or an ``*.encoding`` sidecar exists
    (n8n still writing a video).
    """
    if not hugo_indexes_present(sftp, cfg, slug):
        return None
    root = hugo_post_remote(cfg, slug)
    try:
        entries = sftp.listdir_attr(root)
    except FileNotFoundError:
        return None
    sizes: dict[str, int] = {}
    for entry in entries:
        name = entry.filename
        if name in (".", ".."):
            continue
        if name.endswith(HUGO_ENCODING_SUFFIX):
            return None
        if not stat.S_ISREG(entry.st_mode):
            continue
        sizes[name] = int(entry.st_size)
    return sizes


def hugo_index_mtimes(sftp: SftpUploader, cfg: Config, slug: str) -> dict[str, int]:
    """Return ``index.*.md -> mtime`` for the post bundle (empty if missing).

    Used by redeploy as a baseline so the wait does not accept the stale
    bundle that is still on SFTP from the previous build.
    """
    try:
        entries = sftp.listdir_attr(hugo_post_remote(cfg, slug))
    except FileNotFoundError:
        return {}
    wanted = {f"index.{lang}.md" for lang in HUGO_INDEX_LANGS}
    return {
        e.filename: int(getattr(e, "st_mtime", 0) or 0)
        for e in entries
        if e.filename in wanted
    }


def hugo_post_ready(sftp: SftpUploader, cfg: Config, slug: str) -> bool:
    """True when indexes exist and no in-progress encode sidecars are present."""
    return hugo_post_bundle_sizes(sftp, cfg, slug) is not None


# --- Payload builders --------------------------------------------------------

def build_blocks(post: BlogPost) -> list[dict]:
    blocks: list[dict] = []
    for b in post.blocks:
        if b.kind == "title":
            blocks.append(
                {"kind": "title", "raw": transform_content(b.raw), "heading_level": b.heading_level}
            )
        elif b.kind == "text":
            blocks.append({"kind": "text", "raw": transform_content(b.raw)})
        elif b.kind == "youtube" or (b.media is not None and b.media.kind == "youtube"):
            yt = b.media.youtube_id if b.media else ""
            blocks.append({"kind": "youtube", "media": {"kind": "youtube", "youtube_id": yt}})
        elif b.kind == "media" and b.media is not None:
            m = b.media
            blocks.append(
                {
                    "kind": "media",
                    "media": {
                        "kind": m.kind,
                        "source_filename": Path(m.filename).name,
                        "alt": m.alt,
                    },
                }
            )
    return blocks


# --- Staging -----------------------------------------------------------------

@dataclass
class Upload:
    local: Path
    remote: str


@dataclass
class StagedVideo:
    index: int
    alt: str
    section_title: str
    section_text: str
    source_filename: str = ""


@dataclass
class StagedPost:
    slug: str
    uploads: list[Upload] = field(default_factory=list)
    header_source: str | None = None
    videos: list[StagedVideo] = field(default_factory=list)


def _copy_into(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def stage_post(
    post: BlogPost,
    cfg: Config,
    workdir: Path,
    *,
    include_social: bool,
) -> StagedPost:
    workdir.mkdir(parents=True, exist_ok=True)
    source_dir = workdir / "source"
    result = StagedPost(slug=post.slug)
    uploaded_names: set[str] = set()

    header_media = post.header_media
    if header_media is not None and header_media.exists and header_media.source_path is not None:
        ext = header_media.source_path.suffix
        name = f"header{ext}"
        local = _copy_into(header_media.source_path, source_dir / name)
        result.uploads.append(Upload(local, source_remote(cfg, post.slug, name)))
        result.header_source = name
        uploaded_names.add(name)

    for src, basename in collect_asset_copies(build_content(post), post.source_path.parent):
        if not src.exists():
            log.warning("missing asset %s", src)
            continue
        if basename in uploaded_names:
            continue
        local = _copy_into(src, source_dir / basename)
        result.uploads.append(Upload(local, source_remote(cfg, post.slug, basename)))
        uploaded_names.add(basename)

    if include_social:
        for index, video in enumerate(post.videos(), start=1):
            if video.source_path is None or not video.source_path.exists():
                log.warning("%s: video %d missing on disk — skipping reel", post.slug, index)
                continue
            basename = video.source_path.name
            section = post.section_for_block(video)
            result.videos.append(
                StagedVideo(
                    index=index,
                    alt=video.alt or basename,
                    section_title=(section.title or "") if section else "",
                    section_text=post.section_text_for_video(video),
                    source_filename=basename,
                )
            )
    return result


# --- Pipeline ----------------------------------------------------------------

@dataclass
class SyndicateReport:
    done: list[str] = field(default_factory=list)
    skipped_marked: list[str] = field(default_factory=list)
    skipped_no_header: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    hugo_pulled: bool = False


def _scan_online(cfg: Config) -> list[BlogPost]:
    from .extract import scan_blog_posts

    return scan_blog_posts(cfg.journals_dir, cfg.pages_dir, online_only=True)


def list_syndication(cfg: Config) -> list[tuple[str, str]]:
    """Return (slug, syndicated-at) for every online post; date empty if not marked."""
    return [(p.slug, read_syndicated_at(p)) for p in _scan_online(cfg)]


def _has_header(post: BlogPost) -> bool:
    hm = post.header_media
    return hm is not None and hm.exists


def _upload_all(sftp: SftpUploader, staged: StagedPost) -> None:
    for up in staged.uploads:
        sftp.upload(up.local, up.remote)


def _check_webhooks(cfg: Config, *, need_reel: bool) -> None:
    if not cfg.shared.webhooks.publish_url:
        raise SystemExit("webhooks.publish_url is not configured in logseq_blogger.yaml")
    if need_reel and not cfg.shared.webhooks.reel_url:
        raise SystemExit("webhooks.reel_url is not configured in logseq_blogger.yaml")


def wait_for_hugo_posts(
    sftp: SftpUploader,
    cfg: Config,
    slugs: list[str],
    baselines: dict[str, dict[str, int]] | None = None,
) -> None:
    """Poll SFTP until every slug's Hugo bundle is complete and size-stable.

    Ready means: all ``index.*.md`` files exist, no ``*.encoding`` sidecars, and
    the post directory's file sizes match the previous poll. That avoids pulling
    while ffmpeg is still rewriting a video into an already-indexed post folder
    (redeploys keep old indexes around).

    ``baselines`` maps slug -> :func:`hugo_index_mtimes` taken before the
    webhook fired; such a slug is only accepted once every index mtime differs
    from its baseline (i.e. n8n has rewritten the bundle).
    """
    baselines = {k: v for k, v in (baselines or {}).items() if v}
    if not slugs:
        return
    timeout = cfg.local.hugo_pull_timeout_sec
    poll = cfg.local.hugo_pull_poll_sec
    deadline = time.monotonic() + timeout
    pending = set(slugs)
    prev_sizes: dict[str, dict[str, int]] = {}
    log.info(
        "waiting for Hugo site on SFTP for %d post(s) (timeout %ds, poll %ds)",
        len(pending),
        timeout,
        poll,
    )
    while pending:
        ready: set[str] = set()
        for slug in pending:
            if slug in baselines:
                current = hugo_index_mtimes(sftp, cfg, slug)
                old = baselines[slug]
                if len(current) < len(HUGO_INDEX_LANGS) or any(
                    current.get(name) == mtime for name, mtime in old.items()
                ):
                    continue
                log.info("Hugo post %s rewritten — waiting for stable bundle", slug)
                del baselines[slug]
            sizes = hugo_post_bundle_sizes(sftp, cfg, slug)
            if sizes is None:
                prev_sizes.pop(slug, None)
                continue
            if prev_sizes.get(slug) == sizes:
                ready.add(slug)
            else:
                if slug in prev_sizes:
                    log.info("Hugo post %s still changing — waiting for stable bundle", slug)
                prev_sizes[slug] = sizes
        if ready:
            for slug in sorted(ready):
                log.info("Hugo post ready: %s", slug)
            pending -= ready
        if not pending:
            return
        if time.monotonic() >= deadline:
            missing = ", ".join(sorted(pending))
            raise TimeoutError(
                f"timed out after {timeout}s waiting for Hugo indexes for: {missing}"
            )
        time.sleep(max(poll, 0))


def pull_hugo_content(
    cfg: Config,
    slugs: list[str],
    baselines: dict[str, dict[str, int]] | None = None,
) -> int:
    """Wait for ``slugs`` on SFTP, then overlay hugo-site/content/ into hugo_dir/content/."""
    if not cfg.local.hugo_dir:
        raise SystemExit("hugo_dir is not configured in config.local.yaml")
    remote = hugo_content_remote(cfg)
    local = cfg.hugo_content_dir
    with sftp_session(cfg) as sftp:
        wait_for_hugo_posts(sftp, cfg, slugs, baselines)
        if not sftp.exists(remote):
            raise FileNotFoundError(f"remote Hugo content missing: {remote}")
        log.info("pulling %s -> %s", remote, local)
        count = sftp.download_tree(remote, local)
    log.info("pulled %d Hugo file(s) into %s", count, local)
    return count


def syndicate(cfg: Config, slug: str | None = None) -> SyndicateReport:
    _check_webhooks(cfg, need_reel=True)
    posts = _scan_online(cfg)
    if slug is not None:
        posts = [p for p in posts if p.slug == slug]
        if not posts:
            raise SystemExit(f"unknown online post slug: {slug}")

    report = SyndicateReport()
    with tempfile.TemporaryDirectory(prefix="logseq-blogger-") as tmp_root:
        tmp = Path(tmp_root)
        with sftp_session(cfg) as sftp:
            for post in posts:
                _syndicate_one(cfg, sftp, post, tmp, report)

    if report.done:
        try:
            pull_hugo_content(cfg, report.done)
            report.hugo_pulled = True
        except (TimeoutError, OSError, FileNotFoundError) as err:
            log.error(
                "Hugo pull failed after syndication — %s "
                "(markers kept; pull hugo-site/content/ by hand if needed)",
                err,
            )
            report.failed.append(("hugo-pull", str(err)))

    log.info(
        "syndicate: %d done, %d already-marked, %d no-header, %d failed%s",
        len(report.done),
        len(report.skipped_marked),
        len(report.skipped_no_header),
        len(report.failed),
        ", hugo pulled" if report.hugo_pulled else "",
    )
    for s, reason in report.failed:
        log.error("  failed: %s (%s)", s, reason)
    for s in report.skipped_no_header:
        log.warning("  skipped (no header): %s", s)
    return report


def _syndicate_one(
    cfg: Config,
    sftp: SftpUploader,
    post: BlogPost,
    tmp: Path,
    report: SyndicateReport,
) -> None:
    slug = post.slug
    if is_syndicated(post):
        log.info("skip %s: already syndicated (delete the marker to re-run)", slug)
        report.skipped_marked.append(slug)
        return
    if not _has_header(post):
        log.warning("skip %s: no header:: image (required — add one and re-run)", slug)
        report.skipped_no_header.append(slug)
        return

    try:
        staged = stage_post(post, cfg, tmp / slug, include_social=True)
        _upload_all(sftp, staged)

        # /publish first so n8n runs the Hugo site build immediately; /reel
        # is queued behind it (calling /reel first would delay the site).
        post_webhook(
            cfg.shared.webhooks.publish_url,
            {
                "slug": post.slug,
                "meta": {
                    "title": post.meta.title,
                    "date": post.meta.date,
                    "language": post.meta.language,
                    "lang_code": post.lang_code,
                    "author": post.meta.author,
                    "summary": summary_for(post),
                    "position": post.meta.position,
                },
                "post_url": post_url(cfg, post.slug, post.lang_code),
                "blocks": build_blocks(post),
                "header_source": staged.header_source or "",
                "flags": {"redeploy": False},
            },
            label=f"/publish {slug}",
        )

        for sv in staged.videos:
            if not sv.source_filename:
                log.warning("%s: video %d has no source filename — skipping /reel", slug, sv.index)
                continue
            post_webhook(
                cfg.shared.webhooks.reel_url,
                {
                    "slug": post.slug,
                    "post": {
                        "title": post.meta.title,
                        "url": post_url(cfg, post.slug, post.lang_code),
                        "summary": summary_for(post),
                        "lang_code": post.lang_code,
                    },
                    "video": {
                        "index": sv.index,
                        "section_title": sv.section_title,
                        "section_text": sv.section_text,
                        "alt": sv.alt,
                    },
                    "source": {"filename": sv.source_filename},
                },
                label=f"/reel {slug} #{sv.index}",
            )

        set_syndicated_at(post)
        report.done.append(slug)
    except (WebhookError, OSError) as err:
        log.error("%s: syndication failed — %s (marker not set; will retry next run)", slug, err)
        report.failed.append((slug, str(err)))


@dataclass
class RedeployReport:
    hugo_pulled: bool = False
    error: str = ""


def redeploy(cfg: Config, slug: str) -> RedeployReport:
    _check_webhooks(cfg, need_reel=False)
    posts = {p.slug: p for p in _scan_online(cfg)}
    if slug not in posts:
        known = "\n  ".join(sorted(posts))
        raise SystemExit(f"unknown online post slug: {slug}\nknown posts:\n  {known}")
    post = posts[slug]
    if not _has_header(post):
        raise SystemExit(f"{slug}: no header:: image — the site build requires a featured image")

    with tempfile.TemporaryDirectory(prefix="logseq-blogger-") as tmp_root:
        tmp = Path(tmp_root)
        staged = stage_post(post, cfg, tmp / slug, include_social=False)

        with sftp_session(cfg) as sftp:
            _upload_all(sftp, staged)
            baseline = hugo_index_mtimes(sftp, cfg, slug)
            post_webhook(
                cfg.shared.webhooks.publish_url,
                {
                    "slug": post.slug,
                    "meta": {
                        "title": post.meta.title,
                        "date": post.meta.date,
                        "language": post.meta.language,
                        "lang_code": post.lang_code,
                        "author": post.meta.author,
                        "summary": summary_for(post),
                        "position": post.meta.position,
                    },
                    "post_url": post_url(cfg, post.slug, post.lang_code),
                    "blocks": build_blocks(post),
                    "header_source": staged.header_source or "",
                    "flags": {"redeploy": True},
                },
                label=f"/publish redeploy {slug}",
            )

    log.info("redeploy %s: site rebuild handed off (no social, no marker)", slug)

    report = RedeployReport()
    try:
        pull_hugo_content(cfg, [slug], baselines={slug: baseline})
        report.hugo_pulled = True
    except (TimeoutError, OSError, FileNotFoundError) as err:
        log.error(
            "Hugo pull failed after redeploy — %s (pull hugo-site/content/ by hand if needed)",
            err,
        )
        report.error = str(err)
    return report
