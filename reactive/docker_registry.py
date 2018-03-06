from charmhelpers.core.hookenv import log
from charmhelpers.core.hookenv import config
from charmhelpers.core.hookenv import open_port
from charmhelpers.core.hookenv import close_port
from charmhelpers.core.hookenv import status_set
from charmhelpers.core.hookenv import resource_get
from charmhelpers.core.hookenv import storage_get
from charmhelpers.core.hookenv import DEBUG
from charmhelpers.core import host
from charmhelpers.core import unitdata

from charms import apt
from charms.docker import Compose

from charms.reactive import hook, when, when_not, when_any
from charms.reactive import set_state, is_state, remove_state

from charms.templating.jinja2 import render

from subprocess import check_call

import os.path
import shutil
import subprocess
import time
import base64


def start():
    compose = Compose('files/docker-registry')
    compose.up()
    open_port(config('registry_port'))
    status_set('active', 'Docker registry ready.')


@hook('stop')
def stop():
    compose = Compose('files/docker-registry')
    compose.down()
    close_port(config().previous('registry_port'))
    status_set('maintenance', 'Docker registry stopped.')


@when('docker.available')
@when_not('docker-registry.standalone.running')
def start_standalone():
    path = resource_get('registry')
    if path:
        check_call(['docker', 'load', '-i', path])
    else:
        status_set('blocked', 'Please attach a registry image.')
        return

    render('docker-compose.yml',
           'files/docker-registry/docker-compose.yml',
           config())

    start()
    set_state('docker-registry.standalone.running')
    status_set('active', 'Docker registry ready.')


@when_any('config.changed.registry_port',
          'config.changed.registry_tag',
          'config.changed.registry_cert',
          'config.changed.registry_key',
          'config.changed.registry_htpasswd',
          'config.changed.registry_config')
def reconfigure():
    cfg = config()
    # guard on first run, no previous values, so do nothing
    if not cfg.previous('registry_port') or not cfg.previous('registry_tag'):
        return

    status_set('maintenance', 'Re-configuring port bindings.')
    close_port(cfg.previous('registry_port'))

    if config('registry_cert') and config('registry_key'):
        status_set('maintenance', 'Writing TLS files to /etc/ssl.')
        with open('/etc/ssl/certs/docker-registry.crt', 'wb') as f:
            f.write(base64.b64decode(config('registry_cert')))
        with open('/etc/ssl/private/docker-registry.key', 'wb') as f:
            f.write(base64.b64decode(config('registry_key')))

    if config('registry_htpasswd') and (not config('registry_key') or
                                        not config('registry_cert')):
        status_set(
            'blocked', 'Registry htpasswd needs TLS for basic-realm security')
        return
    else:
        status_set('maintenance', 'Writing htpasswd to /usr/local/etc.')
        with open('/usr/local/etc/htpasswd', 'wb') as f:
            f.write(base64.b64decode(config('registry_htpasswd')))

    if config('registry_config'):
        status_set('maintenance', 'Writing registry config to /usr/local/etc.')
        with open('/usr/local/etc/registry.yaml', 'wb') as f:
            f.write(base64.b64decode(config('registry_config')))

    status_set('maintenance', 'Re-generating Docker compose YAML.')
    render('docker-compose.yml',
           'files/docker-registry/docker-compose.yml',
           config())

    remove_state('docker-registry.standalone.running')
    remove_state('docker-registry.running')

    status_set('active', 'Docker registry ready.')


@when('website.available')
def configure_website(website):
    website.configure(port=config('registry_port'))


# storage support was all taken from the postgresql charm
data_path_key = 'docker-registry.storage.registry.path'
data_mount_key = 'docker-registry.storage.registry.mount'


@hook('docker-registry-storage-attached')
def attach():
    mount = storage_get()['location']
    unitdata.kv().set(data_mount_key, mount)
    unitdata.kv().set(
        data_path_key, os.path.join(mount, 'var/lib/docker'))

    log('Docker registry storage attached: {}'.format(mount))

    if os.path.exists('/var/lib/docker'):
        required_space = shutil.disk_usage('/var/lib/docker').used
        free_space = shutil.disk_usage(mount).free

        if required_space > free_space:
            status_set('blocked', 'Not enough free storage space.')
        return

    apt.queue_install(['rsync'])
    set_state('docker-registry.storage.docker-registry.attached')


@hook('docker-registry-storage-detaching')
def detaching():
    unitdata.kv().unset(data_mount_key)
    unitdata.kv().unset(data_path_key)
    remove_state('docker-registry.storage.docker-registry.attached')


@when('docker-registry.storage.docker-registry.attached')
@when('docker.available')
@when('docker-registry.standalone.running')
@when('apt.installed.rsync')
@when_not('docker-registry.storage.docker-registry.migrated')
def migrate():
    if is_state('docker-registry.standalone.running'):
        stop()
        host.service_stop('docker')

    old_data_dir = '/var/lib/docker'
    new_data_dir = unitdata.kv().get(data_path_key)

    if old_data_dir == new_data_dir:
        status_set(
            'blocked', 'Cannot migrate {} over itself.'.format(old_data_dir))
        return

    backup_data_dir = '{}-{}'.format(old_data_dir, int(time.time()))

    status_set('maintenance', 'Migrating data from {} to {}'.format(
        old_data_dir, new_data_dir))

    if os.path.exists(new_data_dir):
        assert os.path.isdir(new_data_dir), '{} is not a directory'
    else:
        os.makedirs(new_data_dir)
    shutil.chown(new_data_dir, 'root', 'root')
    os.chmod(new_data_dir, 0o700)

    try:
        rsync_cmd = ['rsync', '-av',
                     old_data_dir + '/',
                     new_data_dir + '/']
        log('Running {}'.format(' '.join(rsync_cmd)), DEBUG)
        subprocess.check_call(rsync_cmd, universal_newlines=True)
    except subprocess.CalledProcessError:
        status_set('blocked', 'Failed to sync data from {} to {}'.format(
            old_data_dir, new_data_dir))
        return

    os.replace(old_data_dir, backup_data_dir)
    os.symlink(new_data_dir, old_data_dir)

    host.service_start('docker')
    start()

    set_state('docker-registry.storage.docker-registry.migrated')
    status_set('active', 'Docker registry ready.')
