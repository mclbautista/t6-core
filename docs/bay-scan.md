# Bay scan

The bay scan reports which Resolve projects each bay holds in its local disk library, so t6 can tell which copies match the
project server. Design: [ADR 0008](adr/0008-bay-scanner-boundary.md). Facts about the library:
[bay-resolve-disk-library.md](research/bay-resolve-disk-library.md).

## The scan script

`src/t6/bay_scanner.py` is one Python file that uses only the standard library and runs on Munki's own Python
(`/usr/local/munki/munki-python`). It reads the bay's serial number, copies each project's `Project.db` to a temporary folder,
reads only the copies (`mode=ro&immutable=1`), deletes them and prints one report as a line of JSON on stdout. It posts
nothing, holds no credential, installs nothing and leaves nothing behind. It never starts Resolve, and when a process
from inside the Resolve app bundle is running it scans nothing and prints a report with status `skipped_resolve_open`.

Exit code 0 means a report was printed, including a skipped one. Exit code 1 means a real failure, with the reason on stderr
and nothing on stdout.

The report format is `t6.bay_scan.BayScanReport` (format version 1). A test runs the script and validates its output with it.

## Run it once by hand

Do this only with the owner's yes for the bay. From a machine that can reach the bay over SSH (the Mac mini):

```sh
ssh <bay> '/usr/local/munki/munki-python -' < src/t6/bay_scanner.py
```

The script goes in on stdin, so nothing is copied to the bay and nothing has to be removed. To run a copy of the file from a
checkout of a t6-core tag instead, use the same command from that checkout. To try it away from a bay, point `--home` at a
folder that holds a `Library/Application Support/Blackmagic Design/DaVinci Resolve/Resolve Project Library/Resolve Projects/Users/<user>/Projects`
tree (macOS only: the serial number comes from `ioreg`).

## Posting a report

The Mac mini posts each report to the t6 API as `bay scan report` with the `bay-scanner` service token, which lives only on
the Mac mini. t6 ties the report to a Bay through the serial and the Machine to Bays mapping, keeps the latest report per Bay,
and refuses a serial that maps to no Bay. `bay scan list [<bay>]` shows each Bay's last report as fresh, stale (scanned more
than 3 days ago) or no report. The playbook that does this nightly is in t6-munki-repo (#20).
