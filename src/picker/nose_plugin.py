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
import six
import hashlib
import json
import logging
import os
import re
import site
import stat
import sys

from nose.plugins import Plugin


# ---------------------------------------------------------------------------
# Path normalization shared by the hash-based path and the duration-based
# (bin-packing) path, so that JSON keys in a --file-durations file line up
# exactly with the paths hash_filename() has always used.
# ---------------------------------------------------------------------------

def _relative_key(filename, cwd=None):
    '''Strip filename down to the same "portable" relative path that
    hash_filename() has always hashed: the part of the (real, absolute) path
    after the current working directory (or, failing that, after whichever
    site-packages directory it lives under).
    '''
    working_dir = cwd if cwd is not None else os.getcwd()
    here = os.path.realpath(working_dir)
    there = os.path.realpath(filename)
    if not there.startswith(here):
        for path in site.getsitepackages():
            if there.startswith(path):
                here = path
    assert there.startswith(here), "{} must start with {} or be in site packages".format(
        there,
        os.path.realpath(working_dir),
    )
    return there[len(here):]


def hash_filename(filename):
    '''Design goal:

    * Take a filename and output a number.
    * Return the same number even if the filename
      is now in a different path.

    To achieve that, it assumes that filename is a sub-path of the current working directory or site packages,
    and then removes the current working directory from the path.
    '''
    shorter_there = six.ensure_binary(_relative_key(filename))
    as_int = int(hashlib.sha1(shorter_there).hexdigest(), 16)
    return as_int


# ---------------------------------------------------------------------------
# Best-effort reimplementation of nose's *default* file-discovery convention
# (nose.selector.Selector.wantFile / wantDirectory, nose.util.ispackage, and
# nose.config.Config's built-in ignoreFiles/testMatch/srcDirs), used only to
# build the complete candidate-file list up front for --file-durations
# bin-packing. This intentionally mirrors nose 1.3.7's defaults; it does NOT
# know about custom --match/--include/--exclude regexes, or directory
# exclusions contributed by other plugins (e.g. nose-exclude's --exclude-dir)
# on top of those defaults. See README for the known gap this leaves.
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_DEFAULT_TESTMATCH_RE = re.compile(r'(?:^|[\b_\.%s-])[Tt]est' % os.sep)
_DEFAULT_IGNORE_FILE_RES = (
    re.compile(r'^\.'),
    re.compile(r'^_'),
    re.compile(r'^setup\.py$'),
)
_DEFAULT_SRC_DIRS = ('lib', 'src')
_EXE_ALLOWED_PLATFORMS = ('win32', 'cli')


def _is_package_dir(path):
    end = os.path.basename(path)
    if not _IDENT_RE.match(end):
        return False
    for init in ('__init__.py', '__init__.pyc', '__init__.pyo'):
        if os.path.isfile(os.path.join(path, init)):
            return True
    return False


def _is_executable(path):
    try:
        st = os.stat(path)
    except OSError:
        return False
    return bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _wants_file(basename, fullpath):
    for ignore_re in _DEFAULT_IGNORE_FILE_RES:
        if ignore_re.search(basename):
            return False
    if sys.platform not in _EXE_ALLOWED_PLATFORMS and _is_executable(fullpath):
        return False
    if not basename.endswith('.py'):
        return False
    return bool(_DEFAULT_TESTMATCH_RE.search(basename))


def _wants_directory(basename, fullpath):
    if _is_package_dir(fullpath):
        return True
    return bool(_DEFAULT_TESTMATCH_RE.search(basename)) or basename in _DEFAULT_SRC_DIRS


def discover_candidate_files(root):
    '''Walk `root` and return the list of full paths for every file that
    nose's *default* discovery convention would hand to wantFile -- i.e. the
    complete candidate set that --which-process/--total-processes selection
    has always operated over one file at a time, without ever seeing the
    full list.
    '''
    root = os.path.realpath(root)
    candidates = []
    visited_dirs = set()

    def _walk(path):
        real = os.path.realpath(path)
        if real in visited_dirs:
            return
        visited_dirs.add(real)
        try:
            entries = sorted(os.listdir(path))
        except OSError:
            return
        for entry in entries:
            if entry.startswith('.'):
                continue
            entry_path = os.path.join(path, entry)
            if os.path.isfile(entry_path):
                if _wants_file(entry, entry_path):
                    candidates.append(entry_path)
            elif os.path.isdir(entry_path):
                if entry.startswith('_'):
                    continue
                if _wants_directory(entry, entry_path):
                    _walk(entry_path)

    _walk(root)
    return candidates


