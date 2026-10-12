# Publication validation, 2026-10-12

Exported offline image-builder host tests ran on Linux with the declared pinned
zstandard0.25.0 dependency:61 tests,60 passed,1 prerequisite-gated test skipped,
0 failures/errors. Traversal/symlink confinement, format/hash/size limits,
overlay roundtrip/refusals and regular-file packaging boundaries were exercised.
Initial Windows verification was incomplete (POSIX/symlink cases skipped and
zstandard missing); it is not the acceptance result. No image/rootfs download,
loop mount, filesystem format, device write or target run occurred in these tests.

The retained Alpha1 builder is historical source, not re-executed/qualified here.
The exported current-session mock uses a fresh task-owned temporary sandbox and
passed 32/32 checks on Linux: launch argv and parent/child environments,
fail-closed integrity checks, display preparation, install and rollback. No live
service or device operation is part of that test.
The current-session templates lack their closed/external deployment payload and
need a newly qualified assembled manifest. A new exact-current-baseline installable
image has not been built or hardware tested during publication.
