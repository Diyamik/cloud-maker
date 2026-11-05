#!/usr/bin/env python3
"""Offline cleanup of exactly the configured Cloud Maker state directory."""
import fcntl
import shutil
from cloudmaker import ROOT, vms, stop

if ROOT.exists():
    with (ROOT / 'service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Stop the API service before running offline cleanup')
        for vm in vms():
            stop(vm)
            shutil.rmtree(ROOT / vm['id'])
        (ROOT / 'images.json').unlink(missing_ok=True)
        # Keep the locked file and directory to avoid races with a new service.
        print('All managed VMs, disks, seeds and image registrations removed. Base images kept.')
