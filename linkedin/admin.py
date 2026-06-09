# linkedin/admin.py
from django import forms
from django.contrib import admin, messages
from django.utils import timezone

from chat.models import ChatMessage

from linkedin.browser.cookies import normalise_cookie_export, storage_state_summary
from linkedin.models import (
    ActionLog,
    Campaign,
    LinkedInProfile,
    Post,
    PostTopic,
    SearchKeyword,
    SiteConfig,
    Task,
)


class LinkedInProfileAdminForm(forms.ModelForm):
    cookie_import_json = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "false"}),
        help_text=(
            "Paste the full cookie JSON export here. On save it will be converted "
            "to Playwright storage_state and this field will be cleared."
        ),
    )

    class Meta:
        model = LinkedInProfile
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.normalised_cookie_state = None

    def clean_cookie_import_json(self):
        value = self.cleaned_data.get("cookie_import_json", "").strip()
        if value:
            try:
                self.normalised_cookie_state = normalise_cookie_export(value)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return value


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "llm_provider", "ai_model", "llm_api_base")
    fields = ("llm_provider", "llm_api_key", "ai_model", "llm_api_base", "figma_token")

    def has_add_permission(self, request):
        return not SiteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "booking_link",
        "is_freemium",
        "outreach_enabled",
        "action_fraction",
        "posting_enabled",
        "posts_per_week",
        "post_language",
        "post_timezone",
    )
    list_filter = ("posting_enabled", "outreach_enabled", "is_freemium")
    search_fields = ("name", "product_docs", "campaign_objective")
    filter_horizontal = ("users",)
    fieldsets = (
        (None, {
            "fields": (
                "name",
                "users",
                "product_docs",
                "campaign_objective",
                "booking_link",
                "is_freemium",
                "outreach_enabled",
                "action_fraction",
                "seed_public_ids",
            ),
        }),
        ("Figma", {
            "fields": ("figma_file_key",),
            "classes": ("collapse",),
        }),
        ("Posting", {
            "fields": (
                "posting_enabled",
                "post_system_prompt",
                "post_language",
                "post_timezone",
                "post_days_of_week",
                "post_times",
                "posts_per_week",
                "post_approval_timeout_hours",
            ),
        }),
        ("Posting — Prompt templates (advanced)", {
            "description": (
                "Шаблоны промптов для AI-генерации постов и обложек. "
                "Если оставить пустыми — используются встроенные дефолты. "
                "Доступные подстановки описаны под каждым полем."
            ),
            "fields": (
                "post_prompt_template",
                "cover_text_prompt_template",
            ),
            "classes": ("collapse",),
        }),
    )


@admin.register(LinkedInProfile)
class LinkedInProfileAdmin(admin.ModelAdmin):
    form = LinkedInProfileAdminForm
    list_display = (
        "user",
        "linkedin_username",
        "active",
        "legal_accepted",
        "has_saved_cookies",
        "browser_timezone",
    )
    list_filter = ("active",)
    raw_id_fields = ("user", "self_lead")
    readonly_fields = ("cookie_summary", "cookie_imported_at")
    actions = ("clear_saved_cookies",)
    fieldsets = (
        (None, {
            "fields": (
                "user",
                "self_lead",
                "linkedin_username",
                "linkedin_password",
                "active",
                "legal_accepted",
                "subscribe_newsletter",
                "newsletter_processed",
            ),
        }),
        ("Limits", {
            "fields": (
                "connect_daily_limit",
                "connect_weekly_limit",
                "follow_up_daily_limit",
            ),
        }),
        ("Browser fingerprint", {
            "fields": (
                "browser_user_agent",
                "browser_locale",
                "browser_timezone",
                "browser_is_mobile",
                "browser_has_touch",
                "browser_viewport_width",
                "browser_viewport_height",
            ),
        }),
        ("Cookies", {
            "fields": (
                "cookie_summary",
                "cookie_imported_at",
                "cookie_import_json",
            ),
        }),
    )

    @admin.display(boolean=True, description="Cookies")
    def has_saved_cookies(self, obj):
        return bool(obj.cookie_data)

    def cookie_summary(self, obj):
        return storage_state_summary(obj.cookie_data)

    def save_model(self, request, obj, form, change):
        normalised_cookie_state = getattr(form, "normalised_cookie_state", None)
        if normalised_cookie_state:
            obj.cookie_data = normalised_cookie_state
            obj.cookie_import_json = ""
            obj.cookie_imported_at = timezone.now()
        super().save_model(request, obj, form, change)
        if normalised_cookie_state:
            self.message_user(
                request,
                "Cookies imported: " + storage_state_summary(normalised_cookie_state),
                level=messages.SUCCESS,
            )

    @admin.action(description="Clear saved cookies")
    def clear_saved_cookies(self, request, queryset):
        updated = queryset.update(
            cookie_data=None,
            cookie_import_json="",
            cookie_imported_at=None,
        )
        self.message_user(request, str(updated) + " profile(s) cleared.")


@admin.register(SearchKeyword)
class SearchKeywordAdmin(admin.ModelAdmin):
    list_display = ("keyword", "campaign", "used", "used_at")
    list_filter = ("used", "campaign")
    raw_id_fields = ("campaign",)


