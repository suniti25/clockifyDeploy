from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Inspect password reset token table columns/indexes (PostgreSQL)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--table",
            default="auth_app_passwordresettoken",
            help="Table name to inspect (default: auth_app_passwordresettoken)",
        )

    def handle(self, *args, **options):
        table = options["table"]

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=%s "
                "ORDER BY ordinal_position",
                [table],
            )
            columns = [row[0] for row in cursor.fetchall()]

            cursor.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema() AND tablename=%s "
                "ORDER BY indexname",
                [table],
            )
            indexes = [row[0] for row in cursor.fetchall()]

        self.stdout.write(f"table: {table}")
        self.stdout.write(f"columns: {columns}")
        self.stdout.write(f"indexes: {indexes}")