# ---------------------------------------------------------------------------
# Duration-aware bin-packing (LPT: longest processing time first).
# ---------------------------------------------------------------------------

def _median(values):
    '''No `statistics` module: this needs to run on Python 2.7.'''
    if not values:
        return 0.0
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _load_durations_file(path, logger):
    '''Load {relative_file_path: total_seconds} from `path`. Returns None
    (and logs why) on any failure, so callers can fall back to the classic
    hash-based behavior -- this must never raise.
    '''
    if not path:
        return None
    try:
        with open(path, 'r') as fh:
            raw = json.load(fh)
    except (IOError, OSError) as exc:
        logger.warning(
            'nose-picker: --file-durations=%s could not be read (%s); '
            'falling back to hash-based file selection.', path, exc,
        )
        return None
    except ValueError as exc:  # json.JSONDecodeError is a ValueError subclass
        logger.warning(
            'nose-picker: --file-durations=%s is not valid JSON (%s); '
            'falling back to hash-based file selection.', path, exc,
        )
        return None

    if not isinstance(raw, dict):
        logger.warning(
            'nose-picker: --file-durations=%s did not contain a JSON object; '
            'falling back to hash-based file selection.', path,
        )
        return None

    durations = {}
    for key, value in raw.items():
        try:
            durations[key] = float(value)
        except (TypeError, ValueError):
            continue
    return durations


def _bin_pack_files(candidate_keys, durations, total_processes, logger, stale_threshold=0.30):
    '''Greedy LPT bin-packing: sort candidates descending by known/estimated
    weight (ties broken by path for determinism), then repeatedly place the
    next-heaviest file into whichever bin currently has the smallest total.

    Files with no entry in `durations` are weighted with the median of all
    known durations, and a warning is logged if too large a fraction of the
    candidate set is unknown (a sign the durations table is stale).

    NOTE: this only balances at file granularity. A single very slow test
    file can never be split across bins, so no bin-packing scheme built on
    top of nose's per-file wantFile() hook can fully even out wall-clock time
    if one file dominates -- see the PR description / README for this ceiling.

    Returns (bin_files, bin_totals), or (None, None) if there is no usable
    timing signal at all for `candidate_keys` (durations table doesn't cover
    any of them, or every covered value is <= 0) -- see the guard below for
    why that specific case can't be bin-packed.
    '''
    known_values = [durations[k] for k in candidate_keys if k in durations]

    if not known_values or max(known_values) <= 0.0:
        # No usable timing signal at all for this candidate set -- every file
        # would get weight 0.0 (median of nothing, or median of all-zeros),
        # which collapses the whole LPT loop onto bin 0: adding 0.0 never
        # changes bin_totals, so bin_totals.index(min(bin_totals)) never
        # advances past index 0 and the entire suite lands on one shard,
        # actively worse than the hash-modulo behavior this is meant to
        # improve on. Signal "can't bin-pack" to the caller so it falls back
        # to classic hash-based selection instead.
        logger.warning(
            'nose-picker: no usable durations found among %d discovered test '
            'files; falling back to hash-based file selection.',
            len(candidate_keys),
        )
        return None, None

    fallback_weight = _median(known_values)
    missing = [k for k in candidate_keys if k not in durations]

    if candidate_keys and missing:
        stale_fraction = len(missing) / float(len(candidate_keys))
        if stale_fraction > stale_threshold:
            logger.warning(
                'nose-picker: %d/%d discovered test files (%.0f%%) have no entry '
                'in the --file-durations table and are being weighted with the '
                'median known duration (%.3fs); the durations cache may be stale.',
                len(missing), len(candidate_keys), stale_fraction * 100.0, fallback_weight,
            )

    weights = dict((key, durations.get(key, fallback_weight)) for key in candidate_keys)
    ordered = sorted(candidate_keys, key=lambda key: (-weights[key], key))

    bin_totals = [0.0] * total_processes
    bin_counts = [0] * total_processes
    bin_files = [[] for _ in range(total_processes)]
    for key in ordered:
        # Break ties on bin total by which bin currently holds the fewest files, not by raw bin
        # index. A plain `bin_totals.index(min(bin_totals))` looks safe but silently clumps any
        # run of *equal-weight* files onto a single bin whenever the increment doesn't strictly
        # grow past the tied group -- which is exactly what happens for weight 0.0 (adding 0.0
        # never changes a bin's total, so it stays tied-for-minimum forever, and `.index(...)`
        # always returns the same one). This isn't just the all-zero/no-signal case guarded above:
        # a perfectly healthy table can still legitimately have a handful of individual files at
        # exactly 0.0 (e.g. trivial/empty test files) mixed in with real positive durations, and
        # those would clump the same way without this. Tracking file counts per bin and using them
        # as the tie-break makes any tied group -- all-zero, partially-zero, or equal-nonzero --
        # round-robin across bins instead.
        target = min(range(total_processes), key=lambda i: (bin_totals[i], bin_counts[i]))
        bin_totals[target] += weights[key]
        bin_counts[target] += 1
        bin_files[target].append(key)

    return bin_files, bin_totals


