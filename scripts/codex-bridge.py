#!/usr/bin/env python3
"""Bounded local subscription RPC; stdout is a redacted JSON contract."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import select
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'gpt-6.1-sol'
SCHEMA = 'phpretro.codex.bridge.v1'
MAX_LINE = 1024 * 1024

class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def child_environment():
    # Native manages its own selected ChatGPT login. No API route/key inheritance.
    allowed = {'HOME','PATH','LANG','LC_ALL','TERM','TMPDIR','USER','LOGNAME','SHELL','CODEX_HOME'}
    return {k:v for k,v in os.environ.items() if k in allowed}

def codex_command(binary='codex'):
    return [binary, 'app-server', '--listen', 'stdio://', '-c', 'model_provider="openai"',
            '-c', 'forced_login_method="chatgpt"', '-c', 'model="'+MODEL+'"',
            '-c', 'sandbox_mode="read-only"', '-c', 'approval_policy="never"',
            '-c', 'mcp_servers={}', '-c', 'features.multi_agent=false',
            '-c', 'features.hooks=false']

def safe_json(path, limit=65536):
    try:
        fd=os.open(path, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>limit:
                raise BridgeError('private_state', 'Private state must be an owned regular mode-0600 file.')
            return json.loads(f.read(limit+1))
    except BridgeError: raise
    except (OSError,ValueError): raise BridgeError('private_state','Private state cannot be read.') from None

def private_directory(path):
    path=Path(path).absolute()
    current=Path(path.anchor)
    for part in path.parts[1:]:
        current=current/part
        try: info=current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700);info=current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise BridgeError('private_state','Private state directory is unsafe.')
    info=path.stat()
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise BridgeError('private_state','Private state directory must be owned and mode0700.')
    return path

def save_json(path,data):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex)
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temp.exists():temp.unlink()

@contextlib.contextmanager
def state_lock(directory):
    fd=os.open(directory/'bridge.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise BridgeError('private_state','Private lock is unsafe.')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise BridgeError('busy','Another bridge request owns this state.') from None
        yield
    finally:os.close(fd)

def normalize_limits(payload,now=None):
    now=time.time() if now is None else now
    windows=[];unknown=False;blocked=False
    if not isinstance(payload,dict):payload={}
    if payload.get('ordinaryUsageAllowed') is not True:unknown=True
    buckets=payload.get('rateLimitsByLimitId')
    if not isinstance(buckets,dict) or not buckets:
        legacy=payload.get('rateLimits');buckets={'legacy':legacy} if isinstance(legacy,dict) else {}
    if not buckets:unknown=True
    for key,bucket in buckets.items():
        if not isinstance(bucket,dict):unknown=True;continue
        if bucket.get('spendControlReached') is True or bucket.get('rateLimitReachedType') is not None:blocked=True
        if bucket.get('spendControlReached') is not False:unknown=True
        found=False
        for position in ('primary','secondary'):
            value=bucket.get(position)
            if value is None:continue
            found=True
            if not isinstance(value,dict):unknown=True;continue
            used=value.get('usedPercent');duration=value.get('windowDurationMins');reset=value.get('resetsAt')
            valid=all(isinstance(n,(int,float)) and not isinstance(n,bool) and math.isfinite(n) for n in (used,duration,reset))
            if not valid or not 0<=used<=100 or duration<=0 or reset<=now:
                unknown=True;continue
            remaining=100-used
            windows.append({'limit_id':str(key),'position':position,'used_percent':used,
                            'remaining_percent':remaining,'duration_minutes':duration,'resets_at':reset})
            if remaining<=10:blocked=True
        if not found:unknown=True
    allowed=bool(windows) and not unknown and not blocked
    return {'dispatch_allowed':allowed,'windows':windows,'daily_available':any(w['duration_minutes']==1440 for w in windows),
            'reason':'quota_unknown' if unknown else 'quota_reserve' if blocked else 'ready',
            'human_action':None if allowed else 'Checkpoint; the owner may switch accounts manually, then request fresh limits. No automatic logout or reset.'}

class RpcClient:
    def __init__(self,command,timeout=30):
        self.deadline=time.monotonic()+timeout
        self.events=queue.Queue(maxsize=256);self.pending=[];self.serial=0;self.active=None
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                      env=child_environment(),cwd=ROOT,start_new_session=True)
        try:
            os.set_blocking(self.process.stdin.fileno(),False)
            threading.Thread(target=self._read,daemon=True).start()
        except BaseException:
            self.close(interrupt=True);raise
    def _read(self):
        try:
            while True:
                line=self.process.stdout.readline(MAX_LINE+1)
                if not line:self.events.put(BridgeError('rpc_eof','App-server closed its RPC stream.'));return
                if len(line)>MAX_LINE:self.events.put(BridgeError('rpc_oversized','App-server RPC frame exceeded its bound.'));return
                try:row=json.loads(line)
                except (ValueError,UnicodeError):self.events.put(BridgeError('rpc_malformed','App-server returned malformed JSON.'));return
                if not isinstance(row,dict):self.events.put(BridgeError('rpc_malformed','App-server returned an invalid RPC frame.'));return
                self.events.put(row)
        except OSError:self.events.put(BridgeError('rpc_eof','App-server RPC stream failed.'))
    def send(self,row,until=None):
        deadline=self.deadline if until is None else until
        data=(json.dumps(row,allow_nan=False)+'\n').encode();offset=0
        try:
            while offset<len(data):
                remaining=deadline-time.monotonic()
                if remaining<=0:raise BridgeError('timeout','App-server RPC input exceeded the bounded deadline.')
                _,ready,_=select.select([], [self.process.stdin.fileno()], [], remaining)
                if not ready:raise BridgeError('timeout','App-server RPC input exceeded the bounded deadline.')
                try:offset+=os.write(self.process.stdin.fileno(),data[offset:])
                except BlockingIOError:continue
        except (OSError,ValueError):raise BridgeError('rpc_eof','App-server RPC input failed.') from None
    def next(self):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise BridgeError('timeout','App-server exceeded the bounded deadline.')
        try:row=self.events.get(timeout=remaining)
        except queue.Empty:raise BridgeError('timeout','App-server exceeded the bounded deadline.') from None
        if isinstance(row,BridgeError):raise row
        if 'method' in row and 'id' in row:
            # Never approve server-initiated commands, edits or dynamic tools.
            self.send({'id':row['id'],'error':{'code':-32601,'message':'Bridge does not authorize server requests.'}})
            return None
        return row
    def request(self,method,params):
        self.serial+=1;ident=self.serial;self.send({'id':ident,'method':method,'params':params})
        while True:
            row=self.next()
            if row is None:continue
            if row.get('id')==ident and 'method' not in row:
                if 'error' in row:raise BridgeError('rpc_error','App-server rejected '+method+'.')
                if 'result' not in row:raise BridgeError('rpc_malformed','App-server response has no result.')
                return row['result']
            if 'method' in row:
                if len(self.pending)>=256:raise BridgeError('rpc_oversized','App-server pending notifications exceeded their bound.')
                self.pending.append(row)
    def cancel(self):
        if self.active:
            thread,turn=self.active
            try:self.send({'id':self.serial+1,'method':'turn/interrupt','params':{'threadId':thread,'turnId':turn}},until=time.monotonic()+0.2)
            except BridgeError:pass
    def close(self,interrupt=False):
        if interrupt:self.cancel()
        # Kill the entire group even if the leader exited but descendants remain.
        if interrupt:
            try:self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:pass
        try:os.killpg(self.process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:pass
        try:os.killpg(self.process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        self.process.wait()
        for pipe in (self.process.stdin,self.process.stdout):
            try:pipe.close()
            except OSError:pass

def inspect_account(client):
    client.request('initialize',{'clientInfo':{'name':'phpretro-builder','title':'PHPRetro builder','version':'1.0'},'capabilities':{'experimentalApi':True}})
    client.send({'method':'initialized','params':{}})
    account=client.request('account/read',{'refreshToken':False})
    if not isinstance(account,dict) or not isinstance(account.get('account'),dict) or account['account'].get('type')!='chatgpt':
        raise BridgeError('subscription_required','Sign in with ChatGPT in native Codex; API-key or other authentication is not used.')
    raw=client.request('account/rateLimits/read',{})
    limits=normalize_limits(raw)
    account_id=raw.get('accountId') if isinstance(raw,dict) else None
    if not isinstance(account_id,str) or not account_id:
        limits.update(dispatch_allowed=False,reason='account_scope_unknown')
    models=[];cursor=None;seen=set()
    for _ in range(20):
        page=client.request('model/list',{'limit':100,'includeHidden':True,'cursor':cursor})
        if not isinstance(page,dict) or not isinstance(page.get('data'),list):raise BridgeError('rpc_malformed','Model catalog is invalid.')
        models.extend(page['data']);cursor=page.get('nextCursor')
        if cursor is None:break
        if not isinstance(cursor,str) or cursor in seen:raise BridgeError('rpc_malformed','Model catalog pagination is invalid.')
        seen.add(cursor)
    else:raise BridgeError('rpc_oversized','Model catalog pagination exceeded its bound.')
    available=any(isinstance(m,dict) and m.get('model')==MODEL for m in models)
    if not available:limits.update(dispatch_allowed=False,reason='model_unavailable')
    effective=client.request('config/read',{'includeLayers':False,'cwd':str(ROOT)})
    cfg=effective.get('config') if isinstance(effective,dict) else None
    if not isinstance(cfg,dict) or cfg.get('model_provider')!='openai':raise BridgeError('provider_config','Native OpenAI provider configuration must be known.')
    servers=cfg.get('mcp_servers',{})
    if not isinstance(servers,dict) or any(not isinstance(v,dict) or v.get('enabled') is not False for v in servers.values()):raise BridgeError('inherited_connectors','Read-only bridge refuses inherited MCP connectors.')
    providers=cfg.get('model_providers',{})
    provider=providers.get('openai',{}) if isinstance(providers,dict) else None
    if not isinstance(provider,dict) or provider.get('base_url') not in (None,'https://api.openai.com/v1','https://api.openai.com/v1/') or cfg.get('chatgpt_base_url') not in (None,'https://chatgpt.com/backend-api','https://chatgpt.com/backend-api/'):
        raise BridgeError('provider_config','Read-only bridge refuses a custom OpenAI endpoint.')
    fingerprint=hashlib.sha256(account_id.encode()).hexdigest() if isinstance(account_id,str) else None
    return limits,available,fingerprint

def retry_allowed(path,task):
    data=safe_json(path)
    try:failures=data['tasks'][task]['failures']
    except (KeyError,TypeError):raise BridgeError('failure_ledger','Trusted task failure evidence is unavailable.') from None
    if not isinstance(failures,int) or isinstance(failures,bool) or failures<3:
        raise BridgeError('failure_threshold','Retry help requires at least three recorded task failures; funding failures do not count.')

def help_turn(client,directory,task,reason,prompt,account):
    registry_path=directory/'threads.json'
    registry=safe_json(registry_path) if registry_path.exists() else {}
    if not isinstance(registry,dict):raise BridgeError('private_state','Thread registry is malformed.')
    key=hashlib.sha256((account+'\0'+str(ROOT)+'\0'+task+'\0'+reason).encode()).hexdigest()
    params={'model':MODEL,'modelProvider':'openai','cwd':str(ROOT),'approvalPolicy':'never','sandbox':'read-only',
            'developerInstructions':'Read-only PHPRetro source review. Never read credentials or private transcripts, change files, launch agents or request approvals. Return concise source-supported findings.'}
    saved=registry.get(key)
    if saved is not None:
        if not isinstance(saved,dict) or not isinstance(saved.get('thread_id'),str) or not saved['thread_id'] or not isinstance(saved.get('turn_started'),bool):raise BridgeError('private_state','Thread registry is malformed.')
        if not saved['turn_started']:raise BridgeError('thread_recovery_required','Previous turn start was not confirmed; inspect private state before explicit recovery.')
        thread=client.request('thread/resume',dict(params,threadId=saved['thread_id'],excludeTurns=True))
    else:thread=client.request('thread/start',dict(params,allowProviderModelFallback=False,ephemeral=False))
    try:thread_id=thread['thread']['id']
    except (KeyError,TypeError):raise BridgeError('rpc_malformed','Thread response is invalid.') from None
    if thread.get('model')!=MODEL or thread.get('modelProvider')!='openai':raise BridgeError('model_mismatch','App-server selected an unexpected model route.')
    if not isinstance(thread_id,str) or not thread_id or (saved is not None and thread_id!=saved['thread_id']):
        raise BridgeError('rpc_malformed','Thread identity does not match its registry.')
    registry[key]={'thread_id':thread_id,'turn_started':False};save_json(registry_path,registry)
    start=client.request('turn/start',{'threadId':thread_id,'model':MODEL,'effort':'high','approvalPolicy':'never',
        'sandboxPolicy':{'type':'readOnly','networkAccess':False},
        'input':[{'type':'text','text':prompt,'text_elements':[]}]})
    try:turn_id=start['turn']['id']
    except (KeyError,TypeError):raise BridgeError('rpc_malformed','Turn response is invalid.') from None
    if not isinstance(turn_id,str) or not turn_id:raise BridgeError('rpc_malformed','Turn identity is invalid.')
    client.active=(thread_id,turn_id)
    registry[key]['turn_started']=True;save_json(registry_path,registry)
    messages={};usage=None
    pending=client.pending;client.pending=[]
    while True:
        row=pending.pop(0) if pending else client.next()
        if row is None:continue
        method=row.get('method');p=row.get('params',{})
        if not isinstance(p,dict) or p.get('threadId')!=thread_id:continue
        if method=='turn/completed':
            turn=p.get('turn',{})
            if not isinstance(turn,dict) or turn.get('id')!=turn_id:continue
            status=turn.get('status');client.active=None
            if status!='completed':raise BridgeError('turn_'+str(status) if status in ('failed','interrupted') else 'turn_invalid','Sol did not complete the requested turn.')
            text='\n'.join(messages.values())
            if not text:raise BridgeError('missing_answer','Completed turn contained no final assistant message.')
            return {'status':'completed','read_only':True,'completed_turn':True,'assistant_text':text,'thread_id':thread_id,'turn_id':turn_id,'usage':usage}
        if p.get('turnId')!=turn_id:continue
        if method=='item/completed':
            item=p.get('item',{})
            if isinstance(item,dict) and item.get('type')=='agentMessage' and isinstance(item.get('text'),str) and item.get('phase') in (None,'final_answer'):
                messages[str(item.get('id'))]=item['text']
                if sum(len(s) for s in messages.values())>65536:raise BridgeError('answer_oversized','Assistant answer exceeded its bound.')
        elif method=='thread/tokenUsage/updated':
            value=p.get('tokenUsage')
            if isinstance(value,dict):
                # Copy recognized token counters only, never arbitrary provider fields.
                usage={part:{k:v for k,v in counters.items() if k in ('inputTokens','cachedInputTokens','outputTokens','reasoningOutputTokens','totalTokens') and isinstance(v,int) and not isinstance(v,bool) and v>=0}
                       for part,counters in value.items() if part in ('total','last') and isinstance(counters,dict)}

def execute(args,command=None):
    directory=private_directory(args.state_dir)
    result={'schema':SCHEMA,'ok':True,'command':args.command,'provider':'codex-chatgpt-subscription','model':MODEL}
    with state_lock(directory):
        if args.command=='report':
            data=safe_json(directory/'last-report.json');result.update(data,cached=True,readiness_fresh=False)
            result['limits'].update(dispatch_allowed=False,reason='cached_report')
            return result
        if args.command=='help':
            if not args.task_id or not args.prompt_file:raise BridgeError('arguments','Help requires task-id and prompt-file.')
            if args.reason=='retry':
                if not args.failures_file:raise BridgeError('failure_ledger','Retry requires a trusted private failures-file.')
                retry_allowed(args.failures_file,args.task_id)
            try:
                raw=Path(args.prompt_file).read_bytes()
                if not raw or len(raw)>16384:raise BridgeError('prompt_bound','Prompt must contain1–16384bytes.')
                prompt=raw.decode('utf-8')
            except (OSError,UnicodeError):raise BridgeError('prompt_bound','Prompt file cannot be read.') from None
        client=RpcClient(command or codex_command(args.codex_bin),args.timeout)
        success=False
        try:
            limits,available,account=inspect_account(client)
            result.update(status='ready' if limits['dispatch_allowed'] else 'paused',limits=limits,model_available=available,
                          model_access_verified=False,readiness_fresh=True,observed_at=time.time(),account_mode='chatgpt',daily_allowance='reported' if limits['daily_available'] else 'unavailable')
            save_json(directory/'last-report.json',{k:v for k,v in result.items() if k not in ('schema','command','ok')})
            if args.command=='help':
                if not limits['dispatch_allowed']:raise BridgeError(limits['reason'],'New Sol dispatch is paused; inspect limits and checkpoint.')
                result.update(help_turn(client,directory,args.task_id,args.reason,prompt,account),model_access_verified=True)
            success=True;return result
        finally:client.close(interrupt=not success)

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('status','limits','help','report'))
    p.add_argument('--state-dir',type=Path,default=ROOT/'.builder/codex-bridge')
    p.add_argument('--codex-bin',default='codex',help='Trusted installed executable; no command string.')
    p.add_argument('--timeout',type=float,default=30)
    p.add_argument('--reason',choices=('architecture','final-review','retry'),default='architecture')
    p.add_argument('--task-id');p.add_argument('--prompt-file',type=Path);p.add_argument('--failures-file',type=Path)
    return p

def main(argv=None):
    args=parser().parse_args(argv)
    if args.command=='help' and args.timeout==30:args.timeout=180
    def interrupted(signum, frame):raise BridgeError('cancelled','Bridge request was cancelled; checkpoint preserved.')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        if not math.isfinite(args.timeout) or not 0<args.timeout<=300:raise BridgeError('arguments','Timeout must be1–300seconds.')
        result=execute(args)
    except (BridgeError,OSError,ValueError,TypeError,KeyboardInterrupt) as error:
        result={'schema':SCHEMA,'ok':False,'command':args.command,'provider':'codex-chatgpt-subscription','model':MODEL,
                'error':{'code':error.code if isinstance(error,BridgeError) else 'local_failure',
                         'message':str(error) if isinstance(error,BridgeError) else 'Local bridge setup failed.'}}
    print(json.dumps(result,allow_nan=False));return 0 if result['ok'] else 2

if __name__=='__main__':raise SystemExit(main())
