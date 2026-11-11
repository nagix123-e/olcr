#!/usr/bin/env python3
"""Check visible numeric claims against captured production API evidence.

Checks arithmetic, observation periods, exact geographic identities and source
citations in snapshot tables. This is supporting evidence for a case review;
completion and numeric matches never constitute a universal semantic PASS.
"""
import argparse,gzip,json,re,sys
from pathlib import Path
from decimal import Decimal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from olcr_api.analysis_semantics import records,dimensions,metric_id
from olcr_api.analysis_summary import normalize_observation
from olcr_api.analysis_scope import observation_country
N=r'[-+−]?\d+(?:\.\d+)?(?:e[-+]?\d+)?'
trend=re.compile(r'^(?P<subject>.+)では(?P<area>.+?)の値が(?P<p1>.+?)の(?P<v1>'+N+r')(?P<u1>.*?)から(?P<p2>.+?)の(?P<v2>'+N+r')(?P<u2>.*?)へ(?P<direction>上昇|低下|横ばい)しています（(?P<delta>'+N+r')(?P<du>.*?)）')
def flatten(e):
 d=e.get('data') or {}; rows=d.get('items') or []
 if d.get('render_nested_evidence') and rows and all(isinstance(r,dict) and isinstance(r.get('data'),dict) for r in rows):
  return [leaf for r in rows for leaf in flatten({**e,'data':r['data'],'sources':r.get('sources') or e.get('sources',[])})]
 return [e]
def canon(v):
 if isinstance(v,dict):v=v.get('value') or v.get('id')
 v=str(v or '')
 if ' → ' in v:return tuple(canon(s) for s in v.split(' → '))
 found=observation_country(v)
 return tuple(sorted(found)) if found else v

def period(row):
 v=row.get('date') or row.get('year') or row.get('period')
 if row.get('year') and re.fullmatch(r'M(?:0[1-9]|1[0-2])', str(row.get('period') or '')):v=f"{row['year']}-{row['period'][1:]}"
 s=str(v)
 if s.endswith('.0'):s=s[:-2]
 return s

def d(v):return Decimal(str(v).replace('−','-'))
def near(raw,shown,ratio=False):
 tolerance=Decimal('0.00500001') if not ratio else max(abs(raw)*Decimal('0.000000051'),Decimal('1e-15'))
 return abs(raw-shown)<=tolerance

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('evidence',type=Path)
parser.add_argument('output',type=Path)
args=parser.parse_args()
source=args.evidence; output=args.output

