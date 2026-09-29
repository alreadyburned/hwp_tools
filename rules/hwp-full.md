# Hangul documents (.hwpx / .hwp)

- Work on .hwpx and .hwp files only through the hwp_* tools of the "hwp" MCP server. They are binary files: never open, read, write or search them with file or shell tools.
- Always pass the absolute file path.
- Find: short documents: hwp_read_document. Long ones: hwp_outline for the sections, then hwp_read_document(range="s2.1") for one section, or hwp_search("words").
- Edit with the matching tool: text (hwp_replace_text, hwp_set_paragraph_text, hwp_insert_paragraph, hwp_delete_paragraphs), character look (hwp_format_text), paragraph look (hwp_set_paragraph_format), styles (hwp_apply_style), tables (hwp_insert_table, hwp_set_cell_text, hwp_format_cells, ...), images, page setup, header/footer.
- Check after every edit: read the "Now:" lines of the result, then call hwp_diff to confirm that only the intended things changed. If not, call hwp_undo.
- Addresses: p12 = paragraph 12; t0.r1.c2 = table 0, row 1, column 2; t0.r0.c* = row 0. After inserting or deleting paragraphs, use the new numbers the result reports.
- Features the tools lack (footnotes, tab leaders, text boxes, equations, sections, tracked changes, ...): use the "hwp-direct" skill if it is available, which edits the file with a Python script.
