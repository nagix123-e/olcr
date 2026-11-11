import unittest

from olcr_api.thinking import decide_thinking, received_reasoning, split_thinking, configured_thinking_mode


class ThinkingContractTests(unittest.TestCase):
    def test_reasoning_loop_detection_ignores_chunk_boundaries(self):
        from olcr_api.thinking import ThinkingProgressGuard
        text=('Let me reconsider this assertion and the sequence of browser actions. '*12)
        guard=ThinkingProgressGuard()
        outcomes=[guard.repeats((text*8)[i:i+7]) for i in range(0,len(text*8),7)]
        self.assertTrue(any(outcomes))
        guard=ThinkingProgressGuard()
        self.assertFalse(any(guard.repeats(f'Unique reasoning step {i}: '+str(i)*20) for i in range(100)))
        self.assertFalse(ThinkingProgressGuard().repeats(' '*5000))

    def test_existing_effort_setting_selects_each_local_role_or_unified_mode(self):
        config = {'mode': 'split', 'efforts': {'brain': 'auto', 'coder': 'off', 'short_coding': 'on', 'general_inference': 'auto'}}
        self.assertEqual('auto', configured_thinking_mode(config))
        self.assertEqual('off', configured_thinking_mode(config,'long_implementation'))
        self.assertEqual('on', configured_thinking_mode(config, 'short_coding_fix_debug'))
        self.assertEqual('auto', configured_thinking_mode(config, 'planner_replanner'))
        self.assertEqual('auto', configured_thinking_mode({**config, 'mode': 'unified'}, 'short_coding_fix_debug'))
        self.assertEqual('auto', configured_thinking_mode({}))

    def test_modes_and_protected_calls(self):
        self.assertFalse(decide_thinking('auto', 'hello')['enabled'])
        self.assertTrue(decide_thinking('auto', 'why does this fail?')['enabled'])
        self.assertTrue(decide_thinking('auto', 'short', role='planner_replanner')['enabled'])
        self.assertTrue(decide_thinking('on', 'hello')['enabled'])
        self.assertFalse(decide_thinking('off', 'design a system')['enabled'])
        for mode in ('auto', 'on', 'off'):
            self.assertEqual(mode!='off',decide_thinking(mode, 'why', structured=True)['enabled'])
            self.assertEqual(mode=='on',decide_thinking(mode, 'why', requested=False)['enabled'])

    def test_max_coding_keeps_thinking_with_structured_operations(self):
        for role in ('long_implementation', 'short_coding_fix_debug'):
            self.assertTrue(decide_thinking('on', 'implement app', structured=True, role=role)['enabled'])
            self.assertFalse(decide_thinking('off', 'implement app', structured=True, role=role)['enabled'])
            self.assertTrue(decide_thinking('on', 'internal control', structured=True, role=role, requested=False)['enabled'])

    def test_max_wins_over_every_role_format_and_internal_off_hint(self):
        for role in ('normal_chat','custom_ollama','planner_replanner','long_implementation','short_coding_fix_debug'):
            for structured in (False,True):
                for requested in (False,True,None):
                    self.assertTrue(decide_thinking('on','hi',role=role,structured=structured,requested=requested)['enabled'])
                    self.assertFalse(decide_thinking('off','検算して',role=role,structured=structured,requested=requested)['enabled'])

    def test_auto_uses_task_roles_and_short_reasoning_requests(self):
        self.assertTrue(decide_thinking('auto','作成して',role='long_implementation',structured=True)['enabled'])
        self.assertTrue(decide_thinking('auto','分割して',role='planner_replanner',structured=True)['enabled'])
        for text in ('これを検算して','この命題を証明して','矛盾を探して','この問題を解いて','verify this result'):
            self.assertTrue(decide_thinking('auto',text)['enabled'],text)
        self.assertFalse(decide_thinking('auto','こんにちは')['enabled'])
        self.assertFalse(decide_thinking('auto','検算して',requested=False)['enabled'])

    def test_parse_frame_not_source_data(self):
        self.assertEqual(('answer', 'work', True), split_thinking('<think>work</think>answer'))
        self.assertEqual(('answer', 'work', True), split_thinking('work</think>answer', reasoning_open=True))
        self.assertEqual(('', 'unfinished', False), split_thinking('unfinished', reasoning_open=True))
        for text in ('{"code":"<think>x</think>"}', '```py\nx="<think>"\n```'):
            self.assertEqual((text, '', True), split_thinking(text))

    def test_logs_are_bounded_and_no_opaque_values(self):
        self.assertIsNone(received_reasoning(None))
        self.assertIsNone(received_reasoning({'encrypted_content': 'secret'}))
        self.assertIsNone(received_reasoning(''))
        log = received_reasoning('x' * 64001, complete=False)
        self.assertEqual(64000, len(log['text']))
        self.assertTrue(log['truncated'])
        self.assertFalse(log['complete'])
