"""Shared test helpers."""

from pathlib import Path

from logseq_blogger.config import Config, LocalConfig, shared_from_dict

FIXTURES = Path(__file__).parent / "fixtures"


def make_cfg(tmp_path: Path) -> Config:
    """Config with a temp Logseq graph (populated from fixtures)."""
    logseq = tmp_path / "logseq"
    (logseq / "journals").mkdir(parents=True)
    (logseq / "pages").mkdir(parents=True)
    for f in (FIXTURES / "journals").glob("*.md"):
        (logseq / "journals" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    for f in (FIXTURES / "pages").glob("*.md"):
        (logseq / "pages" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    shared = shared_from_dict(
        {
            "site": {"base_url": "https://example.org", "default_language": "en"},
            "sftp": {"host": "staging.example", "port": 22, "user": "sftp", "base_dir": "/syndicator"},
            "webhooks": {
                "publish_url": "https://n8n.example/webhook/publish",
                "reel_url": "https://n8n.example/webhook/reel",
            },
        }
    )
    hugo = tmp_path / "hugo"
    (hugo / "content").mkdir(parents=True)
    local = LocalConfig(
        logseq_dir=logseq,
        sftp_key=tmp_path / "sftp_key",
        hugo_dir=hugo,
        hugo_pull_timeout_sec=5,
        hugo_pull_poll_sec=0,
    )
    return Config(shared=shared, local=local, repo_root=tmp_path)


def create_dummy_assets(posts) -> None:
    """Create placeholder files for all media referenced by the given posts."""
    for post in posts:
        for media in post.all_media():
            if media.kind == "youtube" or media.source_path is None:
                continue
            media.source_path.parent.mkdir(parents=True, exist_ok=True)
            if not media.source_path.exists():
                media.source_path.write_bytes(b"dummy")
