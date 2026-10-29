"""DDR patent PDFs: fetch in memory, OCR pages with EasyOCR, persist text and progress only."""
import argparse
import csv
import io
import logging
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin, parse_qs, parse_qsl, urlencode, urlunparse

import pymupdf as fitz
import numpy as np
import requests
from bs4 import BeautifulSoup
from PIL import Image

LOG = logging.getLogger('ddr_ocr')


def load_csv(path, limit):
    with open(path, 'r', encoding='cp1252', newline='') as f:
        reader = csv.DictReader(f, delimiter=';')
        required = {'veroeffentlichungsnummer', 'pdfurl', 'recherchierbarertexturl'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('CSV missing fields: ' + str(required - set(reader.fieldnames or [])))
        rows = []
        for row in reader:
            identifier = (row['veroeffentlichungsnummer'] or '').strip()
            if not identifier:
                continue
            rows.append((identifier, (row['pdfurl'] or '').strip(),
                         (row['recherchierbarertexturl'] or '').strip()))
            if limit and len(rows) >= limit:
                break
        return rows


def validate_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'depatisnet.dpma.de':
        raise ValueError('Unexpected host or invalid HTTPS URL')


class AccessRestricted(RuntimeError):
    pass


def check_response(response):
    validate_url(response.url)
    if response.status_code in (401, 403, 429):
        raise AccessRestricted(f'Access restriction HTTP {response.status_code}')
    response.raise_for_status()


def get_viewer_pdf_url(session, page_url, timeout=60, referer=None):
    """Adapted from test_pdf_connection.py, preserving its viewer flow."""
    validate_url(page_url)
    headers = {'Referer': referer} if referer else {}
    response = session.get(page_url, headers=headers, timeout=(15, timeout))
    check_response(response)
    if response.content.startswith(b'%PDF-'):
        return response.url, referer
    soup = BeautifulSoup(response.content, 'html.parser')
    iframe = soup.find('iframe')
    if not iframe or not iframe.get('src'):
        visible = soup.get_text(' ', strip=True).lower()
        if ('captcha' in visible or 'verify you are human' in visible or
                soup.select_one('input[name*="captcha" i]')):
            raise AccessRestricted('Challenge page presented instead of PDF viewer')
        raise ValueError('No iframe in viewer')
    iframe_url = urljoin(response.url, iframe['src'])
    validate_url(iframe_url)
    if 'pdf_menu3.jsp' in iframe_url:
        response2 = session.get(iframe_url, headers={'Referer': response.url},
                                timeout=(15, timeout))
        check_response(response2)
        soup2 = BeautifulSoup(response2.content, 'html.parser')
        iframe2 = soup2.find('iframe', id='dpma-sa-pdf-iframe')
        if not iframe2 or not iframe2.get('src'):
            raise ValueError('PDF viewer iframe missing from menu')
        iframe_url = urljoin(response2.url, iframe2['src'])
        referer = response2.url
    else:
        referer = response.url
    validate_url(iframe_url)
    params = parse_qs(urlparse(iframe_url).query)
    pdf_urls = params.get('file', [])
    if not pdf_urls:
        raise ValueError('No file parameter in PDF viewer')
    pdf_url = urljoin(iframe_url, pdf_urls[0])
    validate_url(pdf_url)
    return pdf_url, referer


def full_document_url(url):
    """Request the complete patent document once."""
    parts = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k.lower() != 'xxxfull']
    params.append(('xxxfull', '1'))
    return urlunparse(parts._replace(query=urlencode(params)))


def fetch_pdf_bytes(session, actual_url, referer, max_mb=60, timeout=60):
    """Download in RAM; verify bytes before passing to PyMuPDF."""
    headers = {'Referer': referer} if referer else {}
    with session.get(actual_url, headers=headers, stream=True,
                     timeout=(15, timeout)) as response:
        check_response(response)
        data = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                data.extend(chunk)
                if len(data) > max_mb * 1024 * 1024:
                    raise ValueError(f'PDF exceeds {max_mb} MiB')
    if not data.startswith(b'%PDF-'):
        if b'captcha' in data[:20000].lower():
            raise AccessRestricted('PDF endpoint returned a challenge page')
        raise ValueError('PDF endpoint did not return PDF bytes')
    return bytes(data)


