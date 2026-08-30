# tests/admin/test_crm_admin.py
"""The handoff queue an operator actually opens."""
import numpy as np
import pytest

from crm.models import Deal
from linkedin.enums import ProfileState
from linkedin.models import Task

from tests.factories import DealFactory, LeadFactory


@pytest.fixture
def staff_client(db, client):
    from django.contrib.auth.models import User

    User.objects.create_superuser("root", "root@example.com", "pw")
    client.login(username="root", password="pw")
    return client


def _handed_over_deal(session, public_id="alice"):
    from datetime import timedelta

    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    lead = LeadFactory(public_identifier=public_id, embedding=np.ones(384, dtype=np.float32).tobytes())
    deal = DealFactory(lead=lead, campaign=session.campaign, state=ProfileState.HANDOFF)
    ct = ContentType.objects.get_for_model(lead)
    for index in range(8):
        ChatMessage.objects.create(
            content_type=ct, object_id=lead.pk,
            content=f"реплика {index}", is_outgoing=(index % 2 == 0),
            owner=session.django_user, linkedin_urn=f"urn:admin:{index}",
            creation_date=deal.creation_date + timedelta(minutes=index),
        )
    return deal


@pytest.mark.django_db
class TestChangelists:
    def test_deal_and_lead_lists_render(self, staff_client, fake_session):
        _handed_over_deal(fake_session)

        assert staff_client.get("/admin/crm/deal/").status_code == 200
        assert staff_client.get("/admin/crm/lead/").status_code == 200

    def test_handoff_filter_is_the_queue(self, staff_client, fake_session):
        _handed_over_deal(fake_session, "alice")
        DealFactory(
            lead=LeadFactory(public_identifier="bob"),
            campaign=fake_session.campaign, state=ProfileState.CONNECTED,
        )

        response = staff_client.get("/admin/crm/deal/?state=Handoff")
        assert response.status_code == 200
        assert b"alice" in response.content
        assert b"bob" not in response.content

    def test_change_form_renders_the_conversation(self, staff_client, fake_session):
        deal = _handed_over_deal(fake_session)

        response = staff_client.get(f"/admin/crm/deal/{deal.pk}/change/")
        assert response.status_code == 200
        assert "реплика 7".encode() in response.content


@pytest.mark.django_db
class TestChangelistStaysCheap:
    """Regression lock: 353s vs 0.02s on the real Netherlands database."""

    def test_heavy_blobs_are_deferred(self, fake_session):
        from crm.admin import DealAdmin
        from django.contrib.admin.sites import site
        from django.test import RequestFactory

        _handed_over_deal(fake_session)
        request = RequestFactory().get("/admin/crm/deal/")
        deferred = DealAdmin(Deal, site).get_queryset(request).query.deferred_loading[0]

        # Campaign.model_blob is a ~21MB pickled GPR model and Lead.embedding a
        # 1536-byte vector. ORDER BY makes SQLite materialise every joined row
        # before LIMIT, so select_related without these defers reads the blob
        # once per deal.
        assert "campaign__model_blob" in deferred
        assert "lead__embedding" in deferred

    def test_changelist_does_not_annotate_the_last_reply(self, fake_session):
        """A correlated Subquery is evaluated before LIMIT — 14x slower here."""
        from crm.admin import DealAdmin
        from django.contrib.admin.sites import site
        from django.test import RequestFactory

        _handed_over_deal(fake_session)
        request = RequestFactory().get("/admin/crm/deal/")
        assert DealAdmin(Deal, site).get_queryset(request).query.annotations == {}


@pytest.mark.django_db
class TestReturnToBot:
    def test_moves_the_deal_back_and_enqueues_a_follow_up(self, fake_session):
        """Proves transition_deal fires the scheduler hook without a session."""
        from crm.admin import DealAdmin
        from django.contrib.admin.sites import site

        deal = _handed_over_deal(fake_session)
        Task.objects.all().delete()

        admin_instance = DealAdmin(Deal, site)
        request = type("R", (), {})()
        admin_instance.message_user = lambda *a, **kw: None
        admin_instance.return_to_bot(request, Deal.objects.filter(pk=deal.pk))

        deal.refresh_from_db()
        assert deal.state == ProfileState.CONNECTED
        assert Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP, status=Task.Status.PENDING,
        ).count() == 1

    def test_close_as_converted(self, fake_session):
        from crm.admin import DealAdmin
        from crm.models import Outcome
        from django.contrib.admin.sites import site

        deal = _handed_over_deal(fake_session)

        admin_instance = DealAdmin(Deal, site)
        admin_instance.message_user = lambda *a, **kw: None
        admin_instance.close_as_converted(type("R", (), {})(), Deal.objects.filter(pk=deal.pk))

        deal.refresh_from_db()
        assert deal.state == ProfileState.COMPLETED
        assert deal.outcome == Outcome.CONVERTED
