# Long-Horizon Memory

AI GameMaster now supports archival campaign memory summaries for long-running threads.

## How It Works

- Turn logs are grouped into fixed-size chunks.
- Each chunk is summarized into a compact memory record.
- Recent memory summaries are injected into LLM context packs.
- This keeps long-term continuity while controlling token usage.

## Storage

- Table: `campaign_memory_summaries`
- Columns:
  - `campaign_id`
  - `start_turn_id`
  - `end_turn_id`
  - `summary`
  - `created_at`

## Configuration

- `AIGM_CONTEXT_MEMORY_SUMMARY_TURNS`
  - Number of turns per archival summary chunk.
- `AIGM_CONTEXT_MEMORY_MAX_ENTRIES`
  - Max archived memory summaries included in packed context.

## Operational Notes

- Lower chunk size: faster memory updates, more rows.
- Higher chunk size: fewer rows, denser summaries.
- Keep this balanced with `AIGM_CONTEXT_RECENT_TURNS` for prompt cost control.
