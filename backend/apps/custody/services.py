"""
收件凭证领域服务

所有写操作都以事务 + 收件单锁串行化，并遵循 append-only 原则：
首件 / 补件 / 作废只产生新版本，从不修改或删除既有清单行。

版本 N 的“整单清单”统一定义为 version_no <= N 的全部清单行
（补件只追加，故后续版本天然包含此前内容；作废版本不新增清单行，
其指纹等于作废时刻的整单快照）。复核时可重算哈希与 manifest_hash 比对。
"""
import hashlib
import time
import uuid
from dataclasses import dataclass

from django.db import OperationalError, connection, transaction
from django.utils import timezone

from apps.core.exceptions import BusinessException, NotFoundException

from .models import (
    CHANGE_INITIAL,
    CHANGE_REVOCATION,
    CHANGE_SUPPLEMENT,
    IntakeAttachment,
    IntakeMutex,
    IntakePacket,
    IntakeVersion,
    VERSION_ACTIVE,
    VERSION_REVOKED,
    VERSION_SUPERSEDED,
)

# 单文件提交判定：新增 / 内容重复 / 同名不同内容
RESULT_ADDED = 'added'
RESULT_DUPLICATE = 'duplicate'
RESULT_NAME_CONFLICT = 'name_conflict'

# SQLite busy_timeout（settings 中配置为 20s）之外的应用层兜底重试
_LOCK_WAIT_ATTEMPTS = 200
_LOCK_WAIT_INTERVAL = 0.05


@dataclass(frozen=True)
class FileDigest:
    """待登记文件摘要（保管服务接收摘要，亦可由上传字节现场算出）。"""

    file_name: str
    file_size: int
    sha256: str


def digest_upload(uploaded_file):
    """从上传字节流计算大小与 SHA-256，不信任客户端自报值。"""
    hasher = hashlib.sha256()
    size = 0
    if hasattr(uploaded_file, 'chunks'):
        for chunk in uploaded_file.chunks():
            hasher.update(chunk)
            size += len(chunk)
    else:
        data = uploaded_file.read()
        hasher.update(data)
        size = len(data)
    return FileDigest(
        file_name=uploaded_file.name,
        file_size=size,
        sha256=hasher.hexdigest(),
    )


def generate_packet_no():
    return f'NB{timezone.now():%Y%m%d}{uuid.uuid4().hex[:8].upper()}'


def _run_with_packet_lock(packet_no, work):
    """持有收件单写锁执行 work；SQLite 下在事务外重试。

    定位锁行与加锁合并为单条写语句（按 packet_no 关联子查询），
    它必须是事务内第一条语句：SQLite deferred 事务首条即写时直接
    取得 RESERVED 锁，避免“先读后升级”造成 database is locked
    （该错误会污染事务、无法在事务内重试）。行不存在时照常执行
    work，由其内部的收件单查询给出 404。
    """
    if connection.features.has_select_for_update:
        with transaction.atomic():
            list(
                IntakeMutex.objects.select_for_update()
                .filter(packet__packet_no=packet_no)
            )
            return work()

    last_error = None
    for _ in range(_LOCK_WAIT_ATTEMPTS):
        try:
            with transaction.atomic():
                # 首条语句即写锁锚点；被占用时先由 busy_timeout 等待
                IntakeMutex.objects.filter(packet__packet_no=packet_no).update(
                    touched_at=timezone.now()
                )
                return work()
        except OperationalError as exc:
            if 'locked' not in str(exc).lower():
                raise
            last_error = exc
            time.sleep(_LOCK_WAIT_INTERVAL)
    raise BusinessException(
        f'收件单正被其他提交占用，请稍后重试: {last_error}', code=409
    )


def _hash_rows(rows):
    rows = sorted(rows, key=lambda row: row[0])
    joined = '\n'.join(f'{sha}|{size}|{name}' for sha, size, name in rows)
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()


def _manifest_hash(packet, version_no, new_items=()):
    """创建时的整单指纹：严格早于 version_no 的既有行 + 本次新增行。

    调用处于收件单锁内，new_items 尚未入库。
    """
    rows = list(
        IntakeAttachment.objects.filter(
            packet=packet, version__version_no__lt=version_no
        ).values_list('sha256', 'file_size', 'file_name')
    )
    rows.extend((item.sha256, item.file_size, item.file_name) for item in new_items)
    return _hash_rows(rows)


def persisted_manifest_hash(packet, version_no):
    """重算任意既有版本时点的整单指纹，用于复核 manifest_hash。"""
    rows = (
        IntakeAttachment.objects.filter(
            packet=packet, version__version_no__lte=version_no
        )
        .values_list('sha256', 'file_size', 'file_name')
    )
    return _hash_rows(list(rows))


def _submitter_snapshot(user):
    return {
        'submitted_by': user,
        'submitted_by_name': user.real_name or user.username,
    }


def _normalize(files):
    result = []
    for item in files:
        sha = str(item.sha256).strip().lower()
        result.append(FileDigest(
            file_name=str(item.file_name),
            file_size=int(item.file_size),
            sha256=sha,
        ))
    return result


