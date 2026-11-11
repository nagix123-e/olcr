"""GitHub CLI adapter for reviewed PR creation and read-only PR inspection."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .git_workspace import GitError, github_url, network_allowed, safe_error


class GitHubReview:
    def __init__(self, git):
        self.git = git

    def repo(self, root, remote):
        urls = self.git._run(root, ['remote', 'get-url', '--push', '--all', remote]).splitlines()
        if len(urls) != 1:
            raise GitError('GIT_MULTIPLE_PUSH_DESTINATIONS_UNSUPPORTED')
        url = github_url(urls[0])
        return re.sub(r'\.git$', '', re.split(r'github\.com[:/]', url, maxsplit=1)[1])

    def run(self, root, args, external_access, *, as_json=False):
        if not network_allowed(external_access):
            raise GitError('EXTERNAL_ACCESS_REQUIRED')
        binary = shutil.which('gh')
        if not binary:
            raise GitError('GITHUB_CLI_NOT_INSTALLED')
        try:
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                result = subprocess.run([binary, *args], cwd=root, env=self.git._env({'GH_HOST': 'github.com'}),
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=90)
                if out.tell() > 4_000_000:
                    raise GitError('GITHUB_RESPONSE_TOO_LARGE')
                out.seek(0); value = out.read().decode('utf-8', 'replace')
                if result.returncode:
                    err.seek(0)
                    raise GitError(safe_error(err.read(12000).decode('utf-8', 'replace')) or 'GITHUB_REQUEST_FAILED')
        except subprocess.TimeoutExpired:
            raise GitError('GITHUB_TIMEOUT_CHECK_PR_STATE_BEFORE_RETRY') from None
        except OSError:
            raise GitError('GITHUB_EXECUTABLE_UNAVAILABLE') from None
        if not as_json:
            return value.strip()
        try: return json.loads(value)
        except ValueError: raise GitError('GITHUB_RESPONSE_INVALID') from None

    def list(self, root, remote, external_access):
        repo = self.repo(root, remote)
        items = self.run(root, ['pr', 'list', '--repo', repo, '--limit', '30', '--state', 'open', '--json',
            'number,title,url,state,isDraft,headRefName,baseRefName,updatedAt'], external_access, as_json=True)
        if not isinstance(items, list): raise GitError('GITHUB_RESPONSE_INVALID')
        return {'repository': repo, 'pull_requests': items}

    def view(self, root, remote, number, external_access):
        if not 1 <= number <= 2_147_483_647:
            raise GitError('GITHUB_PR_NUMBER_INVALID')
        repo = self.repo(root, remote)
        value = self.run(root, ['pr', 'view', str(number), '--repo', repo, '--json',
            'number,title,url,body,state,isDraft,headRefName,headRefOid,baseRefName,mergeable,reviewDecision,comments,reviews,statusCheckRollup,files'], external_access, as_json=True)
        if not isinstance(value, dict): raise GitError('GITHUB_RESPONSE_INVALID')
        value['diff'] = self.run(root, ['pr', 'diff', str(number), '--repo', repo], external_access)
        return value

    def prepare(self, root, remote, branch, base, title, body, draft, external_access):
        if not title.strip() or not base or base.startswith('-'):
            raise GitError('GITHUB_PR_TITLE_AND_BASE_REQUIRED')
        if self.git._run(root, ['check-ref-format', '--branch', base], ok=(0,128)).strip() != base or branch == base:
            raise GitError('GITHUB_PR_BASE_INVALID')
        if self.git._files(root):
            raise GitError('GITHUB_COMMIT_AND_PUSH_BEFORE_PR')
        url = self.git._push_target(root, remote, branch)
        if not network_allowed(external_access): raise GitError('EXTERNAL_ACCESS_REQUIRED')
        head = self.git._head(root)
        remote_head = self.git._remote_head(root, url, branch)
        if not head or remote_head != head:
            raise GitError('GITHUB_PUSH_CURRENT_BRANCH_BEFORE_PR')
        base_head = self.git._remote_head(root, url, base)
        if not base_head:
            raise GitError('GITHUB_BASE_BRANCH_NOT_FOUND')
        repo = self.repo(root, remote)
        local_base = self.git._run(root, ['rev-parse', '--verify', base_head + '^{commit}'], ok=(0,128)).strip()
        diff = (self.git._run(root, ['diff', '--no-ext-diff', '--no-textconv', base_head + '...' + head])
                if local_base else self.run(root, ['api', '--method', 'GET',
                    f'repos/{repo}/compare/{base_head}...{head}', '--header', 'Accept: application/vnd.github.diff'], external_access))
        if not diff:
            raise GitError('GITHUB_FETCH_BASE_OR_ADD_CHANGES_BEFORE_PR')
        return {'repository': repo, 'remote': remote, 'remote_url': url, 'destination_branch': branch,
                'remote_head': remote_head, 'base': base, 'base_head': base_head, 'title': title.strip(), 'body': body,
                'draft': bool(draft), 'diff': diff}

    def create(self, root, op, external_access):
        if not network_allowed(external_access): raise GitError('EXTERNAL_ACCESS_REQUIRED')
        if (self.git._push_target(root, op['remote'], op['destination_branch']) != op['remote_url']
                or self.git._remote_head(root, op['remote_url'], op['destination_branch']) != op['head']
                or self.git._remote_head(root, op['remote_url'], op['base']) != op['base_head']):
            raise GitError('GITHUB_BRANCH_CHANGED_PREPARE_AGAIN')
        existing = self.run(root, ['pr', 'list', '--repo', op['repository'], '--head', op['destination_branch'],
            '--base', op['base'], '--state', 'open', '--json', 'url'], external_access, as_json=True)
        if not isinstance(existing, list): raise GitError('GITHUB_RESPONSE_INVALID')
        if existing:
            url = existing[0].get('url', '')
            if not re.fullmatch(r'https://github\.com/' + re.escape(op['repository']) + r'/pull/\d+', url, re.I):
                raise GitError('GITHUB_RESPONSE_INVALID')
            return {'pr_url': url, 'pr_already_exists': True}
        # Body files preserve literal newlines and never pass content through a shell.
        with tempfile.TemporaryDirectory(prefix='olcr-pr-') as temporary:
            path = Path(temporary) / 'body.md'
            path.write_text(op['body'], encoding='utf-8')
            args = ['pr', 'create', '--repo', op['repository'], '--head', op['destination_branch'], '--base', op['base'],
                    '--title', op['title'], '--body-file', str(path)]
            if op['draft']: args.append('--draft')
            url = self.run(root, args, external_access)
        if not re.fullmatch(r'https://github\.com/' + re.escape(op['repository']) + r'/pull/\d+', url, re.I):
            raise GitError('GITHUB_PR_RESULT_INVALID_CHECK_REPOSITORY')
        return {'pr_url': url}
