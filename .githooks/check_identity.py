#!/usr/bin/env python3
"""Offline clone-local guard; server-side review rules remain necessary."""
import json
from pathlib import Path
import re
import subprocess
import sys


def git(*args):
    result = subprocess.run(['git', *args], text=True, capture_output=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError('Cannot read required Git configuration.')
    return result.stdout.strip()


def main():
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else ''
        if mode not in ('commit', 'push'):
            raise ValueError('Expected commit or push mode.')
        root = Path(git('rev-parse', '--show-toplevel'))
        policy = json.loads((root / '.githooks/policy.json').read_text())
        repository = policy['repository']
        if not re.fullmatch(r'spsvent/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid repository policy.')

        def allowed(url):
            return url in {
                prefix + repository + suffix
                for prefix in ('https://github.com/', 'git@github.com:',
                               'git@github.com-skypark:', 'ssh://git@github.com/',
                               'ssh://git@github.com-skypark/')
                for suffix in ('', '.git')
            }

        for kind in ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT'):
            match = re.search(r'<([^<>]+)> \d+ [+-]\d{4}$', git('var', kind))
            if not match or not match.group(1).lower().endswith('@skyparksantasvillage.com'):
                raise ValueError('A SkyPark work identity is required for author and committer.')
        if not allowed(git('remote', 'get-url', 'origin')):
            raise ValueError('Origin must be this repository in the spsvent organization.')
        push_urls = git('remote', 'get-url', '--push', '--all', 'origin').splitlines()
        if not push_urls or not all(allowed(url) for url in push_urls):
            raise ValueError('All origin push URLs must target this spsvent repository.')
        protected = {'main', 'master', policy['default_branch']}
        if mode == 'commit':
            branch = git('symbolic-ref', '--quiet', 'HEAD')
            if branch in {'refs/heads/' + name for name in protected}:
                raise ValueError('Commit on an issue branch, not the default/main/master branch.')
        else:
            if len(sys.argv) != 4 or not allowed(sys.argv[3]):
                raise ValueError('Actual push destination must be this spsvent repository.')
            for line in sys.stdin:
                parts = line.split()
                if len(parts) != 4:
                    raise ValueError('Cannot validate the proposed push ref.')
                if parts[2] in {'refs/heads/' + branch for branch in protected}:
                    raise ValueError('Push an issue branch and open a PR; default-branch pushes are blocked.')
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('SkyPark Git guard: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