def _classify(packet, files):
    """持锁状态下逐文件判定 added / duplicate / name_conflict。

    - 同 SHA-256：字节完全相同，一律视为内容重复（即使文件名不同，
      结果中带回首次登记的文件名与版本）；
    - 新哈希但文件名已被占用：同名不同内容，拒绝该文件；
    - 同一批次内部适用相同规则。
    """
    known = {
        row.sha256: row
        for row in IntakeAttachment.objects.filter(packet=packet).select_related('version')
    }
    known_names = {row.file_name: row for row in known.values()}

    batch_sha = set()
    batch_names = set()
    new_items = []
    outcomes = []

    for digest in files:
        if digest.sha256 in known or digest.sha256 in batch_sha:
            row = known.get(digest.sha256)
            outcome = {
                'file_name': digest.file_name,
                'file_size': digest.file_size,
                'sha256': digest.sha256,
                'result': RESULT_DUPLICATE,
            }
            if row is not None:
                outcome.update({
                    'registered_name': row.file_name,
                    'registered_version_no': row.version.version_no,
                })
            outcomes.append(outcome)
            continue

        if digest.file_name in known_names or digest.file_name in batch_names:
            row = known_names.get(digest.file_name)
            outcome = {
                'file_name': digest.file_name,
                'file_size': digest.file_size,
                'sha256': digest.sha256,
                'result': RESULT_NAME_CONFLICT,
            }
            if row is not None:
                outcome.update({
                    'existing_sha256': row.sha256,
                    'existing_version_no': row.version.version_no,
                })
            outcomes.append(outcome)
            continue

        new_items.append(digest)
        batch_sha.add(digest.sha256)
        batch_names.add(digest.file_name)
        outcomes.append({
            'file_name': digest.file_name,
            'file_size': digest.file_size,
            'sha256': digest.sha256,
            'result': RESULT_ADDED,
        })

    return new_items, outcomes


def _create_version(packet, version_no, change_type, status, submitter, remark, new_items):
    """创建版本与清单行；整单指纹在插入前一次算好，创建后不再改动。

    唯一约束冲突（并发下版本号或 packet+sha256 被抢占）统一转成 409。
    """
    snapshot = _submitter_snapshot(submitter)
    version = IntakeVersion(
        packet=packet,
        version_no=version_no,
        change_type=change_type,
        status=status,
        remark=remark or '',
        manifest_hash=_manifest_hash(packet, version_no, new_items),
        **snapshot,
    )
    try:
        version.save()
        IntakeAttachment.objects.bulk_create(
            [
                IntakeAttachment(
                    version=version,
                    packet=packet,
                    file_name=item.file_name,
                    file_size=item.file_size,
                    sha256=item.sha256,
                    biz_type=packet.biz_type,
                    biz_ref=packet.biz_ref,
                    **snapshot,
                )
                for item in new_items
            ]
        )
    except Exception:
        raise BusinessException('并发提交冲突，版本号或文件内容已被占用，请重试', code=409)
    return version


@transaction.atomic
def create_packet(*, doc_type, title, biz_type, biz_ref, submitter, files, biz_label=''):
    """首次收件：建立收件锚点、锁与 v1 清单。"""
    files = _normalize(files)
    if not files:
        raise BusinessException('首次收件至少需要一个文件摘要')

    packet = IntakePacket.objects.create(
        packet_no=generate_packet_no(),
        doc_type=doc_type,
        title=title,
        biz_type=biz_type,
        biz_ref=biz_ref,
        biz_label=biz_label or '',
        created_by=submitter,
    )
    IntakeMutex.objects.create(packet=packet)

    new_items, outcomes = _classify(packet, files)
    version = _create_version(
        packet, 1, CHANGE_INITIAL, VERSION_ACTIVE, submitter, '首次收件', new_items
    )
    return _submit_result(packet, version, outcomes, version_created=True)


def supplement_packet(*, packet_no, submitter, files, remark=''):
    """补件：同名不同内容拒绝；相同内容记为重复；新内容生成新版本。"""
    files = _normalize(files)
    if not files:
        raise BusinessException('补件至少需要一个文件摘要')

    def work():
        packet = _get_packet(packet_no)
        current = packet.current_version
        if current is not None and current.status == VERSION_REVOKED:
            raise BusinessException('收件单已作废，不能补件；请重新登记收件')

        new_items, outcomes = _classify(packet, files)
        if not new_items:
            # 全部为重复 / 冲突：不制造空版本，逐文件返回明确结果
            return _submit_result(packet, current, outcomes, version_created=False)

        next_no = (current.version_no if current else 0) + 1
        version = _create_version(
            packet, next_no, CHANGE_SUPPLEMENT, VERSION_ACTIVE,
            submitter, remark or '补件', new_items
        )
        if current is not None and current.status == VERSION_ACTIVE:
            current.status = VERSION_SUPERSEDED
            current.save(update_fields=['status'])
        return _submit_result(packet, version, outcomes, version_created=True)

    return _run_with_packet_lock(packet_no, work)


