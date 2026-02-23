from django.db import migrations


def _repair_schema_forward(apps, schema_editor):
    """Repair drifted Postgres schema for PasswordResetToken.

    This migration is intentionally database-only (no state changes). It makes
    existing databases match the state defined by earlier migrations.
    """

    if schema_editor.connection.vendor != "postgresql":
        return

    Model = apps.get_model("auth_app", "PasswordResetToken")
    table = Model._meta.db_table

    # 1) Ensure missing column exists.
    schema_editor.execute(
        "ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} timestamp with time zone NULL;".format(
            tbl=schema_editor.quote_name(table),
            col=schema_editor.quote_name("used_at"),
        )
    )

    # 2) Get existing index names/defs.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema() AND tablename = %s
            """,
            [table],
        )
        indexes = [(row[0], row[1]) for row in cursor.fetchall()]

    existing_names = {name for name, _ in indexes}

    def ensure_index(expected_name: str, column: str) -> None:
        if expected_name in existing_names:
            return

        # Prefer renaming an existing single-column index on this column to avoid duplicates.
        target = None
        needle_1 = f"({column})"
        needle_2 = f'("{column}")'
        for name, indexdef in indexes:
            if name in existing_names and (
                needle_1 in indexdef or needle_2 in indexdef
            ):
                target = name
                break

        if target and target != expected_name:
            schema_editor.execute(
                f"ALTER INDEX {schema_editor.quote_name(target)} RENAME TO {schema_editor.quote_name(expected_name)};"
            )
            existing_names.discard(target)
            existing_names.add(expected_name)
            return

        # Otherwise create the expected index.
        schema_editor.execute(
            "CREATE INDEX IF NOT EXISTS {idx} ON {tbl} ({col});".format(
                idx=schema_editor.quote_name(expected_name),
                tbl=schema_editor.quote_name(table),
                col=schema_editor.quote_name(column),
            )
        )
        existing_names.add(expected_name)

    ensure_index("auth_app_pa_expires_0d700d_idx", "expires_at")
    ensure_index("auth_app_pa_used_at_a18f43_idx", "used_at")


class Migration(migrations.Migration):
    dependencies = [
        (
            "auth_app",
            "0002_rename_auth_app_pa_expires__a1feaf_idx_auth_app_pa_expires_0d700d_idx_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(
            _repair_schema_forward, reverse_code=migrations.RunPython.noop
        ),
    ]
