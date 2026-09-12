// Story 3.1: shows which other Category docs already share the selected commodity,
// so staff can see the fan-out before linking another one.

frappe.ui.form.on("Category", {
	refresh: render_linked_categories,
	commodity: render_linked_categories,
});

function render_linked_categories(frm) {
	if (!frm.doc.commodity) {
		frm.set_df_property("linked_categories_html", "options", "");
		return;
	}

	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "Category",
			filters: {
				commodity: frm.doc.commodity,
				name: ["!=", frm.doc.name || ""],
			},
			fields: ["title"],
			limit_page_length: 0,
		},
		callback: function (r) {
			const rows = r.message || [];
			if (!rows.length) {
				frm.set_df_property(
					"linked_categories_html",
					"options",
					'<div class="text-muted">No other category uses this commodity yet.</div>'
				);
				return;
			}
			const items = rows
				.map((row) => `<li>${frappe.utils.escape_html(row.title)}</li>`)
				.join("");
			frm.set_df_property(
				"linked_categories_html",
				"options",
				`<div><strong>Also used by:</strong><ul>${items}</ul></div>`
			);
		},
	});
}
