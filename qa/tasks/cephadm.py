"""
Ceph cluster task, deployed via cephadm orchestrator
"""
import argparse
import configobj
import contextlib
import logging
import os
import json
import re
import uuid
import yaml

from io import BytesIO, StringIO
from tarfile import ReadError
from tasks.ceph_manager import CephManager
from teuthology import misc as teuthology
from teuthology import contextutil
from teuthology.orchestra import run
from teuthology.orchestra.daemon import DaemonGroup
from teuthology.config import config as teuth_config

# these items we use from ceph.py should probably eventually move elsewhere
from tasks.ceph import get_mons, healthy

CEPH_ROLE_TYPES = ['mon', 'mgr', 'osd', 'mds', 'rgw', 'prometheus']

log = logging.getLogger(__name__)


def _shell(ctx, cluster_name, remote, args, extra_cephadm_args=[], **kwargs):
    teuthology.get_testdir(ctx)
    return remote.run(
        args=[
            'sudo',
            ctx.cephadm,
            '--image', ctx.ceph[cluster_name].image,
            'shell',
            '-c', '/etc/ceph/{}.conf'.format(cluster_name),
            '-k', '/etc/ceph/{}.client.admin.keyring'.format(cluster_name),
            '--fsid', ctx.ceph[cluster_name].fsid,
            ] + extra_cephadm_args + [
            '--',
            ] + args,
        **kwargs
    )

def build_initial_config(ctx, config):
    cluster_name = config['cluster']

    path = os.path.join(os.path.dirname(__file__), 'cephadm.conf')
    conf = configobj.ConfigObj(path, file_error=True)

    conf.setdefault('global', {})
    conf['global']['fsid'] = ctx.ceph[cluster_name].fsid

    # overrides
    for section, keys in config.get('conf',{}).items():
        for key, value in keys.items():
            log.info(" override: [%s] %s = %s" % (section, key, value))
            if section not in conf:
                conf[section] = {}
            conf[section][key] = value

    return conf

def update_archive_setting(ctx, key, value):
    """
    Add logs directory to job's info log file
    """
    with open(os.path.join(ctx.archive, 'info.yaml'), 'r+') as info_file:
        info_yaml = yaml.safe_load(info_file)
        info_file.seek(0)
        if 'archive' in info_yaml:
            info_yaml['archive'][key] = value
        else:
            info_yaml['archive'] = {key: value}
        yaml.safe_dump(info_yaml, info_file, default_flow_style=False)

@contextlib.contextmanager
def normalize_hostnames(ctx):
    """
    Ensure we have short hostnames throughout, for consistency between
    remote.shortname and socket.gethostname() in cephadm.
    """
    log.info('Normalizing hostnames...')
    ctx.cluster.run(args=[
        'sudo',
        'hostname',
        run.Raw('$(hostname -s)'),
    ])

    try:
        yield
    finally:
        pass

@contextlib.contextmanager
def download_cephadm(ctx, config, ref):
    cluster_name = config['cluster']

    if config.get('cephadm_mode') != 'cephadm-package':
        ref = config.get('cephadm_branch', ref)
        git_url = config.get('cephadm_git_url', teuth_config.get_ceph_git_url())
        log.info('Downloading cephadm (repo %s ref %s)...' % (git_url, ref))
        if git_url.startswith('https://github.com/'):
            # git archive doesn't like https:// URLs, which we use with github.
            rest = git_url.split('https://github.com/', 1)[1]
            rest = re.sub(r'\.git/?$', '', rest).strip() # no .git suffix
            ctx.cluster.run(
                args=[
                    'curl', '--silent',
                    'https://raw.githubusercontent.com/' + rest + '/' + ref + '/src/cephadm/cephadm',
                    run.Raw('>'),
                    ctx.cephadm,
                    run.Raw('&&'),
                    'ls', '-l',
                    ctx.cephadm,
                ],
            )
        else:
            ctx.cluster.run(
                args=[
                    'git', 'archive',
                    '--remote=' + git_url,
                    ref,
                    'src/cephadm/cephadm',
                    run.Raw('|'),
                    'tar', '-xO', 'src/cephadm/cephadm',
                    run.Raw('>'),
                    ctx.cephadm,
                ],
            )
        # sanity-check the resulting file and set executable bit
        cephadm_file_size = '$(stat -c%s {})'.format(ctx.cephadm)
        ctx.cluster.run(
            args=[
                'test', '-s', ctx.cephadm,
                run.Raw('&&'),
                'test', run.Raw(cephadm_file_size), "-gt", run.Raw('1000'),
                run.Raw('&&'),
                'chmod', '+x', ctx.cephadm,
            ],
        )

    try:
        yield
    finally:
        log.info('Removing cluster...')
        ctx.cluster.run(args=[
            'sudo',
            ctx.cephadm,
            'rm-cluster',
            '--fsid', ctx.ceph[cluster_name].fsid,
            '--force',
        ])

        if config.get('cephadm_mode') == 'root':
            log.info('Removing cephadm ...')
            ctx.cluster.run(
                args=[
                    'rm',
                    '-rf',
                    ctx.cephadm,
                ],
            )

