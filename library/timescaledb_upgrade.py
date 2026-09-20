#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Extension-aware SQL operations for the TimescaleDB role.

Ansible owns containers, physical copies and activation. This module only talks
to those containers through docker exec; it never renames or deletes PGDATA.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r"""
---
module: timescaledb_upgrade
short_description: Upgrade TimescaleDB extensions or restore a staged cluster
description:
  - Updates installed TimescaleDB and Toolkit extensions using fresh SQL sessions.
  - Migrates isolated source and target containers with matching extension versions.
  - The caller must preserve source data and isolate both migration containers.
author: salty
options:
  action:
    description: Extension maintenance or full logical migration.
    type: str
    choices: [upgrade, migrate]
    required: true
  source:
    description: Source Docker container name.
    type: str
    required: true
  target:
    description: Empty target Docker container for migration.
    type: str
  username:
    description: PostgreSQL superuser used in both containers.
    type: str
    required: true
  temporary_directory:
    description: Parent directory for temporary logical backups on the host.
    type: path
"""

EXAMPLES = r"""
- name: Update installed extensions
  timescaledb_upgrade:
    action: upgrade
    source: timescaledb
    username: salty
- name: Migrate staged data
  timescaledb_upgrade:
    action: migrate
    source: timescaledb_old
    target: timescaledb_new
    username: salty
"""

RETURN = r"""
databases:
  description: Database names checked or migrated.
  type: list
  elements: str
  returned: success
extensions:
  description: Installed extension versions per database after the operation.
  type: dict
  returned: success
"""

MANAGED = {"timescaledb", "timescaledb_toolkit"}


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    return "E'" + value.replace('\\', '\\\\').replace("'", "''") + "'"


def version(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"Unsupported extension version {value!r}; use a stable TimescaleDB release.")
    return tuple(map(int, value.split('.')))


class Cluster:
    def __init__(self, container, username):
        self.container = container
        self.username = username

    def command(self, argv, stdin=None, stdout=None):
        result = subprocess.run(
            ['docker', 'exec', '-i', self.container] + argv,
            input=stdin if isinstance(stdin, bytes) else None,
            stdin=stdin if stdin is not None and not isinstance(stdin, bytes) else None,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"{self.container}: {argv!r} failed ({result.returncode}): "
                + result.stderr.decode(errors='replace')[-8000:]
            )
        return result.stdout.decode() if result.stdout is not None else ''

    def sql(self, database, statement):
        # --dbname accepts conninfo/URIs too. Quote the database as a conninfo
        # value so names containing '=' cannot redirect the connection.
        dbname = "dbname='" + database.replace('\\', '\\\\').replace("'", "\\'") + "'"
        return self.command(
            ['psql', '-X', '--set=ON_ERROR_STOP=1', '--tuples-only', '--no-align',
             '--username', self.username, '--dbname', dbname], statement.encode()
        ).strip()

    def rows(self, database, query):
        value = self.sql(database, f"SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) FROM ({query}) t;")
        return json.loads(value)

    def databases(self):
        rows = self.rows('postgres',
                         "SELECT datname, datallowconn FROM pg_database WHERE datname <> 'template0' ORDER BY datname")
        disabled = [row['datname'] for row in rows if not row['datallowconn']]
        if disabled:
            raise ValueError(f"Cannot inspect databases with connections disabled: {disabled}")
        return [row['datname'] for row in rows]

    def extensions(self, database):
        return {row['extname']: row['extversion'] for row in self.rows(
            database, 'SELECT extname, extversion FROM pg_extension ORDER BY extname')}

    def extension_layout(self, database):
        return {row['name']: row for row in self.rows(database,
            "SELECT e.extname AS name, n.nspname AS schema, "
            "ARRAY(SELECT required.extname FROM pg_depend d "
            "JOIN pg_extension required ON required.oid=d.refobjid "
            "WHERE d.classid='pg_extension'::regclass AND d.objid=e.oid "
            "AND d.refclassid='pg_extension'::regclass AND d.deptype='n') AS dependencies "
            "FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace")}

    def available(self):
        rows = self.rows('postgres',
                         'SELECT name, version FROM pg_available_extension_versions')
        versions = {}
        for row in rows:
            versions.setdefault(row['name'], set()).add(row['version'])
        defaults = {row['name']: row['default_version'] for row in self.rows(
            'postgres', 'SELECT name, default_version FROM pg_available_extensions')}
        return versions, defaults

    def paths(self, name):
        return {(row['source'], row['target']) for row in self.rows(
            'postgres', f"SELECT source, target FROM pg_extension_update_paths({literal(name)}) WHERE path IS NOT NULL")}

    def update(self, database, name, desired):
        # First statement in a fresh connection: Timescale must not already be
        # loaded by an earlier query in this session.
        self.sql(database, f"ALTER EXTENSION {identifier(name)} UPDATE TO {literal(desired)};")


