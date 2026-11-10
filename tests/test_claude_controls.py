"""Exercise Telegram controls through real isolated tmux and the Claude runner.

The default backend is a deterministic CLI fixture. Set
TELEAGENT_CLAUDE_LIVE_TEST=1 to exercise the installed, signed-in Claude CLI.
Telegram transport is captured locally; this never polls or sends as a real bot.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def wait_for(check, label, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Timed out: ' + label)


def scenario(root: Path, live: bool):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import telegram_agent_registry as registry
    from teleagent import commands, delivery, lifecycle, messages, models, processes, queue, submission, transport, ui
    repo = root / 'repo'
    runtime = root / 'runtime'
    repo.mkdir(); runtime.mkdir()
    (repo/'scripts').symlink_to(ROOT/'scripts', target_is_directory=True)
    (repo/'AGENTS.md').write_text('Follow only the test request. Do not read unrelated files.\n')
    home = root/'rollouts'
    binary = shutil.which('claude')
    if not live:
        binary = str(root/'claude')
        Path(binary).write_text('''#!/usr/bin/env python3
import json,sys,time,uuid,subprocess
if sys.argv[1:] == ['auth','status']:
    print(json.dumps({'loggedIn':True,'authMethod':'fixture'}));raise SystemExit(0)
if '--output-format' in sys.argv and sys.argv[sys.argv.index('--output-format')+1]=='json':
    assert sys.stdin.read()=='/usage'
    print(json.dumps({'is_error':False,'result':'Current session: 17% used'}));raise SystemExit(0)
args=sys.argv[1:];text=sys.stdin.read()
sid=args[args.index('--resume')+1] if '--resume' in args else str(uuid.uuid4())
print(json.dumps({'type':'system','subtype':'init','session_id':sid,'model':'claude-sonnet-fixture'}),flush=True)
if 'sleep 30' in text:
    time.sleep(30)
reply='CONTROL_OK'
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':sid,'result':reply}),flush=True)
''')
        Path(binary).chmod(0o755)
    os.environ.update(TELEAGENT_CLAUDE_BIN=binary, TELEAGENT_REPO=str(repo),
        TELEAGENT_SCRATCH=str(root/'scratch'),TELEAGENT_LOG_DIR=str(runtime),
        TELEAGENT_CODEX_HOME=str(home), TELEAGENT_TMUX_SESSION='claude-controls',
        TELEAGENT_AGENT_DIR=str(runtime/'agents'), TELEAGENT_INSTANCE='main',
        TELEAGENT_PERSONALITY_FILE=str(root/'absent-personality'),
        TELEAGENT_SECRET_ENV=str(root/'absent-secrets'), TELEAGENT_CODEX_ACCESS_MODE='full-access',
        TELEAGENT_CODEX_SUPERVISOR_BASE_DELAY='1',TELEAGENT_CODEX_SUPERVISOR_MAX_DELAY='1')
    args=argparse.Namespace(repo_root=repo,session='claude-controls',codex_window='codex',
        target_pane='claude-controls:codex.0',submit_delay=2.0,relay_mode='tmux-enter',
        allow_shell_pane=False,bridge_ack=False,max_log_chars=4000,tmux_lines=30,
        agent_recovery_wait=3,agent_watchdog=True,
        codex_usage_state_path=str(runtime/'usage.json'),codex_reset_state_path=str(runtime/'reset.json'),
        codex_auth_state_path=str(runtime/'auth.json'),agent_lifecycle_state_path=str(runtime/'life.json'),
        relay_confirmation_state_path=str(runtime/'pending.json'),
        relay_queue_state_path=str(runtime/'queue.json'),agent_message_state_path=str(runtime/'messages.json'))
    routes=runtime/'routes.json';log=runtime/'listener.jsonl';sent=[];counter=0;formatted=[]
    def receive(text):
        nonlocal counter
        counter+=1
        update={'update_id':counter,'message':{'message_id':counter,'date':int(time.time()),
            'chat':{'id':'123','type':'private'},'from':{'id':123},'text':text}}
        original=messages.format_agent_message
        def capture(*a,**kw):
            text=original(*a,**kw);formatted.append(text);return text
        with mock.patch.object(messages,'format_agent_message',side_effect=capture):
            record=commands.handle_update(update,args,{},'offline','123',log,route_state_path=routes)
        return record,update
    def metadata():
        meta=registry.active_agent_for_pane(args.target_pane)
        if meta:
            meta=registry.refresh_codex_session_link(meta,target_pane=args.target_pane)
            if meta.get('codex_session_path'):return meta
        return None
    def records():
        meta=metadata()
        if not meta:return []
        return [json.loads(s) for s in Path(meta['codex_session_path']).read_text().splitlines()]
    def completed():
        rows=records()
        return rows and rows[-1].get('payload',{}).get('type')=='task_complete'
    def native_session():
        p=runtime/'claude_session.json'
        return json.loads(p.read_text())['session_id'] if p.exists() else None
    def drain():
        return delivery.drain_codex_agent_messages('offline','123',metadata(),runtime/'messages.json',
            None,{},sessions_root=home,route_state_path=routes)
    with mock.patch.object(transport,'send_reply',side_effect=lambda *a,**kw:sent.append((a,kw))):
        receive('/claude sonnet low')
        wait_for(metadata,'session registration')
        request='Reply with exactly CONTROL_OK.\nDo not call any tools.'
        record,update=receive(request)
        assert 'submission confirmed' in record.get('relay_result',''), 'normal message was not confirmed'
        expected=formatted[-1]
        user_rows=[r['payload']['content'][0]['text'] for r in records()
                   if r.get('type')=='response_item' and r.get('payload',{}).get('role')=='user']
        if user_rows != [expected]:
            actual=user_rows[0] if user_rows else ''
            first=next((i for i,(a,b) in enumerate(zip(actual,expected)) if a!=b),min(len(actual),len(expected)))
            raise AssertionError(f'tmux input changed or split the original request: rows={len(user_rows)}, lengths={len(actual)}/{len(expected)}, first_difference={first}, actual_fragment={actual[first:first+35]!r}, expected_fragment={expected[first:first+35]!r}')
        wait_for(completed,'first real response',90)
        first_session=native_session();assert first_session
        before=len(sent);drain()
        assert any('CONTROL_OK' in str(x[0][2]) for x in sent[before:]), 'root reply was not forwarded'
        assert drain()==0, 'reply was duplicated'
        receive('/status')
        assert 'idle' in str(sent[-1][0][2]), 'status does not recognize idle Claude'
        receive('/model sol')
        assert 'native Claude Code' in str(sent[-1][0][2]), 'Codex model keys reached Claude'
        receive('/reasoning high')
        assert 'native Claude Code' in str(sent[-1][0][2]), 'Codex reasoning keys reached Claude'
        receive('/agent_status')
        assert 'Claude Code signed in' in str(sent[-1][0][2]), 'agent status used the wrong account'
        count=len(records())
        receive('/claude_auth status')
        assert 'Claude Code signed in' in str(sent[-1][0][2]), 'Claude account status failed'
        receive('/claude_usage')
        assert 'Current session:' in str(sent[-1][0][2]), 'native usage was not forwarded'
        assert len(records())==count, 'account controls entered the agent conversation'
        for control in ['/codex_usage','/codex_reset','/confirm','/resume_goal','/codex /clear']:
            receive(control)
            assert ('Codex accounts' in str(sent[-1][0][2]) or 'unavailable in native Claude Code' in str(sent[-1][0][2]))
        assert len(records())==count, 'unsupported Codex controls entered the Claude conversation'
        print('PASS launch, exact multiline paste, reply forwarding, duplicate suppression, idle status, model/effort guards',flush=True)

        # Interrupt while a real Claude turn is running, without a newline key.
        receive('Run sleep 30 with the Bash tool, then reply LATE.')
        wait_for(lambda: submission.codex_session_turn_active(Path(metadata()['codex_session_path'])), 'active turn')
        sleeping=[]
        if live:
            def find_sleep():
                tree=set(registry.descendants(registry.tmux_pane_pid(args.target_pane)))
                return [pid for pid,_,comm in registry.ps_rows() if pid in tree and comm=='sleep']
            sleeping=wait_for(find_sleep,'real Claude Bash tool starts',30)
        receive('/status');assert 'working' in str(sent[-1][0][2])
        # Stale Codex credentials must not block an independently signed-in Claude.
        Path(args.codex_auth_state_path).write_text(json.dumps({'blocked':True}))
        counter+=1
        queued={'update_id':counter,'message':{'message_id':counter,'date':int(time.time()),
            'chat':{'id':'123','type':'private'},'from':{'id':123},'text':'Reply with exactly CONTROL_OK. Do not call tools.'}}
        assert queue.dispatch_telegram_update(queued,args,{},'offline','123',log,route_state_path=routes)=='queued'
        assert len(queue.telegram_relay_queue_tasks(Path(args.relay_queue_state_path)))==1
        counter+=1
        quick={'update_id':counter,'message':{'message_id':counter,'chat':{'id':'123','type':'private'},
            'from':{'id':123},'text':'/queue'}}
        assert ui.handle_quick_update(quick,args,'offline','123','123','',{})
        assert 'CONTROL_OK' in str(sent[-1][0][2]), 'queue listing omitted waiting request'
        record,_=receive('/interrupt Reply with exactly CONTROL_OK. Do not call tools.')
        assert record['action']=='interrupt', 'interrupt handler failed'
        wait_for(completed,'replacement response',90)
        if live:
            wait_for(lambda:not any(Path(f'/proc/{pid}').exists() for pid in sleeping),
                     'interrupted Bash child exits',10)
        assert native_session()==first_session, 'interrupt changed native conversation'
        assert any(r.get('payload',{}).get('type')=='turn_aborted' for r in records())
        assert queue.drain_telegram_relay_queue(args,{},'offline','123',log,route_state_path=routes)
        wait_for(completed,'queued reply after interrupt',90)
        assert not queue.telegram_relay_queue_tasks(Path(args.relay_queue_state_path))
        Path(args.codex_auth_state_path).unlink()
        print('PASS active status, Escape interrupt, replacement instruction, retained native conversation',flush=True)
        print('PASS queue listing and delivery despite stale Codex authentication',flush=True)

        # Supervisor crash recovery must retain Claude's exact native session.
        old=Path(metadata()['codex_session_path'])
        pane_pid=registry.tmux_pane_pid(args.target_pane)
        pids=[pid for pid in registry.descendants(pane_pid)
              if Path(f'/proc/{pid}/comm').read_text().strip()=='codex-claude']
        assert len(pids)==1
        os.kill(pids[0],signal.SIGKILL)
        wait_for(lambda: metadata() and Path(metadata()['codex_session_path'])!=old,'supervisor recovery')
        assert native_session()==first_session
        record,_=receive('Reply with exactly CONTROL_OK. Do not call tools.')
        assert 'submission confirmed' in record.get('relay_result','')
        wait_for(completed,'reply after recovery',90)
        assert native_session()==first_session
        print('PASS supervisor crash recovery and reply after exact session resume',flush=True)

        receive('/restart_agent cc-sonnet low')
        wait_for(metadata,'explicit restart registration')
        record,_=receive('Reply with exactly CONTROL_OK. Do not call tools.')
        assert 'submission confirmed' in record.get('relay_result','')
        wait_for(completed,'reply after fresh restart',90)
        assert native_session()!=first_session, 'explicit restart reused native conversation'
        stopped,_=receive('/kill_agent')
        assert stopped['action']=='kill_agent', f"stop failed: {stopped.get('error','unknown')}"
        assert lifecycle.agent_desired_state(args)=='stopped'
        lifecycle.maintain_managed_codex_agent(args,log)
        assert not processes.tmux_window_exists(args.session,args.codex_window), 'watchdog ignored intentional stop'
        receive('/start_agent cc-sonnet low')
        wait_for(metadata,'explicit start registration')
        assert processes.tmux_pane_has_claude_process(args.target_pane)
        receive('/kill_agent')
        print('PASS fresh restart, intentional stop, stopped watchdog guard, explicit native start',flush=True)


class ClaudeControlIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('tmux'),'tmux required')
    def test_isolated_control_path(self):
        parent=Path(os.environ.get('TELEAGENT_TEST_TMPDIR',str(Path.home()/'.ta-test')))
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            env={k:v for k,v in os.environ.items() if not k.startswith(('TELEAGENT_','CODEX_'))}
            env['TELEAGENT_INSTANCE']='main'
            live=os.environ.get('TELEAGENT_CLAUDE_LIVE_TEST')=='1'
            result=subprocess.run([str(ROOT/'scripts/tmux_isolated_test.sh'),'--',sys.executable,
                __file__,'--scenario',tmp,'live' if live else 'fixture'],env=env,
                capture_output=True,text=True,timeout=330)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            if live:print(result.stdout)


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--scenario':
        scenario(Path(sys.argv[2]),sys.argv[3]=='live')
    else:
        unittest.main()
