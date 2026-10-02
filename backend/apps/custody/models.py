"""
收件凭证模型

同一份收件（鉴定报告 / 移交文书等）的全部历史以“仅追加”方式保存：

- ``IntakePacket``    一次收件的业务锚点，对外以 packet_no 稳定引用；
- ``IntakeVersion``   首件、补件、作废均产生新版本，旧版本只做状态迁移，不被覆盖；
- ``IntakeAttachment`` 每次收件逐行记录文件名、大小、SHA-256、提交人、业务快照，
                      创建后即冻结（模型层禁止修改与删除）。
"""
from django.conf import settings
from django.db import models
from django.db.models.signals import pre_save, pre_delete
from django.dispatch import receiver


# 收件材料类别
DOC_APPRAISAL = 'appraisal_report'   # 鉴定报告
DOC_HANDOVER = 'handover_document'   # 移交文书

DOC_TYPE_CHOICES = [
    (DOC_APPRAISAL, '鉴定报告'),
    (DOC_HANDOVER, '移交文书'),
]

# 版本状态：当版有效 / 已被新版本取代 / 已作废
VERSION_ACTIVE = 'active'
VERSION_SUPERSEDED = 'superseded'
VERSION_REVOKED = 'revoked'

VERSION_STATUS_CHOICES = [
    (VERSION_ACTIVE, '有效'),
    (VERSION_SUPERSEDED, '已失效'),
    (VERSION_REVOKED, '已作废'),
]

# 版本变更性质
CHANGE_INITIAL = 'initial'   # 首次收件
CHANGE_SUPPLEMENT = 'supplement'  # 补件
CHANGE_REVOCATION = 'revocation'  # 作废

CHANGE_TYPE_CHOICES = [
    (CHANGE_INITIAL, '首次收件'),
    (CHANGE_SUPPLEMENT, '补件'),
    (CHANGE_REVOCATION, '作废'),
]


class FrozenModelError(Exception):
    """清单行 / 版本内容在创建后被尝试修改或删除。"""


class _FrozenQuerySet(models.QuerySet):
    """对 ORM 批量 update/delete 实施与信号一致的冻结策略。

    ``_frozen_fields`` 为 None 表示整记录冻结（任何字段都不能 update）；
    为集合时仅冻结集合内字段（版本因此仍可迁移 status）。
    """

    _frozen_fields = None
    _allow_delete = False

    def update(self, **kwargs):
        if self._frozen_fields is None:
            if kwargs:
                raise FrozenModelError(
                    f'{self.model.__name__} 为不可变记录，禁止修改: {sorted(kwargs)}'
                )
        else:
            forbidden = set(kwargs) & self._frozen_fields
            if forbidden:
                raise FrozenModelError(
                    f'{self.model.__name__} 不可变，禁止修改字段: {sorted(forbidden)}'
                )
        return super().update(**kwargs)

    def delete(self):
        if not self._allow_delete:
            raise FrozenModelError(f'{self.model.__name__} 为不可变记录，禁止删除')
        return super().delete()


def frozen_manager(*, fields=None, allow_delete=False):
    """构造带冻结策略的管理器（策略固化在 QuerySet 子类上，clone 不丢失）。"""

    class _ScopedQuerySet(_FrozenQuerySet):
        _frozen_fields = fields
        _allow_delete = allow_delete

    class _Manager(models.Manager.from_queryset(_ScopedQuerySet)):
        pass

    return _Manager()


class IntakePacket(models.Model):
    """一次收件登记：版本链的稳定业务锚点。"""

    # 对外引用标识：创建后不再变更，业务记录改名 / 换主键均不影响其效力
    packet_no = models.CharField('收件编号', max_length=40, unique=True)
    doc_type = models.CharField('材料类别', max_length=30, choices=DOC_TYPE_CHOICES)
    title = models.CharField('收件标题', max_length=200)

    # 关联业务以字符串标识快照保存，避免业务记录变更导致凭证外键失效
    biz_type = models.CharField('关联业务类型', max_length=50)
    biz_ref = models.CharField('关联业务标识', max_length=100)
    biz_label = models.CharField('关联业务说明', max_length=200, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='intake_packets', verbose_name='登记人'
    )
    created_at = models.DateTimeField('登记时间', auto_now_add=True)

    objects = frozen_manager()

    class Meta:
        db_table = 'cd_intake_packet'
        verbose_name = '收件登记'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.packet_no} - {self.title}'

    @property
    def current_version(self):
        """当前生效版本（版本号最大者）。"""
        return self.versions.order_by('-version_no').first()


