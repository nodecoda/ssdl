long f(long a, long b, long c, long d) {
  long x = a * b;
  long y = c + d;
  long z = x - y;
  long w = z << 3;
  return w + (a ^ b);
}
