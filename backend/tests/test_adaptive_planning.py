import unittest

from olcr_api.coding_tasks import compact_normal_plan, derive_task_graph, normalize_coding_requirements
from olcr_api.planning_policy import planning_policy, validate_definition_steps, lean_plan_prompt


LUNCH = '''空のプロジェクトから昼食ルーレットというWebアプリを新規作成してください。
- 料理名を1行に1つ入力できる
- 候補から1つをランダムに選んで表示する
- 直近5回の結果を履歴として表示する
- 直前と同じ料理を避け、空の入力にはエラーを表示する
HTML・CSS・JavaScriptを使い、外部ライブラリやネットワークは使わない。
画面と抽選ロジックを分け、Node.jsのnode:testでテストを追加する。'''


class AdaptivePlanningTests(unittest.TestCase):
    def test_bounded_offline_production_api_is_generated_together_before_its_tests(self):
        requirements=normalize_coding_requirements(LUNCH)
        policy=planning_policy(LUNCH,requirements,'MEDIUM')
        self.assertTrue(policy['coherent_static_application'])
        prompt=lean_plan_prompt(LUNCH,requirements,'MEDIUM')
        self.assertIn('Use 1-2 coherent deliverable phases',prompt)
        self.assertIn('keeping their requested separate source files',prompt)
        self.assertIn('actual produced API',prompt)
        for changed,size,overrides in ((LUNCH,'LARGE',{}),(LUNCH,'MEDIUM',{'new_project_intent':False}),
                (LUNCH,'MEDIUM',{'mutation_mode':'FIX'}),(LUNCH,'MEDIUM',{'dependency_requirements':[{'name':'library'}]}),
                (LUNCH.replace('JavaScript','TypeScript'),'MEDIUM',{})):
            self.assertFalse(planning_policy(changed,{**requirements,**overrides},size)['coherent_static_application'])

    def assess(self, text, size='SMALL'):
        return planning_policy(text, normalize_coding_requirements(text), size)

    def test_complete_small_definition_uses_lean_execution(self):
        self.assertEqual('LEAN', self.assess(LUNCH, 'MEDIUM')['mode'])
        explicit = LUNCH + '\nまず実装計画を作成し、承認後に進めてください。'
        requirements = normalize_coding_requirements(explicit)
        self.assertTrue(requirements['planning_only'])
        self.assertEqual('LEAN', planning_policy(explicit, requirements, 'MEDIUM')['mode'])

    def test_length_does_not_replace_a_product_definition(self):
        vague = '新規Webアプリをいい感じに作成して。\n' + 'きれいな見た目で、おしゃれにしたい。\n' * 30
        result = self.assess(vague)
        self.assertEqual('CLARIFY', result['mode'])
        self.assertIn('操作', result['questions'][0])

    def test_large_work_requires_more_definition_than_small_work(self):
        text = 'ReactでローカルWebアプリを新規作成し、商品を検索できるようにする。'
        self.assertEqual('LEAN', self.assess(text)['mode'])
        self.assertEqual('CLARIFY', self.assess(text, 'LARGE')['mode'])
        self.assertEqual('CLARIFY', self.assess(text * 12, 'LARGE')['mode'])
        self.assertEqual('LEAN', self.assess(LUNCH, 'LARGE')['mode'])
        self.assertEqual('LEAN', self.assess(LUNCH.replace('\n',' '), 'LARGE')['mode'])

    def test_focused_existing_code_fix_and_delegated_design_do_not_require_a_session(self):
        self.assertEqual('LEAN', self.assess('Fix the empty input error in app.js')['mode'])
        self.assertEqual('DELEGATED_DESIGN', self.assess('新規Webアプリを作成して。詳細は任せる。')['mode'])

    def test_ui_logic_separation_requires_a_declared_boundary(self):
        one = {'phases':[{'execution_mode':'IMPLEMENTATION_AND_VERIFICATION'}]}
        self.assertTrue(validate_definition_steps(one, LUNCH))
        two = {'phases':[{'id':'ui','goal':'画面を実装','execution_mode':'IMPLEMENTATION'}, {'id':'logic','goal':'抽選ロジックを実装','execution_mode':'IMPLEMENTATION_AND_VERIFICATION'}]}
        self.assertEqual([], validate_definition_steps(two, LUNCH))

    def test_separate_source_files_do_not_require_extra_execution_phases(self):
        plan={'phases':[{'id':'app','goal':'Create the requested app and tests',
                         'execution_mode':'IMPLEMENTATION_AND_VERIFICATION'}],
              'file_manifest':[{'path':'index.html','role':'画面'},
                               {'path':'selection.js','role':'抽選ロジック'},
                               {'path':'selection.test.js','role':'node:test'}]}
        self.assertEqual([],validate_definition_steps(plan,LUNCH))
        plan.pop('file_manifest')
        plan['scope']={'allowed':['index.html','lottery.js','lottery.test.js']}
        self.assertEqual([],validate_definition_steps(plan,LUNCH))
        plan['scope']['allowed']=['index.html','lottery.test.js']
        self.assertTrue(validate_definition_steps(plan,LUNCH))

    def test_test_only_implementation_phase_after_source_creation_is_rejected(self):
        plan={'phases':[
            {'id':'app','goal':'Implement the application UI and logic','execution_mode':'IMPLEMENTATION'},
            {'id':'tests','goal':'Create tests and verify the implementation','execution_mode':'IMPLEMENTATION',
             'dependencies':['app']},
            {'id':'final','goal':'Run final acceptance checks','execution_mode':'VERIFICATION_ONLY'}],
            'file_manifest':[
                {'path':'index.html','action':'create','owner_phase':'app'},
                {'path':'app.js','action':'create','owner_phase':'app'},
                {'path':'tests/meal-planner.test.js','action':'create','owner_phase':'tests'}]}
        errors=validate_definition_steps(plan,'一週間の献立アプリを新規作成し、動作を確認してください。')
        self.assertTrue(any('same implementation phase' in error for error in errors),errors)

    def test_lean_planning_never_requests_a_standalone_test_creation_phase(self):
        prompt=lean_plan_prompt(LUNCH,normalize_coding_requirements(LUNCH),'MEDIUM')
        self.assertIn('same implementation phase',prompt)
        self.assertNotIn('second implementation phase',prompt)
        self.assertNotIn('up to 4 for separate test-code creation',prompt)

    def test_explicit_user_separation_of_test_phase_is_preserved(self):
        plan={'phases':[
            {'id':'app','goal':'Implement the application UI and logic','execution_mode':'IMPLEMENTATION'},
            {'id':'tests','goal':'Create tests','execution_mode':'IMPLEMENTATION','dependencies':['app']}],
            'file_manifest':[
                {'path':'index.html','action':'create','owner_phase':'app'},
                {'path':'app.js','action':'create','owner_phase':'app'},
                {'path':'tests/app.test.js','action':'create','owner_phase':'tests'}]}
        self.assertEqual([],validate_definition_steps(plan,'Build the app and create a separate test phase.'))

    def test_compaction_preserves_logic_and_test_code_creation(self):
        phases = [dict(id=f'p{i}', goal=goal, dependencies=[] if i == 0 else [f'p{i-1}']) for i, goal in enumerate(
            ['Create index.html', 'Implement lottery logic', 'Create lottery.test.js tests', 'Run tests'])]
        plan = {'phases':phases}
        goal = 'Create a static HTML website with randomized selections and tests.'
        self.assertEqual(plan, compact_normal_plan(plan, goal))

    def test_derived_graph_preserves_branch_dependencies(self):
        plan={'phases':[{'id':'base','dependencies':[]}, {'id':'ui','dependencies':['base']},
                        {'id':'logic','dependencies':['base']}, {'id':'test','dependencies':['ui','logic']}]}
        tasks=derive_task_graph(plan)['tasks']
        self.assertEqual(['base-task'],tasks[2]['depends_on'])
        self.assertEqual(['ui-task','logic-task'],tasks[3]['depends_on'])


if __name__ == '__main__':
    unittest.main()