@contextlib.contextmanager
def ceph_log(ctx, config):
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    update_archive_setting(ctx, 'log', '/var/log/ceph')


    try:
        yield

    except Exception:
        # we need to know this below
        ctx.summary['success'] = False
        raise

    finally:
        log.info('Checking cluster log for badness...')
        def first_in_ceph_log(pattern, excludes):
            """
            Find the first occurrence of the pattern specified in the Ceph log,
            Returns None if none found.

            :param pattern: Pattern scanned for.
            :param excludes: Patterns to ignore.
            :return: First line of text (or None if not found)
            """
            args = [
                'sudo',
                'egrep', pattern,
                '/var/log/ceph/{fsid}/ceph.log'.format(
                    fsid=fsid),
            ]
            if excludes:
                for exclude in excludes:
                    args.extend([run.Raw('|'), 'egrep', '-v', exclude])
            args.extend([
                run.Raw('|'), 'head', '-n', '1',
            ])
            r = ctx.ceph[cluster_name].bootstrap_remote.run(
                stdout=StringIO(),
                args=args,
            )
            stdout = r.stdout.getvalue()
            if stdout != '':
                return stdout
            return None

        if first_in_ceph_log('\[ERR\]|\[WRN\]|\[SEC\]',
                             config.get('log-ignorelist')) is not None:
            log.warning('Found errors (ERR|WRN|SEC) in cluster log')
            ctx.summary['success'] = False
            # use the most severe problem as the failure reason
            if 'failure_reason' not in ctx.summary:
                for pattern in ['\[SEC\]', '\[ERR\]', '\[WRN\]']:
                    match = first_in_ceph_log(pattern, config['log-ignorelist'])
                    if match is not None:
                        ctx.summary['failure_reason'] = \
                            '"{match}" in cluster log'.format(
                                match=match.rstrip('\n'),
                            )
                        break

        if ctx.archive is not None and \
                not (ctx.config.get('archive-on-error') and ctx.summary['success']):
            # and logs
            log.info('Compressing logs...')
            run.wait(
                ctx.cluster.run(
                    args=[
                        'sudo',
                        'find',
                        '/var/log/ceph',   # all logs, not just for the cluster
                        '/var/log/rbd-target-api', # ceph-iscsi
                        '-name',
                        '*.log',
                        '-print0',
                        run.Raw('|'),
                        'sudo',
                        'xargs',
                        '-0',
                        '--no-run-if-empty',
                        '--',
                        'gzip',
                        '--',
                    ],
                    wait=False,
                ),
            )

            log.info('Archiving logs...')
            path = os.path.join(ctx.archive, 'remote')
            try:
                os.makedirs(path)
            except OSError:
                pass
            for remote in ctx.cluster.remotes.keys():
                sub = os.path.join(path, remote.name)
                try:
                    os.makedirs(sub)
                except OSError:
                    pass
                try:
                    teuthology.pull_directory(remote, '/var/log/ceph',  # everything
                                              os.path.join(sub, 'log'))
                except ReadError:
                    pass

@contextlib.contextmanager
def ceph_crash(ctx, config):
    """
    Gather crash dumps from /var/lib/ceph/$fsid/crash
    """
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    update_archive_setting(ctx, 'crash', '/var/lib/ceph/crash')

    try:
        yield

    finally:
        if ctx.archive is not None:
            log.info('Archiving crash dumps...')
            path = os.path.join(ctx.archive, 'remote')
            try:
                os.makedirs(path)
            except OSError:
                pass
            for remote in ctx.cluster.remotes.keys():
                sub = os.path.join(path, remote.name)
                try:
                    os.makedirs(sub)
                except OSError:
                    pass
                try:
                    teuthology.pull_directory(remote,
                                              '/var/lib/ceph/%s/crash' % fsid,
                                              os.path.join(sub, 'crash'))
                except ReadError:
                    pass

