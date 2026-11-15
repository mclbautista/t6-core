# Package Specs database

Create one Notion database (data source) named **Package Specs**. Each row is
one spec for an exact Package kind and optional Client spec. Keep only one live
row per combination; the reader returns duplicates so callers can detect them.
Property names and types must match exactly:

| Property | Notion type | Meaning |
| --- | --- | --- |
| Name | Title | Human-readable row name. |
| Package kind | Text (rich text) | Required package kind; matched exactly. |
| Client spec | Text (rich text) | Optional client-specific name; blank means the general spec. |
| Version | Text (rich text) | Required version identifier, copied as text when a Package is prepared. |
| Advisory items | Text (rich text) | One checklist item per line. |
| Blocking items | Text (rich text) | One checklist item per line; leave blank for a new spec. |

Blank lines are ignored and surrounding whitespace on item lines is removed.
Items cannot contain line breaks; advisory items are returned before blocking
items, in their respective line order. A blank Blocking items property means
all items are advisory. Move an item to Blocking items only when the team
intends it to stop a handoff. Maintain the Version explicitly: assign a new,
unique version whenever the checklist or its blocking flags change, so a
prepared Package can record which requirements it used. The reader does not
write to Notion or increment versions. General and client-specific rows are
selected independently; there is no automatic fallback or merge.
