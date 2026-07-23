nose-picker
===========

**nose-picker** is a plugin that picks a subset of your unit tests (in django
too!)

This plugin modifies nose's unit test discovery to only pick a (1/N) subset of
unit tests to run. By passing in the ``--total-processes`` arguments, you pick
the denominator (the N above) which you want to run. The ``--which-process``
argument controls which part of that subset to run, so if you had 5 subsets you
could pick 0, 1, 2, 3, or 4.

How does it work? Very simple! It hashes the filenames that nose is
running through, does a modulo division by N, then sees if this file is "its".
Very simple, but it lets you run multiple of these **nose-picker** enabled
runners in parallel, each running a separate subset of the unit tests!

Optional: duration-aware bin-packing (``--file-durations``)
-------------------------------------------------------------

By default (and if you do nothing differently), **nose-picker** still just
hashes each candidate file's path and mods it by ``--total-processes`` --
completely unchanged from prior releases. If your files vary a lot in how
long they take to run, hashing can produce very unbalanced shards even
though each shard gets roughly the same *number* of files.

As of 0.6.0, you can opt in to duration-aware bin-packing instead::

    --file-durations=/path/to/durations.json

where ``durations.json`` looks like::

    {
        "/ebapps/foo/tests/test_bar.py": 12.34,
        "/common/tests/test_baz.py": 0.87
    }

(keys are relative paths using the same "strip the cwd/site-packages prefix"
convention ``hash_filename()`` has always used).

When given a valid file, nose-picker does **not** try to rediscover your test
files itself. Instead, in nose's plugin ``configure()`` step (before test
collection starts), it bin-packs the durations table's own set of files --
real ground truth captured from an actual prior nose run's output, not a
guess -- across ``--total-processes`` bins by descending duration (longest
processing time first), so each shard ends up with roughly the same *total*
runtime instead of roughly the same file count. Nose's own real
discovery keeps driving everything else exactly as before: it still calls
this plugin's ``wantFile()`` hook once per file, one at a time, as its own
``Loader``/``Selector`` finds each one. For a file the durations table
already covers, ``wantFile()`` becomes a simple membership check against the
precomputed assignment; for a file the table *doesn't* cover (new since the
table was last refreshed, or any other reason), that one file falls back to
the classic hash so it still runs in exactly one shard rather than
vanishing from all of them. Files with no entry in the table are weighted
with the median of all known durations at bin-packing time (if there aren't
enough of them to make bin-packing meaningless -- see below), and a
staleness warning is logged (via the ``nose.plugins.picker`` logger, at the
end of the run, from nose's standard ``report()`` plugin hook) if too large
a fraction of the files actually visited this run fell back to the hash
individually, since that's a sign the table has gone stale.

An earlier version of this feature had nose-picker walk the filesystem
itself to build a candidate list, reimplementing nose's own default
discovery convention (``testMatch``/``ignoreFiles``/``srcDirs``/package
detection). That was dropped in favor of the design above: any mismatch
between a hand-rolled reimplementation and nose's *actual* discovery
behavior (custom ``--match``/``--include``/``--exclude`` regexes, other
plugins' directory exclusions, subtle edge cases) risked a file nose really
does visit never appearing in *any* shard's assigned set -- a correctness
bug, not just a balance one. Deriving the candidate set from the durations
table itself, and leaving 100% of real discovery to nose as it's always
worked, removes that entire risk category.

**Backward compatibility guarantee**: if ``--file-durations`` is not passed,
or the given path doesn't exist, can't be read, or doesn't parse as JSON,
behavior is *exactly* the classic hash-modulo selection, unchanged. This is
deliberate and load-bearing: nose-picker has other consumers besides the one
that motivated this feature, and none of them should see any behavior change
unless they explicitly pass the new flag.

**Known ceiling**: bin-packing here only ever operates at whole-file
granularity, because nose only ever asks this plugin "do you want this
file?" one file at a time -- there's no hook to split a single file's tests
across shards. If one test file alone takes far longer to run than an even
share of the total suite, no bin-packing scheme built on this hook can even
out wall-clock time; that file's own duration becomes a floor for whatever
shard it lands in. Bin-packing helps a lot when slowness is spread across
many files, much less when it's concentrated in one.

Motivation
----------

The nose multiprocess plugin takes over the test runner when it runs, and thus
is not amenable to environments where you need a custom test runner.
**nose-picker** lets you keep your test runner!

Installing
----------

Through ``pip``::

    pip install --user nose-picker

Sample Multiprocess Script
--------------------------

Something like::

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

License
-------

**nose-picker** is copyright 2014 Eventbrite and Contributors, and is made
available under BSD-style license; see LICENSE for details.
