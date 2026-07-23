from __future__ import absolute_import

import json
import logging
import os
import random
import shutil
import tempfile
import unittest

from picker.nose_plugin import (
    NosePicker,
    _bin_pack_files,
    _load_durations_file,
    _looks_testlike,
    _median,
    _relative_key,
    hash_filename,
)


class _FakeOptions(object):
    """Minimal stand-in for optparse.Values, good enough to drive
    NosePicker.configure() the way nose's own OptionParser would.
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, item):
        return None


class _FakeConfig(object):
    pass


def _make_options(which_process, total_processes, file_durations=None):
    return _FakeOptions(**{
        'with-nose-picker': True,  # NosePicker.enableOpt is 'with-nose-picker', dash not underscore
        'which_process': which_process,
        'total_processes': total_processes,
        'futz_with_django': False,
        'file_durations': file_durations,
    })


class _ListHandler(logging.Handler):
    """Captures emitted records' messages so tests can assert on logger.warning() calls
    without depending on stderr/stdout capture.
    """

    def __init__(self):
        logging.Handler.__init__(self)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class LooksTestlikeTest(unittest.TestCase):
    """_looks_testlike is a best-effort, reporting-only heuristic (see its module-level comment)
    -- it must never be used for selection, only to keep report()'s staleness metric from being
    swamped by the ordinary non-test .py files nose also calls wantFile() for.
    """

    def test_testlike_names(self):
        for name in ('test_foo.py', 'foo_test.py', 'Test_Foo.py', 'tests.py'):
            self.assertTrue(_looks_testlike(name), name)

    def test_non_testlike_names(self):
        for name in ('helpers.py', '__init__.py', 'utils.py', 'models.py', 'views.py', 'contest.py'):
            self.assertFalse(_looks_testlike(name), name)


class MedianTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_median([]), 0.0)

    def test_single(self):
        self.assertEqual(_median([5]), 5.0)

    def test_odd(self):
        self.assertEqual(_median([3, 1, 2]), 2.0)

    def test_even(self):
        self.assertEqual(_median([4, 1, 3, 2]), 2.5)


class LoadDurationsFileTest(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger('test.nose_picker')

    def test_none_path(self):
        self.assertIsNone(_load_durations_file(None, self.logger))

    def test_missing_path(self):
        self.assertIsNone(_load_durations_file('/no/such/file.json', self.logger))

    def test_invalid_json(self):
        fh = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        fh.write(b'{not valid json')
        fh.close()
        try:
            self.assertIsNone(_load_durations_file(fh.name, self.logger))
        finally:
            os.unlink(fh.name)

    def test_non_object_json(self):
        fh = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        fh.write(b'[1, 2, 3]')
        fh.close()
        try:
            self.assertIsNone(_load_durations_file(fh.name, self.logger))
        finally:
            os.unlink(fh.name)

    def test_valid_json(self):
        fh = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        fh.write(json.dumps({'/a/test_foo.py': 12.5}).encode('utf-8'))
        fh.close()
        try:
            durations = _load_durations_file(fh.name, self.logger)
            self.assertEqual(durations, {'/a/test_foo.py': 12.5})
        finally:
            os.unlink(fh.name)

    def test_nan_and_infinity_values_are_dropped(self):
        # Regression test: Python's json module accepts bare NaN/Infinity/-Infinity literals by
        # default (a non-standard extension) -- a corrupt or hand-edited durations file can
        # produce these even though float(value) "succeeds". A NaN weight breaks every comparison
        # downstream (NaN is never <, >, or == anything, including itself), which can silently
        # collapse the whole LPT bin-packing loop onto a single shard. These must be dropped at
        # ingestion, same as any other value that fails to parse as a float.
        fh = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        fh.write(b'{"/nan.py": NaN, "/inf.py": Infinity, "/neg_inf.py": -Infinity, "/ok.py": 5.0}')
        fh.close()
        try:
            durations = _load_durations_file(fh.name, self.logger)
            self.assertEqual(durations, {'/ok.py': 5.0})
        finally:
            os.unlink(fh.name)

    def test_negative_values_are_dropped(self):
        # Regression test: a negative duration is never physically meaningful (nothing stops a
        # corrupt file or a buggy upstream aggregator from producing one), and is dangerous in a
        # different way than NaN/Infinity: it *lowers* whichever bin it's added to during
        # bin-packing, so the LPT loop keeps preferring that artificially-cheap bin for every
        # later placement and piles most of the suite onto it. Must be dropped at ingestion, same
        # as NaN/Infinity and any other invalid value. Zero is fine (legitimately measured for a
        # trivial/empty test file).
        fh = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        fh.write(json.dumps({'/negative.py': -5.0, '/zero.py': 0.0, '/ok.py': 5.0}).encode('utf-8'))
        fh.close()
        try:
            durations = _load_durations_file(fh.name, self.logger)
            self.assertEqual(durations, {'/zero.py': 0.0, '/ok.py': 5.0})
        finally:
            os.unlink(fh.name)


class BinPackFilesTest(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger('test.nose_picker')

    def test_balances_known_weights(self):
        durations = {'/a.py': 10.0, '/b.py': 1.0, '/c.py': 1.0, '/d.py': 1.0}
        keys = list(durations.keys())
        bins, totals = _bin_pack_files(keys, durations, 2, self.logger)
        # the one big file should be alone in a bin, balanced against the 3 small ones
        self.assertEqual(sorted(sum(bins, [])), sorted(keys))
        self.assertAlmostEqual(max(totals) - min(totals), 7.0)

    def test_unknown_files_get_median_weight(self):
        durations = {'/a.py': 10.0, '/b.py': 20.0}
        keys = ['/a.py', '/b.py', '/unknown.py']
        bins, totals = _bin_pack_files(keys, durations, 3, self.logger)
        assigned = sum(bins, [])
        self.assertEqual(sorted(assigned), sorted(keys))

    def test_deterministic_tie_break_by_path(self):
        # One known value so every file (including the two unknowns, which fall back to the
        # median of known values) ties at the same weight -- exercises path tie-breaking without
        # tripping the all-unknown collapse guard covered separately below.
        durations = {'/z.py': 5.0}
        keys = ['/z.py', '/a.py', '/m.py']
        bins1, _ = _bin_pack_files(keys, durations, 3, self.logger)
        bins2, _ = _bin_pack_files(list(reversed(keys)), durations, 3, self.logger)
        self.assertEqual(bins1, bins2)

    def test_no_usable_durations_signals_fallback_instead_of_collapsing(self):
        # Regression test: previously, when no candidate file had a known duration (or all known
        # durations were <= 0), every file got weight 0.0, and the LPT loop's
        # `bin_totals.index(min(bin_totals))` never advanced past index 0 (adding 0.0 never
        # changes bin_totals) -- the entire suite silently collapsed onto a single shard, with
        # every other shard getting nothing. _bin_pack_files must now signal "can't bin-pack" by
        # returning (None, None) instead, so the caller falls back to hash-based selection.
        keys = ['/a.py', '/b.py', '/c.py']

        bins, totals = _bin_pack_files(keys, {}, 3, self.logger)
        self.assertIsNone(bins)
        self.assertIsNone(totals)

        all_zero_durations = dict((key, 0.0) for key in keys)
        bins, totals = _bin_pack_files(keys, all_zero_durations, 3, self.logger)
        self.assertIsNone(bins)
        self.assertIsNone(totals)

    def test_partial_zero_weight_files_round_robin_instead_of_clumping(self):
        # Narrower, separate regression case from the total-collapse one above: a perfectly
        # healthy table (real positive durations exist, so the bail-out guard doesn't fire) can
        # still legitimately have a handful of individual files measured at exactly 0.0 (trivial
        # or empty test files). LPT sorts heaviest-first, so all the 0.0-weight files end up
        # adjacent at the tail of `ordered`; a naive `bin_totals.index(min(bin_totals))` tie-break
        # would clump every one of them onto whichever single bin happened to be minimum when the
        # zero-weight run started, since adding 0.0 never changes that bin's total. They must
        # instead round-robin, by file count, across whichever bins are currently tied-minimum.
        zero_weight_files = ['/zero_%d.py' % i for i in range(6)]
        durations = dict((f, 0.0) for f in zero_weight_files)
        durations['/big.py'] = 100.0
        keys = ['/big.py'] + zero_weight_files
        bins, totals = _bin_pack_files(keys, durations, 3, self.logger)

        self.assertEqual(sorted(sum(bins, [])), sorted(keys))
        zero_weight_bin_sizes = sorted(
            sum(1 for f in b if f in zero_weight_files) for b in bins
        )
        # /big.py's bin is left at total=100 after the first placement, so it's correctly never
        # tied-minimum again -- the 6 zero-weight files round-robin evenly across the *other* two
        # bins only (3/3), rather than all 6 clumping onto whichever single bin the old
        # `bin_totals.index(min(...))` tie-break happened to pick first (the bug this regression
        # test covers would have produced something like [0, 0, 6] here).
        self.assertEqual(zero_weight_bin_sizes, [0, 3, 3])

    def test_nan_weight_does_not_collapse_bins(self):
        # Regression test: _bin_pack_files defensively re-sanitizes its own `durations` input
        # (not just relying on _load_durations_file() having already done so), since a NaN value
        # anywhere in it breaks ordinary float comparisons (NaN is never <, >, or == anything,
        # including itself) badly enough to silently collapse the whole LPT loop onto one shard.
        # A NaN entry must be treated exactly like a missing one -- falls back to the median.
        durations = {
            '/a.py': 10.0, '/b.py': 20.0, '/c.py': 30.0,
            '/nan.py': float('nan'),
        }
        keys = list(durations.keys())
        bins, totals = _bin_pack_files(keys, durations, 3, self.logger)
        self.assertIsNotNone(bins)
        self.assertEqual(sorted(sum(bins, [])), sorted(keys))
        # every bin actually got at least one file -- the collapse bug this guards against would
        # have dumped everything onto a single bin instead.
        self.assertTrue(all(b for b in bins), bins)

    def test_all_nan_durations_signals_fallback(self):
        keys = ['/a.py', '/b.py']
        durations = dict((k, float('nan')) for k in keys)
        bins, totals = _bin_pack_files(keys, durations, 2, self.logger)
        self.assertIsNone(bins)
        self.assertIsNone(totals)

    def test_negative_weight_does_not_skew_bins(self):
        # Regression test: a negative duration artificially lowers whichever bin it's placed
        # into, making the LPT loop keep preferring that bin for every later placement and piling
        # most of the suite onto it -- unlike NaN, this doesn't collapse everything onto one bin
        # via broken comparisons, it just badly imbalances the result. _bin_pack_files must treat
        # a negative entry exactly like a missing one (falls back to the median), same defensive
        # re-sanitization as the NaN case above.
        durations = {
            '/a.py': 10.0, '/b.py': 10.0, '/c.py': 10.0, '/d.py': 10.0,
            '/negative.py': -1000.0,
        }
        keys = list(durations.keys())
        bins, totals = _bin_pack_files(keys, durations, 2, self.logger)
        self.assertIsNotNone(bins)
        self.assertEqual(sorted(sum(bins, [])), sorted(keys))
        # A poisoned negative weight would otherwise make one bin's total go deeply negative and
        # never lose the "cheapest bin" comparison again, piling everything else onto it -- assert
        # the two bins are still reasonably balanced instead.
        self.assertLess(abs(totals[0] - totals[1]), 15.0)


class HashFilenameBackwardCompatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.tmp)

    def test_stable_across_relative_and_absolute(self):
        path = os.path.join(self.tmp, 'test_foo.py')
        open(path, 'w').close()
        self.assertEqual(hash_filename(path), hash_filename('test_foo.py'))


class FullShardCoverageTest(unittest.TestCase):
    """End-to-end style checks: across every shard 0..N-1, every file nose's real walk visits
    this run must be claimed by exactly one shard -- in legacy hash mode, in duration mode, and
    in every fallback path duration mode can take.

    nose-picker does not do its own filesystem walk (see the module docstring in
    nose_plugin.py for why): the candidate set for bin-packing is just the --file-durations
    table's own keys, and nose's real Loader/Selector keeps calling wantFile() one file at a
    time exactly as it always did. So "files nose visits this run" here is simply a fixed list
    of paths fed directly into _should_run(), one at a time, mirroring that -- there's no
    filesystem convention to fake, and the files don't even need to exist on disk (this module's
    path handling is pure os.path.realpath()-based string manipulation).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.files_this_run = [
            os.path.join(self.tmp, 'pkg', 'test_foo.py'),
            os.path.join(self.tmp, 'pkg', 'bar_test.py'),
            os.path.join(self.tmp, 'pkg', 'sub', 'test_deep.py'),
            os.path.join(self.tmp, 'lib', 'test_in_lib.py'),
        ]
        self.expected_keys = sorted(_relative_key(f, cwd=self.tmp) for f in self.files_this_run)

    def tearDown(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.tmp)

    def _run_all_shards(self, total_processes, file_durations=None):
        assignment = {}
        for which in range(total_processes):
            plugin = NosePicker()
            plugin.configure(
                _make_options(which, total_processes, file_durations),
                _FakeConfig(),
            )
            for full_path in self.files_this_run:
                key = _relative_key(full_path, cwd=self.tmp)
                wanted = plugin._should_run(full_path) is None
                if wanted:
                    assignment.setdefault(key, []).append(which)
        return assignment

    def _assert_bijection(self, assignment, expected_keys=None):
        expected_keys = self.expected_keys if expected_keys is None else expected_keys
        self.assertEqual(sorted(assignment.keys()), sorted(expected_keys))
        for key, shards in assignment.items():
            self.assertEqual(len(shards), 1, 'file %s claimed by %r' % (key, shards))

    def test_legacy_hash_mode(self):
        assignment = self._run_all_shards(3, file_durations=None)
        self._assert_bijection(assignment)

    def test_duration_mode_fully_known(self):
        durations = dict((k, random.uniform(1, 100)) for k in self.expected_keys)
        path = os.path.join(self.tmp, 'durations.json')
        with open(path, 'w') as fh:
            json.dump(durations, fh)
        assignment = self._run_all_shards(3, file_durations=path)
        self._assert_bijection(assignment)

    def test_duration_mode_partially_known_still_covers_everything(self):
        # Half the files this run visits are in the durations table (bin-packed), half are new
        # (not in the table, e.g. added since it was last refreshed) and fall back to the hash
        # individually. Every file must still land in exactly one shard either way.
        known_keys = self.expected_keys[:2]
        durations = dict((k, random.uniform(1, 100)) for k in known_keys)
        path = os.path.join(self.tmp, 'partial.json')
        with open(path, 'w') as fh:
            json.dump(durations, fh)
        assignment = self._run_all_shards(3, file_durations=path)
        self._assert_bijection(assignment)

    def test_duration_mode_wholly_unmatched_table_falls_back_identically_to_legacy(self):
        # A durations table that doesn't overlap the files visited this run at all (e.g. it only
        # knows about files that no longer exist) means _should_run() treats every file visited
        # this run as "unknown" and falls back to the hash for each -- byte-for-byte the same as
        # legacy mode, not some degraded in-between state.
        path = os.path.join(self.tmp, 'wholly_unmatched.json')
        with open(path, 'w') as fh:
            json.dump({'/this/file/does/not/exist.py': 123.0}, fh)
        legacy = self._run_all_shards(3, file_durations=None)
        fallback = self._run_all_shards(3, file_durations=path)
        self._assert_bijection(fallback)
        self.assertEqual(legacy, fallback)

    def test_duration_mode_all_zero_durations_falls_back_identically_to_legacy(self):
        path = os.path.join(self.tmp, 'all_zero.json')
        with open(path, 'w') as fh:
            json.dump(dict((k, 0.0) for k in self.expected_keys), fh)
        legacy = self._run_all_shards(3, file_durations=None)
        fallback = self._run_all_shards(3, file_durations=path)
        self._assert_bijection(fallback)
        self.assertEqual(legacy, fallback)

    def test_missing_durations_file_falls_back_identically_to_legacy(self):
        legacy = self._run_all_shards(3, file_durations=None)
        fallback = self._run_all_shards(3, file_durations='/no/such/file.json')
        self.assertEqual(legacy, fallback)

    def test_invalid_json_falls_back_identically_to_legacy(self):
        path = os.path.join(self.tmp, 'bad.json')
        with open(path, 'w') as fh:
            fh.write('{not valid json')
        legacy = self._run_all_shards(3, file_durations=None)
        fallback = self._run_all_shards(3, file_durations=path)
        self.assertEqual(legacy, fallback)


