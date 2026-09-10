"""Official SEC Form 13F Data Sets acquisition and safe archive validation."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarError, SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, HttpTransport, UrlLibHttpTransport

SEC_13F_DATA_SETS_CATALOG_URL = (
    "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
)
_MAX_CATALOG_PAGE_BYTES = 2 * 1024 * 1024  # 2 MiB
_MAX_ZIP_BYTES = 160 * 1024 * 1024  # 160 MiB
_MAX_UNCOMPRESSED_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB
_MAX_ZIP_MEMBERS = 16
_MAX_COMPRESSION_RATIO = 100

ALLOWED_ZIP_MEMBERS = frozenset(
    {
        "SUBMISSION.tsv",
        "COVERPAGE.tsv",
        "INFOTABLE.tsv",
        "OTHERMANAGER.tsv",
        "OTHERMANAGER2.tsv",
        "SIGNATURE.tsv",
        "SUMMARYPAGE.tsv",
        "FORM13F_metadata.json",
        "FORM13F_readme.htm",
        "readme.htm",
        "readme.txt",
    }
)
REQUIRED_ZIP_MEMBERS = frozenset({"SUBMISSION.tsv", "COVERPAGE.tsv", "INFOTABLE.tsv"})
ALLOWED_SUBMISSION_FORMS = frozenset({"13F-HR", "13F-HR/A"})

_ZIP_FILENAME_PATTERN = re.compile(
    r"^(\d{2}[a-z]{3}\d{4})-(\d{2}[a-z]{3}\d{4})_form13f\.zip$",
    re.IGNORECASE,
)
_HREF_PATTERN = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

_MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


class Sec13FDataSetError(SecEdgarError):
    """Failure acquiring, validating, or parsing official Form 13F Data Sets."""


def parse_sec_dataset_token_date(token: str) -> date:
    """Parse DDmonYYYY like 01mar2026 into a date object."""
    cleaned = token.strip().lower()
    if len(cleaned) != 9:
        raise Sec13FDataSetError(f"Invalid date token in dataset filename: {token}")
    day_str = cleaned[:2]
    mon_str = cleaned[2:5]
    year_str = cleaned[5:]
    if not (day_str.isdigit() and year_str.isdigit() and mon_str in _MONTH_MAP):
        raise Sec13FDataSetError(f"Invalid date format in dataset filename: {token}")
    try:
        return date(int(year_str), _MONTH_MAP[mon_str], int(day_str))
    except ValueError as error:
        raise Sec13FDataSetError(f"Invalid calendar date in dataset filename: {token}") from error


def parse_sec_date_value(value: str) -> date:
    """Parse date from TSV values such as DD-MON-YYYY or YYYY-MM-DD."""
    cleaned = value.strip()
    if not cleaned:
        raise Sec13FDataSetError("Empty date value in dataset TSV")
    if "-" in cleaned:
        parts = cleaned.split("-")
        if len(parts) == 3:
            if (
                len(parts[0]) == 4
                and parts[0].isdigit()
                and parts[1].isdigit()
                and parts[2].isdigit()
            ):
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
            if (
                len(parts[0]) <= 2
                and parts[0].isdigit()
                and parts[1].lower() in _MONTH_MAP
                and parts[2].isdigit()
            ):
                return date(int(parts[2]), _MONTH_MAP[parts[1].lower()], int(parts[0]))
    raise Sec13FDataSetError(f"Unrecognized date format in dataset TSV: {value}")


@dataclass(frozen=True, slots=True)
class Sec13FDataSetLink:
    """One validated dataset link discovered on the official catalog page."""

    url: str
    period_start: date
    period_end: date
    filename: str


@dataclass(frozen=True, slots=True)
class Sec13FDataSetDownload:
    """Validated raw download of one Form 13F dataset archive."""

    url: str
    period_start: date
    period_end: date
    content: bytes
    sha256: str
    size_bytes: int
    retrieved_at: datetime


class Sec13FDataSetsClient:
    """Client for acquiring official Form 13F Data Sets strictly from sec.gov."""

    def __init__(
        self,
        identity: SecEdgarIdentity,
        transport: HttpTransport | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_seconds: float = 60.0,
    ) -> None:
        self._identity = identity
        self._transport = transport or UrlLibHttpTransport()
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    def discover_catalog_links(
        self, html_content: str, catalog_url: str = SEC_13F_DATA_SETS_CATALOG_URL
    ) -> tuple[Sec13FDataSetLink, ...]:
        """Parse and validate dataset archive links from the catalog HTML."""
        links_by_period: dict[tuple[date, date], Sec13FDataSetLink] = {}
        parsed_catalog = urlsplit(catalog_url)
        if parsed_catalog.hostname != "www.sec.gov":
            raise Sec13FDataSetError(f"Catalog URL must reside on www.sec.gov: {catalog_url}")

        for match in _HREF_PATTERN.finditer(html_content):
            href = match.group(1).strip()
            parsed_href = urlsplit(href)
            if parsed_href.scheme and (
                parsed_href.scheme != "https" or parsed_href.hostname != "www.sec.gov"
            ):
                continue
            path = parsed_href.path
            if not path.startswith("/files/structureddata/data/form-13f-data-sets/"):
                continue
            filename = path.rsplit("/", 1)[-1]
            file_match = _ZIP_FILENAME_PATTERN.match(filename)
            if not file_match:
                continue

            start_str, end_str = file_match.group(1), file_match.group(2)
            period_start = parse_sec_dataset_token_date(start_str)
            period_end = parse_sec_dataset_token_date(end_str)

            if period_start > period_end:
                raise Sec13FDataSetError(
                    f"Invalid period range in link {filename}: {period_start} > {period_end}"
                )

            full_url = f"https://www.sec.gov{path}"
            period_key = (period_start, period_end)
            if period_key in links_by_period:
                existing = links_by_period[period_key]
                if existing.url != full_url:
                    raise Sec13FDataSetError(
                        f"Duplicate contradictory link for period {period_key}: "
                        f"{existing.url} vs {full_url}"
                    )
                raise Sec13FDataSetError(
                    f"Duplicate identical link in catalog page for period {period_key}"
                )

            links_by_period[period_key] = Sec13FDataSetLink(
                url=full_url,
                period_start=period_start,
                period_end=period_end,
                filename=filename,
            )

        if not links_by_period:
            raise Sec13FDataSetError("No valid Form 13F dataset links found in catalog page")

        # Sort descending by period_end, then period_start
        sorted_links = tuple(
            sorted(
                links_by_period.values(),
                key=lambda item: (item.period_end, item.period_start),
                reverse=True,
            )
        )
        return sorted_links

    def fetch_catalog_page(self, url: str = SEC_13F_DATA_SETS_CATALOG_URL) -> str:
        """Fetch the official catalog HTML with strict validation."""
        if url != SEC_13F_DATA_SETS_CATALOG_URL:
            raise Sec13FDataSetError("Only the official Form 13F catalog URL is permitted")

        headers = {
            "User-Agent": self._identity.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        }
        response: HttpResponse = self._transport.get(
            url,
            headers=headers,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=_MAX_CATALOG_PAGE_BYTES,
        )
        if response.status_code != 200:
            raise Sec13FDataSetError(
                f"Catalog page request failed with HTTP {response.status_code}"
            )
        if response.body_truncated or not response.body:
            raise Sec13FDataSetError("Catalog page response is empty or exceeded the 2 MiB limit")

        parsed_final = urlsplit(response.url)
        if parsed_final.hostname != "www.sec.gov":
            raise Sec13FDataSetError(f"Catalog page redirected away from sec.gov to {response.url}")

        content_type = response.headers.get(
            "Content-Type", response.headers.get("content-type", "")
        ).casefold()
        if (
            content_type
            and "text/html" not in content_type
            and "application/xhtml+xml" not in content_type
        ):
            raise Sec13FDataSetError(f"Unexpected Content-Type for catalog page: {content_type}")

        try:
            return response.body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise Sec13FDataSetError("Catalog page body is not valid UTF-8") from error

    def fetch_dataset_archive(self, link: Sec13FDataSetLink) -> Sec13FDataSetDownload:
        """Download and strictly validate one Form 13F dataset ZIP archive."""
        parsed_url = urlsplit(link.url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "www.sec.gov"
            or not parsed_url.path.startswith("/files/structureddata/data/form-13f-data-sets/")
        ):
            raise Sec13FDataSetError(f"Invalid dataset download URL: {link.url}")

        headers = {
            "User-Agent": self._identity.user_agent,
            "Accept": "application/zip,application/octet-stream",
        }
        response: HttpResponse = self._transport.get(
            link.url,
            headers=headers,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=_MAX_ZIP_BYTES,
        )
        if response.status_code != 200:
            raise Sec13FDataSetError(f"Dataset download failed with HTTP {response.status_code}")
        if response.body_truncated or not response.body:
            raise Sec13FDataSetError(
                "Dataset archive download is empty or exceeded the 160 MiB limit"
            )

        parsed_final = urlsplit(response.url)
        if parsed_final.hostname != "www.sec.gov":
            raise Sec13FDataSetError(
                f"Dataset download redirected away from sec.gov to {response.url}"
            )

        content_type = response.headers.get(
            "Content-Type", response.headers.get("content-type", "")
        ).casefold()
        if content_type and any(
            invalid in content_type for invalid in ("text/html", "application/json", "text/plain")
        ):
            raise Sec13FDataSetError(f"Unexpected Content-Type for dataset archive: {content_type}")

        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise Sec13FDataSetError(
                "Clock returned naive datetime; timezone-aware UTC is required"
            )
        retrieved_at = retrieved_at.astimezone(UTC)

        content = response.body
        validate_sec_13f_zip_archive(content)

        return Sec13FDataSetDownload(
            url=link.url,
            period_start=link.period_start,
            period_end=link.period_end,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            retrieved_at=retrieved_at,
        )

    def fetch_latest_dataset(self) -> Sec13FDataSetDownload:
        """Discover the catalog, select the latest period, and download its ZIP archive."""
        html = self.fetch_catalog_page()
        links = self.discover_catalog_links(html)
        latest_link = links[0]
        return self.fetch_dataset_archive(latest_link)


def validate_sec_13f_zip_archive(content: bytes) -> None:
    """Perform fail-closed security and structural validation on the ZIP archive bytes."""
    if not content:
        raise Sec13FDataSetError("ZIP content is empty")
    if len(content) > _MAX_ZIP_BYTES:
        raise Sec13FDataSetError(
            f"ZIP archive size {len(content)} exceeds maximum limit of {_MAX_ZIP_BYTES}"
        )

    stream = io.BytesIO(content)
    if not zipfile.is_zipfile(stream):
        raise Sec13FDataSetError("Content is not a valid ZIP archive")

    with zipfile.ZipFile(stream, "r") as archive:
        infolist = archive.infolist()
        if len(infolist) > _MAX_ZIP_MEMBERS:
            raise Sec13FDataSetError(
                f"ZIP archive has {len(infolist)} members, exceeding limit of {_MAX_ZIP_MEMBERS}"
            )

        seen_names: set[str] = set()
        total_uncompressed = 0

        for info in infolist:
            # Check for encryption
            if info.flag_bits & 0x1:
                raise Sec13FDataSetError(f"Encrypted ZIP member is prohibited: {info.filename}")

            name = info.filename
            # Path traversal checks
            if (
                name.startswith(("/", "\\"))
                or ".." in name
                or "\\" in name
                or name.startswith("..")
            ):
                raise Sec13FDataSetError(f"Path traversal detected in ZIP member: {name}")

            lower_name = name.lower()
            if lower_name in seen_names:
                raise Sec13FDataSetError(f"Duplicate member in ZIP archive: {name}")
            seen_names.add(lower_name)

            if name not in ALLOWED_ZIP_MEMBERS:
                raise Sec13FDataSetError(f"Unexpected member in official ZIP archive: {name}")

            total_uncompressed += info.file_size
            if total_uncompressed > _MAX_UNCOMPRESSED_TOTAL_BYTES:
                raise Sec13FDataSetError(
                    "Total uncompressed size of ZIP archive exceeds 1 GiB limit"
                )

            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > _MAX_COMPRESSION_RATIO:
                    raise Sec13FDataSetError(
                        f"Pathological compression ratio ({ratio:.1f}) in member {name}"
                    )

        member_set = {info.filename for info in infolist}
        missing_required = REQUIRED_ZIP_MEMBERS - member_set
        if missing_required:
            raise Sec13FDataSetError(
                f"ZIP archive missing required member tables: {sorted(missing_required)}"
            )
