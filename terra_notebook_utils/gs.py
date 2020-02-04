import io
import os
import sys
import json

import threading
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

from google.cloud.storage import Client
from google.oauth2 import service_account
from google.resumable_media.requests import ChunkedDownload
import google.auth

from terra_notebook_utils.xprofile import profile
from terra_notebook_utils.progress_bar import ProgressBar
import logging
logging.getLogger("google.resumable_media.requests.download").setLevel(logging.WARNING)

chunk_size = 1024 * 1024 * 32

def get_access_token():
    """
    Retrieve the access token using the default GCP account
    returns the same result as `gcloud auth print-access-token`
    """
    if os.environ.get("TERRA_NOTEBOOK_GOOGLE_ACCESS_TOKEN"):
        token = os.environ['TERRA_NOTEBOOK_GOOGLE_ACCESS_TOKEN']
    elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        from oauth2client.service_account import ServiceAccountCredentials
        scopes = ['https://www.googleapis.com/auth/userinfo.profile', 'https://www.googleapis.com/auth/userinfo.email']
        creds = ServiceAccountCredentials.from_json_keyfile_name(os.environ['GOOGLE_APPLICATION_CREDENTIALS'],
                                                                 scopes=scopes)
        token = creds.get_access_token().access_token
    else:
        import google.auth.transport.requests
        creds, projects = google.auth.default()
        creds.refresh(google.auth.transport.requests.Request())
        token = creds.token
    return token

def get_client(credentials_data: dict=None, project: str=None):
    kwargs = dict()
    if credentials_data is not None:
        creds = service_account.Credentials.from_service_account_info(credentials_data)
        kwargs['credentials'] = creds
    if project is not None:
        kwargs['project'] = project
    client = Client(**kwargs)
    if credentials_data is None:
        client._credentials.refresh(google.auth.transport.requests.Request())
    return client

class ChunkedDownloader:
    def __init__(self, blob):
        self.blob = blob
        self.part_numbers = list(range(1 + blob.size // chunk_size))

    def fetch_part(self, part_number):
        start_chunk = part_number * chunk_size
        end_chunk = start_chunk + chunk_size - 1
        fh = io.BytesIO()
        self.blob.download_to_file(fh, start=start_chunk, end=end_chunk)
        fh.seek(0)
        return fh.read()

def oneshot_copy(src_bucket, dst_bucket, src_key, dst_key):
    """
    Download an object into memory from `src_bucket` and upload it to `dst_bucket`
    """
    fh = io.BytesIO()
    src_bucket.blob(src_key).download_to_file(fh)
    fh.seek(0)
    dst_bucket.blob(dst_key).upload_from_file(fh)

def _compose_part_name(key, part_number):
    return "%spart%06i" % (key, part_number)

@profile("multipart_copy")
def multipart_copy(src_bucket, dst_bucket, src_key, dst_key):
    """
    Download an upjoct in chunks from `src_bucket` and upload each chunk to `dst_bucket` as separate objets.
    The objects are then composed into a single object, and the parts are deleted from `dst_bucket`.
    """
    src_blob = src_bucket.get_blob(src_key)
    print(f"Copying from {src_bucket.name}/{src_key}")
    print(f"Copying to {dst_bucket.name}/{dst_key}")
    progress_bar = ProgressBar(1 + src_blob.size // chunk_size, prefix="Copying:", suffix="Parts")
    downloader = ChunkedDownloader(src_blob)

    def _transfer_chunk(part_number):
        data = downloader.fetch_part(part_number)
        blob_name = _compose_part_name(dst_key, part_number)
        progress_bar.update()
        dst_bucket.blob(blob_name).upload_from_file(io.BytesIO(data))
        return blob_name

    with ThreadPoolExecutor(max_workers=12) as e:
        futures = [e.submit(_transfer_chunk, part_number) for part_number in downloader.part_numbers]
        dst_blob_names = [f.result() for f in as_completed(futures)]
    progress_bar.finish("composing parts...")
    _compose_parts(dst_bucket, sorted(dst_blob_names), dst_key)

def _iter_chunks(lst: list, size=32):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]

def _compose_parts(bucket, blob_names, dst_key):
    def _compose(names, dst_part_name):
        blobs = [bucket.get_blob(n) for n in names]
        dst_blob = bucket.blob(dst_part_name)
        dst_blob.compose(blobs)
        for blob in blobs:
            try:
                blob.delete()
            except Exception:
                pass
        return dst_part_name

    part_numbers = [len(blob_names)]
    while True:
        if 32 >= len(blob_names):
            _compose(blob_names, dst_key)
            break
        else:
            chunks = [ch for ch in _iter_chunks(blob_names)]
            part_numbers = range(part_numbers[-1], part_numbers[-1] + len(chunks))
            with ThreadPoolExecutor(max_workers=8) as e:
                futures = [e.submit(_compose, ch, _compose_part_name(dst_key, part_number))
                           for ch, part_number in zip(chunks, part_numbers)]
                blob_names = sorted([f.result() for f in as_completed(futures)])

def copy(src_bucket, dst_bucket, src_key, dst_key):
    src_blob = src_bucket.blob(src_key)
    src_blob.reload()
    if chunk_size >= src_blob.size:
        oneshot_copy(src_bucket, dst_bucket, src_key, dst_key)
    else:
        multipart_copy(src_bucket, dst_bucket, src_key, dst_key)
    src_blob.reload()
    dst_blob = dst_bucket.get_blob(dst_key)
    print("Finished copy", "size:", dst_blob.size)
    assert src_blob.crc32c == dst_blob.crc32c
