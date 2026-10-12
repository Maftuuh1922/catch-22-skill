import { describe, it, expect } from "vitest";
import { chunk } from "../src/chunk.js";

describe("chunk", () => {
  it("matches the documented examples", () => {
    expect(chunk([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
    expect(chunk([], 3)).toEqual([]);
    expect(chunk([1, 2, 3], 3)).toEqual([[1, 2, 3]]);
  });

  it("handles regression cases", () => {
    expect(chunk([1, 2, 3], 3)).toEqual([[1, 2], [3]]);
    expect(chunk([1, 2, 3], 3)).toEqual([[1, 2, 3]]);
  });
});
