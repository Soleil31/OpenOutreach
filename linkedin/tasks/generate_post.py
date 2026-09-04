# linkedin/tasks/generate_post.py
"""Handler for generate_post tasks — fills a Post's body from its topic.

This was the missing link in the publishing chain. A `Post` row was created
with an empty `text`, `PostAdmin.regenerate_posts` called
`enqueue_generate_post`, and that function did not exist: the admin action
raised ImportError, and `Task.TaskType.GENERATE_POST` had no entry in the
daemon's handler table, so a row of that type would sit PENDING forever.

The chain is now: PostTopic → Post(text="") → generate_post → PENDING_REVIEW
→ a human approves in the admin → publish_post → LinkedIn.

Generation is deliberately separate from publishing. The LLM call is slow and
can fail on its own terms, and a human must read the result before anything
reaches the company's feed — the client asked for approval in the admin, not
for a bot that posts on its own.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from linkedin.models import Post

logger = logging.getLogger(__name__)


def handle_generate_post(task, session, qualifiers) -> None:
    """Write the post body. Payload: {"post_id": int}.

    Never publishes and never approves — it only moves a Post from "no text"
    to "text a human can read".
    """
    from linkedin.agents.post_generator import generate_cover_text, generate_post_text

    post_id = task.payload.get("post_id")
    post = Post.objects.select_related("campaign").filter(pk=post_id).first()
    if not post:
        logger.error("generate_post: Post %s not found", post_id)
        return

    # A post approved, published or rejected while this task waited in the
    # queue must not be silently rewritten under the human who acted on it.
    if post.status != Post.Status.PENDING_REVIEW:
        logger.info("generate_post: post %s is %s — skipping", post_id, post.status)
        return

    if post.text.strip():
        logger.info("generate_post: post %s already has text — skipping", post_id)
        return

    campaign = post.campaign
    try:
        post.text = generate_post_text(
            session,
            campaign,
            topic=post.topic,
            include_hashtags=post.include_hashtags,
            cta=post.cta,
            language=post.language or campaign.post_language or "Russian",
            hashtags_count=post.hashtags_count,
        )
    except Exception as exc:
        # A failed generation is a FAILED post a human can retry from the
        # admin, not a crashed daemon — the queue must keep moving.
        post.status = Post.Status.FAILED
        post.fail_reason = "не удалось сгенерировать текст: %s" % exc
        post.save(update_fields=["status", "fail_reason", "updated_at"])
        logger.exception("generate_post: post %s failed", post_id)
        return

    fields = ["text", "updated_at"]
    if post.media_mode == Post.MediaMode.TEMPLATE and not post.cover_text:
        try:
            post.cover_text = generate_cover_text(
                campaign,
                topic=post.topic,
                post_text=post.text,
                language=post.language or campaign.post_language or "Russian",
            )
            fields.append("cover_text")
        except Exception as exc:
            # The cover is decoration; losing it must not lose the post.
            logger.warning("generate_post: cover phrase for %s failed: %s", post_id, exc)

    # Restart the approval clock from the moment there is something to read:
    # the deadline was set when the empty Post row was created, and generation
    # may have queued behind other work for hours.
    post.approval_deadline = timezone.now() + timedelta(
        hours=campaign.post_approval_timeout_hours or 24,
    )
    fields.append("approval_deadline")
    post.save(update_fields=fields)
    logger.info(
        "generate_post: post %s ready for review (%d chars)", post_id, len(post.text),
    )
