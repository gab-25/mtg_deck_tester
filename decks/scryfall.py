"""Fetching card data and images from Scryfall (with local cache)."""

import time
import urllib.parse

import requests

from .text_utils import card_slug, front_face_name

# Scryfall asks every client to identify itself with a custom User-Agent.
SCRYFALL_HEADERS = {"User-Agent": "MTGDeckTester/0.1.0", "Accept": "application/json"}

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


def download_image(url: str) -> bytes | None:
    """Downloads an image from Scryfall and returns its bytes (or None)."""
    resp = _scryfall_get(url)
    if resp is not None and resp.status_code == 200:
        return resp.content
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


def process_cached_card(card: dict, cache) -> dict:
    """Processes Scryfall card JSON, caches missing images, and structures the data.

    The returned ``image_paths`` are cache *keys* (image basenames); the bytes
    themselves live in the cache backend.
    """
    card_id = card.get("id", "unknown")
    lang = card.get("lang", "en")
    image_names = []

    def _ensure_image(name: str, url: str) -> None:
        if not cache.has_image(name):
            data = download_image(url)
            if data:
                cache.set_image(name, data)
        if cache.has_image(name):
            image_names.append(name)

    # Check layout / image uris.
    if "image_uris" in card:
        # Single physical image.
        _ensure_image(f"img_{card_id}_{lang}.jpg", card["image_uris"]["normal"])

    elif (
        "card_faces" in card
        and len(card["card_faces"]) > 0
        and "image_uris" in card["card_faces"][0]
    ):
        # Double-sided card (distinct images per face).
        for idx, face in enumerate(card["card_faces"]):
            _ensure_image(
                f"img_{card_id}_{lang}_face{idx}.jpg", face["image_uris"]["normal"]
            )

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
        "image_paths": image_names,
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


def fetch_card_raw(card_name: str, cache) -> dict | None:
    """Returns a card's raw English Scryfall JSON, from cache or Scryfall.

    ``None`` when Scryfall doesn't know the card, or couldn't be reached (then
    nothing is cached, so it is retried next time). The rules compiler needs
    fields the processed card leaves out (``oracle_id``, ``keywords``,
    ``colors``), and they are all in the JSON already cached at import time.
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
        return cached

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
    return card


def fetch_card_data(card_name: str, cache) -> dict:
    """Fetches English card data from cache or Scryfall.

    ``cache`` is a cache backend (see :mod:`decks.cache`) exposing
    ``get_card``/``set_card``/``has_image``/``get_image``/``set_image``.
    """
    card = fetch_card_raw(card_name, cache)
    if card is None:
        return None
    return process_cached_card(card, cache)
