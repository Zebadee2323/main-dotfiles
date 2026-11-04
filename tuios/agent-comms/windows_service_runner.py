"""Task Scheduler action: run the listener in this PID and capture early errors."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import runpy
import sys
import traceback


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    forwarded = args.args[1:] if args.args[:1] == ["--"] else args.args
    # No caller pane identity or speech credentials belong in the OS-owned job.
    for key in list(os.environ):
        if key.startswith(("TUIOS_", "ELEVENLABS_", "AGENT_COMMS_")):
            os.environ.pop(key)
    os.environ["AGENT_COMMS_SERVICE_ID"] = args.service_id
    log = Path(args.log_file)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8", buffering=1) as stream:
        old_out, old_err, old_argv = sys.stdout, sys.stderr, sys.argv
        try:
            sys.stdout = sys.stderr = stream
            script = Path(__file__).with_name("agent_comms.py").resolve()
            sys.argv = [str(script), *forwarded]
            # runpy, not Popen: Task Scheduler owns the actual listener PID.
            runpy.run_path(str(script), run_name="__main__")
            return 0
        except SystemExit as exc:
            if exc.code is None or isinstance(exc.code, int):
                return exc.code or 0
            print(exc.code, file=stream)
            return 1
        except Exception:
            traceback.print_exc(file=stream)
            return 1  # Nonzero exit activates the task's failure-retry policy.
        finally:
            sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv


if __name__ == "__main__":
    sys.exit(main())
