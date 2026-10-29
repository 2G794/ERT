
import requests
import pymupdf
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

PATENT = "DD000000000001A1"

URL = (
    "https://depatisnet.dpma.de/DepatisNet/depatisnet"
    f"?window=1&space=main&content=treffer"
    f"&action=pdf&docid={PATENT}"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0"
})


def get_viewer_pdf_url(page_url, referer=None):
    headers = {}
    if referer:
        headers["Referer"] = referer

    r = session.get(
        page_url,
        headers=headers,
        timeout=30
    )
    r.raise_for_status()

    if "captcha" in r.text.lower():
        raise RuntimeError("CAPTCHA encountered")

    soup = BeautifulSoup(r.content, "html.parser")

    iframe = soup.find("iframe")
    if not iframe:
        raise RuntimeError("No iframe found")

    iframe_url = urljoin(r.url, iframe["src"])

    # The first iframe may contain another iframe.
    if "pdf_menu3.jsp" in iframe_url:
        r2 = session.get(
            iframe_url,
            headers={"Referer": r.url},
            timeout=30
        )
        r2.raise_for_status()

        soup2 = BeautifulSoup(r2.content, "html.parser")
        iframe2 = soup2.find(
            "iframe",
            id="dpma-sa-pdf-iframe"
        )

        if not iframe2:
            raise RuntimeError("PDF viewer not found")

        iframe_url = urljoin(r2.url, iframe2["src"])
        referer = r2.url
    else:
        referer = r.url

    params = parse_qs(urlparse(iframe_url).query)
    pdf_urls = params.get("file", [])

    if not pdf_urls:
        raise RuntimeError("No PDF URL in viewer")

    return pdf_urls[0], referer


def inspect_pdf(url, referer):
    with session.get(
        url,
        headers={"Referer": referer},
        stream=True,
        timeout=60
    ) as r:
        r.raise_for_status()

        print("Content-Type:", r.headers.get("Content-Type"))

        data = bytearray()

        for chunk in r.iter_content(65536):
            data.extend(chunk)
            if len(data) > 80 * 1024 * 1024:
                raise RuntimeError("PDF exceeds 80 MiB")

    if not data.startswith(b"%PDF-"):
        raise RuntimeError(
            "Server returned HTML instead of PDF"
        )

    with pymupdf.open(
        stream=bytes(data),
        filetype="pdf"
    ) as doc:
        count = len(doc)
        chars = sum(len(p.get_text()) for p in doc)

    print("PDF size:", len(data), "bytes")
    print("PDF pages:", count)
    print("Embedded text characters:", chars)

    return count


try:
    print("\n=== TEST 1: FIRST PAGE ===")

    pdf_url, referer = get_viewer_pdf_url(URL)
    first_pages = inspect_pdf(pdf_url, referer)

    print("\n=== TEST 2: COMPLETE DOCUMENT ===")

    # Ask DEPATISnet to load the full document.
    full_url = URL + "&xxxfull=1"

    full_pdf_url, full_referer = get_viewer_pdf_url(
        full_url,
        referer=URL
    )

    print("Full PDF URL:", full_pdf_url)

    full_pages = inspect_pdf(
        full_pdf_url,
        full_referer
    )

    print("\n=== RESULT ===")
    print("First request pages:", first_pages)
    print("Full request pages:", full_pages)

    if full_pages > first_pages:
        print("SUCCESS: Full-document mode works!")
    else:
        print(
            "Full-document mode did not increase page count."
        )

finally:
    session.close()