@admin.register(ActionLog)
class ActionLogAdmin(admin.ModelAdmin):
    list_display = ("action_type", "linkedin_profile", "campaign", "created_at")
    list_filter = ("action_type", "campaign")
    raw_id_fields = ("linkedin_profile", "campaign")
    date_hierarchy = "created_at"
    readonly_fields = ("linkedin_profile", "campaign", "action_type", "created_at")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("task_type", "status", "scheduled_at", "payload", "created_at")
    list_filter = ("task_type", "status")
    readonly_fields = (
        "task_type", "status", "scheduled_at", "payload",
        "created_at", "started_at", "completed_at",
    )
    date_hierarchy = "scheduled_at"


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("content_type", "object_id", "owner", "creation_date")
    list_filter = ("content_type", "owner")
    raw_id_fields = ("owner", "answer_to", "topic")
    date_hierarchy = "creation_date"
    readonly_fields = ("content_type", "object_id", "content", "owner", "creation_date")


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "campaign",
        "status",
        "short_topic",
        "scheduled_at",
        "approval_deadline",
        "media_mode",
        "created_at",
    )
    list_filter = ("status", "campaign", "media_mode", "source", "include_hashtags")
    search_fields = ("topic", "text", "campaign__name")
    readonly_fields = ("created_at", "updated_at", "published_at", "generation_attempts")
    fields = (
        "campaign", "status", "source",
        "topic", "text",
        "language", "include_hashtags", "hashtags_count", "cta",
        "media_mode", "image_path", "image_template_key", "cover_text",
        "scheduled_at", "approval_deadline",
        "generation_attempts", "fail_reason",
        "created_at", "updated_at", "published_at",
    )
    actions = ["approve_posts", "reject_posts", "regenerate_posts"]
    date_hierarchy = "created_at"

    @admin.display(description="Topic")
    def short_topic(self, obj):
        return obj.topic[:60] + "..." if len(obj.topic) > 60 else obj.topic

    @admin.action(description="Approve selected posts")
    def approve_posts(self, request, queryset):
        from linkedin.tasks.scheduler import enqueue_publish_post
        updated = 0
        skipped = 0
        for post in queryset.filter(status=Post.Status.PENDING_REVIEW):
            if post.cancel_if_overdue():
                skipped += 1
                continue
            if not post.text.strip():
                skipped += 1
                continue
            if post.approval_deadline and timezone.now() > post.approval_deadline:
                post.status = Post.Status.CANCELLED
                post.save(update_fields=["status", "updated_at"])
                skipped += 1
                continue
            post.status = Post.Status.APPROVED
            post.save(update_fields=["status", "updated_at"])
            delay = max(0, (post.scheduled_at - timezone.now()).total_seconds()) if post.scheduled_at else 10
            enqueue_publish_post(post.pk, delay_seconds=delay)
            updated += 1
        self.message_user(
            request,
            str(updated) + " post(s) approved and queued; "
            + str(skipped) + " skipped.",
        )

    @admin.action(description="Reject selected posts")
    def reject_posts(self, request, queryset):
        updated = queryset.filter(status=Post.Status.PENDING_REVIEW).update(
            status=Post.Status.REJECTED,
            updated_at=timezone.now(),
        )
        self.message_user(request, str(updated) + " post(s) rejected.")

    @admin.action(description="Regenerate selected posts")
    def regenerate_posts(self, request, queryset):
        from datetime import timedelta
        from linkedin.tasks.scheduler import enqueue_generate_post

        regenerated = 0
        for post in queryset.filter(status__in=[Post.Status.REJECTED, Post.Status.FAILED]):
            post.status = Post.Status.PENDING_REVIEW
            post.text = ""
            post.fail_reason = ""
            post.generation_attempts += 1
            post.approval_deadline = timezone.now() + timedelta(
                hours=post.campaign.post_approval_timeout_hours or 24,
            )
            post.save(update_fields=["status", "text", "fail_reason", "generation_attempts", "approval_deadline", "updated_at"])
            enqueue_generate_post(post.pk, post.campaign_id)
            regenerated += 1
        self.message_user(request, str(regenerated) + " post(s) queued for regeneration.")


@admin.register(PostTopic)
class PostTopicAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "campaign",
        "short_prompt",
        "language",
        "media_mode",
        "include_hashtags",
        "hashtags_count",
        "consumed_at",
        "created_at",
    )
    list_filter = ("campaign", "media_mode", "include_hashtags", "source", "consumed_at")
    search_fields = ("prompt", "campaign__name", "cta", "image_template_key")
    raw_id_fields = ("campaign", "post")
    readonly_fields = ("consumed_at", "post", "created_at", "updated_at")
    fields = (
        "campaign",
        "prompt",
        "language",
        "include_hashtags",
        "hashtags_count",
        "cta",
        "media_mode",
        "image_template_key",
        "source",
        "consumed_at",
        "post",
        "created_at",
        "updated_at",
    )

    @admin.display(description="Prompt")
    def short_prompt(self, obj):
        return obj.prompt[:80] + "..." if len(obj.prompt) > 80 else obj.prompt
