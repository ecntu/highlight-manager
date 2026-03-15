from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib import error, request

from sqlalchemy.orm import joinedload

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db_schema
from app.models import Highlight, HighlightStatus, NtfyConfig, Reminder


def build_notification_body(reminder_count: int) -> str:
    return (
        f"You have {reminder_count} reminder{'s' if reminder_count != 1 else ''} due."
    )


def send_ntfy_message(
    config: NtfyConfig, reminder_count: int, click_url: str | None
) -> None:
    base_url = config.server_url.rstrip("/")
    topic = (config.topic or "").strip()
    if not topic:
        raise ValueError("Missing ntfy topic")

    title = "Highlight Reminders due"
    body = build_notification_body(reminder_count).encode("utf-8")
    req = request.Request(
        f"{base_url}/{topic}",
        data=body,
        method="POST",
        headers={
            "Title": title,
            "Tags": "bell",
            "Priority": "default",
        },
    )
    if config.access_token:
        req.add_header("Authorization", f"Bearer {config.access_token}")

    if click_url:
        req.add_header("Click", click_url)

    with request.urlopen(req, timeout=15) as response:
        status_code = getattr(response, "status", response.getcode())
        if status_code >= 400:
            raise RuntimeError(f"ntfy returned HTTP {status_code}")


def main() -> int:
    init_db_schema()
    db = SessionLocal()
    now = datetime.now(UTC).replace(tzinfo=None)
    sent_batches = 0
    failed_batches = 0
    skipped = 0

    try:
        due_reminders = (
            db.query(Reminder)
            .options(joinedload(Reminder.highlight).joinedload(Highlight.source))
            .join(NtfyConfig, NtfyConfig.user_id == Reminder.user_id)
            .filter(
                Reminder.remind_at <= now,
                Reminder.notification_sent_at.is_(None),
                NtfyConfig.enabled.is_(True),
                NtfyConfig.topic.is_not(None),
            )
            .order_by(Reminder.remind_at.asc())
            .all()
        )

        reminders_by_user: dict[str, list[Reminder]] = defaultdict(list)
        for reminder in due_reminders:
            highlight = reminder.highlight
            if not highlight or highlight.status != HighlightStatus.ACTIVE:
                skipped += 1
                continue
            reminders_by_user[reminder.user_id].append(reminder)

        app_base_url = os.getenv("PHM_BASE_URL", "").strip().rstrip("/")

        for user_id, reminders in reminders_by_user.items():
            config = db.query(NtfyConfig).filter(NtfyConfig.user_id == user_id).first()
            if not config or not config.enabled or not config.topic:
                skipped += len(reminders)
                continue

            click_url = f"{app_base_url}/reminders" if app_base_url else None
            for reminder in reminders:
                reminder.notification_last_attempt_at = now
            try:
                send_ntfy_message(config, len(reminders), click_url)
            except (
                error.HTTPError,
                error.URLError,
                TimeoutError,
                ValueError,
                RuntimeError,
            ) as exc:
                for reminder in reminders:
                    reminder.notification_error = str(exc)
                failed_batches += 1
            else:
                sent_at = datetime.now(UTC).replace(tzinfo=None)
                for reminder in reminders:
                    reminder.notification_sent_at = sent_at
                    reminder.notification_error = None
                sent_batches += 1
            db.commit()

        print(
            "ntfy reminders processed: "
            f"sent_batches={sent_batches} failed_batches={failed_batches} "
            f"skipped={skipped} total_reminders={len(due_reminders)} users={len(reminders_by_user)}"
        )
        return 0 if failed_batches == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
