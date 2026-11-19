"""Copy closed day files to S3-compatible object storage (UpCloud Managed Object Storage), then
free the server's disk.

What goes up, under <prefix> in the bucket, with the same layout as on disk:
  book/<day>.jsonl.gz and markets/<day>.json   once the UTC day is over (never today's)
  trades/<condition_id>.jsonl.gz              whenever the local file changed since its last upload

A file counts as archived only when the bucket reports the same size as the local file. Only then
may a book day older than `keep_local_days` be deleted locally. The recorder's own size cap
(--max-gb) stays as the last line of defence if uploads stop working.

Credentials come from the environment, never from the repository (see deploy/book-recorder.env.example):
  BOOK_S3_ENDPOINT   e.g. https://xxxxx.upcloudobjects.com
  BOOK_S3_BUCKET     bucket name
  BOOK_S3_KEY_ID / BOOK_S3_SECRET   access key of a user limited to that bucket
  BOOK_S3_REGION     optional (UpCloud accepts any value, default "europe-1")
  BOOK_S3_PREFIX     optional, default "polymarket/"
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .store import write_json

log = logging.getLogger("book_recorder.offload")


def client_from_env():
    import boto3
    from botocore.config import Config
    missing = [v for v in ("BOOK_S3_ENDPOINT", "BOOK_S3_BUCKET", "BOOK_S3_KEY_ID", "BOOK_S3_SECRET")
               if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"missing environment variables: {', '.join(missing)}")
    client = boto3.client(
        "s3", endpoint_url=os.environ["BOOK_S3_ENDPOINT"],
        aws_access_key_id=os.environ["BOOK_S3_KEY_ID"], aws_secret_access_key=os.environ["BOOK_S3_SECRET"],
        region_name=os.environ.get("BOOK_S3_REGION", "europe-1"),
        config=Config(retries={"max_attempts": 5, "mode": "standard"}))
    return client, os.environ["BOOK_S3_BUCKET"], os.environ.get("BOOK_S3_PREFIX", "polymarket/")


class Offloader:
    def __init__(self, out, client, bucket: str, prefix: str = "polymarket/", keep_local_days: int = 7,
                 now: Optional[datetime] = None):
        self.out, self.client, self.bucket = Path(out), client, bucket
        self.prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        self.keep_local_days = keep_local_days
        self.now = now or datetime.now(timezone.utc)
        self.state_path = self.out / "offload_state.json"
        try:
            self.state: Dict[str, dict] = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}

    def _today(self) -> str:
        return self.now.strftime("%Y-%m-%d")

    def candidates(self) -> List[Path]:
        today, files = self._today(), []
        for sub, pattern in (("book", "*.jsonl.gz"), ("markets", "*.json")):
            for f in sorted((self.out / sub).glob(pattern)):
                if f.name.split(".")[0] < today:
                    files.append(f)
        files += sorted((self.out / "trades").glob("*.jsonl.gz"))
        return files

    def _changed(self, f: Path, rel: str) -> bool:
        st, s = self.state.get(rel), f.stat()
        return not st or st.get("size") != s.st_size or st.get("mtime") != int(s.st_mtime)

    def upload(self) -> Dict[str, int]:
        done = {"uploaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
        for f in self.candidates():
            rel = f.relative_to(self.out).as_posix()
            if not self._changed(f, rel):
                done["skipped"] += 1
                continue
            size, mtime = f.stat().st_size, int(f.stat().st_mtime)
            key = self.prefix + rel
            try:
                self.client.upload_file(str(f), self.bucket, key)
                remote = self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
                if remote != size:
                    raise RuntimeError(f"size mismatch after upload: local {size}, bucket {remote}")
            except Exception as e:
                log.error("%s: %s", rel, e)
                done["failed"] += 1
                continue
            self.state[rel] = {"size": size, "mtime": mtime, "uploaded": self.now.isoformat(timespec="seconds")}
            write_json(self.state_path, self.state)
            done["uploaded"] += 1
            done["bytes"] += size
        return done

    def prune_local(self) -> List[str]:
        """Delete book days older than keep_local_days that the bucket holds at their current size."""
        if self.keep_local_days is None or self.keep_local_days < 0:
            return []
        limit = (self.now - timedelta(days=self.keep_local_days)).strftime("%Y-%m-%d")
        deleted = []
        for f in sorted((self.out / "book").glob("*.jsonl.gz")):
            day = f.name.split(".")[0]
            rel = f.relative_to(self.out).as_posix()
            if day >= limit or self._changed(f, rel):
                continue
            f.unlink()
            deleted.append(rel)
            log.info("deleted local %s (archived)", rel)
        return deleted


def fetch(client, bucket: str, prefix: str, dest, days: List[str]) -> List[str]:
    """Download archived book and market files of the given days, e.g. to a research machine."""
    got = []
    for day in days:
        for rel in (f"book/{day}.jsonl.gz", f"markets/{day}.json"):
            target = Path(dest) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                client.download_file(bucket, prefix + rel, str(target))
                got.append(rel)
            except Exception as e:
                log.error("%s: %s", rel, e)
    return got
