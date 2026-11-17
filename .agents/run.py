#!/usr/bin/env python3
"""Task in, verified PR out. The script owns git, gates and GitHub; models only plan, write, review and QA.

  run.py init                     detect check commands, write config.json + context.md, validate them
  run.py task "<text>" | <file>   one task -> one PR
  run.py queue [OBJECTIVES.md]    split objectives into tasks, one PR each (re-run to resume)
  run.py status                   what happened to each task
  run.py check [build|lint|test]  run the configured checks in the current directory (used by the worker)
"""
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE = HERE / 'state'
CHECK = f'python3 {HERE}/run.py check'
READ_ONLY = ['--tools', 'Read,Grep,Glob,Bash', '--allowedTools',
             'Read,Grep,Glob,Bash(git diff *),Bash(git show *),Bash(git log *),Bash(ls *)']
CFG = {
    'base': 'main',
    'checks': {'setup': '', 'build': '', 'lint': '', 'test': ''},
    'copy': [],                      # gitignored files a fresh worktree needs, e.g. ".env.test"
    'protected': ['.agents/', '.github/', '.claude/', 'CLAUDE.md', '.env*'],
    'test_regex': r'(^|/)(tests?|__tests__|specs?|e2e)/|(^|/)test_[^/]*$|[._-](test|spec)\.[^/]+$|Tests?\.[^/.]+$',
    'max_added_lines': 400,          # non-test lines; the bloat budget
    'max_rounds': 3,
    'prove_tests': True,
    'models': {'planner': 'opus', 'worker': 'sonnet', 'reviewer': 'opus', 'qa': 'sonnet'},
    'budget_usd': {'call': 5, 'task': 20},
    'max_turns': 60,
    'check_timeout': 900,
    'session_timeout': 3600,
    'limit_wait': 900,               # seconds between retries while out of usage
    'worker_allow': [],              # extra permission rules, e.g. "Bash(npm test *)"
    'qa': {'instructions': '', 'allow': []},
}
SPENT = {'usd': 0.0, 'calls': 0}

STR, LIST = {'type': 'string'}, {'type': 'array', 'items': {'type': 'string'}}


def schema(optional='', **props):
    # Models drop fields they would leave empty, so those are optional; claude() fills them back in.
    return {'type': 'object', 'properties': props, 'required': [p for p in props if p not in optional.split()]}


BRIEF = schema('no_test_reason assumptions out_of_scope blocker', title=STR, goal=STR, files=LIST, acceptance=LIST, test_plan=STR, no_test_reason=STR,
               assumptions=LIST, out_of_scope=LIST, risk={'type': 'string', 'enum': ['low', 'medium', 'high']},
               blocker=STR)
WORK = schema(status={'type': 'string', 'enum': ['done', 'blocked']}, summary=STR)
REVIEW = schema(summary=STR, findings={'type': 'array', 'items': schema(
    severity={'type': 'string', 'enum': ['important', 'nit', 'preexisting']}, where=STR, issue=STR, fix=STR)})
QA = schema(verdict={'type': 'string', 'enum': ['pass', 'fail', 'unknown']}, summary=STR,
            steps={'type': 'array', 'items': schema(did=STR, expected=STR, observed=STR)})
SPLIT = schema(tasks={'type': 'array', 'items': schema('depends_on', id=STR, title=STR, task=STR, depends_on=STR)})
INIT = schema('setup build lint test qa_instructions context', setup=STR, build=STR, lint=STR, test=STR, qa_instructions=STR, context=STR)


class Blocked(Exception):
    """This task cannot continue; the queue moves on."""


def sh(args, cwd=ROOT, check=True, data=None, timeout=300):
    p = subprocess.run([str(a) for a in args], cwd=cwd, input=data, capture_output=True, text=True,
                       errors='replace', timeout=timeout)
    if check and p.returncode:
        raise Blocked(f'{" ".join(map(str, args[:3]))} failed: {(p.stderr or p.stdout)[-1500:]}')
    return p


