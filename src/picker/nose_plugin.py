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
import site

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
# Duration-aware bin-packing (LPT: longest processing time first).
#
# Design note: nose-picker deliberately does NOT do its own filesystem walk
# to build a candidate-file list. An earlier version of this feature did --
# reimplementing nose's default discovery convention (testMatch/ignoreFiles/
# srcDirs/package detection) well enough to build a complete file list up
# front, since nose's own wantFile() plugin hook only ever hands files over
# one at a time, as nose's own real walk discovers them. That reimplementation
# risked silently diverging from nose's actual behavior (custom --match/
# --include/--exclude regexes, other plugins' directory exclusions, subtle
# edge cases) -- a mismatch there means some file nose really does visit
# never appearing in *any* shard's assigned set, which is a correctness bug,
# not just a balance one.
#
# Instead: nose's own real discovery keeps driving everything exactly as it
# always has (wantFile() is still called once per file, one at a time, as
# nose's own Loader/Selector finds it -- nose-picker never walks anything
# itself). The candidate set used for bin-packing is simply the key set of
# the --file-durations table itself: real ground truth captured from an
# actual prior nose run's junit output (see eventbrite/core's CI wiring),
# not a guess about what nose would discover. Bin-packing happens once, in
# configure(), over that known key set. As nose's real walk then calls
# wantFile() file by file, _should_run() looks the file up in the
# precomputed assignment; a file nose visits that ISN'T in that known set
# (new since the durations table was last refreshed, or the table doesn't
# cover it for any other reason) falls back to the classic hash for that one
# file -- the same safety net as the "unknown file" case, but now the
# *expected*, routine path for new files rather than a reimplementation-bug
# escape hatch.
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
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if not _is_finite(parsed):
            # Python's json module accepts bare NaN/Infinity/-Infinity literals by default (a
            # non-standard extension), so a corrupt or hand-edited durations file can produce one
            # of these here even though float(value) "succeeded". A NaN weight breaks every
            # comparison downstream (NaN is never <, >, or == anything, including itself) --
            # _bin_pack_files' own bail-out guard and its LPT tie-break both rely on ordinary
            # float comparisons behaving normally, so silently admitting a NaN/Infinity here would
            # reintroduce a single-shard collapse through the back door. Treat it exactly like any
            # other value that failed to parse: drop the entry, fall back to the median for that
            # key at bin-packing time.
            continue
        durations[key] = parsed
    return durations


def _is_finite(value):
    '''No math.isfinite on Python 2.7 (added in Python 3.2). NaN is the only float that doesn't
    equal itself; +-inf are the only floats equal to float('inf')/float('-inf').
    '''
    return value == value and value not in (float('inf'), float('-inf'))


