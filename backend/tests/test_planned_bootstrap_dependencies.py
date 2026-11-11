import copy
import unittest
from olcr_api.coding_tasks import associate_dependency_requirements, dependency_requirement_specs, phase_executor_capability

class PlannedBootstrapTests(unittest.TestCase):
    def plan(self):
        return {'file_manifest':[{'path':p,'action':'create'} for p in ['package.json','vite.config.ts','tsconfig.json','src/main.tsx','src/App.tsx','src/planner.test.ts']],
                'assumptions':['React + TypeScript + Vite + Tailwind CSS'],
                'phases':[{'id':'p1','execution_mode':'IMPLEMENTATION_AND_VERIFICATION','requires_dependency_installation':True},
                          {'id':'p2','execution_mode':'VERIFICATION_ONLY'}]}
    def test_new_project_plan_gets_executable_bounded_contract(self):
        plan,changes=associate_dependency_requirements(self.plan(),{'new_project_intent':True,'dependency_requirements':[]})
        self.assertTrue(changes)
        specs=dependency_requirement_specs(plan['phases'][0]['dependency_requirements'])
        self.assertEqual({'react','react-dom','@types/react','@types/react-dom','vite','@vitejs/plugin-react','typescript','tailwindcss','@tailwindcss/vite','vitest'},set(specs))
        self.assertTrue(phase_executor_capability({**plan['phases'][0],'package_manager_executor':'AUTHORIZED_PACKAGE_MANAGER','authorized_package_requirements':specs})['executable'])
        self.assertNotIn('dependency_requirements',plan['phases'][1])
        again,changes=associate_dependency_requirements(plan,{'new_project_intent':True})
        self.assertEqual(plan,again);self.assertEqual([],changes)
    def test_existing_restricted_or_incomplete_projects_do_not_gain_authorization(self):
        for req in ({}, {'new_project_intent':False},{'new_project_intent':True,'dependency_policy':'MINIMAL'}, {'new_project_intent':True,'forbidden_stack':['React']}):
            plan=self.plan();self.assertEqual((plan,[]),associate_dependency_requirements(plan,req))
        plan=self.plan();plan['file_manifest'][0]['action']='modify'
        self.assertEqual((plan,[]),associate_dependency_requirements(plan,{'new_project_intent':True}))
    def test_arbitrary_packages_in_plan_text_are_not_authorized(self):
        plan=self.plan();plan['assumptions'].append('npm install arbitrary-package')
        normalized,_=associate_dependency_requirements(plan,{'new_project_intent':True})
        self.assertNotIn('arbitrary-package',dependency_requirement_specs(normalized['phases'][0]['dependency_requirements']))