def git(*args, cwd=ROOT):
    return sh(['git', *args], cwd=cwd).stdout.strip()


def test_count(text):
    # ponytail: heuristic over common runners (pytest, jest, cargo, XCTest, unittest); 0 = unknown, count gate off.
    return sum(int(a or b or c) for a, b, c in
               re.findall(r'(\d+) (?:tests? )?passed|Executed (\d+) tests?|Ran (\d+) tests?', text))


def run_check(kind, cwd):
    cmd = CFG['checks'].get(kind)
    if not cmd:
        return None
    try:
        p = subprocess.run(['bash', '-lc', cmd], cwd=cwd, capture_output=True, text=True, errors='replace',
                           timeout=CFG['check_timeout'])
        out, code = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, code = f'timed out after {CFG["check_timeout"]}s', 124
    return {'cmd': cmd, 'exit': code, 'tail': out[-3000:], 'tests': test_count(out) if kind == 'test' else None}


def is_test(path):
    return bool(re.search(CFG['test_regex'], path))


def protected(path):
    return any(('/' + p) in ('/' + path) if p.endswith('/') else fnmatch.fnmatch(Path(path).name, p)
               for p in CFG['protected'])


def in_scope(path, files):
    return any(path == f or (f.endswith('/') and path.startswith(f)) or fnmatch.fnmatch(path, f) for f in files)


def stop_check():
    if (HERE / 'STOP').exists():
        raise SystemExit('.agents/STOP exists; stopping. Delete it to continue.')


