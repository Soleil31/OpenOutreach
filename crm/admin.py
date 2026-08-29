# crm/admin.py
"""Django Admin for Leads and Deals — the human side of the pipeline.

This is where a handed-over lead is actually picked up: filter Deals by
``state = Handoff`` and you are looking at everyone waiting for a call.
Until this module existed neither model was visible in the admin at all,
so the only way to see the pipeline was sqlite3 on the server.
"""
from django.contrib import admin, messages
from django.contrib.contenttypes.models import ContentType
from django.db.models import OuterRef, Subquery
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

    def get_queryset(self, request):
        lead_ct = ContentType.objects.get_for_model(Lead)
        newest_reply = (
            ChatMessage.objects
            .filter(content_type=lead_ct, object_id=OuterRef("lead_id"), is_outgoing=False)
            .order_by("-creation_date", "-pk")
            .values("content")[:1]
        )
        return (
            super().get_queryset(request)
            .select_related("lead", "campaign")
            .annotate(_last_lead_reply=Subquery(newest_reply))
        )

    @admin.display(description="LinkedIn")
    def profile(self, obj):
        return _profile_link(obj.lead.public_identifier if obj.lead_id else "")

    @admin.display(description="Последний ответ лида")
    def last_lead_reply(self, obj):
        text = getattr(obj, "_last_lead_reply", None) or ""
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
