export function sumDigits(n) {
  return String(Math.abs(n)).split("").reduce((a, d) => a + Number(d), 0);
}