def revoke_packet(*, packet_no, submitter, remark=''):
    """作废：当前有效版本迁移为 revoked，并追加作废版本留存事实。"""

    def work():
        packet = _get_packet(packet_no)
        current = packet.current_version
        if current is None:
            raise BusinessException('收件单尚无版本，无法作废', code=409)
        if current.status == VERSION_REVOKED:
            raise BusinessException('收件单已作废，不能重复作废', code=409)

        prior_status = current.status
        current.status = VERSION_REVOKED
        current.save(update_fields=['status'])

        next_no = current.version_no + 1
        version = IntakeVersion(
            packet=packet,
            version_no=next_no,
            change_type=CHANGE_REVOCATION,
            status=VERSION_REVOKED,
            remark=remark or '整单作废',
            manifest_hash=_manifest_hash(packet, next_no),
            **_submitter_snapshot(submitter),
        )
        version.save()

        return {
            'packet_no': packet.packet_no,
            'version_no': version.version_no,
            'change_type': version.change_type,
            'status': version.status,
            'prior_status': prior_status,
            'revoked_at': version.created_at,
            'manifest_hash': version.manifest_hash,
        }

    return _run_with_packet_lock(packet_no, work)


def verify_digest(*, packet_no, version_no, sha256):
    """验证某文件摘要是否属于指定版本，并说明其当前效力。"""
    packet = _get_packet(packet_no)
    try:
        version = packet.versions.get(version_no=version_no)
    except IntakeVersion.DoesNotExist:
        raise NotFoundException(f'版本 v{version_no} 不存在')

    digest = str(sha256).strip().lower()
    attachment = IntakeAttachment.objects.filter(
        packet=packet, version=version, sha256=digest
    ).first()
    current = packet.current_version
    first_seen = IntakeAttachment.objects.filter(
        packet=packet, sha256=digest
    ).select_related('version').first()

    return {
        'packet_no': packet.packet_no,
        'version_no': version.version_no,
        'sha256': digest,
        'belongs': attachment is not None,
        'file_name': attachment.file_name if attachment else None,
        'file_size': attachment.file_size if attachment else None,
        'version_status': version.status,
        'in_current_version': bool(
            first_seen and current and first_seen.version.version_no <= current.version_no
        ),
        'current_version_no': current.version_no if current else None,
        'current_status': current.status if current else None,
        'first_registered_version_no': first_seen.version.version_no if first_seen else None,
        'manifest_match': (
            persisted_manifest_hash(packet, version.version_no) == version.manifest_hash
        ),
    }


def list_versions(packet_no):
    """返回版本链及每个版本时点的整单清单切片。"""
    packet = _get_packet(packet_no)
    versions = list(packet.versions.order_by('version_no'))
    result = []
    for version in versions:
        lines = (
            IntakeAttachment.objects
            .filter(packet=packet, version__version_no__lte=version.version_no)
            .order_by('sha256')
        )
        result.append({
            'version_no': version.version_no,
            'change_type': version.change_type,
            'status': version.status,
            'submitted_by_name': version.submitted_by_name,
            'remark': version.remark,
            'manifest_hash': version.manifest_hash,
            'created_at': version.created_at,
            'attachments': [
                {
                    'file_name': line.file_name,
                    'file_size': line.file_size,
                    'sha256': line.sha256,
                    'submitted_by_name': line.submitted_by_name,
                    'biz_type': line.biz_type,
                    'biz_ref': line.biz_ref,
                    'introduced_in_version': line.version.version_no,
                    'received_at': line.created_at,
                }
                for line in lines
            ],
        })
    return result


def get_packet(packet_no):
    return _get_packet(packet_no)


def packet_summary(packet):
    current = packet.current_version
    return {
        'packet_no': packet.packet_no,
        'doc_type': packet.doc_type,
        'title': packet.title,
        'biz_type': packet.biz_type,
        'biz_ref': packet.biz_ref,
        'biz_label': packet.biz_label,
        'created_by_name': packet.created_by.real_name or packet.created_by.username,
        'created_at': packet.created_at,
        'current_version_no': current.version_no if current else None,
        'current_status': current.status if current else None,
    }


def _get_packet(packet_no):
    try:
        return IntakePacket.objects.get(packet_no=packet_no)
    except IntakePacket.DoesNotExist:
        raise NotFoundException('收件登记不存在')


def _submit_result(packet, version, outcomes, *, version_created):
    counts = {RESULT_ADDED: 0, RESULT_DUPLICATE: 0, RESULT_NAME_CONFLICT: 0}
    for item in outcomes:
        counts[item['result']] += 1
    return {
        'packet_no': packet.packet_no,
        'version_no': version.version_no if version_created else None,
        'current_version_no': version.version_no if version else None,
        'version_created': version_created,
        'status': version.status if version else None,
        'biz_type': packet.biz_type,
        'biz_ref': packet.biz_ref,
        'manifest_hash': version.manifest_hash if version else None,
        'files': outcomes,
        'counts': counts,
    }
