// Number formatting for chart axes, legend and tooltip.
//
// uPlot's default formatter (Intl.NumberFormat) keeps at most 3 decimals, so CFD values
// such as residuals (1e-5) or small physical times (2.5e-4 s) would all read "0".

const SIG_DIGITS = 6;
const SMALL = 1e-3;
const LARGE = 1e6;

function trimExponent(text) {
  // "1.500000e-5" -> "1.5e-5", "2.000e+6" -> "2e+6"
  const [mantissa, exponent] = text.split("e");
  const m = mantissa.includes(".") ? mantissa.replace(/\.?0+$/, "") : mantissa;
  return `${m}e${exponent}`;
}

function useExponent(v) {
  const a = Math.abs(v);
  return a !== 0 && (a < SMALL || a >= LARGE);
}

/** Full-precision value for legend and tooltip ("—" when missing). */
export function fmtValue(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (useExponent(v)) return trimExponent(v.toExponential(SIG_DIGITS - 1));
  return String(Number(v.toPrecision(SIG_DIGITS)));
}

/** Short tick label; float noise (0.30000000000000004) is removed. */
export function fmtTick(v) {
  if (v === null || v === undefined) return "";
  if (useExponent(v)) return trimExponent(v.toExponential(3));
  return String(Number(v.toPrecision(10)));
}
