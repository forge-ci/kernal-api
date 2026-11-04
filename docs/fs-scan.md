# Native disk scan implementation

The `fs-scan` facade moves the scanner core from forge-ci/jettison PR #1.
The native mechanisms below preserve its allocation, deduplication, fallback
and live-filesystem semantics. No new third-party dependencies are added.
The facade is independent of `fs` and its dirs/globset/jwalk/reflink-copy/tempfile graph.

## Linux: implement and measure on this host

Read raw getdents64 into one reusable 128 KiB worker buffer. Validate record
lengths and NUL terminators before borrowing names. Use raw SYS_statx once per entry (no glibc 2.28 symbol dependency),
relative to its open parent, with AT_SYMLINK_NOFOLLOW, AT_STATX_DONT_SYNC and
TYPE/SIZE/BLOCKS/INO/MNT_ID masks. ENOSYS falls back to fstatat. Blocks are 512
bytes independently of filesystem block size. openat O_NOFOLLOW|O_DIRECTORY
keeps traversal fd-relative and protects default symlink semantics. Opened
directory identity is checked against discovery metadata to reject replacements.
Names are copied only when emitted or queued; metadata never needs a full path.

The raw statx buffer uses the fixed 256-byte
[Linux UAPI layout](https://github.com/torvalds/linux/blob/master/include/uapi/linux/stat.h)
inside the Linux platform module. libc 0.2.186 does not expose statx and its masks
for musl targets. Layout/offset assertions and an actual syscall comparison with
fstatat cover the shared ABI without changing either libc's structs or the public
API. Both GNU and musl retain the same raw syscall and getdents64 fast path.

N workers use mutex-protected local deques of owned directory descriptors, pop
local back and steal remote front. An outstanding-work counter and condition
variable terminate idle workers without polling. Default min(CPU count, 8);
benchmark multiple thread counts before claiming that default is optimal. Deque tasks retain owned descriptors and root-relative paths. A shared receiver drains bounded entry batches;
callbacks run on the caller thread, never require Send, and can cancel workers.
A limit on queued descriptors forces local depth-first work when queues grow.
The facade reports opaque parent identities independent of completion order;
clients such as jettison retain ownership of directory aggregation.
One-filesystem compares mount IDs if returned, otherwise device IDs. Default
pseudo-filesystem exclusions are exact /proc, /sys and /dev prefixes on devices
other than the root device; explicit roots remain scannable.

## Windows: raw NTFS fast path and conservative fallbacks

Open the volume device with sharing read/write/delete; administrator privileges
are ordinarily needed for raw volume access. Query NTFS volume data, get the
first MFT record, apply update-sequence fixups, and decode the nonresident $MFT
DATA runlist. Read mapped extents sequentially in MiB chunks with buffered I/O;
no unmeasured claim about unbuffered/overlapped speed. Parse FILE records with
bounds checks, fixups, in-use/sequence flags, FILE_NAME namespaces and DATA
allocated/logical sizes. Build a file-ID parent map, resolving only selected
paths. Reserved NTFS metadata records (slots 0–15, except root 5) and their descendants
have separate accounting outside totals. User dollar names such as $RECYCLE.BIN
remain visible. Root FILE_NAME "." is accepted only for record 5.
ATTRIBUTE_LIST, reparse attributes, named DATA streams and unresolved/malformed extents force a correctness fallback,
never silently return incomplete totals. Resident DATA uses its value length;
compressed/sparse nonresident DATA uses its physical allocation field.

Try USN enumeration after raw access failure; this still requires a usable volume
handle and is not promised to work without administrator rights. USN gives names
and parents, not trustworthy byte sizes, so obtain metadata before accounting.
Then use FindFirstFileExW FindExInfoBasic + LARGE_FETCH (retry without the flag
when unsupported) and per-file handle metadata for identity/allocation. A plain
standard-library walk is the final compatibility path. Volume-only MFT traversal
is eligible only for a whole-volume root with no followed links. All optimized
paths fail closed to walking if completeness cannot be established.

Both serial and parallel walkers use the same handle metadata source: the volume
serial and high/low file index from BY_HANDLE_FILE_INFORMATION. Only multiply
linked files require hardlink deduplication when links are not followed. Sparse
and compressed files query FILE_COMPRESSION_INFO.CompressedFileSize for physical
allocation; FILE_STANDARD_INFO.AllocationSize describes stream allocation and
must not substitute for that value. A failed physical-size query remains a
visible error. Regular files and directories retain standard allocation.
See [Microsoft's sparse-size guidance](https://learn.microsoft.com/en-us/windows/win32/fileio/obtaining-the-size-of-a-sparse-file)
and [physical allocation semantics](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/484bc722-6440-49a0-982a-9ba27cddf513).

Extended drive/UNC paths and FindFirstFileExW names retain UTF-16 code units,
including unpaired surrogates, through OsString::from_wide. No scanner path uses
lossy string conversion. Host-runnable tests exercise path prefix/root decisions,
allocation selection, dot-entry filtering and terminated enumeration records;
native tests check sparse allocation against GetCompressedFileSizeW and preserve
an unpaired-surrogate filename through serial and parallel scans.

The Windows equivalence failure was localized natively to the sparse fixture:
its physical allocation changed from 0 to 65536 between live scans while logical
size, entries and errors stayed identical. Pending cached writes had not yet
materialized the sparse allocation unit. Correct physical-size queries do not
freeze a filesystem. The fixture now syncs and closes its sparse writer before
comparing scans; it retains all accounting assertions and repeats the 2/8-worker
comparison 24 times. A native regression observes allocation before/after flush
and checks the persisted physical size against an independent Windows API.

## macOS: derust bulk enumeration

Credit: derust, MIT license, src/scan/bulk.rs and explorer.rs, locally inspected.
Reuse the syscall/parsing technique, not GUI code or rayon dependency. Call
getattrlistbulk on open directories with 128 KiB buffers and RETURNED_ATTRS,
NAME, OBJTYPE, ERROR, FILE_ALLOCSIZE; also request IDs and logical length where
supported. Parse packed returned masks, signed relative name offsets and record
boundaries defensively. Open subdirectories with openat/no-follow. Metadata
fallback fills directory size, identity and fields absent from returned masks.
All raw calls stay inside the macOS platform scan module. Attribute decoder has host-runnable
synthetic tests; native filesystem tests run on macOS runners.

Names use the record length prefix separately from the returned-attributes
bitmap. ATTR_CMN_NAME is a signed offset relative to its attrreference_t and
attr_length includes the final NUL; the decoder borrows exactly those bytes,
excluding the NUL and record padding. OsStr::from_bytes and Path::join preserve
them without decoding or normalization. Synthetic tests include 0xff, newline,
quote, optional ERROR slots and record boundaries.

APFS rejects invalid UTF-8 at file creation (EILSEQ), before scanning. It preserves
the supplied normalization of valid UTF-8 names. HFS+ instead stores decomposed
names; byte agreement with a caller's precomposed creation path is impossible.
See [Apple's APFS filename contract](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/FAQ/FAQ.html).
The native filename fixture identifies the filesystem, asserts invalid-byte
rejection on APFS/HFS+, and checks newline/quote names with both composed and
decomposed Unicode. APFS must return the exact creation bytes; HFS+ must return
the stored decomposed bytes. Other filesystems retain the arbitrary-byte check.


## Evidence limits

The source scanner had all six native cells verified before migration.
New facade runtime evidence must be recorded separately. Privileged raw MFT/USN,
non-admin Windows fallback, HFS+ normalization and musl wheel installation
remain UNVERIFIED. Foreign parser tests run on Linux independently of native APIs.
