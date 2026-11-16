"""Dispatch and status polling for the OAN Message outbox.

* ``dispatch_queued`` sends due queued rows: one row per transaction,
  ``FOR UPDATE SKIP LOCKED`` (safe with several workers), using the tested
  ``outbox_dispatch.dispatch`` logic. A 202/200 marks the row *Submitted* (accepted by
  OAN), never "Sent". An uncertain outcome is retried later with the same key and body.
* ``poll_statuses`` asks OAN for the status of submitted rows on a backoff schedule
  (``outbox_dispatch.reconcile``). It never resends; ``unknown`` sets
  ``needs_attention``. SMS polling stops after ``receipt_wait_days``.

* ``purge_content`` (daily) clears the message text and masks the recipient of rows
  that no longer need them (no longer queued, and polling finished or never needed),
  once they are older than ``content_retention_days`` (default 7). Delivery metadata
  stays as the audit trail.

Configuration: **OAN Messaging Settings**. ``site_config.json`` keys
``oan_messaging_enabled``, ``oan_messaging_base_url``, ``oan_messaging_token`` and
``oan_messaging_ca_bundle`` override it, so a deployment can inject the token from its secret store.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import frappe
from frappe.utils import now_datetime

from .vendor.oan_client import ClientConfig, OanClient, Secret
from .vendor.outbox_dispatch import OutboxRow, dispatch, reconcile

DOCTYPE = "OAN Message"
SETTINGS = "OAN Messaging Settings"
TABLE = f"`tab{DOCTYPE}`"
ROW_FIELDS = [f.name for f in dataclasses.fields(OutboxRow)]
DESK_STATUS = {"queued": "Queued", "accepted": "Submitted"}
_DATES = ("expires_at", "first_submitted_at", "next_attempt_at", "next_poll_at", "poll_deadline")
_FLAGS = ("oan_final", "needs_attention", "poll_complete")
#: Tests may inject an httpx transport (e.g. ``httpx.MockTransport``).
_transport: Any = None


def _settings() -> dict[str, Any]:
	s = frappe.get_cached_doc(SETTINGS)
	token = frappe.conf.get("oan_messaging_token")
	if not token and s.get("token"):
		token = s.get_password("token", raise_exception=False)
	return {
		"enabled": bool(frappe.conf.get("oan_messaging_enabled", s.get("enabled"))),
		"base_url": frappe.conf.get("oan_messaging_base_url") or s.get("base_url"),
		"token": token,
		"ca_bundle": frappe.conf.get("oan_messaging_ca_bundle") or s.get("ca_bundle") or None,
		"max_attempts": int(s.get("max_attempts") or 3),
		"receipt_wait": timedelta(days=int(s.get("receipt_wait_days") or 7)),
	}


def enabled() -> bool:
	s = _settings()
	return bool(s["enabled"] and s["base_url"] and s["token"])


def _run(step: Callable[[OanClient], Awaitable[Any]]) -> Any:
	s = _settings()
	cfg = ClientConfig(base_url=s["base_url"], token=Secret(s["token"]),
	                   ca_bundle=s["ca_bundle"], max_attempts=s["max_attempts"])

	async def go() -> Any:
		client = OanClient(cfg, transport=_transport) if _transport is not None else OanClient(cfg)
		async with client:
			return await step(client)

	return asyncio.run(go())  # Frappe workers are synchronous


def _iso(value: datetime) -> str:
	return value.astimezone(UTC).isoformat()  # always UTC: lexically sortable in SQL


def record_for_insert(row: OutboxRow) -> dict[str, Any]:
	rec = row.to_record()
	for name in _FLAGS:
		rec[name] = 1 if rec[name] else 0
	for name in _DATES:
		if rec[name]:
			rec[name] = _iso(datetime.fromisoformat(rec[name]))
	return rec


def _row(doc: Any) -> OutboxRow:
	rec = {name: doc.get(name) or None for name in ROW_FIELDS}
	rec["attempts"] = int(doc.get("attempts") or 0)
	rec["polls"] = int(doc.get("polls") or 0)
	for name in _FLAGS:
		rec[name] = bool(doc.get(name))
	return OutboxRow.from_record(rec)


def _save(name: str, row: OutboxRow) -> None:
	rec = record_for_insert(row)
	rec.pop("delivery_id")  # never rewritten
	rec["desk_status"] = DESK_STATUS.get(row.state, "Failed")
	frappe.db.set_value(DOCTYPE, name, rec, update_modified=True)


def _retention_days() -> int:
	conf = frappe.conf.get("oan_messaging_content_retention_days")
	if conf is None:
		conf = frappe.db.get_single_value(SETTINGS, "content_retention_days")
	try:
		return max(1, int(conf or 7))
	except (TypeError, ValueError):
		return 7


def mask_recipient(recipient: str | None) -> str:
	"""Keep only the last two digits, e.g. ``********01``."""
	value = (recipient or "").strip()
	return "*" * max(0, len(value) - 2) + value[-2:] if value else ""


def purge_content(limit: int = 500) -> int:
	"""Clear text and mask the recipient of finished rows past the retention period.

	Runs whether or not the connector is enabled (privacy). Queued rows, and submitted
	rows still being polled, keep their content. Returns the number of rows purged.
	"""
	# ``modified`` is stored in the site's time zone, like now_datetime().
	cutoff = now_datetime() - timedelta(days=_retention_days())
	rows = frappe.db.sql(
		f"""select name, recipient from {TABLE}
		    where ifnull(text, '') != '' and state != 'queued'
		      and (poll_complete = 1 or state != 'accepted')
		      and modified < %s
		    order by modified limit %s""",
		(cutoff, limit),
	)
	for name, recipient in rows:
		frappe.db.set_value(DOCTYPE, name, {"text": "", "recipient": mask_recipient(recipient)},
		                    update_modified=False)
	frappe.db.commit()
	return len(rows)


def dispatch_queued(limit: int = 50) -> int:
	"""Send due queued rows; returns how many dispatch runs were made."""
	if not enabled():
		return 0
	runs = 0
	for _ in range(limit):
		now = datetime.now(UTC)
		found = frappe.db.sql(
			f"""select name from {TABLE} where state = 'queued'
			    and (next_attempt_at is null or next_attempt_at <= %s)
			    order by creation limit 1 for update skip locked""",
			(_iso(now),),
		)
		if not found:
			break
		name = found[0][0]
		row = _row(frappe.get_doc(DOCTYPE, name))
		_run(lambda client: dispatch(client, row, now))
		_save(name, row)
		frappe.db.commit()
		runs += 1
	return runs


def poll_statuses(limit: int = 100) -> int:
	"""GET the OAN status of submitted rows whose poll is due; never resends."""
	if not enabled():
		return 0
	now = datetime.now(UTC)
	wait = _settings()["receipt_wait"]
	names = [r[0] for r in frappe.db.sql(
		f"""select name from {TABLE} where state = 'accepted' and poll_complete = 0
		    and oan_final = 0 and (next_poll_at is null or next_poll_at <= %s)
		    order by next_poll_at limit %s""",
		(_iso(now), limit),
	)]
	for name in names:
		if not frappe.db.sql(f"select name from {TABLE} where name = %s for update skip locked",
		                     (name,)):
			continue
		row = _row(frappe.get_doc(DOCTYPE, name))
		_run(lambda client: reconcile(client, row, now, receipt_wait=wait))
		_save(name, row)
		frappe.db.commit()
	return len(names)
