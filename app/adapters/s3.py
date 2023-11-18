from __future__ import annotations

import logging
from typing import Any

import botocore.exceptions

import app.clients
import settings


async def upload(
    body: bytes,
    file_name: str,
    folder: str,
    content_type: str | None = None,
    acl: str | None = None,
) -> None:
    params: dict[str, Any] = {
        "Bucket": settings.AWS_BUCKET_NAME,
        "Key": f"{folder}/{file_name}",
        "Body": body,
    }
    if content_type is not None:
        params["ContentType"] = content_type
    if acl is not None:
        params["ACL"] = acl

    try:
        await app.clients.s3_client.put_object(**params)
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
        logging.error("Failed to upload file to S3", exc_info=exc)
        return None

    return None


async def download(file_name: str, folder: str) -> bytes | None:
    try:
        response = await app.clients.s3_client.get_object(
            Bucket=settings.AWS_BUCKET_NAME,
            Key=f"{folder}/{file_name}",
        )
    except app.clients.s3_client.exceptions.NoSuchKey:
        return None
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
        logging.error("Failed to download file from S3", exc_info=exc)
        return None

    return await response["Body"].read()
