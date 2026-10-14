"""Tests for the webhook client (accept handling + retries)."""

from unittest.mock import patch

import httpx
import pytest

from logseq_blogger.webhook import WebhookError, post_webhook


def _resp(status_code=200, json_body=None):
    req = httpx.Request("POST", "https://n8n.example/webhook/publish")
    if json_body is None:
        return httpx.Response(status_code, request=req)
    return httpx.Response(status_code, json=json_body, request=req)


def test_http_200_is_accepted():
    with patch("logseq_blogger.webhook.httpx.post", return_value=_resp(200)):
        post_webhook("https://n8n.example/webhook/publish", {"a": 1}, label="/publish")


def test_http_200_ignores_body():
    """n8n onReceived may return a default body; only the status code matters."""
    with patch(
        "logseq_blogger.webhook.httpx.post",
        return_value=_resp(200, {"message": "Workflow was started"}),
    ):
        post_webhook("https://n8n.example/webhook/publish", {"a": 1})


def test_missing_url_raises():
    with pytest.raises(WebhookError):
        post_webhook("", {"a": 1})


def test_retries_then_raises_on_persistent_failure():
    with (
        patch("logseq_blogger.webhook.time.sleep"),
        patch("logseq_blogger.webhook.httpx.post", side_effect=httpx.ConnectError("down")) as post,
    ):
        with pytest.raises(WebhookError):
            post_webhook("https://n8n.example/webhook/publish", {"a": 1}, retries=3)
    assert post.call_count == 3


def test_http_error_is_retried():
    with (
        patch("logseq_blogger.webhook.time.sleep"),
        patch("logseq_blogger.webhook.httpx.post", return_value=_resp(500)),
    ):
        with pytest.raises(WebhookError):
            post_webhook("https://n8n.example/webhook/publish", {"a": 1}, retries=2)


def test_read_timeout_is_accepted_without_retry():
    req = httpx.Request("POST", "https://n8n.example/webhook/publish")
    with patch(
        "logseq_blogger.webhook.httpx.post",
        side_effect=httpx.ReadTimeout("queued", request=req),
    ) as post:
        post_webhook("https://n8n.example/webhook/publish", {"a": 1})
    assert post.call_count == 1


def test_connect_timeout_is_retried():
    req = httpx.Request("POST", "https://n8n.example/webhook/publish")
    with (
        patch("logseq_blogger.webhook.time.sleep"),
        patch(
            "logseq_blogger.webhook.httpx.post",
            side_effect=httpx.ConnectTimeout("down", request=req),
        ) as post,
    ):
        with pytest.raises(WebhookError):
            post_webhook("https://n8n.example/webhook/publish", {"a": 1}, retries=3)
    assert post.call_count == 3
