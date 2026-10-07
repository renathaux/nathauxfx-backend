"""Explicit operator CLI, never called by the maintenance server."""
import os
import sys

def safe_environment(source):
    result = {key: source[key] for key in ('CUTOVER_DATABASE_URL','LANG','TZ') if key in source}
    result.update(PATH='/opt/venv/bin:/usr/local/bin:/usr/bin:/bin',
                  PYTHONDONTWRITEBYTECODE='1', PYTHON_DOTENV_DISABLED='1')
    return result

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('inventory','copy','verify'):
        raise ValueError('OPERATION')
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(1000)
        os.setuid(501)
    if os.getresuid() != (501,501,501) or os.getresgid() != (1000,1000,1000) or os.getgroups():
        raise ValueError('IDENTITY')
    try:
        os.setuid(0)
    except PermissionError:
        pass
    else:
        raise ValueError('ROOT_REGAINED')
    os.chdir('/app/Backend')
    os.execve('/opt/venv/bin/python', ['/opt/venv/bin/python','-B','-m',
              'startup_recovery.cutover',*sys.argv[1:]], safe_environment(os.environ))

if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('MAINTENANCE_HELPER_REJECTED', file=sys.stderr)
        sys.exit(1)
