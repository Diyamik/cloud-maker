#!/usr/bin/env python3
"""Small local QEMU/KVM control plane. No root privileges required."""
import argparse
import fcntl
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pycdlib

LOCK = threading.RLock()
ROOT = Path(os.environ.get('CLOUDMAKER_STATE', 'state')).resolve()
MAX_CPU = int(os.environ.get('CLOUDMAKER_MAX_CPU', '4'))
MAX_RAM = int(os.environ.get('CLOUDMAKER_MAX_RAM_MB', '4096'))


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def qmp(vm, command):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(5)
        s.connect(str(ROOT / vm['id'] / 'qmp.sock'))
        with s.makefile('rwb') as stream:
            json.loads(stream.readline())
            for cmd in ('qmp_capabilities', command):
                stream.write(json.dumps({'execute': cmd}).encode() + b'\n')
                stream.flush()
                while True:
                    raw = stream.readline()
                    if not raw:
                        raise OSError('QMP closed')
                    reply = json.loads(raw)
                    if 'error' in reply:
                        raise ValueError(str(reply['error']))
                    if 'return' in reply:
                        break
            return reply['return']


def status(vm):
    try:
        vm['status'] = qmp(vm, 'query-status')['status']
    except (OSError, ValueError):
        vm['status'] = 'stopped'
    return vm


def get_vm(ident):
    if not re.fullmatch(r'[0-9a-f]{12}', ident):
        raise KeyError('VM not found')
    path = ROOT / ident / 'vm.json'
    if not path.exists():
        raise KeyError('VM not found')
    return status(json.loads(path.read_text()))


def vms():
    return [status(json.loads(p.read_text())) for p in sorted(ROOT.glob('*/vm.json'))]


def images():
    return json.loads((ROOT / 'images.json').read_text())


def register(data):
    name = data.get('name', '')
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', name):
        raise ValueError('Invalid image name')
    path = Path(data.get('path', '')).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Image must be a file')
    info = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', str(path)]))
    if info['format'] != 'qcow2' or info.get('backing-filename'):
        raise ValueError('Use a standalone qcow2 cloud image without backing files')
    db = images()
    db[name] = {'name': name, 'path': str(path), 'description': str(data.get('description', ''))}
    save(ROOT / 'images.json', db)
    return db[name]


def positive(data, key, default, maximum):
    val = data.get(key, default)
    if type(val) is not int or not 1 <= val <= maximum:
        raise ValueError(f'{key} must be an integer between 1 and {maximum}')
    return val


def create(data):
    image = images().get(data.get('image'))
    if not image:
        raise ValueError('Register the image first')
    cpu = positive(data, 'cpus', 1, MAX_CPU)
    ram = positive(data, 'memory_mb', 512, MAX_RAM)
    disk = positive(data, 'disk_gb', 4, 100)
    image_info = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', image['path']]))
    if disk * 1024**3 < image_info['virtual-size']:
        raise ValueError('disk_gb is smaller than the base image virtual size')
    existing = vms()
    if sum(v['cpus'] for v in existing) + cpu > MAX_CPU or sum(v['memory_mb'] for v in existing) + ram > MAX_RAM:
        raise ValueError('Resource quota exceeded (stopped VMs also reserve resources)')
    hostname = data.get('hostname', 'cloudmaker')
    if not isinstance(hostname, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', hostname) or hostname.endswith('-'):
        raise ValueError('Invalid hostname')
    keys = data.get('ssh_keys', [])
    if not isinstance(keys, list) or not all(isinstance(k, str) and k.startswith(('ssh-', 'ecdsa-')) and '\n' not in k for k in keys):
        raise ValueError('ssh_keys must be a list of SSH public keys')
    user_data = data.get('user_data')
    if user_data is None:
        user_data = '#cloud-config\n' + json.dumps({
            'hostname': hostname, 'manage_etc_hosts': True,
            'users': [{'name': 'ubuntu', 'shell': '/bin/bash', 'sudo': 'ALL=(ALL) NOPASSWD:ALL',
                       'lock_passwd': True, 'ssh_authorized_keys': keys}],
            'ssh_pwauth': False,
            'write_files': [{'path': '/etc/cloudmaker-ready', 'content': 'cloud-init configured this VM\n'}],
        }) + '\n'
    if not isinstance(user_data, str) or not user_data.startswith('#cloud-config\n'):
        raise ValueError('user_data must start with #cloud-config followed by newline')
    ident = uuid.uuid4().hex[:12]
    directory = ROOT / ident
    directory.mkdir()
    try:
        # Ask the OS for a currently available port; QEMU reports a bind error if another process races us.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', image['path'], str(directory / 'disk.qcow2')], check=True, capture_output=True)
        subprocess.run(['qemu-img', 'resize', str(directory / 'disk.qcow2'), f'{disk}G'], check=True, capture_output=True)
        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=3, joliet=3, rock_ridge='1.09', vol_ident='cidata')
        for name, content, iso_name in [
            ('user-data', user_data, 'USERDATA.;1'),
            ('meta-data', json.dumps({'instance-id': ident, 'local-hostname': hostname}), 'METADATA.;1')]:
            raw = content.encode()
            iso.add_fp(io.BytesIO(raw), len(raw), iso_path='/' + iso_name, rr_name=name, joliet_path='/' + name)
        iso.write(str(directory / 'seed.iso'))
        iso.close()
        vm = {'id': ident, 'hostname': hostname, 'image': data['image'], 'cpus': cpu,
              'memory_mb': ram, 'disk_gb': disk, 'ssh_port': port, 'created_at': time.time()}
        save(directory / 'vm.json', vm)
        start(vm)
        return status(vm)
    except Exception:
        # Only remove artifacts after ensuring a partially started guest is gone.
        if (directory / 'vm.json').exists():
            try:
                stop(json.loads((directory / 'vm.json').read_text()))
            except Exception:
                raise RuntimeError(f'Creation failed; inspect and delete VM {ident}')
        shutil.rmtree(directory)
        raise


