# linkedin/admin.py
from django.contrib import admin

from chat.models import ChatMessage

from linkedin.models import ActionLog, Campaign, LinkedInProfile, Post, SearchKeyword, SiteConfig, Task


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "llm_provider", "ai_model", "llm_api_base")

    def has_add_permission(self, request):
        return not SiteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "booking_link", "is_freemium", "action_fraction")
    filter_horizontal = ("users",)


@admin.register(LinkedInProfile)
class LinkedInProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "linkedin_username", "active", "legal_accepted")
    list_filter = ("active",)
    raw_id_fields = ("user", "self_lead")


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
    list_display = ("id", "campaign", "status", "short_topic", "scheduled_at", "approval_deadline", "created_at")
    list_filter = ("status", "campaign")
    readonly_fields = ("created_at", "updated_at", "published_at", "generation_attempts")
    fields = (
        "campaign", "status", "topic", "text", "image_path",
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
        from django.utils import timezone
        updated = 0
        for post in queryset.filter(status=Post.Status.PENDING_REVIEW):
            if timezone.now() > post.approval_deadline:
                post.status = Post.Status.CANCELLED
                post.save(update_fields=["status", "updated_at"])
                continue
            post.status = Post.Status.APPROVED
            post.save(update_fields=["status", "updated_at"])
            delay = max(0, (post.scheduled_at - timezone.now()).total_seconds()) if post.scheduled_at else 10
            enqueue_publish_post(post.pk, delay_seconds=delay)
            updated += 1
        self.message_user(request, str(updated) + " post(s) approved and queued.")

    @admin.action(description="Reject selected posts")
    def reject_posts(self, request, queryset):
        updated = queryset.filter(status=Post.Status.PENDING_REVIEW).update(status=Post.Status.REJECTED)
        self.message_user(request, str(updated) + " post(s) rejected.")

    @admin.action(description="Regenerate selected posts")
    def regenerate_posts(self, request, queryset):
        from django.utils import timezone
        from datetime import timedelta
        regenerated = 0
        for post in queryset.filter(status__in=[Post.Status.REJECTED, Post.Status.FAILED]):
            post.status = Post.Status.PENDING_REVIEW
            post.text = ""
            post.fail_reason = ""
            post.generation_attempts += 1
            post.approval_deadline = timezone.now() + timedelta(hours=24)
            post.save(update_fields=["status", "text", "fail_reason", "generation_attempts", "approval_deadline", "updated_at"])
            regenerated += 1
        self.message_user(request, str(regenerated) + " post(s) queued for regeneration.")
