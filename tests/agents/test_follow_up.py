"""Tests for the follow-up agent context builder + Jinja template."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from tests.factories import LeadFactory, DealFactory


@pytest.fixture
def deal_with_summaries(db, fake_session):
    lead = LeadFactory(public_identifier="alice")
    return DealFactory(
        lead=lead,
        campaign=fake_session.campaign,
        profile_summary={"facts": [
            "Senior engineer at Acme Corp.",
            "Based in Berlin, Germany.",
            "Speaks English and German.",
        ]},
        chat_summary={"facts": [
            "Lead is curious about pricing.",
            "Lead has a small team budget.",
        ]},
    )


def _msg(content, is_outgoing):
    m = MagicMock()
    m.content = content
    m.is_outgoing = is_outgoing
    m.creation_date = None
    return m


def _store(deal, session, *pairs):
    """Persist (content, is_outgoing) pairs as ChatMessages for deal.lead."""
    from datetime import timedelta

    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(deal.lead)
    for index, (content, is_outgoing) in enumerate(pairs):
        ChatMessage.objects.create(
            content_type=ct, object_id=deal.lead_id,
            content=content, is_outgoing=is_outgoing,
            owner=session.django_user,
            linkedin_urn=f"urn:msg:{deal.pk}:{index}",
            creation_date=deal.creation_date + timedelta(minutes=index + 1),
        )


class TestRenderSystemPrompt:
    def test_includes_three_summary_blocks(self, db, fake_session, deal_with_summaries):
        from linkedin.agents.follow_up import _render_system_prompt

        # Stub session.self_profile so the prompt builder works without a browser.
        fake_session.self_profile = {"first_name": "Bob", "last_name": "Builder", "urn": "urn:li:fsd_profile:SELF"}

        recent = [_msg("Hi, what do you do?", is_outgoing=True), _msg("Sales tooling.", is_outgoing=False)]
        prompt = _render_system_prompt(fake_session, deal_with_summaries, recent)

        # Profile facts appear under the lead-knowledge block.
        assert "Senior engineer at Acme Corp." in prompt
        assert "Based in Berlin, Germany." in prompt
        # Chat facts appear under the conversation-knowledge block.
        assert "Lead is curious about pricing." in prompt
        # Verbatim recent messages appear in Me:/Lead: format.
        assert "Me: Hi, what do you do?" in prompt
        assert "Lead: Sales tooling." in prompt
        # The legacy flat fields are gone.
        assert "Headline:" not in prompt
        assert "Company:" not in prompt

    def test_handles_missing_summaries_gracefully(self, db, fake_session):
        from linkedin.agents.follow_up import _render_system_prompt

        lead = LeadFactory(public_identifier="bob")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)
        fake_session.self_profile = {"first_name": "Bob", "last_name": "Builder", "urn": "urn:li:fsd_profile:SELF"}

        prompt = _render_system_prompt(fake_session, deal, [])

        # Renders without crashing and shows the empty placeholders.
        assert "(none yet)" in prompt
        assert "No recent messages." in prompt


class TestConversationStage:
    """The pivot is decided in Python — the agent only ever sees 6 messages."""

    @pytest.mark.parametrize("pairs,expected", [
        ([], "opening"),
        # We spoke, nobody answered yet.
        ([("Видел ваш профиль — чем занимаетесь?", True)], "opening"),
        # The lead wrote first: we must not open with a commercial question.
        ([("Добрый день, вы по каким проектам?", False)], "opening"),
        # Politeness is not an answer.
        ([("Видел ваш профиль", True), ("ок", False)], "opening"),
        # A real answer arms the pivot.
        ([("Видел ваш профиль", True), ("да, возим оборудование сами", False)], "qualify"),
        # Two of our turns later the window has closed.
        ([
            ("Видел ваш профиль", True),
            ("да, возим оборудование сами", False),
            ("а платежи как проводите?", True),
            ("через банк напрямую", False),
            ("любопытно", True),
        ], "advance"),
    ])
    def test_stage_table(self, pairs, expected):
        from linkedin.agents.follow_up import _conversation_stage

        assert _conversation_stage([_msg(c, o) for c, o in pairs]) == expected

    def test_lead_question_outranks_everything(self):
        """Request #2: answering the lead beats whatever stage the counters say."""
        from linkedin.agents.follow_up import _conversation_stage

        messages = [_msg(c, o) for c, o in [
            ("Видел ваш профиль", True),
            ("да, возим оборудование сами", False),
            ("а платежи как проводите?", True),
            ("через банк", False),
            ("понятно", True),
            ("а почему вы задаёте их мне?", False),
        ]]
        # The counters alone would say "advance".
        assert _conversation_stage(messages) == "answer_first"


