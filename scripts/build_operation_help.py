"""Export the reviewed, bilingual operation guide as a relocatable Graphify snapshot.

This is curated Markdown extraction, not inferred code/LLM extraction.
"""
import hashlib
import json
from pathlib import Path


def build(root: Path):
    topics=json.loads((root/'topics.json').read_text())
    hashes={'topics.json':hashlib.sha256((root/'topics.json').read_bytes()).hexdigest()}
    nodes=[];links=[]
    for topic in topics:
        for language,tag in [('ja','japanese'),('en','english')]:
            source=f"{topic['id']}-{language}.md"
            hashes[source]=hashlib.sha256((root/source).read_bytes()).hexdigest()
            key=topic['id']+tag
            nodes.append({'id':key,'label':key+' '+topic['titles'][language], 'source_file':source,'source_location':'L1','file_type':'document'})
            if topic['id']!='overview':
                links.append({'source':'overview'+tag,'target':key,'relation':'documents','confidence':'EXTRACTED','source_file':'overview-'+language+'.md'})
    nodes.append({'id':'operationhelpindex','label':'Operation guide question and procedure index',
                  'source_file':'topics.json','source_location':'L1','file_type':'document'})
    for topic in topics:
        if topic.get('parent'):
            for tag in ('japanese','english'):
                links.append({'source':topic['parent']+tag,'target':topic['id']+tag,
                              'relation':'has_procedure','confidence':'EXTRACTED','source_file':'topics.json'})
    data={'graph':{'olcr':{'contract':'olcr-graphify-v1','workspace_root':'__OLCR_BUNDLED_HELP__','source_hashes':hashes,
          'graphify_version':'curated-help-v1','extraction':'reviewed_markdown_topics','reviewed':'2026-10-29'}},'nodes':nodes,'links':links}
    destination=root/'graphify-out';destination.mkdir(exist_ok=True)
    (destination/'graph.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(f'Operation help: {len(topics)} topics, {len(nodes)} nodes, {len(links)} links')

if __name__=='__main__':
    build(Path(__file__).resolve().parents[1]/'backend/olcr_api/help_knowledge')
