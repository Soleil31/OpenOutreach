# linkedin/tasks/publish_post.py
"""Handler for publish_post tasks.

Publishes through LinkedIn's Voyager API (``/voyager/api/contentCreation/normShares``)
rather than driving the feed UI. The bot's browser profile is mobile
(Android Chrome UA), and LinkedIn's mobile web UI deliberately omits the
"create a post" composer — the feature only exists in the native app or
on desktop. Voyager is the same API LinkedIn's own clients use; it accepts
our session regardless of UI variant, and the requests go out from the
existing browser context so cookies + fingerprint match perfectly.
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.utils import timezone

from linkedin.models import Post

logger = logging.getLogger(__name__)


def handle_publish_post(task, session, qualifiers) -> None:
    """Publish an approved post to LinkedIn.

    Payload: {"post_id": int}
    """
    post_id = task.payload.get("post_id")
    post = Post.objects.select_related("campaign").filter(pk=post_id).first()

    if not post:
        logger.error("Post %s not found", post_id)
        return

    # Check approval deadline — cancel if overdue
    if post.cancel_if_overdue():
        logger.info("Post %s cancelled (approval deadline passed)", post_id)
        return

    if post.status != Post.Status.APPROVED:
        logger.info("Post %s skipped (status=%s)", post_id, post.status)
        return

    from linkedin.api.client import PlaywrightLinkedinAPI

    session.ensure_browser()
    # Voyager requests must originate from a linkedin.com page so fetch()
    # inherits the right cookies/headers. Navigate the bot to the feed if
    # it isn't already on a linkedin.com URL.
    current_url = session.page.url or ""
    if "linkedin.com" not in current_url:
        session.page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )

    try:
        image_path = _maybe_prepare_cover(post)
        client = PlaywrightLinkedinAPI(session)

        if image_path:
            image_bytes = Path(image_path).read_bytes()
            filename = Path(image_path).name
            content_type = _guess_image_content_type(filename)
            logger.info(
                "Post %s: registering Voyager image upload (%d bytes, %s)",
                post_id, len(image_bytes), content_type,
            )
            upload_url, media_urn = client.register_image_upload(
                len(image_bytes), filename,
            )
            logger.info("Post %s: media URN = %s", post_id, media_urn)
            client.put_image_bytes(upload_url, image_bytes, content_type=content_type)
            post_urn = client.create_post(post.text, media_urn=media_urn)
            logger.info(
                "Post %s published with image via Voyager → %s",
                post_id, post_urn,
            )
        else:
            post_urn = client.create_post(post.text)
            logger.info(
                "Post %s published (text-only) via Voyager → %s",
                post_id, post_urn,
            )

        post.status = Post.Status.PUBLISHED
        post.published_at = timezone.now()
        post.fail_reason = ""
        post.save(update_fields=["status", "published_at", "fail_reason", "updated_at"])
        logger.info("Post %s published for campaign %s", post_id, post.campaign.name)
    except Exception as exc:
        post.status = Post.Status.FAILED
        post.fail_reason = str(exc)
        post.save(update_fields=["status", "fail_reason", "updated_at"])
        raise


def _guess_image_content_type(filename: str) -> str:
    """Map a filename suffix to an HTTP Content-Type for image uploads."""
    suffix = Path(filename).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")


def _maybe_prepare_cover(post):
    """Return a path to a composed cover image, or None if no cover is needed.

    Returns None when:
      * ``media_mode`` is not ``TEMPLATE`` (text-only / AI / uploaded handled elsewhere)
      * the campaign has no ``figma_file_key`` configured
      * ``SiteConfig.figma_token`` is missing

    Crashes on Figma API errors per project convention (caller marks Post FAILED).
    """
    if post.media_mode != Post.MediaMode.TEMPLATE:
        return None

    campaign = post.campaign
    if not campaign.figma_file_key:
        logger.info(
            "Post %s media_mode=template but campaign has no figma_file_key — text-only",
            post.pk,
        )
        return None

    from linkedin.integrations.figma import compose_cover, get_template_png
    from linkedin.models import SiteConfig

    cfg = SiteConfig.load()
    if not cfg.figma_token:
        logger.warning(
            "Post %s wants Figma cover but SiteConfig.figma_token is empty — text-only",
            post.pk,
        )
        return None

    template_png = get_template_png(campaign.figma_file_key, cfg.figma_token)
    cover_text = post.cover_text or post.topic[:120]
    cover_path = compose_cover(template_png, cover_text, post.pk)
    # Persist for traceability — image_path lets the admin/UI see what was sent
    post.image_path = str(cover_path)
    post.save(update_fields=["image_path", "updated_at"])
    logger.info("Post %s cover composed: %s", post.pk, cover_path)
    return cover_path
