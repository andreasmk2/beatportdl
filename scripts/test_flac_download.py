#!/usr/bin/env python3
"""Download one Beatport track as FLAC for testing the project's auth flow.

Requires: python -m pip install requests

Example:
  BEATPORT_USERNAME='you@example.com' BEATPORT_PASSWORD='...' \
    python scripts/test_flac_download.py 1696999
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse


API_BASE = "https://api.beatport.com/v4"
# Same public OAuth client ID used by internal/beatport/auth.go.
CLIENT_ID = "ryZ8LuyQVPqbK2mBX2Hwt4qSMtnWuTYSqBPO92yQ"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


class BeatportError(RuntimeError):
    pass


def api_error(response: requests.Response) -> str:
    """Extract a useful API error without exposing credentials or tokens."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error")
        if detail:
            return str(detail)
    return f"HTTP {response.status_code} {response.reason}".strip()


def authenticate(session: requests.Session, username: str, password: str) -> str:
    login = session.post(
        f"{API_BASE}/auth/login/",
        json={"username": username, "password": password},
        timeout=30,
    )
    if not login.ok:
        raise BeatportError(f"Login failed: {api_error(login)}")

    # The Go client explicitly reads this cookie from the login response.
    session_id = login.cookies.get("sessionid")
    if not session_id:
        raise BeatportError("Login succeeded but did not return a sessionid cookie")

    authorize = session.get(
        f"{API_BASE}/auth/o/authorize/",
        params={"client_id": CLIENT_ID, "response_type": "code"},
        cookies={"sessionid": session_id},
        allow_redirects=False,
        timeout=30,
    )
    location = authorize.headers.get("Location")
    if not location:
        raise BeatportError(
            f"Authorization did not return a redirect (HTTP {authorize.status_code}); "
            "check that the account can use Beatport's API"
        )

    code = parse_qs(urlparse(location).query).get("code", [None])[0]
    if not code:
        raise BeatportError("Authorization redirect did not contain an authorization code")

    token_response = session.post(
        f"{API_BASE}/auth/o/token/",
        data={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
        },
        timeout=30,
    )
    if not token_response.ok:
        raise BeatportError(f"Token exchange failed: {api_error(token_response)}")

    token = token_response.json().get("access_token")
    if not token:
        raise BeatportError("Token response did not contain an access_token")
    return token


def download_track(
    session: requests.Session,
    token: str,
    track_id: int,
    destination: Path,
    overwrite: bool,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    download_response = session.get(
        f"{API_BASE}/catalog/tracks/{track_id}/download/",
        params={"quality": "lossless"},
        headers=headers,
        timeout=30,
    )
    if not download_response.ok:
        raise BeatportError(f"Could not request track download: {api_error(download_response)}")

    try:
        download_info = download_response.json()
    except ValueError as exc:
        raise BeatportError("Download endpoint returned invalid JSON") from exc

    quality = download_info.get("stream_quality")
    location = download_info.get("location")
    if quality != ".flac":
        raise BeatportError(
            f"Beatport returned quality {quality!r}, not FLAC; "
            "lossless downloads require an eligible subscription"
        )
    if not location:
        raise BeatportError("Download response did not contain a file location")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise BeatportError(f"Output already exists (pass --overwrite to replace it): {destination}")

    # Use a separate, unauthenticated request for the signed file URL so the
    # Beatport bearer token is never sent to the download host.
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination.name}.", suffix=".part", dir=destination.parent, delete=False
        ) as output:
            temp_path = Path(output.name)
            with requests.get(location, stream=True, timeout=(30, 120)) as file_response:
                if not file_response.ok:
                    raise BeatportError(f"File download failed: {api_error(file_response)}")
                for chunk in file_response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)

        with temp_path.open("rb") as downloaded_file:
            if downloaded_file.read(4) != b"fLaC":
                raise BeatportError("Downloaded file does not have a FLAC signature")

        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("track_id", type=int, help="Beatport track ID")
    parser.add_argument(
        "-o", "--output", type=Path, help="output file (default: track-<id>.flac)"
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    args = parser.parse_args()

    if args.track_id <= 0:
        parser.error("track_id must be a positive integer")

    username = os.environ.get("BEATPORT_USERNAME")
    password = os.environ.get("BEATPORT_PASSWORD")
    if not username or not password:
        parser.error("set BEATPORT_USERNAME and BEATPORT_PASSWORD in the environment")

    destination = args.output or Path(f"track-{args.track_id}.flac")
    if destination.exists() and not args.overwrite:
        parser.error(f"output already exists: {destination} (pass --overwrite to replace it)")

    global requests
    try:
        import requests
    except ImportError:
        print("Missing dependency: install requests with `python -m pip install requests`", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    try:
        token = authenticate(session, username, password)
        download_track(session, token, args.track_id, destination, args.overwrite)
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1
    except (BeatportError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    print(f"Downloaded FLAC to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
