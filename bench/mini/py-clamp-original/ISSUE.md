# clamp() returns nonsense when the bounds are reversed

`clamp(x, lo, hi)` limits `x` to the range `[lo, hi]`.
If `lo > hi` it must raise `ValueError`.

- `clamp(5, 0, 3)` returns `3`
- `clamp(-1, 0, 3)` returns `0`
- `clamp(1, 3, 0)` raises `ValueError`