class NosePicker(Plugin):
    name = 'nose-picker'

    def __init__(self, *args, **kwargs):
        self.output = True
        self.enableOpt = 'with-nose-picker'
        self.logger = logging.getLogger('nose.plugins.picker')
        self._assigned_files = None
        self._all_candidate_keys = frozenset()

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
        parser.add_option(
            '--file-durations',
            type='string',
            dest='file_durations',
            default=None,
            help=(
                'nose-picker: Optional path to a JSON file mapping relative test '
                'file paths (the same convention hash_filename() strips paths '
                'down to) to their most recent total run duration in seconds. '
                'When given a valid file, nose-picker bin-packs the full set of '
                'discovered test files across --total-processes bins by duration '
                '(greedy LPT) instead of hashing filenames. If this flag is '
                'omitted, or the file is missing/unreadable/invalid, behavior is '
                'unchanged from the classic hash-modulo selection.'
            ),
        )
        super(NosePicker, self).options(parser, env=env)

    def configure(self, options, config):
        self.enabled = getattr(options, self.enableOpt)
        self.total_processes = options.total_processes
        self.which_process = options.which_process
        self._assigned_files = None
        self._all_candidate_keys = frozenset()

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

        file_durations_path = getattr(options, 'file_durations', None)
        if self.enabled and file_durations_path:
            # Anything going wrong here -- a bad walk, an unreadable path, a
            # bug in the bin-packer -- must degrade to the classic hash
            # behavior, never take the whole test run down. This is the
            # backward-compat guarantee for everyone who hasn't opted in.
            try:
                self._configure_file_durations(file_durations_path)
            except Exception:
                self.logger.warning(
                    'nose-picker: unexpected error configuring '
                    '--file-durations=%s; falling back to hash-based file '
                    'selection.', file_durations_path, exc_info=True,
                )
                self._assigned_files = None
                self._all_candidate_keys = frozenset()

        super(NosePicker, self).configure(options, config)

    def _configure_file_durations(self, file_durations_path):
        durations = _load_durations_file(file_durations_path, self.logger)
        if durations is None:
            return

        candidate_paths = discover_candidate_files(os.getcwd())
        candidate_keys = [_relative_key(path) for path in candidate_paths]

        if not (0 <= self.which_process < self.total_processes):
            self.logger.warning(
                'nose-picker: which_process=%s is out of range for '
                'total_processes=%s; falling back to hash-based file '
                'selection.', self.which_process, self.total_processes,
            )
            return

        bins, totals = _bin_pack_files(
            candidate_keys, durations, self.total_processes, self.logger,
        )
        if bins is None:
            # No usable timing signal at all (see _bin_pack_files) -- it already
            # logged why. self._assigned_files is still None from configure(),
            # so _should_run() falls back to hash-based selection for this run.
            return
        self._assigned_files = frozenset(bins[self.which_process])
        self._all_candidate_keys = frozenset(candidate_keys)
        self.logger.info(
            'nose-picker: --file-durations bin-packing active; process %d/%d '
            'assigned %d of %d discovered files, totalling %.1fs (all bin '
            'totals: %s)',
            self.which_process, self.total_processes,
            len(bins[self.which_process]), len(candidate_keys),
            totals[self.which_process],
            ', '.join('%.1f' % total for total in totals),
        )

    def wantFile(self, fullpath):
        """
        Do we want to run this file?  See _should_run.
        """
        return self._should_run(fullpath)

    def _should_run(self, name):
        if self.enabled:
            if self._assigned_files is not None:
                key = _relative_key(name)
                if key in self._all_candidate_keys:
                    if key in self._assigned_files:
                        return None
                    return False
                # This file exists (nose is asking about it) but our own
                # upfront walk didn't enumerate it -- e.g. it was added after
                # configure() ran, or our reimplementation of nose's
                # discovery conventions missed a case. Fall back to the hash
                # for this one file so it still runs in exactly one shard
                # instead of silently vanishing from all of them.
                return self._hash_should_run(name)
            return self._hash_should_run(name)

        return None

    def _hash_should_run(self, name):
        hashed_value = hash_filename(name) % self.total_processes
        if hashed_value == self.which_process:
            return None
        return False