class IntakeVersion(models.Model):
    """收件清单的一个不可变版本。"""

    packet = models.ForeignKey(
        IntakePacket, on_delete=models.PROTECT,
        related_name='versions', verbose_name='收件登记'
    )
    version_no = models.PositiveIntegerField('版本号')
    change_type = models.CharField(
        '变更性质', max_length=20, choices=CHANGE_TYPE_CHOICES, default=CHANGE_INITIAL
    )
    # active / superseded / revoked：仅允许沿状态机迁移，清单内容字段永不变更
    status = models.CharField(
        '版本状态', max_length=20, choices=VERSION_STATUS_CHOICES, default=VERSION_ACTIVE
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='intake_versions', verbose_name='提交人'
    )
    submitted_by_name = models.CharField('提交人姓名（快照）', max_length=50)
    remark = models.CharField('备注', max_length=255, blank=True)
    manifest_hash = models.CharField('整单清单哈希', max_length=64, blank=True, default='')
    created_at = models.DateTimeField('提交时间', auto_now_add=True)

    objects = frozen_manager(fields=frozenset((
        'packet_id', 'version_no', 'change_type', 'submitted_by_id',
        'submitted_by_name', 'remark', 'manifest_hash',
    )))

    class Meta:
        db_table = 'cd_intake_version'
        verbose_name = '收件版本'
        verbose_name_plural = verbose_name
        ordering = ['packet_id', '-version_no']
        constraints = [
            models.UniqueConstraint(
                fields=['packet', 'version_no'], name='cd_uniq_packet_version_no'
            ),
        ]

    def __str__(self):
        return f'{self.packet_id} v{self.version_no}'


class IntakeMutex(models.Model):
    """收件单版本链的串行化锁锚点（每个收件单一行，不对外暴露）。

    补件 / 作废事务的第一步即写此行：PostgreSQL 表现为行锁阻塞，
    SQLite 借助写锁（RESERVED）与 busy_timeout 让并发提交排队。
    """

    packet = models.OneToOneField(
        IntakePacket, primary_key=True, related_name='mutex',
        on_delete=models.CASCADE, verbose_name='收件登记'
    )
    touched_at = models.DateTimeField('最近锁定时间', auto_now=True)

    class Meta:
        db_table = 'cd_intake_mutex'
        verbose_name = '收件版本锁'
        verbose_name_plural = verbose_name


class IntakeAttachment(models.Model):
    """清单行：一个文件在某次提交时的不可变摘要。"""

    version = models.ForeignKey(
        IntakeVersion, on_delete=models.PROTECT,
        related_name='attachments', verbose_name='所属版本'
    )
    packet = models.ForeignKey(
        IntakePacket, on_delete=models.PROTECT,
        related_name='attachments', verbose_name='收件登记'
    )
    file_name = models.CharField('文件名', max_length=255)
    file_size = models.PositiveBigIntegerField('文件大小（字节）')
    sha256 = models.CharField('SHA-256', max_length=64)
    # 逐行冻结提交人，即便同名账号后续变更也能证明当时由谁提交
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='intake_attachments', verbose_name='提交人'
    )
    submitted_by_name = models.CharField('提交人姓名（快照）', max_length=50)
    # 关联业务随清单行一并快照
    biz_type = models.CharField('关联业务类型', max_length=50)
    biz_ref = models.CharField('关联业务标识', max_length=100)
    created_at = models.DateTimeField('接收时间', auto_now_add=True)

    objects = frozen_manager()

    class Meta:
        db_table = 'cd_intake_attachment'
        verbose_name = '收件文件摘要'
        verbose_name_plural = verbose_name
        ordering = ['packet_id', '-version_id', 'id']
        constraints = [
            # 同一收件单内相同内容只登记一次，重复上传命中既有清单行
            models.UniqueConstraint(
                fields=['packet', 'sha256'], name='cd_uniq_packet_sha256'
            ),
        ]

    def __str__(self):
        return f'{self.file_name}@{self.sha256[:12]}'


