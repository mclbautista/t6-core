# Resolve local Project Library: 202 Mac Pro (#374)

**2026-11-22, Manila time · Partial.** One approved local library on **202 Mac Pro**. Installed DaVinci Resolve **21.1.0**, build **21.1.00014**; macOS **15.6.1**. The app version describes the installed executable, not the version that originally wrote every project. The Bay identity was checked against its Notion Bays row and the existing SSH host identity. No other Bay or Resolve project server was accessed.

## 1. Location, layout, and version coverage

- The `Local Database` registration in `~/Library/Preferences/Blackmagic Design/DaVinci Resolve/dblist.conf` points to `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Resolve Project Library` on this Bay. Use the selected registration; do not infer the active library from this default path on another Bay.
- That one library contained **15 `Project.db` files**, all with SQLite headers. Each was in its own directory below `Resolve Projects/Users/<local-user>/Projects/`: **11** directly below that root and **4** one folder deeper. There were **20** SQLite-header files overall, including a `User.db`; its schema was not inspected. Three `Project.db` files opened read-only. Each sampled file had **146 tables** and one `SM_Project` row. Some columns store blobs, but the project containers are SQLite, not opaque blobs.
- A Notion Projects query for `Rotation = in` returned **59** rows (no next page); all **59** were blank for `DaVinci Resolve Version`. Do not infer which other versions are in use from the field's options or old inventory. Build coverage is **21.1.0 on this Bay only**. Compiling a complete in-use version list requires current Bay/version evidence or filled Projects fields.

## 2. Which fields are readable, and what they prove

| Field | Observed on the copied local library | Comparison limit |
| --- | --- | --- |
| Library name | `Local Database` is the registration label; the filesystem root has a different name. | No equivalence to a PostgreSQL database name was established. |
| Folder path | The sampled layout has directory nesting under `Projects`. Two of three sampled `SM_Project` rows had a non-null `Folder` value, but `SM_ProjectFolder` had zero rows in all three. | Begin with filesystem-relative folder segments on this observed layout. A SQL `Folder` value alone is not a resolved local folder path. Do not assume a server path mapping or behavior for deeper nesting not observed here. |
| Project name | `SM_Project.ProjectName` was populated in all three files and matched each containing directory name. | A bare name is not an identity key. |
| Local modified time | `Project.db` filesystem modification time was available. `SM_Project.LastModTimeInSecs` was present as `bigint`; its populated values, epoch, and update behavior were not verified. `Sm2Timeline.ModTimeInSecs` is timeline-level, not project-level. | Neither timestamp was validated as a cross-store content signal. |
| Internal ID | `SM_Project.SM_Project_id` is declared `uuid` and populated in the three sampled rows. | Not shown to survive a local/server copy or identify a known pair. |
| Timeline count | `Sm2Timeline` row counts and `SM_Project_Sm2Timeline` join row counts agreed in the three sampled files. | Different counts may become a scoped difference signal once compared for a known pair with equivalent semantics. Equal counts do not establish identical content. |
| Fingerprint | SHA-256 reads the copied file bytes consistently across the probe. | This is a local byte fingerprint, not a content hash comparable to PostgreSQL. |

**Recommendation for #380:** Do not emit content `matches` or `differs` yet. `t6.resolve.ProjectServer.projects()` exposes database, folder path, name, and internal ID, but no verified equivalent content signal. Validate identity first with a known local/server pair, including a rename or same-name collision case. Only after that, an identity-only version can say **same project on server; content unverified** for a unique validated key, **not on server** for a validated key absent from a complete successful inventory, and **unverified** when the mapping or inventory is incomplete or ambiguous. If #380 requires a content verdict, keep it blocked until a comparable signal distinguishes a known unchanged pair from a known changed pair without claiming more than it measures.

## 3. Offline first-frame marker stamps

**Unverified (not “no”).** The copied schema exposes `Sm2Timeline.FieldsBlob`; no approved synthetic project with a known first-frame `t6t_` stamp and an unstamped control was supplied. No real marker payloads were read. The [#185 note](resolve-marker-custom-data.md) is a PostgreSQL catalog observation, not proof about this local library. A valid test must associate the exact known stamp with its timeline and first frame, and return no stamp for the control, while Resolve is closed. If offline reads prove unsupported, propose keeping #18 project-only and re-drafting [#381](https://github.com/mclbautista/t6-core/issues/381) and [#274](https://github.com/mclbautista/t6-core/issues/274) around a separately authorized Resolve-running read path. Do not add this fixture's marker behavior by guessing a blob format.

## 4. Process guard and read-only evidence

- The installed bundle executable name is `Resolve`. The SSH user matched the console user, and the all-session process list contained hundreds of entries. Exact executable-name checks found no `Resolve` before copy, after copy, and after inspection. Detection while Resolve is open was **not** exercised. For the nightly job, skip before data access if the process is found, or if process visibility is uncertain; validate the positive case before relying on this guard.
- The scope contained **130 file/directory entries**, including **94 files**. No symlinks, special files, lock-named files, or SQLite `-wal`/`-shm`/`-journal` sidecars were observed. The copy was on the same Bay in a mode-`0700` temporary directory. The source was not opened by SQLite. The source entry set, file sizes, and modification times were unchanged across checks.
- The three copied SQLite databases opened with `mode=ro` and `query_only=ON`. Only file headers, schema metadata, presence/equality tests, and counts were inspected; no real names, rows, IDs, hashes, or database files were exported. The copy's entry set, modification metadata, and file SHA-256 checksums matched before and after inspection. No new sidecar or lock appeared. The task-owned temporary directory was removed and its absence checked.
- This verifies **copy-first** read-only inspection on this library with Resolve closed. It does not verify opening the live source in place or behavior when a WAL or active writer is present.

## Fixture and remaining gates

The [21.1.0 fixture generator](../../tests/fixtures/resolve/local/21.1.0/build.py) creates a minimal stand-in with one SQLite file per project and only invented names and IDs. It represents the observed project-file layout and selected schema fields, not the full vendor schema. No live files were retained. Other layouts, offline marker controls, and a validated local/server comparison pair remain open; #374 is not ready to close.
