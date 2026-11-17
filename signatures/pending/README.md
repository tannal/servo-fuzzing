# Pending signatures

One `<id>.json` per panic that was found here and has no `B-fuzzing` issue in
`servo/servo` yet. They are loaded together with `../rust-panic.json`, so a
pending panic is classified as `KNOWN_CRASH`. Its report is in `reports/<id>/`.

Files are added by the `new-panic/<id>` pull requests and removed by the
signature sync once a matching upstream issue exists.
