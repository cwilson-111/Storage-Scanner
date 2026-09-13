"""Human-readable size and percentage-bar formatting helpers."""


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{num:,.0f} {unit}"
            return f"{num:,.1f} {unit}"
        num /= 1024.0


_PARTIALS = "░▏▎▍▌▋▊▉█"   # 1/8-cell steps for a smooth, precise bar


def bar(fraction, width=14):
    fraction = max(0.0, min(1.0, fraction))
    full = fraction * width
    filled = int(full)
    cells = ["█"] * filled
    if filled < width:
        cells.append(_PARTIALS[int(round((full - filled) * 8))])
        cells.extend("░" * (width - filled - 1))
    return "".join(cells)
