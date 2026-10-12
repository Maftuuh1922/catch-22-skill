# median() is wrong for even-length lists

`median(values)` returns the middle value of the sorted list. For an even number of
values it must return the mean of the two middle values.

- `median([3, 1, 2])` returns `2`
- `median([1, 2, 3, 4])` returns `2.5`
