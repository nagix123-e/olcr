"""Validation and mandatory disclosure for messages sent through OLCR."""
from __future__ import annotations

from email.headerregistry import Address
from email.message import EmailMessage
from email import policy
from email.utils import getaddresses
import base64

DISCLOSURE_FOOTER = 'このメールはAIエージェントによって送信されています。'
MAX_MESSAGE_CHARS = 200_000
MAX_RECIPIENTS = 50


def _recipients(value, field: str) -> list[str]:
    if value is None and field != 'to':
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError('MAIL_RECIPIENTS_INVALID')
    if not values or len(values) > MAX_RECIPIENTS:
        raise ValueError('MAIL_RECIPIENTS_INVALID')
    result = []
    for item in values:
        if not isinstance(item, str) or not item.strip() or len(item) > 500 or any(ord(char) < 32 or ord(char) in {127, 0x2028, 0x2029} for char in item):
            raise ValueError('MAIL_RECIPIENTS_INVALID')
        parsed = getaddresses([item.strip()])
        if not parsed or len(parsed) + len(result) > MAX_RECIPIENTS:
            raise ValueError('MAIL_RECIPIENTS_INVALID')
        for name, address in parsed:
            if not address or address.count('@') != 1:
                raise ValueError('MAIL_RECIPIENTS_INVALID')
            try:
                rendered = str(Address(display_name=name, addr_spec=address))
            except (TypeError, ValueError):
                raise ValueError('MAIL_RECIPIENTS_INVALID') from None
            result.append(rendered)
    return result


def prepare_mail(arguments: dict) -> dict:
    """Normalize a plain-text email and always put OLCR's disclosure last."""
    if not isinstance(arguments, dict) or set(arguments) - {'to', 'cc', 'bcc', 'subject', 'body'}:
        raise ValueError('MAIL_ARGUMENTS_INVALID')
    to = _recipients(arguments.get('to'), 'to')
    cc = _recipients(arguments.get('cc'), 'cc')
    bcc = _recipients(arguments.get('bcc'), 'bcc')
    if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
        raise ValueError('MAIL_RECIPIENTS_INVALID')
    subject = arguments.get('subject')
    body = arguments.get('body')
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 998 or any(ord(char) < 32 or ord(char) in {127, 0x2028, 0x2029} for char in subject):
        raise ValueError('MAIL_SUBJECT_INVALID')
    if not isinstance(body, str) or len(body) > MAX_MESSAGE_CHARS or any(ord(char) < 32 and char not in '\r\n\t' or ord(char) in {127, 0x2028, 0x2029} for char in body):
        raise ValueError('MAIL_BODY_INVALID')
    body = body.replace('\r\n', '\n').replace('\r', '\n').rstrip()
    if body.endswith(DISCLOSURE_FOOTER):
        body = body[:-len(DISCLOSURE_FOOTER)].rstrip()
    body = f'{body}\n\n{DISCLOSURE_FOOTER}' if body else DISCLOSURE_FOOTER
    return {'to': to, 'cc': cc, 'bcc': bcc, 'subject': subject.strip(), 'body': body,
            'recipient_count': len(to) + len(cc) + len(bcc)}


def gmail_raw_message(mail: dict) -> str:
    message = EmailMessage(policy=policy.SMTP)
    message['To'] = ', '.join(mail['to'])
    if mail['cc']:
        message['Cc'] = ', '.join(mail['cc'])
    if mail['bcc']:
        message['Bcc'] = ', '.join(mail['bcc'])
    message['Subject'] = mail['subject']
    message.set_content(mail['body'], charset='utf-8')
    return base64.urlsafe_b64encode(message.as_bytes()).decode('ascii').rstrip('=')