@contextlib.contextmanager
def ceph_bootstrap(ctx, config):
    """
    Bootstrap ceph cluster.

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """
    cluster_name = config['cluster']
    testdir = teuthology.get_testdir(ctx)
    fsid = ctx.ceph[cluster_name].fsid

    bootstrap_remote = ctx.ceph[cluster_name].bootstrap_remote
    first_mon = ctx.ceph[cluster_name].first_mon
    first_mon_role = ctx.ceph[cluster_name].first_mon_role
    mons = ctx.ceph[cluster_name].mons

    ctx.cluster.run(args=[
        'sudo', 'mkdir', '-p', '/etc/ceph',
        ]);
    ctx.cluster.run(args=[
        'sudo', 'chmod', '777', '/etc/ceph',
        ]);
    try:
        # write seed config
        log.info('Writing seed config...')
        conf_fp = BytesIO()
        seed_config = build_initial_config(ctx, config)
        seed_config.write(conf_fp)
        bootstrap_remote.write_file(
            path='{}/seed.{}.conf'.format(testdir, cluster_name),
            data=conf_fp.getvalue())
        log.debug('Final config:\n' + conf_fp.getvalue().decode())
        ctx.ceph[cluster_name].conf = seed_config

        # register initial daemons
        ctx.daemons.register_daemon(
            bootstrap_remote, 'mon', first_mon,
            cluster=cluster_name,
            fsid=fsid,
            logger=log.getChild('mon.' + first_mon),
            wait=False,
            started=True,
        )
        if not ctx.ceph[cluster_name].roleless:
            first_mgr = ctx.ceph[cluster_name].first_mgr
            ctx.daemons.register_daemon(
                bootstrap_remote, 'mgr', first_mgr,
                cluster=cluster_name,
                fsid=fsid,
                logger=log.getChild('mgr.' + first_mgr),
                wait=False,
                started=True,
            )

        # bootstrap
        log.info('Bootstrapping...')
        cmd = [
            'sudo',
            ctx.cephadm,
            '--image', ctx.ceph[cluster_name].image,
            '-v',
            'bootstrap',
            '--fsid', fsid,
            '--config', '{}/seed.{}.conf'.format(testdir, cluster_name),
            '--output-config', '/etc/ceph/{}.conf'.format(cluster_name),
            '--output-keyring',
            '/etc/ceph/{}.client.admin.keyring'.format(cluster_name),
            '--output-pub-ssh-key', '{}/{}.pub'.format(testdir, cluster_name),
        ]
        if not ctx.ceph[cluster_name].roleless:
            cmd += [
                '--mon-id', first_mon,
                '--mgr-id', first_mgr,
                '--orphan-initial-daemons',   # we will do it explicitly!
                '--skip-monitoring-stack',    # we'll provision these explicitly
            ]
        if mons[first_mon_role].startswith('['):
            cmd += ['--mon-addrv', mons[first_mon_role]]
        else:
            cmd += ['--mon-ip', mons[first_mon_role]]
        if config.get('skip_dashboard'):
            cmd += ['--skip-dashboard']
        # bootstrap makes the keyring root 0600, so +r it for our purposes
        cmd += [
            run.Raw('&&'),
            'sudo', 'chmod', '+r',
            '/etc/ceph/{}.client.admin.keyring'.format(cluster_name),
        ]
        bootstrap_remote.run(args=cmd)

        # fetch keys and configs
        log.info('Fetching config...')
        ctx.ceph[cluster_name].config_file = \
            bootstrap_remote.read_file(f'/etc/ceph/{cluster_name}.conf')
        log.info('Fetching client.admin keyring...')
        ctx.ceph[cluster_name].admin_keyring = \
            bootstrap_remote.read_file(f'/etc/ceph/{cluster_name}.client.admin.keyring')
        log.info('Fetching mon keyring...')
        ctx.ceph[cluster_name].mon_keyring = \
            bootstrap_remote.read_file(f'/var/lib/ceph/{fsid}/mon.{first_mon}/keyring', sudo=True)

        # fetch ssh key, distribute to additional nodes
        log.info('Fetching pub ssh key...')
        ssh_pub_key = bootstrap_remote.read_file(
            f'{testdir}/{cluster_name}.pub').decode('ascii').strip()

        log.info('Installing pub ssh key for root users...')
        ctx.cluster.run(args=[
            'sudo', 'install', '-d', '-m', '0700', '/root/.ssh',
            run.Raw('&&'),
            'echo', ssh_pub_key,
            run.Raw('|'),
            'sudo', 'tee', '-a', '/root/.ssh/authorized_keys',
            run.Raw('&&'),
            'sudo', 'chmod', '0600', '/root/.ssh/authorized_keys',
        ])

        # set options
        if config.get('allow_ptrace', True):
            _shell(ctx, cluster_name, bootstrap_remote,
                   ['ceph', 'config', 'set', 'mgr', 'mgr/cephadm/allow_ptrace', 'true'])

        # add other hosts
        for remote in ctx.cluster.remotes.keys():
            if remote == bootstrap_remote:
                continue
            log.info('Writing (initial) conf and keyring to %s' % remote.shortname)
            remote.write_file(
                path='/etc/ceph/{}.conf'.format(cluster_name),
                data=ctx.ceph[cluster_name].config_file)
            remote.write_file(
                path='/etc/ceph/{}.client.admin.keyring'.format(cluster_name),
                data=ctx.ceph[cluster_name].admin_keyring)

            log.info('Adding host %s to orchestrator...' % remote.shortname)
            _shell(ctx, cluster_name, remote, [
                'ceph', 'orch', 'host', 'add',
                remote.shortname
            ])
            r = _shell(ctx, cluster_name, remote,
                       ['ceph', 'orch', 'host', 'ls', '--format=json'],
                       stdout=StringIO())
            hosts = [node['hostname'] for node in json.loads(r.stdout.getvalue())]
            assert remote.shortname in hosts

        yield

    finally:
        log.info('Cleaning up testdir ceph.* files...')
        ctx.cluster.run(args=[
            'rm', '-f',
            '{}/seed.{}.conf'.format(testdir, cluster_name),
            '{}/{}.pub'.format(testdir, cluster_name),
        ])

        log.info('Stopping all daemons...')

        # this doesn't block until they are all stopped...
        #ctx.cluster.run(args=['sudo', 'systemctl', 'stop', 'ceph.target'])

        # so, stop them individually
        for role in ctx.daemons.resolve_role_list(None, CEPH_ROLE_TYPES, True):
            cluster, type_, id_ = teuthology.split_role(role)
            try:
                ctx.daemons.get_daemon(type_, id_, cluster).stop()
            except Exception:
                log.exception(f'Failed to stop "{role}"')
                raise

        # clean up /etc/ceph
        ctx.cluster.run(args=[
            'sudo', 'rm', '-f',
            '/etc/ceph/{}.conf'.format(cluster_name),
            '/etc/ceph/{}.client.admin.keyring'.format(cluster_name),
        ])