def start(vm):
    if status(vm)['status'] != 'stopped':
        raise ValueError('VM is already running')
    directory = ROOT / vm['id']
    (directory / 'qmp.sock').unlink(missing_ok=True)
    command = ['qemu-system-x86_64', '-name', 'cloudmaker-' + vm['id'], '-enable-kvm', '-cpu', 'host',
               '-smp', str(vm['cpus']), '-m', str(vm['memory_mb']), '-display', 'none',
               '-monitor', 'none', '-serial', 'file:' + str(directory / 'serial.log'),
               '-drive', f'file={directory}/disk.qcow2,if=virtio,format=qcow2',
               '-drive', f'file={directory}/seed.iso,media=cdrom,readonly=on',
               '-netdev', f'user,id=net0,hostfwd=tcp:127.0.0.1:{vm["ssh_port"]}-:22',
               '-device', 'virtio-net-pci,netdev=net0',
               '-qmp', f'unix:{directory}/qmp.sock,server=on,wait=off',
               '-pidfile', str(directory / 'qemu.pid'), '-daemonize']
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode:
        raise ValueError('QEMU failed: ' + proc.stderr.strip())
    for _ in range(50):
        if status(vm)['status'] == 'running':
            return vm
        time.sleep(.1)
    raise ValueError('QEMU did not become ready')


def stop(vm):
    if status(vm)['status'] == 'stopped':
        return vm
    qmp(vm, 'quit')
    for _ in range(100):
        if status(vm)['status'] == 'stopped':
            # QMP may close just before QEMU releases its disk; wait for socket removal.
            if not (ROOT / vm['id'] / 'qmp.sock').exists():
                return vm
        time.sleep(.1)
    raise ValueError('VM did not stop; artifacts preserved')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def dispatch(self, method):
        try:
            with LOCK:
                if self.headers.get('Origin'):
                    raise ValueError('Browser cross-origin requests are disabled')
                data = {}
                if method == 'POST':
                    if self.headers.get_content_type() != 'application/json':
                        raise ValueError('Content-Type application/json required')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 131072:
                        raise ValueError('JSON body must be 1..131072 bytes')
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError('JSON object required')
                parts = self.path.strip('/').split('/')
                code = 200
                if method == 'GET' and parts == ['health']:
                    result = {'status': 'ok', 'max_cpu': MAX_CPU, 'max_ram_mb': MAX_RAM}
                elif parts == ['images'] and method == 'GET':
                    result = list(images().values())
                elif parts == ['images'] and method == 'POST':
                    result, code = register(data), 201
                elif parts == ['vms'] and method == 'GET':
                    result = vms()
                elif parts == ['vms'] and method == 'POST':
                    result, code = create(data), 201
                elif len(parts) in (2, 3) and parts[0] == 'vms':
                    vm = get_vm(parts[1])
                    if len(parts) == 2 and method == 'GET':
                        result = vm
                    elif len(parts) == 2 and method == 'DELETE':
                        stop(vm)
                        shutil.rmtree(ROOT / vm['id'])
                        result = {'deleted': vm['id']}
                    elif len(parts) == 3 and method == 'POST' and parts[2] in ('start', 'stop'):
                        result = start(vm) if parts[2] == 'start' else stop(vm)
                    else:
                        raise KeyError('Route not found')
                else:
                    raise KeyError('Route not found')
            self.reply(code, result)
        except KeyError as exc:
            self.reply(404, {'error': str(exc)})
        except (ValueError, TypeError, OSError, subprocess.SubprocessError) as exc:
            self.reply(400, {'error': str(exc)})
        except Exception as exc:
            self.log_error('Internal error: %r', exc)
            self.reply(500, {'error': 'Internal server error; see service log'})

    def reply(self, code, result):
        body = json.dumps(result).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ROOT, 0o700)
    # One API writer per state directory, including across independent processes.
    lockfile = (ROOT / 'service.lock').open('w')
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if len(str(ROOT)) > 70:
        raise SystemExit('State path too long for QMP Unix sockets; set CLOUDMAKER_STATE to a shorter absolute path')
    if not os.access('/dev/kvm', os.R_OK | os.W_OK):
        raise SystemExit('No access to /dev/kvm; check KVM support and user permissions')
    if not (ROOT / 'images.json').exists():
        save(ROOT / 'images.json', {})
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Cloud Maker: http://127.0.0.1:{args.port}; state={ROOT}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
