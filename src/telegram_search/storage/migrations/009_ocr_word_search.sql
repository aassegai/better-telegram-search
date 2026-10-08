-- Contentless FTS stores only derived postings, no second OCR text copy.
CREATE VIRTUAL TABLE ocr_gram_fts USING fts5(grams, content='', tokenize='unicode61');
CREATE TRIGGER ocr_grams_ai AFTER INSERT ON ocr_cache WHEN new.state='ready' BEGIN
    INSERT INTO ocr_gram_fts(rowid,grams) VALUES(new.rowid,ocr_search_grams(new.text_normalized));
END;
CREATE TRIGGER ocr_grams_ad AFTER DELETE ON ocr_cache WHEN old.state='ready' BEGIN
    INSERT INTO ocr_gram_fts(ocr_gram_fts,rowid,grams)
    VALUES('delete',old.rowid,ocr_search_grams(old.text_normalized));
END;
CREATE TRIGGER ocr_grams_au AFTER UPDATE OF text_normalized,state ON ocr_cache BEGIN
    INSERT INTO ocr_gram_fts(ocr_gram_fts,rowid,grams)
    SELECT 'delete',old.rowid,ocr_search_grams(old.text_normalized) WHERE old.state='ready';
    INSERT INTO ocr_gram_fts(rowid,grams)
    SELECT new.rowid,ocr_search_grams(new.text_normalized) WHERE new.state='ready';
END;
INSERT INTO ocr_gram_fts(rowid,grams)
SELECT rowid,ocr_search_grams(text_normalized) FROM ocr_cache WHERE state='ready';
INSERT INTO schema_migrations(version) VALUES(9);