@contextlib.contextmanager
def ceph_mons(ctx, config):
    """
    Deploy any additional mons
    """
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    try:
        daemons = {}
        if config.get('add_mons_via_daemon_add'):
            # This is the old way of adding mons that works with the (early) octopus
            # cephadm scheduler.
            num_mons = 1
            for remote, roles in ctx.cluster.remotes.items():
                for mon in [r for r in roles
                            if teuthology.is_type('mon', cluster_name)(r)]:
                    c_, _, id_ = teuthology.split_role(mon)
                    if c_ == cluster_name and id_ == ctx.ceph[cluster_name].first_mon:
                        continue
                    log.info('Adding %s on %s' % (mon, remote.shortname))
                    num_mons += 1
                    _shell(ctx, cluster_name, remote, [
                        'ceph', 'orch', 'daemon', 'add', 'mon',
                        remote.shortname + ':' + ctx.ceph[cluster_name].mons[mon] + '=' + id_,
                    ])
                    ctx.daemons.register_daemon(
                        remote, 'mon', id_,
                        cluster=cluster_name,
                        fsid=fsid,
                        logger=log.getChild(mon),
                        wait=False,
                        started=True,
                    )
                    daemons[mon] = (remote, id_)

                    with contextutil.safe_while(sleep=1, tries=180) as proceed:
                        while proceed():
                            log.info('Waiting for %d mons in monmap...' % (num_mons))
                            r = _shell(
                                ctx=ctx,
                                cluster_name=cluster_name,
                                remote=remote,
                                args=[
                                    'ceph', 'mon', 'dump', '-f', 'json',
                                ],
                                stdout=StringIO(),
                            )
                            j = json.loads(r.stdout.getvalue())
                            if len(j['mons']) == num_mons:
                                break
        else:
            nodes = []
            for remote, roles in ctx.cluster.remotes.items():
                for mon in [r for r in roles
                            if teuthology.is_type('mon', cluster_name)(r)]:
                    c_, _, id_ = teuthology.split_role(mon)
                    log.info('Adding %s on %s' % (mon, remote.shortname))
                    nodes.append(remote.shortname
                                 + ':' + ctx.ceph[cluster_name].mons[mon]
                                 + '=' + id_)
                    if c_ == cluster_name and id_ == ctx.ceph[cluster_name].first_mon:
                        continue
                    daemons[mon] = (remote, id_)

            _shell(ctx, cluster_name, remote, [
                'ceph', 'orch', 'apply', 'mon',
                str(len(nodes)) + ';' + ';'.join(nodes)]
                   )
            for mgr, i in daemons.items():
                remote, id_ = i
                ctx.daemons.register_daemon(
                    remote, 'mon', id_,
                    cluster=cluster_name,
                    fsid=fsid,
                    logger=log.getChild(mon),
                    wait=False,
                    started=True,
                )

            with contextutil.safe_while(sleep=1, tries=180) as proceed:
                while proceed():
                    log.info('Waiting for %d mons in monmap...' % (len(nodes)))
                    r = _shell(
                        ctx=ctx,
                        cluster_name=cluster_name,
                        remote=remote,
                        args=[
                            'ceph', 'mon', 'dump', '-f', 'json',
                        ],
                        stdout=StringIO(),
                    )
                    j = json.loads(r.stdout.getvalue())
                    if len(j['mons']) == len(nodes):
                        break

        # refresh our (final) ceph.conf file
        bootstrap_remote = ctx.ceph[cluster_name].bootstrap_remote
        log.info('Generating final ceph.conf file...')
        r = _shell(
            ctx=ctx,
            cluster_name=cluster_name,
            remote=bootstrap_remote,
            args=[
                'ceph', 'config', 'generate-minimal-conf',
            ],
            stdout=StringIO(),
        )
        ctx.ceph[cluster_name].config_file = r.stdout.getvalue()

        yield

    finally:
        pass

@contextlib.contextmanager
def ceph_mgrs(ctx, config):
    """
    Deploy any additional mgrs
    """
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    try:
        nodes = []
        daemons = {}
        for remote, roles in ctx.cluster.remotes.items():
            for mgr in [r for r in roles
                        if teuthology.is_type('mgr', cluster_name)(r)]:
                c_, _, id_ = teuthology.split_role(mgr)
                log.info('Adding %s on %s' % (mgr, remote.shortname))
                nodes.append(remote.shortname + '=' + id_)
                if c_ == cluster_name and id_ == ctx.ceph[cluster_name].first_mgr:
                    continue
                daemons[mgr] = (remote, id_)
        if nodes:
            _shell(ctx, cluster_name, remote, [
                'ceph', 'orch', 'apply', 'mgr',
                str(len(nodes)) + ';' + ';'.join(nodes)]
            )
        for mgr, i in daemons.items():
            remote, id_ = i
            ctx.daemons.register_daemon(
                remote, 'mgr', id_,
                cluster=cluster_name,
                fsid=fsid,
                logger=log.getChild(mgr),
                wait=False,
                started=True,
            )

        yield

    finally:
        pass

@contextlib.contextmanager
def ceph_osds(ctx, config):
    """
    Deploy OSDs
    """
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    try:
        log.info('Deploying OSDs...')

        # provision OSDs in numeric order
        id_to_remote = {}
        devs_by_remote = {}
        for remote, roles in ctx.cluster.remotes.items():
            devs_by_remote[remote] = teuthology.get_scratch_devices(remote)
            for osd in [r for r in roles
                        if teuthology.is_type('osd', cluster_name)(r)]:
                _, _, id_ = teuthology.split_role(osd)
                id_to_remote[int(id_)] = (osd, remote)

        cur = 0
        for osd_id in sorted(id_to_remote.keys()):
            osd, remote = id_to_remote[osd_id]
            _, _, id_ = teuthology.split_role(osd)
            assert int(id_) == cur
            devs = devs_by_remote[remote]
            assert devs   ## FIXME ##
            dev = devs.pop()
            if all(_ in dev for _ in ('lv', 'vg')):
                short_dev = dev.replace('/dev/', '')
            else:
                short_dev = dev
            log.info('Deploying %s on %s with %s...' % (
                osd, remote.shortname, dev))
            _shell(ctx, cluster_name, remote, [
                'ceph-volume', 'lvm', 'zap', dev])
            _shell(ctx, cluster_name, remote, [
                'ceph', 'orch', 'daemon', 'add', 'osd',
                remote.shortname + ':' + short_dev
            ])
            ctx.daemons.register_daemon(
                remote, 'osd', id_,
                cluster=cluster_name,
                fsid=fsid,
                logger=log.getChild(osd),
                wait=False,
                started=True,
            )
            cur += 1

        yield
    finally:
        pass

