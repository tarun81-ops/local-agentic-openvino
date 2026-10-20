-- Keep Hindi words whole in memory and chat-recall keyword search. The default unicode61 tokenizer treats
-- Devanagari vowel signs and the virama as separators, so "देवता" was indexed as fragments ("द", "वत").
-- Both tables are external-content FTS indexes, so they are rebuilt from their source tables.
DROP TABLE IF EXISTS memories_fts;
CREATE VIRTUAL TABLE memories_fts USING fts5(text, content='memories', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2 categories 'L* N* Co Mn Mc'");
INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');

DROP TABLE IF EXISTS chat_index_fts;
CREATE VIRTUAL TABLE chat_index_fts USING fts5(text, content='chat_index', content_rowid='msg_id',
    tokenize="unicode61 remove_diacritics 2 categories 'L* N* Co Mn Mc'");
INSERT INTO chat_index_fts(chat_index_fts) VALUES ('rebuild');
