""".env.example must stay in step with Settings.

run.sh copies .env.example to .env on a fresh install, so a value that disagrees
with the code default silently changes behaviour for a new checkout, and a
missing key hides a knob entirely.
"""
import re

from app.config import ROOT_DIR, Settings

EXAMPLE = (ROOT_DIR / ".env.example").read_text()

# Keys deliberately left out of the example: secrets and per-machine paths that
# have no sensible shared default.
_EXEMPT = {
    "anthropic_api_key", "gemini_api_key", "websearch_api_key",
    "pricecharting_token", "ebay_client_id", "ebay_client_secret",
    "ebay_user_refresh_token", "ebay_ru_name", "ebay_verification_token",
    "ebay_deletion_endpoint_url", "collection_photos_dir",
    "public_image_base_url", "database_url",
}


def _example_values() -> dict[str, str]:
    values = {}
    for line in EXAMPLE.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().lower()] = value.strip()
    return values


def test_every_setting_appears_in_the_example():
    documented = _example_values()
    missing = [
        name for name in Settings.model_fields
        if name not in documented and name not in _EXEMPT
    ]
    assert not missing, f"add these to .env.example: {sorted(missing)}"


def test_example_values_match_the_code_defaults():
    documented = _example_values()
    defaults = Settings.model_fields
    mismatched = []
    for name, value in documented.items():
        field = defaults.get(name)
        if field is None or name in _EXEMPT:
            continue
        default = field.default
        if isinstance(default, bool):
            same = value.lower() == str(default).lower()
        elif isinstance(default, (int, float)):
            same = bool(re.fullmatch(r"-?[\d.]+", value)) and float(value) == float(default)
        else:
            same = value == str(default)
        if not same:
            mismatched.append(f"{name}: example={value!r} code default={default!r}")
    assert not mismatched, "\n".join(mismatched)
