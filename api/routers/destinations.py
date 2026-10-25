"""CRUD for relay destinations. Stream keys are encrypted at rest and never returned."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from api.deps import DbDep, RedisDep, SecretBoxDep, current_user
from api.schemas import (
    DestinationCreate,
    DestinationOut,
    DestinationRuntime,
    DestinationUpdate,
)
from api.services.destinations import (
    effective_status,
    get_stream_key,
    list_destinations,
    load_runtime,
    mask_key,
    set_stream_key,
)
from core.events import publish_event
from core.logging import get_logger
from core.models import Destination, utcnow
from core.redis import EVENT_DESTINATIONS_CHANGED

router = APIRouter(
    prefix="/api/v1/destinations", tags=["destinations"], dependencies=[Depends(current_user)]
)
log = get_logger(__name__)


def _to_out(
    dest: Destination, box: SecretBoxDep, runtime: dict[str, object] | None
) -> DestinationOut:
    assert dest.id is not None
    rt = DestinationRuntime.model_validate(runtime) if runtime else None
    return DestinationOut(
        id=dest.id,
        name=dest.name,
        platform=dest.platform,  # stored as str, validated on write
        rtmp_url=dest.rtmp_url,
        enabled=dest.enabled,
        profile=dest.profile,
        ffmpeg_args=dest.ffmpeg_args,
        stream_key_masked=mask_key(get_stream_key(dest, box)),
        status=effective_status(dest, runtime),
        last_error=(rt.last_error if rt else None) or dest.last_error,
        runtime=rt,
        created_at=dest.created_at,
        updated_at=dest.updated_at,
    )


def _get_or_404(session: DbDep, dest_id: int) -> Destination:
    dest = session.get(Destination, dest_id)
    if dest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="destination not found")
    return dest


@router.get("", response_model=list[DestinationOut])
async def list_all(session: DbDep, redis: RedisDep, box: SecretBoxDep) -> list[DestinationOut]:
    rows = list_destinations(session)
    runtime = await load_runtime(redis, [r.id for r in rows if r.id is not None])
    return [_to_out(r, box, runtime.get(r.id or -1)) for r in rows]


@router.post("", response_model=DestinationOut, status_code=status.HTTP_201_CREATED)
async def create(
    body: DestinationCreate, session: DbDep, redis: RedisDep, box: SecretBoxDep
) -> DestinationOut:
    dest = Destination(
        name=body.name,
        platform=body.platform,
        rtmp_url=body.rtmp_url,
        enabled=body.enabled,
        profile=body.profile,
        ffmpeg_args=body.ffmpeg_args,
    )
    set_stream_key(dest, box, body.stream_key)
    session.add(dest)
    session.commit()
    session.refresh(dest)
    log.info("destination.created", id=dest.id, name=dest.name, platform=dest.platform)
    await publish_event(redis, EVENT_DESTINATIONS_CHANGED, id=dest.id, op="create")
    return _to_out(dest, box, None)


@router.get("/{dest_id}", response_model=DestinationOut)
async def get_one(
    dest_id: int, session: DbDep, redis: RedisDep, box: SecretBoxDep
) -> DestinationOut:
    dest = _get_or_404(session, dest_id)
    runtime = await load_runtime(redis, [dest_id])
    return _to_out(dest, box, runtime.get(dest_id))


@router.patch("/{dest_id}", response_model=DestinationOut)
async def update(
    dest_id: int,
    body: DestinationUpdate,
    session: DbDep,
    redis: RedisDep,
    box: SecretBoxDep,
) -> DestinationOut:
    dest = _get_or_404(session, dest_id)
    changes = body.model_dump(exclude_unset=True)
    stream_key = changes.pop("stream_key", None)
    for field, value in changes.items():
        setattr(dest, field, value)
    if stream_key is not None:
        set_stream_key(dest, box, stream_key)
    if dest.profile == "custom" and not dest.ffmpeg_args:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="profile 'custom' requires ffmpeg_args"
        )
    dest.updated_at = utcnow()
    session.add(dest)
    session.commit()
    session.refresh(dest)
    log.info(
        "destination.updated",
        id=dest.id,
        fields=sorted(changes) + (["stream_key"] if stream_key is not None else []),
    )
    await publish_event(redis, EVENT_DESTINATIONS_CHANGED, id=dest.id, op="update")
    runtime = await load_runtime(redis, [dest_id])
    return _to_out(dest, box, runtime.get(dest_id))


@router.delete("/{dest_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(dest_id: int, session: DbDep, redis: RedisDep) -> Response:
    dest = _get_or_404(session, dest_id)
    session.delete(dest)
    session.commit()
    log.info("destination.deleted", id=dest_id)
    await publish_event(redis, EVENT_DESTINATIONS_CHANGED, id=dest_id, op="delete")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
