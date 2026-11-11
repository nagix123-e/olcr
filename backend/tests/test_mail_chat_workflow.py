import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from olcr_api.assistant_workflows import AssistantWorkflows
from olcr_api.config import Settings
from olcr_api.db import Database
from olcr_api.mail_message import DISCLOSURE_FOOTER
from olcr_api.service_connectors import ServiceConnectors

REQUEST = 'friend@example.org 宛てに、私のアップルのメアドから、京都での旅行の予定をどうするか尋ねるメールを出して'


class MailChatWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'isolated.db'); self.db.initialize()
        for cid in ('a', 'b'): self.db.create_conversation(cid, 1, cid, None)
        self.settings = Settings(external_access_enabled=True, permission_level='automatic')
        self.apple = Mock(); self.apple.status.return_value = {'state': 'connected'}
        self.apple.execute.return_value = {'sent': True, 'provider': 'apple_mail'}
        self.google = Mock(); self.google.status.return_value = {'state': 'connected'}
        self.service = ServiceConnectors(self.db, lambda: self.settings, self.google, apple_mail=self.apple)
        self.service.set_category('mail', True)
        self.service.policy('service.apple.mail', {'enabled': True})
        self.context = {'db': self.db, 'settings': self.settings, 'service_connectors': self.service}
        self.generate = Mock(return_value={'subject': '京都旅行の予定について', 'body': '京都旅行の予定をどうするか、希望を教えてください。'})
        self.hub = AssistantWorkflows(lambda: self.context, self.generate); self.n = 0

    def turn(self, text, cid='a', mid=None):
        self.n += 1
        value = SimpleNamespace(message=text, conversation_id=cid, message_id=mid or str(self.n))
        return self.hub.respond(value, lambda v: (cid, '', self.db.conversation(cid)))

    def command(self, result):
        return '/workflow ' + result['blocks'][0]['id'] + ' confirm'

    def test_screenshot_request_prepares_exact_mail_review_and_single_send(self):
        draft = self.turn(REQUEST, mid='original')
        self.assertIsNotNone(draft)
        block = draft['blocks'][0]
        self.assertEqual((block['domain'], block['status']), ('mail', 'review'))
        details = '\n'.join(block['details'])
        for text in ('friend@example.org', '京都旅行', DISCLOSURE_FOOTER, '既定'):
            self.assertIn(text, details)
        self.apple.execute.assert_not_called()
        self.assertEqual(self.turn(REQUEST, mid='original'), draft)
        self.hub = AssistantWorkflows(lambda: self.context, self.generate)
        done = self.turn(self.command(draft), mid='approved')
        self.assertIn('Apple Mailへの送信依頼を受け付けました', done['response'])
        self.apple.execute.assert_called_once()
        action, payload = self.apple.execute.call_args.args
        self.assertEqual(action, 'send'); self.assertEqual(payload['to'], ['friend@example.org'])
        self.assertTrue(payload['body'].endswith(DISCLOSURE_FOOTER))
        self.assertEqual(self.turn(self.command(draft), mid='approved'), done)
        self.turn(self.command(draft)); self.assertEqual(self.apple.execute.call_count, 1)

    def test_missing_recipient_is_clarified_and_original_request_retained(self):
        pending = self.turn('Apple Mailから京都の旅行の予定を尋ねるメールを送って')
        self.assertIsNotNone(pending); self.assertIn('宛先', pending['response'])
        self.generate.assert_not_called()
        draft = self.turn('friend@example.org 宛てで')
        self.assertEqual(draft['blocks'][0]['status'], 'review')
        self.assertIn('京都', json.dumps(self.generate.call_args.args[1], ensure_ascii=False))
        self.apple.execute.assert_not_called()

    def test_provider_ambiguity_requires_choice_and_never_falls_back(self):
        self.service.policy('service.google.gmail', {'enabled': True})
        pending = self.turn('friend@example.org に旅行のメールを送って')
        self.assertIsNotNone(pending); self.assertIn('サービス', pending['response'])
        self.generate.assert_not_called()
        draft = self.turn('Apple Mailで')
        self.assertEqual(draft['blocks'][0]['status'], 'review')
        self.service.policy('service.apple.mail', {'enabled': False})
        failed = self.turn(self.command(draft))
        self.assertNotIn('送信しました', failed['response'])
        self.apple.execute.assert_not_called(); self.google.execute.assert_not_called()

    def test_changed_cross_conversation_and_expired_approvals_cannot_send(self):
        draft = self.turn(REQUEST)
        self.assertIsNotNone(draft)
        self.turn(self.command(draft), cid='b')
        revised = self.turn('件名を「旅行の相談」に変えて')
        self.assertNotEqual(draft['blocks'][0]['id'], revised['blocks'][0]['id'])
        self.turn(self.command(draft)); self.apple.execute.assert_not_called()
        with self.db.connect() as con:
            row = con.execute('SELECT state_json FROM assistant_workflow_turns WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', ('a',)).fetchone()
            state = json.loads(row[0]); state['expires_at'] = 1
            con.execute('UPDATE assistant_workflow_turns SET state_json=? WHERE conversation_id=?', (json.dumps(state), 'a'))
        self.assertIn('期限', self.turn(self.command(revised))['response'])
        self.apple.execute.assert_not_called()

    def test_unknown_outcome_is_not_reported_as_sent_or_retried(self):
        draft = self.turn(REQUEST); self.assertIsNotNone(draft)
        self.apple.execute.side_effect = ValueError('APPLE_MAIL_REQUEST_TIMEOUT')
        failed = self.turn(self.command(draft))
        self.assertIn('送信済み', failed['response']); self.assertNotIn('送信しました', failed['response'])
        self.turn(self.command(draft)); self.assertEqual(self.apple.execute.call_count, 1)

    def test_apple_mail_timeout_says_result_is_unknown_not_server_success(self):
        draft = self.turn(REQUEST); self.assertIsNotNone(draft)
        self.apple.execute.side_effect = ValueError('APPLE_MAIL_REQUEST_TIMEOUT')
        failed = self.turn(self.command(draft))
        block=failed['blocks'][0]
        self.assertEqual(block['status'],'error')
        self.assertEqual(block['title'],'送信結果を確認してください')
        self.assertTrue(any('時間内に応答しませんでした' in item for item in block['details']))
        self.assertIn('送信済み',failed['response'])
        self.assertNotIn('送信しました',failed['response'])
        with self.db.connect() as con:
            row=con.execute('SELECT status FROM assistant_workflow_approvals WHERE conversation_id=?',( 'a',)).fetchone()
        self.assertEqual(row[0],'uncertain')

    def test_review_payload_passes_real_apple_connector_validation(self):
        from olcr_api.apple_mail import AppleMailConnector
        native = Mock(return_value={'sent': True})
        self.service.apple_mail = AppleMailConnector(runner=native)
        draft = self.turn(REQUEST)
        done = self.turn(self.command(draft))
        self.assertIn('Apple Mailへの送信依頼を受け付けました', done['response'])
        native.assert_called_once()
        request = native.call_args.args[0]
        self.assertEqual((request['operation'], request['to']), ('send', ['friend@example.org']))
        self.assertEqual(request['body'].count(DISCLOSURE_FOOTER), 1)

    def test_mail_operations_do_not_become_help_or_calendar_operations(self):
        from olcr_api.operation_help import requested
        from olcr_api.assistant_actions import action_intent
        self.assertFalse(requested(REQUEST))
        self.assertIsNone(action_intent(REQUEST))
        dated = 'friend@example.org にApple Mailで明日の会議の予定を確認するメールを送って'
        self.assertIsNone(action_intent(dated))
        question_body = 'Apple Mailでfriend@example.orgに、明日の会議をどうすればいいか尋ねるメールを送って'
        self.assertIsNone(action_intent(question_body))
        self.assertFalse(requested(question_body))
        for text in ('Apple Mailでメールを送る方法は？', 'メール送信機能を実装して', '明日の予定を見せて'):
            self.assertIsNone(self.turn(text), text)
        self.generate.assert_not_called()

    def test_explicit_sender_and_cc_are_not_silently_ignored(self):
        for text in ('friend@example.org 宛てにApple Mailでsender@example.netからメールを送って',
                     'Apple Mailでfriend@example.orgに、送信元はsender@example.netでメールを送って',
                     'Apple Mailでfriend@example.orgに From: sender@example.net でメールを送って',
                     'Apple Mailでfriend@example.orgにCC copy@example.netでメールを送って'):
            result = self.turn(text)
            self.assertIsNotNone(result)
            self.assertNotEqual(result['blocks'][0]['status'], 'review')
        self.apple.execute.assert_not_called()

    def test_cc_country_domain_is_a_recipient_not_a_copy_instruction(self):
        draft = self.turn('Apple Mailでfriend@example.ccにメールを送って')
        self.assertIsNotNone(draft)
        self.assertEqual(draft['blocks'][0]['status'], 'review')
        self.assertEqual(self.hub.state('a')['payload']['to'], ['friend@example.cc'])

    def test_body_and_subject_addresses_do_not_change_recipients_or_provider(self):
        self.turn(REQUEST)
        for text in ('件名を support@example.org に変更して', '本文に support@example.org に連絡してくださいと追加して'):
            revised = self.turn(text)
            state = self.hub.state('a')
            self.assertEqual(state['payload']['to'], ['friend@example.org'])
            self.assertEqual(revised['blocks'][0]['status'], 'review')
        draft = self.turn('Gmailでfriend@example.orgにApple Mailの設定についてメールを送って')
        self.assertEqual(self.hub.state('a')['provider'], 'service.google.gmail')
        self.apple.execute.assert_not_called()

    def test_named_service_never_falls_back_to_another_connected_service(self):
        for text in ('Send an email to friend@example.org with Gmail',
                     'Gmailのアカウントでfriend@example.orgにメールを送って'):
            with self.subTest(text=text):
                self.turn(text)
                self.assertEqual(self.hub.state('a')['provider'], 'service.google.gmail')
        self.apple.execute.assert_not_called()

    def test_mixed_content_and_recipient_edit_changes_only_explicit_recipient(self):
        self.turn(REQUEST)
        draft = self.turn('本文はそのままで、宛先を other@example.org に変更して')
        self.assertEqual(draft['blocks'][0]['status'], 'review')
        self.assertEqual(self.hub.state('a')['payload']['to'], ['other@example.org'])

    def test_multiple_explicit_recipients_are_retained(self):
        self.turn('Apple Mailでfriend@example.org,other@example.net宛てにメールを送って')
        self.assertEqual(self.hub.state('a')['payload']['to'], ['friend@example.org', 'other@example.net'])

    def test_conflicting_provider_names_are_clarified(self):
        result = self.turn('friend@example.orgにApple MailとGmailでメールを送って')
        self.assertIsNotNone(result)
        self.assertEqual(result['blocks'][0]['status'], 'collecting')
        self.generate.assert_not_called(); self.apple.execute.assert_not_called()

    def test_requested_scheduling_or_attachments_are_not_lost_in_an_immediate_send(self):
        for text in ('明日9時にfriend@example.org宛てにApple Mailでメールを送って',
                     'Apple Mailでfriend@example.orgに明日9時にメールを送って',
                     'friend@example.orgにApple Mailでファイルを添付したメールを送って'):
            result = self.turn(text)
            self.assertIsNotNone(result)
            self.assertEqual(result['blocks'][0]['status'], 'collecting')
        self.generate.assert_not_called(); self.apple.execute.assert_not_called()
