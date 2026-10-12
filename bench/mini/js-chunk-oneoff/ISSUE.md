# chunk() drops the last partial chunk

`chunk(arr, size)` splits an array into groups of `size`; the last group may be shorter.

- `chunk([1, 2, 3, 4, 5], 2)` returns `[[1, 2], [3, 4], [5]]`
- `chunk([], 3)` returns `[]`
