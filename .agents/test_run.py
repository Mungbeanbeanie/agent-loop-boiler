#!/usr/bin/env python3
"""Self-test: real local git, fake `claude` and `gh`. No network, no model spend. Run: python3 .agents/test_run.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run  # noqa: E402

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
role = Path(sys.argv[sys.argv.index('--append-system-prompt-file') + 1]).name.split('.')[0]
counter = Path(os.environ['FAKE']) / role
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
scenario = os.environ['SCENARIO']
out = {}
if role == 'planner':
    out = dict(title='Add double', goal='Add double(x).', files=['calc.py', 'test_calc.py'], acceptance=['double(2) == 4'],
               test_plan='test_calc.py', no_test_reason='', assumptions=[], out_of_scope=[], risk='low', blocker='')
elif role == 'worker':
    Path('calc.py').write_text('def add(a, b):\n    return a + b\n\n\ndef double(x):\n    return x * 2\n')
    check = 'self.assertTrue(True)' if scenario == 'tautology' else 'self.assertEqual(calc.double(2), 4)'
    Path('test_calc.py').write_text('import unittest, calc\n\n\nclass T(unittest.TestCase):\n'
        '    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n\n'
        '    def test_double(self):\n        ' + check + '\n')
    if scenario == 'scope':
        Path('extra.py').write_text('x = 1\n') if n == 1 else Path('extra.py').unlink(missing_ok=True)
    out = dict(status='done', summary='Added double.')
elif role == 'reviewer':
    bad = scenario == 'review' and n == 1
    out = dict(summary='1 important' if bad else '0 important', findings=[dict(
        severity='important', where='calc.py:5', issue='x', fix='y')] if bad else [])
if scenario == 'limit' and role == 'planner' and n == 1:
    print(json.dumps(dict(is_error=True, result='You have hit your usage limit', session_id='s1')))
else:
    print(json.dumps(dict(is_error=False, structured_output=out, session_id='s1', total_cost_usd=0.01)))
'''
FAKE_GH = '''#!/bin/sh
echo "$@" >> "$FAKE/gh.log"
[ "$1 $2" = "pr create" ] && echo https://example.test/pr/1
exit 0
'''


def scenario(name):
    tmp = Path(tempfile.mkdtemp())
    fake, proj = tmp / 'fake', tmp / 'proj'
    fake.mkdir()
    for tool, body in (('claude', FAKE_CLAUDE), ('gh', FAKE_GH)):
        (fake / tool).write_text(body)
        (fake / tool).chmod(0o755)
    g = lambda *a, cwd=proj: subprocess.run(['git', *a], cwd=cwd, check=True, capture_output=True, text=True).stdout
    g('init', '-q', '--bare', '-b', 'main', str(tmp / 'origin.git'), cwd=tmp)
    g('clone', '-q', str(tmp / 'origin.git'), str(proj), cwd=tmp)
    (proj / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
    (proj / 'test_calc.py').write_text('import unittest, calc\n\n\nclass T(unittest.TestCase):\n'
                                       '    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n')
    (proj / '.gitignore').write_text('__pycache__/\n')
    shutil.copytree(HERE, proj / '.agents', ignore=shutil.ignore_patterns('state', '__pycache__', 'config.json', 'context.md'))
    (proj / '.agents/config.json').write_text(json.dumps({'checks': {'test': 'python3 -m unittest -q 2>&1'}, 'limit_wait': 5}))
    g('-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '-A')
    g('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init')
    g('push', '-q', 'origin', 'HEAD:main')
    env = {**os.environ, 'PATH': f'{fake}:{os.environ["PATH"]}', 'FAKE': str(fake), 'SCENARIO': name,
           'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
    p = subprocess.run([sys.executable, str(proj / '.agents/run.py'), 'task', 'add double'], cwd=proj, env=env,
                       capture_output=True, text=True)
    result = json.loads(next((proj / '.agents/state').glob('*/result.json')).read_text())
    calls = {f.name: int(f.read_text()) for f in fake.iterdir() if f.name in ('planner', 'worker', 'reviewer')}
    pushed = g('ls-remote', '--heads', 'origin', 'agent/*')
    files = g('diff', '--name-only', 'origin/main', result['branch']).split() if pushed else []
    commits = g('rev-list', '--count', f'origin/main..{result["branch"]}').strip() if pushed else '0'
    shutil.rmtree(tmp)
    return p.returncode, result, calls, pushed, files, commits


def main():
    assert run.is_test('test_calc.py') and run.is_test('src/a.test.ts') and run.is_test('pkg/a_test.go')
    assert run.is_test('Tests/FooTests.swift') and not run.is_test('src/contest.py') and not run.is_test('calc.py')
    assert run.protected('.agents/run.py') and run.protected('web/.env.local') and run.protected('a/.github/x.yml')
    assert not run.protected('src/agents/x.py')
    assert run.in_scope('a/b.py', ['a/']) and run.in_scope('a.py', ['a.py']) and not run.in_scope('ab.py', ['a.py'])
    assert run.test_count('Ran 3 tests in 0.1s') == 3 and run.test_count('12 passed, 1 skipped') == 12
    assert run.test_count('nothing here') == 0

    code, result, calls, pushed, files, commits = scenario('happy')
    assert code == 0 and result['status'] == 'pr' and result['pr'].endswith('/pr/1'), result
    assert pushed and sorted(files) == ['calc.py', 'test_calc.py'] and commits == '1', (files, commits)
    assert calls == {'planner': 1, 'worker': 1, 'reviewer': 1}, calls

    code, result, calls, pushed, files, _ = scenario('scope')   # out-of-scope file is sent back, never reviewed
    assert code == 0 and calls == {'planner': 1, 'worker': 2, 'reviewer': 1} and 'extra.py' not in files, (result, calls)

    code, result, calls, pushed, files, _ = scenario('review')  # an important finding costs one more round
    assert code == 0 and calls == {'planner': 1, 'worker': 2, 'reviewer': 2}, (result, calls)

    code, result, calls, pushed, _, _ = scenario('tautology')   # a test that proves nothing never becomes a PR
    assert code == 1 and result['status'] == 'blocked' and 'reverted' in result['reason'], result
    assert not pushed and calls['worker'] == 3 and 'reviewer' not in calls, calls
    code, result, calls, _, _, _ = scenario('limit')            # out of usage: wait, retry, finish
    assert code == 0 and result['status'] == 'pr' and calls['planner'] == 2, (result, calls)
    print('ok: 5 scenarios')


if __name__ == '__main__':
    main()
