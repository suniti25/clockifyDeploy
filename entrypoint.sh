#!/bin/bash
set -e

python manage.py migrate --noinput --settings=leave_management_system.settings
python manage.py collectstatic --noinput --clear --settings=leave_management_system.settings

exec "$@"
