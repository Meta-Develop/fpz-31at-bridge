# Contributing

Keep static findings, synthetic-device observations, software tests and physical-machine measurements distinguishable. For a protocol claim, include the analyzed file hash and preferred-image address or a sanitized capture with the exact operator action and explicit synthetic-reply labels. Record failed observations as well as successes.

Run the offline suite before submitting changes:

```sh
python3 -m unittest discover -s tests -v
```

No test should discover, open, claim or send commands to a live machine. Hardware experiments require their own explicit scope and physical prerequisites; see [the validation plan](docs/hardware-validation.md). Do not introduce a live CLI mode by simply translating G-code to command strings.

Do not include vendor binaries, license keys, machine-specific setup files, unredacted traces, local absolute paths, USB unit identifiers or private conversations. Small synthetic fixtures and carefully selected factual summaries are appropriate. Preserve third-party notices if a future change includes third-party code.

Package metadata uses version 0.1.0 for the research snapshot. It is not a release certification or a statement of hardware compatibility.
