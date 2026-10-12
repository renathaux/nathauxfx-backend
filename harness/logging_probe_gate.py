"""Native isolated proof for five logging assertions, without product edits."""
from pathlib import Path
import sys
from diagnostic_gate import run

if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), logging_only=True)
