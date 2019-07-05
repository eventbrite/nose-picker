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

import hashlib
import logging
import os

from nose.plugins import Plugin


def hash_filename(filename):
    '''Design goal:

    * Take a filename and output a number.
    * Return the same number even if the filename
      is now in a different path.

    To achieve that, it assumes that filename is a sub-path
    of the current working directory, and then removes the
    current working directory from the path.
    '''
    here = os.path.abspath(os.getcwd())
    there = os.path.abspath(filename)
    assert there.startswith(here)

    shorter_there = there[len(here):]
    as_int = int(hashlib.sha1(shorter_there).hexdigest(), 16)
    return as_int


class NosePicker(Plugin):
    name = 'nose-picker'

    def __init__(self, *args, **kwargs):
        self.output = True
        self.enableOpt = 'with-nose-picker'
        self.logger = logging.getLogger('nose.plugins.picker')

    def options(self, parser, env=os.environ):
        parser.add_option(
            '--which-process',
            type='int',
            dest='which_process',
            help='nose-picker: Which process number this is of the total.',
        )
        parser.add_option(
            '--futz-with-django',
            action='store_true',
            dest='futz_with_django',
            help='nose-picker: Whether to futz with the django configuration.',
        )
        parser.add_option(
            '--total-processes',
            type='int',
            dest='total_processes',
            help='nose-picker: How many total processes to run with.',
        )
        super(NosePicker, self).options(parser, env=env)

    def configure(self, options, config):
        self.enabled = getattr(options, self.enableOpt)
        self.total_processes = options.total_processes
        self.which_process = options.which_process

        if options.futz_with_django:
            import django
            from django.db import connections

            for connection in connections.all():
                test_alias = 'test_{name}__{process}'.format(
                    name=connection.settings_dict['NAME'],
                    process=self.which_process,
                )

                if django.VERSION >= (1, 7):
                    connection.settings_dict.setdefault('TEST', {})
                    connection.settings_dict['TEST']['NAME'] = test_alias
                else:
                    connection.settings_dict['TEST_NAME'] = test_alias

        super(NosePicker, self).configure(options, config)

    def wantFile(self, fullpath):
        """
        Do we want to run this file?  See _should_run.
        """
        return self._should_run(fullpath)

    def _should_run(self, name):
        if self.enabled:
            hashed_value = hash_filename(name) % self.total_processes
            if hashed_value == self.which_process:
                return None
            return False

        return None
