"""
清单服务层：收件登记、作废、摘要验证。

所有写操作在事务内完成：先对业务记录加行锁，再分配版本号，
并以 (document, version_no) 唯一约束兜底；并发补件冲突时重试，
保证版本号连续唯一、清单内容不交叉。
"""
import hashlib
import time

from django.db import IntegrityError, OperationalError, transaction

from apps.core.exceptions import BusinessException
from .models import CustodyDocument, ManifestEntry, ManifestVersion

MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 0.05


def _is_lock_contention(exc):
    return 'locked' in str(exc).lower()


def _run_serialized(fn, conflict_message):
    """串行化执行清单写操作。

    并发补件撞上 (document, version_no) 唯一约束，或 SQLite 写锁
    竞争（database is locked）时按指数退避重试；重试耗尽后抛出
    明确的 409 业务异常，调用方得到确定结果而不是半完成状态。
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fn()
        except IntegrityError:
            continue
        except OperationalError as exc:
            if not _is_lock_contention(exc):
                raise
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
    raise BusinessException(conflict_message, code=409)


def compute_manifest_hash(*, doc_no, version_no, prev_hash, entries):
    """计算清单哈希：对版本内全部条目做规范化序列化后取 SHA-256。

    entries 为字典列表，键含 action/file_name/file_size/sha256/void_sha256。
    排序后序列化，与条目写入顺序无关，任何一方都可独立复算。
    """
    lines = [
        f"doc:{doc_no}",
        f"version:{version_no}",
        f"prev:{prev_hash or 'GENESIS'}",
    ]
    for e in sorted(entries, key=lambda i: (i['sha256'], i['file_name'], i['action'])):
        lines.append('|'.join([
            e['action'],
            e['file_name'],
            str(e['file_size']),
            e['sha256'],
            e.get('void_sha256') or '',
        ]))
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()


def get_active_entries(document):
    """当前有效条目：已登记且未被后续版本作废。"""
    voided_ids = (
        ManifestEntry.objects
        .filter(version__document=document, action='void')
        .values_list('voids_id', flat=True)
    )
    return (
        ManifestEntry.objects
        .filter(version__document=document, action='add')
        .exclude(pk__in=voided_ids)
        .order_by('id')
    )


def _next_version_no(document):
    last = (
        ManifestVersion.objects
        .filter(document=document)
        .order_by('-version_no')
        .values_list('version_no', flat=True)
        .first()
    )
    return (last or 0) + 1


def _create_version(*, document, reason, note, user, entry_specs):
    """在事务内创建清单版本与条目；调用方须已持有 document 行锁。"""
    version_no = _next_version_no(document)
    prev = (
        ManifestVersion.objects
        .filter(document=document)
        .order_by('-version_no')
        .first()
    )
    prev_hash = prev.manifest_hash if prev else ''
    manifest_hash = compute_manifest_hash(
        doc_no=document.doc_no,
        version_no=version_no,
        prev_hash=prev_hash,
        entries=entry_specs,
    )
    version = ManifestVersion.objects.create(
        document=document,
        version_no=version_no,
        reason=reason,
        note=note or '',
        doc_no_snapshot=document.doc_no,
        doc_title_snapshot=document.title,
        submitted_by=user,
        submitted_by_name=user.username if user else '',
        prev_hash=prev_hash,
        manifest_hash=manifest_hash,
    )
    entries = [
        ManifestEntry.objects.create(
            version=version,
            action=spec['action'],
            file_name=spec['file_name'],
            file_size=spec['file_size'],
            sha256=spec['sha256'],
            voids_id=spec.get('voids_id'),
        )
        for spec in entry_specs
    ]
    return version, entries


def _duplicate_info(file, existing, duplicate_of):
    # existing 为库内有效条目（模型）或本次请求中先出现的文件（字典）
    if isinstance(existing, dict):
        existing_id = existing_version_no = None
        existing_name, existing_size = existing['file_name'], existing['file_size']
    else:
        existing_id = existing.id
        existing_version_no = existing.version.version_no
        existing_name, existing_size = existing.file_name, existing.file_size
    return {
        'file_name': file['file_name'],
        'file_size': file['file_size'],
        'sha256': file['sha256'],
        'duplicate_of': duplicate_of,
        'existing_entry_id': existing_id,
        'existing_version_no': existing_version_no,
        'existing_file_name': existing_name,
        'existing_file_size': existing_size,
    }


def receive_files(*, document_id, files, note, user):
    """收件登记：为业务记录追加一个新的清单版本。

    返回 dict：
    - result='created'   已生成新版本，entries 为新登记条目；
    - result='duplicate' 本次文件全部与当前有效条目重复，未产生新版本。
    与有效条目 sha256 相同的文件视为重复上传，不重复登记；
    文件名与有效条目相同但内容不同的正常登记，并在 name_conflicts 中提示。
    """
    normalized = [
        {
            'file_name': f['file_name'],
            'file_size': f['file_size'],
            'sha256': f['sha256'].lower(),
        }
        for f in files
    ]

    def _receive():
        with transaction.atomic():
            document = (
                CustodyDocument.objects
                .select_for_update()
                .get(pk=document_id)
            )
            active = list(get_active_entries(document))
            active_by_hash = {}
            for entry in active:
                active_by_hash.setdefault(entry.sha256, entry)

            new_specs, duplicates = [], []
            seen_in_request = {}
            for f in normalized:
                existing = active_by_hash.get(f['sha256'])
                if existing is not None:
                    duplicates.append(_duplicate_info(f, existing, 'active'))
                    continue
                if f['sha256'] in seen_in_request:
                    duplicates.append(_duplicate_info(f, seen_in_request[f['sha256']], 'request'))
                    continue
                new_specs.append({'action': 'add', **f})
                seen_in_request[f['sha256']] = f

            if not new_specs:
                return {
                    'result': 'duplicate',
                    'version': None,
                    'entries': [],
                    'duplicates': duplicates,
                    'name_conflicts': [],
                }

            active_names = {}
            for entry in active:
                active_names.setdefault(entry.file_name, []).append(entry)
            name_conflicts = [
                {
                    'file_name': spec['file_name'],
                    'sha256': spec['sha256'],
                    'conflicting_entry_ids': [e.id for e in active_names[spec['file_name']]],
                    'conflicting_sha256': [e.sha256 for e in active_names[spec['file_name']]],
                }
                for spec in new_specs
                if spec['file_name'] in active_names
            ]

            reason = 'supplement' if document.versions.exists() else 'initial'
            version, entries = _create_version(
                document=document, reason=reason, note=note,
                user=user, entry_specs=new_specs,
            )
            return {
                'result': 'created',
                'version': version,
                'entries': entries,
                'duplicates': duplicates,
                'name_conflicts': name_conflicts,
            }

    return _run_serialized(_receive, '并发收件冲突，请稍后重试')


def invalidate_entries(*, document_id, entry_ids, note, user):
    """作废登记：以新版本表达对既有条目的作废，原摘要保持原样。

    返回 dict：
    - result='created'        已生成作废版本，voided_entry_ids 为本次作废的条目；
    - result='already_voided' 目标条目此前均已作废，未产生新版本。
    """
    # 请求内去重并保持顺序
    entry_ids = list(dict.fromkeys(entry_ids))

    def _invalidate():
        with transaction.atomic():
            document = (
                CustodyDocument.objects
                .select_for_update()
                .get(pk=document_id)
            )
            found = {
                e.id: e
                for e in ManifestEntry.objects.filter(
                    version__document=document, action='add', pk__in=entry_ids
                )
            }
            missing = [i for i in entry_ids if i not in found]
            if missing:
                raise BusinessException(f'清单条目不存在或不属于该业务记录: {missing}', code=400)

            active_ids = {e.id for e in get_active_entries(document)}
            to_void = [found[i] for i in entry_ids if i in active_ids]
            already_voided = [i for i in entry_ids if i not in active_ids]

            if not to_void:
                return {
                    'result': 'already_voided',
                    'version': None,
                    'entries': [],
                    'voided_entry_ids': [],
                    'already_voided_entry_ids': already_voided,
                }

            specs = [
                {
                    'action': 'void',
                    'file_name': e.file_name,
                    'file_size': e.file_size,
                    'sha256': e.sha256,
                    'voids_id': e.id,
                    'void_sha256': e.sha256,
                }
                for e in to_void
            ]
            version, entries = _create_version(
                document=document, reason='invalidation', note=note,
                user=user, entry_specs=specs,
            )
            return {
                'result': 'created',
                'version': version,
                'entries': entries,
                'voided_entry_ids': [e.id for e in to_void],
                'already_voided_entry_ids': already_voided,
            }

    return _run_serialized(_invalidate, '并发作废冲突，请稍后重试')


def verify_digest_in_version(*, document, version_no, sha256, file_name=None, file_size=None):
    """验证某个摘要是否属于指定版本。

    返回 {'matched', 'entries', 'mismatches'}；matched 只依据 sha256，
    若额外提供了文件名或大小，不一致的字段列入 mismatches。
    """
    try:
        version = ManifestVersion.objects.get(document=document, version_no=version_no)
    except ManifestVersion.DoesNotExist:
        raise BusinessException('清单版本不存在', code=404)

    matched = list(version.entries.filter(sha256=sha256.lower()))
    mismatches = []
    if matched:
        if file_name is not None and not any(e.file_name == file_name for e in matched):
            mismatches.append('file_name')
        if file_size is not None and not any(e.file_size == file_size for e in matched):
            mismatches.append('file_size')
    return {'matched': bool(matched), 'entries': matched, 'mismatches': mismatches}


def verify_digest_current(*, document, sha256):
    """验证某个摘要当前是否有效。

    status: active（当前有效）/ voided（曾登记但已作废）/ unknown（从未登记）。
    """
    sha256 = sha256.lower()
    active = get_active_entries(document).filter(sha256=sha256).first()
    if active is not None:
        return {'matched': True, 'status': 'active', 'entry': active, 'voided_in_version': None}

    historical = (
        ManifestEntry.objects
        .filter(version__document=document, action='add', sha256=sha256)
        .order_by('id')
        .first()
    )
    if historical is None:
        return {'matched': False, 'status': 'unknown', 'entry': None, 'voided_in_version': None}

    void_entry = (
        ManifestEntry.objects
        .filter(version__document=document, action='void', voids=historical)
        .order_by('id')
        .first()
    )
    return {
        'matched': False,
        'status': 'voided',
        'entry': historical,
        'voided_in_version': void_entry.version.version_no if void_entry else None,
    }