BRAND = "Cexim Group"


class TestScriptStages:
    """The script the client specified: opener → discovery → qualify →
    introduce → advance, with refusal and readiness overriding everything."""

    def _stage(self, pairs):
        from linkedin.agents.follow_up import _conversation_stage

        return _conversation_stage([_msg(c, o) for c, o in pairs], BRAND)

    def test_discovery_holds_while_nothing_concrete_was_said(self):
        assert self._stage([
            ("Видел профиль — чем занимаетесь?", True),
            ("Занимаемся сборкой узлов, всё довольно стандартно", False),
        ]) == "discovery"

    def test_a_named_pain_pivots_immediately(self):
        """No waiting for the counter once they name the actual problem."""
        assert self._stage([
            ("Видел профиль", True),
            ("Возим оборудование из Китая, с платежами постоянные сложности", False),
        ]) == "qualify"

    def test_the_question_budget_pivots_without_any_signal(self):
        from linkedin.agents.follow_up import PIVOT_AFTER_OUR_QUESTIONS

        pairs = [("Видел профиль, чем занимаетесь?", True), ("Управляю производством узлов", False)]
        for i in range(PIVOT_AFTER_OUR_QUESTIONS - 1):
            pairs += [(f"А как у вас устроено {i}?", True), ("Ну, по-разному бывает у нас", False)]
        assert self._stage(pairs) == "qualify"

    def test_introduce_fires_once_the_commercial_question_is_answered(self):
        assert self._stage([
            ("Видел профиль", True),
            ("Возим оборудование сами, платежи через банк идут туго", False),
            ("А напрямую возите или через подрядчика?", True),
            ("Напрямую, своими силами", False),
        ]) == "introduce"

    def test_advance_once_we_have_named_ourselves(self):
        assert self._stage([
            ("Видел профиль", True),
            ("Возим оборудование сами, с платежами беда", False),
            ("А напрямую или через подрядчика?", True),
            ("Напрямую", False),
            (f"Мы в {BRAND} занимаемся финансовой логистикой.", True),
            ("Понятно, спасибо за пояснение", False),
        ]) == "advance"


class TestRefusalAndReadinessOverride:
    def _stage(self, pairs):
        from linkedin.agents.follow_up import _conversation_stage

        return _conversation_stage([_msg(c, o) for c, o in pairs], BRAND)

    @pytest.mark.parametrize("refusal", [
        "Нет, спасибо. Эта последовательность понятна",
        "Времени для встречи с вами у меня нет. Добра вам!",
        "Не интересно, извините",
        "Это допрос какой-то",
        "Not interested, thanks",
    ])
    def test_a_refusal_stops_the_script(self, refusal):
        assert self._stage([("Видел профиль", True), (refusal, False)]) == "stand_down"

    def test_a_refusal_stays_in_force_after_our_own_reply(self):
        """The bot kept narrowing after андрей-бутов said no — twice."""
        assert self._stage([
            ("Видел профиль", True),
            ("Возим оборудование, платежи сложные", False),
            ("Времени для встречи с вами у меня нет. Добра вам!", False),
            ("Понял, не буду отвлекать.", True),
        ]) == "stand_down"

    @pytest.mark.parametrize("signal", [
        "Лучше, полагаю, созвониться",
        "Интересно, расскажите подробнее",
        "Мой номер +79832100002, напишите в Максе",
        "Можем запланировать созвон и обсудить голосом",
        "Пишите на importsales@fesco.com",
    ])
    def test_readiness_moves_straight_to_closing(self, signal):
        assert self._stage([("Видел профиль", True), (signal, False)]) == "closing"

    def test_closing_holds_after_we_proposed_a_slot(self):
        """Otherwise the bot drifts back to questions and the interest cools."""
        assert self._stage([
            ("Видел профиль", True),
            ("Давайте созвонимся", False),
            ("Отлично, давайте завтра в 11:00 по Москве.", True),
        ]) == "closing"

    def test_a_bare_ok_confirms_a_proposed_slot(self):
        assert self._stage([
            ("Видел профиль", True),
            ("Возим оборудование, платежи идут туго", False),
            ("Давайте завтра в 11:30, напишу в Telegram.", True),
            ("Ок, жду", False),
        ]) == "closing"

    def test_a_bare_ok_on_its_own_means_nothing(self):
        assert self._stage([
            ("Видел профиль, чем занимаетесь?", True),
            ("Ок", False),
        ]) == "opening"

    def test_a_refusal_outranks_a_readiness_word(self):
        assert self._stage([
            ("Видел профиль", True),
            ("Созвон не нужен, не интересно", False),
        ]) == "stand_down"


