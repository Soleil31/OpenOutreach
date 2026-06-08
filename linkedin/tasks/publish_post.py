# linkedin/tasks/publish_post.py
"""Handler for publish_post tasks.

Publishing strategy: spawn a one-shot desktop browser context on the
session's existing Playwright/Browser, copy cookies from the mobile
context, drive the desktop feed composer UI to create the post, then
close the desktop context. See ``linkedin/actions/post.py`` for details.

Why not Voyager API: LinkedIn's ``/voyager/api/contentCreation/normShares``
endpoint returns 404 — they migrated content-creation to GraphQL with
session-scoped queryIds we can't recover without DevTools access. UI
automation against the desktop composer is the documented cookie-based
publishing pattern and stable across LinkedIn releases.
"""
from __future__ import annotations

import logging

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

    from linkedin.actions.post import publish_image_post, publish_text_post

    session.ensure_browser()
    try:
        image_path = _maybe_prepare_cover(post)
        if image_path:
            publish_image_post(session, post.text, image_path)
        else:
            publish_text_post(session, post.text)

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