@contextlib.contextmanager
def ceph_mdss(ctx, config):
    """
    Deploy MDSss
    """
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    nodes = []
    daemons = {}
    for remote, roles in ctx.cluster.remotes.items():
        for role in [r for r in roles
                    if teuthology.is_type('mds', cluster_name)(r)]:
            c_, _, id_ = teuthology.split_role(role)
            log.info('Adding %s on %s' % (role, remote.shortname))
            nodes.append(remote.shortname + '=' + id_)
            daemons[role] = (remote, id_)
    if nodes:
        _shell(ctx, cluster_name, remote, [
            'ceph', 'orch', 'apply', 'mds',
            'all',
            str(len(nodes)) + ';' + ';'.join(nodes)]
        )
    for role, i in daemons.items():
        remote, id_ = i
        ctx.daemons.register_daemon(
            remote, 'mds', id_,
            cluster=cluster_name,
            fsid=fsid,
            logger=log.getChild(role),
            wait=False,
            started=True,
        )

    yield

@contextlib.contextmanager
def ceph_monitoring(daemon_type, ctx, config):
    """
    Deploy prometheus, node-exporter, etc.
    """
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    nodes = []
    daemons = {}
    for remote, roles in ctx.cluster.remotes.items():
        for role in [r for r in roles
                    if teuthology.is_type(daemon_type, cluster_name)(r)]:
            c_, _, id_ = teuthology.split_role(role)
            log.info('Adding %s on %s' % (role, remote.shortname))
            nodes.append(remote.shortname + '=' + id_)
            daemons[role] = (remote, id_)
    if nodes:
        _shell(ctx, cluster_name, remote, [
            'ceph', 'orch', 'apply', daemon_type,
            str(len(nodes)) + ';' + ';'.join(nodes)]
        )
    for role, i in daemons.items():
        remote, id_ = i
        ctx.daemons.register_daemon(
            remote, daemon_type, id_,
            cluster=cluster_name,
            fsid=fsid,
            logger=log.getChild(role),
            wait=False,
            started=True,
        )

    yield

@contextlib.contextmanager
def ceph_rgw(ctx, config):
    """
    Deploy rgw
    """
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    nodes = {}
    daemons = {}
    for remote, roles in ctx.cluster.remotes.items():
        for role in [r for r in roles
                    if teuthology.is_type('rgw', cluster_name)(r)]:
            c_, _, id_ = teuthology.split_role(role)
            log.info('Adding %s on %s' % (role, remote.shortname))
            svc = '.'.join(id_.split('.')[0:2])
            if svc not in nodes:
                nodes[svc] = []
            nodes[svc].append(remote.shortname + '=' + id_)
            daemons[role] = (remote, id_)

    for svc, nodes in nodes.items():
        _shell(ctx, cluster_name, remote, [
            'ceph', 'orch', 'apply', 'rgw', svc,
             '--placement',
             str(len(nodes)) + ';' + ';'.join(nodes)]
        )
    for role, i in daemons.items():
        remote, id_ = i
        ctx.daemons.register_daemon(
            remote, 'rgw', id_,
            cluster=cluster_name,
            fsid=fsid,
            logger=log.getChild(role),
            wait=False,
            started=True,
        )

    yield


@contextlib.contextmanager
def ceph_iscsi(ctx, config):
    """
    Deploy iSCSIs
    """
    cluster_name = config['cluster']
    fsid = ctx.ceph[cluster_name].fsid

    nodes = []
    daemons = {}
    for remote, roles in ctx.cluster.remotes.items():
        for role in [r for r in roles
                    if teuthology.is_type('iscsi', cluster_name)(r)]:
            c_, _, id_ = teuthology.split_role(role)
            log.info('Adding %s on %s' % (role, remote.shortname))
            nodes.append(remote.shortname + '=' + id_)
            daemons[role] = (remote, id_)
    if nodes:
        poolname = 'iscsi'
        # ceph osd pool create iscsi 3 3 replicated
        _shell(ctx, cluster_name, remote, [
            'ceph', 'osd', 'pool', 'create',
            poolname, '3', '3', 'replicated']
        )

        _shell(ctx, cluster_name, remote, [
            'ceph', 'osd', 'pool', 'application', 'enable',
            poolname, 'rbd']
        )

        # ceph orch apply iscsi iscsi user password
        _shell(ctx, cluster_name, remote, [
            'ceph', 'orch', 'apply', 'iscsi',
            poolname, 'user', 'password',
            '--placement', str(len(nodes)) + ';' + ';'.join(nodes)]
        )
    for role, i in daemons.items():
        remote, id_ = i
        ctx.daemons.register_daemon(
            remote, 'iscsi', id_,
            cluster=cluster_name,
            fsid=fsid,
            logger=log.getChild(role),
            wait=False,
            started=True,
        )

    yield