class TestStripAckOpener:
    """Request #3 — the deterministic half, which catches the first repeat."""

    @pytest.mark.parametrize("original,expected", [
        (
            "Понял, спасибо за уточнение. А как сейчас выстроен процесс закупок?",
            "А как сейчас выстроен процесс закупок?",
        ),
        # The dash-joined form is the common Russian one and defeats a naive
        # sentence splitter.
        (
            "Понял, спасибо за уточнение — а как сейчас выстроен процесс закупок?",
            "А как сейчас выстроен процесс закупок?",
        ),
        # Synonym rotation must not slip through.
        (
            "Ясно, спасибо за пояснение. Работаете напрямую или через подрядчиков?",
            "Работаете напрямую или через подрядчиков?",
        ),
        (
            "Got it, thanks for clarifying. How do you handle customs today?",
            "How do you handle customs today?",
        ),
    ])
    def test_strips_and_recapitalizes(self, original, expected):
        from linkedin.agents.follow_up import _strip_ack_opener

        assert _strip_ack_opener(original) == expected

    @pytest.mark.parametrize("original", [
        "Понял.",                                  # nothing would remain
        "Понял. Да.",                              # only a fragment would remain
        "А как вы сейчас возите оборудование?",    # no formula to strip
        "",
    ])
    def test_leaves_message_alone(self, original):
        from linkedin.agents.follow_up import _strip_ack_opener

        assert _strip_ack_opener(original) == original


class TestPreviousOpeners:
    def test_own_messages_newest_first_deduped(self):
        from linkedin.agents.follow_up import _previous_openers

        openers = _previous_openers([_msg(c, o) for c, o in [
            ("Понял, спасибо. А как?", True),
            ("через подрядчика", False),
            ("Понял, спасибо. А что по срокам?", True),
            ("Добрый день! Видел профиль.", True),
            ("", True),
        ]])
        assert openers == ["Добрый день", "Понял, спасибо"]

    def test_empty_for_a_fresh_lead(self):
        from linkedin.agents.follow_up import _previous_openers

        assert _previous_openers([]) == []
        assert _previous_openers([_msg("привет", False)]) == []


class TestStagedPrompt:
    """The prompt must carry exactly one mode, and never the deleted lines."""

    @pytest.fixture
    def prepared(self, db, fake_session, deal_with_summaries):
        fake_session.self_profile = {
            "first_name": "Bob", "last_name": "Builder", "urn": "urn:li:fsd_profile:SELF",
        }
        return fake_session, deal_with_summaries

    def test_opening_stage_hides_the_qualifying_question(self, prepared):
        from linkedin.agents.follow_up import _render_system_prompt
        from linkedin.agents.follow_up_defaults import DEFAULT_QUALIFYING_QUESTION

        session, deal = prepared
        prompt = _render_system_prompt(session, deal, [])

        assert "Mode: Discovery" in prompt
        assert DEFAULT_QUALIFYING_QUESTION not in prompt

    def test_qualify_stage_carries_the_question(self, prepared):
        from linkedin.agents.follow_up import _render_system_prompt
        from linkedin.agents.follow_up_defaults import DEFAULT_QUALIFYING_QUESTION

        session, deal = prepared
        _store(deal, session, ("Видел ваш профиль", True), ("да, возим оборудование сами", False))
        prompt = _render_system_prompt(session, deal, [])

        assert "Mode: Qualify" in prompt
        assert DEFAULT_QUALIFYING_QUESTION in prompt
        assert "never paste it verbatim" in prompt
        assert "Mode: Discovery" not in prompt

    def test_campaign_override_wins_over_the_repo_default(self, prepared):
        from linkedin.agents.follow_up import _render_system_prompt
        from linkedin.agents.follow_up_defaults import DEFAULT_QUALIFYING_QUESTION

        session, deal = prepared
        deal.campaign.qualifying_question = "Кто у них возит станки из Китая?"
        deal.campaign.save()
        _store(deal, session, ("Видел ваш профиль", True), ("да, возим оборудование сами", False))
        prompt = _render_system_prompt(session, deal, [])

        assert "Кто у них возит станки из Китая?" in prompt
        assert DEFAULT_QUALIFYING_QUESTION not in prompt

    def test_answer_first_stage_answers_instead_of_pitching(self, prepared):
        from linkedin.agents.follow_up import _render_system_prompt
        from linkedin.agents.follow_up_defaults import DEFAULT_QUALIFYING_QUESTION

        session, deal = prepared
        _store(
            deal, session,
            ("Видел ваш профиль", True),
            ("да, возим оборудование сами", False),
            ("а платежи как проводите?", True),
            ("а почему вы задаёте их мне?", False),
        )
        prompt = _render_system_prompt(session, deal, [])

        assert "Mode: Answer them straight" in prompt
        assert "up to 4 short sentences" in prompt
        assert DEFAULT_QUALIFYING_QUESTION not in prompt

    def test_advance_stage_forbids_re_asking(self, prepared):
        from linkedin.agents.follow_up import _render_system_prompt
        from linkedin.agents.follow_up_defaults import DEFAULT_QUALIFYING_QUESTION

        session, deal = prepared
        _store(
            deal, session,
            ("Видел ваш профиль", True),
            ("да, возим оборудование сами", False),
            ("а платежи как проводите?", True),
            ("через банк", False),
            ("Мы в Cexim Group занимаемся финансовой логистикой.", True),
            ("понятно", False),
        )
        prompt = _render_system_prompt(session, deal, [])

        assert "Never ask it again" in prompt
        assert DEFAULT_QUALIFYING_QUESTION not in prompt

    def test_openers_block_appears_only_when_we_have_spoken(self, prepared):
        from linkedin.agents.follow_up import _render_system_prompt

        session, deal = prepared
        assert "Openers You Have Already Used" not in _render_system_prompt(session, deal, [])

        _store(deal, session, ("Понял, спасибо за уточнение. А как?", True))
        prompt = _render_system_prompt(session, deal, [])
        assert "Openers You Have Already Used" in prompt
        assert '"Понял, спасибо за уточнение"' in prompt
        assert "must NOT begin like any of them" in prompt

    def test_history_before_the_deal_does_not_arm_the_pivot(self, prepared):
        """Months of pre-existing human threads live on these accounts."""
        from datetime import timedelta

        from chat.models import ChatMessage
        from django.contrib.contenttypes.models import ContentType

        from linkedin.agents.follow_up import _render_system_prompt

        session, deal = prepared
        ct = ContentType.objects.get_for_model(deal.lead)
        for index, (content, outgoing) in enumerate([
            ("старое сообщение от нас", True),
            ("старый содержательный ответ лида", False),
        ]):
            ChatMessage.objects.create(
                content_type=ct, object_id=deal.lead_id,
                content=content, is_outgoing=outgoing,
                owner=session.django_user, linkedin_urn=f"urn:old:{index}",
                creation_date=deal.creation_date - timedelta(days=30),
            )

        assert "Mode: Discovery" in _render_system_prompt(session, deal, [])