def check_snapshots(record):
 from urllib.parse import urlsplit
 aliases={'united states of america':'united states', 'south korea':'korea, rep.'}
 def label(row):
  area=row.get('geo_area') or row.get('country') or ''
  return str(area.get('value') or area.get('id') or '') if isinstance(area,dict) else str(area)
 def name_key(value):
  raw=str(value).casefold().strip()
  return aliases.get(raw,raw)
 def explicit_code(row):
  for key in ('countryiso3code','country_code'):
   v=str(row.get(key) or '').upper()
   if re.fullmatch('[A-Z]{3}',v):return 'ISO3:'+v
  a=row.get('country')
  v=str(a.get('id') or '') if isinstance(a,dict) else str(a or '')
  if re.fullmatch('[A-Z]{3}',v):return 'ISO3:'+v
  return None
 def url_key(value):
  u=urlsplit(str(value or ''))
  return (u.netloc.casefold(),u.path.rstrip('/'))
 registry={}; raw_rows=[]
 for e in record['results']:
  for leaf in flatten(e):
   data=leaf.get('data') or {}
   sources={url_key(s.get('canonical_url') or s.get('url') or s.get('page_url')) for s in leaf.get('sources',[]) if isinstance(s,dict)}
   for raw in records(data):
    row=normalize_observation(raw,leaf.get('tool_id'))
    try:value=d(row.get('value'))
    except Exception:continue
    if not value.is_finite():continue
    code=explicit_code(row)
    if code:registry.setdefault(name_key(label(row)),set()).add(code)
    raw_rows.append((row,data,sources,value,code))
 def identity(value,code=None):
  if code:return code
  if re.fullmatch('[A-Z]{3}',str(value)):return 'ISO3:'+str(value)
  codes=registry.get(name_key(value),set())
  return next(iter(codes)) if len(codes)==1 else 'LABEL:'+name_key(value)
 measures=[('実質GDP成長率','NY.GDP.MKTP.KD.ZG'),('1人あたり名目GDP','NY.GDP.PCAP.CD'),
  ('一般政府総債務','GGXWDG_NGDP'),('消費者物価上昇率','FP.CPI.TOTL.ZG'),('失業率（ILO','SL.UEM.TOTL.ZS'),
  ('難民数','refugees'),('庇護申請者数','asylum_seekers'),('国内避難民数','idps'),('総人口','SP.POP.TOTL'),
  ('電力アクセス率','EG.ELC.ACCS.ZS'),('電力発電量に占める再生可能','EG.ELC.RNEW.ZS'),
  ('最終エネルギー消費に占める再生可能','EG.FEC.RNEW.ZS'),('温室効果ガス全体の排出量（Mt','EN.GHG.ALL.MT.CE.AR5'),
  ('温室効果ガス全体の排出量（t','EN.GHG.ALL.PC.CE.AR5'),('CO₂排出量（Mt','EN.GHG.CO2.MT.CE.AR5'),
  ('CO₂排出量（t','EN.GHG.CO2.PC.CE.AR5'),('利用可能な淡水資源','ER.H2O.FWST.ZS'),('農林水産業','NV.AGR.TOTL.ZS'),
  ('家庭用電力価格','electricity_prices'),('都道府県別の総人口','jp_regional_population'),
  ('都道府県別の就業者数','jp_regional_employment'),('国際観光客到着数','ST.INT.ARVL'),
  ('登録航空会社の年間出発便数','IS.AIR.DPRT'),('1人あたりエネルギー使用量','EG.USE.PCAP.KG.OE'),
  ('年平均PM2.5曝露濃度','EN.ATM.PM25.MC.M3')]
 cells=[]; pending_period=None; headers=None
 body=(record.get('response') or '').split('## 分析',1)[-1]
 for line in body.splitlines():
  matched=re.match(r'同一対象・同一期間で揃えた比較（([^、]+)',line)
  if matched:pending_period=matched[1];headers=None;continue
  if pending_period and line.strip() and not line.startswith('|'):
   pending_period=None;headers=None
  if not pending_period or not line.startswith('|'):continue
  parts=[p.strip() for p in line.split('|')[1:-1]]
  if not parts:continue
  if parts[0]=='国・地域':headers=parts[1:];continue
  if parts[0].startswith('---') or not headers:continue
  area=identity(parts[0])
  for header,cell in zip(headers,parts[1:]):
   metric=next((code for title,code in measures if header.startswith(title)),None)
   if metric is None:
    cells.append({'area':parts[0],'period':pending_period,'header':header,'cell':cell,'supported':None,'reason':'metric_requires_manual_review'});continue
   cite=re.search(r'https?://[^\s)]+',header)
   url=url_key(cite[0]) if cite else None
   number=re.match(N,cell)
   wanted=d(number[0]) if number else None
   candidates=[]
   for row,data,urls,value,code in raw_rows:
    if identity(label(row),code)!=area or period(row)!=pending_period or metric_id(row,data)!=metric:continue
    if url and url not in urls:continue
    dims=dimensions(row,data)
    if any((key+'=') in header and (key+'='+str(v)) not in header for key,v in dims.items()):continue
    candidates.append(value)
   supported=wanted is not None and any(near(value,wanted,'/' in cell) for value in candidates)
   cells.append({'area':parts[0],'period':pending_period,'metric':metric,'cell':cell,'supported':supported,
                 'source_candidates':[str(v) for v in candidates]})
 return cells

