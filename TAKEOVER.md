# Cost Agent (Mantra): takeover

Marko, 13.11.2026: "Please build a guard to ask me, are you sure you want to recognize images? This will cost you. Are you sure anything which costs money, it needs to ask me and give me how much money it costs. ... Make Mantra Cost Agent in star menu."

What it is: every paid call (Groq vision and chat in Queen, Speechify above an allowance, paid images, any API key) asks the gate first. The gate shows Marko a box, "Are you sure? This will cost about $X (N image recognitions with Groq). YES / NO", and a NO (or no answer) means nothing is spent.

Parts:
- `gate/costagent.py`: the gate (python3 standard library). `costagent.py serve`; CLI `ask`, `spent`, `status`. Port from ~/.config/mantra-ports.json ("costagent", 9047). Local callers need nothing; the VM calls it over Tailscale with X-Cost-Token.
- `app/main.swift`: Cost Agent (Mantra).app. With `--ask "<text>"` it is the box (floats on top at the mouse, prints YES or NO, NO on Escape and on the timeout); without it, the dashboard (today / week / month per provider and per process, waiting questions, history of answers).
- Marko away: when ~/.voice/route says `phone <adb target>` the question is also spoken on the Pixel (phonesay) and a YES / NO page is opened on it (Tailscale address of the Mac, token in the URL).

Rules of the gate: one question per job, a YES is a grant for that job's estimate, a job past estimate + 20 % asks again, a job with no declared size is granted one chunk ($0.25) at a time, a NO is remembered for ten minutes, free providers pass, Speechify passes inside a daily allowance shown on the dashboard.

Data (ledgers, answers, token, config) is in ~/.mantra-os/cost-agent, never in this repository. Build: only GitHub (.github/workflows/build.yml), release Latest; the Mac downloads the release and signs it "Mantra Apps". Rule: MANTRA_MANIFEST modules/cost-agent.md. Test the gate with the environment COSTAGENT_DATA, COSTAGENT_PORT, COSTAGENT_NO_UI=1 and a fake provider; never a real paid call.
