import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from olcr_api.db import Database

_APP_PROFILES = []


class MedicationChatTests(unittest.TestCase):
    def setUp(self):
        from olcr_api.medication_chat import respond_in_chat
        self.respond=respond_in_chat
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db=Database(str(Path(self.tmp.name)/'test.db')); self.db.initialize()
        self.db.create_conversation('meds',1,'chat',None)
        self.n=0
    def turn(self,text,ident=None):
        self.n+=1
        value=SimpleNamespace(message=text,conversation_id='chat',message_id=ident or str(self.n),image=None,
                              client_time_zone='Asia/Tokyo',resume_control=False,resume_task_id=None)
        return self.respond(self.db,value,lambda v:('chat','',self.db.conversation('chat')),None)
    def register(self):
        r=self.turn('服薬を登録\n薬名：テスト薬\n規格：5mg\n1回量：1錠\n用法：朝夕各1錠\n24時間上限：2錠\n最小間隔：6時間')
        self.assertIn('確認',r['response'])
        return self.turn('服薬を確定')
    def test_registration_requires_confirmation_and_exact_name(self):
        from olcr_api.medication_records import MedicationRecords
        self.turn('服薬を登録\n薬名：テスト薬\n規格：5mg\n1回量：1錠\n用法：朝夕各1錠')
        self.assertEqual(MedicationRecords(self.db).medications(),[])
        self.turn('服薬を確定')
        self.assertEqual(MedicationRecords(self.db).medications()[0]['name'],'テスト薬')
    def test_take_and_retry_keep_one_intake_and_history_is_named(self):
        self.register()
        r=self.turn('今、テスト薬を1錠飲んだ','same')
        self.assertIn('記録しました',r['response'])
        self.assertEqual(self.turn('今、テスト薬を1錠飲んだ','same')['response'],r['response'])
        history=self.turn('服薬履歴を見せて')['response']
        self.assertIn('テスト薬',history); self.assertIn('5mg',history); self.assertIn('1回',history)
    def test_no_hijack_or_inferred_dose(self):
        self.assertIsNone(self.turn('服薬管理APIを実装して'))
        self.register()
        self.assertIsNone(self.turn('テスト薬はまだ飲んでいない'))
        self.assertIn('量',self.turn('今、テスト薬を飲んだ')['response'])
        self.assertIsNone(self.turn('Pythonのコードを書いて'))
    def test_ambiguous_strength_never_records(self):
        from olcr_api.medication_records import MedicationRecords
        self.register(); store=MedicationRecords(self.db)
        store.register(dict(name='テスト薬',strength='10mg',dose='1',unit='錠',instructions='夕方1錠'),'other')
        self.assertIn('複数',self.turn('今、テスト薬を1錠飲んだ')['response'])
        self.assertEqual(store.summary(__import__('datetime').datetime.now(__import__('datetime').timezone.utc),'UTC')['medications'],[])
    def test_incomplete_draft_followup_and_cancel(self):
        self.turn('服薬を登録\n薬名：テスト薬')
        self.turn('規格：5mg\n1回量：1錠\n用法：朝夕各1錠')
        r=self.turn('服薬を確定'); self.assertIn('登録しました',r['response'])
        self.turn('服薬を登録\n薬名：別の薬')
        self.assertIn('取り消',self.turn('キャンセル')['response'])

    def test_ocr_labels_on_separate_lines_are_only_a_draft(self):
        from olcr_api.medication_chat import prescription_fields
        raw='薬名\nアセトアミノフェン錠200mg\n規格\n200mg\n1回量\n1錠\n用法\n1日3回 食後'
        draft=prescription_fields(raw)
        self.assertEqual(draft['name'],'アセトアミノフェン錠200mg')
        self.assertEqual(draft['dose_text'],'1錠')

    def test_multiple_drugs_are_not_combined_into_one_prescription(self):
        from olcr_api.medication_chat import prescription_fields
        with self.assertRaisesRegex(ValueError,'MULTIPLE'):
            prescription_fields('薬名：薬A\n規格：5mg\n薬名：薬B\n規格：10mg')

    def test_rewind_does_not_silently_add_another_intake(self):
        from olcr_api.medication_records import MedicationRecords
        self.register(); self.turn('今、テスト薬を1錠飲んだ','taken')
        last=self.db.conversation('chat')['messages'][-1]['id']
        self.db.rewind_conversation('chat','taken',last,None,__import__('time').time())
        response=self.turn('今、テスト薬を2錠飲んだ','edited')['response']
        self.assertIn('訂正',response)
        report=MedicationRecords(self.db).summary(__import__('datetime').datetime.now(__import__('datetime').timezone.utc),'UTC')
        self.assertEqual(report['medications'][0]['count'],1)

    def test_unknown_intake_id_is_reported_as_unregistered(self):
        for ident in ('[c498b4b576877ee1f36c6bf9]', '00000000000000000000000000000000'):
            result=self.turn('服用記録を取り消し '+ident)
            self.assertIsNotNone(result)
            self.assertIn('登録されていません',result['response'])

    def test_image_request_to_add_schedule_starts_prescription_confirmation(self):
        from unittest.mock import patch
        value=SimpleNamespace(message='服用予定に入れて',conversation_id='chat',message_id='image',
            image={'name':'prescription.png'},client_time_zone='Asia/Tokyo',resume_control=False,resume_task_id=None)
        with patch('olcr_api.medication_chat._ocr',return_value='薬名：薬A\n規格：5mg\n1回量：1錠\n用法：朝夕'):
            result=self.respond(self.db,value,lambda v:('chat','',self.db.conversation('chat')),None)
        self.assertIn('確認',result['response']); self.assertIn('薬A',result['response'])

    def test_medical_turns_are_not_eligible_for_ordinary_semantic_memory(self):
        self.register(); self.turn('今、テスト薬を1錠飲んだ')
        self.assertEqual(self.db.completed_turns(),[])
        self.db.add_message('chat','user','普通の作業の質問',1,'normal-u')
        self.db.add_message('chat','assistant','通常回答',2,'normal-a')
        self.assertEqual(self.db.completed_turns()[0]['user_message'],'普通の作業の質問')

    def test_ordinary_chat_exits_before_opening_medication_store(self):
        from unittest.mock import patch
        with patch('olcr_api.medication_chat.MedicationRecords') as store:
            self.assertIsNone(self.turn('こんにちは'))
            store.assert_not_called()

    def test_real_chat_and_stream_routes_share_medication_confirmation(self):
        import os
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        bootstrap=tempfile.TemporaryDirectory(); _APP_PROFILES.append(bootstrap)
        with patch.dict(os.environ,{'OLCR_DB_PATH':str(Path(bootstrap.name)/'bootstrap.db'),
                'OLCR_APP_SUPPORT':bootstrap.name,'OLCR_ALLOWED_ROOTS':bootstrap.name,'OLCR_VECTOR_ENABLED':'false'}):
            import olcr_api.app as api
        with patch.object(api,'db',self.db),patch.object(api,'_gui_session_token',''), \
                patch.object(api,'get_assistant_service',return_value=SimpleNamespace()), \
                patch.object(api,'get_service_connectors',return_value=SimpleNamespace()), \
                patch.object(api,'_chat_request_intent',side_effect=AssertionError('model routing reached')):
            with TestClient(api.app) as client:
                draft=client.post('/api/chat',json={'message':'服薬を登録\n薬名：薬A\n規格：5mg\n1回量：1錠\n用法：毎朝',
                    'conversation_id':'chat','message_id':'api-rx','project_id':None})
                self.assertEqual(draft.status_code,200);self.assertIn('確認',draft.json()['response'])
                result=client.post('/api/chat/stream',json={'message':'服薬を確定','conversation_id':'chat',
                    'message_id':'api-confirm','project_id':None})
                import json
                events=[json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
                self.assertEqual([event['type'] for event in events],['meta','chunk','done'])
                self.assertIn('登録しました',events[1]['text'])
                self.assertEqual(events[2]['response_owner'],'MEDICATION_MANAGEMENT')
                bad=client.post('/api/chat',json={'message':'服薬を確定','conversation_id':'chat',
                    'message_id':'api-confirm','project_id':'wrong-project'})
                self.assertEqual(bad.status_code,403)
                self.assertNotIn('薬A',api.active_conversation_context('chat'))
