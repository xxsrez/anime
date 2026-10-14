"""Explicit episode completion, with idempotent requests and guarded undo."""
import datetime as dt
import json
import uuid


SCHEMA = """create table if not exists user_manual_episode_completions (
    user_id integer not null references users(id) on delete cascade,
    operation_id text not null,
    anime_id integer not null references anime(id) on delete cascade,
    episode_id integer not null references episodes(id) on delete cascade,
    member_ids text not null,
    before_state text not null,
    after_state text not null,
    created_at text not null,
    undone_at text,
    primary key (user_id, operation_id)
)"""


def ensure_schema(con):
    existed = con.execute("select 1 from sqlite_master where name = 'user_manual_episode_completions'").fetchone()
    con.execute(SCHEMA)
    return not existed


def snapshot(con, user_id, members):
    placeholders = ','.join('?' for _ in members)
    result = {}
    for table in ('user_title_state', 'user_episode_state'):
        result[table] = [dict(row) for row in con.execute(
            f'select * from {table} where user_id = ? and anime_id in ({placeholders}) order by rowid',
            (user_id, *members),
        )]
    result['event_id'] = con.execute(
        f'''select coalesce(max(id), 0) from user_watch_events
            where user_id = ? and anime_id in ({placeholders})
            and (engaged_seconds > 0 or event_type in ('player_engaged', 'fullscreen_enter', 'pip_open'))''',
        (user_id, *members),
    ).fetchone()[0]
    return result


def record(payload, db_path, user_id, core):
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object')
    try:
        operation_id = str(uuid.UUID(payload.get('operation_id', '')))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('operation_id must be a UUID') from None
    action = payload.get('action', 'complete')
    if action not in ('complete', 'undo'):
        raise ValueError('invalid action')
    con = core.connect(db_path)
    try:
        user_id = core.require_user_id(con, user_id)
        # Cache construction may persist catalog metadata on another connection.
        # Resolve before acquiring the user-state write transaction, as playback does.
        group = core.canonical_group_for_anime_ref(con, payload.get('anime_id'), user_id) if action == 'complete' else None
        con.execute('begin immediate')
        operation = con.execute(
            'select * from user_manual_episode_completions where user_id = ? and operation_id = ?',
            (user_id, operation_id),
        ).fetchone()
        if action == 'undo':
            if not operation:
                raise ValueError('Отметка не найдена')
            if not operation['undone_at']:
                members = json.loads(operation['member_ids'])
                if snapshot(con, user_id, members) != json.loads(operation['after_state']):
                    raise ValueError('После отметки прогресс изменился. Отмена затронула бы более новые изменения.')
                before = json.loads(operation['before_state'])
                placeholders = ','.join('?' for _ in members)
                for table in ('user_title_state', 'user_episode_state'):
                    con.execute(f'delete from {table} where user_id = ? and anime_id in ({placeholders})', (user_id, *members))
                    for row in before[table]:
                        columns = ','.join(row)
                        values = ','.join('?' for _ in row)
                        con.execute(f'insert into {table} ({columns}) values ({values})', tuple(row.values()))
                con.execute('update user_manual_episode_completions set undone_at = ? where user_id = ? and operation_id = ?',
                            (core.now_iso(), user_id, operation_id))
            con.commit()
            return {'ok': True, 'operation_id': operation_id, 'undone': True}

        if payload.get('confirmed') is not True or type(payload.get('episode_id')) is not int:
            raise ValueError('Нужно подтвердить конкретную серию')
        if not group:
            raise ValueError('anime_id is invalid')
        members = group.get('source_member_ids') or [group['id']]
        episode = core.load_event_episode(con, members, payload)
        if operation:
            if operation['anime_id'] != group['id'] or operation['episode_id'] != episode['id']:
                raise ValueError('operation_id already used')
            return {'ok': True, 'operation_id': operation_id, 'undone': bool(operation['undone_at'])}
        before = snapshot(con, user_id, members)
        key = core.episode_number_key(episode['number'], episode['id'])
        if any(row['completed_at'] and core.episode_number_key(row['episode_number'], row['episode_id']) == key
               for row in before['user_episode_state']):
            return {'ok': True, 'already_completed': True}
        timestamp = core.now_iso()
        number = core.episode_progress_number(episode['number'])
        current = core.get_group_state(con, members, user_id)
        progress = max(number or 0, current.get('progress_episode_number') or 0)
        core.upsert_episode_watch_state(
            con, user_id=user_id, anime_id=group['id'], episode_id=episode['id'],
            timestamp=timestamp, event_type='manual_completed', confidence=1.0,
            engaged_seconds=0, heartbeat_count=0, started=True, playback_ended=True,
            episode_number=episode['number'], progress_episode_number=number,
            video_source_id=None, source=None, source_anime_id=None,
            translation_id=None, translation_title=None, provider_id=None, provider_title=None, embed_host=None,
        )
        later = [row for row in before['user_episode_state'] if row['started_at']
                 and (row['progress_episode_number'] or 0) > (number or 0)]
        if later:
            last_seen = max(row['last_seen_at'] for row in later)
            older = (dt.datetime.fromisoformat(last_seen) - dt.timedelta(seconds=1)).isoformat(timespec='seconds')
            con.execute('update user_episode_state set last_seen_at = ? where user_id = ? and anime_id = ? and episode_id = ?',
                        (older, user_id, group['id'], episode['id']))
            progress = max(progress, *(row['progress_episode_number'] for row in later))
        core.apply_watch_progress_to_user_state(con, group, user_id, progress, timestamp, 'manual_completed')
        core.reconcile_completed_titles(con, user_id, [group])
        after = snapshot(con, user_id, members)
        con.execute('''insert into user_manual_episode_completions
            (user_id, operation_id, anime_id, episode_id, member_ids, before_state, after_state, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, operation_id, group['id'], episode['id'], json.dumps(members),
             json.dumps(before), json.dumps(after), timestamp))
        con.commit()
        return {'ok': True, 'operation_id': operation_id, 'undone': False}
    finally:
        con.close()
