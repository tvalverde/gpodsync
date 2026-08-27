"""Key episode-action idempotency on content, not on the reported moment.

A device re-importing its database re-stamps identical content with a fresh
wall time, so a uniqueness key carrying `happened_at` turned every re-import
into a full duplicate of the history. The dedupe runs first — the new, wider
constraint cannot land on a table already holding content-duplicates.

Schema-reversible: any dataset satisfying the content constraint trivially
satisfies the old, stricter-keyed moment constraint, so reverse-migrating the
schema never fails. Data-irreversible: the rows the dedupe deletes were, by
definition, redundant re-reports of surviving content, so the RunPython
reverse is a deliberate noop.
"""

import django.db.models.functions.comparison
from django.conf import settings
from django.db import migrations, models

from gpodsync.domain.episode_actions import content_key

# SQLite's bound-variable limit caps how many ids one DELETE may name.
DELETE_CHUNK = 500


def dedupe_content_duplicates(apps, schema_editor):
    # Keep the lowest id per (user, content) group: the first report sticks,
    # keeping its happened_at — the same outcome ignore_conflicts produces
    # against the new constraint from now on.
    record_model = apps.get_model("gpodsync", "EpisodeActionRecord")
    seen: set[tuple] = set()
    surplus: list[int] = []
    rows = record_model.objects.order_by("id").values_list(
        "id", "user_id", "podcast", "episode", "action", "guid", "started", "position", "total"
    )
    for (
        row_id,
        user_id,
        podcast,
        episode,
        action,
        guid,
        started,
        position,
        total,
    ) in rows.iterator():
        key = (
            user_id,
            content_key(
                podcast=podcast,
                episode=episode,
                action=action,
                guid=guid,
                started=started,
                position=position,
                total=total,
            ),
        )
        if key in seen:
            surplus.append(row_id)
        seen.add(key)
    for start in range(0, len(surplus), DELETE_CHUNK):
        record_model.objects.filter(id__in=surplus[start : start + DELETE_CHUNK]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("gpodsync", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(dedupe_content_duplicates, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="episodeactionrecord",
            name="one_row_per_reported_moment",
        ),
        migrations.AddConstraint(
            model_name="episodeactionrecord",
            constraint=models.UniqueConstraint(
                models.F("user"),
                models.F("podcast"),
                models.F("episode"),
                models.F("action"),
                models.F("guid"),
                django.db.models.functions.comparison.Coalesce("started", models.Value(-1)),
                django.db.models.functions.comparison.Coalesce("position", models.Value(-1)),
                django.db.models.functions.comparison.Coalesce("total", models.Value(-1)),
                name="one_row_per_reported_content",
            ),
        ),
        migrations.AddIndex(
            model_name="episodeactionrecord",
            index=models.Index(
                fields=["user", "podcast", "episode", "action", "happened_at"],
                name="gpodsync_ep_winner_idx",
            ),
        ),
    ]
