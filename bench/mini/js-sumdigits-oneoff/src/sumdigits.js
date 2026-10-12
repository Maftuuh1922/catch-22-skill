export function sumDigits(n) {
  return String(n).split("").reduce((a, d) => a + Number(d), 0);
}
