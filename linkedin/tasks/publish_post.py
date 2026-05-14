# linkedin/tasks/publish_post.py
"""Handler for publish_post tasks."""
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

    from linkedin.actions.post import publish_text_post

    session.ensure_browser()
    try:
        publish_text_post(session.page, post.text)
        post.status = Post.Status.PUBLISHED
        post.published_at = timezone.now()
        post.save(update_fields=["status", "published_at", "updated_at"])
        logger.info("Post %s published for campaign %s", post_id, post.campaign.name)
    except Exception as exc:
        post.status = Post.Status.FAILED
        post.fail_reason = str(exc)
        post.save(update_fields=["status", "fail_reason", "updated_at"])
        raise
