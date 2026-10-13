"""Exercise the actual stdio/process boundary without model or provider calls."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('codex_bridge',ROOT/'scripts/codex-bridge.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
FAKE=r'''
import json,sys,time,subprocess
from pathlib import Path
scenario,record=sys.argv[1:]
def send(row):print(json.dumps(row),flush=True)
for line in sys.stdin:
 row=json.loads(line);method=row.get('method');ident=row.get('id');p=row.get('params',{})
 with open(record,'a') as f:f.write(json.dumps({'method':method,'params':p})+'\n')
 if method=='initialize':value={}
 elif method=='initialized':continue
 elif method=='account/read':value={'account':{'type':'apiKey' if scenario=='apikey' else 'chatgpt','email':'private@example.invalid','planType':'plus'}}
 elif method=='account/rateLimits/read':
  value={'ordinaryUsageAllowed':True,'accountId':'other' if scenario=='other' else 'account-one','rateLimitsByLimitId':{'codex':{'spendControlReached':False,'rateLimitReachedType':None,'primary':{'usedPercent':90 if scenario=='reserve' else 5,'windowDurationMins':300,'resetsAt':time.time()+3600},'secondary':{'usedPercent':1,'windowDurationMins':10080,'resetsAt':time.time()+9000}}}}
  if scenario=='unknown':value['ordinaryUsageAllowed']=None
 elif method=='config/read':
  value={'config':{'model_provider':'openai','mcp_servers':{},'chatgpt_base_url':'https://chatgpt.com/backend-api/'}}
  if scenario=='custom-route':value['config']['chatgpt_base_url']='https://wrong.invalid/backend-api'
  if scenario=='connectors':value['config']['mcp_servers']={'unreviewed':{'enabled':True}}
 elif method=='model/list':value={'data':[{'id':'sol','model':'gpt-6.1-sol'}],'nextCursor':None}
 elif method in ('thread/start','thread/resume'):value={'thread':{'id':p.get('threadId','thread-one')},'model':'gpt-6.1-sol','modelProvider':'openai'}
 elif method=='turn/start':
  if p.get('sandboxPolicy')!={'type':'readOnly','networkAccess':False}:
   send({'id':ident,'error':{'code':-32602,'message':'installed legacy schema invalid'}});continue
  send({'id':ident,'result':{'turn':{'id':'turn-one'}}})
  if scenario=='eof':sys.exit(0)
  if scenario=='malformed':print('private-bad-json',flush=True);continue
  if scenario=='oversized':print('x'*1048577,flush=True);continue
  if scenario=='timeout':continue
  if scenario=='descendant':
   child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(100)'])
   with open(record,'a') as f:f.write(json.dumps({'child_pid':child.pid})+'\n')
   continue
  send({'method':'turn/completed','params':{'threadId':'thread-one','turn':{'id':'wrong-turn','status':'completed'}}})
  send({'id':99,'method':'item/commandExecution/requestApproval','params':{'secret':'do-not-forward'}})
  send({'method':'item/completed','params':{'threadId':'thread-one','turnId':'turn-one','item':{'type':'agentMessage','id':'message','text':'Source review complete.','phase':'final_answer'}}})
  send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-one','turnId':'turn-one','tokenUsage':{'last':{'inputTokens':10,'outputTokens':5,'private':'never-copy'}}}})
  send({'method':'turn/completed','params':{'threadId':'thread-one','turn':{'id':'turn-one','status':scenario if scenario in ('failed','interrupted') else 'completed'}}})
  continue
 elif method=='turn/interrupt':
  send({'id':ident,'result':{}})
  sys.exit(0)
 elif 'error' in row:continue
 else:value={}
 if ident is not None:send({'id':ident,'result':value})
'''

class BridgeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.state=self.root/'state'
  self.fake=self.root/'fake.py';self.fake.write_text(FAKE)
  self.record=self.root/'calls.jsonl';self.prompt=self.root/'prompt.md';self.prompt.write_text('Review the fixture source.')
 def tearDown(self):self.temp.cleanup()
 def args(self,command='help'):
  return bridge.parser().parse_args([command,'--state-dir',str(self.state),'--prompt-file',str(self.prompt),'--task-id','fixture','--timeout','2'])
 def run_fake(self,scenario='completed',args=None):
  return bridge.execute(args or self.args(),[sys.executable,str(self.fake),scenario,str(self.record)])
 def rows(self):return [json.loads(s) for s in self.record.read_text().splitlines()]
 def test_real_process_handshake_completion_and_private_thread_resume(self):
  first=self.run_fake();second=self.run_fake()
  self.assertEqual(first['status'],'completed');self.assertEqual(second['assistant_text'],'Source review complete.')
  self.assertEqual(first['usage'],{'last':{'inputTokens':10,'outputTokens':5}})
  methods=[r.get('method') for r in self.rows()]
  self.assertEqual(methods[:5],['initialize','initialized','account/read','account/rateLimits/read','model/list'])
  self.assertIn('thread/resume',methods)
  self.assertEqual((self.state/'threads.json').stat().st_mode&0o777,0o600)
  self.assertNotIn('private@example',json.dumps(first));self.assertNotIn('account-one',json.dumps(first))
 def test_unconfirmed_turn_start_cannot_silently_restart(self):
  self.run_fake()
  path=self.state/'threads.json';registry=json.loads(path.read_text())
  for record in registry.values():record['turn_started']=False
  path.write_text(json.dumps(registry));path.chmod(0o600)
  self.record.unlink()
  with self.assertRaises(bridge.BridgeError) as error:self.run_fake()
  self.assertEqual(error.exception.code,'thread_recovery_required')
  self.assertNotIn('turn/start',[r.get('method') for r in self.rows()])
 def test_different_account_gets_new_thread(self):
  self.run_fake();self.run_fake('other')
  self.assertEqual(sum(r.get('method')=='thread/start' for r in self.rows()),2)
 def test_failed_interrupted_eof_malformed_and_oversized_are_not_success(self):
  for scenario,code in [('failed','turn_failed'),('interrupted','turn_interrupted'),('eof','rpc_eof'),('malformed','rpc_malformed'),('oversized','rpc_oversized')]:
   with self.subTest(scenario=scenario),self.assertRaises(bridge.BridgeError) as error:self.run_fake(scenario)
   self.assertEqual(error.exception.code,code);self.assertNotIn('private-bad',str(error.exception))
 def test_auth_quota_denial_never_starts_turn(self):
  for scenario in ('apikey','reserve','unknown','custom-route','connectors'):
   self.record.unlink(missing_ok=True)
   with self.assertRaises(bridge.BridgeError):self.run_fake(scenario)
   self.assertNotIn('turn/start',[r.get('method') for r in self.rows()])
 def test_deadline_interrupt_and_process_group_descendant_cleanup(self):
  args=self.args();args.timeout=.2
  with self.assertRaises(bridge.BridgeError) as error:self.run_fake('descendant',args)
  self.assertEqual(error.exception.code,'timeout')
  rows=self.rows();self.assertIn('turn/interrupt',[r.get('method') for r in rows])
  pid=next(r['child_pid'] for r in rows if 'child_pid' in r)
  for _ in range(20):
   status=Path('/proc')/str(pid)/'stat'
   if not status.exists() or status.read_text().split()[2]=='Z':break
   time.sleep(.02)
  else:self.fail('descendant remains running')
 def test_nonreading_rpc_stdin_cannot_outlive_deadline(self):
  client=bridge.RpcClient([sys.executable,'-c','import time;time.sleep(100)'],.2)
  started=time.monotonic()
  try:
   with self.assertRaises(bridge.BridgeError) as error:client.send({'payload':'x'*65536})
   self.assertEqual(error.exception.code,'timeout')
   self.assertLess(time.monotonic()-started,1)
  finally:client.close()
 def test_lock_ledger_permissions_and_retry_threshold(self):
  bridge.private_directory(self.state)
  with bridge.state_lock(self.state),self.assertRaises(bridge.BridgeError) as caught:
   with bridge.state_lock(self.state):pass
  self.assertEqual(caught.exception.code,'busy')
  ledger=self.root/'failures.json';ledger.write_text(json.dumps({'tasks':{'fixture':{'failures':2}}}));ledger.chmod(0o600)
  args=self.args();args.reason='retry';args.failures_file=ledger
  with self.assertRaises(bridge.BridgeError):self.run_fake(args=args)
  self.assertFalse(self.record.exists())
  ledger.write_text(json.dumps({'tasks':{'fixture':{'failures':3}}}));self.assertTrue(self.run_fake(args=args)['ok'])
  ledger.chmod(0o644)
  with self.assertRaises(bridge.BridgeError):bridge.retry_allowed(ledger,'fixture')
 def test_environment_never_forwards_provider_credentials(self):
  with patch.dict(os.environ,{'OPENAI_API_KEY':'private','ACCESS_TOKEN':'private','A6API_API_KEY':'old','ANTHROPIC_AUTH_TOKEN':'private','CODEX_HOME':'/private/selected'}):
   env=bridge.child_environment();self.assertEqual(env['CODEX_HOME'],'/private/selected')
   self.assertFalse(any(k.endswith(('API_KEY','TOKEN')) for k in env))
 def test_limits_actual_windows_daily_unknown_bool_stale_and_reserve(self):
  raw={'ordinaryUsageAllowed':True,'rateLimits':{'spendControlReached':False,'primary':{'usedPercent':5,'windowDurationMins':300,'resetsAt':200}}}
  result=bridge.normalize_limits(raw,100);self.assertTrue(result['dispatch_allowed']);self.assertFalse(result['daily_available'])
  for field,value in [('usedPercent',True),('usedPercent',90),('usedPercent',float('nan')),('resetsAt',99),('windowDurationMins',None)]:
   changed=json.loads(json.dumps(raw));changed['rateLimits']['primary'][field]=value
   self.assertFalse(bridge.normalize_limits(changed,100)['dispatch_allowed'])
  raw['rateLimits']['primary']['windowDurationMins']=1440;self.assertTrue(bridge.normalize_limits(raw,100)['daily_available'])
  raw['ordinaryUsageAllowed']=None;self.assertFalse(bridge.normalize_limits(raw,100)['dispatch_allowed'])
 def test_report_cached_metadata_is_not_fresh_permission(self):
  self.run_fake(args=self.args('status'));report=self.run_fake(args=self.args('report'))
  self.assertTrue(report['cached']);self.assertNotIn('assistant_text',report)

if __name__=='__main__':unittest.main()