def claude(role, prompt, cwd, logdir, out_schema, model=None, resume=None, tools=READ_ONLY):
    """One bounded headless session. Returns (structured_output, session_id)."""
    stop_check()
    if SPENT['usd'] >= CFG['budget_usd']['task']:
        raise Blocked(f'task budget of ${CFG["budget_usd"]["task"]} spent')
    context = HERE / 'context.md'
    system = logdir / f'{role}.system.md'
    system.write_text((HERE / 'prompts' / f'{role}.md').read_text()
                      + ('\n\n# Project context\n' + context.read_text() if context.exists() else ''))
    # --setting-sources '' keeps personal hooks, plugins and MCP servers out of unattended runs.
    args = ['claude', '-p', '--model', model or CFG['models'][role], '--max-turns', CFG['max_turns'],
            '--max-budget-usd', CFG['budget_usd']['call'], '--output-format', 'json', '--setting-sources', '',
            '--strict-mcp-config', '--permission-mode', 'dontAsk', '--append-system-prompt-file', system,
            '--json-schema', json.dumps(out_schema), *tools]
    if resume:
        args += ['--resume', resume]
    try:
        p = sh(args, cwd=cwd, check=False, data=prompt, timeout=CFG['session_timeout'])
        out = p.stdout
    except subprocess.TimeoutExpired:
        out = json.dumps({'is_error': True, 'result': f'timed out after {CFG["session_timeout"]}s'})
    SPENT['calls'] += 1
    (logdir / f'{SPENT["calls"]:02d}-{role}.json').write_text(out)
    try:
        r = json.loads(out)
    except ValueError:
        r = {'is_error': True, 'result': out[-1500:]}
    SPENT['usd'] += r.get('total_cost_usd') or 0
    if r.get('is_error') or not isinstance(r.get('structured_output'), dict):
        reason = f'{r.get("result") or r.get("subtype")} {r.get("errors") or ""} {r.get("api_error_status") or ""}'[-800:]
        if re.search(r'429|rate.?limit|usage limit|credit balance|quota', reason, re.I):
            # ponytail: the reset time is not reported reliably, so poll; a refused call costs nothing.
            print(f'usage limit during {role}; retrying every {CFG["limit_wait"]}s. {reason[:200]}', flush=True)
            for _ in range(CFG['limit_wait'] // 5):
                stop_check()
                time.sleep(5)
            return claude(role, prompt, cwd, logdir, out_schema, model, r.get('session_id') or resume, tools)
        raise Blocked(f'{role} session failed: {reason}')
    out = r['structured_output']
    for name, spec in out_schema['properties'].items():
        out.setdefault(name, [] if spec['type'] == 'array' else '')
    return out, r.get('session_id')


def worker_tools(logdir):
    deny = ['Read(**/.env*)', 'Bash(git push*)', 'Bash(gh *)']
    for p in CFG['protected']:
        deny += [f'{t}(**/{p}**)' if p.endswith('/') else f'{t}(**/{p})' for t in ('Edit', 'Write')]
    allow = ['Read', 'Grep', 'Glob', 'Edit', 'Write', 'Bash(git status*)', 'Bash(git diff*)', 'Bash(git log*)',
             'Bash(git show*)', 'Bash(ls *)', f'Bash({CHECK}*)', *CFG['worker_allow']]
    path = logdir / 'worker.settings.json'
    path.write_text(json.dumps({'permissions': {'allow': allow, 'deny': deny}}, indent=1))
    return ['--settings', path]


def changed(wt, base):
    """[(status, path)] of everything in the worktree versus base, staged."""
    git('add', '-A', cwd=wt)
    return [tuple(r.split('\t', 1)) for r in
            git('diff', '--cached', '--no-renames', '--name-status', base, cwd=wt).splitlines()]


def scope_fail(rows, brief):
    out = [p for _, p in rows if protected(p) or not in_scope(p, brief['files'])]
    return [f'Files changed outside the brief; revert them (if a check generated them, they belong in .gitignore): {out}'] if out else []


def gates(wt, base, brief, baseline):
    """Deterministic checks. Returns (failures, evidence); no failures means the diff may go to review."""
    rows = changed(wt, base)
    if not rows:
        return ['No changes were made.'], {}
    fails = scope_fail(rows, brief)
    gone = [p for s, p in rows if s == 'D' and is_test(p)]
    if gone:
        fails.append(f'Existing test files were deleted; restore them: {gone}')
    if not brief['no_test_reason'] and not any(s != 'D' and is_test(p) for s, p in rows):
        fails.append('No test was added or changed. Add a test that fails without your change.')
    added = sum(int(a) for a, _, p in (line.split('\t') for line in
                git('diff', '--cached', '--no-renames', '--numstat', base, cwd=wt).splitlines())
                if a != '-' and not is_test(p))
    if added > CFG['max_added_lines']:
        fails.append(f'The diff adds {added} non-test lines; the budget is {CFG["max_added_lines"]}. '
                     'Delete everything the brief does not require.')
    if fails:
        return fails, {}
    evidence = {}
    for kind in ('build', 'lint', 'test'):
        r = run_check(kind, wt)
        if r:
            evidence[kind] = r
            if r['exit']:
                return [f'{kind} failed (`{r["cmd"]}`, exit {r["exit"]}):\n{r["tail"]}'], evidence
    was, now = baseline['test']['tests'], evidence['test']['tests']
    if was and now < was:
        fails.append(f'Passing test count dropped from {was} to {now}. Do not remove or skip existing tests.')
    return fails + scope_fail(changed(wt, base), brief), evidence


def tests_prove_change(wt, base, rows):
    """Revert the non-test changes and re-run the suite: it must now fail, or the new tests prove nothing."""
    head = git('rev-parse', 'HEAD', cwd=wt)
    try:
        for status, path in rows:
            if not is_test(path):
                (wt / path).unlink() if status == 'A' else git('checkout', base, '--', path, cwd=wt)
        return run_check('test', wt)['exit'] != 0
    finally:
        git('reset', '-q', '--hard', head, cwd=wt)
        git('clean', '-qfd', cwd=wt)


def baseline(wt, sha):
    cache = STATE / f'baseline-{sha[:12]}.json'
    if cache.exists():
        return json.loads(cache.read_text())
    result = {k: run_check(k, wt) for k in ('build', 'lint', 'test') if CFG['checks'].get(k)}
    red = {k: r['tail'][-600:] for k, r in result.items() if r['exit']}
    if red:
        raise Blocked(f'checks already fail on the base branch before any change; fix that first: {json.dumps(red)}')
    cache.write_text(json.dumps(result))
    return result


def block(text):
    return '```\n' + '\n'.join(text.strip().splitlines()[-15:]) + '\n```'


def pr_body(brief, work, evidence, base_tests, proof, review, qa, rounds):
    def bullets(items):
        return '\n'.join(f'- {i}' for i in items) or '- none'
    parts = [f'**Summary**\n{work["summary"]}', f'**Goal**\n{brief["goal"]}',
             f'**Acceptance criteria**\n{bullets(brief["acceptance"])}',
             f'**Assumptions taken**\n{bullets(brief["assumptions"])}',
             '**Verification** (run by the harness on the final commit, not reported by the agent)']
    for kind, r in evidence.items():
        count = f', {base_tests} -> {r["tests"]} passing tests' if kind == 'test' and r['tests'] else ''
        parts.append(f'`{r["cmd"]}`: exit {r["exit"]}{count}\n{block(r["tail"])}')
    parts += [f'New tests fail when the source change is reverted: {proof}',
              f'**Review** (fresh context, round {rounds} of {CFG["max_rounds"]})\n{review["summary"]}\n'
              + bullets(f'{f["severity"]}: {f["where"]} {f["issue"]}' for f in review['findings'])]
    if qa:
        parts.append(f'**QA: {qa["verdict"]}**\n{qa["summary"]}\n'
                     + bullets(f'{s["did"]} | expected: {s["expected"]} | observed: {s["observed"]}' for s in qa['steps']))
    parts += [f'**Out of scope**\n{bullets(brief["out_of_scope"])}',
              f'Risk: {brief["risk"]}. Agent cost: ${SPENT["usd"]:.2f} over {SPENT["calls"]} sessions.']
    return '\n\n'.join(parts)


def run_task(text, slug, dep_branch=None):
    """One task -> one PR. Returns {'status': 'pr'|'blocked', ...}; a blocked task keeps its worktree."""
    SPENT.update(usd=0.0, calls=0)
    logdir = STATE / slug
    logdir.mkdir(parents=True, exist_ok=True)
    branch, wt = f'agent/{slug}', ROOT.parent / (ROOT.name + '.worktrees') / slug
    result = {'status': 'blocked', 'branch': branch, 'worktree': str(wt)}
    try:
        if not dep_branch:
            git('fetch', '-q', 'origin', CFG['base'])
        git('worktree', 'add', '-q', '-b', branch, wt, dep_branch or f'origin/{CFG["base"]}')
        base = git('rev-parse', 'HEAD', cwd=wt)
        for f in CFG['copy']:
            if (ROOT / f).is_file():
                (wt / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / f, wt / f)
        setup = run_check('setup', wt)
        if setup and setup['exit']:
            raise Blocked(f'setup failed: {setup["tail"][-800:]}')
        base_checks = baseline(wt, base)

        brief, _ = claude('planner', f'Write the brief for this task. Do not implement it.\n\nTask:\n{text}', wt, logdir, BRIEF)
        (logdir / 'brief.json').write_text(json.dumps(brief, indent=1))
        if brief['blocker']:
            raise Blocked(f'planner needs an answer: {brief["blocker"]}')
        bad = [f for f in brief['files'] if f.startswith('/') or '..' in f or protected(f)]
        if bad or not brief['files']:
            raise Blocked(f'brief names no files or protected ones: {bad}')

        prompt = f'Implement this brief. Run all checks with exactly `{CHECK}` as its own Bash call.\n{json.dumps(brief, indent=1)}'
        session, tools, seen, fails = None, worker_tools(logdir), [], []
        for rounds in range(1, CFG['max_rounds'] + 1):
            work, session = claude('worker', prompt, wt, logdir, WORK, resume=session, tools=tools)
            if work['status'] == 'blocked':
                raise Blocked(f'worker: {work["summary"]}')
            fails, evidence = gates(wt, base, brief, base_checks)
            proof, review, qa = 'not applicable (no test required)', None, None
            if not fails:
                rows = changed(wt, base)
                if sh(['git', 'diff', '--cached', '--quiet'], cwd=wt, check=False).returncode:
                    git('commit', '-q', '-m', f'wip: round {rounds}', cwd=wt)
                if CFG['prove_tests'] and not brief['no_test_reason'] and any(not is_test(p) for _, p in rows):
                    proof = 'yes'
                    if not tests_prove_change(wt, base, rows):
                        fails = ['Your tests still pass with your source changes reverted, so they do not test the '
                                 'change. Write a test that fails without it.']
            if not fails:
                touched = [p for s, p in rows if s == 'M' and is_test(p)]
                review, _ = claude('reviewer', json.dumps({
                    'brief': brief, 'diff_command': f'git diff {base} HEAD', 'existing_tests_modified': touched,
                    'already_reported_do_not_repeat': seen}, indent=1), wt, logdir, REVIEW)
                important = [f for f in review['findings'] if f['severity'] == 'important']
                seen += important
                fails = [f'Review: {f["where"]}: {f["issue"]} Fix: {f["fix"]}' for f in important]
            if not fails and CFG['qa']['instructions']:
                qa, _ = claude('qa', json.dumps({'brief': brief, 'how_to_run': CFG['qa']['instructions']}, indent=1),
                               wt, logdir, QA, tools=['--tools', 'Read,Grep,Glob,Bash', '--allowedTools',
                                                      ','.join(['Read', 'Grep', 'Glob', *CFG['qa']['allow']])])
                if qa['verdict'] == 'fail':
                    fails = [f'QA failed: {qa["summary"]} {json.dumps(qa["steps"])}']
                git('reset', '-q', '--hard', cwd=wt)   # QA may not leave edits behind
                git('clean', '-qfd', cwd=wt)
            if not fails:
                break
            prompt = ('Not done. Fix exactly these and nothing else, then finish again:\n- ' + '\n- '.join(fails))
        if fails:
            raise Blocked(f'{CFG["max_rounds"]} rounds used; still failing:\n- ' + '\n- '.join(fails))

        stop_check()
        git('reset', '-q', '--soft', base, cwd=wt)
        git('commit', '-q', '-m', f'{brief["title"]}\n\n{brief["goal"]}', cwd=wt)
        body = logdir / 'pr.md'
        body.write_text(pr_body(brief, work, evidence, base_checks['test']['tests'], proof, review, qa, rounds))
        git('push', '-q', '-u', 'origin', branch, cwd=wt)
        url = sh(['gh', 'pr', 'create', '--head', branch, '--base', dep_branch or CFG['base'],
                  '--title', brief['title'], '--body-file', body], cwd=wt).stdout.strip()
        sh(['git', 'worktree', 'remove', '--force', wt], check=False)
        result = {'status': 'pr', 'branch': branch, 'pr': url}
    except Blocked as error:
        result['reason'] = str(error)
    result.update(cost_usd=round(SPENT['usd'], 2), task=text[:200])
    (logdir / 'result.json').write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1), flush=True)
    return result


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:40].strip('-') + f'-{int(time.time()) % 100000}'


