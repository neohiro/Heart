#!/usr/bin/env python3
"""
banctl.py — strict ban enforcement for neohiro surfaces.

Per BAN_ENFORCEMENT.md: a godadmin-only tool. Adds, lists, or removes
ban entries from the single source of truth at:
    /shared/brain/audit/ban_list.yaml

Every add/remove also:
  1. Calls GitHub API (gh api) to remove the user from each org.
  2. Revokes OAuth grants (gh api).
  3. Emits an audit line to /shared/heart/audit/ban_enforcement.jsonl.
  4. Emits an instant error file if any step fails.

Usage:
    python banctl.py add --identifier github_login --value <user> --reason <reason> --scope all
    python banctl.py add --identifier email_hash --value <sha256> --reason abuse --expires 2027-01-01
    python banctl.py list
    python banctl.py list --format json
    python banctl.py remove --ban-id <id>

Environment:
    NEOHIRO_SHARED_ROOT   Root of /shared (default: /shared)
    NEOHIRO_GH_ORG        Org to remove from (default: neohiro)
    NEOHIRO_GH_ORGS       Comma-sep orgs to affect (default: neohiro,transhumanists,FrenzyPenguin)
    GH_TOKEN              GitHub PAT (required for add/remove; list is read-only)

Identifier types: github_login | email_hash | phone_hash | ip_hash |
                 pgp_fingerprint | userdata_id | cookie_token | device_node_id

Scope values:    all | dashboard_only | chat_only |
                 org:neohiro | org:transhumanists | org:FrenzyPenguin |
                 repo:<owner>/<repo>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import ulid


class BanListError(Exception):
    """Raised when the ban_list.yaml is unparseable."""

BUILTIN_REASONS = frozenset([
    'terms_of_service_violation',
    'abuse',
    'rate_limit_breach',
    'spam',
    'harassment',
    'copyright_violation',
    'security_threat',
    'api_misuse',
    'other',
])

IDENTIFIER_TYPES = frozenset([
    'github_login',
    'email_hash',
    'phone_hash',
    'ip_hash',
    'pgp_fingerprint',
    'userdata_id',
    'cookie_token',
    'device_node_id',
])

SCOPE_RE = re.compile(
    r'^(all|dashboard_only|chat_only|'
    r'org:(?:neohiro|transhumanists|FrenzyPenguin)|'
    r'repo:[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)$'
)

SHA256_RE = re.compile(r'^[a-f0-9]{64}$')


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _ulid_now() -> str:
    return ulid.new().str


def _shared_root() -> Path:
    return Path(os.environ.get('NEOHIRO_SHARED_ROOT', '/shared'))


def _ban_list_path() -> Path:
    p = _shared_root() / 'brain' / 'audit' / 'ban_list.yaml'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _audit_log_path() -> Path:
    p = _shared_root() / 'heart' / 'audit' / 'ban_enforcement.jsonl'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _instant_error_path(ban_id: str) -> Path:
    p = _shared_root() / 'heart' / 'audit' / 'instant' / f'ban-{ban_id}.yaml'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ─── YAML I/O ─────────────────────────────────────────────────────────────────

def _read_yaml_raw(path: Path) -> str:
    if not path.is_file():
        return ''
    return path.read_text(encoding='utf-8')


def _write_yaml_raw(path: Path, content: str) -> None:
    path.write_text(content, encoding='utf-8')


def _parse_ban_list(raw: str) -> dict:
    if not raw.strip():
        return {'schema_version': 1, 'last_updated': _iso_now(), 'bans': []}
    import yaml
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise BanListError(f'ban_list.yaml parse error: {e}') from e
    if not isinstance(data, dict):
        return {'schema_version': 1, 'last_updated': _iso_now(), 'bans': []}
    data.setdefault('bans', [])
    return data


def _format_ban_list(data: dict) -> str:
    try:
        import yaml
    except ImportError as e:
        print(f'warning: yaml not available for ban_list dump: {e}; falling back to str()', file=sys.stderr)
        return str(data)
    try:
        return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except (yaml.YAMLError, TypeError, ValueError) as e:
        print(f'warning: yaml dump failed for ban_list: {e}; falling back to str()', file=sys.stderr)
        return str(data)


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_identifier(id_type: str, value: str) -> None:
    if id_type not in IDENTIFIER_TYPES:
        die(f'unknown identifier type: {id_type!r}\n  known: {", ".join(sorted(IDENTIFIER_TYPES))}')
    if id_type == 'github_login':
        if not re.fullmatch(r'[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?', value) or len(value) > 39:
            die(f'invalid github_login: {value!r} — must be a valid GitHub username (1-39 chars, alphanumeric, dash, underscore)')
    elif id_type in ('email_hash', 'phone_hash', 'ip_hash', 'cookie_token'):
        if not SHA256_RE.match(value):
            die(f'{id_type} must be a 64-char lowercase hex sha256 (got {len(value)} chars)')
    elif id_type == 'pgp_fingerprint':
        if not re.fullmatch(r'[a-fA-F0-9]{40}', value):
            die(f'pgp_fingerprint must be 40 hex chars (got {len(value)})')
    elif id_type == 'userdata_id':
        try:
            ulid.parse(value)
        except (ValueError, TypeError):
            die(f'userdata_id must be a valid ULID (got {value!r})')
    elif id_type == 'device_node_id' and not re.fullmatch(r'n[a-zA-Z0-9]{19,}', value):
        die(f'device_node_id format error (got {value!r})')


def validate_reason(reason: str) -> None:
    if reason not in BUILTIN_REASONS:
        print(f'warning: reason {reason!r} not in builtin set; allowed: {", ".join(BUILTIN_REASONS)}', file=sys.stderr)


def validate_scope(scope: str) -> None:
    if not SCOPE_RE.match(scope):
        die(f'invalid scope: {scope!r}\n  expected: all | dashboard_only | chat_only | '
            f'org:neohiro | org:transhumanists | org:FrenzyPenguin | repo:owner/repo')


def validate_ban_id(ban_id: str) -> None:
    if not re.fullmatch(r'ban-\d{4}-\d{2}-\d{2}-\d{3}', ban_id):
        die(f'invalid ban_id format: {ban_id!r}\n  expected: ban-YYYY-MM-DD-NNN')


# ─── GitHub actions ───────────────────────────────────────────────────────────

def _parse_http_status(text: str) -> int | None:
    """Pull the HTTP status code from a `gh api --include` response block.

    `gh api --include` writes the response headers + body to stdout; the
    status line looks like 'HTTP/1.1 204 No Content'. When the call fails
    (non-zero exit) the headers may appear in either stdout or stderr.
    Returns the integer status, or None if no parseable status line.
    """
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith('HTTP/'):
            parts = line.split(' ', 2)
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    return None
    return None


def _gh_api(method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    """Call gh api. Returns (status_code, body_str).

    status_code is the HTTP status extracted from the `gh api --include`
    output (e.g. 204, 404, 500). On subprocess-level failures (no gh,
    timeout, spawn error) it returns a sentinel: -1, 124, or 1.
    """
    cmd = ['gh', 'api', '--method', method.upper(), path, '--include']
    if body is not None:
        cmd.extend(['--input', '-'])
    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(body) if body is not None else None,
            text=True,
            timeout=30,
            check=False,
        )
        combined = result.stdout + result.stderr
        status = _parse_http_status(combined)
        if status is not None:
            return status, combined
        if result.returncode == 0:
            # gh succeeded but didn't print a status line. For DELETE,
            # the documented success is 204; for GET/POST, 200.
            return 204 if method.upper() == 'DELETE' else 200, result.stdout
        return result.returncode, combined
    except subprocess.TimeoutExpired:
        return 124, 'timeout'
    except FileNotFoundError:
        return -1, 'gh CLI not found'


def _is_404(status: int) -> bool:
    """Strict 404 check: rely on the integer HTTP status from _gh_api."""
    return status == 404


def _remove_from_github_org(login: str, org: str) -> bool:
    """Remove a user from a GitHub org. Returns True on success (including 404)."""
    status, body = _gh_api('DELETE', f'/orgs/{org}/members/{login}')
    if status in (204, 200):
        return True
    if _is_404(status):
        print(f'  [skip] {login} not a member of {org}', file=sys.stderr)
        return True
    print(f'  [warn] gh api DELETE /orgs/{org}/members/{login}: status={status} body={body[:200]}', file=sys.stderr)
    return False


def _revoke_oauth_grant(client_id: str, login: str) -> bool:
    """Revoke an OAuth grant. Returns True on success (including 404)."""
    status, body = _gh_api('DELETE', f'/applications/{client_id}/grants/{login}')
    if status in (204, 200):
        return True
    if _is_404(status):
        return True
    print(f'  [warn] gh api revoke grant: status={status} body={body[:200]}', file=sys.stderr)
    return False


# ─── Audit log ────────────────────────────────────────────────────────────────

def _emit_audit(surface: str, action: str, identifier_type: str,
                identifier_hash: str, ban_id: str, response_code: int,
                request_path: str = '') -> None:
    """Append one JSONL line to the audit log. Best-effort: a write
    failure is logged to stderr but does NOT propagate, because the
    caller has already written the ban to disk and a crash here would
    be more confusing than helpful."""
    entry = {
        'ts': _iso_now(),
        'ban_id': ban_id,
        'identifier_type': identifier_type,
        'identifier_hash': identifier_hash,
        'surface': surface,
        'action': action,
        'request_path': request_path,
        'response_code': response_code,
    }
    audit_path = _audit_log_path()
    try:
        with audit_path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError as e:
        print(f'banctl: warning: cannot write audit log {audit_path}: {e}', file=sys.stderr)


def _emit_instant_error(ban_id: str, step: str, error: str) -> None:
    """Write an instant-error YAML file. Best-effort (see _emit_audit)."""
    import yaml
    err_path = _instant_error_path(ban_id)
    err_data = {
        'ts': _iso_now(),
        'phase': 'ban_enforcement',
        'severity': 'error',
        'ban_id': ban_id,
        'step': step,
        'error': error,
    }
    try:
        err_path.write_text(yaml.dump(err_data), encoding='utf-8')
    except OSError as e:
        print(f'banctl: warning: cannot write instant error {err_path}: {e}', file=sys.stderr)


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_add(args: argparse.Namespace) -> int:
    validate_identifier(args.identifier, args.value)
    validate_reason(args.reason)
    validate_scope(args.scope)

    try:
        data = _parse_ban_list(_read_yaml_raw(_ban_list_path()))
    except BanListError as e:
        die(str(e))

    # Build the entry
    ban_id = f'ban-{datetime.now(timezone.utc).strftime("%Y-%m-%d")}-{args.seq:03d}'
    entry = {
        'id': ban_id,
        'identifier': args.identifier,
        'value': args.value,
        'reason': args.reason,
        'issued_at': _iso_now(),
        'issued_by': os.environ.get('NEOHIRO_GH_USER', 'godadmin'),
        'expires_at': 'never',
        'scope': args.scope,
    }
    if args.expires:
        try:
            exp_dt = datetime.fromisoformat(args.expires.replace('Z', '+00:00'))
            entry['expires_at'] = exp_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            die(f'--expires must be YYYY-MM-DD or ISO8601 (got {args.expires!r})')

    # Idempotency check: skip if identical entry already exists
    ban_path = _ban_list_path()
    for existing in data.get('bans', []):
        if existing.get('identifier') == args.identifier and existing.get('value') == args.value:
            if not args.force:
                print(f'already banned: {args.identifier}={args.value} (id={existing["id"]})')
                return 0
            print(f'force: replacing existing ban {existing["id"]}', file=sys.stderr)
            data['bans'].remove(existing)
            break

    # Append new ban
    data['bans'].append(entry)
    data['last_updated'] = _iso_now()
    data['schema_version'] = max(data.get('schema_version', 1), 1)

    # Write atomically: stage → rename
    stage_path = ban_path.with_suffix('.yaml.stage')
    _write_yaml_raw(stage_path, _format_ban_list(data))
    try:
        stage_path.replace(ban_path)
    except OSError as e:
        raise BanListError(f'failed to persist ban list: {e}') from e

    # GitHub enforcement (org removal)
    orgs = os.environ.get('NEOHIRO_GH_ORGS', 'neohiro,transhumanists,FrenzyPenguin').split(',')
    errors = []
    if args.identifier == 'github_login':
        for org in orgs:
            org = org.strip()
            if not org:
                continue
            ok = _remove_from_github_org(args.value, org)
            if not ok:
                errors.append(f'failed to remove {args.value} from {org}')
            else:
                _emit_audit(
                    surface='github_api',
                    action='removed_from_org',
                    identifier_type=args.identifier,
                    identifier_hash=args.value,
                    ban_id=ban_id,
                    response_code=204,
                    request_path=f'/orgs/{org}/members/{args.value}',
                )

    # OAuth revocation
    client_id = os.environ.get('GH_OAUTH_CLIENT_ID', '')
    if client_id and args.identifier == 'github_login':
        ok = _revoke_oauth_grant(client_id, args.value)
        _emit_audit(
            surface='oauth_grant',
            action='revoked' if ok else 'revoke_failed',
            identifier_type=args.identifier,
            identifier_hash=args.value,
            ban_id=ban_id,
            response_code=204 if ok else 500,
        )

    # Ban enforcement audit
    _emit_audit(
        surface='ban_list',
        action='added',
        identifier_type=args.identifier,
        identifier_hash=args.value,
        ban_id=ban_id,
        response_code=200,
    )

    if errors:
        _emit_instant_error(ban_id, 'github_org_removal', '; '.join(errors))
        print(f'ban {ban_id} written, but: ' + '; '.join(errors), file=sys.stderr)
        return 1

    print(f'ban {ban_id} added: {args.identifier}={args.value} scope={args.scope}')
    print(f'  written to: {ban_path}')
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    ban_path = _ban_list_path()
    try:
        data = _parse_ban_list(_read_yaml_raw(ban_path))
    except BanListError as e:
        die(str(e))
    bans = data.get('bans', [])
    now = datetime.now(timezone.utc)

    # Filter expired
    active = []
    for b in bans:
        exp = b.get('expires_at', 'never')
        if exp == 'never':
            active.append(b)
        else:
            try:
                exp_dt = datetime.fromisoformat(exp.replace('Z', '+00:00'))
                if exp_dt > now:
                    active.append(b)
            except ValueError:
                active.append(b)

    if args.format == 'json':
        print(json.dumps({'bans': active, 'total': len(active)}, indent=2))
        return 0

    # Human-readable table
    print(f'{"BAN ID":<25} {"IDENTIFIER":<20} {"VALUE":<30} {"REASON":<30} {"SCOPE":<20} {"EXPIRES":<12}')
    print('-' * 140)
    for b in active:
        identifier = b.get('identifier', '')
        value = b.get('value', '')
        reason = b.get('reason', '')
        scope = b.get('scope', '')
        expires = b.get('expires_at', 'never')
        ban_id = b.get('id', '')
        print(f'{ban_id:<25} {identifier:<20} {str(value)[:28]:<30} {reason[:28]:<30} {scope:<20} {expires:<12}')
    print(f'\n{len(active)} active ban(s) / {len(bans)} total')
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    validate_ban_id(args.ban_id)
    ban_path = _ban_list_path()
    try:
        data = _parse_ban_list(_read_yaml_raw(ban_path))
    except BanListError as e:
        die(str(e))

    removed_entry = None
    new_bans = []
    for b in data.get('bans', []):
        if b.get('id') == args.ban_id:
            removed_entry = b
        else:
            new_bans.append(b)

    if removed_entry is None:
        print(f'ban {args.ban_id} not found')
        return 1

    data['bans'] = new_bans
    data['last_updated'] = _iso_now()

    # Write atomically
    stage_path = ban_path.with_suffix('.yaml.stage')
    _write_yaml_raw(stage_path, _format_ban_list(data))
    try:
        stage_path.replace(ban_path)
    except OSError as e:
        raise BanListError(f'failed to persist ban list: {e}') from e

    _emit_audit(
        surface='ban_list',
        action='removed',
        identifier_type=removed_entry.get('identifier', ''),
        identifier_hash=removed_entry.get('value', ''),
        ban_id=args.ban_id,
        response_code=200,
    )

    print(f'ban {args.ban_id} removed: {removed_entry.get("identifier")}={removed_entry.get("value")}')
    return 0


# ─── CLI ─────────────────────────────────────────────────────────────────────

def die(msg: str) -> NoReturn:
    print(f'banctl: error: {msg}', file=sys.stderr)
    sys.exit(1)


def _determine_seq() -> int:
    """Return the next sequence number for today (1-999)."""
    ban_path = _ban_list_path()
    try:
        data = _parse_ban_list(_read_yaml_raw(ban_path))
    except BanListError as e:
        die(str(e))
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    seq = 1
    for b in data.get('bans', []):
        bid = b.get('id', '')
        if bid.startswith(f'ban-{today}-'):
            try:
                seq = max(seq, int(bid[-3:]) + 1)
            except ValueError:
                continue  # malformed id; ignore and move on
    if seq > 999:
        die(f'more than 999 bans issued today — manually handle {today}')
    return seq


def main() -> int:
    parser = argparse.ArgumentParser(
        prog='banctl',
        description='Strict ban enforcement for neohiro surfaces. Godadmin only.',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    # add
    addp = sub.add_parser('add', help='add a ban entry')
    addp.add_argument('--identifier', required=True,
                      help='github_login | email_hash | phone_hash | ip_hash | pgp_fingerprint | userdata_id | cookie_token | device_node_id')
    addp.add_argument('--value', required=True, help='value of the identifier')
    addp.add_argument('--reason', required=True,
                      help=f'reason ({", ".join(sorted(BUILTIN_REASONS))})')
    addp.add_argument('--scope', default='all',
                      help='all | dashboard_only | chat_only | org:... | repo:... (default: all)')
    addp.add_argument('--expires', default=None,
                      help='ISO8601 expiry, e.g. 2027-01-01 or 2027-01-01T00:00:00Z (default: never)')
    addp.add_argument('--force', action='store_true',
                      help='replace existing ban with same identifier+value')
    # `--seq` is auto-injected by main() from the existing ban list. Declared
    # with a sentinel default of 0 so that the attribute always exists on the
    # namespace; main() replaces it before cmd_add() reads it.
    addp.add_argument('--seq', type=int, default=0,
                      help=argparse.SUPPRESS)
    addp.set_defaults(command_fn=cmd_add)

    # list
    listp = sub.add_parser('list', help='list active bans')
    listp.add_argument('--format', choices=['text', 'json'], default='text',
                       help='output format (default: text)')
    listp.set_defaults(command_fn=cmd_list)

    # remove
    remp = sub.add_parser('remove', help='remove a ban entry')
    remp.add_argument('--ban-id', required=True,
                      help='ban id to remove (e.g. ban-2026-08-30-001)')
    remp.set_defaults(command_fn=cmd_remove)

    args = parser.parse_args()

    # Auto-inject seq for add
    if args.command == 'add':
        args.seq = _determine_seq()

    return args.command_fn(args)


if __name__ == '__main__':
    sys.exit(main())