@contextlib.contextmanager
def ceph_clients(ctx, config):
    cluster_name = config['cluster']

    log.info('Setting up client nodes...')
    clients = ctx.cluster.only(teuthology.is_type('client', cluster_name))
    for remote, roles_for_host in clients.remotes.items():
        for role in teuthology.cluster_roles_of_type(roles_for_host, 'client',
                                                     cluster_name):
            name = teuthology.ceph_role(role)
            client_keyring = '/etc/ceph/{0}.{1}.keyring'.format(cluster_name,
                                                                name)
            r = _shell(
                ctx=ctx,
                cluster_name=cluster_name,
                remote=remote,
                args=[
                    'ceph', 'auth',
                    'get-or-create', name,
                    'mon', 'allow *',
                    'osd', 'allow *',
                    'mds', 'allow *',
                    'mgr', 'allow *',
                ],
                stdout=StringIO(),
            )
            keyring = r.stdout.getvalue()
            remote.sudo_write_file(client_keyring, keyring, mode='0644')
    yield

@contextlib.contextmanager
def ceph_initial():
    try:
        yield
    finally:
        log.info('Teardown complete')

## public methods
@contextlib.contextmanager
def stop(ctx, config):
    """
    Stop ceph daemons

    For example::
      tasks:
      - ceph.stop: [mds.*]

      tasks:
      - ceph.stop: [osd.0, osd.2]

      tasks:
      - ceph.stop:
          daemons: [osd.0, osd.2]

    """
    if config is None:
        config = {}
    elif isinstance(config, list):
        config = {'daemons': config}

    daemons = ctx.daemons.resolve_role_list(
        config.get('daemons', None), CEPH_ROLE_TYPES, True)
    clusters = set()

    for role in daemons:
        cluster, type_, id_ = teuthology.split_role(role)
        ctx.daemons.get_daemon(type_, id_, cluster).stop()
        clusters.add(cluster)

#    for cluster in clusters:
#        ctx.ceph[cluster].watchdog.stop()
#        ctx.ceph[cluster].watchdog.join()

    yield

def shell(ctx, config):
    """
    Execute (shell) commands
    """
    cluster_name = config.get('cluster', 'ceph')

    env = []
    if 'env' in config:
        for k in config['env']:
            env.extend(['-e', k + '=' + ctx.config.get(k, '')])
        del config['env']

    if 'all' in config and len(config) == 1:
        a = config['all']
        roles = teuthology.all_roles(ctx.cluster)
        config = dict((id_, a) for id_ in roles)

    for role, ls in config.items():
        (remote,) = ctx.cluster.only(role).remotes.keys()
        log.info('Running commands on role %s host %s', role, remote.name)
        for c in ls:
            _shell(ctx, cluster_name, remote,
                   ['bash', '-c', c],
                   extra_cephadm_args=env)

def apply(ctx, config):
    """
    Apply spec
    
      tasks:
        - cephadm.apply:
            specs:
            - service_type: rgw
              service_id: foo
              spec:
                rgw_frontend_port: 8000
            - service_type: rgw
              service_id: bar
              spec:
                rgw_frontend_port: 9000
                zone: bar
                realm: asdf

    """
    cluster_name = config.get('cluster', 'ceph')

    specs = config.get('specs', [])
    y = '\n---\n'.join(map(yaml.dump, specs))

    log.info(f'Applying spec:\n{y}')
    _shell(
        ctx, cluster_name, ctx.ceph[cluster_name].bootstrap_remote,
        ['ceph', 'orch', 'apply', '-i', '-'],
        stdin=y,
    )


@contextlib.contextmanager
def tweaked_option(ctx, config):
    """
    set an option, and then restore it with its original value

    Note, due to the way how tasks are executed/nested, it's not suggested to
    use this method as a standalone task. otherwise, it's likely that it will
    restore the tweaked option at the /end/ of 'tasks' block.
    """
    saved_options = {}
    # we can complicate this when necessary
    options = ['mon-health-to-clog']
    type_, id_ = 'mon', '*'
    cluster = config.get('cluster', 'ceph')
    manager = ctx.managers[cluster]
    if id_ == '*':
        get_from = next(teuthology.all_roles_of_type(ctx.cluster, type_))
    else:
        get_from = id_
    for option in options:
        if option not in config:
            continue
        value = 'true' if config[option] else 'false'
        option = option.replace('-', '_')
        old_value = manager.get_config(type_, get_from, option)
        if value != old_value:
            saved_options[option] = old_value
            manager.inject_args(type_, id_, option, value)
    yield
    for option, value in saved_options.items():
        manager.inject_args(type_, id_, option, value)

@contextlib.contextmanager
def restart(ctx, config):
    """
   restart ceph daemons

   For example::
      tasks:
      - ceph.restart: [all]

   For example::
      tasks:
      - ceph.restart: [osd.0, mon.1, mds.*]

   or::

      tasks:
      - ceph.restart:
          daemons: [osd.0, mon.1]
          wait-for-healthy: false
          wait-for-osds-up: true

    :param ctx: Context
    :param config: Configuration
    """
    if config is None:
        config = {}
    elif isinstance(config, list):
        config = {'daemons': config}

    daemons = ctx.daemons.resolve_role_list(
        config.get('daemons', None), CEPH_ROLE_TYPES, True)
    clusters = set()

    log.info('daemons %s' % daemons)
    with tweaked_option(ctx, config):
        for role in daemons:
            cluster, type_, id_ = teuthology.split_role(role)
            d = ctx.daemons.get_daemon(type_, id_, cluster)
            assert d, 'daemon %s does not exist' % role
            d.stop()
            if type_ == 'osd':
                ctx.managers[cluster].mark_down_osd(id_)
            d.restart()
            clusters.add(cluster)

    if config.get('wait-for-healthy', True):
        for cluster in clusters:
            healthy(ctx=ctx, config=dict(cluster=cluster))
    if config.get('wait-for-osds-up', False):
        for cluster in clusters:
            ctx.managers[cluster].wait_for_all_osds_up()
    yield

