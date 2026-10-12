import { describe, it, expect } from "vitest";
import { titleCase } from "../src/titlecase.js";

describe("titleCase", () => {
  it("matches the documented examples", () => {
    expect(titleCase("hello world")).toEqual("Hello World");
    expect(titleCase("a TALE of two")).toEqual("A Tale Of Two");
    expect(titleCase("JS")).toEqual("Js");
  });

  it("handles regression cases", () => {
    expect(titleCase("JS")).toEqual("Js");
  });
});
