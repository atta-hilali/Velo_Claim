from __future__ import annotations

import os

from velo_claim.storage.interfaces import ObjectStoreInterface


class S3ObjectStore(ObjectStoreInterface):
    """S3/MinIO object store implementation using boto3."""

    def __init__(
        self,
        *,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        prefix: str = "",
    ) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("Install boto3 to use S3ObjectStore: pip install boto3") from exc
        self.bucket = bucket or os.getenv("OBJECT_STORE_BUCKET", "velo-claim")
        self.prefix = prefix.strip("/")
        self.client = boto3.client("s3", endpoint_url=endpoint_url or os.getenv("OBJECT_STORE_ENDPOINT_URL") or None)

    def put_bytes(self, key: str, value: bytes, content_type: str = "application/octet-stream") -> str:
        full_key = "/".join(part for part in [self.prefix, key.strip("/")] if part)
        self.client.put_object(
            Bucket=self.bucket,
            Key=full_key,
            Body=value,
            ContentType=content_type,
        )
        return f"s3://{self.bucket}/{full_key}"

    def put_text(self, key: str, value: str, content_type: str = "text/plain") -> str:
        return self.put_bytes(key, value.encode("utf-8"), content_type)

    def get_bytes(self, uri: str) -> bytes:
        bucket, key = _parse_s3_uri(uri)
        response = self.client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    def get_text(self, uri: str) -> str:
        return self.get_bytes(uri).decode("utf-8")


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"Expected s3:// URI, got {uri}")
    rest = uri[len("s3://") :]
    bucket, key = rest.split("/", 1)
    return bucket, key