def plan_extensions(source, target, databases):
    """Preflight every database before changing any extension."""
    source_versions, _ = source.available()
    target_versions, defaults = target.available()
    paths_source = {name: source.paths(name) for name in MANAGED if name in source_versions}
    paths_target = {name: target.paths(name) for name in MANAGED if name in target_versions}
    plans = {}
    for database in databases:
        plans[database] = {}
        for name, installed in source.extensions(database).items():
            if name not in MANAGED:
                if installed not in target_versions.get(name, set()):
                    raise ValueError(f"{database}: target image lacks {name} {installed}.")
                plans[database][name] = installed
                continue
            desired = defaults.get(name)
            if desired is None or version(installed) > version(desired):
                raise ValueError(f"{database}: cannot downgrade {name} {installed} to target default {desired}.")
            candidates = source_versions.get(name, set()) & target_versions.get(name, set())
            compatible = [candidate for candidate in candidates
                          if re.fullmatch(r'\d+\.\d+\.\d+', candidate)
                          and version(installed) <= version(candidate) <= version(desired)
                          and (candidate == installed or (installed, candidate) in paths_source.get(name, set()))
                          and (candidate == desired or (candidate, desired) in paths_target.get(name, set()))]
            if not compatible:
                raise ValueError(f"{database}: no supported {name} bridge from {installed} to {desired}; deploy an intermediate image.")
            plans[database][name] = max(compatible, key=version)
    return plans


def upgrade(cluster):
    databases = cluster.databases()
    plan = plan_extensions(cluster, cluster, databases)
    changed = False
    for database, extensions in plan.items():
        installed = cluster.extensions(database)
        for name in sorted(MANAGED):
            if name in extensions and installed[name] != extensions[name]:
                cluster.update(database, name, extensions[name])
                changed = True
        if cluster.extensions(database) != extensions:
            raise RuntimeError(f"{database}: extension validation failed after upgrade.")
    return dict(changed=changed, databases=databases, extensions=plan)


