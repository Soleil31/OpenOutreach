# linkedin/profile_budget.py
"""How many LinkedIn profiles the account may open, per day and per hour.

On 2026-09-18 LinkedIn restricted the NL account: "it has accessed an
unusually high volume of LinkedIn profile data". The day before, the bot had
opened 742 profiles — about a hundred an hour for eight hours. Nothing in the
code limited that. Slow model fits, browser crash storms and wedged tasks had
been throttling the pipeline by accident, and fixing them removed the brake.

``check`` runs BEFORE the Voyager request, so a spent budget costs nothing on
LinkedIn's side. ``record`` runs after it, whatever the response: LinkedIn
counts the request, not whether we liked the answer.
"""
from __future__ import annotations

import datetime

from django.utils import timezone

from linkedin.exceptions import ProfileViewLimitReached


def check(linkedin_profile) -> None:
    """Raise ProfileViewLimitReached when the daily or hourly budget is spent."""
    from linkedin.models import ProfileView

    # Limits are read fresh every time, so an edit in the admin applies to
    # the very next profile without a restart.
    linkedin_profile.refresh_from_db(
        fields=["profile_view_daily_limit", "profile_view_hourly_limit"])
    views = ProfileView.objects.filter(linkedin_profile=linkedin_profile)
    now = timezone.now()

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_limit = linkedin_profile.profile_view_daily_limit
    if views.filter(created_at__gte=day_start).count() >= daily_limit:
        tomorrow = day_start + datetime.timedelta(days=1)
        raise ProfileViewLimitReached(
            (tomorrow - now).total_seconds(),
            f"daily profile-view budget spent ({daily_limit})",
        )

    hourly_limit = linkedin_profile.profile_view_hourly_limit
    last_hour = views.filter(created_at__gte=now - datetime.timedelta(hours=1))
    if last_hour.count() >= hourly_limit:
        oldest = last_hour.order_by("created_at").first().created_at
        raise ProfileViewLimitReached(
            max((oldest + datetime.timedelta(hours=1) - now).total_seconds(), 60),
            f"hourly profile-view budget spent ({hourly_limit})",
        )


def record(linkedin_profile) -> None:
    from linkedin.models import ProfileView

    ProfileView.objects.create(linkedin_profile=linkedin_profile)