class ReportStalenessWarningTest(unittest.TestCase):
    """The durations table is no longer compared against an upfront full candidate list (there
    isn't one -- see module docstring), so staleness can only be observed live, as nose's real
    walk visits each file one at a time. report() -- nose's standard end-of-run plugin hook --
    logs a one-time summary warning if too large a fraction of *this run's actually-visited*
    files fell back to hash-based selection individually.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.logger_handler = _ListHandler()
        self.plugin = NosePicker()
        self.plugin.logger.addHandler(self.logger_handler)
        self.plugin.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self.plugin.logger.removeHandler(self.logger_handler)
        os.chdir(self.old_cwd)
        shutil.rmtree(self.tmp)

    def _visit(self, *relnames):
        for relname in relnames:
            self.plugin._should_run(os.path.join(self.tmp, relname))

    def test_mostly_unknown_files_this_run_warns_on_report(self):
        durations_path = os.path.join(self.tmp, 'durations.json')
        with open(durations_path, 'w') as fh:
            json.dump({_relative_key(os.path.join(self.tmp, 'test_known.py'), cwd=self.tmp): 5.0}, fh)
        self.plugin.configure(_make_options(0, 2, durations_path), _FakeConfig())

        self._visit('test_known.py', 'test_new_a.py', 'test_new_b.py', 'test_new_c.py')
        self.logger_handler.messages = []  # only care about what report() itself logs
        self.plugin.report(None)

        self.assertTrue(
            any('durations cache may be stale' in msg for msg in self.logger_handler.messages),
            self.logger_handler.messages,
        )

    def test_mostly_known_files_this_run_does_not_warn_on_report(self):
        known_names = ['test_a.py', 'test_b.py', 'test_c.py', 'test_d.py']
        durations = dict(
            (_relative_key(os.path.join(self.tmp, name), cwd=self.tmp), 5.0)
            for name in known_names
        )
        durations_path = os.path.join(self.tmp, 'durations.json')
        with open(durations_path, 'w') as fh:
            json.dump(durations, fh)
        self.plugin.configure(_make_options(0, 2, durations_path), _FakeConfig())

        self._visit(*known_names)
        self.logger_handler.messages = []
        self.plugin.report(None)

        self.assertFalse(
            any('durations cache may be stale' in msg for msg in self.logger_handler.messages),
            self.logger_handler.messages,
        )

    def test_legacy_hash_mode_never_warns_on_report(self):
        self.plugin.configure(_make_options(0, 2, None), _FakeConfig())
        self._visit('test_a.py', 'test_b.py')
        self.logger_handler.messages = []
        self.plugin.report(None)
        self.assertEqual(self.logger_handler.messages, [])

    def test_non_testlike_files_do_not_inflate_staleness_metric(self):
        # Regression test: nose's Selector.wantFile calls every plugin's wantFile() hook for
        # *every* non-ignored .py file it walks, not just test-like ones (testMatch only decides
        # nose's own default answer once every plugin has abstained). A flood of ordinary,
        # non-test source files -- none in the durations table, since that table only ever
        # contains real test-file keys -- must not count toward the unknown-hit staleness metric,
        # or the warning would fire constantly regardless of whether the table is actually fresh.
        durations_path = os.path.join(self.tmp, 'durations.json')
        with open(durations_path, 'w') as fh:
            json.dump({_relative_key(os.path.join(self.tmp, 'test_known.py'), cwd=self.tmp): 5.0}, fh)
        self.plugin.configure(_make_options(0, 2, durations_path), _FakeConfig())

        self._visit(
            'test_known.py',
            # a "flood" of ordinary source files nose also visits via wantFile(), none of which
            # look test-like and none of which are (or ever would be) in the durations table
            'helpers.py', '__init__.py', 'utils.py', 'models.py', 'views.py', 'serializers.py',
            'urls.py', 'admin.py', 'forms.py', 'managers.py',
        )
        self.logger_handler.messages = []
        self.plugin.report(None)

        self.assertFalse(
            any('durations cache may be stale' in msg for msg in self.logger_handler.messages),
            self.logger_handler.messages,
        )


if __name__ == '__main__':
    unittest.main()