@contextlib.contextmanager
def distribute_config_and_admin_keyring(ctx, config):
    """
    Distribute a sufficient config and keyring for clients
    """
    cluster_name = config['cluster']
    log.info('Distributing (final) config and client.admin keyring...')
    for remote, roles in ctx.cluster.remotes.items():
        remote.write_file(
            '/etc/ceph/{}.conf'.format(cluster_name),
            ctx.ceph[cluster_name].config_file,
            sudo=True)
        remote.write_file(
            path='/etc/ceph/{}.client.admin.keyring'.format(cluster_name),
            data=ctx.ceph[cluster_name].admin_keyring,
            sudo=True)
    try:
        yield
    finally:
        ctx.cluster.run(args=[
            'sudo', 'rm', '-f',
            '/etc/ceph/{}.conf'.format(cluster_name),
            '/etc/ceph/{}.client.admin.keyring'.format(cluster_name),
        ])

@contextlib.contextmanager
def crush_setup(ctx, config):
    cluster_name = config['cluster']

    profile = config.get('crush_tunables', 'default')
    log.info('Setting crush tunables to %s', profile)
    _shell(ctx, cluster_name, ctx.ceph[cluster_name].bootstrap_remote,
        args=['ceph', 'osd', 'crush', 'tunables', profile])
    yield

@contextlib.contextmanager
def create_rbd_pool(ctx, config):
    if config.get('create_rbd_pool', False):
      cluster_name = config['cluster']
      log.info('Waiting for OSDs to come up')
      teuthology.wait_until_osds_up(
          ctx,
          cluster=ctx.cluster,
          remote=ctx.ceph[cluster_name].bootstrap_remote,
          ceph_cluster=cluster_name,
      )
      log.info('Creating RBD pool')
      _shell(ctx, cluster_name, ctx.ceph[cluster_name].bootstrap_remote,
          args=['sudo', 'ceph', '--cluster', cluster_name,
                'osd', 'pool', 'create', 'rbd', '8'])
      _shell(ctx, cluster_name, ctx.ceph[cluster_name].bootstrap_remote,
          args=['sudo', 'ceph', '--cluster', cluster_name,
                'osd', 'pool', 'application', 'enable',
                'rbd', 'rbd', '--yes-i-really-mean-it'
          ])
    yield

@contextlib.contextmanager
def _bypass():
    yield

@contextlib.contextmanager
def initialize_config(ctx, config):
    cluster_name = config['cluster']
    testdir = teuthology.get_testdir(ctx)

    ctx.ceph[cluster_name].thrashers = []
    # fixme: setup watchdog, ala ceph.py

    ctx.ceph[cluster_name].roleless = False  # see below

    first_ceph_cluster = False
    if not hasattr(ctx, 'daemons'):
        first_ceph_cluster = True

    # cephadm mode?
    if 'cephadm_mode' not in config:
        config['cephadm_mode'] = 'root'
    assert config['cephadm_mode'] in ['root', 'cephadm-package']
    if config['cephadm_mode'] == 'root':
        ctx.cephadm = testdir + '/cephadm'
    else:
        ctx.cephadm = 'cephadm'  # in the path

    if first_ceph_cluster:
        # FIXME: this is global for all clusters
        ctx.daemons = DaemonGroup(
            use_cephadm=ctx.cephadm)

    # uuid
    fsid = str(uuid.uuid1())
    log.info('Cluster fsid is %s' % fsid)
    ctx.ceph[cluster_name].fsid = fsid

    # mon ips
    log.info('Choosing monitor IPs and ports...')
    remotes_and_roles = ctx.cluster.remotes.items()
    ips = [host for (host, port) in
           (remote.ssh.get_transport().getpeername() for (remote, role_list) in remotes_and_roles)]

    if config.get('roleless', False):
        # mons will be named after hosts
        first_mon = None
        for remote, _ in remotes_and_roles:
            ctx.cluster.remotes[remote].append('mon.' + remote.shortname)
            if not first_mon:
                first_mon = remote.shortname
                bootstrap_remote = remote
        log.info('No mon roles; fabricating mons')

    roles = [role_list for (remote, role_list) in ctx.cluster.remotes.items()]

    ctx.ceph[cluster_name].mons = get_mons(
        roles, ips, cluster_name,
        mon_bind_msgr2=config.get('mon_bind_msgr2', True),
        mon_bind_addrvec=config.get('mon_bind_addrvec', True),
    )
    log.info('Monitor IPs: %s' % ctx.ceph[cluster_name].mons)

    if config.get('roleless', False):
        ctx.ceph[cluster_name].roleless = True
        ctx.ceph[cluster_name].bootstrap_remote = bootstrap_remote
        ctx.ceph[cluster_name].first_mon = first_mon
        ctx.ceph[cluster_name].first_mon_role = 'mon.' + first_mon
    else:
        first_mon_role = sorted(ctx.ceph[cluster_name].mons.keys())[0]
        _, _, first_mon = teuthology.split_role(first_mon_role)
        (bootstrap_remote,) = ctx.cluster.only(first_mon_role).remotes.keys()
        log.info('First mon is mon.%s on %s' % (first_mon,
                                                bootstrap_remote.shortname))
        ctx.ceph[cluster_name].bootstrap_remote = bootstrap_remote
        ctx.ceph[cluster_name].first_mon = first_mon
        ctx.ceph[cluster_name].first_mon_role = first_mon_role

        others = ctx.cluster.remotes[bootstrap_remote]
        mgrs = sorted([r for r in others
                       if teuthology.is_type('mgr', cluster_name)(r)])
        if not mgrs:
            raise RuntimeError('no mgrs on the same host as first mon %s' % first_mon)
        _, _, first_mgr = teuthology.split_role(mgrs[0])
        log.info('First mgr is %s' % (first_mgr))
        ctx.ceph[cluster_name].first_mgr = first_mgr
    yield

