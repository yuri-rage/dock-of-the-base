"""
Jinja2 template filters

-- Yuri - May 2026
"""


def deg_to_dms(value, latlon="lat"):
    """convert degrees to degrees, minutes, seconds (DMS) format"""
    directions = {
        "lat": ("N", "S"),
        "lon": ("E", "W"),
    }

    positive, negative = directions[latlon]

    sign = positive if value >= 0 else negative
    value = abs(value)

    degrees = int(value)
    minutes_full = (value - degrees) * 60
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60

    return f"{degrees:02d}° {minutes:02d}' {seconds:05.2f}\" {sign}"


def m_to_ft(value):
    """convert meters to feet"""
    return value * 3.280839895


def google_maps_link(lat, lon):
    """generate a Google Maps link, given latitude and longitude"""
    return f"https://www.google.com/maps?q={lat},{lon}"
