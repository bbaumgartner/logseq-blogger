"""Tests for waiting on Hugo indexes and overlaying SFTP content locally."""

from __future__ import annotations

import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from logseq_blogger.extract import scan_blog_posts
from logseq_blogger.marker import is_syndicated
from logseq_blogger.trigger import (
    HUGO_INDEX_LANGS,
    hugo_index_paths,
    hugo_post_ready,
    pull_hugo_content,
    syndicate,
    wait_for_hugo_posts,
)
from conftest import create_dummy_assets, make_cfg


@dataclass
class _Attr:
    filename: str
    st_mode: int


class FakeSftp:
    """Local-directory stand-in for SftpUploader download/exists APIs."""

    def __init__(self, remote_root: Path):
        self.remote_root = remote_root
        self.exists_calls = 0

    def _local(self, remote: str) -> Path:
        return self.remote_root / remote.lstrip("/")

    def exists(self, remote: str) -> bool:
        self.exists_calls += 1
        return self._local(remote).exists()

    def listdir(self, remote: str) -> list[str]:
        return [p.name for p in self._local(remote).iterdir()]

    def listdir_attr(self, remote: str) -> list[_Attr]:
        out: list[_Attr] = []
        for path in self._local(remote).iterdir():
            mode = stat.S_IFDIR | 0o755 if path.is_dir() else stat.S_IFREG | 0o644
            out.append(_Attr(filename=path.name, st_mode=mode))
        return out

    def download(self, remote_path: str, local_path: Path) -> None:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self._local(remote_path).read_bytes())

    def download_tree(self, remote_dir: str, local_dir: Path) -> int:
        from logseq_blogger.sftp import SftpUploader

        return SftpUploader.download_tree(self, remote_dir, local_dir)  # type: ignore[arg-type]


def _seed_indexes(remote_root: Path, base: str, slug: str, langs=HUGO_INDEX_LANGS) -> None:
    post = remote_root / base.strip("/") / "hugo-site" / "content" / "posts" / slug
    post.mkdir(parents=True, exist_ok=True)
    for lang in langs:
        (post / f"index.{lang}.md").write_text(f"title {lang}\n", encoding="utf-8")
    (post / "featured.jpg").write_bytes(b"img")


