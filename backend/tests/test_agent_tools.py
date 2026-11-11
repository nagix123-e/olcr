import json
import tempfile
import unittest
from pathlib import Path
from olcr_api.db import Database
from olcr_api.agent_tools import ToolStore

class ReusableToolTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.db=Database(str(Path(self.tmp.name)/'db'));self.db.initialize();self.store=ToolStore(self.db)
  self.recipe={'name':'Normalize text','description':'Lowercase text','parameters':[{'name':'text','type':'string','description':'Text'}],
    'steps':[{'tool_id':'lowercase','arguments_json':'{"text":"${inputs.text}"}'}]}
 def test_persist_catalog_execute_and_archive(self):
  tool=self.store.save(self.recipe)
  from olcr_api.tools import registry
  self.assertEqual(ToolStore(self.db).catalog()[0]['schema']['properties']['text']['type'],'string')
  result=self.store.execute(tool['id'],{'text':'HELLO'},lambda key,args:registry()[key].run(args)[0])
  self.assertEqual(result['result'],{'text':'hello'})
  self.store.archive(tool['id'])
  with self.assertRaises(ValueError):self.store.execute(tool['id'],{'text':'A'},lambda *args:None)
 def test_forbids_mutations_recursion_unknown_inputs_and_forward_refs(self):
  for key,args in [('email_send','{}'),('shell','{}'),('recipe.other','{}'),('lowercase','{"text":"${inputs.unknown}"}'),('lowercase','{"text":"${steps.0.text}"}')]:
   with self.assertRaises(ValueError):self.store.save({**self.recipe,'steps':[{'tool_id':key,'arguments_json':args}]})
 def test_prior_step_output_is_typed_and_arguments_bounded(self):
  tool=self.store.save({**self.recipe,'steps':self.recipe['steps']+[{'tool_id':'lowercase','arguments_json':'{"text":"${steps.0.text}"}'}]})
  from olcr_api.tools import registry
  self.assertEqual(self.store.execute(tool['id'],{'text':'A'},lambda key,args:registry()[key].run(args)[0])['steps_completed'],2)
  with self.assertRaises(ValueError):self.store.execute(tool['id'],{'text':False},lambda *args:None)
 def test_custom_agent_catalog_and_definition_allow_recipe(self):
  tool=self.store.save(self.recipe)
  from olcr_api.custom_agents import tool_catalog
  self.assertIn(tool['id'],{r['id'] for r in tool_catalog(self.db)})

 def test_agent_runtime_executes_only_selected_recipe_and_existing_permissions(self):
  from types import SimpleNamespace
  from olcr_api.custom_agent_runtime import AgentRuntime
  from olcr_api.models import Task
  from olcr_api.config import Settings
  tool=self.store.save(self.recipe)
  runner=AgentRuntime(self.db,None,Settings(external_access_enabled=False),None,None)
  result=runner.tool({'tool_id':tool['id'],'args':{'text':'HELLO'}},{'tools':[tool['id']]},Task('recipe'),None)
  self.assertEqual(result['result']['text'],'hello')
  with self.assertRaises(ValueError):runner.tool({'tool_id':tool['id'],'args':{'text':'A'}},{'tools':[]},Task('denied'),None)
  external=self.store.save({'name':'Weather','description':'Weather lookup','parameters':[{'name':'location','type':'string'}],
    'steps':[{'tool_id':'knowledge.wikidata','arguments_json':'{"location":"${inputs.location}"}'}]})
  with self.assertRaises(ValueError):runner.tool({'tool_id':external['id'],'args':{'location':'Kyoto'}},{'tools':[external['id']]},Task('offline'),None)

 def test_reusable_calculator_preserves_whole_number_zeroes(self):
  from olcr_api.tools import registry
  recipe=self.store.save({'name':'Multiply','description':'Multiply a numeric expression','parameters':[],
    'steps':[{'tool_id':'calculator','arguments_json':'{"expression":"5*2"}'}]})
  self.assertEqual(self.store.execute(recipe['id'],{},lambda key,args:registry()[key].run(args)[0])['result']['result'],'10')
