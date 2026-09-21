#!/usr/bin/env python3
"""Download official writeups for retired Hack The Box Sherlocks."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import requests

API_BASE = "https://labs.hackthebox.com/api/v4"
APP_ORIGIN = "https://app.hackthebox.com"
MAX_RETRIES = 6
DEFAULT_RPM = 14
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)

LIST_ENDPOINT = 'sherlocks'
WRITEUP_ENDPOINT = 'sherlocks/{id}/writeup/official'
DEFAULT_OUTPUT_DIR = 'writeups/sherlocks'
KIND = 'Sherlock'


class HTBError(RuntimeError):
    pass


class HTBAuthError(HTBError):
    pass


class HTBHTTPError(HTBError):
    def __init__(self, status_code: int, endpoint: str, message: str = "") -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        text = f"HTB returned HTTP {status_code} for /{endpoint}"
        if message:
            text += f": {message}"
        super().__init__(text)


def read_token(path: Optional[str]) -> str:
    if path:
        p = Path(path).expanduser()
        try:
            mode = stat.S_IMODE(p.stat().st_mode)
            if os.name != "nt" and mode & 0o077:
                print(
                    f"WARNING: token file permissions are {mode:04o}; "
                    f"consider: chmod 600 {p}",
                    file=sys.stderr,
                )
        except OSError:
            pass

        try:
            token = p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HTBError(f"Could not read token file: {p}") from exc
    else:
        token = (os.environ.get("HTB_TOKEN") or "").strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token:
        raise HTBError(
            "Missing HTB token. Use --token-file ~/.htb-token or set HTB_TOKEN."
        )
    return token


def retry_after_seconds(value: Optional[str], fallback: float) -> float:
    if not value:
        return fallback
    try:
        return max(0.5, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.5, dt.timestamp() - time.time())
    except Exception:
        return fallback


def safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", name).strip(" .")
    if not name:
        name = "writeup"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def filename_from_content_disposition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", value, flags=re.I)
    if match:
        return safe_filename(unquote(match.group(1).strip().strip('"')))

    match = re.search(r'filename\s*=\s*"([^"]+)"', value, flags=re.I)
    if match:
        return safe_filename(match.group(1))

    match = re.search(r"filename\s*=\s*([^;]+)", value, flags=re.I)
    if match:
        return safe_filename(match.group(1).strip().strip('"'))

    return None


def positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("data", "machines", "info"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def extract_meta(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
        return payload["meta"]
    return {}


class HTBClient:
    def __init__(
        self,
        token: str,
        *,
        requests_per_minute: int,
        timeout: int = 30,
        debug: bool = False,
    ) -> None:
        self.timeout = timeout
        self.debug = debug
        self.min_interval = 60.0 / requests_per_minute
        self.last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": BROWSER_UA,
                "Accept-Language": "en-US,en;q=0.5",
                "Origin": APP_ORIGIN,
                "Referer": f"{APP_ORIGIN}/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
            }
        )

    def _pace(self) -> None:
        if self.last_request_at <= 0:
            return
        remaining = self.min_interval - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def request(
        self,
        endpoint: str,
        *,
        params: Optional[dict[str, Any]] = None,
        accept: str = "application/json, text/plain, */*",
        stream: bool = False,
    ) -> requests.Response:
        url = f"{API_BASE}/{endpoint.lstrip('/')}"

        for attempt in range(MAX_RETRIES):
            self._pace()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={"Accept": accept},
                    timeout=self.timeout,
                    stream=stream,
                    allow_redirects=True,
                )
                self.last_request_at = time.monotonic()
            except requests.RequestException as exc:
                if attempt + 1 >= MAX_RETRIES:
                    raise HTBError(
                        f"Network request failed after {MAX_RETRIES} attempts."
                    ) from exc
                delay = min(20.0, 0.75 * (2 ** attempt))
                if self.debug:
                    print(
                        f"[debug] network error; retrying in {delay:.1f}s",
                        file=sys.stderr,
                    )
                time.sleep(delay)
                continue

            if self.debug:
                remaining = response.headers.get("X-RateLimit-Remaining", "?")
                limit = response.headers.get("X-RateLimit-Limit", "?")
                print(
                    f"[debug] GET /{endpoint.lstrip('/')} -> "
                    f"{response.status_code} rate={remaining}/{limit}",
                    file=sys.stderr,
                )

            if response.status_code == 429:
                if attempt + 1 >= MAX_RETRIES:
                    response.close()
                    raise HTBError("HTB rate limit reached repeatedly.")
                delay = retry_after_seconds(
                    response.headers.get("Retry-After"),
                    min(60.0, 2.0 * (2 ** attempt)),
                )
                response.close()
                if self.debug:
                    print(f"[debug] 429; sleeping {delay:.1f}s", file=sys.stderr)
                time.sleep(delay)
                continue

            if 500 <= response.status_code <= 599:
                if attempt + 1 >= MAX_RETRIES:
                    code = response.status_code
                    response.close()
                    raise HTBHTTPError(code, endpoint)
                delay = min(20.0, 0.75 * (2 ** attempt))
                response.close()
                time.sleep(delay)
                continue

            return response

        raise HTBError("Request failed.")

    def get_json(
        self,
        endpoint: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        response = self.request(endpoint, params=params)
        try:
            if response.status_code >= 400:
                snippet = response.text[:200].replace("\n", " ")
                raise HTBHTTPError(response.status_code, endpoint, snippet)
            try:
                return response.json()
            except ValueError as exc:
                raise HTBError(
                    f"HTB returned non-JSON data for /{endpoint}."
                ) from exc
        finally:
            response.close()


def fetch_all_pages(
    client: HTBClient,
    endpoint: str,
    *,
    extra_params: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    page = 1
    per_page = 100
    last_page: Optional[int] = None
    items: list[dict[str, Any]] = []

    while last_page is None or page <= last_page:
        params = {"page": page, "per_page": per_page}
        if extra_params:
            params.update(extra_params)

        payload = client.get_json(endpoint, params=params)
        chunk = extract_items(payload)
        meta = extract_meta(payload)
        items.extend(chunk)

        if page == 1:
            last_page = (
                positive_int(meta.get("last_page"))
                or positive_int(meta.get("lastPage"))
                or positive_int(meta.get("pages"))
            )

        if last_page is None:
            if len(chunk) < per_page:
                break
        elif page >= last_page:
            break
        page += 1

    unique: dict[int, dict[str, Any]] = {}
    for item in items:
        item_id = positive_int(item.get("id"))
        if item_id is not None:
            unique[item_id] = item
    return list(unique.values())


def is_retired(item: dict[str, Any]) -> bool:
    retired = item.get("retired")
    if isinstance(retired, bool) and retired:
        return True
    if isinstance(retired, int) and retired != 0:
        return True
    state = str(item.get("state") or "").strip().lower()
    return state.startswith("retired")


def item_name(item: dict[str, Any]) -> Optional[str]:
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def list_targets(client: HTBClient) -> list[dict[str, Any]]:
    return [
        item
        for item in fetch_all_pages(
            client,
            LIST_ENDPOINT,
            extra_params={"state": "retired"},
        )
        if is_retired(item) and item_name(item)
    ]


def validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTBError("HTB returned an invalid/non-HTTPS download URL.")


def stream_pdf(response: requests.Response, output: Path) -> None:
    tmp = output.with_name(output.name + ".part")
    try:
        prefix = bytearray()
        with tmp.open("wb") as fh:
            if os.name != "nt":
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                if len(prefix) < 16:
                    prefix.extend(chunk[: 16 - len(prefix)])
                fh.write(chunk)

        check = bytes(prefix).lstrip(b"\xef\xbb\xbf \t\r\n")
        if not check.startswith(b"%PDF-"):
            ctype = response.headers.get("Content-Type", "unknown")
            raise HTBError(
                f"Final download was not a PDF (Content-Type: {ctype})."
            )
        os.replace(tmp, output)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def download_signed_url(url: str, output: Path, *, debug: bool) -> None:
    validate_download_url(url)
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "application/pdf,application/octet-stream,*/*",
            },
            timeout=90,
            stream=True,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise HTBError("Failed to download the PDF from HTB storage.") from exc

    try:
        if debug:
            print(
                f"[debug] storage GET -> {response.status_code} "
                f"content-type={response.headers.get('Content-Type', '-')}",
                file=sys.stderr,
            )
        if response.status_code == 403:
            raise HTBError(
                "The signed HTB download URL was rejected or expired."
            )
        if response.status_code >= 400:
            raise HTBError(
                f"HTB storage returned HTTP {response.status_code}."
            )
        stream_pdf(response, output)
    finally:
        response.close()


def official_endpoint(item_id: int) -> str:
    return WRITEUP_ENDPOINT.format(id=item_id)


def download_one(
    client: HTBClient,
    item: dict[str, Any],
    output_dir: Path,
    *,
    force: bool,
) -> str:
    item_id = positive_int(item.get("id"))
    name = item_name(item)
    if item_id is None or not name:
        return "failed"

    output_dir.mkdir(parents=True, exist_ok=True)
    fallback_output = output_dir / safe_filename(name)

    if fallback_output.exists() and not force:
        print(f"[skip] {name} (already exists)")
        return "existing"

    endpoint = official_endpoint(item_id)
    response = client.request(endpoint, stream=True)

    try:
        if response.status_code == 401:
            raise HTBAuthError("HTB returned 401. Check your App Token.")
        if response.status_code in (403, 404, 422):
            print(f"[skip] {name} (no accessible official writeup)")
            return "unavailable"
        if response.status_code >= 400:
            raise HTBHTTPError(response.status_code, endpoint)

        ctype = (
            response.headers.get("Content-Type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )

        direct_name = filename_from_content_disposition(
            response.headers.get("Content-Disposition")
        )
        output = output_dir / (direct_name or safe_filename(name))

        if output.exists() and not force:
            print(f"[skip] {name} (already exists)")
            return "existing"

        if ctype == "application/pdf":
            stream_pdf(response, output)
        else:
            try:
                payload = response.json()
            except ValueError as exc:
                raise HTBError(
                    f"/{endpoint} returned neither PDF nor valid JSON."
                ) from exc

            url = payload.get("url") if isinstance(payload, dict) else None
            if not isinstance(url, str) or not url.strip():
                print(f"[skip] {name} (no official writeup URL)")
                return "unavailable"

            response.close()
            download_signed_url(url.strip(), output, debug=client.debug)

        print(f"[ok]   {name} -> {output}")
        return "downloaded"
    finally:
        response.close()


def select_targets(
    targets: list[dict[str, Any]],
    name: Optional[str],
    limit: Optional[int],
) -> list[dict[str, Any]]:
    targets = sorted(
        targets,
        key=lambda item: (item_name(item) or "").casefold(),
    )

    if name:
        exact = [
            item for item in targets
            if (item_name(item) or "").casefold() == name.casefold()
        ]
        if not exact:
            raise HTBError(f"No retired {KIND} named '{name}' was found.")
        return exact[:1]

    if limit is not None:
        return targets[:limit]
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Download official writeup PDFs for retired HTB Sherlocks.',
    )
    parser.add_argument(
        "name",
        nargs="?",
        help="Optional exact name. Omit to process all retired items.",
    )
    parser.add_argument(
        "--token-file",
        help="Read HTB App Token from this file.",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Destination directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--requests-per-minute",
        type=int,
        default=DEFAULT_RPM,
        help=f"HTB API request rate (default: {DEFAULT_RPM}/min).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only process the first N retired items (useful for testing).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing PDFs.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show request diagnostics. Tokens and signed URLs are never printed.",
    )
    args = parser.parse_args()

    if not 1 <= args.requests_per_minute <= 60:
        print("ERROR: --requests-per-minute must be between 1 and 60.", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("ERROR: --limit must be >= 1.", file=sys.stderr)
        return 2

    try:
        token = read_token(args.token_file)
        client = HTBClient(
            token,
            requests_per_minute=args.requests_per_minute,
            debug=args.debug,
        )

        targets = select_targets(list_targets(client), args.name, args.limit)
        print(f"Retired {KIND} targets: {len(targets)}")

        counts = {
            "downloaded": 0,
            "existing": 0,
            "unavailable": 0,
            "failed": 0,
        }

        for index, item in enumerate(targets, start=1):
            name = item_name(item) or f"id={item.get('id')}"
            print(f"[{index}/{len(targets)}] {name}")
            try:
                result = download_one(
                    client,
                    item,
                    Path(args.output_dir).expanduser(),
                    force=args.force,
                )
                counts[result] += 1
            except HTBAuthError:
                raise
            except HTBError as exc:
                print(f"[fail] {name}: {exc}", file=sys.stderr)
                counts["failed"] += 1

        print(
            "Done. "
            f"downloaded={counts['downloaded']} "
            f"existing={counts['existing']} "
            f"unavailable={counts['unavailable']} "
            f"failed={counts['failed']}"
        )
        return 0 if counts["failed"] == 0 else 1

    except KeyboardInterrupt:
        print("\\nInterrupted.", file=sys.stderr)
        return 130
    except HTBError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
