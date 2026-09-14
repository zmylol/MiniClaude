# Constraint memory pilot

Mode: **live**.

Synthetic structured decision-log continuation; not end-to-end coding success.
Equal UTF-8 memory-byte caps, not equal tokens. One model/run is exploratory.
Shared small-summary preprocessing is charged to each arm; arm totals are not API spend.
Failed calls may have unknown billable usage; retained reservations are shown separately.

| Method | Pass | Constraint errors | Evidence | Input tokens | Output tokens |
|---|---:|---:|---:|---:|---:|
| retrieval | 30/36 | 8.3% | 88.0% | 162321 | 28762 |
| summary | 29/36 | 11.1% | 88.9% | 154644 | 34684 |
| versioned | 30/36 | 8.3% | 91.7% | 152910 | 28790 |

Paired versioned comparisons (wins / losses / ties):

- vs retrieval: 0 / 0 / 36
- vs summary: 2 / 1 / 33
