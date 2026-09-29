# Hangul documents (.hwpx / .hwp)

- Work on .hwpx and .hwp files only through the hwp_* tools of the "hwp" MCP server. They are binary files: never open, read, write or search them with file or shell tools.
- Always pass the absolute file path.
- Find: hwp_outline shows the sections. Then hwp_read_document(range="s2") reads one section, or hwp_search("words") finds the paragraphs about a topic.
- Edit: hwp_replace_text changes words, hwp_set_paragraph_text replaces a whole paragraph, hwp_insert_paragraph and hwp_delete_paragraphs add and remove paragraphs, hwp_set_cell_text writes table cells.
- Check after every edit: read the "Now:" lines of the result, then call hwp_diff to confirm that only the intended text changed. If not, call hwp_undo.
- Addresses: p12 = paragraph 12; t0.r1.c2 = table 0, row 1, column 2. After inserting or deleting paragraphs, use the new numbers the result reports.
