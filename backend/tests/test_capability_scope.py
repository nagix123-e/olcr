import hashlib
from pathlib import Path
import unittest

from olcr_api.capability_scope import parse_prompt_blocks
from olcr_api.coding_tasks import normalize_coding_requirements


FIXTURE = Path(__file__).parent / 'fixtures' / 'animated_website_19691.txt'


class CapabilityScopeTests(unittest.TestCase):
    def test_polarity_and_semantic_scopes(self):
        cases = [
            ('The button must not:\n- call a backend\n- create an API', [], ['backend']),
            ('MVP Exclusions\n\nDo NOT implement:\nbackend\ndatabase\n\nImplementation Order\n\nResolve:\n- target frontend stack', [], ['backend', 'database']),
            ('Show the APIs integrated into OLCR.', [], []),
            ('Implement a backend REST API.', ['backend'], []),
            ('Add a server endpoint.', ['backend'], []),
            ('Add a project-local dependency.', ['dependencies'], []),
            ('If Anime.js MCP is unavailable, report BLOCKED.', [], []),
            ('If Anime.js v3 is incompatible with v4 guidance, stop.', [], []),
            ('Use Anime.js v4.\nDo not add unnecessary dependencies.', ['dependencies'], []),
            ('Do not add any dependencies.', [], ['dependencies']),
            ('Implement the React page.\nDo not modify unrelated frontend files.', ['frontend'], []),
            ('Do not modify frontend code.', [], ['frontend']),
            ('Use React and Anime.js.\nDo not introduce GSAP or Three.js.', ['dependencies', 'frontend'], []),
            ('FailureHandling\n\nDo not modify backend. Use React.', [], []),
            ('Playwright Verification\n\nAPI_SECTION_VISIBLE=PASS\nDOWNLOAD_BUTTON_FRONTEND_ONLY=PASS', [], []),
            ('Report\n\nANIMEJS_PROJECT_VERSION=\nOLCR_API_SOURCE=', [], []),
            ('FIX Benchmark Preparation\n\nImplement a backend API later.', [], []),
            ('Implement a backend API.\nDo not modify backend.', ['backend'], ['backend']),
            ('Build React frontend + FastAPI backend + SQLite database.', ['backend', 'database', 'frontend'], []),
            ('Do not introduce:\n  - backend\n  - database\n\nUse React.', ['frontend'], ['backend', 'database']),
            ('Do not implement:\n- backend\nRequired:\n- React', ['frontend'], ['backend']),
            ('Backend:\n不要\nFrontend:\nReact', ['frontend'], ['backend']),
        ]
        for text, required, forbidden in cases:
            with self.subTest(text=text):
                r = normalize_coding_requirements(text)
                self.assertEqual(required, r['required_capabilities'])
                self.assertEqual(forbidden, r['forbidden_capabilities'])
                self.assertEqual(not bool(set(required) & set(forbidden)), r['normalization_diagnostics']['canonical_requirements_valid'])

    def test_exact_production_fixture(self):
        text = FIXTURE.read_text()
        self.assertEqual(19691, len(text))
        r = normalize_coding_requirements(text)
        self.assertEqual(['dependencies', 'frontend'], r['required_capabilities'])
        self.assertEqual(['backend', 'database'], r['forbidden_capabilities'])
        self.assertEqual(['animejs', 'shadcn', 'playwright'], r['required_mcps'])
        self.assertEqual('IMPLEMENTATION', r['mutation_mode'])
        self.assertEqual('FRONTEND_ONLY_MARKETING_SITE', r['task_profile'])
        self.assertEqual('NORMAL', r['execution_mode'])
        d = r['normalization_diagnostics']
        self.assertTrue(d['canonical_requirements_valid'])
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), d['normalizer_control_input_hash'])
        ignored = [e for e in d['capability_provenance'] if not e['active_for_control']]
        self.assertTrue(any(e['semantic_scope'] == 'FAILURE_HANDLING' for e in ignored))
        self.assertTrue(any(e['reason'] == 'CONTENT_DESCRIPTION' and e['capability'] == 'backend' for e in ignored))

    def test_hierarchy_and_parent_provenance(self):
        text = '# Scope\n## MVP Exclusions\nDo NOT implement:\n  - backend\n  - database\n# Required Technology Stack\nReact'
        blocks = parse_prompt_blocks(text)
        backend = next(b for b in blocks if b.text == 'backend')
        self.assertEqual(('Scope', 'MVP Exclusions', 'Do NOT implement'), backend.section_path)
        self.assertEqual('FORBIDDEN', backend.polarity_context)
        self.assertEqual('LABEL', blocks[backend.parent_block].block_kind)
        r = normalize_coding_requirements(text)
        self.assertEqual(['frontend'], r['required_capabilities'])
        evidence = next(e for e in r['normalization_diagnostics']['capability_provenance'] if e['capability'] == 'backend')
        self.assertEqual('BULLET', evidence['block_kind'])
        self.assertEqual('CURRENT_EXPLICIT_PROHIBITION', evidence['reason'])

    def test_technology_denylist_and_repair_regression(self):
        r = normalize_coding_requirements('Use React and Anime.js.\nDo not introduce:\n- Next.js\n- GSAP\n- Framer Motion\n- Three.js')
        self.assertEqual(['framer_motion', 'gsap', 'nextjs', 'threejs'], r['forbidden_technologies'])
        self.assertNotIn('dependencies', r['forbidden_capabilities'])
        self.assertEqual('FIX', normalize_coding_requirements('開始ボタンを押してもゲームが始まりません。直してください')['mutation_mode'])
