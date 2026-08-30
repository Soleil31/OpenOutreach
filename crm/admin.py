# crm/admin.py
"""Django Admin for Leads and Deals — the human side of the pipeline.

This is where a handed-over lead is actually picked up: filter Deals by
``state = Handoff`` and you are looking at everyone waiting for a call.
Until this module existed neither model was visible in the admin at all,
so the only way to see the pipeline was sqlite3 on the server.
"""
from django.contrib import admin, messages
from django.contrib.contenttypes.models import ContentType
from django.utils.html import format_html, format_html_join

from chat.models import ChatMessage

from crm.models import Deal, Lead, Outcome
from linkedin.enums import ProfileState
from linkedin.handoff import profile_url


def _profile_link(public_identifier: str):
    if not public_identifier:
        return "—"
    return format_html(
        '<a href="{}" target="_blank" rel="noopener">{}</a>',
        profile_url(public_identifier), public_identifier,
    )


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("public_identifier", "profile", "disqualified", "creation_date")
    list_filter = ("disqualified",)
    search_fields = ("public_identifier", "linkedin_url")
    # The 384-dim embedding blob has no business in a form.
    exclude = ("embedding",)
    ordering = ("-creation_date",)

    @admin.display(description="LinkedIn")
    def profile(self, obj):
        return _profile_link(obj.public_identifier)


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    """The work queue. `state = Handoff` is the list a salesperson works."""

    list_display = (
        "id", "profile", "campaign", "state", "outcome", "last_lead_reply", "update_date",
    )
    list_filter = ("state", "campaign", "outcome")
    search_fields = ("lead__public_identifier", "reason")
    # A plain FK select renders every Lead — thousands of rows on the phone
    # where the Telegram alert lands.
    raw_id_fields = ("lead",)
    readonly_fields = ("profile", "conversation", "creation_date", "update_date")
    ordering = ("-update_date",)
    actions = ("return_to_bot", "close_as_converted")
    # The changelist's own count() is enough; the extra unfiltered total is not
    # worth a second full scan of a table this size.
    show_full_result_count = False

    def get_queryset(self, request):
        # The two defers are not an optimisation, they are what makes this page
        # load at all. `Campaign.model_blob` holds the pickled GPR model and
        # `Lead.embedding` a 1536-byte vector; ORDER BY forces SQLite to
        # materialise every joined row before LIMIT, so select_related drags
        # both blobs across all ~6.8k deals. Measured on the Netherlands
        # database: 353s with them, 0.02s without.
        #
        # `last_lead_reply` is filled per displayed row rather than by a
        # Subquery annotation for the same reason — a correlated subquery in
        # the SELECT list is evaluated before LIMIT too. Measured on the same
        # data: 0.488s annotated vs 0.035s for 100 per-row lookups.
        return (
            super().get_queryset(request)
            .select_related("lead", "campaign")
            .defer("campaign__model_blob", "lead__embedding")
        )

    @admin.display(description="LinkedIn")
    def profile(self, obj):
        return _profile_link(obj.lead.public_identifier if obj.lead_id else "")

    @admin.display(description="Последний ответ лида")
    def last_lead_reply(self, obj):
        if not obj.lead_id:
            return "—"
        text = (
            ChatMessage.objects
            .filter(
                content_type=ContentType.objects.get_for_model(Lead),
                object_id=obj.lead_id,
                is_outgoing=False,
            )
            .order_by("-creation_date", "-pk")
            .values_list("content", flat=True)
            .first()
        ) or ""
        text = text.strip()
        return (text[:80] + "…") if len(text) > 80 else (text or "—")

    @admin.display(description="Переписка")
    def conversation(self, obj):
        if not obj.lead_id:
            return "—"
        lead_ct = ContentType.objects.get_for_model(Lead)
        messages_qs = (
            ChatMessage.objects
            .filter(content_type=lead_ct, object_id=obj.lead_id)
            .order_by("-creation_date", "-pk")[:20]
        )
        rows = [
            ("Я" if m.is_outgoing else "Лид", (m.content or "").strip())
            for m in reversed(list(messages_qs))
        ]
        if not rows:
            return "—"
        return format_html_join("", "<div><b>{}:</b> {}</div>", rows)

    @admin.action(description="Вернуть боту (снова CONNECTED)")
    def return_to_bot(self, request, queryset):
        from linkedin.db.deals import transition_deal

        moved = 0
        for deal in queryset.select_related("lead"):
            # transition_deal fires the scheduler hook, which enqueues the
            # follow_up that HANDOFF deliberately withheld.
            transition_deal(deal, ProfileState.CONNECTED.value, reason="возвращён боту вручную")
            moved += 1
        self.message_user(request, f"Возвращено боту: {moved}", messages.SUCCESS)

    @admin.action(description="Закрыть как converted")
    def close_as_converted(self, request, queryset):
        from linkedin.db.deals import transition_deal

        closed = 0
        for deal in queryset.select_related("lead"):
            transition_deal(
                deal, ProfileState.COMPLETED.value, outcome=Outcome.CONVERTED,
            )
            closed += 1
        self.message_user(request, f"Закрыто как converted: {closed}", messages.SUCCESS)
