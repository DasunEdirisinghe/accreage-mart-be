"""Branded transactional email.

One layout, parameterised for the three flows that send mail in Epic 1:
welcome (buyer/seller), staff invite, and password reset. Rendered inline (no
external CSS / images) so it holds up across mail clients.
"""

import frappe

GREEN = "#1b5e3d"
AMBER = "#f59e0b"
INK = "#1f2937"
MUTED = "#6b7280"
BG = "#f4f2ec"


def send_branded_email(
	*,
	recipient: str,
	subject: str,
	heading: str,
	body_lines: list[str],
	cta_label: str,
	cta_url: str,
	footer_note: str = "",
):
	html = _render_html(heading, body_lines, cta_label, cta_url, footer_note)
	text = _render_text(heading, body_lines, cta_label, cta_url, footer_note)

	frappe.sendmail(
		recipients=[recipient],
		subject=subject,
		message=html,
		as_markdown=False,
		now=True,
		content=text,
	)


def smtp_configured() -> bool:
	if frappe.conf.get("mail_server") or frappe.conf.get("mail_login"):
		return True
	return bool(
		frappe.db.get_value("Email Account", {"default_outgoing": 1, "enable_outgoing": 1}, "name")
	)


def _render_html(heading, body_lines, cta_label, cta_url, footer_note) -> str:
	paragraphs = "".join(
		f'<p style="margin:0 0 16px;color:{INK};font-size:15px;line-height:1.6;">{line}</p>'
		for line in body_lines
	)
	footer = (
		f'<p style="margin:16px 0 0;color:{MUTED};font-size:12px;line-height:1.5;">{footer_note}</p>'
		if footer_note
		else ""
	)
	return f"""\
<div style="margin:0;padding:24px 0;background:{BG};font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;">
    <tr>
      <td style="background:{GREEN};padding:20px 28px;border-radius:12px 12px 0 0;">
        <span style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:-0.01em;">Accreage&nbsp;Mart</span>
      </td>
    </tr>
    <tr>
      <td style="background:#ffffff;padding:28px;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 12px 12px;">
        <h1 style="margin:0 0 16px;color:{INK};font-size:20px;">{heading}</h1>
        {paragraphs}
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:20px 0;">
          <tr>
            <td style="background:{AMBER};border-radius:8px;">
              <a href="{cta_url}" style="display:inline-block;padding:12px 22px;color:#1f2937;font-size:15px;font-weight:600;text-decoration:none;">{cta_label}</a>
            </td>
          </tr>
        </table>
        <p style="margin:0;color:{MUTED};font-size:13px;line-height:1.5;">
          If the button doesn't work, copy this link into your browser:<br>
          <a href="{cta_url}" style="color:{GREEN};word-break:break-all;">{cta_url}</a>
        </p>
        {footer}
      </td>
    </tr>
    <tr>
      <td style="padding:16px 28px;color:{MUTED};font-size:12px;">
        Accreage Mart — Sri Lanka's agricultural B2B marketplace
      </td>
    </tr>
  </table>
</div>"""


def _render_text(heading, body_lines, cta_label, cta_url, footer_note) -> str:
	parts = [heading, ""]
	parts += body_lines
	parts += ["", f"{cta_label}: {cta_url}"]
	if footer_note:
		parts += ["", footer_note]
	parts += ["", "Accreage Mart — Sri Lanka's agricultural B2B marketplace"]
	return "\n".join(parts)
