import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.config import settings

class EmailService:
    def _send_email_sync(self, to_email: str, subject: str, body: str, sender_email: str, sender_password: str, smtp_server: str, smtp_port: int):
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        smtp_email: str = None,
        smtp_password: str = None,
        smtp_server: str = "smtp.gmail.com",
        smtp_port: int = 587
    ) -> dict:
        sender_email = smtp_email or getattr(settings, "GMAIL_SENDER_EMAIL", "")
        sender_password = smtp_password or getattr(settings, "GMAIL_APP_PASSWORD", "")

        if not sender_email or not sender_password:
            return {"success": False, "message": "Gmail App Password or Sender Email missing."}

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._send_email_sync,
                to_email,
                subject,
                body,
                sender_email,
                sender_password,
                smtp_server,
                smtp_port
            )
            return {"success": True, "message": f"Email successfully sent to {to_email}"}
        except Exception as e:
            print(f"Gmail SMTP Error: {e}")
            return {"success": False, "message": str(e)}

email_service = EmailService()
