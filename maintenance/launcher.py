"""Empty mount preparation only; never imports the FlowSignal application."""
import json
import os
import stat
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

UID, GID = 501, 1000

def valid_port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError('PORT')
    return port

def mounted_fd():
    fd = os.open('/state', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        with open('/proc/self/fdinfo/'+str(fd), encoding='ascii') as f:
            mount_id = next(line.split()[1] for line in f if line.startswith('mnt_id:'))
        with open('/proc/self/mountinfo', encoding='ascii') as f:
            records = [line.split() for line in f]
        matches = [r for r in records if r[0] == mount_id and r[4] == '/state']
        if len(matches) != 1 or matches[0][2] != f'{os.major(s.st_dev)}:{os.minor(s.st_dev)}':
            raise ValueError('NOT_MOUNTED')
        named = os.stat('/state', follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (s.st_dev, s.st_ino):
            raise ValueError('PATH_CHANGED')
        return fd
    except BaseException:
        os.close(fd)
        raise

def state_info(fd):
    s = os.fstat(fd)
    if (s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)) != (UID, GID, 0o700):
        raise ValueError('STATE_IDENTITY')
    return {'maintenance': True, 'runtime_uid': os.geteuid(), 'runtime_gid': os.getegid(),
            'state_uid': s.st_uid, 'state_gid': s.st_gid, 'state_mode': '0700'}

def drop_privileges():
    if os.geteuid() != 0:
        raise ValueError('ROOT_REQUIRED')
    os.setgroups([])
    os.setgid(GID)
    os.setuid(UID)
    if os.getresuid() != (UID, UID, UID) or os.getresgid() != (GID, GID, GID) or os.getgroups():
        raise ValueError('PRIVILEGE_DROP')
    try:
        os.setuid(0)
    except PermissionError:
        return
    raise ValueError('ROOT_REGAINED')

def prepare():
    port = valid_port(os.environ.get('PORT', '10000'))
    if os.geteuid() != 0:
        raise ValueError('ROOT_REQUIRED')
    fd = mounted_fd()
    try:
        if os.listdir(fd):
            raise ValueError('STATE_NOT_EMPTY')
        before = os.fstat(fd)
        # Avoid even ctime changes when already correct. Never recurse.
        if (before.st_uid, before.st_gid) != (UID, GID):
            os.fchown(fd, UID, GID)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        state_info(fd)
        if os.listdir(fd):
            raise ValueError('STATE_CHANGED')
        check = mounted_fd()
        try:
            a, b = os.fstat(fd), os.fstat(check)
            if (a.st_dev,a.st_ino) != (b.st_dev,b.st_ino):
                raise ValueError('MOUNT_CHANGED')
        finally:
            os.close(check)
    finally:
        os.close(fd)
    drop_privileges()
    os.execve('/opt/venv/bin/python', ['/opt/venv/bin/python','-I','-B',
              '/usr/local/lib/flowsignal-maintenance/launcher.py','serve'],
              {'PORT':str(port),'PYTHONDONTWRITEBYTECODE':'1','PYTHON_DOTENV_DISABLED':'1'})

def serve():
    if os.getresuid() != (UID,UID,UID) or os.getresgid() != (GID,GID,GID) or os.getgroups():
        raise ValueError('RUNTIME_IDENTITY')
    fd = mounted_fd()
    state_info(fd)
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                data = json.dumps(state_info(fd), sort_keys=True).encode()
                status = 200
            except Exception:
                data, status = b'{}', 503
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    try:
        HTTPServer(('0.0.0.0',valid_port(os.environ.get('PORT','10000'))), Health).serve_forever()
    finally:
        os.close(fd)

if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['serve']:
            serve()
        elif not sys.argv[1:]:
            prepare()
        else:
            raise ValueError('ARGUMENTS')
    except Exception:
        print('MAINTENANCE_START_REJECTED', file=sys.stderr)
        sys.exit(1)
