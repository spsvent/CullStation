#!/usr/bin/env python3
"""Check instruction entry points/configuration; cannot attest model behavior."""
import json
import os
from pathlib import Path
import re
import sys


def main():
    root = Path.cwd()
    errors = []

    def read(path):
        try:
            return (root/path).read_text()
        except (OSError, UnicodeError):
            errors.append('Missing or unreadable: ' + path)
            return ''

    agents = read('AGENTS.md')
    if '## Required agent startup' not in agents:
        errors.append('AGENTS.md must contain the Required agent startup checklist.')
    for path, first in [('CLAUDE.md', '@AGENTS.md'), ('GEMINI.md', '@./AGENTS.md')]:
        if read(path).splitlines()[:1] != [first]:
            errors.append(path + ' must import the shared AGENTS.md on its first line.')
    if 'AGENTS.md' not in read('.github/copilot-instructions.md'):
        errors.append('Copilot must point to AGENTS.md.')
    cursor = read('.cursor/rules/skypark.mdc')
    frontmatter = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', cursor, re.S)
    if not frontmatter or not re.search(r'^alwaysApply: true[ \t]*$', frontmatter.group(1), re.M) or '@AGENTS.md' not in cursor:
        errors.append('Cursor must always apply the AGENTS.md startup rule.')
    owners = read('.github/CODEOWNERS')
    if not re.search(r'^\*\s+@\S+', owners, re.M):
        errors.append('CODEOWNERS must identify a default owner.')
    read('.github/PULL_REQUEST_TEMPLATE.md')
    read('docs/handoffs/README.md')
    read('.githooks/check_identity.py')
    for name in ['pre-commit', 'pre-push', 'pre-merge-commit', 'prepare-commit-msg', 'pre-applypatch']:
        path = '.githooks/' + name
        if 'check_identity.py' not in read(path) or not os.access(root/path, os.X_OK):
            errors.append('Missing or non-executable identity hook: ' + path)
    try:
        policy = json.loads(read('.githooks/policy.json'))
        repository = policy.get('repository', '')
        if not re.fullmatch(r'spsvent/[A-Za-z0-9_.-]+', repository):
            errors.append('Hook policy must identify a canonical spsvent repository.')
        if os.environ.get('GITHUB_REPOSITORY', repository) != repository:
            errors.append('Hook policy does not match the current GitHub repository.')
        if not isinstance(policy.get('default_branch'), str) or not policy['default_branch']:
            errors.append('Hook policy must identify the actual default branch.')
    except (ValueError, AttributeError, TypeError):
        errors.append('Hook policy is not valid JSON configuration.')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('PASS: SkyPark agent instruction entry points and guard configuration.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
