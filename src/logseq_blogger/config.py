"""Load YAML config into plain dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class SiteConfig:
    base_url: str
    title: str = "Sailing Nomads"
    default_language: str = "en"


@dataclass
class SftpConfig:
    host: str
    port: int = 22
    user: str = "sftp"
    base_dir: str = "/syndicator"


@dataclass
class WebhooksConfig:
    publish_url: str = ""
    reel_url: str = ""


@dataclass
class SharedConfig:
    site: SiteConfig
    sftp: SftpConfig
    webhooks: WebhooksConfig = field(default_factory=WebhooksConfig)


@dataclass
class LocalConfig:
    logseq_dir: Path
    sftp_key: Path
    hugo_dir: Path
    hugo_pull_timeout_sec: int = 3600
    hugo_pull_poll_sec: int = 15


@dataclass
class Config:
    shared: SharedConfig
    local: LocalConfig
    repo_root: Path = REPO_ROOT

    @property
    def journals_dir(self) -> Path:
        return self.local.logseq_dir / "journals"

    @property
    def pages_dir(self) -> Path:
        return self.local.logseq_dir / "pages"

    @property
    def hugo_content_dir(self) -> Path:
        return self.local.hugo_dir / "content"


def _site(raw: dict) -> SiteConfig:
    return SiteConfig(
        base_url=raw["base_url"],
        title=raw.get("title", "Sailing Nomads"),
        default_language=raw.get("default_language", "en"),
    )


def _sftp(raw: dict) -> SftpConfig:
    return SftpConfig(
        host=raw["host"],
        port=int(raw.get("port", 22)),
        user=raw.get("user", "sftp"),
        base_dir=raw.get("base_dir", "/syndicator"),
    )


def _webhooks(raw: dict | None) -> WebhooksConfig:
    raw = raw or {}
    return WebhooksConfig(
        publish_url=raw.get("publish_url", "") or "",
        reel_url=raw.get("reel_url", "") or "",
    )


def shared_from_dict(data: dict) -> SharedConfig:
    return SharedConfig(
        site=_site(data["site"]),
        sftp=_sftp(data["sftp"]),
        webhooks=_webhooks(data.get("webhooks")),
    )


def local_from_dict(data: dict) -> LocalConfig:
    key = data["sftp_key"]
    hugo = data["hugo_dir"]
    return LocalConfig(
        logseq_dir=Path(data["logseq_dir"]),
        sftp_key=Path(key).expanduser() if isinstance(key, str) else Path(key),
        hugo_dir=Path(hugo).expanduser() if isinstance(hugo, str) else Path(hugo),
        hugo_pull_timeout_sec=int(data.get("hugo_pull_timeout_sec", 3600)),
        hugo_pull_poll_sec=int(data.get("hugo_pull_poll_sec", 15)),
    )


def load_config(repo_root: Path | None = None) -> Config:
    root = repo_root or REPO_ROOT
    path = root / "config.local.yaml"

    if not path.exists():
        raise FileNotFoundError(
            f"missing {path} — copy config.local.yaml.example and fill in values"
        )

    data = yaml.safe_load(path.read_text()) or {}
    return Config(
        shared=shared_from_dict(data),
        local=local_from_dict(data),
        repo_root=root,
    )
