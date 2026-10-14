"""Webhook client for the n8n ``/publish`` and ``/reel`` endpoints."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class WebhookError(RuntimeError):
    """A webhook could not be delivered after all retries."""


def post_webhook(
    url: str,
    payload: dict,
    *,
    label: str = "webhook",
    retries: int = 3,
    accept_timeout: float = 15.0,
) -> None:
    """POST ``payload`` and treat a 2xx or a queued hold-open as a successful hand-off.

    n8n webhooks use ``responseMode: onReceived``. When a production slot is
    free, n8n replies with HTTP 2xx as soon as the execution is created. When
    ``N8N_CONCURRENCY_PRODUCTION_LIMIT`` is in effect and the slot is taken,
    n8n saves the execution then holds the HTTP connection until a slot
    frees. After the request body has been sent, a read timeout therefore
    means the job is queued, not that delivery failed. Body content is
    ignored.
    """
    if not url:
        raise WebhookError(f"{label}: no webhook URL configured")

    timeout = httpx.Timeout(accept_timeout, connect=10.0)
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            log.info("%s accepted HTTP %s (attempt %d)", label, resp.status_code, attempt)
            return
        except httpx.ReadTimeout:
            log.info(
                "%s sent; no reply within %ss — queued in n8n",
                label,
                accept_timeout,
            )
            return
        except httpx.HTTPError as err:
            last_err = err
            if attempt == retries:
                break
            wait = 2 ** (attempt - 1)
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %ds",
                label,
                attempt,
                retries,
                err,
                wait,
            )
            time.sleep(wait)

    raise WebhookError(f"{label}: not accepted after {retries} attempts") from last_err