def queue(path):
    text = path.read_text()
    key, qfile = hashlib.sha256(text.encode()).hexdigest(), STATE / 'queue.json'
    q = json.loads(qfile.read_text()) if qfile.exists() else {}
    if q.get('key') != key:   # new or edited objectives: plan again
        STATE.mkdir(exist_ok=True)
        split, _ = claude('splitter', f'Split these objectives into tasks. Do not implement them.\n\n{text}', ROOT, STATE, SPLIT,
                         model=CFG['models']['planner'])
        q = {'key': key, 'tasks': split['tasks']}
    done = {t['id']: t for t in q['tasks']}
    for t in q['tasks']:
        if t.get('status') == 'pr':
            continue
        dep = done.get(t.get('depends_on'))
        if dep and dep.get('status') != 'pr':
            t.update(status='skipped', reason=f'depends on {dep["id"]}, which has no PR')
        else:
            # ponytail: one dependency, stacked on its branch; a merged dependency means branch from base again.
            merged = dep and sh(['gh', 'pr', 'view', dep['pr'], '--json', 'state', '--jq', '.state'],
                                check=False).stdout.strip() == 'MERGED'
            t.update(run_task(t['task'], slugify(t['title']), dep['branch'] if dep and not merged else None))
        qfile.write_text(json.dumps(q, indent=1))


