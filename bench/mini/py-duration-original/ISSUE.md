# parse_duration() miscounts minutes

`parse_duration(text)` converts strings like `"1h30m"` to seconds (`h`, `m`, `s` units).

- `parse_duration("1h30m")` returns `5400`
- `parse_duration("45s")` returns `45`
- `parse_duration("2m")` returns `120`
