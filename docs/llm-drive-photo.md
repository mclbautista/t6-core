# Drive photo read

Ticket #621, parent #615. The internal task is
`t6.llm.drive_photo.PHOTO_READ` (`drive-photo-read-v1`).
`read_drive_photo(Image(photo_bytes, ImageType.jpeg), provider=provider)`
returns Drive readings, a reason, and the existing task trace. Each reading
has nullable label and serial text, copied without normalization, and a
finite confidence from 0 to 1. At least one field must be readable.

A malformed answer discards the entire result and gives an empty list with
the existing task failure reason. A valid empty list gives reason
`unreadable`. Nothing is inferred from missing text.

The provider configuration must declare `supports_images: true` for a model
and endpoint that accept images. The default refuses image input before
sending. Image parts carry inline bytes, with PNG, JPEG or WebP media types.
Text-only messages retain their existing wire shape.

The task requires `zdr_enforced` retention through the existing policy gate;
unknown retention sends nothing. It uses no photo persistence, and the result
and trace contain no image bytes. Recording-channel admission belongs to
Capture and resolution under ADR 0009; callers must enforce it before calling
this task.

## Eval cases

- **Task:** Read labels and maker sticker serials from one photo, including
  several Drives, without completing obscured text.
- **Expected properties:** Exact readable text, one entry per readable Drive,
  null for hidden fields, bounded confidence, no invented Drives, and an
  empty list when nothing is readable.
- **Graders:** `grade_photo` checks schema, exact text and count, independent
  of ordering. A person checks visual grounding and confidence calibration.
  No model judge decides pass or fail.
- **Synthetic cases:** `t6.llm.drive_photo_evals.CASES` holds described
  fixtures for one Drive, several Drives (one sticker hidden), and an
  unreadable photo. Labels and serials are invented. Tests build a blank PNG
  in a temporary folder and use `FakeProvider`; they verify the task and
  grading contract, not OCR accuracy.
- **Real cases:** None are supplied or committed by this ticket. Keep real
  photos and run records in private storage, outside the repository.

A recorded live run with human review is required before release of this
task or a change to its prompt, schema, model or provider, under
`docs/agents/llm-task-specs.md`. Fixture descriptions must first be rendered
as synthetic photos for that run; sending the descriptions as text does not
test photo reading. This ticket's offline tests are not live-run evidence.
