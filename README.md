# Engineering Document Analysis Pipeline

Multi-stage AI pipeline that processes engineering specification PDFs using
Amazon Bedrock (Claude). Generates consolidated **steelworks.json** for
manufacturer quoting.

No system dependencies -- uses `pypdfium2`. Works on Windows, Mac, Linux.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py --input input/sample.pdf --output output/
python main.py --input input/sample.pdf --output output/ --stages 1
python main.py --input input/sample.pdf --output output/ --resume-from stage2 --run-id <prev>
python main.py --input input/sample.pdf --output output/ --max-tokens 65536 --verbose
```

## AWS Configuration

```bash
set AWS_ACCESS_KEY_ID=...
set AWS_SECRET_ACCESS_KEY=...
set AWS_SESSION_TOKEN=...
set AWS_REGION=ap-southeast-2
```

## Key Features

### Truncation Recovery (NEW)
For large documents (50+ pages), Claude's JSON response often exceeds the
token limit and gets cut off mid-object. This caused empty Stage 2/3/4 outputs.

Now uses a 3-layer defense:
1. **Continuation** -- detects `stop_reason: max_tokens`, asks Claude to
   continue from where it left off via multi-turn conversation
2. **JSON repair** -- if still truncated, finds the last complete JSON object
   in the array and closes the structure
3. **Increased limits** -- default `max_tokens_large` is 32768 (was 8192)

### Claude-Based Orientation Detection
Uses Claude vision to check if text is readable (not dimension heuristics).
Landscape engineering drawings with readable text are LEFT ALONE.

### Scope Indicator Detection
- "New [item]" / "Proposed [item]" / "To be [verb]" patterns
- Treated as definitive new scope unless N.I.C. or By Others

### Steelworks Table Extraction
- 50+ keyword auto-detection
- Each steel member as individual cost item
- Consolidated `steelworks.json` at run root

## Output Structure

```
output/{run_id}/
    steelworks.json              <-- send to manufacturer
    run_metadata.json
    error_log.json
    stage1_indexing/
    stage2_categorisation/
    stage3_costing/
    stage4_claimability/
```

## Troubleshooting

### Empty Stage 2/3/4 Output
Check error_log.json for `json_parse_error`. If truncation was the cause,
increase tokens: `--max-tokens 65536`

### AWS Credentials Error
Refresh session tokens before long runs (52 pages = ~40 min).

### Duplicate Log Lines
Fixed in this version. `setup_logging()` clears existing handlers.
