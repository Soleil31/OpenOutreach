# tests/tasks/test_posts.py
"""The publishing chain: topic → post → generated text → review → publish."""
import pytest
from unittest.mock import patch

from linkedin.models import Post, PostTopic, Task
from linkedin.tasks.generate_post import handle_generate_post
from linkedin.tasks.scheduler import enqueue_generate_post, seed_posts_from_topics

from tests.tasks.test_tasks import _build_context, _make_task


def _topic(session, prompt="Как проходят платежи по импорту из Китая", **kwargs):
    return PostTopic.objects.create(campaign=session.campaign, prompt=prompt, **kwargs)


def _run(session, post):
    task = _make_task(
        Task.TaskType.GENERATE_POST,
        {"post_id": post.pk, "campaign_id": session.campaign.pk},
    )
    handle_generate_post(task, session, _build_context(session))
    post.refresh_from_db()
    return post


@pytest.mark.django_db
class TestSeedingFromTopics:
    def test_disabled_campaign_produces_nothing(self, fake_session):
        """Both dead accounts must stay silent, and so must an opted-out one."""
        fake_session.campaign.posting_enabled = False
        fake_session.campaign.save()
        _topic(fake_session)

        assert seed_posts_from_topics(fake_session) == 0
        assert Post.objects.count() == 0

    def test_topic_becomes_a_post_awaiting_generation(self, fake_session):
        fake_session.campaign.posting_enabled = True
        fake_session.campaign.save()
        topic = _topic(fake_session)

        assert seed_posts_from_topics(fake_session) == 1

        post = Post.objects.get()
        assert post.topic == topic.prompt
        assert post.text == ""
        assert post.status == Post.Status.PENDING_REVIEW
        assert post.source == Post.Source.GENERATED_TOPIC
        assert Task.objects.filter(
            task_type=Task.TaskType.GENERATE_POST, status=Task.Status.PENDING,
        ).count() == 1

        topic.refresh_from_db()
        assert topic.post_id == post.pk
        assert topic.consumed_at is not None

    def test_a_topic_is_never_consumed_twice(self, fake_session):
        """reconcile runs on every idle cycle — this must not multiply posts."""
        fake_session.campaign.posting_enabled = True
        fake_session.campaign.save()
        _topic(fake_session)

        seed_posts_from_topics(fake_session)
        seed_posts_from_topics(fake_session)
        seed_posts_from_topics(fake_session)

        assert Post.objects.count() == 1


@pytest.mark.django_db
class TestGeneration:
    @patch("linkedin.agents.post_generator.generate_post_text", return_value="Текст поста.")
    def test_fills_the_body_and_leaves_it_for_review(self, mock_gen, fake_session):
        post = Post.objects.create(
            campaign=fake_session.campaign, topic="ВЭД", text="",
            status=Post.Status.PENDING_REVIEW,
        )
        post = _run(fake_session, post)

        assert post.text == "Текст поста."
        # Never self-approves: a human reads it first.
        assert post.status == Post.Status.PENDING_REVIEW
        assert post.approval_deadline is not None

    @patch("linkedin.agents.post_generator.generate_post_text", return_value="Новый текст.")
    def test_never_overwrites_a_post_a_human_acted_on(self, mock_gen, fake_session):
        """A task queued before approval must not rewrite the approved text."""
        post = Post.objects.create(
            campaign=fake_session.campaign, topic="ВЭД", text="Одобренный текст.",
            status=Post.Status.APPROVED,
        )
        post = _run(fake_session, post)

        assert post.text == "Одобренный текст."
        mock_gen.assert_not_called()

    @patch("linkedin.agents.post_generator.generate_post_text", return_value="Второй текст.")
    def test_skips_a_post_that_already_has_text(self, mock_gen, fake_session):
        post = Post.objects.create(
            campaign=fake_session.campaign, topic="ВЭД", text="Уже написано.",
            status=Post.Status.PENDING_REVIEW,
        )
        post = _run(fake_session, post)

        assert post.text == "Уже написано."
        mock_gen.assert_not_called()

    @patch("linkedin.agents.post_generator.generate_post_text",
           side_effect=RuntimeError("шлюз недоступен"))
    def test_a_failed_generation_is_a_failed_post_not_a_dead_queue(
        self, mock_gen, fake_session,
    ):
        post = Post.objects.create(
            campaign=fake_session.campaign, topic="ВЭД", text="",
            status=Post.Status.PENDING_REVIEW,
        )
        post = _run(fake_session, post)

        assert post.status == Post.Status.FAILED
        assert "шлюз недоступен" in post.fail_reason

    @patch("linkedin.agents.post_generator.generate_cover_text",
           side_effect=RuntimeError("figma молчит"))
    @patch("linkedin.agents.post_generator.generate_post_text", return_value="Текст.")
    def test_a_lost_cover_does_not_lose_the_post(self, mock_text, mock_cover, fake_session):
        post = Post.objects.create(
            campaign=fake_session.campaign, topic="ВЭД", text="",
            media_mode=Post.MediaMode.TEMPLATE, status=Post.Status.PENDING_REVIEW,
        )
        post = _run(fake_session, post)

        assert post.text == "Текст."
        assert post.cover_text == ""

    def test_missing_post_is_survivable(self, fake_session):
        task = _make_task(
            Task.TaskType.GENERATE_POST,
            {"post_id": 999999, "campaign_id": fake_session.campaign.pk},
        )
        handle_generate_post(task, fake_session, _build_context(fake_session))


@pytest.mark.django_db
class TestEnqueue:
    def test_the_admin_action_can_import_and_call_it(self, fake_session):
        """This is what raised ImportError: the function did not exist."""
        post = Post.objects.create(campaign=fake_session.campaign, topic="ВЭД")
        Task.objects.all().delete()

        assert enqueue_generate_post(post.pk, fake_session.campaign.pk) is True
        assert Task.objects.filter(task_type=Task.TaskType.GENERATE_POST).count() == 1

    def test_one_pending_generation_per_post(self, fake_session):
        post = Post.objects.create(campaign=fake_session.campaign, topic="ВЭД")
        Task.objects.all().delete()

        enqueue_generate_post(post.pk, fake_session.campaign.pk)
        enqueue_generate_post(post.pk, fake_session.campaign.pk)

        assert Task.objects.filter(task_type=Task.TaskType.GENERATE_POST).count() == 1


@pytest.mark.django_db
class TestHandlerIsWired:
    def test_daemon_knows_how_to_run_a_generate_post_task(self):
        """A type with no handler sits PENDING forever and nothing says why."""
        from linkedin.daemon import TASK_WATCHDOG_SECONDS, _HANDLERS

        assert Task.TaskType.GENERATE_POST in _HANDLERS
        assert Task.TaskType.GENERATE_POST in TASK_WATCHDOG_SECONDS
