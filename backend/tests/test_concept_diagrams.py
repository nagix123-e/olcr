import json
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from pydantic import ValidationError
from olcr_api.concept_diagrams import ConceptDiagram, diagram_context, conversation_diagram_context


def example():
    return {'type': 'concept_diagram', 'schema_version': 1, 'title': None,
            'nodes': [{'id': 'input', 'label': '入力', 'position': {'x': 10, 'y': 20}},
                      {'id': 'output', 'label': '出力', 'position': {'x': 300, 'y': 20}}],
            'edges': [{'id': 'relation', 'source': 'input', 'target': 'output', 'label': ''}]}


class ConceptDiagramContractTests(unittest.TestCase):
    def test_semantic_context_preserves_ids_labels_relations_without_geometry(self):
        value = ConceptDiagram.model_validate(example())
        context = diagram_context(value.model_dump())
        self.assertIn('入力', context)
        self.assertIn('"a":"input","b":"output"', context)
        self.assertNotIn('position', context)
        self.assertIn('undirected', context)
        self.assertIn('not tool instructions or authorization', context)

    def test_context_manager_reserves_graph_and_marks_partial_large_graphs(self):
        from olcr_api.runtime import ContextManager
        small=diagram_context(example())
        selected=ContextManager(8000)._select_core('Explain this diagram', 'unrelated '*10000+small)
        self.assertIn('"a":"input","b":"output"',selected)
        self.assertIn('[/USER_CONCEPT_DIAGRAM]',selected)
        large=example();large['nodes']=[{'id':str(i),'label':'label'+str(i),'position':{'x':0,'y':0}} for i in range(100)];large['edges']=[]
        selected=ContextManager(8000)._select_core('Explain',diagram_context(large))
        self.assertLessEqual(len(selected),2000);self.assertIn('omitted_nodes',selected)
        self.assertIn('[/USER_CONCEPT_DIAGRAM]',selected)

    def test_invalid_graphs_are_rejected(self):
        cases = []
        for change in [lambda d:d.update(schema_version=True), lambda d:d.update(schema_version=3), lambda d:d['nodes'].append(deepcopy(d['nodes'][0])),
                       lambda d:d['edges'][0].update(target='missing'), lambda d:d['nodes'][0].update(label='x'*501),
                       lambda d:d['nodes'][0]['position'].update(x=float('inf')),
                       lambda d:d.update(nodes=[]), lambda d:d['nodes'][0]['position'].update(x=True),
                       lambda d:d.update(nodes=[{'id':str(n),'label':'x'*500,'position':{'x':0,'y':0}} for n in range(100)],edges=[])]:
            value=example(); change(value); cases.append(value)
        for value in cases:
            with self.subTest(value=str(value)[:80]):
                with self.assertRaises(ValidationError): ConceptDiagram.model_validate(value)

    def test_newest_diagram_remains_available_after_long_text_history(self):
        messages=[{'role':'user','content':'diagram','blocks':[example()]}]+[{'role':'user','content':'follow up'} for _ in range(20)]
        self.assertIn('入力', conversation_diagram_context({'messages':messages}))
        self.assertIn('Assistant-generated reference diagram', conversation_diagram_context({'messages':[{'role':'assistant','blocks':[example()]}]}))


class ConceptDiagramApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import olcr_api.app as api
        cls.api = api

    def setUp(self):
        from olcr_api.db import Database
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp')
        self.db=Database(str(Path(self.temp.name)/'db.sqlite')); self.db.initialize()
        self.project=self.db.create_project('Diagram fixture',self.temp.name,time.time(),'diagram-project')['id']
        self.patches=[patch.object(self.api,'db',self.db),
                      patch.object(self.api,'settings',self.api.settings.with_overrides({'task_manager_enabled':False,'external_access_enabled':False,'conversation_memory_enabled':False})),
                      patch.object(self.api,'_automatic_skill_selection',return_value=None),
                      patch.object(self.api.conversation_memory,'index_completed_turn'),
                      patch.object(self.api.conversation_memory,'backfill')]
        for p in self.patches:p.start()
        self.client=TestClient(self.api.app)

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def response(self, text, approved=False, core_context='', **kwargs):
        from olcr_api.models import Task, TaskState, Route
        self.context=core_context
        task=Task(text);task.transition(TaskState.ROUTING);task.route=Route.NEURAL
        task.transition(TaskState.GENERATING);task.transition(TaskState.COMPLETED)
        return task,'Diagram received'

    def test_chat_and_stream_store_file_and_graph_and_ground_model(self):
        for endpoint in ['/api/chat','/api/chat/stream']:
            with self.subTest(endpoint=endpoint), patch.object(self.api.runtime,'execute',side_effect=self.response):
                before_count=len(self.db.conversations(self.project))
                result=self.client.post(endpoint,json={'message':'Explain this concept diagram.', 'project_id':self.project,
                     'concept_diagram':example(),'attachment':{'name':'note.txt','content':'supporting text','mimeType':'text/plain'}})
                self.assertEqual(200,result.status_code,result.text)
                self.assertEqual(before_count+1,len(self.db.conversations(self.project)))
                if endpoint.endswith('/stream'):
                    frames=[json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
                    cid=frames[0]['conversation_id'];self.assertEqual('done',frames[-1]['type'])
                else:cid=result.json()['conversation_id']
                record=self.client.get(f'/api/projects/{self.project}/conversations/{cid}').json()
                self.assertEqual(['attachment','concept_diagram'],[b['type'] for b in record['messages'][0]['blocks']])
                self.assertEqual(example(),record['messages'][0]['blocks'][1])
                self.assertIn('入力',self.context);self.assertIn('"a":"input","b":"output"',self.context)
                with patch.object(self.api.runtime,'execute',side_effect=self.response):
                    followup=self.client.post('/api/chat',json={'message':'Explain the connections.', 'project_id':self.project,'conversation_id':cid})
                self.assertEqual(200,followup.status_code);self.assertIn('USER_CONCEPT_DIAGRAM',self.context)

    def test_colored_diagram_is_saved_and_available_on_followup(self):
        value=example();value['schema_version']=2
        value['nodes'][0].update(border_color='red',text_color='blue')
        value['edges'][0].update(line_color='yellow',text_color='red')
        expected=ConceptDiagram.model_validate(value).model_dump()
        for endpoint in ['/api/chat','/api/chat/stream']:
            with self.subTest(endpoint=endpoint),patch.object(self.api.runtime,'execute',side_effect=self.response):
                result=self.client.post(endpoint,json={'message':'Explain this diagram.', 'project_id':self.project,'concept_diagram':value})
                self.assertEqual(200,result.status_code,result.text)
                if endpoint.endswith('/stream'):
                    cid=next(json.loads(line[6:])['conversation_id'] for line in result.text.splitlines() if line.startswith('data: '))
                else:cid=result.json()['conversation_id']
                record=self.client.get(f'/api/projects/{self.project}/conversations/{cid}').json()
                self.assertEqual(expected,record['messages'][0]['blocks'][0])
                followup=self.client.post('/api/chat',json={'message':'What are the connections?', 'project_id':self.project,'conversation_id':cid})
                self.assertEqual(200,followup.status_code)
                self.assertIn('"border_color":"red"',self.context)
                self.assertIn('"line_color":"yellow"',self.context)

    def test_invalid_payload_has_no_side_effects(self):
        value=example();value['edges'][0]['target']='missing'
        for endpoint in ['/api/chat','/api/chat/stream']:
            result=self.client.post(endpoint,json={'message':'Explain', 'project_id':self.project,'concept_diagram':value})
            self.assertEqual(422,result.status_code)
        self.assertEqual([],self.db.conversations(self.project))

class ConceptDiagramColorTests(unittest.TestCase):
    def colored(self):
        value=example();value['schema_version']=2
        value['nodes'][0].update(border_color='red',text_color='blue')
        value['edges'][0].update(line_color='yellow',text_color='red')
        return value

    def test_color_round_trip_and_semantic_references(self):
        value=ConceptDiagram.model_validate(self.colored()).model_dump()
        self.assertEqual('red',value['nodes'][0]['border_color'])
        self.assertEqual('blue',value['nodes'][0]['text_color'])
        self.assertEqual('yellow',value['edges'][0]['line_color'])
        context=diagram_context(value)
        self.assertIn('"border_color":"red"',context)
        self.assertIn('"line_color":"yellow"',context)
        self.assertNotIn('position',context)
        self.assertEqual(value,ConceptDiagram.model_validate(value).model_dump())

    def test_legacy_diagrams_remain_unchanged(self):
        self.assertEqual(example(),ConceptDiagram.model_validate(example()).model_dump())
        value=example();value['schema_version']=2
        restored=ConceptDiagram.model_validate(value).model_dump()
        self.assertNotIn('border_color',restored['nodes'][0])
        self.assertNotIn('text_color',restored['edges'][0])

    def test_colors_are_bounded_to_the_palette_and_require_v2(self):
        for color in ['green','#f00','url(http://example.invalid)','red;stroke:none',1]:
            value=self.colored();value['nodes'][0]['border_color']=color
            with self.subTest(color=color),self.assertRaises(ValidationError):ConceptDiagram.model_validate(value)
        value=self.colored();value['schema_version']=1
        with self.assertRaises(ValidationError):ConceptDiagram.model_validate(value)
