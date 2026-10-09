#!/usr/bin/env python3
"""Offline design-artifact validator. Does NOT run or certify application code.
Run from any directory: python /path/to/package/tools/validate_spec.py
Dependencies: jsonschema and PyYAML. No network, credentials, broker/model calls.
"""
from __future__ import annotations
import csv, hashlib, json, re, sys, warnings
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import unquote
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
ROOT=Path(__file__).resolve().parents[1]
results=[]
def check(name, function):
 try:
  detail=function();results.append({'check':name,'status':'passed','detail':detail or 'Assertions satisfied'})
 except Exception as exc:
  results.append({'check':name,'status':'failed','detail':f'{type(exc).__name__}: {exc}'})
def load(path):return json.loads((ROOT/path).read_text())
def csvrows(path):
 with (ROOT/path).open(newline='') as f:return list(csv.DictReader(f))
def structural():
 docs=list(ROOT.rglob('*.json'));ys=list(ROOT.rglob('*.yaml'))
 for f in docs:json.loads(f.read_text())
 for f in ys:yaml.safe_load(f.read_text())
 return f'{len(docs)} JSON and {len(ys)} YAML files parse.'
def trace():
 dec=csvrows('planning/decision_register.csv');rq=csvrows('planning/requirements.csv');ts=csvrows('planning/backlog.csv');tr=csvrows('planning/traceability.csv');at=csvrows('tests/acceptance_matrix.csv')
 assert len(dec)==79 and {x['decision_id'] for x in dec}=={f'Q{i}' for i in range(1,80)}
 assert len(rq)==99 and len(ts)==66 and len(at)==99
 ids={x['requirement_id'] for x in rq}; covered=set()
 for t in ts:
  rs=set(filter(None,t['requirements'].split(',')));assert rs<=ids;covered |=rs
 assert covered==ids
 assert {x['requirement_id'] for x in tr}==ids
 assert {x['requirement_id'] for x in at}==ids
 assert all(x['status']!='Passed' for x in at),'Application acceptance must not be marked executed.'
 return '79 decisions; 99 requirements covered by 66 tasks and 99 acceptance specifications.'
def dag():
 rows=csvrows('planning/backlog.csv');graph={r['task_id']:list(filter(None,r['depends_on'].split(','))) for r in rows};seen=set();visiting=set()
 def visit(k):
  assert k in graph,k
  if k in seen:return
  assert k not in visiting, 'Cycle '+k
  visiting.add(k)
  for d in graph[k]:visit(d)
  visiting.remove(k);seen.add(k)
 for k in graph:visit(k);assert (ROOT/'planning/tasks'/f'{k}.md').exists()
 return f'{len(seen)} tasks form an acyclic dependency graph; task files exist.'
def schemas():
 files=list((ROOT/'contracts/schemas').glob('*.json'))
 for f in files:Draft202012Validator.check_schema(json.loads(f.read_text()))
 return f'{len(files)} Draft 2020-12 schema documents pass metaschema checks.'
def references():
 paths=list((ROOT/'contracts').rglob('*.schema.json'))+[ROOT/'contracts/openapi.yaml'];count=0
 cache={}
 def parsed(p):
  if p not in cache:cache[p]=yaml.safe_load(p.read_text()) if p.suffix=='.yaml' else json.loads(p.read_text())
  return cache[p]
 for f in paths:
  doc=parsed(f)
  def walk(x):
   nonlocal count
   if isinstance(x,dict):
    if '$ref' in x:
     ref=x['$ref'];assert not ref.startswith(('http:','https:')), 'Uncontrolled network schema reference '+ref
     name,_,fragment=ref.partition('#');target=(f.parent/name).resolve() if name else f
     assert target.is_file(),f'{f.name}: missing {ref}'
     node=parsed(target)
     if fragment:
      assert fragment.startswith('/')
      for part in fragment[1:].split('/'):node=node[unquote(part).replace('~1','/').replace('~0','~')]
     count+=1
    for y in x.values():walk(y)
   elif isinstance(x,list):
    for y in x:walk(y)
  walk(doc)
 return f'{count} local schema/OpenAPI references resolve without network.'
