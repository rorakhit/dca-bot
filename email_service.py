"""
email_service.py — Resend email helper + error email template.
"""

import resend

from config import EMAIL_FROM, NOTIFY_EMAIL, log


def _send_email(subject: str, html_body: str):
    """Send email via Resend HTTP API. Raises on failure."""
    resend.Emails.send({
        "from":    EMAIL_FROM,
        "to":      [NOTIFY_EMAIL],
        "subject": subject,
        "html":    html_body,
    })


def send_error_email(context: str, error: Exception):
    """
    Send a plain-text error notification so failures don't go unnoticed.
    Swallows its own exceptions so a broken SMTP config doesn't cause a double-fault.
    """
    try:
        html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,sans-serif;padding:24px;color:#111827">
  <div style="max-width:520px;background:#fef2f2;border:1px solid #fecaca;
              border-radius:12px;padding:24px">
    <h2 style="color:#dc2626;margin:0 0 12px">⚠️ DCA Bot Error</h2>
    <p style="margin:0 0 8px"><strong>Context:</strong> {context}</p>
    <pre style="background:#fff;border-radius:8px;padding:12px;font-size:12px;
                overflow-x:auto;white-space:pre-wrap">{type(error).__name__}: {error}</pre>
    <p style="font-size:12px;color:#6b7280;margin:12px 0 0">
      Check <code>logs/dca_bot.log</code> for the full traceback.
    </p>
  </div>
</body></html>"""
        _send_email("⚠️ DCA Bot — Error notification", html)
        log.info(f"Error notification sent for: {context}")
    except Exception as e:
        log.error(f"Could not send error email: {e}")
