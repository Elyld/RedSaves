"""Aggregation queries for the Leaderboard page: top subreddits, top posters,
and links-per-week over the last 8 weeks. All counts use status='sent' links."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func

from app.models import Link


def top_subreddits(session, limit: int = 10) -> list[tuple[str, int]]:
    rows = (
        session.query(Link.subreddit, func.count(Link.id))
        .filter(Link.status == "sent", Link.subreddit != "", Link.subreddit.isnot(None))
        .group_by(Link.subreddit)
        .order_by(func.count(Link.id).desc(), Link.subreddit)
        .limit(limit)
        .all()
    )
    return [(sub, count) for sub, count in rows]


def top_authors(session, limit: int = 10) -> list[tuple[str, int]]:
    rows = (
        session.query(Link.author, func.count(Link.id))
        .filter(Link.status == "sent", Link.author != "", Link.author.isnot(None))
        .group_by(Link.author)
        .order_by(func.count(Link.id).desc(), Link.author)
        .limit(limit)
        .all()
    )
    return [(author, count) for author, count in rows]


def links_per_week(session, weeks: int = 8) -> list[tuple[str, int]]:
    """(week label, sent count) for the last `weeks` full weeks + current week,
    oldest first. Labels are the Monday date, e.g. 'Oct 5'."""
    today = datetime.now().date()
    this_monday = today - timedelta(days=today.weekday())
    out = []
    for i in range(weeks - 1, -1, -1):
        start = datetime.combine(this_monday - timedelta(days=7 * i), datetime.min.time())
        end = start + timedelta(days=7)
        count = (
            session.query(func.count(Link.id))
            .filter(Link.status == "sent",
                    Link.sent_at.isnot(None),
                    Link.sent_at >= start, Link.sent_at < end)
            .scalar()
        ) or 0
        out.append((start.strftime("%b %-d"), count))
    return out