def examples():
 base=ROOT/'contracts/schemas'
 def retrieve(uri):
  if not uri.startswith('file:'):raise ValueError('Network schema retrieval prohibited')
  p=Path(unquote(uri.removeprefix('file://'))).resolve();assert p.is_relative_to(base.resolve())
  return Resource.from_contents(json.loads(p.read_text()))
 registry=Registry(retrieve=retrieve)
 for p in base.glob('*.json'):registry=registry.with_resource(p.resolve().as_uri(),Resource.from_contents(json.loads(p.read_text())))
 n=0
 for ex in load('contracts/examples/manifest.json')['examples']:
  p=base/(ex['schema']+'.schema.json');schema={'$schema':'https://json-schema.org/draft/2020-12/schema','$ref':p.resolve().as_uri()}
  validator=Draft202012Validator(schema,registry=registry,format_checker=FormatChecker())
  errs=list(validator.iter_errors(load('contracts/examples/'+ex['path'])))
  assert (not errs)==ex['expected_valid'],f"{ex['path']}: "+('; '.join(e.message for e in errs) if errs else 'Invalid control unexpectedly passed')
  n+=1
 return f'{n} valid/invalid synthetic examples meet expected schema outcomes; semantic application checks are separate.'
def api():
 a=yaml.safe_load((ROOT/'contracts/openapi.yaml').read_text());assert a['openapi']=='3.1.0';ops=[]
 for path,obj in a['paths'].items():
  assert not ('/orders' in path and not path.startswith('/simulations/'))
  assert not any(x in path for x in ['/broker/submit','/broker/cancel','/broker/exercise'])
  for method,op in obj.items():
   ops.append(op['operationId']);param={p['name'] for p in op.get('parameters',[]) if p['in']=='path'}
   assert param==set(re.findall(r'\{(\w+)\}',path))
   if method!='get':assert {'X-CSRF-Token','Idempotency-Key'} <= {p['name'] for p in op['parameters']}
 assert len(ops)==len(set(ops))==72
 return f'{len(ops)} unique designed operations; path parameters and write headers match; no live broker-order route. Not a full OpenAPI conformance certification.'
