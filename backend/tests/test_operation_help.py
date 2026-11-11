import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from olcr_api.operation_help import ROOT, GRAPH, topics, requested, answer
from olcr_api.graphify_rag import GraphifyRAG

class OperationHelpTests(unittest.TestCase):
    def test_every_topic_has_verified_bilingual_graph_sources(self):
        for row in topics():
            for language in ('ja','en'):
                with self.subTest(topic=row['id'],language=language):
                    # Query with the article's indexed public title. Internal
                    # IDs concatenate tokens and cannot locate the source.
                    key=row['titles'][language]
                    hits,handled=GRAPH.search(key,str(ROOT),1)
                    self.assertTrue(handled);self.assertEqual(1,len(hits))
                    self.assertEqual(row['id']+'-'+language+'.md',Path(hits[0].source).name)
                    self.assertEqual(hashlib.sha256(Path(hits[0].source).read_bytes()).hexdigest(),hits[0].graphify['source_hash'])
    def test_questions_and_actions_have_distinct_ownership(self):
        for text in ['OLCRの使い方を教えて','カレンダーに接続するには？','音声入力の使い方','OLCRでQuickとは？','How do I use Skills in OLCR?','OLCRで何ができる？','Thinkingのログはどこで見る？']:
            self.assertTrue(requested(text),text)
        for text in ['カレンダーに接続して','タイマーを5分','OLCRにカレンダー機能を実装して','/skill apple-design UIを直して','Pythonのソートの使い方','このアプリを作って']:
            self.assertFalse(requested(text),text)
    def test_answers_are_from_sources_without_model_or_workspace(self):
        for text,language,expected in [('OLCRの音声入力の使い方','ja','LFM2.5-Audio-1.5B-JP'),('How do I use Quick in OLCR?','en','lightning'),('OLCRのカレンダー接続方法','ja','APIキーやGoogleの開発者設定は不要')]:
            result=answer(text,language)
            self.assertIn(expected,result['response'])
            self.assertTrue(result['knowledge_provenance']['verified'])

    def test_programming_howto_does_not_become_product_help(self):
        for question in ['テトリスの実装方法を教えて', '二分探索の実装方法は？',
                         'How do I learn coding?', 'Pythonでキャッシュを実装するには？']:
            with self.subTest(question=question):
                self.assertFalse(requested(question))
        self.assertTrue(requested('コーディングの検証結果はどこ？'))
    def test_anticipated_questions_select_the_feature_or_its_specific_procedure(self):
        from olcr_api.operation_help import matching_topics
        available=topics();by_id={row['id']:row for row in available};count=0
        repo=Path(__file__).resolve().parents[2]
        for row in available:
            for source in row.get('implementation_sources',[]):
                self.assertTrue((repo/source).is_file(),source)
            for lang,questions in row.get('questions',{}).items():
                for question in questions:
                    count+=1
                    with self.subTest(topic=row['id'],question=question,language=lang):
                        self.assertTrue(requested(question))
                        result=answer(question,lang)
                        primary=by_id[result['knowledge_provenance']['topics'][0]]
                        self.assertTrue(primary['id']==row['id'] or primary.get('parent')==row['id'],primary['id'])
                        self.assertTrue(result['knowledge_provenance']['verified'])
        self.assertGreaterEqual(count,200)

    def test_multiple_features_remain_distinct_and_commands_keep_their_owner(self):
        ids=answer('音声入力のマイク権限とカレンダーの権限はどこ？')['knowledge_provenance']['topics']
        self.assertIn('voice-permission',ids);self.assertIn('calendar-denied',ids)
        for q in ['/skill apple-design この画面を修正して','タイマーを5分開始して','Quickをオンにして','カレンダーに接続して','画像付きエージェントのバグを直して','音声入力機能を実装して','使い方を教えて。音声入力機能も実装して','Quickを実装してください。使い方も教えて']:
            self.assertFalse(requested(q),q)
        self.assertTrue(requested('OLCRで「実装して」と頼む方法は？'))
        self.assertFalse(requested('How do I compare digital image sorting algorithms?'))

    def test_relocatable_marker_does_not_trust_a_workspace_graph(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'graphify-out').mkdir()
            (root/'graphify-out/graph.json').write_text((ROOT/'graphify-out/graph.json').read_text())
            rows,owned=GraphifyRAG().search('voicejapanese',str(root))
            self.assertFalse(owned);self.assertEqual([],rows)
