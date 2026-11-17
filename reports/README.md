# Reports of pending panics

`reports/<id>/` is the evidence pack of the panic in
`signatures/pending/<id>.json`: `issue.md` (draft for Servo developers),
`context.json` and `AGENT.md` (for agents), `analysis.md` (root cause notes by
people and agents), the reduced testcase and its `output.log`.

Nothing here is posted to `servo/servo` automatically. A report is removed by
the signature sync once its panic has an upstream `B-fuzzing` issue.