_IMMUTABLE_ATTACHMENT_FIELDS = frozenset((
    'version_id', 'packet_id', 'file_name', 'file_size', 'sha256',
    'submitted_by_id', 'submitted_by_name', 'biz_type', 'biz_ref',
))
_VERSION_CONTENT_FIELDS = frozenset((
    'packet_id', 'version_no', 'change_type', 'submitted_by_id',
    'submitted_by_name', 'remark', 'manifest_hash',
))


@receiver(pre_save, sender=IntakeAttachment)
def _freeze_attachment_on_save(sender, instance, raw=False, **kwargs):
    """清单行一经创建即冻结：拒绝任何 UPDATE。"""
    if raw or instance.pk is None:
        return
    try:
        persisted = sender.objects.values(*_IMMUTABLE_ATTACHMENT_FIELDS).get(pk=instance.pk)
    except sender.DoesNotExist:
        # 显式指定主键的新增，允许插入
        return
    for field in _IMMUTABLE_ATTACHMENT_FIELDS:
        if getattr(instance, field) != persisted[field]:
            raise FrozenModelError(
                f'收件清单行不可变，禁止修改（{instance.pk}#{field}）'
            )


@receiver(pre_delete, sender=IntakeAttachment)
def _forbid_attachment_delete(sender, instance, **kwargs):
    raise FrozenModelError('收件清单行不可删除，只能通过新版本作废')


_ALLOWED_VERSION_STATUS_TRANSITIONS = {
    VERSION_ACTIVE: {VERSION_SUPERSEDED, VERSION_REVOKED},
    VERSION_SUPERSEDED: {VERSION_REVOKED},
    VERSION_REVOKED: set(),
}


@receiver(pre_save, sender=IntakeVersion)
def _guard_version_on_save(sender, instance, raw=False, **kwargs):
    """版本内容字段冻结；status 只允许沿 active -> superseded / revoked 迁移。"""
    if raw or instance.pk is None:
        return
    try:
        persisted = sender.objects.values(
            'status', *_VERSION_CONTENT_FIELDS
        ).get(pk=instance.pk)
    except sender.DoesNotExist:
        return
    for field in _VERSION_CONTENT_FIELDS:
        if getattr(instance, field) != persisted[field]:
            raise FrozenModelError(
                f'收件版本内容不可变，禁止修改（{instance.pk}#{field}）'
            )
    new_status = instance.status
    if new_status != persisted['status']:
        if new_status not in _ALLOWED_VERSION_STATUS_TRANSITIONS[persisted['status']]:
            raise FrozenModelError(
                f'版本状态不允许从 {persisted["status"]} 迁移到 {new_status}'
            )


@receiver(pre_delete, sender=IntakeVersion)
def _forbid_version_delete(sender, instance, **kwargs):
    raise FrozenModelError('收件版本不可删除，只能通过新版本表达作废')


@receiver(pre_save, sender=IntakePacket)
def _freeze_packet_on_save(sender, instance, raw=False, **kwargs):
    """收件锚点创建后冻结：编号、材料类别、关联业务等标识一律不得变更。"""
    if raw or instance.pk is None:
        return
    if not sender.objects.filter(pk=instance.pk).exists():
        return
    persisted = sender.objects.values(
        'packet_no', 'doc_type', 'title', 'biz_type', 'biz_ref',
        'biz_label', 'created_by_id'
    ).get(pk=instance.pk)
    for field, value in persisted.items():
        if getattr(instance, field) != value:
            raise FrozenModelError(
                f'收件登记不可变，禁止修改（{instance.pk}#{field}）'
            )


@receiver(pre_delete, sender=IntakePacket)
def _forbid_packet_delete(sender, instance, **kwargs):
    raise FrozenModelError('收件登记不可删除，只能通过新版本作废')