@contextlib.contextmanager
def task(ctx, config):
    """
    Deploy ceph cluster using cephadm

    For example, teuthology.yaml can contain the 'defaults' section:

        defaults:
          cephadm:
            containers:
              image: 'quay.io/ceph-ci/ceph'

    Using overrides makes it possible to customize it per run.
    The equivalent 'overrides' section looks like:

        overrides:
          cephadm:
            containers:
              image: 'quay.io/ceph-ci/ceph'

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """
    if config is None:
        config = {}

    assert isinstance(config, dict), \
        "task only supports a dictionary for configuration"

    overrides = ctx.config.get('overrides', {})
    teuthology.deep_merge(config, overrides.get('ceph', {}))
    teuthology.deep_merge(config, overrides.get('cephadm', {}))
    log.info('Config: ' + str(config))

    # set up cluster context
    if not hasattr(ctx, 'ceph'):
        ctx.ceph = {}
        ctx.managers = {}
    if 'cluster' not in config:
        config['cluster'] = 'ceph'
    cluster_name = config['cluster']
    if cluster_name not in ctx.ceph:
        ctx.ceph[cluster_name] = argparse.Namespace()
        ctx.ceph[cluster_name].bootstrapped = False

    # image
    teuth_defaults = teuth_config.get('defaults', {})
    cephadm_defaults = teuth_defaults.get('cephadm', {})
    containers_defaults = cephadm_defaults.get('containers', {})
    container_image_name = containers_defaults.get('image', None)

    containers = config.get('containers', {})
    container_image_name = containers.get('image', container_image_name)


    if not hasattr(ctx.ceph[cluster_name], 'image'):
        ctx.ceph[cluster_name].image = config.get('image')
    ref = None
    if not ctx.ceph[cluster_name].image:
        if not container_image_name:
            raise Exception("Configuration error occurred. "
                            "The 'image' value is undefined for 'cephadm' task. "
                            "Please provide corresponding options in the task's "
                            "config, task 'overrides', or teuthology 'defaults' "
                            "section.")
        sha1 = config.get('sha1')
        flavor = config.get('flavor', 'default')

        if sha1:
            if flavor == "crimson":
                ctx.ceph[cluster_name].image = container_image_name + ':' + sha1 + '-' + flavor
            else:
                ctx.ceph[cluster_name].image = container_image_name + ':' + sha1
            ref = sha1
        else:
            # hmm, fall back to branch?
            branch = config.get('branch', 'master')
            ref = branch
            ctx.ceph[cluster_name].image = container_image_name + ':' + branch
    log.info('Cluster image is %s' % ctx.ceph[cluster_name].image)


    with contextutil.nested(
            #if the cluster is already bootstrapped bypass corresponding methods
            lambda: _bypass() if (ctx.ceph[cluster_name].bootstrapped)\
                              else initialize_config(ctx=ctx, config=config),
            lambda: ceph_initial(),
            lambda: normalize_hostnames(ctx=ctx),
            lambda: _bypass() if (ctx.ceph[cluster_name].bootstrapped)\
                              else download_cephadm(ctx=ctx, config=config, ref=ref),
            lambda: ceph_log(ctx=ctx, config=config),
            lambda: ceph_crash(ctx=ctx, config=config),
            lambda: _bypass() if (ctx.ceph[cluster_name].bootstrapped)\
                              else ceph_bootstrap(ctx, config),
            lambda: crush_setup(ctx=ctx, config=config),
            lambda: ceph_mons(ctx=ctx, config=config),
            lambda: distribute_config_and_admin_keyring(ctx=ctx, config=config),
            lambda: ceph_mgrs(ctx=ctx, config=config),
            lambda: ceph_osds(ctx=ctx, config=config),
            lambda: ceph_mdss(ctx=ctx, config=config),
            lambda: ceph_rgw(ctx=ctx, config=config),
            lambda: ceph_iscsi(ctx=ctx, config=config),
            lambda: ceph_monitoring('prometheus', ctx=ctx, config=config),
            lambda: ceph_monitoring('node-exporter', ctx=ctx, config=config),
            lambda: ceph_monitoring('alertmanager', ctx=ctx, config=config),
            lambda: ceph_monitoring('grafana', ctx=ctx, config=config),
            lambda: ceph_clients(ctx=ctx, config=config),
            lambda: create_rbd_pool(ctx=ctx, config=config),
    ):
        ctx.managers[cluster_name] = CephManager(
            ctx.ceph[cluster_name].bootstrap_remote,
            ctx=ctx,
            logger=log.getChild('ceph_manager.' + cluster_name),
            cluster=cluster_name,
            cephadm=True,
        )

        try:
            if config.get('wait-for-healthy', True):
                healthy(ctx=ctx, config=config)

            log.info('Setup complete, yielding')
            yield

        finally:
            log.info('Teardown begin')
