from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from form_app.models import LeaveRequest
from form_app.helpers import compute_paid_status

from discord_app.services import (
    update_discord_leave_message,
    send_admin_approved_today,
    send_approval_email_to_employee,
    send_rejection_email_to_employee,
)


logger = logging.getLogger(__name__)


def notify_leave_decision(*, leave_id: int) -> None:
    logger.debug("notify_leave_decision called for leave_id=%s", leave_id)
    try:
        lr = LeaveRequest.objects.select_related("employee", "employee__user").get(
            id=leave_id
        )
    except LeaveRequest.DoesNotExist:
        logger.debug(
            "LeaveRequest %s does not exist in notify_leave_decision", leave_id
        )
        return

    # Best-effort admin daily summary
    try:
        if (
            lr.status == LeaveRequest.STATUS_APPROVED
            and timezone.localdate().weekday() not in (5, 6)
        ):
            send_admin_approved_today()
    except Exception:
        pass

    # Best-effort discord updates
    try:
        update_discord_leave_message(lr)
    except Exception:
        pass

    old_id = getattr(lr, "reapplied_from_id", None)
    if old_id:
        try:
            old_leave = (
                LeaveRequest.objects.select_related("employee", "employee__user")
                .filter(id=old_id)
                .first()
            )
            if old_leave and old_leave.status == LeaveRequest.STATUS_VOIDED:
                update_discord_leave_message(old_leave)
        except Exception:
            pass

    # Best-effort email
    try:
        if lr.status == LeaveRequest.STATUS_APPROVED:
            send_approval_email_to_employee(lr)
        else:
            send_rejection_email_to_employee(lr)
    except Exception:
        pass


def decide_leave(
    *,
    leave_id: int,
    new_status: str,
    message: str | None = None,
    notify: bool = True,
) -> LeaveRequest:
    msg = (message or "").strip() or None
    if new_status not in (LeaveRequest.STATUS_APPROVED, LeaveRequest.STATUS_REJECTED):
        raise ValueError("new_status must be APPROVED or REJECTED")

    old_leave = None

    with transaction.atomic():
        lr = (
            LeaveRequest.objects.select_for_update()
            .select_related("employee", "employee__user")
            .get(id=leave_id)
        )

        if lr.status != LeaveRequest.STATUS_PENDING:
            raise ValueError(f"Already processed. Current status: {lr.status}")

        if new_status == LeaveRequest.STATUS_APPROVED:
            lr.status = LeaveRequest.STATUS_APPROVED
            lr.rejection_reason = None
            lr.approval_reason = msg

            #  approval timestamp
            lr.approved_at = timezone.now()

            #  reset daily notification flags
            lr.notified_admin_at = None
            lr.notified_employee_at = None

            leave_days = lr.total_days()
            lr.is_paid = bool(
                compute_paid_status(
                    employee=lr.employee,
                    leave_type=lr.leave_type,
                    leave_days=leave_days,
                    start_date=lr.start_date,
                    end_date=getattr(lr, "end_date", None),
                    instance_id=lr.id,
                )
            )

            lr.save(
                update_fields=[
                    "status",
                    "is_paid",
                    "rejection_reason",
                    "approval_reason",
                    "approved_at",
                    "notified_admin_at",
                    "notified_employee_at",
                ]
            )
            # If this is a reapply and old leave was APPROVED, void the old leave
            old_id = getattr(lr, "reapplied_from_id", None)
            if old_id:
                old_leave = (
                    LeaveRequest.objects.select_for_update()
                    .select_related("employee", "employee__user")
                    .filter(id=old_id)
                    .first()
                )
                if old_leave and old_leave.status == LeaveRequest.STATUS_APPROVED:
                    old_leave.status = LeaveRequest.STATUS_VOIDED
                    old_leave.save(update_fields=["status"])

        else:
            if not msg:
                raise ValueError("message is required for rejection")

            lr.status = LeaveRequest.STATUS_REJECTED
            lr.rejection_reason = msg
            lr.approval_reason = None

            # optional: clear approval metadata
            lr.approved_at = None
            lr.notified_admin_at = None
            lr.notified_employee_at = None

            lr.save(
                update_fields=[
                    "status",
                    "rejection_reason",
                    "approval_reason",
                    "approved_at",
                    "notified_admin_at",
                    "notified_employee_at",
                ]
            )

    if notify:
        notify_leave_decision(leave_id=lr.id)

    return lr
