# slugify() keeps uppercase letters

`slugify(text)` should turn a title into a URL slug: lowercase, words joined by `-`,
anything that is not a letter or digit dropped.

Examples from the docs:

- `slugify("Hello World")` returns `"hello-world"`
- `slugify("  Rock & Roll ")` returns `"rock-roll"`
- `slugify("Python 3.12")` returns `"python-3-12"`

Right now `slugify("Hello World")` gives `"Hello-World"`.
