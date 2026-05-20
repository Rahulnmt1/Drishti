"""bank_kb — Indian banking knowledge-base engine.

Package layout:
    fetcher.py     HTTP client with polite delays, retries, conditional GET.
    discover.py    Scrape an IR/press-release page and yield PDF URLs.
    classify.py    Classify a PDF (type, fiscal quarter, year) from URL+filename.
    extractor.py   Extract text from a PDF.
    indexer.py     SQLite (FTS5) full-text index for retrieval.
    orchestrator.py  Glue: per-bank pipeline, backfill / daily modes.
"""