def fetch_pdf(session, url, max_mb=60, timeout=60, full=True, test_sequence=False):
    """Obtain a complete PDF using the tested iframe flow. No PDF files saved."""
    validate_url(url)
    if test_sequence and full:
        # Reproduce TEST 1 then TEST 2, with one extra PDF transfer.
        first_url, first_ref = get_viewer_pdf_url(session, url, timeout)
        first_blob = fetch_pdf_bytes(session, first_url, first_ref, max_mb, timeout)
        with fitz.open(stream=first_blob, filetype='pdf') as first_doc:
            LOG.info('Test sequence: first request had %s page(s)', len(first_doc))
        del first_blob
    target = full_document_url(url) if full else url
    actual_url, referer = get_viewer_pdf_url(
        session, target, timeout, referer=url if full else None)
    LOG.info('Resolved PDF endpoint: %s', actual_url)
    return fetch_pdf_bytes(session, actual_url, referer, max_mb, timeout)


def create_db(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE IF NOT EXISTS documents (
        patent_id TEXT PRIMARY KEY, pdf_url TEXT, textpdf_url TEXT,
        total_pages INTEGER, status TEXT, error TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    db.execute('''CREATE TABLE IF NOT EXISTS pages (
        patent_id TEXT, page_no INTEGER, text TEXT, status TEXT,
        error TEXT, PRIMARY KEY (patent_id,page_no))''')
    db.commit()
    return db


def save_page(db, identifier, number, text, status, error=''):
    db.execute('''INSERT INTO pages(patent_id,page_no,text,status,error)
                  VALUES(?,?,?,?,?) ON CONFLICT(patent_id,page_no)
                  DO UPDATE SET text=excluded.text,status=excluded.status,error=excluded.error''',
               (identifier, number, text, status, error))
    db.commit()


def export_csv(db, target):
    with open(target, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['patent_id', 'pdf_url', 'textpdf_url', 'total_pages',
                         'pages_done', 'pages_failed', 'status', 'error', 'ocr_full_text'])
        for identifier, pdf_url, text_url, n, status, err in db.execute('''
            SELECT patent_id,pdf_url,textpdf_url,total_pages,status,error
            FROM documents ORDER BY patent_id'''):
            pages = db.execute('SELECT status,text FROM pages WHERE patent_id=? ORDER BY page_no',
                               (identifier,)).fetchall()
            done = sum(s == 'done' for s, _ in pages)
            failed = sum(s == 'failed' for s, _ in pages)
            full = '\n\n'.join(t or '' for _, t in pages)
            writer.writerow([identifier, pdf_url, text_url, n, done, failed, status, err, full])


def ocr_page(reader, pdf_page, dpi):
    pix = pdf_page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
    img = Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
    arr = np.asarray(img)
    lines = reader.readtext(arr, detail=0, paragraph=False)
    return '\n'.join(lines)


def run(args):
    # Import EasyOCR only after an actual PDF page is available.
    base = Path(__file__).resolve().parent
    if not Path(args.csv).is_absolute():
        args.csv = str(base / args.csv)
    if not Path(args.output).is_absolute():
        args.output = str(base / args.output)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    db = create_db(output / 'progress.sqlite3')
    rows = load_csv(args.csv, args.limit)
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    reader = None
    try:
        for idx, (identifier, pdf_url, text_url) in enumerate(rows, 1):
            if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
                LOG.warning('Skipping unsafe identifier %r', identifier)
                continue
            record = db.execute('SELECT total_pages FROM documents WHERE patent_id=?',
                                (identifier,)).fetchone()
            if record and record[0] and not args.force:
                statuses = db.execute('SELECT status FROM pages WHERE patent_id=?', (identifier,)).fetchall()
                done_count = sum(x[0] == 'done' for x in statuses)
                if done_count == record[0]:
                    LOG.info('[%s/%s] SKIP already done: %s', idx, len(rows), identifier)
                    continue
            db.execute('''INSERT INTO documents(patent_id,pdf_url,textpdf_url,status)
                          VALUES(?,?,?,'pending') ON CONFLICT(patent_id) DO UPDATE SET
                          pdf_url=excluded.pdf_url,textpdf_url=excluded.textpdf_url''',
                       (identifier, pdf_url, text_url))
            db.commit()
            LOG.info('[%s/%s] %s', idx, len(rows), identifier)
            try:
                if not pdf_url:
                    raise ValueError('Missing pdfurl')
                validate_url(pdf_url)
                blob = None
                for attempt in range(args.http_retries + 1):
                    try:
                        blob = fetch_pdf(session, pdf_url, args.max_mb, args.timeout, full=not args.first_page_only,
                                          test_sequence=args.test_sequence)
                        break
                    except (requests.RequestException, ValueError) as exc:
                        if attempt == args.http_retries:
                            raise
                        LOG.warning('HTTP retry: %s', exc)
                        time.sleep(2 ** attempt)
                with fitz.open(stream=blob, filetype='pdf') as doc:
                    total = len(doc)
                    LOG.info('Downloaded PDF has %d page(s). Check completeness against DEPATISnet viewer.', total)
                    db.execute('UPDATE documents SET total_pages=?,status=? WHERE patent_id=?',
                               (total, 'running', identifier))
                    db.commit()
                    for number in range(1, total + 1):
                        previous = db.execute('SELECT status FROM pages WHERE patent_id=? AND page_no=?',
                                              (identifier, number)).fetchone()
                        if previous and previous[0] == 'done' and not args.force:
                            continue
                        if previous and previous[0] == 'failed' and not (args.retry_failed or args.force):
                            continue
                        if reader is None:
                            LOG.info('Initializing EasyOCR German + English model')
                            import easyocr
                            reader = easyocr.Reader(['de', 'en'], gpu=args.gpu)
                        last_exc = None
                        for attempt in range(args.ocr_retries + 1):
                            try:
                                text = ocr_page(reader, doc[number - 1], args.dpi)
                                save_page(db, identifier, number, text, 'done')
                                last_exc = None
                                break
                            except Exception as exc:
                                last_exc = exc
                                if attempt < args.ocr_retries:
                                    time.sleep(2 ** attempt)
                        if last_exc is not None:
                            save_page(db, identifier, number, '', 'failed', str(last_exc))
                            LOG.error('  Page %s failed: %s', number, last_exc)
                        else:
                            LOG.info('  Page %s/%s OCR done', number, total)
                del blob
                counts = db.execute('SELECT status,COUNT(*) FROM pages WHERE patent_id=? GROUP BY status',
                                    (identifier,)).fetchall()
                counter = dict(counts)
                new_status = 'done' if counter.get('done', 0) == total else 'partial'
                db.execute('UPDATE documents SET status=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE patent_id=?',
                           (new_status, identifier))
                db.commit()
            except Exception as exc:
                LOG.error('  %s: %s', type(exc).__name__, exc)
                db.execute('UPDATE documents SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE patent_id=?',
                           ('failed', str(exc), identifier))
                db.commit()
                if isinstance(exc, AccessRestricted):
                    LOG.error('Stopping because the server denied access')
                    break
            if idx % args.export_every == 0:
                export_csv(db, output / 'ocr_results.csv')
            if idx < len(rows):
                time.sleep(args.delay)
    finally:
        export_csv(db, output / 'ocr_results.csv')
        db.close()
        session.close()
    LOG.info('Results: %s', output / 'ocr_results.csv')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', default='DDR Patente_Regionaldaten GESIS_final.csv')
    parser.add_argument('--output', default='ocr_output')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--dpi', type=int, default=220)
    parser.add_argument('--delay', type=float, default=3.0)
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--max-mb', type=int, default=60)
    parser.add_argument('--http-retries', type=int, default=2)
    parser.add_argument('--ocr-retries', type=int, default=1)
    parser.add_argument('--export-every', type=int, default=5)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--test-sequence', action='store_true',
                        help='Reproduce test script: fetch first PDF before full PDF')
    parser.add_argument('--first-page-only', action='store_true',
                        help='Debug option: do not request xxxfull=1')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    run(args)