def migrate(source, target, temporary_directory):
    if source.container == target.container:
        raise ValueError('Source and target containers must differ.')
    databases = source.databases()
    plan = plan_extensions(source, target, databases)
    layouts = {database: source.extension_layout(database) for database in databases}
    for cluster in (source, target):
        tablespaces = cluster.rows('postgres',
                                   "SELECT spcname FROM pg_tablespace WHERE spcname NOT IN ('pg_default', 'pg_global')")
        if tablespaces:
            raise ValueError('Automatic migration does not support external tablespaces.')
    # Target must be the newly initialized staging cluster. Never overwrite an
    # existing application database when this module is called independently.
    if set(target.databases()) != {'postgres', 'template1'}:
        raise ValueError('Migration target must contain only the freshly initialized postgres and template databases.')
    for database in target.databases():
        objects = target.rows(database,
                              "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                              "WHERE n.nspname='public' AND c.relkind IN ('r','p','m','v','S','f') "
                              "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass "
                              "AND d.objid=c.oid AND d.deptype='e')")
        if objects:
            raise ValueError(f'Target database {database} is not empty.')
    for database, extensions in plan.items():
        installed = source.extensions(database)
        for name in sorted(MANAGED):
            if name in extensions and installed[name] != extensions[name]:
                source.update(database, name, extensions[name])

    maintenance = 'saltbox_migration_' + uuid.uuid4().hex
    target.sql('postgres', f'CREATE DATABASE {identifier(maintenance)} TEMPLATE template0;')
    with tempfile.TemporaryDirectory(prefix='saltbox-timescaledb-', dir=temporary_directory) as scratch:
        globals_path = os.path.join(scratch, 'globals.sql')
        with open(globals_path, 'wb') as stream:
            source.command(['pg_dumpall', '--globals-only', '--quote-all-identifiers',
                            '--username', source.username], stdout=stream)
        # The initialized target already owns this superuser. Keep ALTER ROLE
        # (including its password) and every other role/privilege statement.
        # --quote-all-identifiers quotes even ordinary identifiers.
        bootstrap = ('CREATE ROLE ' + identifier(source.username) + ';').encode()
        with open(globals_path, 'rb') as src, open(globals_path + '.filtered', 'wb') as dst:
            for line in src:
                if line.rstrip(b'\r\n') != bootstrap:
                    dst.write(line)
        with open(globals_path + '.filtered', 'rb') as stream:
            target.command(['psql', '-X', '--set=ON_ERROR_STOP=1', '--username', target.username,
                            '--dbname', maintenance], stdin=stream)
        for database in databases:
            archive = os.path.join(scratch, 'database.dump')
            connection = "dbname='" + database.replace('\\', '\\\\').replace("'", "\\'") + "'"
            with open(archive, 'wb') as stream:
                source.command(['pg_dump', '--format=custom', '--create', '--quote-all-identifiers',
                                '--username', source.username, '--dbname', connection], stdout=stream)
            with open(archive, 'rb') as stream:
                toc = target.command(['pg_restore', '--list', '--create'], stdin=stream)
            create_entries, metadata_entries, schema_entries, content_entries = [], [], [], []
            for line in toc.splitlines():
                if re.match(r'^\d+; \d+ \d+ DATABASE ', line) and ' DATABASE PROPERTIES ' not in line:
                    create_entries.append(line)
                elif re.match(r'^\d+; \d+ \d+ (DATABASE PROPERTIES |(?:ACL|COMMENT|SECURITY LABEL) - DATABASE )', line):
                    metadata_entries.append(line)
                if re.match(r'^\d+; \d+ \d+ SCHEMA ', line):
                    schema_entries.append(line)
                else:
                    content_entries.append(line)
            if len(create_entries) != 1:
                raise RuntimeError(f'{database}: expected one database creation entry in pg_dump archive.')
            if database in ('postgres', 'template1'):
                # These contain only image initialization objects. Drop whole
                # databases so no target-default extension schemas leak in.
                if database == 'template1':
                    target.sql(maintenance, 'ALTER DATABASE template1 IS_TEMPLATE false;')
                target.sql(maintenance, f'DROP DATABASE {identifier(database)} WITH (FORCE);')
            list_path = '/tmp/' + maintenance + '.list'

            def restore_entries(entries, create=False, destination=maintenance):
                target.command(['sh', '-c', 'cat > "$1"', 'sh', list_path],
                               ('\n'.join(entries) + '\n').encode())
                with open(archive, 'rb') as stream:
                    target.command(['pg_restore', '--exit-on-error', '--use-list', list_path,
                                    '--username', target.username, '--dbname', destination]
                                   + (['--create'] if create else []), stdin=stream)

            # pg_restore always restores DATABASE and DATABASE PROPERTIES with
            # --create, even when omitted from --use-list. Include both here to
            # preserve their order, and restore them exactly once.
            restore_entries(create_entries + metadata_entries, create=True)
            if not target.rows(maintenance, f'SELECT datname FROM pg_database WHERE datname={literal(database)}'):
                raise RuntimeError(f'{database}: creation did not create database; entries: {create_entries}')
            restore_entries(schema_entries, destination=connection)
            pending = dict(layouts[database])
            created = {'plpgsql'}
            pending.pop('plpgsql', None)
            while pending:
                ready = [name for name, layout in pending.items() if set(layout['dependencies']) <= created]
                if not ready:
                    raise ValueError(f'{database}: unresolved extension dependencies: {list(pending)}')
                for name in ready:
                    installed = plan[database][name]
                    schema = pending[name]['schema']
                    target.sql(database, f'CREATE EXTENSION {identifier(name)} SCHEMA {identifier(schema)} VERSION {literal(installed)};')
                    created.add(name)
                    del pending[name]
            if 'timescaledb' in plan[database]:
                target.sql(database, 'SELECT timescaledb_pre_restore();')
            # No parallel restore: Timescale's catalogs require serial restore.
            restore_entries(content_entries, destination=connection)
            if 'timescaledb' in plan[database]:
                target.sql(database, 'SELECT timescaledb_post_restore();')
            target.command(['rm', '--', list_path])
            if target.extensions(database) != plan[database]:
                raise RuntimeError(f'{database}: restored extension versions differ from the source.')
            if target.extension_layout(database) != layouts[database]:
                raise RuntimeError(f'{database}: restored extension schemas or dependencies differ from the source.')
            target.sql(database, 'ANALYZE;')
        target.sql('postgres', f'DROP DATABASE {identifier(maintenance)};')

    if set(source.databases()) != set(target.databases()):
        raise RuntimeError('Migrated database names differ from the source.')
    roles_query = 'SELECT rolname FROM pg_roles ORDER BY rolname'
    if source.rows('postgres', roles_query) != target.rows('postgres', roles_query):
        # New PostgreSQL versions can add built-in roles.
        old = {row['rolname'] for row in source.rows('postgres', roles_query)}
        new = {row['rolname'] for row in target.rows('postgres', roles_query)}
        if not old <= new:
            raise RuntimeError('Migrated cluster is missing source roles.')
    result = upgrade(target)
    result['changed'] = True
    return result


def main():
    module = AnsibleModule(
        argument_spec=dict(
            action=dict(type='str', required=True, choices=['upgrade', 'migrate']),
            source=dict(type='str', required=True), target=dict(type='str'),
            username=dict(type='str', required=True), temporary_directory=dict(type='path'),
        ),
        required_if=[('action', 'migrate', ['target'])],
        supports_check_mode=False,
    )
    try:
        source = Cluster(module.params['source'], module.params['username'])
        if module.params['action'] == 'upgrade':
            result = upgrade(source)
        else:
            target = Cluster(module.params['target'], module.params['username'])
            result = migrate(source, target, module.params['temporary_directory'])
        module.exit_json(**result)
    except (OSError, RuntimeError, ValueError) as error:
        module.fail_json(msg=str(error))


if __name__ == '__main__':
    main()
