import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from olcr_api.db import Database
from olcr_api.agent_tools import ToolStore
from olcr_api import tool_form_chat as chat


class ToolFormChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'chat.db')
        self.db.initialize()
        self.settings = SimpleNamespace(permission_level='automatic', external_access_enabled=True)
        self.browser = Mock()
        self.identity = {'session_id': 'session-one', 'process_id': 123,
                         'url': 'https://forms.example/contact', 'origin': 'https://forms.example', 'document_id': 100.0}
        self.browser.list.return_value = [{'session_id': 'session-one', 'url': self.identity['url']}]
        self.browser.inspect_public_form.return_value = {'status': 'AVAILABLE', **self.identity,
            'fields': [{'label': 'Subject', 'selector': '#subject', 'tag': 'input', 'type': 'text'}]}
        self.browser.fill_public_form.return_value = {'status': 'AVAILABLE'}
        self.host = {'db': self.db, 'settings': self.settings, 'browser_sessions': self.browser}
        self.recipe = {'name': 'Normalize', 'description': 'Lowercase supplied text',
                       'parameters': [{'name': 'text', 'type': 'string', 'description': 'Text to lowercase'}],
                       'steps': [{'tool_id': 'lowercase', 'arguments_json': '{"text":"${inputs.text}"}'}]}

    def handle(self, text, state=None, generate=None, cid='chat-one'):
        return chat.handle(self.host, cid, text, state, 'Asia/Tokyo', generate or Mock(return_value={}))

    def test_recipe_create_review_serialization_confirm_list_disable(self):
        generation = Mock(return_value=self.recipe)
        result = self.handle('Create a reusable tool to lowercase text', generate=generation)
        self.assertEqual('review', result['state']['stage'])
        self.assertEqual([], ToolStore(self.db).list())
        self.assertNotIn('arguments_json', result['response'])
        self.assertTrue(result['details'])
        reviewed = json.loads(json.dumps(result['state']))
        saved = self.handle('承認', reviewed)
        self.assertIsNone(saved['state'])
        records = ToolStore(self.db).list()
        self.assertEqual(1, len(records))
        listed = self.handle('List saved tools')
        self.assertIn('Normalize', listed['details'][0])
        disable = self.handle('Disable tool Normalize')
        self.assertEqual(1, len(ToolStore(self.db).list()))
        self.handle('confirm', disable['state'])
        self.assertEqual([], ToolStore(self.db).list())

    def test_generic_tool_creation_collects_purpose_in_chat(self):
        generation = Mock(return_value=self.recipe)
        for text in ('Create tool', 'ツールを作って'):
            initial = self.handle(text, generate=generation)
            self.assertEqual('collecting', initial['state']['stage'])
            generation.assert_not_called()
            next_turn = self.handle('Lowercase user supplied text', initial['state'], generation)
            self.assertEqual('review', next_turn['state']['stage'])
            generation.reset_mock()

    def test_invalid_recipe_is_never_saved_and_cancel_does_not_save(self):
        invalid = {**self.recipe, 'steps': [{'tool_id': 'shell', 'arguments_json': '{}'}]}
        result = self.handle('Create tool to lowercase text', generate=Mock(return_value=invalid))
        self.assertIsNone(result['state'])
        draft = self.handle('Create tool to lowercase text', generate=Mock(return_value=self.recipe))
        self.handle('cancel', draft['state'])
        self.assertEqual([], ToolStore(self.db).list())

    def test_tool_run_missing_inputs_and_typed_extraction(self):
        ToolStore(self.db).save(self.recipe)
        generation = Mock(side_effect=[{'text': None}, {'text': 'HELLO'}])
        missing = self.handle('Run tool Normalize', generate=generation)
        self.assertEqual('collecting', missing['state']['stage'])
        review = self.handle('text: HELLO', missing['state'], generation)
        self.assertEqual({'text': 'HELLO'}, review['state']['payload']['arguments'])
        # Real existing safe executor, with Task persistence but no network access.
        result = self.handle('yes', review['state'])
        self.assertIn('hello', result['details'][0])
        self.assertIsNone(result['state'])

    def test_tool_args_cannot_be_invented_and_typed_boolean_is_not_number(self):
        record = ToolStore(self.db).save(self.recipe)
        result = self.handle('Run tool Normalize', generate=Mock(return_value={'text': 'invented'}))
        self.assertEqual('collecting', result['state']['stage'])
        self.assertNotIn('arguments', result['state']['payload'])
        self.recipe['parameters'] = [{'name': 'count', 'type': 'number'}]
        self.recipe['steps'] = [{'tool_id': 'sort_ascending', 'arguments_json': '{"items":"${inputs.count}"}'}]
        schema = chat._argument_schema(self.recipe)
        with self.assertRaises(ValueError):
            schema.model_validate({'count': True})

    def test_ambiguous_named_tools_require_unique_name_or_id(self):
        first = ToolStore(self.db).save(self.recipe)
        ToolStore(self.db).save(self.recipe)
        generation = Mock(return_value={'text': 'HELLO'})
        ambiguous = self.handle('Run tool Normalize with HELLO', generate=generation)
        generation.assert_not_called()
        self.assertEqual('collecting', ambiguous['state']['stage'])
        selected = self.handle(first['id'], ambiguous['state'], generation)
        self.assertEqual(first['id'], selected['state']['payload']['tool_id'])

    def test_tool_execution_keeps_existing_external_permission_guard(self):
        external = {'name': 'Lookup', 'description': 'Lookup a location',
            'parameters': [{'name': 'location', 'type': 'string'}],
            'steps': [{'tool_id': 'knowledge.wikidata', 'arguments_json': '{"location":"${inputs.location}"}'}]}
        ToolStore(self.db).save(external)
        draft = self.handle('Run tool Lookup for Kyoto', generate=Mock(return_value={'location': 'Kyoto'}))
        self.settings.external_access_enabled = False
        with patch('olcr_api.external_tools.execute') as execute:
            result = self.handle('confirm', draft['state'])
        execute.assert_not_called()
        self.assertIn('許可', result['response'])
        self.assertNotIn('実行しました', result['response'])

    def test_form_draft_and_exact_original_confirmation_after_serialization(self):
        model = Mock(return_value={'fields': [{'label': 'Subject', 'value': 'Hello'}]})
        result = self.handle('Fill form https://forms.example/contact Subject: Hello', generate=model)
        self.browser.open.assert_not_called()
        self.browser.fill_public_form.assert_not_called()
        self.assertEqual('review', result['state']['stage'])
        restored = json.loads(json.dumps(result['state']))
        original = restored['payload']['review']
        result = self.handle('承認', restored)
        self.assertIn('1項目', result['response'])
        self.browser.fill_public_form.assert_called_once()
        call = self.browser.fill_public_form.call_args
        self.assertEqual(original['session'], call.args[1])
        self.assertEqual(original['fields'], call.args[2])
        # BrowserForms independently rejects replay even outside hub idempotency.
        replay = self.handle('confirm', restored)
        self.assertNotIn('入力しました', replay['response'])
        self.assertEqual(1, self.browser.fill_public_form.call_count)

    def test_form_collects_missing_values_and_ambiguous_session_in_chat(self):
        self.browser.list.return_value.append({'session_id': 'session-two', 'url': 'https://forms.example/other'})
        model = Mock(return_value={'fields': []})
        result = self.handle('Fill form Subject: Hello', generate=model)
        model.assert_not_called()
        self.assertEqual('collecting', result['state']['stage'])
        picked = self.handle('session-one', result['state'], model)
        self.assertIn('Subject', picked['details'])
        filled = self.handle('Subject: Hello', picked['state'], Mock(return_value={'fields': [{'label': 'Subject', 'value': 'Hello'}]}))
        self.assertEqual('review', filled['state']['stage'])
        self.browser.fill_public_form.assert_not_called()

    def test_unknown_explicit_session_and_multiple_urls_never_fallback(self):
        model = Mock()
        result = self.handle('Fill form session_id: missing Subject: Hello', generate=model)
        self.assertEqual('collecting', result['state']['stage'])
        model.assert_not_called()
        result = self.handle('Fill form https://a.example/form https://b.example/form Subject: Hello', generate=model)
        self.assertEqual('collecting', result['state']['stage'])
        self.browser.open.assert_not_called()

    def test_form_open_only_literal_public_url_after_permission(self):
        self.browser.list.return_value = []
        self.browser.open.return_value = {'status': 'READY', 'session_id': 'session-one'}
        self.browser.navigate.return_value = {'status': 'AVAILABLE'}
        model = Mock(return_value={'fields': [{'label': 'Subject', 'value': 'Hello'}]})
        self.settings.external_access_enabled = False
        self.handle('Fill form https://forms.example/contact Subject: Hello', generate=model)
        self.browser.open.assert_not_called()
        model.assert_not_called()
        self.settings.external_access_enabled = True
        result = self.handle('Fill form https://forms.example/contact Subject: Hello', generate=model)
        self.assertEqual('review', result['state']['stage'])
        self.browser.navigate.assert_called_once_with('session-one', self.identity['url'])

    def test_form_guesses_auth_payment_and_values_not_in_user_text_are_blocked(self):
        model = Mock(return_value={'fields': [{'label': 'Subject', 'value': 'Invented'}]})
        result = self.handle('Fill form Subject: Hello', generate=model)
        self.assertIsNone(result['state'])
        self.browser.fill_public_form.assert_not_called()
        for url in ('https://forms.example/login', 'https://forms.example/checkout', 'http://127.0.0.1/form'):
            self.handle('Fill form ' + url + ' Subject: Hello', generate=model)
        self.browser.open.assert_not_called()
        self.browser.navigate.assert_not_called()

    def test_form_review_rejects_mutation_expiry_changed_page_and_permission_revocation(self):
        model = Mock(return_value={'fields': [{'label': 'Subject', 'value': 'Hello'}]})
        def draft():
            return self.handle('Fill form Subject: Hello', generate=model)['state']
        altered = draft()
        altered['payload']['review']['fields'][0]['value'] = 'Changed'
        self.handle('confirm', altered)
        expired = draft()
        with self.db.connect() as connection:
            connection.execute('UPDATE browser_form_reviews SET expires_at=0')
        self.handle('confirm', expired)
        changed = draft()
        self.browser.inspect_public_form.return_value['process_id'] = 124
        self.handle('confirm', changed)
        self.browser.inspect_public_form.return_value['process_id'] = 123
        revoked = draft()
        self.settings.external_access_enabled = False
        self.handle('confirm', revoked)
        self.browser.fill_public_form.assert_not_called()

    def test_form_cancel_and_amendment_consume_previous_review(self):
        model = Mock(return_value={'fields': [{'label': 'Subject', 'value': 'Hello'}]})
        first = self.handle('Fill form Subject: Hello', generate=model)['state']
        self.handle('cancel', first)
        self.handle('confirm', first)
        first = self.handle('Fill form Subject: Hello', generate=model)['state']
        second = self.handle('Change Subject to Bye', first, Mock(return_value={'fields': [{'label': 'Subject', 'value': 'Bye'}]}))
        self.assertEqual('review', second['state']['stage'])
        self.handle('confirm', first)
        self.browser.fill_public_form.assert_not_called()
        self.handle('confirm', second['state'])
        self.browser.fill_public_form.assert_called_once()

    def test_cross_chat_and_unvalidated_stale_command_cannot_confirm(self):
        draft = self.handle('Create tool to lowercase text', generate=Mock(return_value=self.recipe))['state']
        self.handle('confirm', draft, cid='chat-two')
        self.handle('/workflow stale-id confirm', draft)
        self.handle('confirm')
        self.assertEqual([], ToolStore(self.db).list())

    def test_explicit_topic_change_escapes_pending_draft(self):
        state = chat._state('chat-one', 'form_fill', source='Fill form')
        for text in ('What is a transformer?', '東京の天気は？', 'Translate hello',
                     'コードを書いて', 'Plan a trip to Kyoto', '旅行の計画を立てて'):
            self.assertFalse(chat.matches(text, state), text)
        for text in ('Subject: Why?', 'Hello world', 'session-one', 'confirm', 'cancel'):
            self.assertTrue(chat.matches(text, state), text)

    def test_intent_matches_only_operational_tools_forms(self):
        for text in ('List tools', 'Create reusable tool to lowercase text', 'Run tool Normalize', 'ツールを作って', 'フォームに入力して'):
            self.assertTrue(chat.matches(text), text)
        for text in ('Hello', 'What is a tool?', 'How do I create a tool?', 'Translate "create tool"', 'コードを書いてツールを作って', 'Explain web forms'):
            self.assertFalse(chat.matches(text), text)
