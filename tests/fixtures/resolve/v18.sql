-- Synthetic Resolve 18 project server schema and rows. No real show names.
CREATE TABLE "Sm2Folder" ("SM_FolderID" TEXT PRIMARY KEY, "FolderName" TEXT NOT NULL, "ParentFolderId" TEXT);
CREATE TABLE "Sm2Project" ("SM_ProjectID" TEXT PRIMARY KEY, "ProjectName" TEXT NOT NULL, "FolderId" TEXT);
INSERT INTO "Sm2Folder" VALUES ('f-root', '', NULL), ('f-a', 'Series A', 'f-root'), ('f-a1', 'Season 1', 'f-a');
INSERT INTO "Sm2Project" VALUES
  ('p-1', 'Ep101 Grade', 'f-a1'),
  ('p-2', 'Ep102 Grade', 'f-a1'),
  ('p-3', 'Sizzle', 'f-root'),
  ('p-4', 'Ep201 Grade', 'f-a');