def test_hugo_index_paths(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    paths = hugo_index_paths(cfg, "2026-06-03_Athen")
    assert paths == [
        f"/syndicator/hugo-site/content/posts/2026-06-03_Athen/index.{lang}.md"
        for lang in HUGO_INDEX_LANGS
    ]


def test_hugo_post_ready_requires_all_langs(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    remote = tmp_path / "remote"
    sftp = FakeSftp(remote)
    slug = "2026-06-03_Athen"
    assert not hugo_post_ready(sftp, cfg, slug)

    _seed_indexes(remote, "/syndicator", slug, langs=("en", "de"))
    assert not hugo_post_ready(sftp, cfg, slug)

    _seed_indexes(remote, "/syndicator", slug)
    assert hugo_post_ready(sftp, cfg, slug)


def test_wait_for_hugo_posts_succeeds(tmp_path: Path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.local.hugo_pull_timeout_sec = 10
    cfg.local.hugo_pull_poll_sec = 0
    remote = tmp_path / "remote"
    sftp = FakeSftp(remote)
    slug = "2026-06-03_Athen"
    sleeps: list[float] = []

    def fake_sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) == 1:
            _seed_indexes(remote, "/syndicator", slug)

    monkeypatch.setattr("logseq_blogger.trigger.time.sleep", fake_sleep)
    wait_for_hugo_posts(sftp, cfg, [slug])
    assert hugo_post_ready(sftp, cfg, slug)
    assert sleeps


def test_wait_for_hugo_posts_timeout(tmp_path: Path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.local.hugo_pull_timeout_sec = 0
    cfg.local.hugo_pull_poll_sec = 0
    sftp = FakeSftp(tmp_path / "remote")
    monkeypatch.setattr("logseq_blogger.trigger.time.sleep", lambda _s: None)
    with pytest.raises(TimeoutError, match="2026-06-03_Athen"):
        wait_for_hugo_posts(sftp, cfg, ["2026-06-03_Athen"])


def test_download_tree_overlays_without_deleting_local(tmp_path: Path):
    remote = tmp_path / "remote"
    content = remote / "syndicator" / "hugo-site" / "content"
    (content / "posts" / "a").mkdir(parents=True)
    (content / "posts" / "a" / "index.en.md").write_text("new\n", encoding="utf-8")
    (content / "posts" / "_index.en.md").write_text("posts index\n", encoding="utf-8")

    local = tmp_path / "hugo" / "content"
    (local / "impressum").mkdir(parents=True)
    (local / "impressum" / "index.md").write_text("keep\n", encoding="utf-8")
    (local / "posts").mkdir(parents=True)
    (local / "posts" / "old.md").write_text("local-only\n", encoding="utf-8")

    sftp = FakeSftp(remote)
    count = sftp.download_tree("/syndicator/hugo-site/content", local)
    assert count == 2
    assert (local / "impressum" / "index.md").read_text(encoding="utf-8") == "keep\n"
    assert (local / "posts" / "old.md").read_text(encoding="utf-8") == "local-only\n"
    assert (local / "posts" / "a" / "index.en.md").read_text(encoding="utf-8") == "new\n"


def test_pull_hugo_content(tmp_path: Path, monkeypatch):
    cfg = make_cfg(tmp_path)
    remote = tmp_path / "remote"
    _seed_indexes(remote, "/syndicator", "2026-06-03_Athen")
    (remote / "syndicator" / "hugo-site" / "content" / "posts" / "_index.de.md").write_text(
        "hi\n", encoding="utf-8"
    )

    fake = FakeSftp(remote)

    class _Session:
        def __enter__(self):
            return fake

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("logseq_blogger.trigger.sftp_session", lambda _cfg: _Session())
    count = pull_hugo_content(cfg, ["2026-06-03_Athen"])
    assert count >= 7  # 6 indexes + featured + _index.de
    assert (cfg.hugo_content_dir / "posts" / "2026-06-03_Athen" / "index.en.md").exists()


def test_syndicate_pulls_hugo_after_done(tmp_path: Path, monkeypatch):
    cfg = make_cfg(tmp_path)
    posts = scan_blog_posts(cfg.journals_dir, cfg.pages_dir, online_only=True)
    create_dummy_assets(posts)
    target = next(p for p in posts if p.slug == "2026-06-03_Athen")

    pulled: list[list[str]] = []

    class _UploadSftp:
        def upload(self, local, remote):
            return None

    @contextmanager
    def fake_session(_cfg):
        yield _UploadSftp()

    monkeypatch.setattr("logseq_blogger.trigger.sftp_session", fake_session)
    monkeypatch.setattr("logseq_blogger.trigger.post_webhook", lambda *a, **k: None)
    monkeypatch.setattr(
        "logseq_blogger.trigger.pull_hugo_content",
        lambda config, slugs: pulled.append(list(slugs)) or 1,
    )
    monkeypatch.setattr("logseq_blogger.trigger._scan_online", lambda config: [target])

    report = syndicate(cfg, slug="2026-06-03_Athen")
    assert report.done == ["2026-06-03_Athen"]
    assert report.hugo_pulled
    assert pulled == [["2026-06-03_Athen"]]


def test_syndicate_hugo_pull_failure_keeps_done(tmp_path: Path, monkeypatch):
    cfg = make_cfg(tmp_path)
    posts = scan_blog_posts(cfg.journals_dir, cfg.pages_dir, online_only=True)
    create_dummy_assets(posts)
    target = next(p for p in posts if p.slug == "2026-06-03_Athen")

    class _UploadSftp:
        def upload(self, local, remote):
            return None

    @contextmanager
    def fake_session(_cfg):
        yield _UploadSftp()

    monkeypatch.setattr("logseq_blogger.trigger.sftp_session", fake_session)
    monkeypatch.setattr("logseq_blogger.trigger.post_webhook", lambda *a, **k: None)
    monkeypatch.setattr(
        "logseq_blogger.trigger.pull_hugo_content",
        lambda config, slugs: (_ for _ in ()).throw(TimeoutError("still building")),
    )
    monkeypatch.setattr("logseq_blogger.trigger._scan_online", lambda config: [target])

    report = syndicate(cfg, slug="2026-06-03_Athen")
    assert report.done == ["2026-06-03_Athen"]
    assert not report.hugo_pulled
    assert report.failed == [("hugo-pull", "still building")]
    assert is_syndicated(target)
