"""Public API for caller apps: queue one SMS through the OAN Messaging Service.

    from oan_messaging_connector.api import queue_sms

    queue_sms(
        recipient=doc.phone_number,
        text=rendered_text,                       # one SMS part, see sms.fits_one_sms
        purpose="transactional",                  # otp | transactional | alert
        source_reference=f"a2c:{doc.name}:application_submitted",
        reference_doctype=doc.doctype, reference_name=doc.name,
        event="application_submitted",
        expires_at=None,                          # aware datetime; required for otp
    )

Call it inside the business transaction. It inserts one **OAN Message** row (its random
``delivery_id`` is the Idempotency-Key) and schedules dispatch **after commit**, so a
rolled-back business change sends nothing. It never calls OAN itself and never raises
because of messaging: when the connector is disabled or the message is invalid it
returns ``None`` and records why in the Error Log, so a business action is never
blocked by an SMS.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import frappe

from .outbox import DOCTYPE, enabled, record_for_insert
from .sms import fits_one_sms
from .vendor.outbox_dispatch import OutboxRow

PURPOSES = frozenset({"otp", "transactional", "alert"})  # broadcast: not in the pilot
_SOURCE_REF = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class InvalidMessage(ValueError):
	pass


def validate(recipient: str, text: str, purpose: str, source_reference: str,
             expires_at: datetime | None) -> None:
	if not recipient or not recipient.strip():
		raise InvalidMessage("recipient is empty")
	if purpose not in PURPOSES:
		raise InvalidMessage(f"purpose must be one of {sorted(PURPOSES)}")
	if not text or not fits_one_sms(text):
		raise InvalidMessage("text is empty or does not fit one SMS part")
	if not _SOURCE_REF.fullmatch(source_reference or ""):
		raise InvalidMessage("source_reference must match [A-Za-z0-9._:-]{1,128}")
	if expires_at is not None and expires_at.tzinfo is None:
		raise InvalidMessage("expires_at must be timezone-aware")
	if purpose == "otp" and expires_at is None:
		raise InvalidMessage("an otp message needs expires_at")


def queue_sms(
	*,
	recipient: str | None,
	text: str,
	purpose: str,
	source_reference: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	event: str | None = None,
	language: str | None = None,
	expires_at: datetime | None = None,
) -> str | None:
	"""Queue one SMS; returns the OAN Message name, or None (disabled or invalid)."""
	if not enabled():
		return None
	try:
		validate(recipient or "", text, purpose, source_reference, expires_at)
	except InvalidMessage as exc:
		frappe.log_error(f"OAN SMS not queued ({reference_doctype} {reference_name} {event}): "
		                 f"{exc}", "OAN Messaging")
		return None
	now = datetime.now(UTC)
	if expires_at is not None and expires_at <= now:
		frappe.log_error(f"OAN SMS not queued ({reference_name} {event}): already expired",
		                 "OAN Messaging")
		return None
	row = OutboxRow(
		delivery_id="",  # set by OAN Message.before_insert (UUID4)
		recipient=recipient.strip(), text=text, source_reference=source_reference,
		purpose=purpose, expires_at=expires_at, first_submitted_at=now,
	)
	values: dict[str, Any] = record_for_insert(row)
	values.update({"doctype": DOCTYPE, "reference_doctype": reference_doctype,
	               "reference_name": reference_name, "event": event,
	               "language": language, "desk_status": "Queued"})
	values.pop("delivery_id")
	doc = frappe.get_doc(values)
	# The reference is informational: it may be a record created in this same
	# transaction, or one a caller deletes later. Never fail the message on it.
	doc.flags.ignore_links = True
	savepoint = "oan_queue_sms"
	frappe.db.savepoint(savepoint)
	try:
		doc.insert(ignore_permissions=True)
	except Exception as exc:  # never break the caller's business action
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(f"OAN SMS not queued ({reference_doctype} {reference_name} {event}): "
		                 f"{type(exc).__name__}", "OAN Messaging")
		return None
	frappe.enqueue("oan_messaging_connector.outbox.dispatch_queued", queue="short",
	               enqueue_after_commit=True)
	return doc.name


def messages_for(reference_doctype: str, reference_name: str) -> list[dict[str, Any]]:
	"""Delivery state of every message about one business record (for UIs and support)."""
	return frappe.get_all(
		DOCTYPE,
		filters={"reference_doctype": reference_doctype, "reference_name": reference_name},
		fields=["name", "event", "desk_status", "oan_status", "needs_attention", "creation"],
		order_by="creation asc",
	)
