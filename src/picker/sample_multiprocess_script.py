# Copyright (c) 2014, Eventbrite and Contributors
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:

# Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.

# Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.

# Neither the name of Eventbrite nor the names of its contributors may
# be used to endorse or promote products derived from this software
# without specific prior written permission.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


from __future__ import absolute_import
import multiprocessing
import subprocess
import sys
import threading
from six.moves import range


def main():
    num_processes = int(multiprocessing.cpu_count() * 2.5)
    tests = []
    for i in range(num_processes):
        test_command = TEST_CMD_TEMPLATE % (
            i,
            num_processes,
        )
        tests.append(TestWatcher(test_command))

    returncode = 0
    for test_watcher in tests:
        test_watcher.join()
        if test_watcher.returncode > 0:
            returncode += test_watcher.returncode
        for line in test_watcher.stderr.splitlines():
            if not (
                line.endswith(' ... ok') or
                '... SKIP' in line
            ):
                sys.stderr.write(line + '\n')

    return returncode


class TestWatcher(threading.Thread):
    def __init__(self, command):
        super(TestWatcher, self).__init__()
        self.command = command
        self.stdout = ''
        self.stderr = ''
        self.start()
        self.returncode = 0

    def run(self):
        p = subprocess.Popen(
            self.command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.stdout, self.stderr = p.communicate()
        self.returncode = p.returncode

if __name__ == '__main__':
    sys.exit(main())
