#!/usr/bin/env python3
"""Serialize JADX-backed Java analysis inside the QQ model sandbox."""
import fcntl
import os
import sys


def main():
    arguments = sys.argv[1:]
    if any('jadx' in value.lower() for value in arguments):
        lock = os.open(os.environ['IMPAD_JAVA_LOCK'], os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Keep ownership across exec, until the analysis JVM exits.
        os.set_inheritable(lock, True)
        arguments = ['-Xmx1536m'] + [value for value in arguments if not value.startswith(('-Xmx', '-XX:MaxHeapSize='))]
    os.execv(os.environ['IMPAD_REAL_JAVA'], [os.environ['IMPAD_REAL_JAVA'], *arguments])


if __name__ == '__main__':
    main()
