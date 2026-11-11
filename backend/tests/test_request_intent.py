import unittest
from olcr_api.request_intent import resolve_request_intent
from olcr_api.runtime import Runtime


class RequestIntentTests(unittest.TestCase):
    def test_software_work_owns_domain_keywords_and_research_instructions(self):
        requests=[
            '一週間の献立プランナーを一から作ってください。保存と買い物リストも追加してください。',
            'YouTube動画検索アプリを作ってください。',
            'カレンダー管理画面を作ってください。明日の予定を一覧表示します。',
            'CSVに変換する機能を実装して。',
            '最新のドキュメントを検索してから、検索APIを実装してください。',
            'https://example.com から商品情報を収集するスクレイピングアプリを作ってください。',
            'Build a weekly meal planner from scratch, with a searchable recipe list.',
            '既存アプリで保存できない原因を調べて直して。',
            '画面のボタンが反応しないので直して',
            'Add search and persistence to this planner.',
            'Implement a calendar dashboard showing today’s events.',
            'Create main.ts and index.html for a timer application.',
            '/skill apple-design\n一週間の献立プランナーを独立したアプリとして一から作ってください。',
        ]
        for text in requests:
            with self.subTest(text=text):
                intent=resolve_request_intent(text)
                self.assertEqual('CODING',intent.owner)
                self.assertTrue(intent.blocks_external_answer)
                self.assertTrue(Runtime._implementation_intent(text))

    def test_real_domain_operations_and_information_requests_stay_non_coding(self):
        for text in ['YouTubeでPythonの動画を検索して','カレンダーに明日の会議を追加して',
                     'タイマーを5分に変更して','献立アプリを検索して','献立を考えて','アプリを作ってくれるサービスを探して',
                     'Rosneftが現在の制裁リストに掲載されているか、利用可能なAPIで確認して。',
                     '天気APIで今日の東京の気温を取得して', 'Explain what a search API is.','カレンダーアプリの使い方を教えて',
                     '/youtube search Python','/calendar list','/git status']:
            with self.subTest(text=text):
                intent=resolve_request_intent(text)
                self.assertFalse(intent.blocks_external_answer)
                self.assertFalse(Runtime._implementation_intent(text))

    def test_contradictory_mutation_is_clarified_instead_of_searched(self):
        intent=resolve_request_intent('このアプリを実装して。ただし実装はしないでください。')
        self.assertEqual('CLARIFY_CODING',intent.owner)
        self.assertTrue(intent.blocks_external_answer)
        self.assertFalse(intent.mutation)

    def test_underspecified_software_request_requires_action_clarification(self):
        for text in ['献立プランナーをお願い','このアプリ','検索API']:
            with self.subTest(text=text):
                intent=resolve_request_intent(text)
                self.assertEqual('CLARIFY_CODING',intent.owner)
                self.assertTrue(intent.blocks_external_answer)
        self.assertEqual('GENERAL',resolve_request_intent('献立プランナーとは何ですか？').owner)