class TestPromptRegressionLocks:
    """Two lines whose return would re-open the client's complaints."""

    def test_pitching_on_a_direct_question_is_gone(self, db, fake_session, deal_with_summaries):
        from linkedin.agents.follow_up import _render_system_prompt

        fake_session.self_profile = {"first_name": "Bob", "last_name": "B", "urn": "urn:x"}
        prompt = _render_system_prompt(fake_session, deal_with_summaries, [])

        # Request #2: this bullet told the bot to pitch at the exact moment the
        # client praised it for explaining itself honestly.
        assert "The lead asks what you do or how you could help" not in prompt

    def test_literal_phrasing_mirroring_is_gone(self, db, fake_session, deal_with_summaries):
        from linkedin.agents.follow_up import _render_system_prompt

        fake_session.self_profile = {"first_name": "Bob", "last_name": "B", "urn": "urn:x"}
        prompt = _render_system_prompt(fake_session, deal_with_summaries, [])

        # Request #3: mirroring the last message is what produced the repeated
        # "Понял, спасибо за уточнение." prefix.
        assert "literal phrasing" not in prompt
        assert "never preface your reply with an acknowledgment" in prompt
        assert "Спасибо за уточнение" in prompt


class TestLoadRecentMessages:
    def test_returns_last_n_in_chronological_order(self, db, fake_session):
        from chat.models import ChatMessage
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone
        from datetime import timedelta

        from linkedin.agents.follow_up import _load_recent_messages, RECENT_MESSAGES_WINDOW

        lead = LeadFactory(public_identifier="alice")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)
        ct = ContentType.objects.get_for_model(lead)

        base = timezone.now()
        for i in range(RECENT_MESSAGES_WINDOW + 3):
            ChatMessage.objects.create(
                content_type=ct, object_id=lead.pk,
                content=f"msg-{i}",
                is_outgoing=(i % 2 == 0),
                owner=fake_session.django_user,
                linkedin_urn=f"urn:msg:{i}",
                creation_date=base + timedelta(minutes=i),
            )

        recent = _load_recent_messages(deal)

        # Window respected and chronological order preserved.
        assert len(recent) == RECENT_MESSAGES_WINDOW
        contents = [m.content for m in recent]
        assert contents == sorted(contents, key=lambda c: int(c.split("-")[1]))
        # Returned the *latest* messages.
        assert contents[-1] == f"msg-{RECENT_MESSAGES_WINDOW + 2}"