def init():
    STATE.mkdir(exist_ok=True)
    found, _ = claude('init', 'Inspect this repository and report how to check it.', ROOT, STATE, INIT,
                      model=CFG['models']['planner'])
    path = HERE / 'config.json'
    cfg = json.loads(path.read_text()) if path.exists() else {}
    for k in ('setup', 'build', 'lint', 'test'):   # never overwrite what a human filled in
        cfg.setdefault('checks', {})[k] = cfg.get('checks', {}).get(k) or found[k]
    cfg.setdefault('qa', {}).setdefault('instructions', found['qa_instructions'])
    path.write_text(json.dumps(cfg, indent=2) + '\n')
    if not (HERE / 'context.md').exists():
        (HERE / 'context.md').write_text(found['context'].strip() + '\n')
    CFG['checks'].update(cfg['checks'])
    for kind in ('setup', 'build', 'lint', 'test'):
        r = run_check(kind, ROOT)
        if r:
            print(f'{kind}: `{r["cmd"]}` -> exit {r["exit"]}' + (f', {r["tests"]} tests counted' if r['tests'] else ''))
            if r['exit']:
                print(r['tail'][-800:])
    print('Review .agents/config.json and .agents/context.md, then commit them.'
          + ('' if cfg['checks']['test'] else ' No test command found: add one before running tasks.'))


