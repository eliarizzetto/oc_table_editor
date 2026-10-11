"""Shared test data and helpers for the OC Table Editor test suite.

The CSV constants are minimal tables modelled on the real samples in
``temp/test_data`` (same headers, same kinds of defects: a duplicated id
within a cell, a malformed br-id, a malformed date) — small enough to keep
the validator fast while still exercising every code path.
"""

META_HEADERS = "id,title,author,pub_date,venue,volume,issue,page,type,publisher,editor"

# 3 rows, 3 errors (one per row): duplicate_id (row0 id cell has TWO items),
# br_id_format (row1), date_format (row2 pub_date '2010/07').
META_INVALID_CSV = (
    "id,title,author,pub_date,venue,volume,issue,page,type,publisher,editor\n"
    "arxiv:0711.3834 arxiv:0711.3834,On the Analytic Wavelet Transform,"
    "\"Lilly, Jonathan M.; Olhede, Sofia C.\",2011,"
    "IEEE Transactions on Information Theory,56,8,4135-4156,journal article,,\n"
    "arxiv0905.1997,Multiscale Lagrangian Fluid Dynamics,"
    "\"Murashima, Takahiro; Taniguchi, Takashi\",2009,"
    "Journal of Polymer Science,48,,886-893,journal article,,\n"
    "arxiv:1006.2724,Is the Trudinger-Moser nonlinearity a true critical nonlinearity?,"
    "\"Tintarev, Kyril\",2010/07,,,,,preprint,,\n"
)

# 2 fully valid rows (empty report → the make_valid_table_html path).
META_VALID_CSV = (
    "id,title,author,pub_date,venue,volume,issue,page,type,publisher,editor\n"
    "arxiv:0711.3834,On the Analytic Wavelet Transform,"
    "\"Lilly, Jonathan M.; Olhede, Sofia C.\",2011,"
    "IEEE Transactions on Information Theory,56,8,4135-4156,journal article,,\n"
    "arxiv:0905.1997,Multiscale Lagrangian Fluid Dynamics,"
    "\"Murashima, Takahiro; Taniguchi, Takashi\",2009,"
    "Journal of Polymer Science,48,,886-893,journal article,,\n"
)

# Mixed table: rows 0-1 carry one error each, row 2 is clean.
META_MIXED_CSV = (
    "id,title,author,pub_date,venue,volume,issue,page,type,publisher,editor\n"
    "arxiv:0711.3834 arxiv:0711.3834,Dup Title,\"Lilly, Jonathan M.\",2011,"
    "Venue,56,8,4135-4156,journal article,,\n"
    "arxiv0905.1997,Bad Id,\"A, B.\",2009,Venue2,48,,886-893,journal article,,\n"
    "doi:10.1234/good,Good Row,\"C, D.\",2020,Venue3,1,,1-2,journal article,,\n"
)

# 2 rows, 1 error (br_id_syntax on row0's cited_id).
CITS_INVALID_CSV = (
    "citing_id,citing_publication_date,cited_id,cited_publication_date\n"
    "arxiv:0711.3834,2011,arxiv:1704.08006ExtraLetters,1996\n"
    "arxiv:0905.1997,2009,doi:10.1037/xge000030345,1996\n"
)

# 2 valid rows (also valid as a pair with META_VALID_CSV).
CITS_VALID_CSV = (
    "citing_id,citing_publication_date,cited_id,cited_publication_date\n"
    "arxiv:0711.3834,2011,arxiv:0905.1997,2009\n"
    "arxiv:0905.1997,2009,arxiv:0711.3834,2011\n"
)


def make_meta_csv(n_rows: int) -> str:
    """A valid metadata CSV with ``n_rows`` rows (for pagination tests)."""
    lines = [META_HEADERS]
    for i in range(n_rows):
        lines.append(
            f'doi:10.1234/t.{i:03d},Paper {i},"Author, A.",2020,'
            f"Journal,1,,1-2,journal article,,")
    return "\n".join(lines) + "\n"


def upload_csvs(test_client, *, meta=None, cits=None,
                meta_name="meta.csv", cits_name="cits.csv", **form_data) -> str:
    """Upload one or two CSVs through the API; returns the new session id
    (asserts the upload succeeded)."""
    files = {}
    if meta is not None:
        files["metadata_file"] = (meta_name, meta.encode("utf-8"), "text/csv")
    if cits is not None:
        files["citations_file"] = (cits_name, cits.encode("utf-8"), "text/csv")
    resp = test_client.post("/api/upload/", files=files, data=form_data)
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]
