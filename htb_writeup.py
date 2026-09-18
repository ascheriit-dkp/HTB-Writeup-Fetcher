#!/usr/bin/env python3
"""
Download ONE official Hack The Box retired-machine writeup.

Supports both observed HTB behaviors:
  1. /machine/writeup/<id> returns application/pdf directly
  2. /machine/writeup/<id> returns JSON containing a temporary signed S3 URL

Single-machine only:
- no machine enumeration
- no ID ranges
- no bulk mode

Examples:
    python3 htb_writeup.py Cap --token-file ~/.htb-token
    python3 htb_writeup.py Cap --token-file ~/.htb-token --debug
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

import requests


API_BASE = "https://labs.hackthebox.com/api/v4"
APP_ORIGIN = "https://app.hackthebox.com"
MAX_RETRIES = 5

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)


class HTBError(RuntimeError):
    pass


def read_token_file(path: str) -> str:
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

    if not token:
        raise HTBError(f"Token file is empty: {p}")

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

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
        name = "writeup.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def filename_from_content_disposition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", value, flags=re.I)
    if m:
        return safe_filename(unquote(m.group(1).strip().strip('"')))

    m = re.search(r'filename\s*=\s*"([^"]+)"', value, flags=re.I)
    if m:
        return safe_filename(m.group(1))

    m = re.search(r"filename\s*=\s*([^;]+)", value, flags=re.I)
    if m:
        return safe_filename(m.group(1).strip().strip('"'))

    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HTBClient:
    def __init__(self, token: str, timeout: int = 30, debug: bool = False) -> None:
        self.timeout = timeout
        self.debug = debug
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

    def request(
        self,
        endpoint: str,
        *,
        accept: str = "application/json, text/plain, */*",
        stream: bool = False,
    ) -> requests.Response:
        url = f"{API_BASE}/{endpoint.lstrip('/')}"

        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.get(
                    url,
                    headers={"Accept": accept},
                    timeout=self.timeout,
                    stream=stream,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                if attempt + 1 >= MAX_RETRIES:
                    raise HTBError(
                        f"Network request failed after {MAX_RETRIES} attempts."
                    ) from exc
                delay = min(10.0, 0.75 * (2 ** attempt))
                if self.debug:
                    print(
                        f"[debug] network error; retrying in {delay:.1f}s",
                        file=sys.stderr,
                    )
                time.sleep(delay)
                continue

            if self.debug:
                print(
                    f"[debug] GET /{endpoint.lstrip('/')} "
                    f"-> {response.status_code}",
                    file=sys.stderr,
                )
                print(
                    f"[debug] content-type="
                    f"{response.headers.get('Content-Type', '-')}",
                    file=sys.stderr,
                )

            if response.status_code == 429:
                if attempt + 1 >= MAX_RETRIES:
                    response.close()
                    raise HTBError("HTB rate limit reached; retry later.")

                delay = retry_after_seconds(
                    response.headers.get("Retry-After"),
                    min(30.0, 1.0 * (2 ** attempt)),
                )
                response.close()

                if self.debug:
                    print(
                        f"[debug] rate limited; retrying in {delay:.1f}s",
                        file=sys.stderr,
                    )

                time.sleep(delay)
                continue

            if 500 <= response.status_code <= 599:
                if attempt + 1 >= MAX_RETRIES:
                    code = response.status_code
                    response.close()
                    raise HTBError(f"HTB returned HTTP {code} repeatedly.")

                delay = min(10.0, 0.75 * (2 ** attempt))
                response.close()
                time.sleep(delay)
                continue

            return response

        raise HTBError("Request failed.")


def get_json(client: HTBClient, endpoint: str) -> Any:
    response = client.request(endpoint)
    try:
        if response.status_code in (401, 403):
            raise HTBError(
                "Authentication/access denied. Check your App Token "
                "and account access."
            )

        if response.status_code == 404:
            raise HTBError(f"HTB returned 404 for /{endpoint}.")

        if response.status_code >= 400:
            raise HTBError(f"HTB returned HTTP {response.status_code}.")

        try:
            return response.json()
        except ValueError as exc:
            raise HTBError("HTB returned non-JSON data unexpectedly.") from exc
    finally:
        response.close()


def unwrap_info(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("info"), dict):
        return payload["info"]
    raise HTBError("Unexpected machine-profile response format.")


def unwrap_walkthroughs(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
        return payload["message"]
    raise HTBError("Unexpected walkthrough response format.")


def validate_signed_download_url(url: str) -> None:
    """
    Avoid following an arbitrary URL with the downloader.

    The current HTB flow returns an HTTPS pre-signed Amazon S3 URL such as:
      htb-content-prod-private-storage.s3.eu-central-1.amazonaws.com
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise HTBError("HTB returned an invalid download URL.") from exc

    host = (parsed.hostname or "").lower()

    if parsed.scheme != "https":
        raise HTBError("HTB returned a non-HTTPS download URL.")

    if not host.endswith(".amazonaws.com"):
        raise HTBError(
            f"HTB returned an unexpected download host: {host or '<empty>'}"
        )

    if not parsed.path.startswith("/machines/writeup/"):
        raise HTBError(
            "HTB returned an unexpected S3 object path for the writeup."
        )