def main():
    cmd, arg = (sys.argv[1:] + ['', ''])[:2]
    path = HERE / 'config.json'
    for k, v in (json.loads(path.read_text()) if path.exists() else {}).items():
        CFG[k].update(v) if isinstance(CFG.get(k), dict) else CFG.__setitem__(k, v)
    if cmd == 'check':
        code = 0
        for kind in [arg] if arg else ['build', 'lint', 'test']:
            r = run_check(kind, Path.cwd())
            if r:
                print(f'== {kind}: `{r["cmd"]}` exit {r["exit"]}\n{r["tail"]}')
                code = code or r['exit']
        sys.exit(code)
    if cmd == 'status':
        for f in sorted(STATE.glob('*/result.json'), key=lambda f: f.stat().st_mtime):
            r = json.loads(f.read_text())
            print(f'{r["status"]:8} {f.parent.name}: {r.get("pr") or r.get("reason", "")[:300]}')
        return
    if cmd == 'init':
        return init()
    if cmd not in ('task', 'queue') or (cmd == 'task' and not arg):
        sys.exit(__doc__)
    for tool in ('git', 'gh', 'claude'):
        if not shutil.which(tool):
            sys.exit(f'missing {tool}')
    if sh(['gh', 'auth', 'status'], check=False).returncode:
        sys.exit('gh is not logged in: run `gh auth login`')
    if not CFG['checks']['test']:
        sys.exit('no test command configured: run `python3 .agents/run.py init` or fill .agents/config.json')
    stop_check()
    if cmd == 'queue':
        return queue(Path(arg) if arg else HERE / 'OBJECTIVES.md')
    text = Path(arg).read_text() if len(arg) < 300 and Path(arg).is_file() else arg
    sys.exit(0 if run_task(text, slugify(text))['status'] == 'pr' else 1)


if __name__ == '__main__':
    main()
