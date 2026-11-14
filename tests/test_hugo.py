"""Tests for content rewriting helpers used by staging + payloads."""

from pathlib import Path

from logseq_blogger.extract import scan_blog_posts
from logseq_blogger.trigger import build_content, collect_asset_copies, hugo_basename, transform_content

FIXTURES = Path(__file__).parent / "fixtures"


def all_posts():
    return {p.slug: p for p in scan_blog_posts(FIXTURES / "journals", FIXTURES / "pages")}


def test_asset_copies_are_flattened():
    post = all_posts()["2024-06-14_Renan"]
    copies = collect_asset_copies(build_content(post), post.source_path.parent)
    assert copies, "Renan references assets"
    for src, name in copies:
        assert "/" not in name
        assert "assets" in str(src)


def test_hugo_basename():
    assert hugo_basename("photo.png") == "photo.png"
    assert hugo_basename("clip.mov") == "clip.mp4"
    assert hugo_basename("already.jpg") == "already.jpg"


def test_transform_content_keeps_source_filenames():
    content = "![a](../assets/Renan/foo.png) ![b](../assets/Renan/bar.MOV)"
    adapted = transform_content(content)
    assert "foo.png" in adapted
    assert '{{< video src="bar.MOV" >}}' in adapted


def test_transform_content_youtube_shortcode():
    content = "{{video https://youtu.be/FAIZtHHsbSM}}"
    assert transform_content(content) == "{{< youtube FAIZtHHsbSM >}}"


def test_asset_path_with_parentheses_in_filename(tmp_path: Path):
    name = "Young_Albanian_being_chased_by_a_Turk_(Dance_of_Zalongo)_1794392035253_0.png"
    journals = tmp_path / "journals"
    assets = tmp_path / "assets"
    journals.mkdir()
    assets.mkdir()
    (assets / name).write_bytes(b"png")
    content = f"![alt_(x).png](../assets/{name})"

    adapted = transform_content(content)
    assert adapted == f"![alt_(x).png]({name})"

    copies = collect_asset_copies(content, journals)
    assert len(copies) == 1
    src, basename = copies[0]
    assert basename == name
    assert src == (assets / name).resolve()
    assert src.exists()
