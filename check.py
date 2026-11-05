#!/usr/bin/env python3
"""End-to-end verification against real QEMU/KVM, SSH and cloud-init."""
import argparse
from contextlib import contextmanager
import shutil
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

parser = argparse.ArgumentParser()
parser.add_argument('--image', required=True, help='Absolute or relative standalone Ubuntu qcow2 cloud image')
args = parser.parse_args()
image = str(Path(args.image).resolve(strict=True))
script = str(Path(__file__).with_name('cloudmaker.py'))

@contextmanager
def test_directory():
    temp = tempfile.mkdtemp(prefix='cm-')
    try:
        yield temp
    finally:
        if list(Path(temp).glob('state/*/vm.json')):
            print('Cleanup incomplete; state preserved at ' + temp, file=sys.stderr)
        else:
            shutil.rmtree(temp)


with test_directory() as temp:
    root = Path(temp)
    env = dict(os.environ, CLOUDMAKER_STATE=str(root / 'state'), CLOUDMAKER_MAX_CPU='2', CLOUDMAKER_MAX_RAM_MB='1536')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    log = (root / 'api.log').open('w+')
    server = None
    vm_id = None

    def request(path, data=None, method=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(base + path, data=body, method=method, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def launch():
        proc = subprocess.Popen([sys.executable, script, '--port', str(port)], env=env, stdout=log, stderr=log)
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError('API exited during startup')
            try:
                request('/health')
                return proc
            except (OSError, urllib.error.URLError):
                time.sleep(.1)
        proc.terminate()
        proc.wait()
        raise RuntimeError('API startup timeout')

    def ssh(command, timeout=20):
        return subprocess.run(['ssh', '-i', str(root / 'key'), '-o', 'IdentitiesOnly=yes',
                               '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=' + str(root / 'known_hosts'),
                               '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', '-p', str(vm['ssh_port']),
                               'ubuntu@127.0.0.1', command], capture_output=True, text=True, timeout=timeout)

    try:
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / 'key')], check=True)
        server = launch()
        request('/images', {'name': 'ubuntu', 'path': image, 'description': 'Ubuntu cloud image'})
        assert request('/images')[0]['name'] == 'ubuntu'
        print('PASS: image registration', flush=True)
        vm = request('/vms', {'image': 'ubuntu', 'hostname': 'cloudmaker-test', 'cpus': 1,
                              'memory_mb': 768, 'disk_gb': 4, 'ssh_keys': [(root / 'key.pub').read_text().strip()]})
        vm_id = vm['id']
        assert vm['status'] == 'running'
        print('PASS: headless QEMU/KVM VM started', flush=True)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            probe = ssh('test -f /var/lib/cloud/instance/boot-finished && test -f /etc/cloudmaker-ready && hostname')
            if probe.returncode == 0:
                assert probe.stdout.strip() == 'cloudmaker-test', probe.stdout
                break
            time.sleep(2)
        else:
            raise RuntimeError('SSH/cloud-init readiness timeout: ' + probe.stderr)
        complete = ssh('sudo cloud-init status --wait', timeout=60)
        assert complete.returncode == 0, complete.stdout + complete.stderr
        print('PASS: SSH key, hostname and completed cloud-init inside guest', flush=True)
        try:
            request('/vms', {'image': 'ubuntu', 'cpus': 2, 'memory_mb': 1024})
            raise AssertionError('Quota should reject creation')
        except urllib.error.HTTPError as exc:
            assert exc.code == 400 and 'quota' in exc.read().decode().lower()
        print('PASS: CPU/RAM quota rejection', flush=True)
        server.terminate()
        server.wait(timeout=10)
        server = launch()
        assert request('/vms/' + vm_id)['status'] == 'running'
        assert ssh('true').returncode == 0
        assert request('/images')[0]['name'] == 'ubuntu'
        print('PASS: service restart preserves guest and image registry', flush=True)
        assert request('/vms/' + vm_id + '/stop', {})['status'] == 'stopped'
        assert request('/vms/' + vm_id + '/start', {})['status'] == 'running'
        print('PASS: stop and start', flush=True)
        request('/vms/' + vm_id, method='DELETE')
        assert request('/vms') == []
        assert not (root / 'state' / vm_id).exists()
        vm_id = None
        print('PASS: delete and cleanup', flush=True)
        vm = request('/vms', {'image': 'ubuntu', 'hostname': 'custom-test', 'memory_mb': 768,
                             'user_data': '#cloud-config\n' + json.dumps({
                                 'hostname': 'custom-test',
                                 'users': [{'name': 'ubuntu', 'shell': '/bin/bash',
                                            'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'lock_passwd': True,
                                            'ssh_authorized_keys': [(root / 'key.pub').read_text().strip()]}],
                                 'write_files': [{'path': '/etc/custom-user-data', 'content': 'custom-marker'}]})})
        vm_id = vm['id']
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            probe = ssh('test -f /var/lib/cloud/instance/boot-finished && cat /etc/custom-user-data')
            if probe.returncode == 0:
                assert probe.stdout.strip() == 'custom-marker'
                break
            time.sleep(2)
        else:
            raise RuntimeError('Custom user_data timeout: ' + probe.stderr)
        print('PASS: custom user_data applied in real guest', flush=True)
        server.terminate()
        server.wait(timeout=10)
        subprocess.run([sys.executable, str(Path(__file__).with_name('cleanup.py'))], env=env, check=True)
        assert not (root / 'state' / vm_id).exists()
        assert not (root / 'state' / 'images.json').exists()
        vm_id = None
        print('PASS: offline cleanup of running VM; all checks passed', flush=True)
    except BaseException:
        log.flush()
        log.seek(0)
        print(log.read(), file=sys.stderr)
        if vm_id:
            serial = root / 'state' / vm_id / 'serial.log'
            if serial.exists():
                print(serial.read_text(errors='replace')[-12000:], file=sys.stderr)
        raise
    finally:
        if vm_id:
            # Preserve state on cleanup failure, rather than deleting a running guest's disk.
            try:
                request('/vms/' + vm_id, method='DELETE')
            except Exception:
                if server and server.poll() is None:
                    server.terminate()
                    server.wait(timeout=10)
                subprocess.run([sys.executable, str(Path(__file__).with_name('cleanup.py'))], env=env, check=True)
        if server and server.poll() is None:
            server.terminate()
            server.wait(timeout=10)
        log.close()
