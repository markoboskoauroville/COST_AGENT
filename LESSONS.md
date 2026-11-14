# Lessons

- 13.11.2026: an image-collection job on the VM spent $4.22 of Groq through Queen's vision, one call at a time, with nobody asked. A guard per call is wrong (400 boxes); one question per job with its estimate, then a running counter, is right.
- Queen's own ledger keeps only the last 100 calls; the gate's ledger keeps everything from now on.
- 14.11.2026: a per-call line alone is a hole: 1000 calls of $0.02 pass every time. The line must also count small calls of the same who+job over a window (10 minutes) and ask when the sum crosses it. Small passes go to decisions.jsonl, not to the spend ledger (spent() writes the real cost; writing both would count twice).
