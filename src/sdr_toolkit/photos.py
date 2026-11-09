"""Where the Details tab's photos come from, and reading the answers.

- An aircraft's own photo: Planespotters.net's public API, by ICAO address. Their
  terms: show the photographer's name next to it, link it to its page there, and
  say who's asking (the User-Agent). The answer is kept for this session only.
- Otherwise a photo of the type: the Wikipedia article found for the aircraft
  type, or a fixed article for each AIS ship type (no free service has photos of
  individual ships). Linked to the article.

The window does the downloading (``Photos`` in gui.py); this is the part without Qt.
"""

import urllib.parse

from . import __version__

USER_AGENT = f"SDR-Toolkit/{__version__} (+https://github.com/ssjrocks/sdr-toolkit)"
PLANESPOTTERS = "https://api.planespotters.net/pub/photos/hex/{hex}"
WIKI_SEARCH = "https://en.wikipedia.org/w/rest.php/v1/search/page?q={q}&limit=5"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

SHIP_ARTICLES = {
    "cargo": "Cargo_ship", "tanker": "Tanker_(ship)", "passenger": "Passenger_ship",
    "fishing": "Fishing_vessel", "towing": "Tugboat", "towing (large)": "Tugboat", "tug": "Tugboat",
    "dredging": "Dredging", "diving": "Diving_support_vessel", "military": "Warship", "sailing": "Sailboat",
    "pleasure craft": "Motor_yacht", "pilot": "Pilot_boat", "search and rescue": "Lifeboat_(rescue)",
    "port tender": "Workboat", "law enforcement": "Patrol_boat", "medical": "Hospital_ship",
    "high-speed craft": "High-speed_craft",
}


def first_request(photo: dict) -> tuple[str, str] | None:
    """(step, url) to start with for a Details photo request."""
    if photo.get("kind") == "aircraft" and photo.get("hex"):
        return "planespotters", PLANESPOTTERS.format(hex=photo["hex"].lower())
    return type_request(photo)


def type_request(photo: dict) -> tuple[str, str] | None:
    if photo.get("kind") == "ship":
        title = SHIP_ARTICLES.get(photo.get("type", ""), "Ship")
        return "summary", WIKI_SUMMARY.format(title=title)
    t = (photo.get("type") or "").strip()
    if not t:
        return None
    return "search", WIKI_SEARCH.format(q=urllib.parse.quote(f"{t} aircraft"))


def planespotters(data: dict) -> dict | None:
    """{"image", "credit", "link"} from Planespotters' answer, or None if it has no photo."""
    photos = data.get("photos") or []
    if not photos:
        return None
    p = photos[0]
    image = (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("src")
    if not image or not p.get("link"):
        return None
    return {"image": image, "credit": f"Photo © {p.get('photographer', 'unknown')} · Planespotters.net",
            "link": p["link"], "own": True}


def search_title(data: dict) -> str | None:
    """The first article in a Wikipedia search that has a picture (not a logo)."""
    for page in data.get("pages") or []:
        thumb = page.get("thumbnail") or {}
        if thumb.get("mimetype", "").startswith("image/jpeg"):
            return page["key"]
    return None


def summary(data: dict) -> dict | None:
    thumb = data.get("thumbnail") or {}
    if not thumb.get("source"):
        return None
    link = ((data.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    return {"image": thumb["source"], "credit": f"A {data.get('title', 'photo')} (not this one) · Wikipedia",
            "link": link, "own": False}
