"""Minimal Cloudflare R2 client (S3 API, AWS Signature V4), standard library only."""
import datetime
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request


class R2Error(Exception):
    pass


def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(method, url, headers, body, key_id, secret, amz_date, region="auto"):
    """Returns the headers to send for a SigV4-signed S3 request (urllib adds Host)."""
    parsed = urllib.parse.urlparse(url)
    day = amz_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    hdrs = {"host": parsed.netloc, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    signed = ";".join(sorted(hdrs))
    canonical = "\n".join([
        method, parsed.path, parsed.query,
        "".join(f"{k}:{str(hdrs[k]).strip()}\n" for k in sorted(hdrs)),
        signed, payload_hash,
    ])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), day), region), "s3"), "aws4_request")
    hdrs["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={signed}, "
        f"Signature={hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()}"
    )
    del hdrs["host"]
    return hdrs


def _describe(status, body, what):
    code = (re.search(rb"<Code>([^<]+)</Code>", body or b"") or [None, b""])[1].decode()
    if status in (401, 403):
        return f"{what}: access denied ({code or status}). Check the R2 token and that it can read/write this bucket."
    if status == 412:
        return f"{what}: changed on the server since your last pull (probably in the admin). Run `pull` again and redo the edit."
    return f"{what}: HTTP {status} {code}".strip()


class R2:
    def __init__(self, endpoint, key_id, secret):
        self.endpoint, self.key_id, self.secret = endpoint.rstrip("/"), key_id, secret

    def request(self, method, bucket, key="", body=b"", headers=None, query=""):
        url = f"{self.endpoint}/{bucket}/{urllib.parse.quote(key)}" + (f"?{query}" if query else "")
        amz_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        hdrs = sign(method, url, headers, body, self.key_id, self.secret, amz_date)
        req = urllib.request.Request(url, data=body if method == "PUT" else None, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, res.headers, res.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()
        except urllib.error.URLError as err:
            raise R2Error(f"Cannot reach R2 ({err.reason}). Check network access to *.r2.cloudflarestorage.com.")

    def get_json(self, bucket, key):
        """(data, etag), or (None, None) when the object doesn't exist."""
        status, headers, body = self.request("GET", bucket, key)
        if status == 404:
            return None, None
        if status != 200:
            raise R2Error(_describe(status, body, f"Reading {bucket}/{key}"))
        return json.loads(body.decode("utf-8")), headers.get("ETag")

    def etag(self, bucket, key):
        status, headers, body = self.request("HEAD", bucket, key)
        if status == 404:
            return None
        if status != 200:
            raise R2Error(_describe(status, body, f"Checking {bucket}/{key}"))
        return headers.get("ETag")

    def put(self, bucket, key, body, content_type, cache_control=None, if_match=None, if_absent=False):
        headers = {"Content-Type": content_type}
        if cache_control:
            headers["Cache-Control"] = cache_control
        if if_match:
            headers["If-Match"] = if_match
        elif if_absent:
            headers["If-None-Match"] = "*"
        status, res_headers, res_body = self.request("PUT", bucket, key, body, headers)
        if status not in (200, 201):
            raise R2Error(_describe(status, res_body, f"Writing {bucket}/{key}"))
        return res_headers.get("ETag")

    def delete(self, bucket, key):
        status, _, body = self.request("DELETE", bucket, key)
        if status not in (200, 204, 404):
            raise R2Error(_describe(status, body, f"Deleting {bucket}/{key}"))

    def _list(self, bucket, prefix, folders):
        params = {"list-type": "2", "prefix": prefix}
        if folders:
            params["delimiter"] = "/"
        # SigV4 needs the query sorted by name and fully encoded ("/" -> %2F).
        query = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote)
        status, _, body = self.request("GET", bucket, "", query=query)
        if status != 200:
            raise R2Error(_describe(status, body, f"Listing {bucket}/{prefix}"))
        tag = rb"<Prefix>([^<]+)</Prefix>" if folders else rb"<Key>([^<]+)</Key>"
        return sorted(m.decode() for m in re.findall(tag, body) if m.decode() != prefix)

    def list_keys(self, bucket, prefix):
        """Object keys under `prefix` (first 1000)."""
        return self._list(bucket, prefix, folders=False)

    def list_folders(self, bucket, prefix):
        """Folder names directly under `prefix`, like `ls`."""
        return [p[len(prefix):].rstrip("/") for p in self._list(bucket, prefix, folders=True)]
