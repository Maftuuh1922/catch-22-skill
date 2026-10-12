def clamp(x, lo, hi):
    if lo > hi:
        raise ValueError("lo must be <= hi")
    return max(lo, min(x, hi))
