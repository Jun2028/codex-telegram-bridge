"""Native Claude Code protocol, session identity, interruption and launch checks."""
from __future__ import annotations

import argparse
import json
import os
import pty
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_agent_runner as runner
import telegram_agent_registry as registry
import telegram_inbox
from teleagent import claude, delivery, lifecycle, messages, models, processes, settings, submission, transport


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get("TELEAGENT_TEST_TMPDIR", str(Path.home() / ".ta-test")))
        parent.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"TELEAGENT_PERSONALITY_FILE": str(self.root / "personality.md")})
        env.start()
        self.addCleanup(env.stop)
        self.binary = self.root / "claude"
        self.binary.write_text('''#!/usr/bin/env python3
import json,sys,time
if sys.argv[1:] == ['auth','status']:
    raise SystemExit(0)
args=sys.argv[1:]
assert args[args.index('--input-format')+1] == 'stream-json'
def read():
    # Streamed input: one JSON user message per line until the runner closes it.
    line=sys.stdin.readline()
    if not line:
        return None
    event=json.loads(line)
    assert event['type'] == 'user' and event['message']['role'] == 'user'
    return event['message']['content']
sid=args[args.index('--resume')+1] if '--resume' in args else 'session-one'
if '--effort' in args and args[args.index('--effort')+1] == 'ultracode':
    print("Warning: Unknown --effort value 'ultracode' - ignoring it and using the default effort.", file=sys.stderr)
prompt=read()
while prompt is not None:
    print(json.dumps({'type':'system','subtype':'init','session_id':sid,'model':'claude-sonnet-fixture'}),flush=True)
    if prompt == 'wait':
        time.sleep(30)
    if prompt == 'missing':
        raise SystemExit(0)
    answer='answer:'+prompt
    if prompt == 'steer':
        # A message added mid-turn joins this turn's answer.
        answer+='+'+read()
    if prompt == 'late':
        time.sleep(1)  # A message sent now arrives after this turn's answer.
    print(json.dumps({'type':'assistant','parent_tool_use_id':'child','message':{'content':[{'type':'text','text':'private child response'}]}}))
    if prompt == 'progress':
        print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Checking the files.'}]}}),flush=True)
        print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'t1','name':'Bash','input':{}}]}}),flush=True)
        print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'t1','content':'ok'}]}}),flush=True)
        print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'answer:progress'}]}}),flush=True)
    print(json.dumps({'type':'result','subtype':'success','is_error':prompt=='error','session_id':sid,'result':answer}),flush=True)
    prompt=read()
''')
        self.binary.chmod(0o755)
        (self.root / "AGENTS.md").write_text("Original instructions.\n")

    def new_runner(self):
        agent = runner.ClaudeAgentRunner(model="cc-sonnet", effort="high", repo=self.root,
                                         state_dir=self.root / "runtime", codex_home=self.root / "logs",
                                         binary=str(self.binary))
        self.addCleanup(agent.log.close)
        return agent

    def events(self, agent):
        return [json.loads(s) for s in agent.log.path.read_text().splitlines()]

    def test_model_namespace_and_payload(self):
        for name in claude.MODELS:
            self.assertFalse(settings.is_agy_model(name))
            self.assertEqual(messages.parse_agent_launch_payload(name)[0:2], (name, "medium" if name == "cc-opus" else "high"))
        self.assertEqual(claude.parse_launch_payload(""), ("cc-opus", "medium"))
        self.assertEqual(claude.parse_launch_payload("opus max"), ("cc-opus", "max"))
        for text in ["opus high extra", "unavailable", "sonnet none"]:
            with self.assertRaises(ValueError):
                claude.parse_launch_payload(text)
        self.assertEqual(registry.closest_codex_pids(1, [(1,0,'bash'),(2,1,'ta-claude')]), [2])

    def test_stdin_final_once_and_exact_resume_after_runner_restart(self):
        agent = self.new_runner()
        marker = "[TELEGRAM USER MESSAGE message_id=42 route_id=u1] hi\nsecond line"
        agent.run_message(marker)
        self.assertTrue(submission.wait_for_codex_submission((agent.log.path, 0), marker, timeout=0))
        self.assertFalse(submission.codex_session_turn_active(agent.log.path))
        replies = [delivery.codex_agent_message(e) for e in self.events(agent)]
        self.assertEqual([v for v in replies if v], [("answer:"+marker, "final_answer")])
        self.assertEqual(models.codex_session_model_and_reasoning_effort(agent.log.path),
                         ('claude-sonnet-fixture','high'))
        self.assertEqual(agent.instructions.read_text(), "Original instructions.\n")
        self.assertEqual(agent.session_path.stat().st_mode & 0o777, 0o600)
        restarted = self.new_runner()
        self.assertEqual(restarted.session_id, "session-one")
        restarted.run_message("next")
        command = claude.build_turn_command(str(self.binary), "cc-sonnet", "high",
                                            agent.instructions, restarted.session_id)
        self.assertEqual(command[-2:], ["--resume", "session-one"])
        self.assertNotIn(marker, command)

    def test_intermediate_text_is_commentary_and_final_is_not_repeated(self):
        agent = self.new_runner()
        agent.run_message("progress")
        replies = [v for v in (delivery.codex_agent_message(e) for e in self.events(agent)) if v]
        self.assertEqual(replies, [("Checking the files.", "commentary"),
                                   ("answer:progress", "final_answer")])

    def test_message_sent_mid_turn_joins_the_running_turn(self):
        agent = self.new_runner()
        thread = threading.Thread(target=agent.run_message, args=("steer",))
        thread.start()
        agent.events.put(("message", "more"))
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        replies = [v for v in (delivery.codex_agent_message(e) for e in self.events(agent)) if v]
        self.assertEqual(replies, [("answer:steer+more", "final_answer")])
        self.assertTrue(submission.wait_for_codex_submission((agent.log.path, 0), "more", timeout=0))
        self.assertEqual(agent.carry, [])
        self.assertFalse(submission.codex_session_turn_active(agent.log.path))

    def test_message_arriving_as_the_turn_ends_gets_its_own_answer(self):
        agent = self.new_runner()
        thread = threading.Thread(target=agent.run_message, args=("late",))
        thread.start()
        agent.events.put(("message", "second"))
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        replies = [v for v in (delivery.codex_agent_message(e) for e in self.events(agent)) if v]
        self.assertEqual(replies, [("answer:late", "final_answer"), ("answer:second", "final_answer")])
        self.assertEqual(agent.carry, [])

    def test_no_result_or_provider_error_never_becomes_success(self):
        for prompt in ["missing", "error"]:
            agent = self.new_runner()
            agent.run_message(prompt)
            replies = [delivery.codex_agent_message(e) for e in self.events(agent)]
            self.assertFalse(any(v and v[1] == "final_answer" for v in replies))
            self.assertFalse(submission.codex_session_turn_active(agent.log.path))

    def test_ignored_effort_is_reported_once_and_never_as_the_answer(self):
        agent = runner.ClaudeAgentRunner(model="cc-sonnet", effort="ultracode", repo=self.root,
                                         state_dir=self.root / "runtime", codex_home=self.root / "logs",
                                         binary=str(self.binary))
        self.addCleanup(agent.log.close)
        agent.run_message("first")
        agent.run_message("second")
        replies = [v for v in (delivery.codex_agent_message(e) for e in self.events(agent)) if v]
        notices = [text for text, phase in replies if phase == "commentary"]
        self.assertEqual(len(notices), 1)
        self.assertIn("does not support effort 'ultracode'", notices[0])
        self.assertEqual([text for text, phase in replies if phase == "final_answer"],
                         ["answer:first", "answer:second"])
        self.assertEqual(replies[0][1], "commentary")  # The notice precedes the final answer.

    def test_mismatched_or_corrupt_session_is_not_replaced(self):
        agent = self.new_runner()
        agent.run_message("hi")
        original = agent.session_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "different session"):
            agent.save_session("unrelated")
        self.assertEqual(agent.session_path.read_bytes(), original)
        d=json.loads(original);d['model']='cc-opus';agent.session_path.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError, "selection changed"):
            self.new_runner()
        agent.session_path.write_text("broken")
        with self.assertRaises(ValueError):
            self.new_runner()

    def test_interrupt_finishes_turn_and_keeps_session(self):
        agent = self.new_runner()
        thread = threading.Thread(target=agent.run_message, args=("wait",))
        thread.start()
        try:
            deadline = time.monotonic()+5
            while time.monotonic()<deadline and not agent.session_id:
                time.sleep(.01)
            self.assertEqual(agent.session_id,"session-one")
            agent.interrupt()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(submission.codex_session_turn_active(agent.log.path))
            self.assertEqual(self.events(agent)[-1]['payload']['type'], 'turn_aborted')
        finally:
            agent.interrupt()
            thread.join(5)

    def test_missing_binary_preflight_does_not_replace_running_agent(self):
        with mock.patch.object(processes, 'ensure_tmux_session'), \
             mock.patch.object(claude, 'ensure_available', side_effect=RuntimeError('not signed in')), \
             mock.patch.object(lifecycle.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'not signed in'):
                lifecycle.start_codex_agent(self.root, 'fixture', 'codex', restart=True, model='cc-sonnet')
            run.assert_not_called()

    def test_chat_only_rejected_before_auth(self):
        with mock.patch.dict(os.environ, {'TELEAGENT_CODEX_ACCESS_MODE':'chat-only'}), \
             mock.patch.object(claude.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'full-access'):
                claude.ensure_available()
            run.assert_not_called()

    def test_real_terminal_escape_interrupts_without_enter(self):
        master, slave = pty.openpty()
        runtime, logs = self.root/'terminal-runtime', self.root/'terminal-logs'
        env = dict(os.environ, TELEAGENT_CLAUDE_BIN=str(self.binary),
                   TELEAGENT_CODEX_ACCESS_MODE='full-access')
        proc = subprocess.Popen([sys.executable, str(ROOT/'scripts/claude_agent_runner.py'),
            '--model','cc-sonnet','--repo',str(self.root),'--state-dir',str(runtime),
            '--codex-home',str(logs)],stdin=slave,stdout=slave,stderr=slave,env=env)
        os.close(slave)
        def await_condition(check):
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if check():return
                time.sleep(.02)
            self.fail('native terminal did not confirm the expected event')
        try:
            await_condition(lambda:list(logs.rglob('*.jsonl')))
            log=next(logs.rglob('*.jsonl'))
            os.write(master,b'wait\n')
            await_condition(lambda:(runtime/'claude_session.json').exists())
            self.assertTrue(submission.codex_session_turn_active(log))
            os.write(master,b'\x1b')
            await_condition(lambda:'turn_aborted' in log.read_text())
            self.assertFalse(submission.codex_session_turn_active(log))
            self.assertIsNone(proc.poll())
        finally:
            proc.terminate()
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill();proc.wait()
            os.close(master)

    def test_claude_command_selects_native_harness(self):
        args=argparse.Namespace(codex_usage_state_path=str(self.root/'usage.json'),
            codex_reset_state_path=str(self.root/'reset.json'), agent_lifecycle_state_path=str(self.root/'life.json'),
            repo_root=self.root,session='fixture',codex_window='codex',max_log_chars=4000,target_pane='fixture:codex.0')
        update={'update_id':100,'message':{'message_id':101,'date':1900000000,
                'chat':{'id':'123'},'from':{'id':456},'text':'/claude opus max'}}
        with mock.patch.object(registry,'active_agent_for_pane',return_value=None), \
             mock.patch.object(processes,'tmux_tail',return_value=''), \
             mock.patch.object(claude,'ensure_available',return_value=str(self.binary)), \
             mock.patch.object(lifecycle,'start_codex_agent',return_value=('fixture:codex.0','started',None)) as start, \
             mock.patch.object(transport,'send_reply') as send:
            telegram_inbox.handle_update(update,args,{},'token','123',self.root/'listener.jsonl')
        self.assertEqual(start.call_args.kwargs['model'],'cc-opus')
        self.assertEqual(start.call_args.kwargs['reasoning_effort'],'max')
        self.assertIn('Claude Code started',send.call_args.args[2])

    def test_failed_claude_command_leaves_stopped_lifecycle_unchanged(self):
        args=argparse.Namespace(codex_usage_state_path=str(self.root/'usage.json'),
            codex_reset_state_path=str(self.root/'reset.json'),agent_lifecycle_state_path=str(self.root/'life.json'),
            repo_root=self.root,session='fixture',codex_window='codex',max_log_chars=4000,target_pane='fixture:codex.0')
        lifecycle.set_agent_desired_state(args,'stopped','test')
        update={'update_id':100,'message':{'message_id':101,'date':1900000000,
                'chat':{'id':'123'},'from':{'id':456},'text':'/claude'}}
        with mock.patch.object(registry,'active_agent_for_pane',return_value=None), \
             mock.patch.object(processes,'tmux_tail',return_value=''), \
             mock.patch.object(claude,'ensure_available',side_effect=RuntimeError('not signed in')), \
             mock.patch.object(lifecycle,'start_codex_agent') as start, \
             mock.patch.object(transport,'send_reply'):
            telegram_inbox.handle_update(update,args,{},'token','123',self.root/'listener.jsonl')
        start.assert_not_called()
        self.assertEqual(lifecycle.agent_desired_state(args),'stopped')


if __name__ == '__main__':
    unittest.main()
