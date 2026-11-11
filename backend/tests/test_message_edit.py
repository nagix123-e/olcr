import tempfile
import unittest
from pathlib import Path
from olcr_api.db import Database
from olcr_api.models import Task, TaskState


class MessageEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / 'edit.db'))
        self.db.initialize()
        self.db.create_conversation('Title', 1, 'c')
        self.db.create_conversation('Other', 1, 'other')
        for n in range(1, 4):
            self.db.add_message('c', 'user', f'question {n}', n * 2, f'u{n}')
            self.db.add_message('c', 'assistant', f'answer {n}', n * 2 + 1, f'a{n}')
        self.db.add_message('other', 'user', 'unrelated', 1, 'other-u')

    def tearDown(self):
        self.tmp.cleanup()

    def rewind(self, message='u2', last='a3', project=None):
        return self.db.rewind_conversation('c', message, last, project, 100)

    def test_suffix_and_memory_removed_prefix_and_other_conversation_retained(self):
        turns = self.db.completed_turns()
        for turn in turns:
            self.db.save_memory_embedding(turn, 'fixture', [1.0], 9, 'v1')
        result = self.rewind()
        self.assertEqual(['u1', 'a1'], [m['id'] for m in result['messages']])
        self.assertEqual('unrelated', self.db.conversation('other')['messages'][0]['content'])
        self.assertEqual([], self.db.memory_embeddings('fixture', 1, 'v1'))
        # A late indexing worker cannot restore the deleted answer.
        self.db.save_memory_embedding(turns[-1], 'fixture', [1.0], 101, 'v1')
        self.assertEqual([], self.db.memory_embeddings('fixture', 1, 'v1'))
        self.db.add_message('c', 'user', 'replacement', 102, 'new-u')
        self.db.add_message('c', 'assistant', 'new answer', 103, 'new-a')
        self.assertEqual([0, 1, 2, 3], [m['ordinal'] for m in self.db.conversation('c')['messages']])

    def test_latest_message_can_be_edited(self):
        self.assertEqual(['u1', 'a1', 'u2', 'a2'], [m['id'] for m in self.rewind('u3')['messages']])

    def test_stale_tail_old_user_assistant_and_scope_are_rejected_without_mutation(self):
        for message, last, project, code in [('u1','a3',None,'MESSAGE_NOT_EDITABLE'), ('a3','a3',None,'MESSAGE_NOT_EDITABLE'), ('u2','a2',None,'CONVERSATION_CHANGED'), ('u2','a3','wrong','CONVERSATION_SCOPE_MISMATCH')]:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                self.rewind(message, last, project)
            self.assertEqual(6, len(self.db.conversation('c')['messages']))

    def test_active_normal_task_prevents_rewind(self):
        task = Task(raw_request='running')
        task.state = TaskState.GENERATING
        self.db.save_task(task, 'c')
        with self.assertRaisesRegex(ValueError, 'CONVERSATION_BUSY'):
            self.rewind()
        self.assertEqual(6, len(self.db.conversation('c')['messages']))

    def test_active_coding_task_prevents_rewind(self):
        self.db.create_coding_task('coding', 'c', 'goal', 'RUNNING', 'IMPLEMENTATION', None, 5, source_message_id='u2')
        with self.assertRaisesRegex(ValueError, 'CONVERSATION_BUSY'):
            self.rewind()
        self.assertEqual(6, len(self.db.conversation('c')['messages']))

    def test_superseded_tasks_cannot_reappear_and_agent_binding_is_retained(self):
        self.db.create_coding_task('coding', 'c', 'goal', 'RESUMABLE', 'NONE', None, 5, source_message_id='u2')
        self.db.create_interactive_planning('c', [{'question': 'old question'}])
        self.rewind()
        task = self.db.coding_task('coding')
        self.assertTrue(task['archived'])
        self.assertEqual('MESSAGE_REPLACED', task['recovery_reason'])
        self.assertIsNone(self.db.active_interactive_planning('c'))
        self.db.add_message('c', 'assistant', 'late recovery', 101, 'late', 'coding')
        self.assertEqual('', self.db.persist_coding_stop('coding', {}, 0))
        self.assertEqual(['u1', 'a1'], [m['id'] for m in self.db.conversation('c')['messages']])

    def test_bound_custom_agent_definition_survives_rewind(self):
        from olcr_api.custom_agents import AgentStore, AgentDefinition
        agent = AgentStore(self.db).save(AgentDefinition(name='Fixture', instructions='Help'))
        bound = AgentStore(self.db).bind(agent)
        cid = bound['id'] if isinstance(bound, dict) else bound
        self.db.add_message(cid, 'user', 'Question', 2, 'agent-u')
        self.db.add_message(cid, 'assistant', 'Answer', 3, 'agent-a')
        result = self.db.rewind_conversation(cid, 'agent-u', 'agent-a', None, 4)
        self.assertEqual(agent['id'], result['custom_agent']['id'])
        self.assertEqual([], result['messages'])
