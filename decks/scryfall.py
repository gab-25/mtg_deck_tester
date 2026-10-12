"""Fetching card data from Scryfall (with local cache)."""

import time
import urllib.parse

import requests

from .text_utils import card_slug, front_face_name

# Scryfall asks every client to identify itself with a custom User-Agent.
SCRYFALL_HEADERS = {"User-Agent": "MTGDeckTester/0.2.0", "Accept": "application/json"}

# Polite delay between Scryfall requests (their guidelines ask for 50-100ms).
_REQUEST_DELAY = 0.1


def _scryfall_get(url: str, max_retries: int = 5):
    """GETs a Scryfall URL, retrying on rate limits (429) and transient errors.

    Returns the Response, or None if it could not be retrieved after retries.
    """
    for attempt in range(max_retries):
        time.sleep(_REQUEST_DELAY)
        try:
            resp = requests.get(url, headers=SCRYFALL_HEADERS, timeout=10)
        except requests.RequestException:
            # Network hiccup: back off and retry.
            time.sleep(0.5 * (attempt + 1))
            continue

        if resp.status_code == 429:
            # Rate limited: honor Retry-After if present, else exponential backoff.
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 0.5 * (2**attempt)
            except ValueError:
                wait = 0.5 * (2**attempt)
            time.sleep(wait)
            continue

        return resp

    return None


def get_face_details(face_or_card: dict) -> dict:
    """Extracts text details from a card or a face object."""
    name = face_or_card.get("name") or ""
    mana_cost = face_or_card.get("mana_cost") or ""
    type_line = face_or_card.get("type_line") or ""
    rules_text = face_or_card.get("oracle_text") or ""

    return {
        "name": name.strip(),
        "mana_cost": mana_cost.strip(),
        "type_line": type_line.strip(),
        "rules_text": rules_text.strip(),
    }


def _extract_price_eur(card: dict) -> float:
    """Extracts the EUR price, falling back to foil EUR and converted USD."""
    prices = card.get("prices", {}) or {}

    eur_str = prices.get("eur")
    if eur_str:
        try:
            return float(eur_str)
        except ValueError:
            pass

    eur_foil_str = prices.get("eur_foil")
    if eur_foil_str:
        try:
            return float(eur_foil_str)
        except ValueError:
            pass

    # Fallback to USD converted to EUR (~0.93 conversion rate).
    usd_str = prices.get("usd")
    if usd_str:
        try:
            return float(usd_str) * 0.93
        except ValueError:
            pass
    else:
        usd_foil_str = prices.get("usd_foil")
        if usd_foil_str:
            try:
                return float(usd_foil_str) * 0.93
            except ValueError:
                pass

    return 0.0


def _combat_stats(card: dict) -> tuple:
    """Power and toughness as printed on the front face (strings, or None).

    Kept verbatim (``"*"``, ``"1+*"``): turning them into numbers is a rules
    decision, and it belongs to the game engine.
    """
    faces = card.get("card_faces") or []
    source = card if "power" in card or not faces else faces[0]
    return source.get("power"), source.get("toughness")


def card_image_urls(card: dict) -> list:
    """The ``normal`` image URL of each printed face, on Scryfall's CDN.

    One URL when the root carries ``image_uris`` (split and room cards too,
    whose faces share one physical image), one per face for double-sided cards.
    The browser loads them straight from ``cards.scryfall.io``.
    """
    if "image_uris" in card:
        return [card["image_uris"]["normal"]]
    faces = card.get("card_faces") or []
    if faces and "image_uris" in faces[0]:
        return [face["image_uris"]["normal"] for face in faces]
    return []


def process_cached_card(card: dict) -> dict:
    """Structures Scryfall card JSON into the data stored on a deck."""
    card_id = card.get("id", "unknown")

    # Extract details.
    faces_details = []
    if "card_faces" in card and len(card["card_faces"]) > 0:
        # Multi-faced layout (split card, room, transform, adventure).
        # Note: for split/room cards the root has image_uris but card_faces holds the descriptions.
        for face in card["card_faces"]:
            faces_details.append(get_face_details(face))
    else:
        # Standard card.
        faces_details.append(get_face_details(card))

    power, toughness = _combat_stats(card)

    return {
        "id": card_id,
        "name": card.get("name") or "Unknown Card",
        "image_urls": card_image_urls(card),
        "faces": faces_details,
        "price_eur": _extract_price_eur(card),
        "type_line": card.get("type_line", ""),
        "cmc": card.get("cmc", 0.0),
        "power": power,
        "toughness": toughness,
        # Color identity drives the Commander color-identity rule and the deck pips.
        "color_identity": card.get("color_identity", []),
        # Colors this card can tap for.
        "produced_mana": card.get("produced_mana", []),
        # Per-format legality; the format's entry drives the ban-list check.
        "legalities": card.get("legalities", {}),
    }


def fetch_card_data(card_name: str, cache) -> dict:
    """Fetches English card data from cache or Scryfall.

    ``cache`` is a cache backend (see :mod:`decks.cache`) exposing
    ``get_card``/``set_card``.
    """
    # Keyed on the front face, so "Sink into Stupor" and "Sink into Stupor //
    # Soporific Springs" share the one entry they both resolve to.
    slug = card_slug(front_face_name(card_name))
    cache_key = f"card_en_{slug}"

    # 1. Check the cache.
    cached = cache.get_card(cache_key)
    if cached is not None:
        if cached.get("error") == "not_found":
            return None
        return process_cached_card(cached)

    # 2. Fetch the exact English match from the Scryfall API.
    encoded_name = urllib.parse.quote(card_name)
    exact_url = f"https://api.scryfall.com/cards/named?exact={encoded_name}"

    resp = _scryfall_get(exact_url)
    if resp is None:
        # Network/rate-limit gave up: do not cache, so it is retried next run.
        return None
    if resp.status_code != 200:
        if resp.status_code == 404:
            # Cache the negative result (the card genuinely does not exist).
            cache.set_card(cache_key, {"error": "not_found"})
        return None
    try:
        card = resp.json()
    except Exception:
        return None

    cache.set_card(cache_key, card)
    return process_cached_card(card)
