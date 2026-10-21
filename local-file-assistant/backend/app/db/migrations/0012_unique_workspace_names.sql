-- Workspace names are unique, ignoring case ("Study" and "study" were two workspaces nobody could tell apart).
-- Existing duplicates are renamed "Name (2)", "Name (3)" by age first, so the index can be created; the new names
-- are worked out before any row changes, so a rename can't shift the next one's number.
-- ponytail: SQLite's lower() folds ASCII only; workspaces.py also compares with casefold() before saving.
CREATE TEMP TABLE ws_renames AS
    SELECT w.id, w.name || ' (' || ((SELECT COUNT(*) FROM workspaces o
                                     WHERE lower(o.name) = lower(w.name) AND o.id < w.id) + 1) || ')' AS new_name
    FROM workspaces w
    WHERE EXISTS (SELECT 1 FROM workspaces o WHERE lower(o.name) = lower(w.name) AND o.id < w.id);
UPDATE workspaces SET name = (SELECT new_name FROM ws_renames r WHERE r.id = workspaces.id)
    WHERE id IN (SELECT id FROM ws_renames);
DROP TABLE ws_renames;
CREATE UNIQUE INDEX workspaces_name ON workspaces(lower(name));