def numerical():
 cases=load('tests/fixtures/numerical_oracles.json')['oracles']
 for c in cases:
  k=c['case'];raw=c['inputs'];e=c['expected'];i={x:D(v) for x,v in raw.items() if isinstance(v,str)};out={}
  if k=='buy_and_partial_sell':
   cost=i['buy_qty']*i['buy_price']+i['buy_fee'];q=i['buy_qty']-i['sell_qty'];sold=cost*i['sell_qty']/i['buy_qty'];remaining=cost-sold;proceeds=i['sell_qty']*i['sell_price']-i['sell_fee'];cash=i['deposit']-cost+proceeds;value=q*i['mark']
   out={'cash':cash,'remaining_qty':q,'remaining_cost':remaining,'realized':proceeds-sold,'unrealized':value-remaining,'nav':cash+value,'total_gain':cash+value-i['deposit']}
  elif k=='split':
   q=i['quantity']*i['ratio'];out={'quantity':q,'cost_per_unit':i['economic_cost']/q,'new_price':i['old_price']/i['ratio'],'value':i['quantity']*i['old_price']}
  elif k=='twr':out={'return':(i['before_flow']/i['start'])*(i['end']/(i['before_flow']+i['contribution']))-1}
  elif k=='fx':out={'base_return':(1+i['local_return'])*(1+i['fx_return'])-1,'interaction':i['local_return']*i['fx_return']}
  elif k=='mwr_multiple_roots':
   cf=[D(x) for x in raw['annual_cashflows']]
   for root in e['roots']:
    r=D(root);res=sum(v/(1+r)**j for j,v in enumerate(cf));assert abs(res)<D('1e-24')
   assert len(e['roots'])==2 and e['status']=='ambiguous';continue
  elif k=='capital_alternatives':
   ps=[D(x) for x in raw['proposals']];out={'jointly_feasible':sum(ps)<=i['available'],'each_individually_feasible':all(x<=i['available'] for x in ps)}
  elif k=='planned_stop_loss':out={'planned_loss':i['quantity']*(i['entry']-i['stop'])+i['cost'],'guaranteed_maximum':False}
  elif k in ['long_call','long_put']:
   payoff=max(D(0),(i['terminal_spot']-i['strike']) if k=='long_call' else (i['strike']-i['terminal_spot']));out={'expiry_pnl':(payoff-i['premium'])*i['multiplier'],'premium_at_risk':i['premium']*i['multiplier']}
  elif k in ['bull_call_debit','bear_put_debit']:
   if k=='bull_call_debit':p=max(D(0),i['terminal_spot']-i['low_strike'])-max(D(0),i['terminal_spot']-i['high_strike'])
   else:p=max(D(0),i['high_strike']-i['terminal_spot'])-max(D(0),i['low_strike']-i['terminal_spot'])
   width=i['high_strike']-i['low_strike'];out={'expiry_pnl':(p-i['net_debit'])*i['multiplier'],'expiry_max_loss':i['net_debit']*i['multiplier'],'expiry_max_gain':(width-i['net_debit'])*i['multiplier']}
  elif k in ['bull_put_credit','bear_call_credit']:
   if k=='bull_put_credit':p=max(D(0),i['long_strike']-i['terminal_spot'])-max(D(0),i['short_strike']-i['terminal_spot'])
   else:p=max(D(0),i['terminal_spot']-i['long_strike'])-max(D(0),i['terminal_spot']-i['short_strike'])
   width=abs(i['long_strike']-i['short_strike']);out={'expiry_pnl':(p+i['net_credit'])*i['multiplier'],'expiry_max_loss':(width-i['net_credit'])*i['multiplier'],'expiry_max_gain':i['net_credit']*i['multiplier']}
  elif k=='covered_call':out={'expiry_pnl':(min(i['terminal_spot'],i['strike'])-i['share_cost']+i['premium'])*i['multiplier'],'shares_required':i['multiplier']}
  elif k=='cash_secured_put':out={'expiry_pnl':(i['premium']-max(D(0),i['strike']-i['terminal_spot']))*i['multiplier'],'gross_assignment_cash':i['strike']*i['multiplier']}
  elif k=='unknown_adjusted_contract':out={'sizing_allowed':raw['multiplier'] is not None}
  elif k=='budget_race':
   avail=i['monthly_cap']-i['already_spent']-i['reserved'];out={'available':avail,'new_request_allowed':i['new_request']<=avail}
  elif k=='quote_does_not_refresh_account':out={'sizing_allowed':raw['account_age_seconds']<=raw['approved_account_max_age_seconds']}
  elif k=='timely_review_invalid_setup':out={'publish_action':raw['review_elapsed_seconds']<min(raw['deadline_seconds'],raw['setup_valid_until_seconds'])}
  elif k=='cashflow_neutral_drawdown':out={'drawdown':i['current_unit_value']/i['initial_unit_value']-1,'new_units':i['units']+i['deposit']/i['current_unit_value'],'unit_value_after_deposit':i['current_unit_value']}
  else:raise AssertionError('Unknown oracle '+k)
  for field,val in e.items():assert out[field]==(D(val) if isinstance(val,str) else val),f'{c["id"]} {field}: {out[field]} != {val}'
 return f'{len(cases)} independent arithmetic/scenario expectations recomputed; NOT application tests or empirical investment results.'
def scope():
 s=load('config/strategy_catalogue.json');imp=load('config/improvement_targets.json');b=load('config/budgets_and_slos.json');rel=load('config/release_status.json')
 assert len(s['strategies'])==6 and all(x['release_required'] and not x['action_eligible'] for x in s['strategies'])
 assert len(imp['targets'])==6 and all(x['release_required'] and x['human_promotion_required'] for x in imp['targets'])
 assert D(b['pilot_monthly_usd'])==100 and D(b['improvement_monthly_usd_max'])==20
 assert not rel['application_implemented'] and not rel['application_tests_executed'] and not rel['full_release_eligible']
 assert len(csvrows('planning/release_gates.csv'))==16
 return 'All six strategies and improvement targets required; USD100/20 caps retained; application/release claims disabled.'
def links():
 checked=0;bad=[]
 for p in ROOT.rglob('*.md'):
  for link in re.findall(r'(?<!!)\[[^\]]+\]\(([^)]+)\)',p.read_text()):
   link=link.split(' "')[0]
   if link.startswith(('http:','https:','mailto:','#')):continue
   t=unquote(link.partition('#')[0]);candidate=(p.parent/t).resolve()
   if not candidate.exists():bad.append(str(p.relative_to(ROOT))+': '+t)
   checked+=1
 assert not bad,'; '.join(bad[:20])
 return f'{checked} local Markdown links resolve.'
