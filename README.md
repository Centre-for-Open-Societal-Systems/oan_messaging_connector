# OAN Messaging Connector (Frappe app)

A reusable Frappe app that sends SMS from any Frappe service (A2C Credit, Grievance, DA,
Auth, ...) through the **OAN Messaging Service** (contract 3.0.0). It owns the parts every
caller would otherwise rebuild:

* the **OAN Message** outbox: one row per intended message, with a random UUID4
  `delivery_id` used as the Idempotency-Key and never changed;
* dispatch **after commit**, plus a one-minute safety sweep (`FOR UPDATE SKIP LOCKED`,
  one commit per row, so it is safe with several workers);
* status polling into a separate field. 202/200 shows as *Submitted*, never "Sent", and
  `unknown` is flagged and never resubmitted;
* the one-SMS-part length check: 160 GSM-7 characters, or 70 if any Amharic character;
* a daily **purge**: finished rows keep only delivery metadata after a retention period
  (default 7 days); the text is cleared and the recipient masked;
* **OAN Messaging Settings**: base URL, caller token (Password), CA bundle, retention.

Status: **trial build, not released**. Licensed under the MIT License, the same as other
OAN services (see `LICENSE`).

## Use it from a caller app

1. Add it to the caller app's `hooks.py`:
   `required_apps = [..., "oan_messaging_connector"]`.
2. At each business event, inside the transaction, call:

   ```python
   from oan_messaging_connector.api import queue_sms

   queue_sms(recipient=doc.phone_number, text=text, purpose="transactional",
             source_reference=f"<app>:{doc.name}:<event>",
             reference_doctype=doc.doctype, reference_name=doc.name, event="<event>")
   ```

   * `purpose` is `transactional` or `alert`. `expires_at`, when given, must be
     timezone-aware.
   * **OTPs are refused.** The outbox stores the text, and an OTP must never be stored.
     OTP senders send at once with the OAN client instead.
   * `queue_sms` never sends inline and never raises: if the connector is disabled, the
     message is invalid, or settings, the database or the queue fail, it returns `None`
     (or the row name, if only the enqueue failed; the sweep then sends it) and writes
     an Error Log entry.
3. Keep the texts and event mapping in the caller app. See A2C's `oan_a2c/oan_messaging/`
   for the pattern: `events.py` holds the templates and mapping, `notify.py` the call
   sites.

## Configure a site

The OAN operator registers the caller and issues its token:

```
python -m oan_messaging.runtime caller --caller a2c --channels sms --purposes transactional
python -m oan_messaging.runtime issue-credential --caller a2c
```

Then fill in **OAN Messaging Settings** (Enabled, Base URL, Token), or provide these
through `site_config.json`, which overrides the doctype (useful for secrets injected by
Kubernetes):

```
bench --site <site> set-config oan_messaging_enabled 1
bench --site <site> set-config oan_messaging_base_url https://oan-messaging.<env>
bench --site <site> set-config oan_messaging_token <token from the OAN operator>
bench --site <site> set-config oan_messaging_ca_bundle /path/to/ca.pem   # only for a private CA
```

The scheduler and a worker for the `short` queue must be running:
`bench --site <site> enable-scheduler` and `bench worker --queue short,default`.

Retention: **Clear Message Text After (days)** in the settings (default 7), or
`oan_messaging_content_retention_days` in `site_config.json`.

## Operate

* Desk, **OAN Message** list: filter **Needs Attention** for messages rejected by OAN,
  reconciliation cases and `unknown` outcomes.
* `oan_messaging_connector.api.messages_for(doctype, name)` returns the delivery state of
  one business record, for support screens.
* Rows cannot be deleted; they are the delivery audit trail. After the retention period
  they keep only metadata (status, ids, masked recipient, times).

## Tests

```
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app oan_messaging_connector
```

The tests use an in-process fake OAN API and need no network.
