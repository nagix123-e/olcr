import tempfile
import unittest
import io
import json
import hashlib
import base64
import zipfile
import stat
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from olcr_api.custom_agents import AgentDefinition, AgentStore
from olcr_api.agent_packages import AgentPackage, export_package, import_package, export_zip, import_zip
from olcr_api.custom_agent_api import register_agent_routes
from olcr_api.db import Database
from olcr_api.agent_knowledge import read_file, reader_path

def pdf_bytes():
    stream=b'BT /F1 18 Tf 40 100 Td (Original PDF knowledge) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    out=b'%PDF-1.4\n'; offsets=[]
    for n,obj in enumerate(objects,1):
        offsets.append(len(out));out+=str(n).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    start=len(out);out+=b'xref\n0 6\n0000000000 65535 f \n'
    out+=b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets)
    return out+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n'


class AgentPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / 'agents.db')); self.db.initialize()
        self.store = AgentStore(self.db)

    def tearDown(self): self.tmp.cleanup()

    def test_round_trip_preserves_every_definition_field_and_delegate_graph(self):
        child = self.store.save(AgentDefinition(name='子', instructions='Child prompt'))
        value = AgentDefinition(name='日本語 / Agent', description='Description', icon='data:image/png;base64,AA==',
                                instructions='Line 1\n秘密の資料を参考に答えてください。', preferred_router='planner_replanner',
                                preferred_model='unavailable-model', knowledge=[{'name':'資料.txt','content':'日本語\n内容'}],
                                tools=['calculator'], skills=['local-skill'], subagent_ids=[child['id']])
        original = self.store.save(value)
        package = AgentPackage.model_validate_json(export_package(self.store, original['id']).model_dump_json())
        result = import_package(self.store, package)
        restored = result['agent']
        self.assertEqual(2, result['imported_count'])
        self.assertNotEqual(original['id'], restored['id'])
        for field in AgentDefinition.model_fields:
            if field != 'subagent_ids': self.assertEqual(original[field], restored[field], field)
        copied_child = self.store.get(restored['subagent_ids'][0])
        self.assertNotEqual(child['id'], copied_child['id'])
        self.assertEqual(child['instructions'], copied_child['instructions'])
        self.assertEqual(1, self.store.get(original['id'])['revision'])
        self.assertEqual(4, len(self.store.list()))

    def test_invalid_bundle_is_rejected_without_partial_writes(self):
        value = {'format':'olcr.agent','version':1,'root_id':'a', 'agents':[{'id':'a','definition':{'name':'A','subagent_ids':['missing']}}]}
        for mutate in [lambda x:x, lambda x:{**x,'version':2}, lambda x:{**x,'execute':'shell'}]:
            with self.assertRaises(ValidationError): AgentPackage.model_validate(mutate(value))
        self.assertEqual([], self.store.list())

    def test_export_missing_delegate_is_explicit_error(self):
        agent = self.store.save(AgentDefinition(name='A',subagent_ids=['missing']))
        with self.assertRaisesRegex(ValueError, 'UNAVAILABLE'): export_package(self.store, agent['id'])

    def test_http_export_and_import_are_available_without_model_runtime(self):
        app=FastAPI(); register_agent_routes(app,lambda:{'db':self.db})
        client=TestClient(app)
        original=self.store.save(AgentDefinition(name='Agent',instructions='Prompt',knowledge=[{'name':'k','content':'Knowledge'}]))
        response=client.get(f"/api/agents/{original['id']}/export")
        self.assertEqual(200,response.status_code)
        self.assertIn('attachment;',response.headers['content-disposition'])
        self.assertEqual(200,client.post('/api/agents/import-zip',content=response.content,headers={'Content-Type':'application/zip'}).status_code)
        self.assertEqual(422,client.post('/api/agents/import',json={'format':'wrong'}).status_code)
        self.assertEqual(404,client.get('/api/agents/missing/export').status_code)

    def test_zip_contains_original_csv_and_definition_with_file_references(self):
        original=b'\xef\xbb\xbfitem,value\r\nJapanese,42\r\n'
        agent=self.store.save(AgentDefinition(name='CSV',knowledge=[read_file('table.csv',original)]))
        data=export_zip(self.store,agent['id'])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest=json.loads(archive.read('.olcr-agent.json'))
            reference=manifest['agents'][0]['definition']['knowledge'][0]
            self.assertNotIn('file_base64',reference)
            self.assertEqual(original,archive.read(reference['path']))
        restored=import_zip(self.store,data)['agent']
        self.assertEqual(original,base64.b64decode(restored['knowledge'][0]['file_base64']))

    def test_pdf_and_png_are_restored_as_original_bytes_with_extracted_text(self):
        try: reader_path()
        except ValueError: self.skipTest('Native knowledge reader is not built')
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aCuoAAAAASUVORK5CYII=')
        knowledge=[read_file('reference.pdf',pdf_bytes()),read_file('picture.png',png)]
        self.assertIn('Original PDF knowledge',knowledge[0]['content'])
        original=self.store.save(AgentDefinition(name='Files',knowledge=knowledge))
        restored=import_zip(self.store,export_zip(self.store,original['id']))['agent']
        self.assertEqual(pdf_bytes(),base64.b64decode(restored['knowledge'][0]['file_base64']))
        self.assertEqual(png,base64.b64decode(restored['knowledge'][1]['file_base64']))

    def corrupt_zip(self,transform):
        original=self.store.save(AgentDefinition(name='A',knowledge=[read_file('k.txt',b'knowledge')]))
        with zipfile.ZipFile(io.BytesIO(export_zip(self.store,original['id']))) as archive:
            files={name:archive.read(name) for name in archive.namelist()}
        transform(files)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            for name,data in files.items(): archive.writestr(name,data)
        return output.getvalue()

    def test_hash_mismatch_is_rejected_before_any_write(self):
        data=self.corrupt_zip(lambda files:files.__setitem__('knowledge/0/0.txt',b'changed'))
        before=len(self.store.list())
        with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'): import_zip(self.store,data)
        self.assertEqual(before,len(self.store.list()))

    def test_type_mismatch_is_rejected_even_with_correct_hash(self):
        def change(files):
            manifest=json.loads(files['.olcr-agent.json']);item=manifest['agents'][0]['definition']['knowledge'][0]
            item['name']='fake.pdf';item['mime_type']='application/pdf'
            files['.olcr-agent.json']=json.dumps(manifest).encode()
        with self.assertRaisesRegex(ValueError,'TYPE_MISMATCH'): import_zip(self.store,self.corrupt_zip(change))

    def test_traversal_and_unexpected_files_are_rejected(self):
        for name in ['../outside.txt','/outside.txt','knowledge/extra.txt']:
            data=self.corrupt_zip(lambda files:files.__setitem__(name,b'bad'))
            before=len(self.store.list())
            with self.assertRaises(ValueError):import_zip(self.store,data)
            self.assertEqual(before,len(self.store.list()))

    def test_invalid_zip_and_missing_manifest_are_rejected(self):
        for data in [b'not a zip',b'PK\x03\x04invalid']:
            with self.assertRaises(ValueError):import_zip(self.store,data)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:archive.writestr('other.json','{}')
        with self.assertRaises(ValueError):import_zip(self.store,output.getvalue())

    def test_symlink_member_is_rejected(self):
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('.olcr-agent.json','{}')
            entry=zipfile.ZipInfo('knowledge/link');entry.external_attr=(stat.S_IFLNK|0o777)<<16
            archive.writestr(entry,'/etc/passwd')
        with self.assertRaisesRegex(ValueError,'UNSAFE_MEMBER'):import_zip(self.store,output.getvalue())
