## SEC EDGAR API (via authed_get)

Search and read public company submission/filing history and XBRL financial
data from the US Securities and Exchange Commission's EDGAR system. The SEC
EDGAR data API is free, public, and requires **NO authentication**. Access it
with `authed_get` using the full API URL on host `data.sec.gov`.

**Base URL:** `https://data.sec.gov`

**CIK (Central Index Key) -- must be 10-digit zero-padded:** Every
`data.sec.gov` path takes a CIK in the form `CIK0000320193` (Apple). The CIK
must be **left-padded with zeros to 10 digits** -- `CIK320193` will be
**rejected**.

**Resolving a ticker to a CIK (ticker->CIK workflow):** The SEC publishes two
ticker->CIK map files on `www.sec.gov`, and **both ARE reachable via
`authed_get`** (they are the only `www.sec.gov` paths that are -- the rest of
`www.sec.gov` is not proxied):

- `https://www.sec.gov/files/company_tickers.json` -- ticker -> CIK + company name.
- `https://www.sec.gov/files/company_tickers_exchange.json` -- same, plus exchange info.

Recommended workflow:
1. Fetch the map file with `authed_get`. It is **large** (thousands of entries),
   so pass `output_file` to stream it to the workspace, then read/search it with
   `get_workspace_file` / `run_python`.
2. The JSON is an object keyed by an arbitrary integer index; each value has
   fields `cik_str` (an integer, **not** zero-padded), `ticker`, and `title`
   (company name). `company_tickers_exchange.json` instead has a `fields` header
   plus a `data` array of rows. Find the entry whose `ticker` matches.
3. **Zero-pad** that `cik_str` to 10 digits yourself (left-pad with zeros), then
   build the `data.sec.gov` URL (e.g. `cik_str` `320193` -> `CIK0000320193`).

If you already know the CIK from the user or prior context, skip the map fetch
and just zero-pad it.

**Key API Paths:**

| Path | Description |
|------|-------------|
| `/submissions/CIK##########.json` | Entity filing history: `name`, `tickers`, `exchanges`, `sic`/`sicDescription`, addresses, `formerNames`, and `filings.recent` (**parallel arrays**: `accessionNumber`, `form`, `filingDate`, `reportDate`, `primaryDocument`, `primaryDocDescription`, `items`, ...). |
| `/submissions/CIK##########-submissions-NNN.json` | Older-filings spillover for large filers; the filenames to fetch are listed in `filings.files[].name` of the primary submissions file. |
| `/api/xbrl/companyconcept/CIK##########/<taxonomy>/<Concept>.json` | One concept's time series for one entity. Taxonomy is e.g. `us-gaap`, `dei`, `ifrs-full`; concept is e.g. `Revenues`, `AccountsPayableCurrent`. |
| `/api/xbrl/companyfacts/CIK##########.json` | **Every** XBRL fact for one entity (very **large** -- use `output_file`). |
| `/api/xbrl/frames/<taxonomy>/<Concept>/<Unit>/CY####Q#.json` | One concept+unit across all reporting entities for one period. Period token is `CY####`, `CY####Q#`, or `CY####Q#I` (`I` = instantaneous/point-in-time, e.g. balance-sheet items). |

**Response notes:**
- Responses are JSON (served gzip/deflate; httpx decompresses transparently --
  no special handling needed).
- The `filings.recent` block uses **parallel arrays**, not an array of objects:
  index `i` across `accessionNumber[i]`, `form[i]`, `filingDate[i]`, ... is one
  filing. Filter by `form` (e.g. `10-K`, `10-Q`, `8-K`) using the index.

**Size gate + `output_file` (IMPORTANT):** The `authed_get` size gate is ~3 KB.
The `companyfacts` payload and busy-filer `submissions` payloads vastly exceed
it and will return `response_too_large` if fetched inline. For those, pass
`output_file` so the body is streamed to the workspace, then read it with
`get_workspace_file` / `run_python`. EDGAR endpoints do **not** support a
`fields[]`-style trimmer, so `output_file` is the primary tool here. When you
need only one metric, prefer `companyconcept` (one concept) over `companyfacts`
(everything).

**Rate limit:** SEC's fair-access policy caps usage at **10 requests/second**.
Do not loop requests aggressively.

**Example tool calls:**

```
# Resolve a ticker to a CIK: fetch the (large) ticker->CIK map to the workspace
tool_call(tool_name="authed_get", arguments={"url": "https://www.sec.gov/files/company_tickers.json", "output_file": "company_tickers.json"})

# Submissions / filing history for one CIK (big filers likely need output_file)
tool_call(tool_name="authed_get", arguments={"url": "https://data.sec.gov/submissions/CIK0000320193.json", "output_file": "aapl_submissions.json"})

# A single company concept (one metric's time series)
tool_call(tool_name="authed_get", arguments={"url": "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/Revenues.json"})

# All XBRL facts for one entity -- large, so stream it to the workspace
tool_call(tool_name="authed_get", arguments={"url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json", "output_file": "aapl_facts.json"})

# A frames query: one concept+unit across all entities for a period (I = instantaneous)
tool_call(tool_name="authed_get", arguments={"url": "https://data.sec.gov/api/xbrl/frames/us-gaap/AccountsPayableCurrent/USD/CY2023Q1I.json", "output_file": "frame.json"})
```

**Important Notes:**
- No authentication is required -- credentials are not needed for this API.
- CIK must be **10-digit zero-padded** in the path (`CIK0000320193`, not `CIK320193`).
- The ticker->CIK map files `company_tickers.json` and
  `company_tickers_exchange.json` on `www.sec.gov` **ARE** reachable via
  `authed_get` (the only `www.sec.gov` paths that are); use them to resolve a
  ticker to a CIK, then zero-pad. The rest of `www.sec.gov` is not proxied.
- Frames period token format: `CY####`, `CY####Q#`, or `CY####Q#I` (`I` = instantaneous).
- SEC requires a descriptive `User-Agent` on every request; the server injects
  one automatically, so you do **not** set it.
- Developer reference: `https://www.sec.gov/search-filings/edgar-application-programming-interfaces`
