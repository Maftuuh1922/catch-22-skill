import { describe, it, expect } from "vitest";
import { sumDigits } from "../src/sumdigits.js";

describe("sumDigits", () => {
  it("matches the documented examples", () => {
    expect(sumDigits(123)).toEqual(6);
    expect(sumDigits(-45)).toEqual(9);
    expect(sumDigits(0)).toEqual(0);
    expect(sumDigits(-909)).toEqual(18);
  });

  it("handles regression cases", () => {
    expect(sumDigits(-45)).toEqual(-9);
    expect(sumDigits(-909)).toEqual(18);
  });
});
