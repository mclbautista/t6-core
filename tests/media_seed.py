"""Seed a Media location the way a person does: through `media link`, preview then confirm."""
from t6.media import MediaStore
from t6.preview import PreviewTokens
from t6.registry.sqlite import SqliteProjectRegistry
from t6.verbs import Done, Preview


def link_media(registry: SqliteProjectRegistry, owner: str, ref: str, *, kind: str = 'nas-path', role: str = 'master', actor: str = 'setup') -> str:
    """Link `ref` to `owner` (a Project or Deliverable ID) and return the Media location's ID."""
    store = MediaStore(registry, PreviewTokens('seed-signing'))
    values = {'owner': owner, 'kind': kind, 'ref': ref, 'role': role}
    key = f'seed:{owner}:{kind}:{ref}:{role}'
    preview = store.write('link', values, actor=actor, idempotency_key=key)
    assert isinstance(preview, Preview), preview
    done = store.write('link', values, actor=actor, idempotency_key=key, confirm=preview.token)
    assert isinstance(done, Done), done
    return str(done.result['media_id'])
