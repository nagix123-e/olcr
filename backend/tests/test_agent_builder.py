import base64
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import Mock
from olcr_api.db import Database
from olcr_api.custom_agents import AgentDefinition,AgentStore
from olcr_api.agent_builder import builder_state,handle_builder,requested,creation_intent
from olcr_api.agent_packages import export_zip


class AgentBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'builder.db'));self.db.initialize()
        self.db.create_conversation('Builder',1,'a',None);self.db.create_conversation('Other',1,'b',None)
        self.context={'db':self.db}
        self.no_generation=Mock(side_effect=AssertionError('Supplied data must never be generated'))

    def send(self,message,attachment=None,cid='a',generate=None,identity=None):
        value=SimpleNamespace(message=message,attachment=attachment,image=None,message_id=identity,agent_builder=True,conversation_id=cid)
        return handle_builder(self.context,value,cid,generate=generate or self.no_generation)

    def file(self,name,data):return {'name':name,'bytesBase64':base64.b64encode(data).decode()}
    def agents(self):return AgentStore(self.db).list()

    def test_supplied_prompt_is_copied_exactly_without_model(self):
        prompt='  Keep these spaces.\nname: this is part of the prompt\nDo not rewrite.\n'
        result=self.send('カスタムエージェントを作成\n名前: Copy\nプロンプト:\n'+prompt)
        self.assertEqual('created',result['blocks'][0]['status'])
        self.assertEqual(prompt,self.agents()[0]['instructions'])
        self.assertEqual('Copy',self.agents()[0]['name']);self.no_generation.assert_not_called()

    def test_knowledge_file_survives_questions_and_restart(self):
        raw=b'\xef\xbb\xbfname,value\r\nA,42\r\n'
        result=self.send('この資料でカスタムエージェントを作成',self.file('table.csv',raw))
        self.assertEqual('collecting',result['blocks'][0]['status'])
        self.assertEqual([],self.agents())
        self.context={'db':Database(self.db.path)}
        self.send('Exact name')
        agent=self.agents()[0]
        self.assertEqual('Exact name',agent['name']);self.assertEqual('',agent['instructions'])
        self.assertEqual(raw,base64.b64decode(agent['knowledge'][0]['file_base64']))
        self.no_generation.assert_not_called()

    def test_working_folder_is_imported_as_knowledge_including_large_files(self):
        from olcr_api.working_folders import WorkingFolders
        folder=Path(self.tmp.name)/'knowledge-folder';folder.mkdir()
        large=b'x'*10_000_001
        (folder/'large.md').write_bytes(large)
        (folder/'notes.txt').write_text('Keep all of this source.')
        result=self.send('名前: 街頭演説エージェント\nこれでエージェントを作成して',
                         {'kind':'folder','name':'knowledge-folder','directoryPath':str(folder)})
        self.assertEqual('created',result['blocks'][0]['status'])
        knowledge=self.agents()[0]['knowledge']
        self.assertEqual({'large.md','notes.txt'},{item['name'] for item in knowledge})
        originals={item['name']:base64.b64decode(item['file_base64']) for item in knowledge}
        self.assertEqual(large,originals['large.md'])
        self.assertEqual(b'Keep all of this source.',originals['notes.txt'])
        self.assertEqual(str(folder.resolve()),WorkingFolders(self.db).get('a')['root'])
        self.no_generation.assert_not_called()

    def test_ambiguous_source_is_asked_about_then_prompt_is_copied(self):
        source='You must preserve\r\n every character.\r\n'
        self.send('カスタムエージェントを作成',self.file('prompt.txt',source.encode()))
        self.assertEqual('source_role',builder_state(self.db,'a')['waiting'])
        self.send('プロンプト')
        self.assertEqual('name',builder_state(self.db,'a')['waiting'])
        self.send('Prompt agent')
        self.assertEqual(source,self.agents()[0]['instructions']);self.no_generation.assert_not_called()

    def test_agent_zip_is_transferred_with_icon_and_delegates(self):
        store=AgentStore(self.db);child=store.save(AgentDefinition(name='Child'))
        original=store.save(AgentDefinition(name='Original',icon='🤖',instructions='Original prompt',subagent_ids=[child['id']]))
        result=self.send('カスタムエージェントを作成',self.file('agent.zip',export_zip(store,original['id'])))
        copy=store.get(result['blocks'][0]['agent_id'])
        self.assertEqual('🤖',copy['icon']);self.assertEqual('Original prompt',copy['instructions'])
        self.assertNotEqual(child['id'],copy['subagent_ids'][0]);self.assertEqual(4,len(self.agents()))
        self.no_generation.assert_not_called()

    def test_existing_knowledge_zip_is_ingested_as_original_files(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as archive:
            archive.writestr('set/data.csv',b'x,y\n1,2\n');archive.writestr('set/note.txt','Keep this.\n')
        self.send('名前: Set\nこの知識でカスタムエージェントを作成',self.file('knowledge.zip',out.getvalue()))
        self.assertEqual([b'x,y\n1,2\n',b'Keep this.\n'],[base64.b64decode(item['file_base64']) for item in self.agents()[0]['knowledge']])
        self.no_generation.assert_not_called()

    def test_generation_can_clarify_across_turns_and_preserves_given_name(self):
        generate=Mock(side_effect=[{'action':'ask','questions':['対象者は誰ですか？']},
                                   {'action':'ask','questions':['何を練習しますか？']},
                                   {'action':'create','name':'Do not use this name','instructions':'Generated instructions',
                                    'knowledge':[{'name':'Generated notes','content':'Generated reference'}]}])
        self.send('名前: 固定名\n条件: 英語の練習を支援する',generate=generate)
        self.send('中級者です',generate=generate)
        self.assertEqual([],self.agents())
        self.send('仕事のメールを練習します',generate=generate)
        self.assertEqual(3,generate.call_count)
        self.assertEqual('固定名',self.agents()[0]['name'])
        self.assertEqual('Generated instructions',self.agents()[0]['instructions'])

    def test_retries_do_not_duplicate_agents_or_transcripts(self):
        message='名前: Unique\n指示: Exact prompt'
        first=self.send(message,identity='retry');second=self.send(message,identity='retry')
        self.assertEqual(first,second);self.assertEqual(1,len(self.agents()))
        self.assertEqual(2,len(self.db.conversation('a')['messages']))

    def test_requested_generated_knowledge_is_not_silently_omitted(self):
        generate=Mock(return_value={'action':'create','name':'Tutor','instructions':'Help with English','knowledge':[]})
        result=self.send('条件: 英語の練習用で、参考知識を生成してほしい',generate=generate)
        self.assertEqual('collecting',result['blocks'][0]['status']);self.assertEqual([],self.agents())

    def test_draft_is_conversation_scoped_and_cancel_exits_creation(self):
        self.send('カスタムエージェントを作成してください')
        value=SimpleNamespace(agent_builder=False,message='Hello',conversation_id='b')
        self.assertFalse(requested(self.db,value))
        value.conversation_id='a';self.assertTrue(requested(self.db,value))
        self.send('キャンセル');self.assertFalse(requested(self.db,value));self.assertEqual([],self.agents())

    def test_an_existing_prompt_without_labels_is_not_regenerated(self):
        prompt='あなたは翻訳者です。\n入力をそのまま翻訳してください。\n'
        self.send(prompt);self.send('Translator')
        self.assertEqual(prompt,self.agents()[0]['instructions']);self.no_generation.assert_not_called()

    def test_unlabelled_supplied_text_is_clarified_without_generation(self):
        source='返答の最後には必ず出典を記載してください。'
        self.send(source)
        self.assertEqual('source_role',builder_state(self.db,'a')['waiting'])
        self.send('指示としてそのまま')
        self.send('原文のまま')
        self.assertEqual(source,self.agents()[0]['instructions']);self.no_generation.assert_not_called()

    def test_explicit_knowledge_json_is_not_reinterpreted_as_configuration(self):
        raw=b'{"name":"Dataset name","instructions":"Reference data"}'
        self.send('名前: Dataset\n知識としてカスタムエージェントを作成',self.file('data.json',raw))
        self.assertEqual(raw,base64.b64decode(self.agents()[0]['knowledge'][0]['file_base64']))
        self.assertEqual('',self.agents()[0]['instructions']);self.no_generation.assert_not_called()

    def test_missing_previous_material_is_requested_instead_of_invented(self):
        self.send('さっき渡した元のプロンプトでカスタムエージェントを作成')
        self.assertEqual('transfer',builder_state(self.db,'a')['mode']);self.assertEqual([],self.agents())
        self.no_generation.assert_not_called()

    def test_intent_does_not_interpret_help_questions_or_file_content(self):
        self.assertTrue(creation_intent('旅行を助けるカスタムエージェントを作って'))
        self.assertTrue(creation_intent('Create a custom agent for editing'))
        self.assertFalse(creation_intent('カスタムエージェントを作成する方法を説明して'))

    def test_development_planning_request_generates_instead_of_asking_source_role(self):
        message='開発計画をユーザーに実装詳細について質問しながら立ててくれるエージェントを作りたい'
        self.assertTrue(creation_intent(message))
        generate=Mock(return_value={'action':'create','name':'開発計画サポート',
                                   'instructions':'ユーザーに実装詳細を質問し、回答を踏まえて開発計画を立ててください。'})
        result=self.send(message,generate=generate)
        self.assertEqual('created',result['blocks'][0]['status'])
        self.assertEqual('generate',result['blocks'][0]['mode'])
        self.assertIn(message,generate.call_args.args[0])
        self.assertEqual('開発計画サポート',self.agents()[0]['name'])

    def test_selected_mode_accepts_behavior_description_without_creation_phrase(self):
        generate=Mock(return_value={'action':'ask','questions':['回答はチェックリスト形式にしますか？']})
        result=self.send('開発計画を実装詳細について質問しながら立ててくれる',generate=generate)
        self.assertIn('チェックリスト',result['response'])
        self.assertNotIn('source_role',builder_state(self.db,'a')['waiting'])
        generate.return_value={'action':'create','name':'計画サポート','instructions':'質問してからチェックリスト形式の開発計画を作成する。'}
        self.send('はい',generate=generate)
        self.assertTrue(any('チェックリスト形式' in item for item in generate.call_args.args[0]))
        self.assertEqual(1,len(self.agents()))

    def test_repeating_request_recovers_old_misclassified_text_draft(self):
        message='開発計画をユーザーに実装詳細について質問しながら立ててくれるエージェントを作りたい'
        state={'status':'collecting','mode':'transfer','definition':{},'conditions':[],
               'locked':[],'waiting':'source_role','pending_text':True,
               'pending_source':{'name':'provided.txt','content':message}}
        with self.db.connect() as connection:
            connection.execute('INSERT INTO custom_agent_builders VALUES(?,?,?)',('a',json.dumps(state),1))
        generate=Mock(return_value={'action':'create','name':'計画','instructions':'実装詳細を質問して計画する。'})
        result=self.send(message,generate=generate)
        self.assertEqual('created',result['blocks'][0]['status']);generate.assert_called_once()

    def test_unresolved_text_remains_text_and_never_generates(self):
        self.send('返答の最後には必ず出典を記載してください。')
        result=self.send('何のこと？')
        self.assertIn('文章',result['response']);self.assertNotIn('ファイル',result['response'])
        self.send('指示');self.send('原文保持')
        self.assertEqual('返答の最後には必ず出典を記載してください。',self.agents()[0]['instructions'])
        self.no_generation.assert_not_called()
