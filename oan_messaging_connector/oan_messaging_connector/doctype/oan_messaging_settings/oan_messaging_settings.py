import frappe
from frappe.model.document import Document


class OANMessagingSettings(Document):
	def validate(self):
		if self.enabled and not (self.base_url or "").startswith(("https://", "http://")):
			frappe.throw(frappe._("Base URL must start with https:// (http:// only for local trials)."))
		if self.max_attempts is not None and int(self.max_attempts) < 1:
			frappe.throw(frappe._("HTTP attempts must be at least 1."))
		if self.hook_expiry_minutes is not None:
			from oan_messaging_connector.sms_hook import MAX_EXPIRY_MINUTES

			ceiling = MAX_EXPIRY_MINUTES.get(self.hook_purpose or "transactional", 30)
			if not 1 <= int(self.hook_expiry_minutes) <= ceiling:
				frappe.throw(
					frappe._("Hook message expiry must be between 1 and {0} minutes for {1}.").format(
						ceiling, self.hook_purpose or "transactional"
					)
				)
