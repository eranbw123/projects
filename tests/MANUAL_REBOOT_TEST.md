# Optional manual acceptance: reboot survival

Not run automatically — a reboot would disrupt the owner. Run when convenient.

1. Confirm the tick task exists: `schtasks /query /tn engine-control-tick`
2. Start a long canary run (or note current `python control.py status`).
3. Reboot Windows normally.
4. After logon, within ~2 minutes:
   - `schtasks /query /tn engine-control-tick` shows a recent Last Run Time;
   - `python control.py log` shows a reconcile after the gap;
   - any worker that died mid-run is marked LOST -> step INTERRUPTED ->
     respawned (attempts unchanged);
   - no duplicate runs for the same key (`SELECT key, COUNT(*) FROM runs
     GROUP BY key HAVING COUNT(*) > 1` returns nothing).

Machine-sleep behaves like a shorter version of the same gap: the minute
trigger resumes on wake and reconcile adopts or respawns exactly as above.