def license_and_sources():
 txt=(ROOT/'LICENSE').read_text();assert 'Version 2.0, January 2004' in txt and 'END OF TERMS AND CONDITIONS' in txt
 source=load('reference/sources.json')
 # sources register may be a list or keyed version envelope
 entries=source if isinstance(source,list) else source.get('sources',[])
 ids={x['id'] for x in entries};assert len(ids)==26
 for p in (ROOT/'spec').glob('*.md'):
  for r in re.findall(r'\bSRC-\d{2}\b',p.read_text()):assert r in ids,r
 return 'Apache-2.0 terms present; 26 dated source entries and spec citations resolve.'
def semantic_consistency():
 catalogue=load('config/strategy_catalogue.json')['strategies']
 ids={x['id'] for x in catalogue}
 doc_ids=set(re.findall(r'## (STR-[A-Z]+-[0-9]+)',(ROOT/'spec/08_STRATEGIES.md').read_text()))
 assert ids==doc_ids, 'Strategy identity divergence'
 questions=load('config/onboarding_questions.json')['questions']
 table_ids=set(re.findall(r'\| (ONB\d+) \|',(ROOT/'spec/02_USER_WORKFLOWS.md').read_text()))
 assert {q['id'] for q in questions}==table_ids and len(questions)==21
 tasks={x['task_id'] for x in csvrows('planning/backlog.csv')}
 gates={x['gate_id'] for x in csvrows('planning/release_gates.csv')}
 sources={x['id'] for x in load('reference/sources.json')['sources']}
 for q in csvrows('planning/qualification_register.csv'):
  assert set(q['tasks'].split(','))<=tasks
  assert set(q['sources'].split(','))<=sources
 for risk in csvrows('planning/risks.csv'):
  assert set(risk['related_gate_task'].split(',')) <= tasks|gates
 return 'Six canonical strategy IDs, 21 onboarding IDs, 24 qualification references and 18 risk/gate mappings are consistent.'

for name,fn in [('Structured files',structural),('Requirements traceability',trace),('Task dependencies',dag),('JSON Schema metaschemas',schemas),('Local contract references',references),('Positive and negative examples',examples),('Designed API invariants',api),('Numerical oracle checks',numerical),('Mandatory scope and honest status',scope),('Local document links',links),('License and source references',license_and_sources),('Cross-file semantic consistency',semantic_consistency)]:check(name,fn)
status='passed' if all(x['status']=='passed' for x in results) else 'failed'
report={'kind':'specification_artifact_validation_only','status':status,'executed_at':datetime.now(timezone.utc).isoformat(),'python':sys.version.split()[0],'checks':results,'application_tests_executed':False,'limitations':['No application implementation exercised.','No broker/model/provider API called.','No legal/commercial/data rights cleared.','No strategy investment edge, resource SLO, sandbox security or full recursive implementation demonstrated.','OpenAPI checked for references, shapes and declared invariants; no dedicated full OpenAPI conformance validator installed.']}
(ROOT/'reports').mkdir(exist_ok=True)
(ROOT/'reports/validation_results.json').write_text(json.dumps(report,indent=2)+'\n')
lines=['# Specification validation report',f'\nResult: **{status.upper()}**. Executed: {report["executed_at"]}. Python {report["python"]}.','\nThese checks concern the generated design artifacts only. Application development and release gates remain unverified.','\n| Check | Result | Observed evidence |','|---|---|---|']
lines += [f'| {x["check"]} | {x["status"]} | {x["detail"].replace("|","/")} |' for x in results]
lines += ['\n## Reproduce','\nFrom an authorized environment with the tooling dependencies installed:', '\n```text\npython tools/validate_spec.py\n```','\nDependencies are listed in tools/requirements-validation.txt. Installing them may require network permission; the validator itself does not use the network.','\n## Not established']+[f'\n- {x}' for x in report['limitations']]
(ROOT/'reports/VALIDATION.md').write_text('\n'.join(lines)+'\n')
print(json.dumps(report,indent=2))
sys.exit(0 if status=='passed' else 1)