def stream_pdf_response_to_file(
    response: requests.Response,
    output: Path,
) -> None:
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
                    needed = 16 - len(prefix)
                    prefix.extend(chunk[:needed])

                fh.write(chunk)

        if not prefix:
            raise HTBError("The download returned an empty response.")

        check = bytes(prefix).lstrip(b"\xef\xbb\xbf \t\r\n")
        if not check.startswith(b"%PDF-"):
            ctype = response.headers.get("Content-Type", "unknown")
            raise HTBError(
                "The final download did not contain a PDF "
                f"(Content-Type: {ctype})."
            )

        os.replace(tmp, output)

    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def download_from_signed_url(
    signed_url: str,
    output: Path,
    *,
    debug: bool,
) -> None:
    validate_signed_download_url(signed_url)

    try:
        response = requests.get(
            signed_url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "application/pdf,application/octet-stream,*/*",
            },
            timeout=60,
            stream=True,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise HTBError("Failed to download the PDF from HTB storage.") from exc

    try:
        if debug:
            print(
                f"[debug] signed storage GET -> {response.status_code}",
                file=sys.stderr,
            )
            print(
                f"[debug] storage content-type="
                f"{response.headers.get('Content-Type', '-')}",
                file=sys.stderr,
            )

        if response.status_code == 403:
            raise HTBError(
                "The signed HTB download URL was rejected or expired. "
                "Run the command again to obtain a fresh URL."
            )

        if response.status_code >= 400:
            raise HTBError(
                f"HTB storage returned HTTP {response.status_code}."
            )

        stream_pdf_response_to_file(response, output)

    finally:
        response.close()


def download_official_writeup(
    client: HTBClient,
    machine_id: int,
    machine_name: str,
    output_dir: Path,
    expected_sha256: Optional[str],
    force: bool,
) -> Path:
    response = client.request(
        f"machine/writeup/{machine_id}",
        accept="application/json, text/plain, */*",
        stream=True,
    )

    try:
        if response.status_code in (401, 403):
            raise HTBError(
                "HTB denied access to the official writeup. "
                "Check your subscription/access rights."
            )

        if response.status_code == 404:
            raise HTBError("Official writeup was not found.")

        if response.status_code >= 400:
            raise HTBError(
                f"Writeup request returned HTTP {response.status_code}."
            )

        ctype = (
            response.headers.get("Content-Type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )

        cd_filename = filename_from_content_disposition(
            response.headers.get("Content-Disposition")
        )
        filename = cd_filename or safe_filename(f"{machine_name}.pdf")

        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / filename

        if output.exists() and not force:
            raise HTBError(
                f"File already exists: {output}\n"
                "Use --force if you intentionally want to replace it."
            )

        if ctype == "application/pdf":
            if client.debug:
                print("[debug] HTB returned PDF directly", file=sys.stderr)

            stream_pdf_response_to_file(response, output)

        else:
            try:
                payload = response.json()
            except ValueError as exc:
                raise HTBError(
                    "HTB returned neither a PDF nor valid JSON."
                ) from exc

            if not isinstance(payload, dict):
                raise HTBError("Unexpected writeup response format.")

            signed_url = payload.get("url")

            if not isinstance(signed_url, str) or not signed_url.strip():
                raise HTBError(
                    "HTB's response did not contain a writeup download URL."
                )

            if client.debug:
                print(
                    "[debug] HTB returned a temporary signed storage URL",
                    file=sys.stderr,
                )

            response.close()

            download_from_signed_url(
                signed_url.strip(),
                output,
                debug=client.debug,
            )

    finally:
        response.close()

    actual_hash = sha256_file(output)

    if expected_sha256:
        if actual_hash.lower() != expected_sha256.strip().lower():
            try:
                output.unlink()
            except OSError:
                pass
            raise HTBError(
                "SHA-256 verification failed. "
                "The downloaded file was deleted."
            )
        print(f"SHA-256 : {actual_hash} (verified)")
    else:
        print(f"SHA-256 : {actual_hash}")

    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download one official retired-machine HTB writeup PDF."
    )
    parser.add_argument(
        "machine",
        help="Exact HTB machine name, for example: Cap",
    )
    parser.add_argument(
        "--token-file",
        help="Read HTB App Token from this file. Preferred.",
    )
    parser.add_argument(
        "--output-dir",
        default="writeups",
        help="Destination directory (default: ./writeups)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing local PDF.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Show HTTP status/content diagnostics. "
            "The HTB token and signed S3 URL are never printed."
        ),
    )
    args = parser.parse_args()

    try:
        if args.token_file:
            token = read_token_file(args.token_file)
        else:
            token = (os.environ.get("HTB_TOKEN") or "").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()

        if not token:
            raise HTBError(
                "Missing HTB token. Use --token-file ~/.htb-token "
                "or set HTB_TOKEN."
            )

        client = HTBClient(token, debug=args.debug)

        profile = unwrap_info(
            get_json(
                client,
                f"machine/profile/{quote(args.machine, safe='')}",
            )
        )

        machine_id = profile.get("id")
        machine_name = str(profile.get("name") or args.machine)
        retired = bool(profile.get("retired"))

        if not isinstance(machine_id, int) or machine_id <= 0:
            raise HTBError("Machine profile did not contain a valid ID.")

        if not retired:
            raise HTBError(
                f"{machine_name} is not retired. "
                "This tool intentionally refuses active machines."
            )

        walkthroughs = unwrap_walkthroughs(
            get_json(client, f"machine/walkthroughs/{machine_id}")
        )

        official = walkthroughs.get("official")
        if not isinstance(official, dict):
            raise HTBError(
                f"{machine_name} does not expose an official writeup "
                "through the walkthrough endpoint."
            )

        expected_sha256 = official.get("sha256")
        if not (
            isinstance(expected_sha256, str)
            and expected_sha256.strip()
        ):
            expected_sha256 = None

        print(f"Machine : {machine_name} (ID {machine_id})")
        print("Official writeup found.")

        output = download_official_writeup(
            client=client,
            machine_id=machine_id,
            machine_name=machine_name,
            output_dir=Path(args.output_dir).expanduser(),
            expected_sha256=expected_sha256,
            force=args.force,
        )

        print(f"Saved   : {output}")
        print("Done.")
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    except HTBError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
