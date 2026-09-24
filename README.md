# SOI KÈO AI Quant Terminal v8

Clean PostgreSQL migration build.

- Removes obsolete `usage_logs.key_hash` legacy column.
- Keeps `access_keys.key_hash` as the authentication hash.
- Migrates legacy numeric IDs/user IDs to TEXT where the app uses opaque IDs.
- Does not reset or delete the PostgreSQL database.
- Preserves existing users, access keys, analyses and usage logs where compatible.
- No Odds API, no Google Search Grounding.
