# Decisions

Notes every candidate worker receives. Curated by the main thread with `decide --add`.
Keep this short — it is injected into every worker packet.

- Metric arithmetic uses `Decimal`; never compare floats.
- Fail closed. A missing field is a bug to fix at the source, not a case to branch on.
