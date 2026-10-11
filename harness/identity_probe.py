"""Real runner identity/sudo checks, inside the network-denied namespace only."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile

assert (os.getuid(), os.getgid()) == (501, 1000)
entry = pwd.getpwuid(501)
assert (entry.pw_name, entry.pw_gid, entry.pw_dir, entry.pw_shell) == (
    'flowsignal-cert-501', 1000, '/nonexistent', '/usr/sbin/nologin')
assert subprocess.check_output(['getent', 'passwd', '501'], text=True).split(':')[0] == entry.pw_name
interfaces = {line.split(':', 1)[0].strip() for line in Path('/proc/net/dev').read_text().splitlines() if ':' in line}
assert interfaces == {'lo'}
net = os.readlink('/proc/self/ns/net')
assert net != sys.argv[2], 'HOST_NETWORK_NAMESPACE'
mount = os.readlink('/proc/self/ns/mnt')
with tempfile.TemporaryDirectory(prefix='identity-probe-', dir=sys.argv[1]) as temp:
    path = Path(temp) / 'probe'
    path.write_bytes(b'synthetic privileged fixture probe\n')
    subprocess.run(['sudo', '-n', '/usr/bin/chown', '0:0', str(path)], check=True)
    assert (path.stat().st_uid, path.stat().st_gid) == (0, 0)
    subprocess.run(['sudo', '-n', '/usr/bin/chown', '501:1000', str(path)], check=True)
    assert (path.stat().st_uid, path.stat().st_gid) == (501, 1000)
    result = subprocess.check_output([
        'sudo', '-n', '/usr/bin/unshare', '--mount', '--propagation', 'private', '--',
        '/bin/sh', '-c', 'readlink /proc/self/ns/net; readlink /proc/self/ns/mnt'], text=True).splitlines()
    assert result[0] == net and result[1] != mount
    # An unrelated privileged executable must remain forbidden.
    denied = subprocess.run(['sudo', '-n', '/usr/bin/id'], capture_output=True)
    assert denied.returncode != 0, 'SUDO_AUTHORIZATION_TOO_BROAD'
print(json.dumps({'uid': 501, 'gid': 1000, 'passwd_identity': entry.pw_name,
    'sudo_chown': True, 'sudo_unshare': True, 'unrelated_sudo_denied': True,
    'network_isolated': True, 'probe_removed': True}, sort_keys=True))