def _bin_pack_files(candidate_keys, durations, total_processes, logger, stale_threshold=0.30):
    '''Greedy LPT bin-packing: sort candidates descending by known/estimated
    weight (ties broken by path for determinism), then repeatedly place the
    next-heaviest file into whichever bin currently has the smallest total.

    Files with no entry in `durations` are weighted with the median of all
    known durations, and a warning is logged if too large a fraction of the
    candidate set is unknown (a sign the durations table is stale). In
    practice `candidate_keys` is normally exactly `durations.keys()` (see the
    module docstring above), so this path rarely triggers today -- it's kept
    general because it's also independently unit-tested, and because a
    caller passing a candidate set that only partially overlaps `durations`
    is a perfectly reasonable thing to support.

    NOTE: this only balances at file granularity. A single very slow test
    file can never be split across bins, so no bin-packing scheme built on
    top of nose's per-file wantFile() hook can fully even out wall-clock time
    if one file dominates -- see the PR description / README for this ceiling.

    Returns (bin_files, bin_totals), or (None, None) if there is no usable
    timing signal at all for `candidate_keys` (durations table doesn't cover
    any of them, or every covered value is <= 0) -- see the guard below for
    why that specific case can't be bin-packed.
    '''
    # Defensively re-sanitize here too, even though _load_durations_file() already filters
    # non-finite values on the normal production path: this function is independently
    # unit-tested and could be called with a hand-built `durations` dict from anywhere. A NaN
    # weight is uniquely dangerous because NaN is never <, >, or == anything (including itself),
    # which breaks both the bail-out guard below (`max()` over a list containing NaN is
    # order-dependent and can silently return NaN, making `NaN <= 0.0` evaluate False) and the
    # LPT tie-break's bin_totals comparisons (a bin total poisoned by NaN can never again compare
    # as strictly less than another bin, effectively freezing every remaining candidate onto a
    # single bin) -- treating a non-finite entry as though the key were simply absent from
    # `durations` (falls back to the median, like any other missing/unparseable value) closes
    # both of those off at the source instead of trying to special-case NaN/Infinity downstream.
    durations = dict((k, v) for k, v in durations.items() if _is_finite(v))
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
            'nose-picker: no usable durations found among %d candidate test '
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
                'nose-picker: %d/%d candidate test files (%.0f%%) have no entry '
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
        self._known_keys = frozenset()
        self._known_hits = 0
        self._unknown_hits = 0

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
                'When given a valid file, nose-picker bin-packs that file set '
                'across --total-processes bins by duration (greedy LPT) instead '
                'of hashing filenames; files nose visits that this run\'s table '
                'doesn\'t cover still fall back to the hash individually. If this '
                'flag is omitted, or the file is missing/unreadable/invalid, '
                'behavior is unchanged from the classic hash-modulo selection.'
            ),
        )
        super(NosePicker, self).options(parser, env=env)

    def configure(self, options, config):
        self.enabled = getattr(options, self.enableOpt)
        self.total_processes = options.total_processes
        self.which_process = options.which_process
        self._assigned_files = None
        self._known_keys = frozenset()
        self._known_hits = 0
        self._unknown_hits = 0

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
            # Anything going wrong here -- an unreadable path, a bug in the
            # bin-packer -- must degrade to the classic hash behavior, never
            # take the whole test run down. This is the backward-compat
            # guarantee for everyone who hasn't opted in.
            try:
                self._configure_file_durations(file_durations_path)
            except Exception:
                self.logger.warning(
                    'nose-picker: unexpected error configuring '
                    '--file-durations=%s; falling back to hash-based file '
                    'selection.', file_durations_path, exc_info=True,
                )
                self._assigned_files = None
                self._known_keys = frozenset()

        super(NosePicker, self).configure(options, config)

    def _configure_file_durations(self, file_durations_path):
        durations = _load_durations_file(file_durations_path, self.logger)
        if durations is None:
            return

        # The candidate set is exactly the durations table's own keys -- real ground truth from
        # an actual prior nose run (see the module docstring above for why nose-picker doesn't do
        # its own filesystem walk to build this list instead).
        candidate_keys = sorted(durations.keys())

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
        self._known_keys = frozenset(candidate_keys)
        self.logger.info(
            'nose-picker: --file-durations bin-packing active; process %d/%d '
            'assigned %d of %d known files, totalling %.1fs (all bin '
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
                if key in self._known_keys:
                    self._known_hits += 1
                    if key in self._assigned_files:
                        return None
                    return False
                # nose is visiting a file this run's durations table doesn't know about (new
                # since the table was last refreshed, or the table doesn't cover it for any
                # other reason). Fall back to the hash for this one file so it still runs in
                # exactly one shard rather than vanishing from all of them.
                self._unknown_hits += 1
                return self._hash_should_run(name)
            return self._hash_should_run(name)

        return None

    def _hash_should_run(self, name):
        hashed_value = hash_filename(name) % self.total_processes
        if hashed_value == self.which_process:
            return None
        return False

    def report(self, stream):
        """Log a one-time staleness summary once collection is complete, since (unlike an
        earlier version of this feature) nose-picker no longer has an upfront full candidate
        list to compare the durations table against -- it only learns "known" vs "unknown" as
        nose's own real walk visits each file live. See module docstring for why.
        """
        total_hits = self._known_hits + self._unknown_hits
        if self._assigned_files is not None and total_hits and self._unknown_hits:
            unknown_fraction = self._unknown_hits / float(total_hits)
            if unknown_fraction > 0.30:
                self.logger.warning(
                    'nose-picker: %d/%d files nose visited this run (%.0f%%) had no entry '
                    'in the --file-durations table and fell back to hash-based selection '
                    'individually; the durations cache may be stale.',
                    self._unknown_hits, total_hits, unknown_fraction * 100.0,
                )
        return None
