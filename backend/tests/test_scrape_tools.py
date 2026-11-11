import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from olcr_api.scrape_tools import EXTRACT_SCRIPT, tool_from_instruction
from olcr_api.scrape_table import transform_rows, xlsx_bytes
from olcr_api.recipes import ScrapeRecipeInput
from olcr_api.db import Database
from olcr_api.scrape_runs import ScrapeRunManager

ROOT = Path(__file__).resolve().parents[2]

class ScrapeToolsTests(unittest.TestCase):
    def test_real_chromium_extractors(self):
        runtime = ROOT / 'mcp-runtime/node'
        node = runtime / 'node/bin/node'
        executable = runtime / 'browser/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell'
        if not node.exists() or not executable.exists(): self.skipTest('prepared browser runtime unavailable')
        code = r'''
const {chromium} = require(process.argv[1]);
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.argv[2]});
 try {
 const page=await browser.newPage();const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
 await page.route('**/*',route=>route.fulfill({body:input.html,contentType:'text/html'}));
 await page.goto('https://fixture.example/catalog');
 const extract=cfg=>page.evaluate('('+input.script.replace('__OLCR_CONFIG__',JSON.stringify(cfg))+')()');
 const result={};for(const tool of ['list','page','text','emails','social','images'])result[tool]=await extract({tool,limit:100,record_selector:'auto'});
 result.selected=await extract({tool:'list',limit:100,record_selector:'auto',point:{x:50,y:70}});
 await page.setContent(`<main><section>${[
  ['横浜赤レンガ倉庫','神奈川県横浜市中区新港1-1','複合施設/商業施設'],
  ['東京ミッドタウン','東京都港区赤坂9-7-1','複合施設/商業施設'],
  ['六本木ヒルズ','東京都港区六本木6-10-1','複合施設/商業施設'],
  ['軽井沢・プリンスショッピングプラザ','長野県北佐久郡軽井沢町軽井沢','複合施設/商業施設'],
 ].map(([name,address,category])=>`<article><a href='/spot'>${name}</a><p>${address}</p><span>&lt;${category}&gt;</span></article>`).join('')}</section></main>`);
 result.named=await extract({tool:'list',limit:100,record_selector:'auto',fields:{facility_name:{selector:'*'},address:{selector:'*'},category:{selector:'*'}}});
 await page.setContent('<main><article><a href="/spot/1">Sample Facility</a></article><nav><a href="/catalog?page=2" aria-label="次のページ">次の20件</a></nav></main>');
 result.pagination=await extract({tool:'list',limit:100,record_selector:'auto'});
 await page.setContent('<main><article>Sample Facility</article><nav><a href="/catalog?page=1">1</a><a href="/catalog?page=2">2</a><a href="/catalog?page=3">3</a></nav></main>');
 result.numberedPagination=await extract({tool:'list',limit:100,record_selector:'auto'});
 await page.setContent('<main><section style="height:400px;overflow-y:auto">'+['A','B','C'].map(name=>`<article style="height:220px"><a>${name} Facility</a></article>`).join('')+'</section></main>');
 result.infiniteScroll=await extract({tool:'list',limit:100,record_selector:'auto'});
 await page.setContent('<pre>'+JSON.stringify({products:[{id:1,title:'Widget',variants:[{id:11,price:'3.50'},{id:12,price:'4'}]}]})+'</pre>');
 result.shopify=await extract({tool:'shopify',limit:100,record_selector:'body'});
 await page.setContent('<pre>&lt;urlset&gt;&lt;url&gt;&lt;loc&gt;https://fixture.example/a&lt;/loc&gt;&lt;/url&gt;&lt;/urlset&gt;</pre>');
 result.sitemap=await extract({tool:'sitemap',limit:100,record_selector:'body'});
 console.log(JSON.stringify(result));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''
        html = '''<html><head><title>Catalog</title><meta property="og:description" content="Public catalog"><script type="application/ld+json">{"@type":"Product","name":"Widget"}</script></head><body><main><ul style="margin:0;padding:0"><li style="height:90px"><a href="/a">Alpha</a><span>10</span><img data-src="/a.png" alt="Alpha photo"></li><li style="height:90px"><a href="/b">Beta</a><span>20</span></li><li style="height:90px"><a href="/c">Gamma</a><span>30</span></li></ul><p>hello@example.com</p><a href="mailto:HELLO@example.com">Email</a><a href="https://www.instagram.com/fixture">Instagram</a><div style="width:30px;height:30px;background-image:url('/bg.png')">Backdrop</div></main></body></html>'''
        completed = subprocess.run([str(node), '-e', code, str(runtime/'node_modules/playwright'), str(executable)], input=json.dumps({'html':html,'script':EXTRACT_SCRIPT}), text=True, capture_output=True, timeout=45)
        self.assertEqual(0, completed.returncode, completed.stderr)
        data = json.loads(completed.stdout)
        self.assertEqual(3, data['list']['record_count'])
        self.assertEqual('https://fixture.example/a', data['list']['rows'][0]['link_1'])
        self.assertEqual('10', data['list']['rows'][0]['text_1'])
        self.assertEqual(3, data['selected']['record_count'])
        self.assertEqual(3, len(data['selected']['record_rects']))
        self.assertEqual(4, data['named']['record_count'])
        self.assertEqual(1, len(data['pagination']['navigation_candidates']))
        self.assertEqual('next', data['pagination']['navigation_candidates'][0]['type'])
        self.assertEqual([{'type':'url_parameter','page_parameter':'page'}],
                         data['numberedPagination']['navigation_candidates'])
        self.assertEqual([{'type':'infinite_scroll'}], data['infiniteScroll']['navigation_candidates'])
        self.assertEqual({'page_url':'https://fixture.example/catalog','facility_name':'横浜赤レンガ倉庫',
                          'address':'神奈川県横浜市中区新港1-1','category':'複合施設/商業施設'},
                         data['named']['rows'][0])
        self.assertEqual({'facility_name':4,'address':4,'category':4},data['named']['field_match_counts'])
        self.assertEqual('長野県北佐久郡軽井沢町軽井沢', data['named']['rows'][3]['address'])
        self.assertEqual('Public catalog', data['page']['rows'][0]['description'])
        self.assertEqual('Widget', data['page']['rows'][0]['json_ld'][0]['name'])
        self.assertIn('Alpha', data['text']['rows'][0]['text'])
        self.assertEqual([{'page_url':'https://fixture.example/catalog','email':'hello@example.com'}], data['emails']['rows'])
        self.assertEqual('www.instagram.com/fixture', data['social']['rows'][0]['url'].split('://')[1])
        self.assertEqual({'https://fixture.example/a.png','https://fixture.example/bg.png'}, {r['image_url'] for r in data['images']['rows']})
        self.assertEqual(2, data['shopify']['record_count'])
        self.assertEqual('https://fixture.example/a', data['sitemap']['rows'][0]['url'])

    def test_image_download_reuses_pinned_public_http_and_preserves_failures(self):
        from unittest.mock import patch
        from email.message import Message
        from olcr_api.scrape_images import image_archive
        class Response:
            headers = Message()
            headers['Content-Type'] = 'image/png'
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def read(self, _limit): return b'png-fixture'
        class Opener:
            def open(self, request, timeout): return Response()
        with patch('olcr_api.scrape_images.validate_url', side_effect=[None, ValueError('PRIVATE_ADDRESS')]) as validate, patch('olcr_api.scrape_images._safe_opener', return_value=Opener()):
            data = image_archive([{'image_url':'https://images.example/a.png'}, {'image_url':'https://images.example/a.png'}, {'image_url':'http://127.0.0.1/private.png'}])
        self.assertEqual(2, validate.call_count)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(b'png-fixture', archive.read('image-0001.png'))
            self.assertEqual(['SAVED', 'DOWNLOAD_FAILED'], [r['status'] for r in json.loads(archive.read('manifest.json'))])

    def test_recipe_roundtrip_and_url_validation(self):
        recipe = ScrapeRecipeInput(name='Emails', start_url='https://one.example/', record_selector='body', tool='emails', urls=['https://one.example/','https://two.example/'], page_delay_seconds=3)
        self.assertEqual(recipe, ScrapeRecipeInput.from_record(recipe.to_record('recipe')))
        with self.assertRaises(ValueError): ScrapeRecipeInput(name='bad',start_url='https://one.example/',record_selector='body',tool='page',urls=['https://user:secret@two.example/'])
        with self.assertRaises(ValueError): ScrapeRecipeInput(name='bad',start_url='https://one.example/',record_selector='body',tool='page',urls=['https://two.example/'], navigation={'type':'infinite_scroll'})
        self.assertEqual('emails',tool_from_instruction('メールを抽出'))
        directory = ScrapeRecipeInput(name='Directory',start_url='https://fixture.example/',
            record_selector='article.spot',tool='list',fields={
                'facility_name':{'selector':'*','extract':'text'},
                'address':{'selector':'*','extract':'text'},
                'category':{'selector':'*','extract':'text'},
            })
        self.assertEqual(set(directory.fields), {'facility_name','address','category'})
        with self.assertRaises(ValueError):
            ScrapeRecipeInput(name='Bad',start_url='https://fixture.example/',
                record_selector='article.spot',tool='list',fields={'name':{'selector':'h2'}})

    def test_table_atomicity_search_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(str(Path(directory)/'data.db')); db.initialize()
            recipe=ScrapeRecipeInput(name='List',start_url='https://fixture.example/',record_selector='auto',tool='list').to_record('r')
            db.save_web_recipe(recipe); db.create_web_scrape_run('run', recipe); db.update_web_scrape_run('run','RUNNING')
            db.checkpoint_web_scrape_page('run',1,[(None,{'title':'Alpha','price':'10'}),(None,{'title':'Beta','price':'invalid'})],'hash',None,discovered_count=2)
            with self.assertRaises(ValueError): db.edit_scrape_table('run','rename','title','name')
            db.update_web_scrape_run('run','COMPLETED')
            original_ids=[r['id'] for r in db.scrape_table_page('run')['rows']]
            with self.assertRaises(ValueError): db.edit_scrape_table('run','number','price')
            self.assertEqual('10',db.scrape_table_page('run')['rows'][0]['values']['price'])
            db.edit_scrape_table('run','rename','title','name')
            page=db.scrape_table_page('run',search='beta')
            self.assertEqual(1,page['total']);self.assertEqual('Beta',page['rows'][0]['values']['name'])
            self.assertEqual(original_ids,[r['id'] for r in db.scrape_table_page('run')['rows']])
            db.edit_scrape_table('run','cell','price',value='20',row_id=page['rows'][0]['id']);db.edit_scrape_table('run','number','price')
            with zipfile.ZipFile(io.BytesIO(xlsx_bytes(db.stream_web_scrape_rows('run'),['name','price']))) as archive:
                xml=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
                self.assertEqual(3,len(xml.find('{*}sheetData')))
            db.edit_scrape_table('run','drop','price')
            self.assertEqual(['name'],db.scrape_table_page('run')['columns'])

    def test_transform_deduplication_and_formula_safe_excel(self):
        rows=[{'x':'=SUM(A1)'},{'x':'=SUM(A1)'},{'x':''}]
        self.assertEqual(2,len(transform_rows(rows,'deduplicate')))
        self.assertEqual(2,len(transform_rows(rows,'drop_empty')))
        self.assertEqual([{'b':2,'a':1}],transform_rows([{'a':1,'b':2}], 'reorder',value='["b","a"]'))
        with self.assertRaises(ValueError): transform_rows([{'a':1}], 'reorder',value='["b"]')
        with zipfile.ZipFile(io.BytesIO(xlsx_bytes(rows,['x']))) as archive:
            xml=archive.read('xl/worksheets/sheet1.xml')
            self.assertNotIn(b'<f>',xml); self.assertIn(b'=SUM(A1)',xml)