def check_composition(record):
 groups={}
 for evidence in record['results']:
  for leaf in flatten(evidence):
   if leaf.get('tool_id')!='trade.un_comtrade':continue
   for row in records(leaf.get('data') or {}):
    key=tuple(str(row.get(k) or '') for k in ('reporter','partner','year','flow_code'))
    try:value=d(row.get('trade_value_usd'))
    except Exception:continue
    groups.setdefault(key,{}).setdefault(str(row.get('commodity_code') or ''),set()).add(value)
 checks=[]; active=None
 heading=re.compile(r'^品目別構成（(.+?) → (.+?)、(\d{4})年、(.+?)、分母TOTAL ('+N+r') US\$）')
 for line in (record.get('response') or '').splitlines():
  m=heading.match(line)
  if m:
   active=(tuple(m.groups()[:4]),d(m[5]));continue
  if not active or not line.startswith('|'):continue
  parts=[s.strip() for s in line.split('|')[1:-1]]
  if len(parts)!=4 or not re.fullmatch(r'\d{2}',parts[0]):continue
  values=groups.get(active[0],{})
  try:
   total=active[1]; amount=d(parts[2]); percent=d(parts[3])
   supported=(total>0 and values.get('TOTAL')=={total} and values.get(parts[0])=={amount}
              and near(amount/total*100,percent))
  except Exception:supported=False
  checks.append({'scope':list(active[0]),'commodity_code':parts[0],'supported':supported})
 return checks

reviews=[]
raw_text=gzip.decompress(source.read_bytes()).decode('utf8') if source.suffix=='.gz' else source.read_text()
for line in raw_text.splitlines():
 record=json.loads(line); groups={}
 for e in record['results']:
  for leaf in flatten(e):
   data=leaf.get('data') or {}
   for raw in records(data):
    r=normalize_observation(raw,leaf.get('tool_id'))
    try:
     value=d(r.get('value'))
     if not value.is_finite():continue
    except Exception:continue
    area=canon(r.get('geo_area') or r.get('country') or r.get('countryiso3code') or '対象地域')
    key=(str(leaf.get('tool_id')),metric_id(r,data),area,json.dumps(dimensions(r,data),sort_keys=True,ensure_ascii=False))
    groups.setdefault(key,{}).setdefault(period(r),[]).append(value)
 claims=[]
 body=record.get('response') or ''
 if '## 分析' in body:body=body.split('## 分析',1)[1]
 for text in body.splitlines():
  m=trend.match(text)
  if not m:continue
  c=m.groupdict(); p1,p2=c['p1'].removesuffix('年'),c['p2'].removesuffix('年'); area=canon(c['area']);ratio='/' in c['u1']
  candidates=[]
  for key,values in groups.items():
   if key[2]!=area:continue
   for first in values.get(p1,[]):
    for last in values.get(p2,[]):
     if near(first,d(c['v1']),ratio) and near(last,d(c['v2']),ratio):candidates.append((key,first,last))
  match=next(((key,a,b) for key,a,b in candidates if near(b-a,d(c['delta']),ratio)
              and c['direction']==('上昇' if b>a else '低下' if b<a else '横ばい')),None)
  claims.append({'text':m.group(0),'supported_operands_and_delta':bool(match),
                 'matched_series':list(match[0][:3]) if match else None})
 snapshots=check_snapshots(record)
 composition=check_composition(record)
 reviews.append({'composition_cells':composition,'composition_failures':sum(not c['supported'] for c in composition),'snapshot_cells':snapshots,'snapshot_failures':sum(cell['supported'] is False for cell in snapshots),'task_id':record['task_id'],'prompt':record['prompt'],'quantitative_trends_checked':len(claims),
                 'quantitative_failures':sum(not c['supported_operands_and_delta'] for c in claims), 'claims':claims,
                 'semantic_pass':None})
output.write_text(json.dumps(reviews,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'cases':len(reviews),'trend_claims_checked':sum(r['quantitative_trends_checked'] for r in reviews),
                  'trend_claim_failures':sum(r['quantitative_failures'] for r in reviews),
                  'snapshot_cells_checked':sum(c['supported'] is not None for r in reviews for c in r['snapshot_cells']),
                  'snapshot_cells_requiring_manual_review':sum(c['supported'] is None for r in reviews for c in r['snapshot_cells']),
                  'snapshot_cell_failures':sum(r['snapshot_failures'] for r in reviews),
                  'composition_cells_checked':sum(len(r['composition_cells']) for r in reviews),
                  'composition_failures':sum(r['composition_failures'] for r in reviews),
                  'snapshot_failure_cases':[{'prompt':r['prompt'],'cells':[c for c in r['snapshot_cells'] if c['supported'] is False]} for r in reviews if r['snapshot_failures']],
                  'failure_cases':[{'prompt':r['prompt'],'failures':[c['text'] for c in r['claims'] if not c['supported_operands_and_delta']]} for r in reviews if r['quantitative_failures']]},ensure_ascii=False))
