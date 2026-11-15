import frappe
from frappe.model.document import Document


class OANMessagingSettings(Document):
	def validate(self):
		if self.enabled and not (self.base_url or "").startswith(("https://", "http://")):
			frappe.throw(frappe._("Base URL must start with https:// (http:// only for local trials)."))
		if self.max_attempts is not None and int(self.max_attempts) < 1:
			frappe.throw(frappe._("HTTP attempts must be at least 1."))
