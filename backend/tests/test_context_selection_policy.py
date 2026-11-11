import json
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from olcr_api.config import Settings
from olcr_api.context_policy import compact_json, context_budget, fit_messages, provider_answer_rules, select_text, select_turns
from olcr_api.runtime import ContextManager


class ContextSelectionPolicyTests(unittest.TestCase):
    def test_recent_unrelated_turn_cannot_displace_older_relevant_turn(self):
        messages = [
            {'role': 'user', 'content': 'Explain NOAA geomagnetic Kp observations'},
            {'role': 'assistant', 'content': 'NOAA geomagnetic Kp observations use three-hour intervals.'},
            {'role': 'user', 'content': 'Compare CPI inflation rates'},
            {'role': 'assistant', 'content': 'CPI inflation rates are economic indicators.'},
        ]
        selected = select_turns('NOAA geomagnetic Kp observations', messages, 2000)
        self.assertIn('three-hour', selected)
        self.assertNotIn('CPI', selected)

    def test_short_context_is_filtered_even_when_it_already_fits(self):
        _, selected = ContextManager(8000).build('Explain NOAA geomagnetic observations', [])
        self.assertEqual([], selected)
        messages, _ = ContextManager(8000).build('Explain NOAA geomagnetic observations', [],
            'NOAA geomagnetic observations use Kp.\n\nCPI inflation is 2.5 percent.')
        context = '\n'.join(message['content'] for message in messages)
        self.assertIn('NOAA', context)
        self.assertNotIn('2.5', context)

    def test_explicit_followup_preserves_one_previous_turn(self):
        selected = select_turns('Continue', [
            {'role': 'user', 'content': 'Explain the satellite orbit'},
            {'role': 'assistant', 'content': 'The satellite orbit has a 90-minute period.'},
        ], 1000)
        self.assertIn('90-minute', selected)

    def test_attached_screenshot_followup_keeps_the_previous_project_turn(self):
        selected = select_turns('スクショを見ればわかるように、そもそもゲームが開始しない', [
            {'role': 'user', 'content': 'ReactでテトリスのWebゲームを作る'},
            {'role': 'assistant', 'content': '実装方針を記録しました。'},
        ], 1000)
        self.assertIn('テトリス', selected)

    def test_model_budget_and_validation(self):
        settings = Settings(model_context_budgets={'mlx:small': 1024, 'openai:large': 20000})
        self.assertEqual(1024, context_budget(settings, 'mlx:small'))
        self.assertEqual(8000, context_budget(settings, 'another'))
        with self.assertRaises(ValueError):
            Settings(model_context_budgets={'x': 0}).validated()
        with self.assertRaises(ValueError):
            Settings(rag_min_relevance=float('nan')).validated()

    def test_provider_json_is_valid_bounded_and_repacked_for_smaller_fallback(self):
        evidence = {'items': [{'date': '2025', 'value': n, 'text': 'observation ' * 200} for n in range(30)]}
        packet, cut = compact_json(evidence, 2000)
        self.assertTrue(cut)
        self.assertLessEqual(len(packet), 2000)
        self.assertTrue(json.loads(packet)['evidence_truncated'])
        messages = [{'role': 'system', 'content': 'Use only evidence; obey exact output format.'},
                    {'role': 'user', 'content': 'Compare observations\n[TOOL_RESULT_UNTRUSTED]\n' + json.dumps(evidence)}]
        large = fit_messages(messages, 'Compare observations', 8000)
        small = fit_messages(large, 'Compare observations', 1000)
        self.assertEqual('Compare observations', small[-1]['content'])
        self.assertEqual(messages[0], small[0])
        packed = next(row['content'].split('\n', 1)[1] for row in small if row['content'].startswith('[PROVIDER_EVIDENCE_JSON]'))
        self.assertLessEqual(len(packed), 1000)
        self.assertTrue(json.loads(packed)['evidence_truncated'])

    def test_threshold_can_reduce_recall_without_padding(self):
        text = 'satellite observation.\n\nGDP finance unemployment.'
        broad = select_text('satellite orbit observation location', text, 1000, .2)
        strict = select_text('satellite orbit observation location', text, 1000, .95)
        self.assertIn('satellite', broad)
        self.assertEqual('', strict)
        self.assertLessEqual(len(select_text('satellite', text, 2, .1)), 2)

    def test_pdf_container_bytes_cannot_be_injected_as_web_text(self):
        from olcr_api.web import fetch
        headers = Message(); headers['Content-Type'] = 'application/pdf'
        response = SimpleNamespace(headers=headers, read=lambda n: b'%PDF-1.7\n<</Type/Catalog>>\nstream\x00\xff')
        opened = MagicMock(); opened.__enter__.return_value = response
        opener = SimpleNamespace(open=lambda *args, **kwargs: opened)
        with patch('olcr_api.web.validate_url'), patch('olcr_api.web._safe_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, 'WEB_DOCUMENT_TEXT_UNAVAILABLE'):
                fetch('https://example.invalid/report.pdf')

    def test_kp_request_does_not_load_unrelated_cpi_or_medical_rules(self):
        rules = provider_answer_rules('NOAA Kpの変化を説明して', 'space.space_weather')
        self.assertNotIn('CPI', rules)
        self.assertNotIn('PDB', rules)
        self.assertNotIn('serious code', rules)
        self.assertIn('CPI index levels', provider_answer_rules('インフレ率を比較して', 'econ.worldbank'))

    def test_compaction_preserves_each_provider_dimension(self):
        providers = [{'tool_id': name, 'data': {'items': [{'value': n, 'text': 'long observation ' * 80} for n in range(12)]}}
                     for name in ('first', 'second', 'third')]
        packet, cut = compact_json(providers, 1600)
        self.assertTrue(cut)
        self.assertLessEqual(len(packet), 1600)
        self.assertEqual({'first', 'second', 'third'}, {row['tool_id'] for row in json.loads(packet)['results']})
